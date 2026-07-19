"""Step handler registry.

Handlers implement the :class:`StepHandler` protocol and register themselves
for a concrete :class:`~model.Step` subclass via :func:`register_handler`.
The engine resolves a handler purely from the step's type, so adding a new
step kind never requires touching the engine (Open/Closed).

Project-specific behaviour is not registered here: a ``call`` step resolves a
``module.function`` from the suite's own directory, which is the seam where
project code (e.g. browser automation glue) plugs in.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from ..context import RunContext
from ..errors import ManuPromptError
from ..model import Step
from ..results import StepResult


class StepHandler(Protocol):
    """Protocol implemented by every step handler."""

    def execute(self, step: Step, ctx: RunContext, phase: str) -> StepResult:
        """Execute ``step`` and return its result.

        Args:
            step: The step to execute.
            ctx: Shared execution context for the current scope.
            phase: Lifecycle phase label recorded on the result.

        Returns:
            The step's :class:`~results.StepResult`.
        """
        ...


_HANDLERS: dict[type[Step], StepHandler] = {}


def now_iso() -> str:
    """Return the current local time as an ISO-8601 string (second precision)."""
    return datetime.now().isoformat(timespec="seconds")


def register_handler(step_type: type[Step]) -> Callable[[type], type]:
    """Class decorator registering a handler for a step type.

    Args:
        step_type: The :class:`~model.Step` subclass the handler serves.

    Returns:
        The decorator, which instantiates the handler and stores it.
    """

    def decorator(cls: type) -> type:
        _HANDLERS[step_type] = cls()
        return cls

    return decorator


def get_handler(step: Step) -> StepHandler:
    """Return the registered handler for ``step``.

    Args:
        step: The step needing a handler.

    Returns:
        The handler registered for the step's concrete type.

    Raises:
        ManuPromptError: If no handler is registered for the step type.
    """
    try:
        return _HANDLERS[type(step)]
    except KeyError as exc:
        raise ManuPromptError(
            f"No handler registered for step type {type(step).__name__}"
        ) from exc
