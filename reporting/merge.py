"""Merge one or more JSON result documents into a single report-ready mapping.

The HTML reporter renders the same JSON-serialisable mapping produced by
:meth:`~results.SuiteResult.to_dict`. This module lets a report be (re)generated
from saved ``result.json`` files later, and lets **several** runs be combined
into one report:

* **Case-level merge.** Test cases from all sources are unioned by ``id``. When
  the same ``id`` appears in more than one source, a caller-supplied *chooser*
  decides which run's result to keep (see :data:`Chooser`).
* **Artifact relocation.** Each source's artifacts are stored as paths relative
  to *that run's* directory. :func:`relocate_artifacts` rebases them so the
  generated report displays them correctly regardless of where it is written —
  copying files in from other directories when needed.

The functions operate on plain dictionaries (not :class:`~results.SuiteResult`)
so a report can be produced without re-running anything.
"""

from __future__ import annotations

import copy
import re
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from logging import Logger
from pathlib import Path
from typing import Any

# Report-only outcome for a test case defined in the suite but absent from every
# result file (i.e. never run). It is not part of the live :class:`Outcome`
# enum because it only arises when comparing a suite against saved results.
UNTESTED: str = "not-tested"

# Outcome string precedence used to aggregate a container's outcome, mirroring
# :func:`results.aggregate_outcome` but operating on the serialised strings.
_ALL_OUTCOMES: tuple[str, ...] = ("pass", "fail", "error", "ack", "skip", UNTESTED)

# Characters not allowed in a copied-artifact filename are collapsed to '_'.
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

# A run directory also holds the report(s) this tool generated (report.html and
# report.md); those are excluded from log collection by extension (not by a
# specific name) since they are rendered output, not logs.
_REPORT_SUFFIXES: frozenset[str] = frozenset({".html", ".htm", ".md", ".markdown"})


@dataclass(frozen=True)
class CaseCandidate:
    """One run's result for a test case, offered to a conflict :data:`Chooser`.

    Attributes:
        source: Path of the ``result.json`` this case came from.
        case: The case-result mapping (as produced by
            :meth:`~results.TestCaseResult.to_dict`).
    """

    source: Path
    case: dict[str, Any]

    @property
    def id(self) -> str:
        """Return the test-case id."""
        return str(self.case.get("id", ""))

    @property
    def name(self) -> str:
        """Return the test-case name."""
        return str(self.case.get("name", ""))

    @property
    def outcome(self) -> str:
        """Return the aggregated case outcome (e.g. ``pass``)."""
        return str(self.case.get("outcome", ""))

    @property
    def started_at(self) -> str:
        """Return the ISO-8601 start timestamp, if any."""
        return str(self.case.get("started_at", ""))

    @property
    def step_count(self) -> int:
        """Return the number of step results recorded for the case."""
        return len(self.case.get("steps") or [])


# Sentinel a chooser may return instead of a candidate index to drop the test
# from the merge entirely (no result kept). When a suite is also provided, such
# a case then surfaces as NOT-TESTED via :func:`apply_untested`.
SKIP_CASE: int = -1

# A chooser is called once per conflicting id with the competing candidates and
# returns the index of the candidate to keep, or :data:`SKIP_CASE` to omit the
# test. It is only invoked when a test id is present in more than one source.
Chooser = Callable[[str, list[CaseCandidate]], int]


def first_candidate(case_id: str, candidates: list[CaseCandidate]) -> int:
    """Default :data:`Chooser` that keeps the first candidate.

    :func:`merge_results` orders a conflict group most-recent-first, so this
    keeps the **newest** run's result.

    Args:
        case_id: The conflicting test-case id (unused).
        candidates: Competing candidates (unused beyond returning ``0``).

    Returns:
        Always ``0``.
    """
    return 0


