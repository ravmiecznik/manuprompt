"""HTML reporter — renders a suite result into a self-contained HTML file.

The reporter consumes the same JSON-serialisable mapping produced by
:meth:`~results.SuiteResult.to_dict`, so it can render either a live result or
one re-loaded from disk. The output is a single file with inline CSS (no
external assets), suitable for archiving or sharing.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any

from .._text import rtf_to_text, strip_ansi
from ..theme import REPORT_DEFAULTS, Theme, css_variables

_PAGE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>
  :root {
    color-scheme: light dark;
$theme_vars
  }
  body { font-family: var(--font); margin: 2rem; line-height: 1.45;
         background: var(--bg); color: var(--fg); }
  h1 { margin: 0 0 .25rem; }
  .desc { color: var(--muted); margin: 0 0 1rem; }
  .meta { color: var(--muted); font-size: .9rem; margin-bottom: 1rem; }
  table { border-collapse: collapse; width: 100%; margin: .5rem 0 1.5rem; }
  th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--border);
           vertical-align: top; }
  th { font-size: .8rem; text-transform: uppercase; letter-spacing: .03em; color: var(--muted); }
  h2 { margin: 1.5rem 0 .25rem; }
  details.case { margin: .35rem 0; border-bottom: 1px solid var(--border); }
  details.case > summary { list-style: none; cursor: pointer; padding: .45rem .25rem;
           font-size: 1.15rem; font-weight: 600; display: flex; flex-wrap: wrap; align-items: center;
           gap: .5rem; border-radius: .3rem; }
  details.case > summary:hover { background: color-mix(in srgb, var(--fg) 5%, transparent); }
  details.case > summary::-webkit-details-marker { display: none; }
  details.case > summary::before { content: "\\25b8"; color: var(--muted); font-size: .9em;
           width: 1em; flex: 0 0 auto; }
  details.case[open] > summary::before { content: "\\25be"; }
  details.case > summary .case-id { font-family: var(--mono-font);
           font-weight: 700; }
  details.case > summary .case-name { font-weight: 600; }
  .case-desc { flex: 1 0 100%; color: var(--muted); margin: 0 0 0 1.5rem; font-size: .9rem;
           font-weight: 400; }
  .case-head { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; padding: .45rem .25rem;
           margin: .35rem 0; font-size: 1.15rem; border-bottom: 1px solid var(--border); }
  .case-head::before { content: "\\2014"; color: var(--muted); width: 1em; flex: 0 0 auto;
           text-align: center; }
  details.case .skip-reason, .case-head + .skip-reason { margin-top: .4rem; }
  .badge { display: inline-block; padding: .1rem .5rem; border-radius: .6rem;
           font-size: .78rem; font-weight: 600; color: #fff; }
  .pass { background: var(--success); } .fail { background: var(--danger); }
  .error { background: color-mix(in srgb, var(--danger) 70%, #7b1fa2 30%); }
  .ack { background: var(--warning); color: #222; }
  .skip { background: var(--muted); }
  .not-tested { background: color-mix(in srgb, var(--muted) 70%, var(--fg) 30%); }
  .env th { text-transform: none; letter-spacing: 0; color: var(--fg); width: 12rem;
            font-weight: 600; }
  .stepname { display: inline-block; max-width: 32rem; overflow: hidden;
              text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom;
              cursor: help; }
  .skip-reason { margin: -.1rem 0 .5rem; padding: .35rem .6rem; border-radius: .3rem;
                 background: color-mix(in srgb, var(--muted) 15%, transparent);
                 color: var(--muted); font-size: .9rem; }
  .detail { font-family: var(--mono-font); font-size: .85rem; }
  .note { color: var(--muted); font-size: .85rem; }
  .authored-note { font-size: .85rem; margin-bottom: .3rem; padding: .25rem .5rem;
                   border-left: 3px solid var(--warning);
                   background: color-mix(in srgb, var(--warning) 10%, transparent);
                   border-radius: .2rem; }
  pre { margin: .3rem 0 0; padding: .4rem .6rem;
        background: color-mix(in srgb, var(--fg) 6%, transparent);
        border-radius: .3rem; overflow-x: auto; font-size: .8rem; max-height: 16rem; }
  summary { cursor: pointer; }
  .artifacts { display: flex; flex-wrap: wrap; gap: .6rem; margin-top: .4rem; }
  .artifact { margin: 0; max-width: 14rem; }
  .artifact img { max-width: 100%; max-height: 12rem; border-radius: .3rem;
                  border: 1px solid color-mix(in srgb, var(--fg) 12%, transparent);
                  display: block; }
  .artifact.video { max-width: 28rem; }
  .artifact.video video { max-width: 100%; max-height: 18rem; border-radius: .3rem;
                  border: 1px solid color-mix(in srgb, var(--fg) 12%, transparent);
                  display: block; background: #000; }
  .artifact-rtf { display: inline-flex; gap: .4rem; align-items: center; }
  .artifact figcaption { font-size: .75rem; color: var(--muted); margin-top: .2rem; }
  .artifact-link { display: inline-block; padding: .25rem .5rem; border-radius: .3rem;
                   background: color-mix(in srgb, var(--accent) 12%, transparent);
                   color: var(--accent); text-decoration: none;
                   font-size: .8rem; }
  .logs h3 { margin: .8rem 0 .2rem; font-size: 1rem; }
  .logs ul { margin: .2rem 0 .6rem; padding-left: 1.2rem; }
  .logs a { color: var(--accent); }
</style>
</head>
<body>
<h1>$title <span class="badge $outcome_class">$outcome</span></h1>
<p class="desc">$description</p>
<p class="meta">Started $started &nbsp;·&nbsp; finished $finished &nbsp;·&nbsp;
   generated $generated</p>
<p>$summary</p>
$environment
$sections
$logs
</body>
</html>
"""
)


