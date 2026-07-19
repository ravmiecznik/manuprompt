"""Execution context shared by step handlers.

A :class:`RunContext` bundles the mutable variable scope, the tool registry,
the artifacts directory, a logger and the operator :class:`~prompter.Prompter`
into a single object passed to every step handler. Handlers read and write
variables through it and resolve ``${var}`` references against it.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import VariableError
from .prompter import Prompter

if TYPE_CHECKING:
    from .results import SuiteResult, TestCaseResult
    from .webui import WebGIO

# Matches ``${name}`` references inside instruction / command strings.
_VAR_RE = re.compile(r"\$\{([^}]+)\}")

# Characters not allowed in an attached-file filename are collapsed to '_'.
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class RunContext:
    """Mutable per-scope execution context handed to step handlers.

    A fresh context is created for each scope (suite setup/teardown and each
    test case) so that variables stored during one case do not leak into the
    next. The prompter, logger, tool registry, artifacts directory, the
    ``resources`` store and the teardown registry are shared by reference
    across every scope of a single run.

    The distinction between ``variables`` and ``resources`` is deliberate:
    ``variables`` are string-interpolable values scoped to the current case,
    whereas ``resources`` hold live Python objects (e.g. a device handle)
    that must persist for the whole run and be cleaned up via
    :meth:`add_teardown`.

    Attributes:
        variables: Variable scope for ``${var}`` resolution and ``store`` /
            ``save_output`` writes.
        tools: Mapping of tool name to binary path / launcher.
        artifacts_dir: Directory for run artifacts (tool output, captures).
        prompter: Operator I/O implementation.
        logger: Logger used by handlers and the engine.
        resources: Run-scoped store of live objects shared across scopes.
        suite_dir: Directory the suite was loaded from, used by ``call``
            steps to resolve their module.
        result: The in-progress :class:`~results.SuiteResult` for the run,
            so steps (e.g. a report generator) can read results so far.
        web_gio: The run's live browser I/O surface, or ``None`` when disabled.
            Producers (glue/handlers) obtain a channel via
            ``ctx.web_gio.channel(name)`` to stream output and, optionally,
            register an input handler with ``channel.on_input(...)``.
    """

    def __init__(
        self,
        variables: dict[str, Any],
        tools: dict[str, str],
        artifacts_dir: Path,
        prompter: Prompter,
        logger: logging.Logger,
        resources: dict[str, Any],
        teardowns: list[Callable[[], None]],
        suite_dir: Path,
        result: SuiteResult | None = None,
        web_gio: WebGIO | None = None,
        case_result: TestCaseResult | None = None,
    ) -> None:
        """Initialise the context.

        Args:
            variables: Initial variable scope (copied defensively).
            tools: Tool-name to path mapping (copied defensively).
            artifacts_dir: Directory for run artifacts.
            prompter: Operator I/O implementation.
            logger: Logger for handlers and the engine.
            resources: Run-scoped object store (shared by reference).
            teardowns: Run-scoped cleanup registry (shared by reference); the
                engine drains it in reverse order at the end of the run.
            suite_dir: Directory the suite was loaded from.
            result: The in-progress suite result (shared by reference).
            web_gio: The run's live browser I/O surface (shared by reference),
                or ``None`` when disabled.
            case_result: The test case currently executing (shared by
                reference), or ``None`` outside a case (suite setup/teardown).
                Used by :meth:`attach_log`.
        """
        self.variables: dict[str, Any] = dict(variables)
        self.tools: dict[str, str] = dict(tools)
        self.artifacts_dir = artifacts_dir
        self.prompter = prompter
        self.logger = logger
        self.resources = resources
        self.suite_dir = suite_dir
        self.result = result
        self.web_gio = web_gio
        self.case_result = case_result
        self._teardowns = teardowns
        # Populated by the engine around each leaf step so :meth:`attach` can
        # record files on the in-progress step without the handler knowing.
        self._step_artifacts: list[dict[str, str]] | None = None

    def set_var(self, name: str, value: Any) -> None:
        """Store a value into the variable scope.

        Args:
            name: Variable name.
            value: Value to store.
        """
        self.variables[name] = value

    def add_teardown(self, cleanup: Callable[[], None]) -> None:
        """Register a cleanup callable to run when the suite run ends.

        Cleanups are drained by the engine in reverse registration order
        inside a ``finally`` block, so they run on normal completion, on
        error, and on operator interruption.

        Args:
            cleanup: A zero-argument callable performing the cleanup.
        """
        self._teardowns.append(cleanup)

    def attach(self, label: str, path: str | Path) -> str | None:
        """Attach a file to the current step as an artifact.

        Copies ``path`` into the run's ``attachments/`` directory and records a
        ``{"label", "path"}`` entry on the step currently executing. The report
        renders it under that step exactly like files collected via the YAML
        ``artifact:`` modifier (images inline, videos playable, etc.).

        This is the low-level counterpart of declaring ``artifact:`` in YAML.
        Prefer returning a file path from a ``call:`` step that already has
        ``artifact:`` labels so the name stays in the suite; use this when glue
        must attach without returning a path.

        Args:
            label: Human-readable name shown for the artifact.
            path: Path to the file to attach.

        Returns:
            The stored path relative to the artifacts directory, or ``None`` if
            nothing was attached.
        """
        if self._step_artifacts is None:
            self.logger.warning("attach(%r) ignored: not inside a step", label)
            return None
        rel = self._copy_attachment(label, path, default_stem="artifact")
        if rel is None:
            return None
        self._step_artifacts.append({"label": label, "path": rel})
        self.logger.info("Attached artifact %r -> %s", label, rel)
        return rel

    def attach_log(self, label: str, path: str | Path) -> str | None:
        """Attach a file to the current test case as a labelled log.

        Copies ``path`` into the run's ``attachments/`` directory and records a
        ``{"label", "path"}`` entry on the current case result, which the report
        links beneath that test under Logs (not as a step artifact). This is
        the domain-agnostic seam for glue to surface a per-test file (e.g. a
        device console log capturing only that test's lines) — the core
        attaches whatever file it is given.

        Prefer a ``call:`` that returns a path with YAML ``artifact:`` (or
        :meth:`attach`) when the file belongs to a specific step; use this for
        case-scoped evidence.

        Has no effect outside a test case (suite setup/teardown), or if the
        source file is missing; a warning is logged in those cases.

        Args:
            label: Human-readable name shown for the link.
            path: Path to the file to attach.

        Returns:
            The stored path relative to the artifacts directory, or ``None`` if
            nothing was attached.
        """
        if self.case_result is None:
            self.logger.warning("attach_log(%r) ignored: not inside a test case", label)
            return None
        rel = self._copy_attachment(label, path, default_stem="log")
        if rel is None:
            return None
        # ``attached: True`` marks this as a genuine per-test log so a merge
        # carries it forward (and distinguishes it from render-only run-level
        # loose files, which are never persisted).
        self.case_result.logs.append({"label": label, "path": rel, "attached": True})
        self.logger.info("Attached log %r -> %s", label, rel)
        return rel

    def _copy_attachment(
        self, label: str, path: str | Path, *, default_stem: str
    ) -> str | None:
        """Copy ``path`` into ``attachments/`` and return its relative path.

        Args:
            label: Operator-facing label (slugified into the filename).
            path: Source file to copy.
            default_stem: Filename stem used when ``label`` slugifies to empty.

        Returns:
            Path relative to :attr:`artifacts_dir`, or ``None`` if the source
            is missing.
        """
        src = Path(path)
        if not src.is_file():
            self.logger.warning("attachment %r: no such file: %s", label, src)
            return None
        dest_dir = self.artifacts_dir / "attachments"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_name = self._unique_attachment_name(
            dest_dir, label, src.suffix, default_stem=default_stem
        )
        shutil.copy2(src, dest_dir / dest_name)
        return (dest_dir / dest_name).relative_to(self.artifacts_dir).as_posix()

    def _unique_attachment_name(
        self,
        dest_dir: Path,
        label: str,
        suffix: str,
        *,
        default_stem: str = "attachment",
    ) -> str:
        """Return a collision-free filename for an attached file.

        Args:
            dest_dir: Directory the file will be copied into.
            label: Operator-facing label (slugified into the filename).
            suffix: File extension to preserve (e.g. ``.log``).
            default_stem: Stem used when ``label`` slugifies to empty.

        Returns:
            A filename not yet present in ``dest_dir``.
        """
        stem = _UNSAFE_FILENAME.sub("_", label).strip("._-") or default_stem
        candidate = f"{stem}{suffix}"
        index = 1
        while (dest_dir / candidate).exists():
            candidate = f"{stem}_{index}{suffix}"
            index += 1
        return candidate

    def resolve(self, text: str) -> str:
        """Substitute every ``${var}`` reference in ``text``.

        Args:
            text: Template string possibly containing ``${var}`` references.

        Returns:
            The text with all references replaced by their string values.

        Raises:
            VariableError: If a referenced variable is undefined or still
                holds ``None``.
        """

        def _replace(match: re.Match[str]) -> str:
            key = match.group(1).strip()
            if key not in self.variables:
                raise VariableError(f"Undefined variable '${{{key}}}'")
            value = self.variables[key]
            if value is None:
                raise VariableError(
                    f"Variable '${{{key}}}' is referenced before it is set"
                )
            return str(value)

        return _VAR_RE.sub(_replace, text)