def merge_results(
    sources: list[tuple[Path, dict[str, Any]]],
    chooser: Chooser = first_candidate,
    *,
    contributing: list[Path] | None = None,
    case_sources: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Merge result mappings into one report-ready mapping.

    A single source is returned essentially unchanged (only its artifact paths
    are rebased to absolute, ready for :func:`relocate_artifacts`). Multiple
    sources are unioned by test-case ``id``; conflicts are resolved by
    ``chooser``. The merged document recomputes its overall outcome and counts,
    spans the earliest start to the latest finish, and unions the
    ``test_environment`` entries. Suite-level setup/teardown sections are
    dropped when merging more than one source, since they are run-specific and
    not associated with a test case.

    Args:
        sources: ``(json_path, result_mapping)`` pairs. ``json_path`` is used
            to rebase that source's relative artifact paths.
        chooser: Resolver invoked per conflicting id; returns the index of the
            candidate to keep. Defaults to :func:`first_candidate`.
        contributing: Optional sink. When given, it is cleared and filled (in
            input order) with the source paths that actually contributed a case
            to the merged result — i.e. sources whose every case lost a conflict
            are excluded. Useful for collecting only the relevant runs' logs.
        case_sources: Optional sink. When given, it is cleared and filled with a
            mapping of each merged case id to the source path it came from,
            letting a caller associate per-run logs with individual cases.

    Returns:
        A merged, report-ready mapping (same shape as
        :meth:`~results.SuiteResult.to_dict`), with absolute artifact paths.

    Raises:
        ValueError: If ``sources`` is empty.
    """
    if not sources:
        raise ValueError("merge_results requires at least one source")
    if contributing is not None:
        contributing.clear()
    if case_sources is not None:
        case_sources.clear()

    rebased: list[tuple[Path, dict[str, Any]]] = []
    for json_path, data in sources:
        clone = copy.deepcopy(data)
        _rebase_artifacts(clone, json_path.resolve().parent)
        rebased.append((json_path, clone))

    if len(rebased) == 1:
        json_path, clone = rebased[0]
        if contributing is not None:
            contributing.append(json_path)
        if case_sources is not None:
            for case in clone.get("cases") or []:
                case_sources[str(case.get("id", ""))] = json_path
        return clone

    cases, winners, sources_by_id = _merge_cases(rebased, chooser)
    if contributing is not None:
        contributing.extend(winners)
    if case_sources is not None:
        case_sources.update(sources_by_id)
    outcomes = [str(case.get("outcome", "")) for case in cases]
    first = rebased[0][1]
    starts = [d.get("started_at", "") for _, d in rebased if d.get("started_at")]
    finishes = [d.get("finished_at", "") for _, d in rebased if d.get("finished_at")]
    return {
        "name": str(first.get("name", "Merged report")),
        "description": _merge_description(rebased),
        "outcome": _aggregate(outcomes),
        "started_at": min(starts, default=""),
        "finished_at": max(finishes, default=""),
        "test_environment": _merge_environment(rebased),
        "counts": _counts(outcomes),
        "suite_setup": [],
        "cases": cases,
        "suite_teardown": [],
        "theme": dict(first.get("theme") or {}),
    }


def apply_untested(
    data: dict[str, Any], planned: list[dict[str, Any]]
) -> dict[str, Any]:
    """Add ``NOT-TESTED`` cases for planned cases missing from the results.

    Mutates ``data`` in place: every planned case whose ``id`` is absent from
    ``data["cases"]`` is appended as a case with outcome :data:`UNTESTED` and no
    steps (only the id and name are known for a case that never ran), the cases
    are re-sorted by id, and the overall ``outcome``/``counts`` are recomputed.

    Args:
        data: A report-ready mapping (e.g. from :func:`merge_results`).
        planned: The full set of suite cases as ``{"id", "name"}`` mappings.

    Returns:
        The same ``data`` mapping, updated in place.
    """
    cases = data.setdefault("cases", [])
    tested = {str(case.get("id", "")) for case in cases}
    for case in planned:
        if str(case.get("id", "")) not in tested:
            cases.append(_untested_case(case))
    cases.sort(key=lambda case: str(case.get("id", "")))
    outcomes = [str(case.get("outcome", "")) for case in cases]
    data["counts"] = _counts(outcomes)
    data["outcome"] = _aggregate(outcomes)
    return data


def relocate_artifacts(
    data: dict[str, Any], output_dir: Path, *, logger: Logger
) -> None:
    """Rewrite artifact paths so the report at ``output_dir`` can display them.

    Mutates ``data`` in place. Copied artifacts are grouped into a per-section
    subdirectory of ``output_dir/attachments/`` named after the test case id
    (or ``suite_setup`` / ``suite_teardown`` for suite-level phases), so files
    with the same name in different cases never clash and stay organised. For
    each artifact path: a file already inside ``output_dir`` is re-expressed
    relative to it (no copy); a file elsewhere is copied into its section
    subdirectory under a name made unique within that subdirectory; a missing
    file is logged, its label suffixed with ``(missing)`` and its path cleared.

    Args:
        data: A report-ready mapping with **absolute** artifact paths (as
            produced by :func:`merge_results`).
        output_dir: Directory the HTML report will be written to.
        logger: Logger for missing-file and copy diagnostics.
    """
    output_dir = output_dir.resolve()
    attachments = output_dir / "attachments"
    _relocate_section(data.get("suite_setup") or [], "suite_setup",
                      output_dir, attachments, logger)
    for case in data.get("cases") or []:
        section = _slug(str(case.get("id", "")) or "case")
        _relocate_section(case.get("steps") or [], section,
                          output_dir, attachments, logger)
        # Per-case attached logs ({label, path}) relocate like step artifacts.
        used: set[str] = set()
        for log in case.get("logs") or []:
            if isinstance(log, dict):
                _relocate_one(log, output_dir, attachments / section, used, logger)
    _relocate_section(data.get("suite_teardown") or [], "suite_teardown",
                      output_dir, attachments, logger)


def collect_logs(
    sources: list[tuple[Path, dict[str, Any]]],
    output_dir: Path,
    *,
    logger: Logger,
) -> dict[Path, dict[str, Any]]:
    """Copy each run's loose files (logs, etc.) into the report bundle.

    A run directory holds more than the artifacts referenced by the result: tool
    logs, a captured device console, and any other files a suite produced. These
    are not named in the JSON, so they would be lost when a report is moved or
    merged. This copies everything in each source's directory **except** the
    loaded ``result.json``, the ``attachments/`` tree (handled separately), the
    ``logs/`` tree (a previous merge's own collected output — recursing into it
    would re-copy and nest the whole history on every re-merge), and any HTML
    report (rendered output, not a log), into ``output_dir/logs/<run>/``,
    preserving each run's relative layout. It is deliberately name-agnostic — no
    file is recognised by a specific log name.

    A source whose directory *is* the output directory (in-place regeneration)
    is not copied — its files already sit beside the report — but it is still
    **listed**: :func:`_list_run_logs` finds the same loose files in place and
    reports their existing paths, so an in-place report (e.g. a `--cli-mode`
    run's automatically written ``report.html``, or ``report -o`` pointed at
    its own input directory) still links a full-session log like a
    continuously-mirrored device console, not just per-test slices.

    Args:
        sources: ``(json_path, result_mapping)`` pairs; only the paths are used.
        output_dir: Directory the report is written to.
        logger: Logger for copy diagnostics.

    Returns:
        A mapping of each source's ``json_path`` to ``{"source": <run name>,
        "files": [<relpath>, ...]}`` (paths relative to ``output_dir``). Sources
        that yielded no files are omitted, so the result may be empty.
    """
    output_dir = output_dir.resolve()
    logs_root = output_dir / "logs"
    sections: set[str] = set()
    collected: dict[Path, dict[str, Any]] = {}
    for json_path, _ in sources:
        run_dir = json_path.resolve().parent
        if run_dir == output_dir:
            files = _list_run_logs(run_dir, json_path.resolve(), logger)
        else:
            section = _unique_section(sections, _slug(run_dir.name) or "run")
            files = _copy_run_logs(
                run_dir, json_path.resolve(), output_dir, logs_root / section, logger
            )
            if files:
                sections.add(section)
        if files:
            collected[json_path] = {"source": run_dir.name, "files": files}
    return collected


def _is_loose_log(src: Path, rel: Path, result_path: Path) -> bool:
    """Return whether a run-directory file counts as a "loose log" to collect.

    Excludes the result JSON, the ``attachments/`` tree (relocated
    separately), the ``logs/`` tree (a previous merge's own collected
    output — recursing into it would re-copy and nest the whole log history
    on every re-merge; genuine per-run loose files live at the run-dir root),
    and any generated report — HTML or Markdown (rendered output, not a log).

    Args:
        src: Resolved absolute path of the candidate file.
        rel: Path of the file relative to the run directory.
        result_path: Absolute path of the loaded ``result.json``.

    Returns:
        ``True`` if the file should be collected/listed as a loose log.
    """
    if src == result_path or (rel.parts and rel.parts[0] in ("attachments", "logs")):
        return False
    return src.suffix.lower() not in _REPORT_SUFFIXES


def _list_run_logs(run_dir: Path, result_path: Path, logger: Logger) -> list[str]:
    """List a run's own loose files in place, without copying them anywhere.

    Used when the report is generated directly inside its own run directory
    (in-place regeneration — see :func:`collect_logs`): the files already sit
    exactly where the report needs to link them, so this only discovers and
    returns their existing paths relative to ``run_dir``.

    Args:
        run_dir: The run directory, which is also the report's output directory.
        result_path: Absolute path of the loaded ``result.json`` (excluded).
        logger: Logger for diagnostics.

    Returns:
        Paths of the loose files, relative to ``run_dir``.
    """
    found: list[str] = []
    for entry in sorted(run_dir.rglob("*")):
        if not entry.is_file():
            continue
        src = entry.resolve()
        rel = entry.relative_to(run_dir)
        if _is_loose_log(src, rel, result_path):
            found.append(rel.as_posix())
    if found:
        logger.info("Found %d log file(s) already in %s", len(found), run_dir.name)
    return found


def _copy_run_logs(
    run_dir: Path,
    result_path: Path,
    output_dir: Path,
    dest_dir: Path,
    logger: Logger,
) -> list[str]:
    """Copy a single run's loose files into ``dest_dir`` (see :func:`collect_logs`).

    Args:
        run_dir: The source run directory.
        result_path: Absolute path of the loaded ``result.json`` (excluded).
        output_dir: The report output directory (its own files are excluded).
            HTML reports in the run dir are excluded too.
        dest_dir: Destination ``logs/<run>/`` directory.
        logger: Logger for copy diagnostics.

    Returns:
        Paths of the copied files, relative to ``output_dir``.
    """
    copied: list[str] = []
    for entry in sorted(run_dir.rglob("*")):
        if not entry.is_file():
            continue
        src = entry.resolve()
        rel = entry.relative_to(run_dir)
        if not _is_loose_log(src, rel, result_path):
            continue
        if src.is_relative_to(output_dir):
            continue
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied.append(target.relative_to(output_dir).as_posix())
    if copied:
        logger.info("Collected %d log file(s) from %s", len(copied), run_dir.name)
    return copied


def _unique_section(used: set[str], name: str) -> str:
    """Return ``name`` made unique against ``used`` by appending ``_N``."""
    if name not in used:
        return name
    index = 1
    while f"{name}_{index}" in used:
        index += 1
    return f"{name}_{index}"


def _relocate_section(
    steps: list[dict[str, Any]],
    section: str,
    output_dir: Path,
    attachments: Path,
    logger: Logger,
) -> None:
    """Relocate every artifact of one section into its own subdirectory.

    Args:
        steps: The section's step mappings.
        section: Subdirectory name under ``attachments`` for this section.
        output_dir: Directory the report is written to.
        attachments: The ``output_dir/attachments`` base directory.
        logger: Logger for diagnostics.
    """
    dest_dir = attachments / section
    used: set[str] = set()
    for step in steps:
        for artifact in step.get("artifacts") or []:
            _relocate_one(artifact, output_dir, dest_dir, used, logger)


def _relocate_one(
    artifact: dict[str, str],
    output_dir: Path,
    dest_dir: Path,
    used: set[str],
    logger: Logger,
) -> None:
    """Relocate a single artifact mapping in place (see :func:`relocate_artifacts`).

    Args:
        artifact: The ``{"label", "path"}`` mapping to update.
        output_dir: Directory the report is written to.
        dest_dir: Subdirectory copied files land in (created on demand).
        used: Names already allocated within ``dest_dir`` this pass.
        logger: Logger for diagnostics.
    """
    raw = artifact.get("path", "")
    if not raw:
        return
    src = Path(raw)
    if not src.is_absolute():
        src = output_dir / src
    src = src.resolve()
    label = artifact.get("label", "")
    if not src.is_file():
        logger.warning("Artifact file missing for %r: %s", label, raw)
        artifact["label"] = f"{label} (missing)".strip()
        artifact["path"] = ""
        return
    if src.is_relative_to(output_dir):
        artifact["path"] = src.relative_to(output_dir).as_posix()
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = _unique_name(dest_dir, label, src, used)
    shutil.copy2(src, dest_dir / name)
    used.add(name)
    rel = (dest_dir / name).relative_to(output_dir).as_posix()
    artifact["path"] = rel
    logger.info("Copied artifact %r -> %s", label, rel)


def _slug(text: str) -> str:
    """Return a filesystem-safe slug for a section/subdirectory name.

    Args:
        text: Raw section name (e.g. a test-case id).

    Returns:
        ``text`` with unsafe characters collapsed to ``_``, or ``"misc"`` if
        nothing usable remains.
    """
    return _UNSAFE_FILENAME.sub("_", text).strip("._-") or "misc"


def _merge_cases(
    rebased: list[tuple[Path, dict[str, Any]]], chooser: Chooser
) -> tuple[list[dict[str, Any]], list[Path], dict[str, Path]]:
    """Union test cases by id, resolving conflicts via ``chooser``.

    Args:
        rebased: ``(json_path, data)`` pairs with rebased artifact paths.
        chooser: Resolver for ids present in more than one source.

    Returns:
        A ``(chosen, contributing, case_sources)`` tuple: the chosen case
        mappings (sorted by id); the source paths that won at least one case, in
        input order; and a mapping of each chosen case id to its source path.

    Competing candidates for a conflicting id are ordered **most-recent-first**
    (by ``started_at``), so the chooser sees — and defaults to — the newest run.
    """
    candidates: dict[str, list[CaseCandidate]] = {}
    order: list[str] = []
    for json_path, data in rebased:
        for case in data.get("cases") or []:
            case_id = str(case.get("id", ""))
            if case_id not in candidates:
                candidates[case_id] = []
                order.append(case_id)
            candidates[case_id].append(CaseCandidate(json_path, case))

    chosen: list[dict[str, Any]] = []
    winners: set[Path] = set()
    case_sources: dict[str, Path] = {}
    for case_id in order:
        # Newest first; ISO-8601 timestamps sort chronologically, missing ones
        # fall to the bottom. Stable, so same-timestamp runs keep input order.
        group = sorted(candidates[case_id], key=lambda c: c.started_at, reverse=True)
        choice = chooser(case_id, group) if len(group) > 1 else 0
        if choice == SKIP_CASE:
            continue  # operator dropped this test from the merge
        index = _clamp(choice, len(group))
        chosen.append(group[index].case)
        winners.add(group[index].source)
        case_sources[case_id] = group[index].source
    chosen.sort(key=lambda case: str(case.get("id", "")))
    contributing = [json_path for json_path, _ in rebased if json_path in winners]
    return chosen, contributing, case_sources


def _clamp(index: int, size: int) -> int:
    """Return ``index`` constrained to ``[0, size)``."""
    if index < 0:
        return 0
    if index >= size:
        return size - 1
    return index


def _merge_environment(
    rebased: list[tuple[Path, dict[str, Any]]],
) -> list[dict[str, str]]:
    """Union ``test_environment`` entries across sources, preserving order.

    Args:
        rebased: ``(json_path, data)`` pairs.

    Returns:
        De-duplicated ``[{"label","value"}]`` entries in first-seen order.
    """
    seen: set[tuple[str, str]] = set()
    merged: list[dict[str, str]] = []
    for _, data in rebased:
        for entry in data.get("test_environment") or []:
            key = (str(entry.get("label", "")), str(entry.get("value", "")))
            if key not in seen:
                seen.add(key)
                merged.append({"label": key[0], "value": key[1]})
    return merged


def _merge_description(rebased: list[tuple[Path, dict[str, Any]]]) -> str:
    """Return the first source's description with a merge provenance note.

    Args:
        rebased: ``(json_path, data)`` pairs.

    Returns:
        A description string noting how many sources were merged and from where.
    """
    base = str(rebased[0][1].get("description", "")).strip()
    names = ", ".join(json_path.name for json_path, _ in rebased)
    note = f"Merged from {len(rebased)} result files: {names}"
    return f"{base} — {note}" if base else note


def _untested_case(planned: dict[str, Any]) -> dict[str, Any]:
    """Build a case-result mapping for a planned case that was never run.

    Only the id and name are populated; a case that never ran has no step
    results to show.

    Args:
        planned: A ``{"id", "name"}`` mapping.

    Returns:
        A case mapping with outcome :data:`UNTESTED` and no steps.
    """
    return {
        "id": str(planned.get("id", "")),
        "name": str(planned.get("name", "")),
        "outcome": UNTESTED,
        "skip_reason": "",
        "started_at": "",
        "finished_at": "",
        "steps": [],
    }


def _aggregate(outcomes: list[str]) -> str:
    """Reduce child outcome strings to a single container outcome string.

    Mirrors :func:`results.aggregate_outcome` on serialised values, with the
    report-only :data:`UNTESTED` state treated as neutral (it never fails a
    container, but an all-untested container reports as untested).

    Args:
        outcomes: Child outcome strings.

    Returns:
        The aggregated outcome string.
    """
    if any(o == "error" for o in outcomes):
        return "error"
    if any(o == "fail" for o in outcomes):
        return "fail"
    if any(o == "pass" for o in outcomes):
        return "pass"
    if outcomes and all(o == UNTESTED for o in outcomes):
        return UNTESTED
    if outcomes and all(o in ("skip", UNTESTED) for o in outcomes):
        return "skip"
    return "pass"


def _counts(outcomes: list[str]) -> dict[str, int]:
    """Tally case outcomes keyed by outcome value.

    Args:
        outcomes: Case outcome strings.

    Returns:
        A mapping with a count for every known outcome value.
    """
    tally = {value: 0 for value in _ALL_OUTCOMES}
    for outcome in outcomes:
        if outcome in tally:
            tally[outcome] += 1
    return tally


def _is_attached_log(log: dict[str, Any]) -> bool:
    """Return whether a case-log entry is a genuine per-test log to carry forward.

    A log explicitly flagged ``attached`` (by :meth:`~context.RunContext.attach_log`)
    always qualifies. For older data without the flag, a **descriptive** label
    (e.g. ``session log``) is treated as genuine, while a **filename-like** label
    (no spaces and a file extension, e.g. ``session.log``, ``app_*.log``)
    is a run-level loose file an earlier merge appended — those are re-derived per
    render, so they are not carried forward (avoids accumulation across merges).

    Args:
        log: A case-log mapping.

    Returns:
        ``True`` to keep the entry on merge.
    """
    if log.get("attached"):
        return True
    label = str(log.get("label", "")).strip()
    return bool(label) and not (" " not in label and bool(Path(label).suffix))


def _rebase_artifacts(data: dict[str, Any], base: Path) -> None:
    """Make every artifact path absolute against ``base`` (in place).

    Args:
        data: A result mapping.
        base: Directory the result's relative artifact paths are relative to.
    """
    for step in _iter_steps(data):
        for artifact in step.get("artifacts") or []:
            path = artifact.get("path", "")
            if path and not Path(path).is_absolute():
                artifact["path"] = str((base / path).resolve())
    for case in data.get("cases") or []:
        logs = case.get("logs")
        if not logs:
            continue
        # Keep only genuine per-test logs (``attach_log`` entries, flagged
        # ``attached``) and rebase their paths so relocation copies the exact
        # files into the new bundle. Everything else — bare strings and the
        # run-level loose files a previous merge appended — is dropped: those
        # are re-derived per render, so carrying them forward would accumulate
        # every run's logs onto every case across consecutive merges.
        kept: list[dict[str, Any]] = []
        for log in logs:
            if not isinstance(log, dict) or not _is_attached_log(log):
                continue
            path = log.get("path", "")
            if path and not Path(path).is_absolute():
                log["path"] = str((base / path).resolve())
            kept.append(log)
        case["logs"] = kept


def _iter_steps(data: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield every step mapping in a result document.

    Walks the suite-level setup/teardown lists and every case's steps.

    Args:
        data: A result mapping.

    Yields:
        Each step-result mapping.
    """
    yield from data.get("suite_setup") or []
    for case in data.get("cases") or []:
        yield from case.get("steps") or []
    yield from data.get("suite_teardown") or []


def _unique_name(dest_dir: Path, label: str, src: Path, used: set[str]) -> str:
    """Return a collision-free filename for a copied artifact.

    Args:
        dest_dir: Directory the artifact will be copied into.
        label: Operator-facing label for the artifact.
        src: Source file being copied (its suffix is preserved).
        used: Names already allocated in this relocation pass.

    Returns:
        A filename (not a full path) unique within ``dest_dir`` and ``used``.
    """
    slug = _UNSAFE_FILENAME.sub("_", label).strip("._-") or "artifact"
    suffix = src.suffix
    # Avoid a doubled extension when the label already carries it (e.g. a label
    # of "dut_console.log" with a ".log" source → "dut_console.log", not
    # "dut_console.log.log").
    if suffix and slug.lower().endswith(suffix.lower()):
        slug = slug[: -len(suffix)]
    candidate = f"{slug}{suffix}"
    index = 1
    while candidate in used or (dest_dir / candidate).exists():
        candidate = f"{slug}_{index}{suffix}"
        index += 1
    return candidate
