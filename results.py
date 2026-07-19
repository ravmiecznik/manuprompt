"""Result model produced by executing a suite.

The result tree (:class:`SuiteResult` -> :class:`TestCaseResult` ->
:class:`StepResult`) is the single source of truth for every reporter. It is
JSON-serialisable via the ``to_dict`` methods so the JSON reporter can persist
it incrementally and the HTML reporter can render it later without re-running
anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class Outcome(str, Enum):
    """Outcome of a single step or an aggregated container.

    Attributes:
        PASS: Step succeeded (operator verdict, exit code 0, or callback
            returned normally).
        FAIL: Step failed (operator verdict, non-zero exit code, or callback
            signalled failure).
        ERROR: Step could not be evaluated (handler raised unexpectedly).
        ACK: Step was acknowledged without a pass/fail verdict (instruction
            or input-capture step).
        SKIP: Step did not run (e.g. a preceding setup step failed).
    """

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    ACK = "ack"
    SKIP = "skip"


# Outcomes that make an enclosing container (case / suite) count as failed.
_FAILING: frozenset[Outcome] = frozenset({Outcome.FAIL, Outcome.ERROR})


def aggregate_outcome(outcomes: Iterable[Outcome]) -> Outcome:
    """Reduce child outcomes to a single container outcome.

    A container fails if any child failed or errored; otherwise it passes if
    at least one child passed; otherwise it is acknowledged (only ack/skip
    children). An empty container is treated as :attr:`Outcome.PASS`.

    Args:
        outcomes: Child outcomes to reduce.

    Returns:
        The aggregated outcome.
    """
    materialised = list(outcomes)
    if any(o == Outcome.ERROR for o in materialised):
        return Outcome.ERROR
    if any(o == Outcome.FAIL for o in materialised):
        return Outcome.FAIL
    if any(o == Outcome.PASS for o in materialised):
        return Outcome.PASS
    if materialised and all(o == Outcome.SKIP for o in materialised):
        return Outcome.SKIP
    return Outcome.PASS


@dataclass
class StepResult:
    """Outcome and evidence captured for a single executed step.

    Attributes:
        name: Step label (mirrors :attr:`model.Step.name`).
        kind: Step kind discriminator (``prompt`` / ``tool`` / ``callback``).
        phase: Lifecycle phase the step ran in (e.g. ``step``,
            ``suite_setup``).
        outcome: Result of the step.
        detail: Resolved instruction / command text shown to the operator.
        output: Captured stdout/stderr (tool steps) or callback return repr.
        note: Author-provided annotation from the step's ``note`` modifier.
        notes: Free-text operator notes.
        error: Exception text when ``outcome`` is :attr:`Outcome.ERROR`.
        artifacts: Files attached to the step as ``{"label", "path"}`` mappings;
            ``path`` is relative to the run's artifacts directory. Filled by
            :meth:`~context.RunContext.attach` during the step and/or by
            operator ``artifact:`` collection afterwards.
        started_at: ISO-8601 start timestamp.
        finished_at: ISO-8601 finish timestamp.
    """

    name: str
    kind: str
    phase: str
    outcome: Outcome
    detail: str = ""
    output: str = ""
    note: str = ""
    notes: str = ""
    error: str = ""
    artifacts: list[dict[str, str]] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the step result."""
        return {
            "name": self.name,
            "kind": self.kind,
            "phase": self.phase,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "output": self.output,
            "note": self.note,
            "notes": self.notes,
            "error": self.error,
            "artifacts": [dict(a) for a in self.artifacts],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class TestCaseResult:
    """Aggregated result for one test case.

    Attributes:
        id: Test-case identifier.
        name: Test-case title.
        steps: Per-step results, including setup/teardown steps tagged by
            their ``phase``.
        skip_reason: Reason the case was skipped, or empty when it ran.
        logs: Files attached to the case as ``{"label", "path"}`` mappings
            (e.g. a per-test device log attached by glue via
            :meth:`~context.RunContext.attach_log`); ``path`` is relative to
            the run's artifacts directory.
        started_at: ISO-8601 start timestamp.
        finished_at: ISO-8601 finish timestamp.
    """

    id: str
    name: str
    steps: list[StepResult] = field(default_factory=list)
    skip_reason: str = ""
    logs: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def outcome(self) -> Outcome:
        """Aggregated outcome across all of the case's steps."""
        return aggregate_outcome(s.outcome for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the case result."""
        return {
            "id": self.id,
            "name": self.name,
            "outcome": self.outcome.value,
            "skip_reason": self.skip_reason,
            "logs": [dict(entry) for entry in self.logs],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class SuiteResult:
    """Top-level result for an executed suite.

    Attributes:
        name: Suite name.
        description: Suite description.
        test_environment: Ordered ``(label, value)`` pairs describing the
            environment the run was performed in.
        suite_setup: Results of the one-time setup steps.
        cases: Per-test-case results.
        suite_teardown: Results of the one-time teardown steps.
        theme: Colour/font overrides (see :mod:`theme`), carried through to
            the JSON so a report regenerated later (see
            :func:`~reporting.merge.merge_results`) stays themed without
            needing the original suite YAML.
        started_at: ISO-8601 start timestamp.
        finished_at: ISO-8601 finish timestamp.
    """

    name: str
    description: str = ""
    test_environment: list[tuple[str, str]] = field(default_factory=list)
    suite_setup: list[StepResult] = field(default_factory=list)
    cases: list[TestCaseResult] = field(default_factory=list)
    suite_teardown: list[StepResult] = field(default_factory=list)
    theme: dict[str, str] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""

    @property
    def outcome(self) -> Outcome:
        """Aggregated outcome across setup, all cases and teardown."""
        outcomes = [s.outcome for s in self.suite_setup]
        outcomes.extend(c.outcome for c in self.cases)
        outcomes.extend(s.outcome for s in self.suite_teardown)
        return aggregate_outcome(outcomes)

    def counts(self) -> dict[str, int]:
        """Return a tally of case outcomes keyed by outcome value."""
        tally: dict[str, int] = {o.value: 0 for o in Outcome}
        for case in self.cases:
            tally[case.outcome.value] += 1
        return tally

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the suite result."""
        return {
            "name": self.name,
            "description": self.description,
            "outcome": self.outcome.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "test_environment": [
                {"label": label, "value": value}
                for label, value in self.test_environment
            ],
            "counts": self.counts(),
            "suite_setup": [s.to_dict() for s in self.suite_setup],
            "cases": [c.to_dict() for c in self.cases],
            "suite_teardown": [s.to_dict() for s in self.suite_teardown],
            "theme": dict(self.theme),
        }
