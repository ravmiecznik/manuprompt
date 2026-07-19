"""Markdown reporter — renders a suite result into a GitHub-flavored Markdown file.

Consumes the same JSON-serialisable mapping produced by
:meth:`~results.SuiteResult.to_dict` as the HTML reporter, so it renders either a
live result or one re-loaded from disk. It is deliberately **leaner** than the
HTML report: it presents *results* — outcomes, steps, notes and artifacts —
without the bulky evidence the HTML report carries. Captured tool output and
log-file links are omitted (the JSON result and the HTML report remain the full
record), keeping the Markdown clean and easy to paste into pull requests, wikis
or issues.

Tables are plain **Markdown pipe-tables**: they render consistently across
viewers (the renderer sizes each column to its content, so the long step text
naturally gets the most room), unlike HTML tables whose width hints many viewers
ignore or mis-apply. Readability instead comes from keeping the columns few and
merging the step name with its resolved detail. Artifact paths are written
relative to the report file (matching the HTML reporter's relocated layout).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

# Leading glyph per outcome, giving a scannable status column that still reads
# sensibly as plain text (the uppercase label is always kept alongside).
_OUTCOME_EMOJI: dict[str, str] = {
    "pass": "✅",       # ✅
    "fail": "❌",       # ❌
    "error": "\U0001f534",  # 🔴
    "ack": "\U0001f7e1",    # 🟡
    "skip": "⚪",       # ⚪
    "not-tested": "⚫",  # ⚫
}


def write_markdown(data: dict[str, Any], path: Path) -> None:
    """Render a suite-result mapping to a GitHub-flavored Markdown file.

    Args:
        data: A mapping as produced by :meth:`~results.SuiteResult.to_dict`.
        path: Destination ``.md`` file. Parent directories are created.
    """
    outcome = str(data.get("outcome", "skip"))
    lines: list[str] = [
        f"# {_esc(data.get('name', 'ManuPrompt report'))} "
        f"— {_outcome_label(outcome)}",
        "",
    ]
    description = str(data.get("description", "")).strip()
    if description:
        lines += [_esc(description), ""]
    generated = datetime.now().isoformat(timespec="seconds")
    lines += [
        f"_Started {_esc(data.get('started_at', '?'))} · "
        f"finished {_esc(data.get('finished_at', '?'))} · "
        f"generated {generated}_",
        "",
        f"**Summary:** {_summary(data.get('counts') or {})}",
        "",
    ]

    lines += _environment(data.get("test_environment") or [])

    setup = data.get("suite_setup") or []
    if setup:
        lines += _phase_section("Suite setup", setup)
    for case in data.get("cases") or []:
        lines += _case_section(case)
    teardown = data.get("suite_teardown") or []
    if teardown:
        lines += _phase_section("Suite teardown", teardown)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _environment(entries: list[dict[str, Any]]) -> list[str]:
    """Return Markdown lines for the test-environment table, or ``[]``."""
    if not entries:
        return []
    rows = [
        [_cell(entry.get("label", "")), _cell(entry.get("value", ""))]
        for entry in entries
    ]
    return ["## Test environment", "", *_table(["Field", "Value"], rows)]


def _phase_section(title: str, steps: list[dict[str, Any]]) -> list[str]:
    """Return Markdown lines for a suite setup/teardown phase."""
    return [f"## {_esc(title)}", "", *_steps(steps)]


def _case_section(case: dict[str, Any]) -> list[str]:
    """Return Markdown lines for a single test case (heading, steps, artifacts)."""
    heading = (
        f"## `{_esc(case.get('id', ''))}` {_esc(case.get('name', ''))} "
        f"— {_outcome_label(case.get('outcome', 'skip'))}"
    )
    lines = [heading, ""]
    reason = str(case.get("skip_reason") or "").strip()
    if reason:
        lines += [f"> **Skipped:** {_esc(reason)}", ""]
    lines += _steps(case.get("steps") or [])
    return lines


def _steps(steps: list[dict[str, Any]]) -> list[str]:
    """Return Markdown lines rendering a list of step-result mappings.

    The former Step and Detail columns are **merged** into one column (the step
    name, with the resolved detail below it when it differs) so few, content-sized
    columns remain and the step text is not squeezed into a couple of words. A
    trailing **Artifacts** column of links is added only when some step in this
    table has artifacts. Captured output and logs are intentionally omitted (see
    the module docstring).
    """
    if not steps:
        return ["_No steps._", ""]
    has_artifacts = any(step.get("artifacts") for step in steps)
    headers = ["#", "Outcome", "Step", "Notes"]
    if has_artifacts:
        headers.append("Artifacts")
    rows: list[list[str]] = []
    for index, step in enumerate(steps, start=1):
        row = [
            str(index),
            _outcome_label(step.get("outcome", "skip")),
            _step_cell(step),
            _cell(_notes(step)),
        ]
        if has_artifacts:
            row.append(_artifact_cell(step.get("artifacts") or []))
        rows.append(row)
    return _table(headers, rows)


def _step_cell(step: dict[str, Any]) -> str:
    """Return the merged Step cell: the step name, and the resolved detail below.

    The detail (a resolved command, or a prompt with its variables substituted)
    is shown on a second line only when it differs from the name — for simple
    steps the two are identical, so just the name appears. Merging what used to
    be two wide columns into one keeps the table to a few content-sized columns.
    """
    name = str(step.get("name", ""))
    detail = str(step.get("detail", ""))
    if detail and detail != name:
        return _cell(f"{name}\n{detail}")
    return _cell(name)


def _notes(step: dict[str, Any]) -> str:
    """Combine a step's authored note, operator notes and error into one cell."""
    parts: list[str] = []
    authored = str(step.get("note") or "").strip()
    if authored:
        parts.append(authored)
    notes = str(step.get("notes") or "").strip()
    if notes:
        parts.append(notes)
    error = str(step.get("error") or "").strip()
    if error:
        parts.append(f"error: {error}")
    return " — ".join(parts)


