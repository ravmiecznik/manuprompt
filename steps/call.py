"""Handler for :class:`~model.CallStep`.

A ``call`` step names a ``module.function`` reference that is resolved against
the suite's own directory (``ctx.suite_dir``): ``browser.launch`` imports
``browser.py`` from the suite directory and calls its ``launch`` function with
the run context. Loaded modules are cached per file path so repeated calls into
the same module do not re-import it.

This is the framework's only seam for project-specific behaviour, which keeps
the core itself free of any project (e.g. Playwright) dependency.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from ..context import RunContext
from ..errors import CallError
from ..model import CallStep, Step
from ..results import Outcome, StepResult
from .base import now_iso, register_handler

# Cache of imported suite modules keyed by absolute file path.
_MODULE_CACHE: dict[Path, ModuleType] = {}


@register_handler(CallStep)
class CallHandler:
    """Invoke a ``module.function`` defined alongside the suite.

    String arguments have their ``${var}`` references resolved before the
    function is called. A function that returns ``False`` records a
    :attr:`Outcome.FAIL`; any other return value (including ``None``) records
    :attr:`Outcome.PASS`. Raising is handled by the engine as an error.

    When the step declares ``artifact:`` labels and the function returns an
    existing file path (or a list of them), those files are attached under the
    labels via :meth:`~context.RunContext.attach` — the suite author names the
    artifact in YAML; glue only produces the file. Labels not covered fall
    through to operator collection.
    """

    def execute(self, step: Step, ctx: RunContext, phase: str) -> StepResult:
        """Run the call step.

        Args:
            step: The call step (a :class:`~model.CallStep`).
            ctx: Shared execution context.
            phase: Lifecycle phase label.

        Returns:
            The step result.

        Raises:
            CallError: If the target cannot be resolved or is not callable.
        """
        assert isinstance(step, CallStep)
        started = now_iso()
        func = _resolve(step.target, ctx.suite_dir)
        resolved_args: dict[str, Any] = {
            key: ctx.resolve(value) if isinstance(value, str) else value
            for key, value in step.args.items()
        }
        ctx.logger.info("Calling %s(%r)", step.target, resolved_args)
        value = func(ctx, **resolved_args)

        attached = False
        if step.artifacts:
            attached = _attach_returned_files(ctx, step.artifacts, value)

        outcome = Outcome.FAIL if value is False else Outcome.PASS
        # Hide path-return noise when it was consumed as an artifact.
        if value is None or attached:
            output = ""
        else:
            output = repr(value)
        return StepResult(
            name=step.name,
            kind="call",
            phase=phase,
            outcome=outcome,
            detail=step.target,
            output=output,
            started_at=started,
            finished_at=now_iso(),
        )


def _attach_returned_files(
    ctx: RunContext, labels: tuple[str, ...], value: Any
) -> bool:
    """Attach files returned by a call under the step's ``artifact:`` labels.

    Args:
        ctx: Active run context (attachment window must be open).
        labels: Artifact labels from the YAML step.
        value: The call's return value.

    Returns:
        ``True`` if at least one file was attached, ``False`` if ``value`` was
        not a usable path/list of paths (operator collection should run).
    """
    paths = _coerce_file_paths(value)
    if paths is None:
        return False
    if len(labels) == 1:
        for path in paths:
            ctx.attach(labels[0], path)
        return True
    for label, path in zip(labels, paths):
        ctx.attach(label, path)
    if len(paths) > len(labels):
        ctx.logger.warning(
            "call returned %d files but step has %d artifact label(s); "
            "extra file(s) ignored",
            len(paths),
            len(labels),
        )
    return True


def _coerce_file_paths(value: Any) -> list[Path] | None:
    """Return existing file paths if ``value`` is a path or sequence of paths.

    Args:
        value: A call return value.

    Returns:
        A non-empty list of paths, or ``None`` when ``value`` should not be
        treated as artifact input (booleans, ``None``, non-path strings, …).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (str, Path)):
        path = Path(value)
        return [path] if path.is_file() else None
    if isinstance(value, (list, tuple)):
        paths: list[Path] = []
        for item in value:
            if not isinstance(item, (str, Path)):
                return None
            path = Path(item)
            if not path.is_file():
                return None
            paths.append(path)
        return paths or None
    return None


def _resolve(target: str, suite_dir: Path) -> Any:
    """Resolve a ``module.function`` target to a callable.

    Args:
        target: A ``module.function`` reference (exactly one dot).
        suite_dir: Directory the module file is loaded from.

    Returns:
        The resolved callable.

    Raises:
        CallError: If the module file or function is missing, the function is
            not callable, or the module raises while importing.
    """
    module_name, _, func_name = target.partition(".")
    module = _load_module(module_name, suite_dir)
    func = getattr(module, func_name, None)
    if func is None:
        raise CallError(
            f"Function '{func_name}' not found in {suite_dir / f'{module_name}.py'}"
        )
    if not callable(func):
        raise CallError(f"'{target}' is not callable")
    return func


def _load_module(module_name: str, suite_dir: Path) -> ModuleType:
    """Import (and cache) a suite-local module by file path.

    Args:
        module_name: Bare module name (no extension).
        suite_dir: Directory the ``<module_name>.py`` file lives in.

    Returns:
        The imported module.

    Raises:
        CallError: If the file does not exist or fails to import.
    """
    module_path = (suite_dir / f"{module_name}.py").resolve()
    cached = _MODULE_CACHE.get(module_path)
    if cached is not None:
        return cached
    if not module_path.is_file():
        raise CallError(f"No module file '{module_name}.py' in {suite_dir}")

    spec = importlib.util.spec_from_file_location(
        f"manuprompt_suite_{module_name}", module_path
    )
    if spec is None or spec.loader is None:
        raise CallError(f"Cannot import module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the module can resolve its own dotted imports.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - surfaced as a CallError
        sys.modules.pop(spec.name, None)
        raise CallError(f"Failed importing {module_path}: {exc}") from exc
    _MODULE_CACHE[module_path] = module
    return module
