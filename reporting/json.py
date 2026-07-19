"""JSON reporter — the canonical, machine-readable run record.

The result is written atomically (to a temporary file then renamed) so a
reader never observes a half-written document, which matters because the
engine rewrites this file after every step for incremental persistence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..results import SuiteResult


def write_json(result: SuiteResult, path: Path) -> None:
    """Write a suite result to ``path`` as pretty-printed JSON.

    The write is atomic: the document is first written to a sibling
    temporary file which is then renamed over ``path``.

    Args:
        result: The suite result to serialise.
        path: Destination file path. Parent directories are created.
    """
    write_result_dict(result.to_dict(), path)


def write_result_dict(data: dict[str, Any], path: Path) -> None:
    """Write an already-serialised result mapping to ``path`` as JSON.

    The write is atomic (temp file + rename), matching :func:`write_json`. This
    accepts the mapping form directly, so a merged/regenerated report bundle can
    persist its ``result.json`` without reconstructing a
    :class:`~results.SuiteResult`.

    Args:
        data: A result mapping (e.g. from :meth:`~results.SuiteResult.to_dict`
            or :func:`~reporting.merge.merge_results`).
        path: Destination file path. Parent directories are created.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def load_result(path: Path) -> dict[str, Any]:
    """Load a previously written result document.

    Args:
        path: Path to a JSON result file produced by :func:`write_json`.

    Returns:
        The parsed result mapping.

    Raises:
        OSError: If the file cannot be read.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    return json.loads(path.read_text(encoding="utf-8"))