def _artifact_cell(artifacts: list[dict[str, str]]) -> str:
    """Return a Markdown table-cell listing a step's artifacts as links.

    Each artifact is a plain Markdown link (not an inline ``![]`` image, which
    renders as broken alt-text when the ``.md`` is viewed away from its
    ``attachments/`` folder); multiple links are separated by ``<br>``. Paths are
    relative to the report file, matching the HTML reporter's relocated layout.
    """
    links: list[str] = []
    for art in artifacts:
        path = str(art.get("path", ""))
        if not path:
            continue
        label = _inline(art.get("label", "") or Path(path).name)
        links.append(f"[{label}]({_link_target(path)})")
    return "<br>".join(links)


def _summary(counts: dict[str, int]) -> str:
    """Return a one-line tally of the non-zero case outcomes."""
    parts = [
        f"{_outcome_label(key)}: {value}"
        for key, value in counts.items()
        if value
    ]
    return " · ".join(parts) or "_no test cases_"


def _outcome_label(outcome: Any) -> str:
    """Return an ``<emoji> OUTCOME`` label for an outcome value."""
    value = str(outcome)
    emoji = _OUTCOME_EMOJI.get(value, "")
    label = value.upper()
    return f"{emoji} {label}".strip()


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Return lines for a GitHub-flavored Markdown pipe-table.

    Standard Markdown tables render consistently across viewers (the renderer
    sizes columns to their content), unlike HTML tables whose width hints are
    often ignored or mis-applied. Cell values must already be pipe-safe — pass
    them through :func:`_cell` (or :func:`_inline` for link labels).

    Args:
        headers: Column header labels.
        rows: Row cells (each row a list matching ``headers``), already escaped.

    Returns:
        The header, separator and body lines, plus a trailing blank line.
    """
    head = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows]
    return [head, separator, *body, ""]


def _cell(value: Any) -> str:
    """Escape a value for a single Markdown table cell.

    Cells cannot span lines, so newlines collapse to ``<br>``; pipes are escaped
    so they do not split the row.
    """
    return _inline(value).replace("\r\n", "\n").replace("\n", "<br>").replace("|", "\\|")


def _link_target(path: str) -> str:
    """Encode characters in a relative link target that would break the syntax."""
    return path.replace(" ", "%20").replace("|", "%7C")


def _inline(value: Any) -> str:
    """Escape Markdown control characters that would corrupt inline text."""
    text = str(value)
    for char in ("\\", "`", "*", "_", "[", "]", "<", ">"):
        text = text.replace(char, "\\" + char)
    return text


# Backwards-compatible alias: headings/prose escaping is the same as inline.
_esc = _inline
