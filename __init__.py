"""ManuPrompt — a YAML-driven, human-in-the-loop test runner.

A suite is described declaratively in YAML: suite/test setup and teardown,
test cases, and ordered steps. Each step is one of:

* ``prompt`` — instruct the operator and record a PASS/FAIL/acknowledge
  verdict (or capture typed input via ``store``);
* ``tool`` — run a configured command-line tool, judged by its exit code;
* ``call`` — call a ``module.function`` defined alongside the suite YAML.

The engine drives the suite, prompting for manual steps and automating the
rest, and writes a JSON result that reporters render into shareable formats.

Example:
    >>> from manuprompt import load_suite, run_suite
    >>> suite = load_suite("demo/demo-suite.yml")
    >>> result = run_suite(suite)  # doctest: +SKIP
    >>> result.outcome  # doctest: +SKIP
    <Outcome.PASS: 'pass'>
"""

from __future__ import annotations

from .context import RunContext
from .engine import Engine
from .errors import (
    CallError,
    ManuPromptError,
    SuiteValidationError,
    VariableError,
)
from .model import (
    CallStep,
    OptionalStep,
    PromptStep,
    ShellStep,
    Step,
    Suite,
    TestCase,
    ToolStep,
)
from .prompter import ConsolePrompter, Prompter
from .reporting import (
    SKIP_CASE,
    CaseCandidate,
    apply_untested,
    collect_logs,
    load_result,
    merge_results,
    relocate_artifacts,
    write_html,
    write_json,
)
from .results import Outcome, StepResult, SuiteResult, TestCaseResult
from .api import generate_report, load_suite, run_suite
from .theme import Theme
from .webui import Channel, FileUpload, WebGIO, WebPrompter

__all__ = [
    "CallError",
    "CallStep",
    "CaseCandidate",
    "Channel",
    "ConsolePrompter",
    "Engine",
    "FileUpload",
    "SKIP_CASE",
    "OptionalStep",
    "Outcome",
    "PromptStep",
    "Prompter",
    "RunContext",
    "ShellStep",
    "Step",
    "StepResult",
    "ManuPromptError",
    "Suite",
    "SuiteResult",
    "SuiteValidationError",
    "TestCase",
    "TestCaseResult",
    "Theme",
    "ToolStep",
    "VariableError",
    "WebGIO",
    "WebPrompter",
    "apply_untested",
    "collect_logs",
    "generate_report",
    "load_result",
    "load_suite",
    "merge_results",
    "relocate_artifacts",
    "run_suite",
    "write_html",
    "write_json",
]
