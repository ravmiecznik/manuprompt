"""Operator I/O abstraction.

The engine never talks to the terminal directly; it goes through a
:class:`Prompter`. This keeps the engine testable (inject a scripted
prompter) and leaves room for alternative front-ends (web UI, GUI) that
implement the same protocol without touching the engine.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from enum import Enum
from typing import Protocol, TextIO

from .results import Outcome, StepResult, TestCaseResult

# ANSI SGR escape codes used to colourise console prompts. Kept module-private
# so the colour scheme lives in one place.
_RESET = "\033[0m"
_BLUE = "\033[1;34m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_DIM = "\033[2m"
# Dedicated colours for captured tool output streams.
_TOOL_STDOUT = "\033[35m"  # magenta
_TOOL_STDERR = "\033[91m"  # bright red

# Max step-name width shown in the end-of-case review (full text kept in data).
_STEP_NAME_DISPLAY_MAX = 80

# Colour used for each outcome's status tag in the end-of-case review.
_OUTCOME_COLORS: dict[Outcome, str] = {
    Outcome.PASS: _GREEN,
    Outcome.FAIL: _RED,
    Outcome.ERROR: _RED,
    Outcome.ACK: _YELLOW,
    Outcome.SKIP: _DIM,
}


class CaseDecision(str, Enum):
    """Operator's decision after reviewing a finished test case.

    Attributes:
        PROCEED: Accept the result and continue to the next test case.
        REPEAT: Discard this attempt and run the test case again.
        STOP: Accept the result and end the session early; remaining cases are
            not run, but suite teardown still runs (so the report is saved).
    """

    PROCEED = "proceed"
    REPEAT = "repeat"
    STOP = "stop"


class Prompter(Protocol):
    """Protocol for collecting operator decisions and input.

    Implementations drive whatever front-end the operator uses (console,
    web, GUI). The engine depends only on this structural interface.
    """

    def ask_verdict(self, text: str) -> tuple[Outcome, str]:
        """Show an instruction and collect a verdict plus optional notes.

        Args:
            text: Instruction to present to the operator.

        Returns:
            A ``(outcome, notes)`` pair. ``outcome`` is one of
            :attr:`Outcome.PASS`, :attr:`Outcome.FAIL` or
            :attr:`Outcome.ACK` (acknowledged without a verdict).
        """
        ...

    def ask_input(self, text: str) -> str:
        """Show an instruction and collect a free-text value.

        Args:
            text: Instruction to present to the operator.

        Returns:
            The operator's typed input, stripped of surrounding whitespace.
        """
        ...

    def ask_confirm(self, text: str, default: bool = True) -> bool:
        """Ask the operator a yes/no question.

        Args:
            text: Question to present to the operator.
            default: Value returned when the operator just presses Enter (and
                at EOF in non-interactive runs).

        Returns:
            ``True`` to proceed, ``False`` to skip.
        """
        ...

    def ask_artifact(self, label: str, error: str = "") -> str:
        """Ask the operator for a path to a file to attach to the step.

        Args:
            label: Human-readable name of the artifact (e.g. the screenshot).
            error: Optional message shown before the prompt, used to report a
                previous invalid path when re-prompting.

        Returns:
            A filesystem path, or an empty string to skip attaching a file.
        """
        ...

    def select_cases(self, cases: Sequence[tuple[str, str]]) -> set[str] | None:
        """Ask the operator which of the planned cases to run.

        Called once, before suite setup and any test case starts, with every
        case the suite would otherwise run (already filtered/ordered by any
        ``-t``/``-k`` CLI selection).

        Args:
            cases: ``(id, name)`` pairs for every planned case, in the order
                they will run.

        Returns:
            The set of case ids to run, or ``None`` to run all of them.
            Non-interactive front-ends should default to ``None``.
        """
        ...

    def start_case(self, case_id: str, name: str, steps: Sequence[str]) -> bool:
        """Announce the next test case and ask whether to run or skip it.

        Args:
            case_id: The test-case id (e.g. ``DEMO001``).
            name: The test-case name.
            steps: Labels of the case's steps, shown so the operator can preview
                what will run before confirming.

        Returns:
            ``True`` to run the case, ``False`` to skip it. Non-interactive
            front-ends should default to ``True`` so a run proceeds.
        """
        ...

    def review_case(self, case: TestCaseResult) -> CaseDecision:
        """Present a finished case's step results and ask how to proceed.

        Args:
            case: The completed test-case result to review.

        Returns:
            :attr:`CaseDecision.PROCEED` to accept and continue, or
            :attr:`CaseDecision.REPEAT` to run the case again.
        """
        ...

    def show_tool_output(
        self, command: str, stdout: str, stderr: str, label: str = ""
    ) -> None:
        """Display the captured output of a tool step to the operator.

        Args:
            command: The full command line that was executed.
            stdout: Captured standard output.
            stderr: Captured standard error.
            label: Name of the producer (a ``tool`` step's tool key, or
                ``"shell"``), used to group output under a named surface (e.g.
                a per-tool tab in the web UI). Empty for an unlabelled stream.
        """
        ...


class ConsolePrompter:
    """Terminal-based :class:`Prompter` implementation.

    Prompts are written to ``output`` and responses are read from ``input``.
    A verdict prompt accepts ``p``/``pass`` for PASS, ``f``/``fail`` for FAIL
    and an empty line to acknowledge and continue. Any text after the verdict
    token is recorded as operator notes (e.g. ``f flickered twice``).

    Output is colourised with ANSI escape codes when the output stream is a
    TTY (and the ``NO_COLOR`` environment variable is unset). Colours can be
    forced on or off via the ``use_color`` argument.

    Attributes:
        output: Stream prompts are written to.
        input: Stream responses are read from.
        use_color: Whether ANSI colour codes are emitted.
    """

    def __init__(
        self,
        output: TextIO = sys.stdout,
        input_stream: TextIO = sys.stdin,
        use_color: bool | None = None,
    ) -> None:
        """Initialise the console prompter.

        Args:
            output: Stream to write prompts to.
            input_stream: Stream to read operator responses from.
            use_color: Force colour on (``True``) or off (``False``). When
                ``None`` (default), colour is enabled only if ``output`` is a
                TTY and ``NO_COLOR`` is not set in the environment.
        """
        self.output = output
        self.input = input_stream
        self.use_color = (
            self._detect_color(output) if use_color is None else use_color
        )

    @staticmethod
    def _detect_color(output: TextIO) -> bool:
        """Return whether colour should be used for ``output``.

        Args:
            output: The stream prompts are written to.

        Returns:
            ``True`` if the stream is an interactive TTY and ``NO_COLOR`` is
            not set, ``False`` otherwise.
        """
        if os.environ.get("NO_COLOR") is not None:
            return False
        is_tty = getattr(output, "isatty", None)
        return bool(is_tty and is_tty())

    def _paint(self, text: str, color: str) -> str:
        """Wrap ``text`` in an ANSI colour code when colour is enabled.

        Args:
            text: Text to colourise.
            color: ANSI SGR escape sequence to apply.

        Returns:
            The colourised text, or ``text`` unchanged when colour is off.
        """
        if not self.use_color:
            return text
        return f"{color}{text}{_RESET}"

    def _write(self, text: str) -> None:
        """Write ``text`` followed by a newline to the output stream."""
        self.output.write(text + "\n")
        self.output.flush()

    def _readline(self) -> str | None:
        """Read one line from the input stream.

        Returns:
            The line without its trailing newline, or ``None`` at EOF (e.g.
            a non-interactive run with no input available).
        """
        line = self.input.readline()
        if line == "":
            return None
        return line.rstrip("\n")

    def ask_verdict(self, text: str) -> tuple[Outcome, str]:
        """Prompt for a PASS/FAIL verdict or acknowledgement.

        Args:
            text: Instruction to present to the operator.

        Returns:
            A ``(outcome, notes)`` pair.
        """
        self._write("")
        self._write(self._paint(text, _BLUE))
        options = (
            f"  {self._paint('[p]ass', _GREEN)}"
            f"  {self._paint('[f]ail', _RED)}"
            f"  {self._paint('[Enter] acknowledge', _YELLOW)}"
            f"   {self._paint('(append a note)', _DIM)}"
        )
        self._write(options)
        while True:
            raw = self._readline()
            if raw is None:
                # No interactive input available; acknowledge and continue.
                return Outcome.ACK, ""
            token, _, note = raw.strip().partition(" ")
            token = token.lower()
            note = note.strip()
            if token in ("p", "pass"):
                return Outcome.PASS, note
            if token in ("f", "fail"):
                return Outcome.FAIL, note
            if token == "":
                return Outcome.ACK, note
            self._write(
                self._paint("  Please enter 'p', 'f', or press Enter.", _RED)
            )

    def ask_input(self, text: str) -> str:
        """Prompt for a free-text value.

        Args:
            text: Instruction to present to the operator.

        Returns:
            The operator's typed input, stripped. Empty string at EOF.
        """
        self._write("")
        self._write(self._paint(text, _BLUE))
        self._write(self._paint("  > ", _CYAN))
        raw = self._readline()
        return "" if raw is None else raw.strip()

    def ask_confirm(self, text: str, default: bool = True) -> bool:
        """Ask a yes/no question; Enter takes ``default``.

        Args:
            text: Question to present to the operator.
            default: Value returned for an empty line (Enter) and at EOF
                (non-interactive runs).

        Returns:
            ``True`` to proceed (``y``), ``False`` to decline (``n``); Enter
            and EOF return ``default``.
        """
        self._write("")
        self._write(self._paint(text, _BLUE))
        if default:
            options = (
                f"  {self._paint('[Enter] yes', _GREEN)}"
                f"  {self._paint('[n] no', _YELLOW)}"
            )
        else:
            options = (
                f"  {self._paint('[y] yes', _GREEN)}"
                f"  {self._paint('[Enter] no', _YELLOW)}"
            )
        self._write(options)
        while True:
            raw = self._readline()
            if raw is None:
                return default
            token = raw.strip().lower()
            if token == "":
                return default
            if token in ("y", "yes"):
                return True
            if token in ("n", "no", "s", "skip"):
                return False
            self._write(
                self._paint("  Please enter 'y' or 'n'.", _RED)
            )

    def ask_artifact(self, label: str, error: str = "") -> str:
        """Prompt for a path to a file to attach under ``label``.

        Args:
            label: Human-readable name of the artifact.
            error: Optional message shown in red before the prompt when
                re-prompting after an invalid path.

        Returns:
            The typed path stripped of whitespace, or an empty string at EOF
            or when the operator skips by pressing Enter.
        """
        self._write("")
        if error:
            self._write(self._paint(f"  {error}", _RED))
        self._write(
            self._paint(f"Attach a file for '{label}' (path, or Enter to skip):", _BLUE)
        )
        self._write(self._paint("  > ", _CYAN))
        raw = self._readline()
        return "" if raw is None else raw.strip()

    def select_cases(self, cases: Sequence[tuple[str, str]]) -> set[str] | None:
        """Run every case; interactive case selection is a web-only feature.

        The console flow already prints the full plan before the run starts
        (see ``api.run_suite``'s ``_announce_plan``) and lets the operator
        skip cases individually via :meth:`start_case`, so this always runs
        everything rather than adding a second, redundant console prompt.

        Args:
            cases: Unused.

        Returns:
            ``None`` (run all).
        """
        return None

    def start_case(self, case_id: str, name: str, steps: Sequence[str]) -> bool:
        """Announce the next case in a prominent banner; ask to run or skip.

        Lists the case's steps beneath the banner so the operator can preview
        what the test will do before confirming.

        Args:
            case_id: The test-case id.
            name: The test-case name.
            steps: Labels of the case's steps.

        Returns:
            ``True`` to run, ``False`` to skip. At EOF (non-interactive),
            defaults to ``True`` so the run proceeds.
        """
        title = f"  Next test: {case_id}  {name}  "
        rule = "━" * len(title)
        self._write("")
        self._write(self._paint(f"┏{rule}┓", _BLUE))
        self._write(self._paint(f"┃{title}┃", _BLUE))
        self._write(self._paint(f"┗{rule}┛", _BLUE))
        if steps:
            self._write(self._paint("  Steps:", _BLUE))
            field = len(str(len(steps)))
            for index, label in enumerate(steps, start=1):
                self._write(
                    self._paint(f"    {index:>{field}}. {self._clip(label)}", _DIM)
                )
        self._write(
            f"  {self._paint('[Enter] run', _GREEN)}"
            f"  {self._paint('[s] skip this test', _YELLOW)}"
        )
        while True:
            raw = self._readline()
            if raw is None:
                return True
            token = raw.strip().lower()
            if token in ("", "r", "run"):
                return True
            if token in ("s", "skip"):
                return False
            self._write(
                self._paint("  Press Enter to run or 's' to skip.", _RED)
            )

    def review_case(self, case: TestCaseResult) -> CaseDecision:
        """Print every step's status, then ask how to proceed.

        Offers three choices: continue to the next case, repeat this case, or
        stop the session early (which still saves the report via suite
        teardown).

        Args:
            case: The completed test-case result to review.

        Returns:
            The operator's :class:`CaseDecision`. At EOF (non-interactive),
            defaults to :attr:`CaseDecision.PROCEED` so a run cannot loop
            forever.
        """
        self._write("")
        header = f"\u2500\u2500 {case.id}  {case.name} \u2500\u2500"
        self._write(self._paint(header, _BLUE))
        for step in case.steps:
            self._write(self._format_step_line(step))
        overall = self._paint(
            case.outcome.value.upper(), _OUTCOME_COLORS.get(case.outcome, "")
        )
        self._write(f"  result: {overall}")
        self._write(
            f"  {self._paint('[Enter] proceed', _GREEN)}"
            f"  {self._paint('[r] repeat test case', _YELLOW)}"
            f"  {self._paint('[s] stop & save report', _CYAN)}"
        )
        while True:
            raw = self._readline()
            if raw is None:
                return CaseDecision.PROCEED
            token = raw.strip().lower()
            if token in ("r", "repeat"):
                return CaseDecision.REPEAT
            if token in ("s", "stop"):
                return CaseDecision.STOP
            if token in ("", "p", "proceed"):
                return CaseDecision.PROCEED
            self._write(
                self._paint(
                    "  Press Enter to proceed, 'r' to repeat, or 's' to stop.", _RED
                )
            )

    def show_tool_output(
        self, command: str, stdout: str, stderr: str, label: str = ""
    ) -> None:
        """Print a tool step's captured stdout/stderr with dedicated colours.

        The command line is shown dimmed; ``stdout`` is rendered in magenta and
        ``stderr`` in bright red. Streams that are empty are omitted, and a
        ``(no output)`` note is shown when the tool produced nothing.

        Args:
            command: The full command line that was executed.
            stdout: Captured standard output.
            stderr: Captured standard error.
            label: Producer name (unused by the console front-end; the command
                line already identifies what ran).
        """
        self._write("")
        self._write(self._paint(f"  $ {command}", _DIM))
        out = stdout.rstrip("\n")
        err = stderr.rstrip("\n")
        if out:
            self._write(self._paint("  stdout:", _TOOL_STDOUT))
            self._write(self._format_stream(out, _TOOL_STDOUT))
        if err:
            self._write(self._paint("  stderr:", _TOOL_STDERR))
            self._write(self._format_stream(err, _TOOL_STDERR))
        if not out and not err:
            self._write(self._paint("  (no output)", _DIM))

    def _format_stream(self, text: str, color: str) -> str:
        """Return ``text`` indented and colourised line by line.

        Args:
            text: Captured stream text (without a trailing newline).
            color: ANSI colour applied to each line.

        Returns:
            The indented, colourised block as a single string.
        """
        return "\n".join(
            self._paint(f"    {line}", color) for line in text.splitlines()
        )

    def _format_step_line(self, step: StepResult) -> str:
        """Format a single reviewed step as a coloured status line.

        Long step names are truncated for terminal alignment; the full text is
        preserved in the result and the reports.

        Args:
            step: The step result to format.

        Returns:
            A status line such as ``  [PASS ] Validate the colour (note)``.
        """
        color = _OUTCOME_COLORS.get(step.outcome, "")
        tag = self._paint(f"[{step.outcome.value.upper():^5}]", color)
        line = f"  {tag} {self._clip(step.name)}"
        detail = step.notes or step.error
        if detail:
            line += self._paint(f"  ({detail})", _DIM)
        return line

    @staticmethod
    def _clip(text: str) -> str:
        """Truncate a step label for terminal display (full text kept elsewhere)."""
        text = " ".join(text.split())
        if len(text) > _STEP_NAME_DISPLAY_MAX:
            return text[: _STEP_NAME_DISPLAY_MAX - 1].rstrip() + "\u2026"
        return text