def write_html(data: dict[str, Any], path: Path) -> None:
    """Render a suite-result mapping to a self-contained HTML file.

    Args:
        data: A mapping as produced by :meth:`~results.SuiteResult.to_dict`.
        path: Destination ``.html`` file. Parent directories are created.
    """
    base_dir = path.parent
    sections: list[str] = []
    setup = data.get("suite_setup") or []
    if setup:
        sections.append(_section("Suite setup", setup, base_dir))
    for case in data.get("cases") or []:
        sections.append(_case_section(case, base_dir))
    teardown = data.get("suite_teardown") or []
    if teardown:
        sections.append(_section("Suite teardown", teardown, base_dir))

    theme = Theme.from_mapping(data.get("theme"))
    page = _PAGE.safe_substitute(
        title=_esc(data.get("name", "ManuPrompt report")),
        theme_vars=css_variables(theme, REPORT_DEFAULTS),
        description=_esc(data.get("description", "")),
        outcome=_esc(str(data.get("outcome", "")).upper()),
        outcome_class=_esc(str(data.get("outcome", "skip"))),
        started=_esc(data.get("started_at", "")),
        finished=_esc(data.get("finished_at", "")),
        generated=datetime.now().isoformat(timespec="seconds"),
        summary=_summary(data.get("counts") or {}),
        environment=_environment(data.get("test_environment") or []),
        sections="\n".join(sections),
        logs=_logs(data.get("logs") or []),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")


def _environment(entries: list[dict[str, Any]]) -> str:
    """Return an HTML section listing the test-environment entries.

    Args:
        entries: List of ``{"label", "value"}`` mappings.

    Returns:
        An HTML ``<h2>`` + table, or an empty string when there are no entries.
    """
    if not entries:
        return ""
    rows = "\n".join(
        "<tr>"
        f"<th>{_esc(entry.get('label', ''))}</th>"
        f"<td>{_esc(entry.get('value', ''))}</td>"
        "</tr>"
        for entry in entries
    )
    return (
        "<h2>Test environment</h2>\n"
        "<table class='env'><tbody>\n" + rows + "\n</tbody></table>"
    )


def _logs(groups: list[dict[str, Any]]) -> str:
    """Return an HTML section linking to logs collected from prior runs.

    Args:
        groups: ``{"source", "files"}`` mappings, where ``files`` are paths
            relative to the report file.

    Returns:
        An HTML ``<h2>`` + per-source link lists, or an empty string when there
        are no collected logs.
    """
    if not groups:
        return ""
    blocks: list[str] = []
    for group in groups:
        files = group.get("files") or []
        if not files:
            continue
        items = "\n".join(
            f'<li><a href="{_esc(rel)}" target="_blank">{_esc(rel.rsplit("/", 1)[-1])}'
            "</a></li>"
            for rel in files
        )
        blocks.append(
            f"<h3>{_esc(group.get('source', ''))}</h3>\n<ul>\n{items}\n</ul>"
        )
    if not blocks:
        return ""
    return "<section class='logs'><h2>Logs</h2>\n" + "\n".join(blocks) + "\n</section>"


def _summary(counts: dict[str, int]) -> str:
    """Return a row of outcome badges for the non-zero case tallies."""
    parts = [
        f'<span class="badge {key}">{key.upper()}: {value}</span>'
        for key, value in counts.items()
        if value
    ]
    return " ".join(parts) or "<em>no test cases</em>"


def _case_section(case: dict[str, Any], base_dir: Path) -> str:
    """Return an HTML section for a single test case.

    The case heading (id, name, outcome badge) is always visible; its step
    table is collapsed into a ``<details>`` so the default report is a clean
    list of cases that the reader expands on demand. A case with nothing to
    show (e.g. ``NOT-TESTED`` with no steps or reason) renders as a flat,
    non-expandable heading.

    Args:
        case: The case-result mapping.
        base_dir: Directory the report is written to (used to resolve and read
            artifact files for inline rendering).
    """
    summary = (
        f"<span class='case-id'>{_esc(case.get('id', ''))}</span>"
        f"<span class='case-name'>{_esc(case.get('name', ''))}</span> "
        f"{_badge(case.get('outcome', 'skip'))}"
    )
    description = str(case.get("description") or "").strip()
    if description:
        summary += f"<span class='case-desc'>{_esc(description)}</span>"
    reason = str(case.get("skip_reason") or "").strip()
    reason_html = (
        f"<p class='skip-reason'>Skipped: {_linkify(reason)}</p>\n" if reason else ""
    )
    steps = case.get("steps") or []
    body = (
        reason_html
        + (_steps_table(steps, base_dir) if steps else "")
        + _case_logs(case.get("logs") or [])
    )
    if not body:
        return f"<div class='case-head'>{summary}</div>"
    return f"<details class='case'><summary>{summary}</summary>\n{body}</details>"


def _case_logs(files: list[Any]) -> str:
    """Return an HTML block linking a case to its logs.

    Each entry is either a ``{"label", "path"}`` mapping (a file attached to the
    case via :meth:`~context.RunContext.attach_log`, e.g. a per-test device log)
    or a plain path string (a source run's loose log file, linked by basename).
    Paths are relative to the report file.

    Args:
        files: Log entries (dicts and/or path strings) for the case.

    Returns:
        An HTML ``<div class="logs">`` block, or an empty string when empty.
    """
    if not files:
        return ""
    items: list[str] = []
    for entry in files:
        if isinstance(entry, dict):
            rel = str(entry.get("path", ""))
            text = str(entry.get("label") or rel.rsplit("/", 1)[-1])
        else:
            rel = str(entry)
            text = rel.rsplit("/", 1)[-1]
        if not rel:
            continue
        items.append(
            f'<li><a href="{_esc(rel)}" target="_blank">\U0001f4c4 {_esc(text)}</a></li>'
        )
    if not items:
        return ""
    return "<div class='logs'><h3>Logs</h3>\n<ul>\n" + "\n".join(items) + "\n</ul></div>"


def _linkify(text: str) -> str:
    """Render ``text`` as an anchor when it is an http(s) URL, else escaped."""
    if text.startswith(("http://", "https://")) and " " not in text:
        return f'<a href="{_esc(text)}">{_esc(text)}</a>'
    return _esc(text)


def _section(title: str, steps: list[dict[str, Any]], base_dir: Path) -> str:
    """Return a collapsible HTML section (heading + steps table) for a phase."""
    summary = f"<span class='case-name'>{_esc(title)}</span>"
    return (
        f"<details class='case'><summary>{summary}</summary>\n"
        f"{_steps_table(steps, base_dir)}</details>"
    )


def _steps_table(steps: list[dict[str, Any]], base_dir: Path) -> str:
    """Return an HTML table rendering a list of step-result mappings."""
    if not steps:
        return "<p class='note'>No steps.</p>"
    rows = "\n".join(_step_row(step, base_dir) for step in steps)
    return (
        "<table><thead><tr>"
        "<th>Outcome</th><th>Step</th><th>Detail</th><th>Notes</th>"
        "</tr></thead><tbody>\n" + rows + "\n</tbody></table>"
    )


def _step_row(step: dict[str, Any], base_dir: Path) -> str:
    """Return a single ``<tr>`` for a step-result mapping."""
    notes = step.get("notes") or ""
    error = step.get("error") or ""
    authored = str(step.get("note") or "").strip()
    note_html = (
        f'<div class="authored-note">{_esc(authored)}</div>' if authored else ""
    )
    if notes:
        note_html += f'<span class="note">{_esc(notes)}</span>'
    if error:
        note_html += f'<span class="badge error">{_esc(error)}</span>'
    output = strip_ansi(step.get("output") or "")
    detail = _esc(step.get("detail", ""))
    if output:
        detail += f"<details><summary>output</summary><pre>{_esc(output)}</pre></details>"
    detail += _artifacts(step.get("artifacts") or [], base_dir)
    return (
        "<tr>"
        f"<td>{_badge(step.get('outcome', 'skip'))}</td>"
        f"<td>{_step_name(step)}<br>"
        f"<span class='note'>{_esc(step.get('phase', ''))}</span></td>"
        f"<td class='detail'>{detail}</td>"
        f"<td>{note_html}</td>"
        "</tr>"
    )


def _step_name(step: dict[str, Any]) -> str:
    """Return the step name, truncated visually with the full text on hover.

    The full step name is kept in the data; long names are clipped with a CSS
    ellipsis and the complete text is exposed via the native ``title`` tooltip.

    Args:
        step: A step-result mapping.

    Returns:
        HTML for the step name cell content.
    """
    name = str(step.get("name", ""))
    return f'<span class="stepname" title="{_esc(name)}">{_esc(name)}</span>'


_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
)

