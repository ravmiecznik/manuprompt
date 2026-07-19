"""Immutable data model for a ManuPrompt suite.

A suite is parsed from YAML into these dataclasses by
:mod:`manuprompt.loader`. The model is deliberately decoupled from
execution: it describes *what* a suite contains, never *how* it runs. Each
:class:`Step` subclass is a small discriminated-union variant identified by
the action key present in the YAML (``prompt`` / ``tool`` / ``callback``),
which the engine dispatches to a matching handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .theme import Theme


@dataclass(frozen=True)
class Step:
    """Base class for a single executable step.

    Attributes:
        name: Short human-readable label used in logs and reports. Derived
            by the loader from the step's content when not given explicitly.
        artifacts: Labels of files the operator is asked to attach after the
            step runs. Each collected file is copied into the run's artifacts
            directory and shown in the report under its label.
        note: Author-provided annotation for the step (e.g. a reminder or
            extra instruction). Shown to the operator when the step runs and
            recorded on the result for the report.
    """

    name: str
    artifacts: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class PromptStep(Step):
    """A step that instructs the operator and waits for a response.

    The operator may record a PASS/FAIL verdict or simply acknowledge the
    step to continue. When ``store`` is set the typed input is captured into
    a variable instead of being interpreted as a verdict.

    Attributes:
        prompt: Instruction text shown to the operator. May contain
            ``${var}`` references resolved at run time.
        store: Optional variable name to capture the operator's typed input
            into. When set, the step collects input rather than a verdict.
    """

    prompt: str = ""
    store: str | None = None


@dataclass(frozen=True)
class ToolStep(Step):
    """A step that runs a configured command-line tool.

    Attributes:
        tool: Key into the suite's ``tools`` mapping (resolves to a binary
            path or launcher).
        command: Arguments appended to the tool path. May contain ``${var}``
            references resolved at run time.
        save_output: Optional variable name to store the tool's stdout into
            for later interpolation or validation.
    """

    tool: str = ""
    command: str = ""
    save_output: str | None = None


@dataclass(frozen=True)
class ShellStep(Step):
    """A step that runs an arbitrary shell command.

    Unlike a :class:`ToolStep`, the command is not resolved against the suite's
    ``tools`` mapping — it runs verbatim (after ``${var}`` substitution).

    Attributes:
        command: Shell command line to run. May contain ``${var}`` references
            resolved at run time. Executed through the shell, so pipes, globs
            and redirections are supported.
        save_output: Optional variable name to store the command's stdout into
            for later interpolation or validation.
    """

    command: str = ""
    save_output: str | None = None


@dataclass(frozen=True)
class CallStep(Step):
    """A step that calls a function defined alongside the suite.

    The ``target`` is a ``module.function`` reference resolved against the
    suite's own directory: ``browser.launch`` loads ``browser.py`` from the suite
    directory and calls its ``launch`` function. This is the seam where
    project-specific behaviour (e.g. browser automation) plugs in, keeping the
    core free of any such dependency.

    Attributes:
        target: Dotted ``module.function`` reference (exactly one dot).
        args: Keyword arguments forwarded to the function. String values may
            contain ``${var}`` references resolved at run time.
    """

    target: str = ""
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OptionalStep(Step):
    """A group of steps the operator may choose to run or skip.

    When reached, the engine asks the operator whether to perform the group.
    On confirmation the nested ``steps`` run in order in the current scope; on
    decline they are recorded as skipped. Nesting is allowed — a nested step
    may itself be an :class:`OptionalStep`.

    Attributes:
        steps: Nested steps run only when the operator opts in.
        prompt: Question shown to the operator. When empty, the engine derives
            one from the nested steps' names.
    """

    steps: tuple[Step, ...] = ()
    prompt: str = ""


@dataclass(frozen=True)
class TestCase:
    """A named, ordered collection of steps.

    Attributes:
        id: Stable identifier (e.g. ``DEMO001``).
        name: Human-readable title.
        steps: Ordered steps to execute.
        variables: Case-scoped variables, overlaid on the suite variables
            for the duration of the case.
        skip_reason: When set, the case is marked to be skipped; the operator
            is told the reason and asked whether to run it anyway. Empty means
            the case runs normally.
        test_setup: Case-specific setup steps, run after the suite's
            ``test_setup`` for this case only.
        test_teardown: Case-specific teardown steps, run before the suite's
            ``test_teardown`` for this case only.
    """

    id: str
    name: str
    steps: tuple[Step, ...]
    variables: dict[str, Any] = field(default_factory=dict)
    skip_reason: str = ""
    test_setup: tuple[Step, ...] = ()
    test_teardown: tuple[Step, ...] = ()


@dataclass(frozen=True)
class Suite:
    """A complete ManuPrompt suite.

    Attributes:
        name: Suite title.
        description: Free-text description.
        variables: Suite-scoped variables, available to every case.
        test_environment: Ordered ``(label, value)`` pairs describing the test
            environment (e.g. target URL, tester name, firmware version). A pair
            with an empty label is a free-text line without a key.
        tools: Mapping of tool name to its binary path / launcher.
        suite_setup: Steps run once before any test case.
        test_setup: Steps run before each test case.
        test_teardown: Steps run after each test case.
        suite_teardown: Steps run once after all test cases.
        test_cases: Ordered test cases.
        web_title: Display name for the run's live output console (shown in the
            startup banner and as the browser page title). Falls back to
            ``name`` when empty.
        theme: Colour/font overrides for the live web UI and the HTML report
            (see :mod:`theme`). Unset fields keep each surface's own default
            look; the web UI and report are themed independently from the
            same overrides.
        source_dir: Directory the suite file was loaded from. ``call`` steps
            resolve their module against this directory. ``None`` when the
            suite was not loaded from a file.
    """

    name: str
    description: str = ""
    variables: dict[str, Any] = field(default_factory=dict)
    test_environment: tuple[tuple[str, str], ...] = ()
    tools: dict[str, str] = field(default_factory=dict)
    suite_setup: tuple[Step, ...] = ()
    test_setup: tuple[Step, ...] = ()
    test_teardown: tuple[Step, ...] = ()
    suite_teardown: tuple[Step, ...] = ()
    test_cases: tuple[TestCase, ...] = ()
    web_title: str = ""
    theme: Theme = field(default_factory=Theme)
    source_dir: Path | None = None
