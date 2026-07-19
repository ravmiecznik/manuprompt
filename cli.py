"""Command-line interface for ManuPrompt.

Usage::

    python -m manuprompt [options] path/to/suite.yml
    python -m manuprompt report RESULT.json [RESULT2.json ...] [-o OUT]

The default form loads a suite, executes it (prompting the operator for manual
steps), writes a JSON result, prints a summary and exits non-zero if any case
failed or errored. Pass ``-t/--test ID`` (repeatable) to run only specific test
cases by id. Every run also writes ``report.html`` and ``report.md`` beside its
``result.json``.

The ``report`` form (re)generates an HTML and/or Markdown report from one or
more saved ``result.json`` files without re-running anything. The format is
chosen by the ``-o`` extension (``.html`` or ``.md``); a directory (or no
``-o``) produces both. Multiple files are merged by test-case id; when the same
id appears in more than one file the operator is asked which run's result to
keep. Artifacts are copied next to the report so they display correctly.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from .errors import ManuPromptError
from .model import Suite
from .reporting import SKIP_CASE, CaseCandidate
from .results import Outcome, SuiteResult
from .api import generate_report, load_suite, run_suite
from .theme import effective_theme

# Case outcomes that make the process exit non-zero.
_FAILING_OUTCOMES: frozenset[Outcome] = frozenset({Outcome.FAIL, Outcome.ERROR})


def main(argv: list[str] | None = None) -> int:
    """Run the ManuPrompt CLI.

    Args:
        argv: Optional argument list. When ``None``, ``sys.argv`` is used.

    Returns:
        Process exit code: 0 on success, 1 if any case failed/errored, 2 on
        a usage or suite-loading error.
    """
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "report":
        return _cmd_report(_build_report_parser().parse_args(raw_args[1:]))
    return _cmd_run(_build_parser().parse_args(raw_args))


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Options precede the suite path, which is the final positional argument::

        python -m manuprompt -t DEMO001 [options] suite.yml
    """
    parser = argparse.ArgumentParser(
        prog="manuprompt",
        description="Run a YAML-defined, human-in-the-loop test suite.",
        epilog=(
            "subcommands:\n"
            "  report FILE [FILE ...] [-s SUITE.yml] [-o OUT]\n"
            "                        Generate an HTML and/or Markdown report "
            "from saved JSON\n"
            "                        result(s) without re-running (format from "
            "the -o extension;\n"
            "                        a directory or no -o produces both). "
            "Several JSON files are\n"
            "                        merged by test-case id\n"
            "                        (you choose which to keep on conflicts). "
            "Pass the suite\n"
            "                        YAML to show planned-but-not-run cases as "
            "NOT-TESTED.\n"
            "                        See 'manuprompt report -h' for details.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-t",
        "--test",
        action="append",
        default=[],
        dest="tests",
        metavar="ID",
        help="Run only these test case id(s), in the order given. Repeatable "
        "and/or comma-separated, e.g. -t DEMO001 -t DEMO002 or "
        "-t DEMO001,DEMO002 (default: run all, in suite order).",
    )
    parser.add_argument(
        "-k",
        "--keyword",
        dest="pattern",
        default=None,
        metavar="REGEX",
        help="Select test cases whose id or name matches this regular "
        "expression (case-insensitive, substring by default), e.g. -k DEMO. "
        "Can be combined with -t to further restrict the selection.",
    )
    parser.add_argument(
        "--out-dir",
        "--artifacts",
        dest="out_dir",
        type=Path,
        default=None,
        help="Directory all run output is written to — result.json, collected "
        "logs, attachments and the HTML report. Default: a timestamped dir "
        "under manuprompt-results/. (--artifacts is a deprecated alias.)",
    )
    parser.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Override or add a suite variable (repeatable).",
    )
    parser.add_argument(
        "--tool",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Override or add a tool path (repeatable).",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=None,
        metavar="PORT",
        help="TCP port for the live web console (default: an OS-chosen free "
        "port). Overrides MANUPROMPT_WEB_PORT.",
    )
    parser.add_argument(
        "--web-title",
        default=None,
        metavar="NAME",
        help="Display name for the live web console (default: the suite's "
        "web_title, else its name). Overrides MANUPROMPT_WEB_TITLE.",
    )
    parser.add_argument(
        "--no-web-console",
        action="store_true",
        help="Disable the live web console for this run.",
    )
    parser.add_argument(
        "--cli-mode",
        action="store_true",
        help="Answer operator prompts in the terminal instead of the browser. "
        "By default prompts are presented on the web session page; the web "
        "console and artifact upload stay available in either mode.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument("suite", type=Path, help="Path to the YAML suite file.")
    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    """Handle the ``run`` subcommand.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """
    logging.getLogger("manuprompt").setLevel(
        logging.DEBUG if args.verbose else logging.INFO
    )
    # Web-console flags override the environment-driven defaults read by
    # run_suite; translate them into the same environment knobs.
    if args.no_web_console:
        os.environ["MANUPROMPT_NO_WEB_CONSOLE"] = "1"
    if args.web_port is not None:
        os.environ["MANUPROMPT_WEB_PORT"] = str(args.web_port)
    if args.web_title is not None:
        os.environ["MANUPROMPT_WEB_TITLE"] = args.web_title
    try:
        suite = load_suite(args.suite)
        suite = _apply_overrides(suite, args.var, args.tool)
        if args.tests:
            suite = _filter_test_cases(suite, args.tests)
        if args.pattern:
            suite = _select_by_pattern(suite, args.pattern)
        # Validate the project-wide theme.yaml + this suite's override now, so
        # a mistake there fails cleanly here rather than mid-run.
        effective_theme(suite.theme)
    except ManuPromptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = run_suite(suite, artifacts_dir=args.out_dir, cli_mode=args.cli_mode)
    _print_summary(result)
    return 1 if result.outcome in _FAILING_OUTCOMES else 0


def _build_report_parser() -> argparse.ArgumentParser:
    """Construct the parser for the ``report`` subcommand."""
    parser = argparse.ArgumentParser(
        prog="manuprompt report",
        description="Generate an HTML and/or Markdown report from one or more "
        "JSON results, merging them by test-case id.",
    )
    parser.add_argument(
        "inputs",
        type=Path,
        nargs="+",
        metavar="FILE",
        help="One or more result.json files to render (merged when several), "
        "and optionally the suite .yml/.yaml so planned-but-not-run cases "
        "show as NOT-TESTED.",
    )
    parser.add_argument(
        "-s",
        "--suite",
        type=Path,
        default=None,
        metavar="SUITE.yml",
        help="Suite YAML; cases defined there but absent from the results are "
        "shown as NOT-TESTED. Overrides a .yml/.yaml given positionally.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Destination: an .html or .md file (format inferred from the "
        "extension), or a directory to hold both report.html and report.md. "
        "Default: a fresh manuprompt-report_<timestamp>/ (or manuprompt-merged_ "
        "when merging) bundle directory. A bundle directory also receives the "
        "(merged) result.json and a copy of every artifact, so it is "
        "self-contained and archivable.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


_YAML_SUFFIXES: frozenset[str] = frozenset({".yml", ".yaml"})

# Output suffixes that name a single report format (and so are used verbatim);
# any other ``-o`` value is treated as a bundle directory that receives both.
_REPORT_SUFFIXES: frozenset[str] = frozenset({".html", ".htm", ".md", ".markdown"})


def _split_report_inputs(
    inputs: list[Path], explicit_suite: Path | None
) -> tuple[list[Path], Path | None]:
    """Partition report inputs into result JSONs and an optional suite YAML.

    Args:
        inputs: Positional input paths (result JSONs and at most one YAML).
        explicit_suite: Suite path from ``-s/--suite``, if given (takes
            precedence over a positionally-supplied YAML).

    Returns:
        A ``(results, suite)`` pair.

    Raises:
        ManuPromptError: If no result JSON is given, or more than one YAML is.
    """
    results = [path for path in inputs if path.suffix.lower() not in _YAML_SUFFIXES]
    yamls = [path for path in inputs if path.suffix.lower() in _YAML_SUFFIXES]
    if len(yamls) > 1:
        raise ManuPromptError(
            f"expected at most one suite YAML, got {len(yamls)}: "
            f"{', '.join(str(y) for y in yamls)}"
        )
    if not results:
        raise ManuPromptError("expected at least one result.json file")
    suite = explicit_suite or (yamls[0] if yamls else None)
    return results, suite


def _cmd_report(args: argparse.Namespace) -> int:
    """Handle the ``report`` subcommand.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code (0 on success, 2 on a read/render error).
    """
    logging.getLogger("manuprompt").setLevel(
        logging.DEBUG if args.verbose else logging.INFO
    )
    try:
        results, suite = _split_report_inputs(args.inputs, args.suite)
        out, formats = _resolve_report_output(results, args.output)
        # Persist the merged JSON into the output dir to make it a self-contained
        # bundle, unless that would overwrite one of the input result files.
        inputs = {path.resolve() for path in results}
        save_json = (out.parent / "result.json").resolve() not in inputs
        generate_report(
            results,
            out,
            suite_path=suite,
            chooser=_console_chooser,
            save_json=save_json,
            formats=formats,
        )
    except (ManuPromptError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if formats is not None:
        print(f"report bundle written to {out.parent}")
    else:
        print(f"report written to {out}")
    return 0


def _resolve_report_output(
    results: list[Path], output: Path | None
) -> tuple[Path, set[str] | None]:
    """Determine the report output path and format(s) from the inputs and ``-o``.

    An explicit ``-o`` naming a report file (``.html``/``.htm`` → HTML,
    ``.md``/``.markdown`` → Markdown) is used verbatim and renders that single
    format. Any other ``-o`` value — or no ``-o`` at all — names a **bundle
    directory** that receives *both* ``report.html`` and ``report.md`` (plus the
    merged ``result.json`` and a copy of every artifact), so it is
    self-contained and archivable. Without ``-o`` a fresh timestamped bundle
    directory is created under the current directory, so the new report never
    links back into a past run directory.

    Args:
        results: The input result JSON files.
        output: The ``-o/--output`` value, or ``None``.

    Returns:
        A ``(path, formats)`` pair: ``formats`` is ``None`` when a single format
        is inferred from ``path``'s suffix, or the set of formats to render into
        ``path``'s parent bundle directory.
    """
    if output is not None:
        if output.suffix.lower() in _REPORT_SUFFIXES:
            return output, None
        return output / "report.html", {"html", "md"}
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    prefix = "manuprompt-report" if len(results) == 1 else "manuprompt-merged"
    return Path.cwd() / f"{prefix}_{stamp}" / "report.html", {"html", "md"}


def _console_chooser(case_id: str, candidates: list[CaseCandidate]) -> int:
    """Ask the operator which run's result to keep for a duplicated test id.

    Args:
        case_id: The conflicting test-case id.
        candidates: Competing results, one per source file.

    Returns:
        The index of the chosen candidate, or :data:`~reporting.SKIP_CASE` to
        drop the test from the report. At EOF or on empty input the first
        candidate is kept.
    """
    print("")
    print(f"Test '{case_id}' appears in {len(candidates)} results — choose one:")
    for index, candidate in enumerate(candidates, start=1):
        print(
            f"  [{index}] {candidate.outcome.upper():5} {candidate.source}  "
            f"(started {candidate.started_at or '?'}, {candidate.step_count} steps)"
        )
    print("  [s] skip this test (exclude from the report)")
    while True:
        try:
            raw = input(f"  selection [1-{len(candidates)}, s] (default 1): ").strip()
        except EOFError:
            return 0
        if raw == "":
            return 0
        if raw.lower() in ("s", "skip"):
            return SKIP_CASE
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            return int(raw) - 1
        print("  invalid selection, try again")


def _apply_overrides(
    suite: Suite, var_overrides: list[str], tool_overrides: list[str]
) -> Suite:
    """Return a copy of ``suite`` with CLI variable/tool overrides applied.

    Args:
        suite: The loaded suite.
        var_overrides: ``NAME=VALUE`` strings for suite variables.
        tool_overrides: ``NAME=PATH`` strings for tools.

    Returns:
        A new suite with merged variables and tools.

    Raises:
        ManuPromptError: If an override is not in ``NAME=VALUE`` form.
    """
    variables = dict(suite.variables)
    variables.update(_parse_key_values(var_overrides, "--var"))
    tools = dict(suite.tools)
    tools.update(_parse_key_values(tool_overrides, "--tool"))
    return dataclasses.replace(suite, variables=variables, tools=tools)


def _filter_test_cases(suite: Suite, ids: list[str]) -> Suite:
    """Return a copy of ``suite`` containing only the requested test cases.

    The selected cases run **in the order the ids are given** (overriding the
    suite's id-sorted order), so the operator controls the sequence. Ids may be
    given as repeated ``-t`` flags and/or comma-separated lists; duplicates are
    ignored, keeping the first occurrence.

    Args:
        suite: The loaded suite.
        ids: Test-case ids to keep (as passed via ``-t``), possibly
            comma-separated.

    Returns:
        A new suite whose ``test_cases`` are exactly the requested ids, in the
        requested order.

    Raises:
        ManuPromptError: If any requested id does not exist in the suite.
    """
    requested = _expand_ids(ids)
    by_id = {case.id: case for case in suite.test_cases}
    missing = [test_id for test_id in requested if test_id not in by_id]
    if missing:
        available = ", ".join(sorted(by_id)) or "(none)"
        raise ManuPromptError(
            f"unknown test id(s): {', '.join(missing)}; available: {available}"
        )
    selected = tuple(by_id[test_id] for test_id in requested)
    return dataclasses.replace(suite, test_cases=selected)


def _select_by_pattern(suite: Suite, pattern: str) -> Suite:
    """Return a copy of ``suite`` keeping cases whose id or name matches ``pattern``.

    The pattern is a case-insensitive regular expression matched (via
    ``re.search``, so a plain substring works) against each case's ``id`` and
    ``name``. Suite order is preserved.

    Args:
        suite: The (possibly already ``-t``-filtered) suite.
        pattern: The ``-k`` regular expression.

    Returns:
        A new suite restricted to the matching cases.

    Raises:
        ManuPromptError: If the pattern is invalid or matches no test case.
    """
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ManuPromptError(f"invalid -k pattern {pattern!r}: {exc}") from exc
    selected = tuple(
        case
        for case in suite.test_cases
        if regex.search(case.id) or regex.search(case.name)
    )
    if not selected:
        available = ", ".join(case.id for case in suite.test_cases) or "(none)"
        raise ManuPromptError(
            f"no test case matches -k pattern {pattern!r}; available: {available}"
        )
    return dataclasses.replace(suite, test_cases=selected)


def _expand_ids(raw_ids: list[str]) -> list[str]:
    """Flatten ``-t`` values into an ordered, de-duplicated id list.

    Each value may itself be a comma-separated list, so ``-t A,B -t C`` and
    ``-t A -t B -t C`` are equivalent. Order is preserved; the first occurrence
    of a duplicate wins.

    Args:
        raw_ids: The raw ``-t`` argument values.

    Returns:
        The expanded ids in request order, without duplicates.
    """
    ordered: list[str] = []
    for raw in raw_ids:
        for part in raw.split(","):
            test_id = part.strip()
            if test_id and test_id not in ordered:
                ordered.append(test_id)
    return ordered


def _parse_key_values(items: list[str], flag: str) -> dict[str, str]:
    """Parse ``NAME=VALUE`` CLI items into a mapping.

    Args:
        items: Raw ``NAME=VALUE`` strings.
        flag: Originating flag name, used in error messages.

    Returns:
        Mapping of name to value.

    Raises:
        ManuPromptError: If any item lacks an ``=`` separator.
    """
    parsed: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise ManuPromptError(f"{flag} expects NAME=VALUE, got {item!r}")
        parsed[name.strip()] = value
    return parsed


def _print_summary(result: SuiteResult) -> None:
    """Print a concise per-case summary and overall outcome to stdout.

    Args:
        result: The completed suite result.
    """
    print("")
    print(f"=== {result.name} — {result.outcome.value.upper()} ===")
    for case in result.cases:
        print(f"  [{case.outcome.value.upper():5}] {case.id}  {case.name}")
    counts = result.counts()
    tally = "  ".join(f"{key}={value}" for key, value in counts.items() if value)
    print(f"  cases: {tally or 'none'}")


if __name__ == "__main__":
    raise SystemExit(main())
