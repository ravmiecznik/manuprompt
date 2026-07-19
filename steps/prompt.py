"""Handler for :class:`~model.PromptStep`."""

from __future__ import annotations

from ..context import RunContext
from ..model import PromptStep, Step
from ..results import Outcome, StepResult
from .base import now_iso, register_handler

# Human-readable labels for the non-"step" lifecycle phases. Prompts shown
# during these phases are prefixed so the operator knows a setup/teardown
# instruction is not a regular test step.
_PHASE_LABELS: dict[str, str] = {
    "suite_setup": "SUITE SETUP",
    "test_setup": "TEST SETUP",
    "test_teardown": "TEST TEARDOWN",
    "suite_teardown": "SUITE TEARDOWN",
}


def _label(text: str, phase: str) -> str:
    """Prefix ``text`` with a phase tag for non-step phases.

    Args:
        text: The resolved instruction text.
        phase: Lifecycle phase the prompt runs in.

    Returns:
        The instruction with a ``[PHASE] `` prefix during setup/teardown
        phases, or unchanged during the regular ``step`` phase.
    """
    label = _PHASE_LABELS.get(phase)
    return f"[{label}] {text}" if label else text


@register_handler(PromptStep)
class PromptHandler:
    """Present an instruction to the operator and record the response.

    A plain prompt collects a PASS/FAIL/acknowledge verdict. A prompt with a
    ``store`` target collects free-text input into a variable instead.
    """

    def execute(self, step: Step, ctx: RunContext, phase: str) -> StepResult:
        """Run the prompt step.

        Args:
            step: The prompt step (a :class:`~model.PromptStep`).
            ctx: Shared execution context.
            phase: Lifecycle phase label.

        Returns:
            The step result. Input-capture prompts return
            :attr:`Outcome.ACK`; verdict prompts return the operator's choice.
        """
        assert isinstance(step, PromptStep)
        started = now_iso()
        text = ctx.resolve(step.prompt)
        shown = _label(text, phase)
        if step.note:
            # Surface the authored note to the operator alongside the prompt.
            shown = f"{shown}\n  Note: {ctx.resolve(step.note)}"

        if step.store:
            value = ctx.prompter.ask_input(shown)
            ctx.set_var(step.store, value)
            ctx.logger.info("Stored %s=%r", step.store, value)
            return StepResult(
                name=step.name,
                kind="prompt",
                phase=phase,
                outcome=Outcome.ACK,
                detail=text,
                notes=f"stored {step.store}={value!r}",
                started_at=started,
                finished_at=now_iso(),
            )

        outcome, notes = ctx.prompter.ask_verdict(shown)
        ctx.logger.info("Prompt verdict: %s", outcome.value)
        return StepResult(
            name=step.name,
            kind="prompt",
            phase=phase,
            outcome=outcome,
            detail=text,
            notes=notes,
            started_at=started,
            finished_at=now_iso(),
        )
