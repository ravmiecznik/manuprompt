"""Reporters that turn a :class:`~results.SuiteResult` into output formats.

The JSON reporter is the canonical, machine-readable record and is written
incrementally during a run. Other formats (HTML, Markdown) render from the same
result structure.
"""

from __future__ import annotations

from .html import write_html
from .json import load_result, write_json, write_result_dict
from .markdown import write_markdown
from .merge import (
    SKIP_CASE,
    UNTESTED,
    CaseCandidate,
    Chooser,
    apply_untested,
    collect_logs,
    first_candidate,
    merge_results,
    relocate_artifacts,
)

__all__ = [
    "SKIP_CASE",
    "UNTESTED",
    "CaseCandidate",
    "Chooser",
    "apply_untested",
    "collect_logs",
    "first_candidate",
    "load_result",
    "merge_results",
    "relocate_artifacts",
    "write_html",
    "write_json",
    "write_markdown",
    "write_result_dict",
]
