"""Suite execution engine.

The engine knows only the *shape* of a suite — one-time setup, per-case
setup/steps/teardown, one-time teardown — and how to record outcomes. It
delegates *what* a step does to a registered handler. Each step runs inside an
error boundary so a misbehaving handler becomes an :attr:`Outcome.ERROR`
result rather than aborting the whole run, and teardown always runs.

After every step the current :class:`~results.SuiteResult` is handed to an
optional ``on_update`` callback, enabling incremental persistence so an
interrupted run still leaves a usable partial report.
"""

from __future__ import annotations

import logging
import re
import shlex
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ._banner import box_url
from .context import RunContext
from .model import OptionalStep, Step, Suite, TestCase
from .prompter import CaseDecision, Prompter
from .results import Outcome, StepResult, SuiteResult, TestCaseResult
from .steps import get_handler, now_iso
from .theme import effective_theme
from .webui import WebGIO

# Outcomes that make a setup phase "blocking": when a setup step yields one of
# these, the dependent steps are skipped.
_BLOCKING: frozenset[Outcome] = frozenset({Outcome.FAIL, Outcome.ERROR})

OnUpdate = Callable[[SuiteResult], None]


class Engine:
    """Executes a :class:`~model.Suite` and produces a :class:`~results.SuiteResult`.

    Attributes:
        prompter: Operator I/O implementation passed to every context.
        artifacts_dir: Directory handed to handlers for run artifacts.
        logger: Logger used by the engine and shared with contexts.
    """

    def __init__(
        self,
        prompter: Prompter,
        artifacts_dir: Path,
        logger: logging.Logger,
        on_update: OnUpdate | None = None,
        web_gio: WebGIO | None = None,
        stop_web_gio: bool = True,
    ) -> None:
        """Initialise the engine.

        Args:
            prompter: Operator I/O implementation.
            artifacts_dir: Directory for run artifacts.
            logger: Logger for the engine and contexts.
            on_update: Optional callback invoked with the in-progress result
                after every step, used for incremental persistence.
            web_gio: Optional live browser I/O surface shared with every
                context and stopped when the run ends.
            stop_web_gio: When ``True`` (default) the engine stops ``web_gio``
                at the end of the run. Callers that keep serving on the surface
                afterwards (e.g. to show a report) pass ``False`` and own its
                shutdown.
        """
        self.prompter = prompter
        self.artifacts_dir = artifacts_dir
        self.logger = logger
        self._on_update = on_update
        self._web_gio = web_gio
        self._stop_web_gio = stop_web_gio
        self._result: SuiteResult | None = None
        self._resources: dict[str, Any] = {}
        self._teardowns: list[Callable[[], None]] = []
        self._suite_dir: Path = Path.cwd()
        # Ids the operator deselected via select_cases(), set once per run.
        self._deselected: frozenset[str] = frozenset()
        # Live progress for the browser session page (see _publish_session).
        self._active_case: TestCase | None = None
        self._active_case_result: TestCaseResult | None = None
        self._active_planned: list[dict[str, Any]] = []
        self._active_phase: str = ""

    def run(self, suite: Suite) -> SuiteResult:
        """Execute the whole suite.

        Args:
            suite: The suite to run.

        Returns:
            The fully populated suite result.
        """
        result = SuiteResult(
            name=suite.name,
            description=suite.description,
            test_environment=list(suite.test_environment),
            theme=effective_theme(suite.theme).overrides(),
            started_at=now_iso(),
        )
        self._result = result
        self._resources = {}
        self._teardowns = []
        self._suite_dir = suite.source_dir or Path.cwd()
        self._persist()

        try:
            self._deselected = self._select_deselected_cases(suite.test_cases)
            suite_vars = dict(suite.variables)
            setup_ctx = self._make_ctx(suite_vars, suite.tools)
            self._run_phase(
                suite.suite_setup, setup_ctx, "suite_setup", result.suite_setup
            )
            # Variables set during suite setup are visible to every test case.
            suite_vars = setup_ctx.variables
            suite_aborted = _has_blocking_failure(result.suite_setup)
            if suite_aborted:
                self.logger.error("Suite setup failed — skipping all test cases.")
                self._notify_error(
                    "Suite setup failed — all test cases are being skipped. "
                    "Check the CLI/log output for the full traceback."
                )

            for case in suite.test_cases:
                if self._run_case(case, suite, suite_vars, suite_aborted):
                    self.logger.info("Session stopped by operator after %s.", case.id)
                    break

            teardown_ctx = self._make_ctx(suite_vars, suite.tools)
            self._run_phase(
                suite.suite_teardown,
                teardown_ctx,
                "suite_teardown",
                result.suite_teardown,
            )
        finally:
            # Always release resources (e.g. stop the serial mirror), even on
            # error or operator interruption, then stop the I/O surface last so
            # any final teardown output still reaches the browser.
            self._run_teardowns()
            if self._web_gio is not None and self._stop_web_gio:
                self._web_gio.stop()
            result.finished_at = now_iso()
            self._persist()
        return result

    def _select_deselected_cases(self, cases: Sequence[TestCase]) -> frozenset[str]:
        """Ask the operator which planned cases to run.

        Called once per run, before suite setup. Front-ends that don't offer
        interactive selection (e.g. :class:`~prompter.ConsolePrompter`) return
        ``None`` from :meth:`Prompter.select_cases`, meaning "run all".

        Args:
            cases: Every case the suite would otherwise run, in order.

        Returns:
            The ids of cases the operator declined to run (empty if all run,
            or there is nothing to choose from).
        """
        if not cases:
            return frozenset()
        selected = self.prompter.select_cases([(c.id, c.name) for c in cases])
        if selected is None:
            return frozenset()
        return frozenset(c.id for c in cases if c.id not in selected)

    def _run_case(
        self,
        case: TestCase,
        suite: Suite,
        suite_vars: dict[str, Any],
        suite_aborted: bool,
    ) -> bool:
        """Execute a single test case and append its result to the suite.

        Args:
            case: The test case to run.
            suite: The owning suite (for setup/teardown step lists and tools).
            suite_vars: Suite-scoped variables to overlay case variables on.
            suite_aborted: When ``True``, the case's steps are skipped because
                suite setup failed (test teardown still runs).

        Returns:
            ``True`` if the operator chose to stop the session after this case
            (the caller should run no further cases), ``False`` to continue.
        """
        assert self._result is not None
        self._clear_session()  # no live step list while announcing/skipping a case

        if case.id in self._deselected:
            self._skip_case(case, "deselected by operator before the run started")
            return False

        if suite_aborted:
            case_result = TestCaseResult(id=case.id, name=case.name, started_at=now_iso())
            self._result.cases.append(case_result)
            self._persist()
            self._skip_steps(case.steps, "step", case_result.steps, "suite setup failed")
            case_result.finished_at = now_iso()
            self._persist()
            return False

        if case.skip_reason:
            # A case marked to skip announces its reason and asks to run anyway.
            if not self._run_skipped_anyway(case):
                self._skip_case(case, case.skip_reason)
                return False
        elif not self.prompter.start_case(
            case.id, case.name, [step.name for step in case.steps]
        ):
            # Otherwise announce the next case and let the operator skip it.
            self._skip_case(case, "skipped by operator")
            return False

        # Run the case, then let the operator review and optionally repeat it.
        # A repeated attempt discards the previous one so only the accepted
        # attempt remains in the result.
        while True:
            case_result = TestCaseResult(id=case.id, name=case.name, started_at=now_iso())
            self._result.cases.append(case_result)
            # Begin publishing this attempt's live step list to the session page.
            self._begin_session(case, case_result)
            self._persist()

            case_vars = {**suite_vars, **case.variables}
            ctx = self._make_ctx(case_vars, suite.tools, case_result=case_result)

            # Suite-level setup is common to every case; case-level setup adds
            # case-specific steps after it. Teardown mirrors this in reverse:
            # case-level teardown runs before the suite-level teardown.
            test_setup = (*suite.test_setup, *case.test_setup)
            test_teardown = (*case.test_teardown, *suite.test_teardown)

            self._active_phase = "test_setup"
            setup_results = self._run_phase(
                test_setup, ctx, "test_setup", case_result.steps
            )
            if _has_blocking_failure(setup_results):
                self.logger.error("Test setup failed for %s — skipping steps.", case.id)
                self._skip_steps(
                    case.steps, "step", case_result.steps, "test setup failed"
                )
            else:
                self._active_phase = "step"
                # Highlight the first step before its handler blocks on a prompt.
                self._publish_session()
                self._run_phase(case.steps, ctx, "step", case_result.steps)

            # Teardown runs regardless of step outcomes.
            self._active_phase = "test_teardown"
            self._run_phase(test_teardown, ctx, "test_teardown", case_result.steps)
            case_result.finished_at = now_iso()
            self._active_phase = "review"
            self._persist()

            decision = self.prompter.review_case(case_result)
            if decision != CaseDecision.REPEAT:
                self._clear_session()
                return decision == CaseDecision.STOP
            self.logger.info("Repeating test case %s", case.id)
            self._result.cases.pop()
            self._clear_session()
            self._persist()

    def _run_skipped_anyway(self, case: TestCase) -> bool:
        """Tell the operator why a case is skipped and ask to run it anyway.

        Args:
            case: The test case marked to be skipped.

        Returns:
            ``True`` if the operator chose to run it despite the skip marker,
            ``False`` to honour the skip (the default).
        """
        question = (
            f"Test case {case.id} ({case.name}) is marked to be skipped.\n"
            f"  Reason: {case.skip_reason}\n"
            "Run it anyway?"
        )
        return self.prompter.ask_confirm(question, default=False)

    def _skip_case(self, case: TestCase, reason: str) -> None:
        """Record a skipped case: mark every step skipped, keep the reason.

        The case still appears in the result with all of its steps listed as
        :attr:`Outcome.SKIP`, so the report shows what would have run.

        Args:
            case: The skipped test case.
            reason: Why the case was skipped (shown in the report).
        """
        assert self._result is not None
        self.logger.info("Skipping test case %s: %s", case.id, reason)
        case_result = TestCaseResult(
            id=case.id, name=case.name, skip_reason=reason, started_at=now_iso()
        )
        self._result.cases.append(case_result)
        self._persist()
        self._skip_steps(case.steps, "step", case_result.steps, "test case skipped")
        case_result.finished_at = now_iso()
        self._persist()

    def _make_ctx(
        self,
        variables: dict[str, Any],
        tools: dict[str, str],
        case_result: TestCaseResult | None = None,
    ) -> RunContext:
        """Create a fresh :class:`~context.RunContext` for a scope.

        Per-scope ``variables`` are copied by the context, while the
        ``resources`` store and teardown registry are shared by reference so
        objects and cleanups created in one scope persist across the run.
        ``case_result`` is the case currently executing (``None`` for suite
        setup/teardown), letting glue attach per-test logs via
        :meth:`~context.RunContext.attach_log`.
        """
        return RunContext(
            variables=variables,
            tools=tools,
            artifacts_dir=self.artifacts_dir,
            prompter=self.prompter,
            logger=self.logger,
            resources=self._resources,
            teardowns=self._teardowns,
            suite_dir=self._suite_dir,
            result=self._result,
            web_gio=self._web_gio,
            case_result=case_result,
        )

    def _run_phase(
        self,
        steps: Sequence[Step],
        ctx: RunContext,
        phase: str,
        sink: list[StepResult],
    ) -> list[StepResult]:
        """Run every step of a phase, appending results to ``sink``.

        Args:
            steps: Steps to run.
            ctx: Context for this scope.
            phase: Phase label recorded on each result.
            sink: List the produced results are appended to.

        Returns:
            The list of results produced by this phase (also appended to
            ``sink``).
        """
        produced: list[StepResult] = []
        for step in steps:
            self._dispatch_step(step, ctx, phase, sink, produced)
        return produced

    def _dispatch_step(
        self,
        step: Step,
        ctx: RunContext,
        phase: str,
        sink: list[StepResult],
        produced: list[StepResult],
    ) -> None:
        """Run one step, expanding control-flow containers as needed.

        Leaf steps run via their handler; an :class:`~model.OptionalStep` is
        offered to the operator and either expanded or skipped. Results are
        appended to both ``sink`` (the phase's result list) and ``produced``
        (returned for blocking-failure detection).

        Args:
            step: The step to run (leaf or container).
            ctx: Context for this scope.
            phase: Phase label recorded on each result.
            sink: List the produced results are appended to.
            produced: Running list of this phase's results.
        """
        if isinstance(step, OptionalStep):
            self._run_optional(step, ctx, phase, sink, produced)
            return
        result = self._run_step(step, ctx, phase)
        produced.append(result)
        sink.append(result)
        self._persist()

    def _run_optional(
        self,
        step: OptionalStep,
        ctx: RunContext,
        phase: str,
        sink: list[StepResult],
        produced: list[StepResult],
    ) -> None:
        """Offer an optional group to the operator and run or skip it.

        Records a marker result for the decision, then either dispatches the
        nested steps (operator opted in) or records them as skipped.

        Args:
            step: The optional group.
            ctx: Context for this scope.
            phase: Phase label recorded on each result.
            sink: List the produced results are appended to.
            produced: Running list of this phase's results.
        """
        question = step.prompt or self._optional_question(step)
        opted_in = self.prompter.ask_confirm(question)
        timestamp = now_iso()
        marker = StepResult(
            name=step.name,
            kind="optional",
            phase=phase,
            outcome=Outcome.ACK if opted_in else Outcome.SKIP,
            detail=question,
            note=step.note,
            notes="opted in" if opted_in else "skipped: declined by operator",
            started_at=timestamp,
            finished_at=now_iso(),
        )
        produced.append(marker)
        sink.append(marker)
        self._persist()

        if opted_in:
            for child in step.steps:
                self._dispatch_step(child, ctx, phase, sink, produced)
            return
        for child in step.steps:
            skip = StepResult(
                name=child.name,
                kind=type(child).__name__,
                phase=phase,
                outcome=Outcome.SKIP,
                note=child.note,
                notes="skipped: optional group declined",
                started_at=timestamp,
                finished_at=timestamp,
            )
            produced.append(skip)
            sink.append(skip)
        self._persist()

    @staticmethod
    def _optional_question(step: OptionalStep) -> str:
        """Return a default operator question for an optional group."""
        names = ", ".join(child.name for child in step.steps)
        return f"Optional — run the following step(s)? {names}"

    def _run_step(self, step: Step, ctx: RunContext, phase: str) -> StepResult:
        """Run a single step inside an error boundary.

        Opens a per-step attachment window on ``ctx`` so glue can call
        :meth:`~context.RunContext.attach` during the handler; those files
        land on the returned result as step artifacts (alongside any collected
        via the YAML ``artifact:`` modifier).

        Args:
            step: The step to run.
            ctx: Context for this scope.
            phase: Phase label recorded on the result.

        Returns:
            The step result. Any exception raised by the handler is converted
            to an :attr:`Outcome.ERROR` result (still carrying any files the
            step attached before failing).
        """
        started = now_iso()
        ctx._step_artifacts = []
        try:
            try:
                result = get_handler(step).execute(step, ctx, phase)
                result.note = step.note
                # Programmatic attachments first (ctx.attach / call return paths),
                # then ask the operator only for labels not yet satisfied.
                result.artifacts = list(ctx._step_artifacts) + list(result.artifacts)
                if step.artifacts:
                    already = {a["label"] for a in result.artifacts}
                    pending = [label for label in step.artifacts if label not in already]
                    if pending:
                        self._collect_artifacts(pending, step.name, result)
                return result
            except Exception as exc:  # noqa: BLE001 - error boundary by design
                self.logger.exception("Step %r raised", step.name)
                self._notify_error(
                    f"Step '{step.name}' raised {type(exc).__name__}: "
                    f"{_first_line(exc)} — check the CLI/log output for the full "
                    "traceback."
                )
                return StepResult(
                    name=step.name,
                    kind=type(step).__name__,
                    phase=phase,
                    outcome=Outcome.ERROR,
                    note=step.note,
                    error=f"{type(exc).__name__}: {exc}",
                    artifacts=list(ctx._step_artifacts),
                    started_at=started,
                    finished_at=now_iso(),
                )
        finally:
            ctx._step_artifacts = None

    def _collect_artifacts(
        self, labels: list[str], step_name: str, result: StepResult
    ) -> None:
        """Collect operator-supplied files for the given artifact labels.

        For every label the operator supplies file(s): by drag-drop in the
        browser when the live surface is running (where a single label may
        collect **several** files), or by typing a filesystem path on the
        console otherwise. Each file is saved into an ``attachments``
        subdirectory of the run's artifacts directory and recorded on
        ``result`` as a ``{"label", "path"}`` mapping with a path relative to
        that directory.

        Labels already satisfied programmatically (``ctx.attach`` or a ``call``
        return path) should be omitted from ``labels`` by the caller.

        Args:
            labels: Artifact labels still needing operator input.
            step_name: Step name used only for log messages.
            result: The step result that the collected metadata is added to.
        """
        dest_dir = self.artifacts_dir / "attachments"
        for label in labels:
            if self._web_gio is not None:
                saved = self._collect_artifact_web(label, dest_dir)
            else:
                one = self._collect_artifact_path(label, dest_dir)
                saved = [one] if one is not None else []
            if not saved:
                self.logger.info("Artifact %r skipped for %r", label, step_name)
                continue
            for path in saved:
                rel = path.relative_to(self.artifacts_dir)
                result.artifacts.append({"label": label, "path": rel.as_posix()})
                self.logger.info("Attached artifact %r -> %s", label, rel)

    def _collect_artifact_web(self, label: str, dest_dir: Path) -> list[Path]:
        """Collect one or more files for an artifact via browser drag-drop.

        Announces the dedicated upload URL and blocks until the operator
        finishes the request (having dropped any number of files) or skips it.

        Args:
            label: Human-readable name of the artifact.
            dest_dir: Directory the uploaded files are written into.

        Returns:
            The paths the files were written to (empty if skipped).
        """
        assert self._web_gio is not None
        hint = "Drag-drop one or more files, then click Done (or Skip) in the browser"
        if self._web_gio.protected:
            hint += " — password required"
        box_url(f"Artifact: {label}", self._web_gio.artifact_url, hint)
        uploads = self._web_gio.request_files(label)
        if not uploads:
            return []
        dest_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for upload in uploads:
            dest_name = self._unique_artifact_name(dest_dir, label, Path(upload.name))
            dest_path = dest_dir / dest_name
            dest_path.write_bytes(upload.data)
            paths.append(dest_path)
        return paths

    def _collect_artifact_path(self, label: str, dest_dir: Path) -> Path | None:
        """Collect one artifact by prompting for a filesystem path (console).

        Args:
            label: Human-readable name of the artifact.
            dest_dir: Directory the file is copied into.

        Returns:
            The path the file was copied to, or ``None`` if skipped.
        """
        src_path = self._prompt_artifact_path(label)
        if src_path is None:
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_name = self._unique_artifact_name(dest_dir, label, src_path)
        dest_path = dest_dir / dest_name
        shutil.copy2(src_path, dest_path)
        return dest_path

    def _prompt_artifact_path(self, label: str) -> Path | None:
        """Prompt for an artifact path, retrying until valid or skipped.

        The typed value is normalised to tolerate shell-style input (surrounding
        quotes and backslash-escaped spaces, as produced by dragging a file into
        a terminal). The operator is re-prompted with an error message when the
        path does not point to an existing file.

        Args:
            label: Human-readable name of the artifact being collected.

        Returns:
            The resolved file path, or ``None`` if the operator skipped.
        """
        error = ""
        while True:
            raw = self.prompter.ask_artifact(label, error)
            if not raw:
                return None
            candidate = self._normalize_artifact_path(raw)
            if candidate.is_file():
                return candidate
            error = f"No such file: {candidate} — try again or press Enter to skip."

    @staticmethod
    def _normalize_artifact_path(raw: str) -> Path:
        """Normalise an operator-typed path into a filesystem path.

        Handles surrounding single/double quotes and backslash-escaped
        characters (e.g. ``\\ `` for spaces), as commonly produced by shells or
        by dragging a file into a terminal, then expands ``~``.

        Args:
            raw: The raw text typed by the operator.

        Returns:
            The normalised, user-expanded path.
        """
        text = raw.strip()
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = []
        if len(tokens) == 1:
            text = tokens[0]
        return Path(text).expanduser()

    @staticmethod
    def _unique_artifact_name(dest_dir: Path, label: str, src: Path) -> str:
        """Return a collision-free filename for a copied artifact.

        Builds a filesystem-safe name from ``label`` and the source suffix,
        appending a numeric suffix if a file of that name already exists.

        Args:
            dest_dir: Directory the artifact will be copied into.
            label: Operator-facing label for the artifact.
            src: Source file being copied.

        Returns:
            A filename (not a full path) that does not yet exist in
            ``dest_dir``.
        """
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_") or "artifact"
        stem, suffix = slug, src.suffix
        candidate = f"{stem}{suffix}"
        index = 1
        while (dest_dir / candidate).exists():
            candidate = f"{stem}_{index}{suffix}"
            index += 1
        return candidate

    def _skip_steps(
        self,
        steps: Sequence[Step],
        phase: str,
        sink: list[StepResult],
        reason: str,
    ) -> None:
        """Append :attr:`Outcome.SKIP` results for steps that were not run.

        Args:
            steps: Steps that are being skipped.
            phase: Phase label recorded on each result.
            sink: List the skip results are appended to.
            reason: Human-readable reason recorded in the notes.
        """
        timestamp = now_iso()
        for step in steps:
            sink.append(
                StepResult(
                    name=step.name,
                    kind=type(step).__name__,
                    phase=phase,
                    outcome=Outcome.SKIP,
                    note=step.note,
                    notes=f"skipped: {reason}",
                    started_at=timestamp,
                    finished_at=timestamp,
                )
            )
        self._persist()

    def _run_teardowns(self) -> None:
        """Drain registered cleanups in reverse order, swallowing errors.

        Each cleanup is isolated so one failing teardown cannot prevent the
        others (or the rest of shutdown) from running.
        """
        while self._teardowns:
            cleanup = self._teardowns.pop()
            try:
                cleanup()
            except Exception as exc:  # noqa: BLE001 - teardown must not raise
                self.logger.exception("Teardown callable raised")
                self._notify_error(
                    f"A teardown callable raised {type(exc).__name__}: "
                    f"{_first_line(exc)} — check the CLI/log output for the "
                    "full traceback."
                )

    def _notify_error(self, message: str) -> None:
        """Surface ``message`` as a browser banner, if the web surface is up.

        Some failures (e.g. an unhandled exception during suite setup, before
        any test case's live step list exists — see :meth:`_publish_session`)
        would otherwise only appear in the CLI/log, leaving an operator using
        only the browser with no indication anything went wrong.

        Args:
            message: Human-readable text to show; no-op if the web surface is
                disabled.
        """
        if self._web_gio is not None:
            self._web_gio.notify(message)

    def _persist(self) -> None:
        """Invoke the ``on_update`` callback with the current result, if set.

        Also refreshes the browser session page's live step list so its
        active-step highlight tracks execution (see :meth:`_publish_session`).
        """
        self._publish_session()
        if self._on_update is None or self._result is None:
            return
        try:
            self._on_update(self._result)
        except OSError as exc:
            self.logger.warning("Failed to persist interim result: %s", exc)

    def _begin_session(self, case: TestCase, case_result: TestCaseResult) -> None:
        """Start publishing live step progress for a case attempt.

        Captures the case's planned steps (flattened so optional groups and
        their children line up 1:1 with the results they produce) as the fixed
        skeleton the session page renders; subsequent :meth:`_publish_session`
        calls fill in outcomes and move the active-step highlight.

        Args:
            case: The test case being run.
            case_result: The result the attempt accumulates into.
        """
        self._active_case = case
        self._active_case_result = case_result
        self._active_planned = self._flatten_planned(case.steps)
        self._active_phase = "test_setup"

    def _clear_session(self) -> None:
        """Stop publishing a live step list (between/after cases)."""
        self._active_case = None
        self._active_case_result = None
        self._active_planned = []
        self._active_phase = ""
        if self._web_gio is not None:
            self._web_gio.set_session(None)

    def _publish_session(self) -> None:
        """Push the current case's step list + active-step to the session page.

        The step list is the planned skeleton captured by :meth:`_begin_session`;
        each entry is resolved to its outcome once the matching step has run (the
        engine appends step-phase results in planned order), the step currently
        executing (or awaiting an operator prompt) is marked ``active`` while the
        run is in the ``step`` phase, and the rest stay ``pending``. During
        setup/teardown/review no step is active. No-op when the browser surface
        is disabled or no case is running.
        """
        gio = self._web_gio
        case = self._active_case
        case_result = self._active_case_result
        if gio is None or case is None or case_result is None:
            return
        done = [r for r in case_result.steps if r.phase == "step"]
        active_index = len(done) if self._active_phase == "step" else -1
        steps: list[dict[str, Any]] = []
        for index, planned in enumerate(self._active_planned):
            entry = dict(planned)
            if index < len(done):
                result = done[index]
                entry["status"] = result.outcome.value
                entry["detail"] = result.notes or result.error
                # Surface a finished command step's output so the page can
                # expand the step to show what the command printed.
                if result.output:
                    entry["command"] = result.detail
                    entry["output"] = result.output
            elif index == active_index:
                entry["status"], entry["detail"] = "active", ""
            else:
                entry["status"], entry["detail"] = "pending", ""
            steps.append(entry)
        gio.set_session(
            {
                "case": {"id": case.id, "name": case.name},
                "phase": self._active_phase,
                "steps": steps,
            }
        )

    @staticmethod
    def _flatten_planned(steps: Sequence[Step]) -> list[dict[str, Any]]:
        """Flatten planned steps into the display skeleton for the session page.

        An :class:`~model.OptionalStep` becomes its own entry (the decision
        marker) followed by its children, mirroring the marker+children results
        the engine records for it, so the flattened list aligns 1:1 with the
        step-phase results in execution order.

        Args:
            steps: The case's top-level steps.

        Returns:
            One ``{"name", "optional", "depth"}`` mapping per displayed step.
        """
        flat: list[dict[str, Any]] = []

        def walk(items: Sequence[Step], depth: int) -> None:
            for item in items:
                optional = isinstance(item, OptionalStep)
                flat.append({"name": item.name, "optional": optional, "depth": depth})
                if optional:
                    walk(item.steps, depth + 1)

        walk(steps, 0)
        return flat


def _has_blocking_failure(results: Sequence[StepResult]) -> bool:
    """Return whether any result in ``results`` failed or errored."""
    return any(result.outcome in _BLOCKING for result in results)


def _first_line(exc: Exception) -> str:
    """Return just the first line of ``str(exc)``.

    Some exceptions (e.g. Playwright's) embed a multi-line, boxed hint in
    their message; the full text still reaches the CLI/log via
    ``logger.exception``, so a browser notice only needs the headline.
    """
    text = str(exc).strip()
    return text.splitlines()[0] if text else ""