# Video suffixes embedded with a <video> player (plus a download fallback).
# No MIME ``type`` is sent on the <source>: a ``video/quicktime`` hint makes
# Chrome reject a ``.mov`` outright even when its codec (e.g. H.264) is
# playable, so we let the browser sniff the actual content instead.
_VIDEO_SUFFIXES = frozenset({".mp4", ".m4v", ".mov", ".webm", ".ogv"})


def _artifacts(artifacts: list[dict[str, str]], base_dir: Path) -> str:
    """Return HTML for a step's attached artifacts.

    Images embed inline; videos (incl. ``.mov``) embed in a ``<video>`` player;
    RTF files link to a generated full-page text view (opened in a new tab);
    every artifact also gets a download link. Paths are relative to the report
    file's directory (``base_dir``), which is also where RTF files are read and
    their view pages written.

    Args:
        artifacts: ``{"label", "path"}`` mappings collected for the step.
        base_dir: Directory the report is written to.

    Returns:
        An HTML fragment, or an empty string when there are no artifacts.
    """
    if not artifacts:
        return ""
    items: list[str] = []
    for art in artifacts:
        label = _esc(art.get("label", ""))
        path = art.get("path", "")
        href = _esc(path)
        suffix = Path(path).suffix.lower()
        if suffix in _IMAGE_SUFFIXES:
            items.append(
                f'<figure class="artifact"><a href="{href}" target="_blank">'
                f'<img src="{href}" alt="{label}"></a>'
                f"<figcaption>{label}</figcaption></figure>"
            )
        elif suffix in _VIDEO_SUFFIXES:
            items.append(_video_artifact(label, href))
        elif suffix == ".rtf":
            items.append(_rtf_artifact(label, path, base_dir / path))
        else:
            items.append(_download_link(label, href))
    return f'<div class="artifacts">{"".join(items)}</div>'


