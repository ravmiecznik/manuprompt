"""Browser-based :class:`~prompter.Prompter` implementation.

:class:`WebPrompter` drives operator prompts through a :class:`~webui.gio.WebGIO`
surface instead of the terminal: each prompt is registered with the live server
and rendered on the session page, and the engine call blocks until the operator
answers there (or the surface is stopped). It is the web-mode counterpart to
:class:`~prompter.ConsolePrompter` and implements the same
:class:`~prompter.Prompter` protocol, so the engine is unchanged.

The class is domain-agnostic — it only maps the protocol's prompt shapes to and
from JSON payloads the page understands.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..prompter import CaseDecision
from ..results import Outcome

if TYPE_CHECKING:
    from ..results import StepResult, TestCaseResult
    from .gio import WebGIO

# Channel captured tool output is streamed to on the session page.
_TOOL_CHANNEL = "Tool output"


class WebPrompter:
    """Collect operator decisions and input through a browser session page.

    Each method registers a pending prompt with the live server via
    :meth:`WebGIO.ask` and blocks until the operator answers it in the browser.
    When the surface is stopped while a prompt is waiting, ``ask`` returns
    ``None`` and the prompter falls back to the same non-interactive default the
    console prompter uses at EOF, so a run cannot hang or loop forever.

    Attributes:
        gio: The live browser I/O surface prompts are shown on.
    """

    def __init__(self, gio: WebGIO) -> None:
        """Initialise the web prompter.

        Args:
            gio: The live :class:`~webui.gio.WebGIO` surface to prompt through.
        """
        self._gio = gio

    def ask_verdict(self, text: str) -> tuple[Outcome, str]:
        """Show an instruction and collect a verdict plus optional notes.

        Args:
            text: Instruction to present to the operator.

        Returns:
            A ``(outcome, notes)`` pair. Defaults to ``(ACK, "")`` if the
            surface is stopped while waiting.
        """
        answer = self._gio.ask("verdict", {"text": text})
        if answer is None:
            return Outcome.ACK, ""
        verdict = str(answer.get("verdict", "")).lower()
        note = str(answer.get("note", "")).strip()
        if verdict == "pass":
            return Outcome.PASS, note
        if verdict == "fail":
            return Outcome.FAIL, note
        return Outcome.ACK, note

    def ask_input(self, text: str) -> str:
        """Show an instruction and collect a free-text value.

        Args:
            text: Instruction to present to the operator.

        Returns:
            The operator's input, stripped. Empty string if the surface stops.
        """
        answer = self._gio.ask("input", {"text": text})
        if answer is None:
            return ""
        return str(answer.get("value", "")).strip()

    def ask_confirm(self, text: str, default: bool = True) -> bool:
        """Ask the operator a yes/no question.

        Args:
            text: Question to present to the operator.
            default: Value returned if the surface stops while waiting.

        Returns:
            ``True`` to proceed, ``False`` to decline.
        """
        answer = self._gio.ask("confirm", {"text": text, "default": default})
        if answer is None:
            return default
        return bool(answer.get("confirm", default))

    def ask_artifact(self, label: str, error: str = "") -> str:
        """Return an empty path — web mode collects files via drag-and-drop.

        In browser mode the engine attaches files through
        :meth:`WebGIO.request_files` (the artifact drop zone), not a typed path,
        so this always declines the path-based prompt.

        Args:
            label: Human-readable name of the artifact (unused).
            error: Previous-error message (unused).

        Returns:
            An empty string.
        """
        return ""

    def select_cases(
        self, cases: Sequence[tuple[str, str, str]]
    ) -> set[str] | None:
        """Show every planned case and let the operator choose which to run.

        Rendered as a standalone card on the session page with one checkbox
        per case (all checked by default) and a button to start the run.

        Args:
            cases: ``(id, name, description)`` triples for every planned case,
                in order.

        Returns:
            The selected case ids, or ``None`` (run all) if the surface stops
            while waiting.
        """
        answer = self._gio.ask(
            "select_cases",
            {
                "text": f"{len(cases)} test case(s) planned. Choose which to run:",
                "cases": [
                    {"id": case_id, "name": name, "description": description}
                    for case_id, name, description in cases
                ],
            },
        )
        if answer is None:
            return None
        selected = answer.get("selected")
        if not isinstance(selected, list):
            return None
        return {str(item) for item in selected}

    def start_case(
        self,
        case_id: str,
        name: str,
        steps: Sequence[str],
        description: str = "",
    ) -> bool:
        """Announce the next test case and ask whether to run or skip it.

        Args:
            case_id: The test-case id.
            name: The test-case name.
            steps: Labels of the case's steps, shown as a preview.
            description: Optional free-text description of the case.

        Returns:
            ``True`` to run the case, ``False`` to skip it. Defaults to ``True``
            if the surface stops while waiting.
        """
        answer = self._gio.ask(
            "start_case",
            {
                "case_id": case_id,
                "name": name,
                "description": description,
                "steps": list(steps),
            },
        )
        if answer is None:
            return True
        return bool(answer.get("run", True))

    def review_case(self, case: TestCaseResult) -> CaseDecision:
        """Present a finished case's step results and ask how to proceed.

        Args:
            case: The completed test-case result to review.

        Returns:
            The operator's :class:`~prompter.CaseDecision`. Defaults to
            :attr:`~prompter.CaseDecision.PROCEED` if the surface stops.
        """
        answer = self._gio.ask(
            "review",
            {
                "case_id": case.id,
                "name": case.name,
                "description": case.description,
                "outcome": case.outcome.value,
                "steps": [self._step_view(step) for step in case.steps],
            },
        )
        if answer is None:
            return CaseDecision.PROCEED
        decision = str(answer.get("decision", "")).lower()
        if decision == CaseDecision.REPEAT.value:
            return CaseDecision.REPEAT
        if decision == CaseDecision.STOP.value:
            return CaseDecision.STOP
        return CaseDecision.PROCEED

    def show_tool_output(
        self, command: str, stdout: str, stderr: str, label: str = ""
    ) -> None:
        """Stream a tool step's captured output to its own console tab.

        Output is published on a channel named after the producer — a ``tool``
        step's tool key (so each tool gets its own tab named exactly as in the
        suite YAML), or ``"shell"`` for shell steps — falling back to a generic
        tab when no label is given.

        Args:
            command: The full command line that was executed.
            stdout: Captured standard output.
            stderr: Captured standard error.
            label: Producer name used as the channel/tab name.
        """
        channel = self._gio.channel(label or _TOOL_CHANNEL)
        channel.write(f"$ {command}")
        out = stdout.rstrip("\n")
        err = stderr.rstrip("\n")
        if out:
            channel.write(out)
        if err:
            channel.write(err)
        if not out and not err:
            channel.write("(no output)")

    @staticmethod
    def _step_view(step: StepResult) -> dict[str, str]:
        """Return a JSON-serialisable summary of a reviewed step.

        Args:
            step: The step result to summarise.

        Returns:
            A dict with the step's ``name``, ``outcome`` and any ``detail``
            (operator notes or error text) for the review panel.
        """
        return {
            "name": step.name,
            "outcome": step.outcome.value,
            "detail": step.notes or step.error,
        }
