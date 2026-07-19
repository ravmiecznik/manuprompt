"""Handler for :class:`~model.ToolStep`."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from ..context import RunContext
from ..errors import ManuPromptError
from ..model import Step, ToolStep
from ..results import Outcome, StepResult
from .base import now_iso, register_handler

# Characters not allowed in a per-tool log filename are collapsed to '_'.
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _append_tool_log(
    ctx: RunContext,
    tool_name: str,
    command: str,
    completed: subprocess.CompletedProcess[str],
    phase: str,
) -> Path:
    """Append a tool invocation's output to its dedicated ``<tool>.log`` file.

    Each registered tool accumulates a single log file in the run's artifacts
    directory, named after the tool (e.g. ``curl.log``). Every invocation
    is recorded with a header (timestamp, phase, command, exit code) followed
    by its stdout and stderr, so the whole tool history for a run is captured
    in one place.

    Args:
        ctx: Active run context (provides the artifacts directory).
        tool_name: Registered tool key used as the log file stem.
        command: Full command line that was executed.
        completed: The finished subprocess carrying stdout/stderr/returncode.
        phase: Lifecycle phase the step ran in.

    Returns:
        The path to the per-tool log file.
    """
    safe_name = _UNSAFE_FILENAME.sub("_", tool_name).strip("._-") or "tool"
    log_path = ctx.artifacts_dir / f"{safe_name}.log"
    sections = [
        f"===== {now_iso()} [{phase}] $ {command} (exit {completed.returncode}) =====",
        completed.stdout.rstrip("\n"),
    ]
    if completed.stderr.strip():
        sections.append("[stderr]")
        sections.append(completed.stderr.rstrip("\n"))
    sections.append("")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(sections) + "\n")
    return log_path


def _resolve_tool_path(value: str, suite_dir: Path) -> str:
    """Resolve a ``tools`` mapping value to an executable command prefix.

    Resolution mirrors how ``call`` steps locate their modules: a binary
    placed next to the suite YAML is preferred, so suites can ship their own
    tools. Otherwise the value is left untouched and resolved by the shell via
    ``PATH`` at execution time.

    Args:
        value: The raw value from the suite ``tools`` mapping (e.g.
            ``curl``, ``./bin/mytool``, or ``python3 -m mytool``).
        suite_dir: Directory the suite was loaded from.

    Returns:
        The command prefix to prepend to the step command. Absolute paths and
        multi-word command prefixes are returned verbatim; a single-token value
        that matches a file next to the suite is returned as a shell-quoted
        absolute path; anything else is returned unchanged for ``PATH`` lookup.
    """
    candidate = Path(value)
    if candidate.is_absolute() or len(value.split()) > 1:
        return value
    local = suite_dir / value
    if local.is_file():
        return shlex.quote(str(local.resolve()))
    return value


@register_handler(ToolStep)
class ToolHandler:
    """Run a configured command-line tool and judge it by its exit code.

    The tool name is resolved against the suite's ``tools`` mapping and the
    ``command`` (with ``${var}`` references substituted) is appended. A tool
    binary placed next to the suite YAML is preferred over one on ``PATH``, so
    suites can ship their own tools (see :func:`_resolve_tool_path`). The
    command is executed through the shell because tool arguments frequently
    contain quoting that ``shlex`` splitting would mangle (e.g. JSON payloads
    passed to a CLI tool). Suites are operator-authored and trusted, so the
    shell is an acceptable execution boundary here.
    """

    def execute(self, step: Step, ctx: RunContext, phase: str) -> StepResult:
        """Run the tool step.

        Args:
            step: The tool step (a :class:`~model.ToolStep`).
            ctx: Shared execution context.
            phase: Lifecycle phase label.

        Returns:
            The step result. :attr:`Outcome.PASS` on exit code 0, otherwise
            :attr:`Outcome.FAIL`.

        Raises:
            ManuPromptError: If the step references an unknown tool name.
        """
        assert isinstance(step, ToolStep)
        started = now_iso()

        if step.tool not in ctx.tools:
            known = ", ".join(sorted(ctx.tools)) or "(none)"
            raise ManuPromptError(
                f"Unknown tool '{step.tool}'. Declared tools: {known}"
            )

        tool_path = _resolve_tool_path(ctx.tools[step.tool], ctx.suite_dir)
        command = ctx.resolve(step.command)
        full_command = f"{tool_path} {command}".strip()
        ctx.logger.info("Running: %s", full_command)

        completed = subprocess.run(
            full_command,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        output = completed.stdout
        if completed.stderr:
            output = f"{output}\n[stderr]\n{completed.stderr}" if output else completed.stderr

        ctx.prompter.show_tool_output(
            full_command, completed.stdout, completed.stderr, label=step.tool
        )
        log_path = _append_tool_log(ctx, step.tool, full_command, completed, phase)
        ctx.logger.info("Appended %s output to %s", step.tool, log_path)

        if step.save_output:
            ctx.set_var(step.save_output, completed.stdout.strip())

        outcome = Outcome.PASS if completed.returncode == 0 else Outcome.FAIL
        return StepResult(
            name=step.name,
            kind="tool",
            phase=phase,
            outcome=outcome,
            detail=full_command,
            output=output.strip(),
            notes=f"exit code {completed.returncode}",
            started_at=started,
            finished_at=now_iso(),
        )
