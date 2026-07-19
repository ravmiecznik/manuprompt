"""Handler for :class:`~model.ShellStep`."""

from __future__ import annotations

import subprocess

from ..context import RunContext
from ..model import ShellStep, Step
from ..results import Outcome, StepResult
from .base import now_iso, register_handler


@register_handler(ShellStep)
class ShellHandler:
    """Run an arbitrary shell command, judged by its exit code.

    Unlike a ``tool`` step, the command is not resolved against the suite's
    ``tools`` mapping; it runs verbatim after ``${var}`` substitution. The
    command is executed through the shell so pipes, globs and redirections
    work. Suites are operator-authored and trusted, so the shell is an
    acceptable execution boundary here.
    """

    def execute(self, step: Step, ctx: RunContext, phase: str) -> StepResult:
        """Run the shell step.

        Args:
            step: The shell step (a :class:`~model.ShellStep`).
            ctx: Shared execution context.
            phase: Lifecycle phase label.

        Returns:
            The step result. :attr:`Outcome.PASS` on exit code 0, otherwise
            :attr:`Outcome.FAIL`.
        """
        assert isinstance(step, ShellStep)
        started = now_iso()
        command = ctx.resolve(step.command)
        ctx.logger.info("Running shell: %s", command)

        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
        output = completed.stdout
        if completed.stderr:
            output = f"{output}\n[stderr]\n{completed.stderr}" if output else completed.stderr

        ctx.prompter.show_tool_output(
            command, completed.stdout, completed.stderr, label="shell"
        )

        if step.save_output:
            ctx.set_var(step.save_output, completed.stdout.strip())

        outcome = Outcome.PASS if completed.returncode == 0 else Outcome.FAIL
        return StepResult(
            name=step.name,
            kind="shell",
            phase=phase,
            outcome=outcome,
            detail=command,
            output=output.strip(),
            notes=f"exit code {completed.returncode}",
            started_at=started,
            finished_at=now_iso(),
        )