def _download_link(label: str, href: str) -> str:
    """Return a paperclip download link for an artifact."""
    return f'<a class="artifact-link" href="{href}" target="_blank">\U0001f4ce {label}</a>'


def _video_artifact(label: str, href: str) -> str:
    """Return an inline ``<video>`` player with a download fallback.

    The source carries no MIME ``type`` so the browser sniffs the content
    (needed for ``.mov`` to play in Chrome when the codec is supported); the
    download link covers browsers that cannot decode it.
    """
    return (
        f'<figure class="artifact video">'
        f'<video controls preload="metadata" src="{href}"></video>'
        f"<figcaption>{label} &nbsp; {_download_link('download', href)}"
        "</figcaption></figure>"
    )


def _rtf_artifact(label: str, path: str, src: Path) -> str:
    """Render an RTF artifact as a link to a full-page text view (new tab).

    RTF is not browser-renderable, so a companion ``<file>.html`` page holding
    the extracted text is written next to the file and linked with
    ``target="_blank"`` — giving a full-width view rather than a cramped inline
    box. Formatting is not preserved. The original ``.rtf`` is also offered for
    download. Falls back to just the download link if the file cannot be read or
    the view page cannot be written.

    Args:
        label: Artifact label.
        path: Relative path to the ``.rtf`` (used to build hrefs).
        src: Absolute path to the ``.rtf`` to read and to site its view page.
    """
    href = _esc(path)
    try:
        raw = src.read_text(encoding="utf-8", errors="replace")
        text = rtf_to_text(raw).strip()
        if not text:
            return _download_link(label, href)
        src.with_name(src.name + ".html").write_text(
            _rtf_view_page(art_label=str(label), text=text), encoding="utf-8"
        )
    except OSError:
        return _download_link(label, href)
    view_href = _esc(path + ".html")
    return (
        '<span class="artifact-rtf">'
        f'<a class="artifact-link" href="{view_href}" target="_blank">\U0001f4c4 {label}</a> '
        f'{_download_link(".rtf", href)}</span>'
    )


def _rtf_view_page(*, art_label: str, text: str) -> str:
    """Return a standalone, readable HTML page rendering RTF-extracted text."""
    return (
        "<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(art_label)}</title><style>"
        ":root{color-scheme:light dark;}"
        "body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.5;}"
        "h1{font-size:1.1rem;color:#666;}"
        "pre{white-space:pre-wrap;word-break:break-word;"
        "font-family:ui-monospace,monospace;font-size:.9rem;}"
        f"</style></head><body><h1>{_esc(art_label)} (text extracted from RTF)</h1>"
        f"<pre>{_esc(text)}</pre></body></html>\n"
    )


def _badge(outcome: str) -> str:
    """Return a coloured badge span for an outcome value."""
    safe = _esc(str(outcome))
    return f'<span class="badge {safe}">{safe.upper()}</span>'


def _esc(value: Any) -> str:
    """HTML-escape a value for safe inclusion in the document."""
    return html.escape(str(value), quote=True)
