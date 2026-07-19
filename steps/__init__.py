"""Step handlers and the handler registry.

Importing this package registers the built-in handlers (prompt, tool, call)
as a side effect, so the engine only needs to ``import
manuprompt.steps`` to have them available.
"""

from __future__ import annotations

from .base import (
    StepHandler,
    get_handler,
    now_iso,
    register_handler,
)

# Import handler modules for their registration side effects.
from . import call, prompt, shell, tool  # noqa: F401

__all__ = [
    "StepHandler",
    "get_handler",
    "now_iso",
    "register_handler",
]
