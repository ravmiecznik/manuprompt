"""High-level entry points for running a ManuPrompt suite.

This module wires the loader, engine, prompter and JSON reporter together
behind two convenience functions, :func:`load_suite` and :func:`run_suite`,
so callers (the CLI or other Python code) do not need to assemble the parts
by hand.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from functools import partial
from pathlib import Path

from ._banner import box_url, numbered_list
from .engine import Engine
from .errors import ManuPromptError
from .loader import load_suite as _load_suite
from .model import Suite
from .prompter import ConsolePrompter, Prompter
from .reporting import (
    Chooser,
    apply_untested,
    collect_logs,
    first_candidate,
    load_result,
    merge_results,
    relocate_artifacts,
    write_html,
    write_json,
    write_markdown,
    write_result_dict,
)
from .results import SuiteResult
from .theme import effective_theme
from .webui import WebGIO, WebPrompter

# Default parent directory for run artifacts and the result JSON.
_DEFAULT_RESULTS_ROOT = Path("manuprompt-results")

# Default TCP port for the live web surface. A fixed port keeps the URL stable
# across runs; the server falls back to an OS-chosen free port if it is busy.
_DEFAULT_WEB_PORT = 9999

# Report renderers keyed by short format name, and each format's file extension.
# The two share one prepared result mapping, so a bundle's report.html and
# report.md reference the same relocated artifacts/logs.
_REPORT_WRITERS = {"html": write_html, "md": write_markdown}
_FORMAT_EXTENSION = {"html": ".html", "md": ".md"}

# File suffixes that select the Markdown renderer; anything else means HTML.
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})


def load_suite(path: Path | str) -> Suite:
    """Load and validate a suite from a YAML file.

    Args:
        path: Path to the YAML suite document.

    Returns:
        The parsed :class:`~model.Suite`.

    Raises:
        SuiteValidationError: If the file is missing or malformed.
    """
    return _load_suite(path)


def run_suite(
    suite: Suite,
    *,
    prompter: Prompter | None = None,
    artifacts_dir: Path | str | None = None,
    json_path: Path | str | None = None,
    logger: logging.Logger | None = None,
    web_gio: WebGIO | None = None,
    cli_mode: bool = False,
) -> SuiteResult:
    """Run a suite, persisting results incrementally to JSON.

    By default operator prompts (verdicts, input, next-test and review) are
    presented in a **web browser** via a live :class:`~webui.WebGIO` surface:
    the session URL is announced at startup and the operator answers each
    prompt on that page. Pass ``cli_mode=True`` (the CLI's ``--cli-mode`` flag)
    to run prompts in the terminal instead; the web surface is still started so
    device output streams to a browser and artifacts can be uploaded there.

    The web surface can be disabled entirely with the ``MANUPROMPT_NO_WEB_CONSOLE``
    environment variable — in that case prompts always fall back to the console.

    Args:
        suite: The suite to execute.
        prompter: Explicit operator I/O implementation. When given it is used
            as-is and ``cli_mode`` is ignored. When omitted, a
            :class:`~webui.WebPrompter` is used in web mode and a
            :class:`~prompter.ConsolePrompter` in ``cli_mode`` (or when the web
            surface is disabled).
        artifacts_dir: Directory for run artifacts. Defaults to a timestamped
            directory under ``manuprompt-results/``. If the directory already
            exists, a ``_N`` suffix is appended so a prior run is never
            overwritten.
        json_path: Path of the result JSON. Defaults to ``result.json`` inside
            ``artifacts_dir``.
        logger: Logger to use. Defaults to a stdout logger.
        web_gio: An explicit live browser I/O surface. When omitted, one is
            created from the environment (unless ``MANUPROMPT_NO_WEB_CONSOLE`` is
            set).
        cli_mode: Run operator prompts in the terminal instead of the browser.

    Returns:
        The completed :class:`~results.SuiteResult`.
    """
    run_logger = logger or _default_logger()
    requested_dir = Path(artifacts_dir) if artifacts_dir else _default_artifacts_dir(suite)
    out_dir = _unique_dir(requested_dir)
    if out_dir != requested_dir:
        run_logger.info(
            "Output directory %s exists; using %s to preserve it", requested_dir, out_dir
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = Path(json_path) if json_path else out_dir / "result.json"

    _announce_plan(suite)

    gio = web_gio or _default_web_gio(suite, run_logger)
    # Web-prompt mode is the default: prompts run in the browser whenever a web
    # surface is available and neither an explicit prompter nor --cli-mode opts
    # out. Otherwise prompts run in the terminal.
    web_prompt = gio is not None and prompter is None and not cli_mode
    if gio is not None:
        gio.start()
        _announce_links(gio, web_prompt)
    if prompter is not None:
        run_prompter: Prompter = prompter
    elif web_prompt:
        assert gio is not None  # narrowed by web_prompt
        run_prompter = WebPrompter(gio)
    else:
        run_prompter = ConsolePrompter()

    run_logger.info("Running suite %r — artifacts in %s", suite.name, out_dir)
    # In web-prompt mode the engine leaves the surface running so the finished
    # report can be shown and downloaded in the browser; run_suite owns its stop.
    engine = Engine(
        prompter=run_prompter,
        artifacts_dir=out_dir,
        logger=run_logger,
        on_update=partial(write_json, path=out_json),
        web_gio=gio,
        stop_web_gio=not web_prompt,
    )
    try:
        result = engine.run(suite)
        run_logger.info(
            "Suite finished: %s — result written to %s", result.outcome.value, out_json
        )
        if web_prompt and gio is not None:
            _present_report(gio, out_dir, out_json, result, run_logger)
        else:
            _write_local_report(out_dir, out_json, run_logger)
    finally:
        if web_prompt and gio is not None:
            gio.stop()
    return result


def _write_local_report(out_dir: Path, out_json: Path, logger: logging.Logger) -> None:
    """Render ``report.html`` and ``report.md`` next to ``result.json``.

    This is the ``--cli-mode`` / non-web-prompt counterpart to
    :func:`_present_report`'s browser report: since there's no session page to
    view it on, both formats are written directly into the run's own output
    directory (regenerating in place, so no artifacts are copied — see
    :func:`~reporting.merge.relocate_artifacts`).

    Args:
        out_dir: The run's output directory (where the reports are written).
        out_json: Path of the run's ``result.json``.
        logger: Logger for diagnostics.
    """
    report_path = out_dir / "report.html"
    try:
        generate_report([out_json], report_path, logger=logger, formats={"html", "md"})
    except (OSError, ValueError, ManuPromptError) as exc:
        logger.warning("Could not build the report: %s", exc)


def _present_report(
    gio: WebGIO,
    out_dir: Path,
    out_json: Path,
    result: SuiteResult,
    logger: logging.Logger,
) -> None:
    """Build the finished report, serve it in the browser, and wait to finish.

    Renders a self-contained report bundle (HTML + Markdown + relocated
    artifacts + collected logs + merged JSON) from the run's result, zips it,
    serves the bundle at ``/report`` and the archive as a download at
    ``/report.zip``, then
    **blocks** on a ``finished`` prompt so the operator can review the report and
    download the zip in the browser. The wait ends when the operator clicks
    *Finish* (or the surface is stopped, e.g. Ctrl-C). The zip is saved next to
    the run's results so the report is preserved after the surface closes; the
    live-serving bundle is a temporary directory removed once the wait ends.

    Also writes plain ``report.html``/``report.md`` directly beside
    ``result.json`` (see :func:`_write_local_report`) referencing artifacts in
    place rather than relocated into a bundle, so every run leaves those files
    behind regardless of prompt mode — matching the ``--cli-mode`` behaviour
    and the promise made in the README.

    Args:
        gio: The running browser surface (kept alive by ``run_suite``).
        out_dir: The run's output directory (where ``report.zip`` is saved).
        out_json: Path of the run's ``result.json``.
        result: The completed suite result (for the summary shown to the operator).
        logger: Logger for diagnostics.
    """
    _write_local_report(out_dir, out_json, logger)

    bundle = Path(tempfile.mkdtemp(prefix="manuprompt-report-"))
    zip_path = out_dir / "report.zip"
    try:
        generate_report(
            [out_json],
            bundle / "report.html",
            logger=logger,
            save_json=True,
            formats={"html", "md"},
        )
        _zip_dir(bundle, zip_path)
    except (OSError, ValueError, ManuPromptError) as exc:
        logger.warning("Could not build the browser report: %s", exc)
        shutil.rmtree(bundle, ignore_errors=True)
        return

    gio.mount_dir("/report", bundle)
    gio.add_download("/report.zip", zip_path)
    logger.info("Report saved to %s and available in the browser", zip_path)
    box_url(
        f"{gio.title} — report ready",
        gio.session_url,
        "View the report and download the .zip in your browser, then click Finish",
    )
    try:
        gio.ask(
            "finished",
            {
                "report_url": "report/report.html",
                "zip_url": "report.zip",
                "outcome": result.outcome.value,
                "cases": len(result.cases),
            },
        )
    finally:
        shutil.rmtree(bundle, ignore_errors=True)


def _zip_dir(src_dir: Path, zip_path: Path) -> None:
    """Write every file under ``src_dir`` into ``zip_path`` (deflated).

    Args:
        src_dir: Directory whose contents are archived (paths are stored
            relative to it, so the archive unpacks to a self-contained bundle).
        zip_path: Destination ``.zip`` file.
    """
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in sorted(src_dir.rglob("*")):
            if entry.is_file():
                archive.write(entry, entry.relative_to(src_dir).as_posix())


def _announce_links(gio: WebGIO, web_prompt: bool) -> None:
    """Print the browser links the operator opens for this run.

    In web-prompt mode the interactive **session** page is the primary link
    (prompts + embedded console), with the standalone console and artifact pages
    listed alongside. In CLI mode the console page is the primary link, since
    prompts are answered in the terminal.

    Args:
        gio: The started live browser I/O surface.
        web_prompt: Whether operator prompts are answered in the browser.
    """
    hint = "Open this link in your browser"
    if gio.protected:
        hint += " (password required; any username)"
    if web_prompt:
        box_url(f"{gio.title} — open the session page", gio.session_url, hint)
        numbered_list(
            "Other pages:",
            [f"Console: {gio.url}", f"Artifacts: {gio.artifact_url}"],
        )
    else:
        box_url(f"{gio.title} — live output", gio.url, hint)
        numbered_list("Other pages:", [f"Artifacts: {gio.artifact_url}"])


def _announce_plan(suite: Suite) -> None:
    """Print the ordered list of test cases the session will run.

    Reflects the suite after any CLI filtering/ordering, so the operator sees
    exactly what is about to run (cases marked to skip are flagged).

    Args:
        suite: The suite about to run.
    """
    items: list[str] = []
    for case in suite.test_cases:
        line = f"{case.id}  {case.name}"
        if case.description.strip():
            line += f" — {case.description.strip()}"
        if case.skip_reason:
            line += f"  [marked skip: {case.skip_reason}]"
        items.append(line)
    numbered_list(f"Test cases to run ({len(suite.test_cases)}):", items)


def _default_web_gio(
    suite: Suite, logger: logging.Logger
) -> WebGIO | None:
    """Build the run's browser I/O surface from the environment, or ``None``.

    Honoured environment variables:

    * ``MANUPROMPT_NO_WEB_CONSOLE`` — when set, disables the web console.
    * ``MANUPROMPT_WEB_PORT`` — fixed TCP port (default ``9999``). The server
      falls back to an OS-chosen free port if this one is busy, so the URL is
      stable run-to-run in the common case without risking a bind failure.
    * ``MANUPROMPT_WEB_HOST`` — hostname used in the printed URL (default
      ``localhost``).
    * ``MANUPROMPT_WEB_TITLE`` — display name for the surface, overriding the
      suite's ``web_title`` / ``name``.
    * ``MANUPROMPT_WEB_PASSWORD`` — password gating the surface, overriding the
      suite's ``test_session_password`` variable.

    The surface is themed with the project-wide ``theme.yaml`` (next to
    :mod:`theme`), overridden per-field by the suite's own ``theme:`` block
    (see :func:`~theme.effective_theme`).

    The surface is password-protected (HTTP Basic, username ignored) when the
    suite defines a ``test_session_password`` variable or the environment sets
    ``MANUPROMPT_WEB_PASSWORD``.

    Args:
        suite: The suite being run (names and optionally guards the surface).
        logger: Logger passed to the surface.

    Returns:
        A configured :class:`~webui.WebGIO`, or ``None`` when disabled.
    """
    if os.environ.get("MANUPROMPT_NO_WEB_CONSOLE"):
        return None
    try:
        port = int(os.environ.get("MANUPROMPT_WEB_PORT", str(_DEFAULT_WEB_PORT)))
    except ValueError:
        logger.warning(
            "Ignoring invalid MANUPROMPT_WEB_PORT; using default port %d", _DEFAULT_WEB_PORT
        )
        port = _DEFAULT_WEB_PORT
    title = os.environ.get("MANUPROMPT_WEB_TITLE") or suite.web_title or suite.name
    suite_password = suite.variables.get("test_session_password")
    password = os.environ.get("MANUPROMPT_WEB_PASSWORD") or (
        str(suite_password) if suite_password else None
    )
    return WebGIO(
        port=port,
        link_host=os.environ.get("MANUPROMPT_WEB_HOST"),
        title=title,
        password=password,
        theme=effective_theme(suite.theme),
        logger=logger,
    )


def generate_report(
    result_paths: list[Path | str],
    output_path: Path | str,
    *,
    suite_path: Path | str | None = None,
    chooser: Chooser | None = None,
    logger: logging.Logger | None = None,
    save_json: bool = False,
    formats: set[str] | None = None,
) -> dict:
    """Render an HTML and/or Markdown report from saved JSON result files.

    The JSON result is the canonical record, so a report can be regenerated at
    any time without re-running a suite. Multiple results are merged by test-case
    id (see :func:`~reporting.merge.merge_results`); ``chooser`` resolves ids
    present in more than one file. Artifacts are relocated next to ``output_path``
    so the report displays them correctly (see
    :func:`~reporting.merge.relocate_artifacts`).

    When ``suite_path`` is given, every test case defined in the suite that has
    no result in any input file is added to the report as ``NOT-TESTED`` (see
    :func:`~reporting.merge.apply_untested`), so the report reflects the full
    planned suite, not only what ran.

    With ``save_json`` the merged result is also written as ``result.json`` next
    to the report. Combined with artifact relocation this makes the output
    directory a **self-contained, archivable bundle** (report + merged JSON +
    ``attachments/``) that no longer depends on the source run directories.

    Args:
        result_paths: One or more paths to ``result.json`` files.
        output_path: Destination ``.html`` file. Its parent directory receives
            an ``attachments/`` folder for any copied artifacts.
        suite_path: Optional path to the suite YAML. When given, planned-but-not
            -run cases are shown as ``NOT-TESTED``.
        chooser: Resolver for duplicate test ids across files. Defaults to
            keeping the first occurrence.
        logger: Logger to use. Defaults to a stdout logger.
        save_json: When ``True``, also write the merged ``result.json`` next to
            the report for a self-contained bundle.
        formats: Which formats to render. When ``None`` (the default) a single
            format is inferred from ``output_path``'s suffix (``.md``/
            ``.markdown`` → Markdown, otherwise HTML). When given (a subset of
            ``{"html", "md"}``) each format is written next to ``output_path``
            using that extension, so both can share one prepared bundle.

    Returns:
        The merged, report-ready mapping that was rendered.

    Raises:
        ValueError: If ``result_paths`` is empty.
        OSError: If a result file cannot be read.
        SuiteValidationError: If ``suite_path`` is given but malformed.
    """
    run_logger = logger or _default_logger()
    sources = [(Path(path), load_result(Path(path))) for path in result_paths]
    contributing: list[Path] = []
    case_sources: dict[str, Path] = {}
    merged = merge_results(
        sources,
        chooser or first_candidate,
        contributing=contributing,
        case_sources=case_sources,
    )
    if suite_path is not None:
        apply_untested(merged, _planned_cases(suite_path))
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    relocate_artifacts(merged, out.parent, logger=run_logger)
    # Persist the result BEFORE decorating cases with run-level loose files:
    # only the precise per-test logs (attach_log) are saved, so they carry
    # cleanly through future re-merges. The run-level loose files below are
    # render-only and would otherwise accumulate across merges.
    if save_json:
        json_path = out.parent / "result.json"
        write_result_dict(merged, json_path)
        run_logger.info("Merged result written to %s", json_path)
    kept = set(contributing)
    log_sources = [source for source in sources if source[0] in kept]
    logs = collect_logs(log_sources, out.parent, logger=run_logger)
    _attach_logs(merged, logs, case_sources, merged=len(sources) > 1)
    for target, writer in _report_targets(out, formats):
        writer(merged, target)
        run_logger.info("Report written to %s", target)
    return merged


def _report_targets(out: Path, formats: set[str] | None) -> list[tuple[Path, Any]]:
    """Return the ``(path, writer)`` pairs to render for a report.

    With ``formats`` unset a single renderer is inferred from ``out``'s suffix.
    Otherwise each requested format is written next to ``out`` under that
    format's own extension (``report.html`` and/or ``report.md``), so both
    formats reuse the same prepared bundle directory.

    Args:
        out: The primary output path (its parent is the bundle directory).
        formats: Requested format names (subset of ``{"html", "md"}``), or
            ``None`` to infer one format from ``out``'s suffix.

    Returns:
        One ``(path, writer)`` pair per format to render.
    """
    if formats is None:
        fmt = "md" if out.suffix.lower() in _MARKDOWN_SUFFIXES else "html"
        return [(out, _REPORT_WRITERS[fmt])]
    return [
        (out.with_suffix(_FORMAT_EXTENSION[fmt]), _REPORT_WRITERS[fmt])
        for fmt in sorted(formats)
    ]


def _attach_logs(
    data: dict,
    logs: dict[Path, dict],
    case_sources: dict[str, Path],
    *,
    merged: bool,
) -> None:
    """Surface collected run-level loose files in the report.

    For a **merged** report each contributing run's loose files are associated
    with the cases that came from that run (via ``case_sources``) and appended to
    ``case["logs"]`` as ``{label, path}`` entries, so a test links the loose logs
    of the run actually chosen for it — alongside any precise per-test logs from
    :meth:`~context.RunContext.attach_log`. (Because :func:`collect_logs` skips a
    prior bundle's ``logs/`` tree, a re-merged bundle contributes no loose files
    here; its cases keep the entries they already carry, relocated forward — so
    logs do not accumulate across consecutive merges.) For a **single** run a
    global ``data["logs"]`` section is used instead, since every case shares it.

    Args:
        data: The report-ready mapping to annotate in place.
        logs: Mapping of source ``json_path`` to ``{"source", "files"}`` as
            returned by :func:`~reporting.merge.collect_logs`.
        case_sources: Mapping of case id to its source ``json_path``.
        merged: Whether the report combines more than one source.
    """
    if not logs:
        return
    if not merged:
        data["logs"] = [entry for entry in logs.values() if entry.get("files")]
        return
    for case in data.get("cases") or []:
        entry = logs.get(case_sources.get(str(case.get("id", ""))))
        if entry and entry.get("files"):
            # Append after any attach_log dicts already on the case.
            case.setdefault("logs", []).extend(
                {"label": rel.rsplit("/", 1)[-1], "path": rel}
                for rel in entry["files"]
            )


def _planned_cases(suite_path: Path | str) -> list[dict]:
    """Return the suite's cases as ``{"id", "name"}`` mappings.

    Used to mark planned-but-not-run cases as NOT-TESTED; a case that never ran
    has no steps to report, so only its id and name are needed.

    Args:
        suite_path: Path to the suite YAML.

    Returns:
        ``{"id", "name"}`` mappings, one per suite case.

    Raises:
        SuiteValidationError: If the suite is missing or malformed.
    """
    suite = _load_suite(suite_path)
    return [
        {"id": case.id, "name": case.name, "description": case.description}
        for case in suite.test_cases
    ]


def _default_artifacts_dir(suite: Suite) -> Path:
    """Return a fresh timestamped artifacts directory for a suite run.

    Args:
        suite: The suite being run (its name seeds the directory name).

    Returns:
        A path under ``manuprompt-results/`` combining a slug of the suite name
        and the current timestamp.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", suite.name.lower()).strip("-") or "suite"
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return _DEFAULT_RESULTS_ROOT / f"{slug}_{stamp}"


def _unique_dir(path: Path) -> Path:
    """Return ``path`` if free, else the first unused ``path_N`` sibling.

    A run never overwrites an existing output directory: when the requested
    directory already exists, a numeric suffix (``_1``, ``_2``, …) is appended
    so a previous run's results are preserved.

    Args:
        path: The requested output directory.

    Returns:
        A path that does not yet exist.
    """
    if not path.exists():
        return path
    index = 1
    while True:
        candidate = path.with_name(f"{path.name}_{index}")
        if not candidate.exists():
            return candidate
        index += 1


def _default_logger() -> logging.Logger:
    """Return a stdout logger for standalone runs.

    The ManuPrompt package is intentionally free of any external logging
    framework dependency so it runs without extra environment setup.
    A plain stdout logger is configured here rather than depending on a shared
    external logging manager.

    Returns:
        A configured logger that writes to stdout once.
    """
    logger = logging.getLogger("manuprompt")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
