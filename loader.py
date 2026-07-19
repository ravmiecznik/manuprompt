"""Parse and validate a YAML suite document into the data model.

The loader is the only place that knows the on-disk YAML shape. It normalises
each step into a typed :class:`~model.Step` subclass by detecting the single
action key present (``prompt`` / ``tool`` / ``callback``) and attaching the
allowed modifiers. Validation errors are raised as
:class:`~errors.SuiteValidationError` with messages that locate the problem.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

from .errors import SuiteValidationError
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
from .theme import Theme
from .theme import parse_theme_mapping as _parse_theme_mapping

# Action keys that identify a step's kind. Exactly one must be present.
_ACTION_KEYS: frozenset[str] = frozenset(
    {"prompt", "tool", "shell", "call", "optional"}
)

# Modifier keys allowed alongside each action key.
_ALLOWED_MODIFIERS: dict[str, frozenset[str]] = {
    "prompt": frozenset({"store", "name"}),
    "tool": frozenset({"command", "save_output", "name"}),
    "shell": frozenset({"save_output", "name"}),
    "call": frozenset({"args", "name"}),
    "optional": frozenset({"message"}),
}

def load_suite(path: Path | str) -> Suite:
    """Load and validate a suite from a YAML file.

    Args:
        path: Path to the YAML suite document.

    Returns:
        The parsed :class:`~model.Suite`.

    Raises:
        SuiteValidationError: If the file is missing, unparsable, or does not
            match the suite schema.
    """
    file_path = Path(path)
    try:
        raw_text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SuiteValidationError(f"Cannot read suite file {file_path}: {exc}") from exc
    try:
        document = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise SuiteValidationError(f"Invalid YAML in {file_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise SuiteValidationError(
            f"{file_path}: top level must be a mapping with a 'suite' key"
        )
    return _parse_suite(document, source_dir=file_path.resolve().parent)


def _parse_suite(document: dict[str, Any], source_dir: Path | None = None) -> Suite:
    """Build a :class:`~model.Suite` from a parsed YAML mapping.

    Args:
        document: Parsed top-level YAML mapping.
        source_dir: Directory the document was loaded from, recorded on the
            suite so ``call`` steps can resolve their module against it.

    Returns:
        The validated suite.

    Raises:
        SuiteValidationError: If required keys are missing or malformed.
    """
    suite_block = document.get("suite")
    if not isinstance(suite_block, dict):
        raise SuiteValidationError("Missing or malformed 'suite' mapping")

    name = suite_block.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SuiteValidationError("suite.name is required and must be a string")

    tools = _parse_tools(suite_block.get("tools", {}))
    variables = _parse_variables(suite_block.get("variables", {}), "suite.variables")
    test_environment = _parse_test_environment(suite_block.get("test_environment"))

    # Test cases are sorted by id so the run order (and reports) are stable and
    # alphabetical regardless of how they are ordered in the YAML.
    cases = tuple(
        sorted(
            (
                _parse_case(item, index)
                for index, item in enumerate(document.get("test_cases", []) or [])
            ),
            key=lambda case: case.id,
        )
    )

    return Suite(
        name=name,
        description=str(suite_block.get("description", "") or ""),
        variables=variables,
        test_environment=test_environment,
        tools=tools,
        suite_setup=_parse_steps(suite_block.get("suite_setup"), "suite_setup"),
        test_setup=_parse_steps(suite_block.get("test_setup"), "test_setup"),
        test_teardown=_parse_steps(suite_block.get("test_teardown"), "test_teardown"),
        suite_teardown=_parse_steps(suite_block.get("suite_teardown"), "suite_teardown"),
        test_cases=cases,
        web_title=str(suite_block.get("web_title", "") or ""),
        theme=_parse_theme(suite_block.get("theme")),
        source_dir=source_dir,
    )


def _parse_theme(raw: Any) -> Theme:
    """Validate and parse the suite's own ``theme:`` block into a :class:`Theme`.

    This is a **per-suite override**, layered on top of the project-wide
    ``theme.yaml`` at run time (see :func:`theme.effective_theme`) — every
    field here is optional; unset fields simply fall through to that file (or
    each surface's own built-in default when neither sets it).

    Args:
        raw: Value of ``suite.theme`` (may be ``None``).

    Returns:
        A :class:`Theme` with the given fields set (empty when ``raw`` is
        absent).

    Raises:
        SuiteValidationError: If ``raw`` is present but not a mapping, has an
            unknown key, or a value is not a non-empty string.
    """
    return _parse_theme_mapping(raw, "suite.theme")


def _parse_tools(raw: Any) -> dict[str, str]:
    """Validate and normalise the ``tools`` mapping.

    Args:
        raw: Value of ``suite.tools``.

    Returns:
        Mapping of tool name to path string.

    Raises:
        SuiteValidationError: If ``tools`` is not a string-to-string mapping.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise SuiteValidationError("suite.tools must be a mapping of name -> path")
    tools: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            raise SuiteValidationError(f"suite.tools.{key} must be a string path")
        tools[str(key)] = value
    return tools


def _parse_test_environment(raw: Any) -> tuple[tuple[str, str], ...]:
    """Validate and normalise the ``test_environment`` block.

    The block describes the environment a run was performed in. It accepts a
    mapping (``key: value``) or a list whose items are either single-/multi-key
    mappings (``- key: value``) or plain scalars (``- free text``). The result
    is an ordered tuple of ``(label, value)`` pairs; scalar entries yield a pair
    with an empty label.

    Args:
        raw: Value of ``suite.test_environment`` (may be ``None``).

    Returns:
        Ordered ``(label, value)`` pairs (empty when ``raw`` is falsy).

    Raises:
        SuiteValidationError: If the block is present but not a mapping or list.
    """
    if not raw:
        return ()
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        items: list[Any] = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        raise SuiteValidationError(
            "suite.test_environment must be a mapping or a list"
        )
    for item in items:
        if isinstance(item, dict):
            pairs.extend((str(key), str(value)) for key, value in item.items())
        else:
            pairs.append(("", str(item)))
    return tuple(pairs)


def _parse_variables(raw: Any, location: str) -> dict[str, Any]:
    """Validate and normalise a ``variables`` mapping.

    Args:
        raw: Value of a ``variables`` block.
        location: Dotted location used in error messages.

    Returns:
        The variables mapping (an empty dict when absent).

    Raises:
        SuiteValidationError: If present but not a mapping.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise SuiteValidationError(f"{location} must be a mapping")
    return {str(key): value for key, value in raw.items()}


def _parse_case(raw: Any, index: int) -> TestCase:
    """Build a :class:`~model.TestCase` from a YAML mapping.

    Args:
        raw: A single ``test_cases`` entry.
        index: Zero-based position used in error messages.

    Returns:
        The validated test case.

    Raises:
        SuiteValidationError: If the case is malformed.
    """
    if not isinstance(raw, dict):
        raise SuiteValidationError(f"test_cases[{index}] must be a mapping")
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise SuiteValidationError(f"test_cases[{index}].id is required")
    name = str(raw.get("name", case_id) or case_id)
    description = str(raw.get("description", "") or "")
    location = f"test_cases[{index}] ({case_id})"
    steps = _parse_steps(raw.get("steps"), f"{location}.steps")
    if not steps:
        raise SuiteValidationError(f"{location} has no steps")
    variables = _parse_variables(raw.get("variables", {}), f"{location}.variables")
    skip_reason = _parse_skip(raw.get("skip"), location)
    test_setup = _parse_steps(raw.get("test_setup"), f"{location}.test_setup")
    test_teardown = _parse_steps(raw.get("test_teardown"), f"{location}.test_teardown")
    return TestCase(
        id=case_id,
        name=name,
        steps=steps,
        description=description,
        variables=variables,
        skip_reason=skip_reason,
        test_setup=test_setup,
        test_teardown=test_teardown,
    )


def _parse_skip(raw: Any, location: str) -> str:
    """Parse a test case's ``skip`` marker into a reason string.

    Accepts either a plain string (``skip: reason``) or a mapping with a
    ``reason`` key (``skip:\\n  reason: ...``). An absent marker yields an
    empty string, meaning the case runs normally.

    Args:
        raw: Value of the case's ``skip`` key (may be ``None``).
        location: Dotted location used in error messages.

    Returns:
        The skip reason, or an empty string when not marked to skip.

    Raises:
        SuiteValidationError: If ``skip`` is malformed or its reason is empty.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        if not raw.strip():
            raise SuiteValidationError(f"{location}.skip must be a non-empty string")
        return raw.strip()
    if isinstance(raw, dict):
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise SuiteValidationError(
                f"{location}.skip.reason is required and must be a string"
            )
        return reason.strip()
    raise SuiteValidationError(
        f"{location}.skip must be a string or a mapping with a 'reason' key"
    )


def _parse_steps(raw: Any, location: str) -> tuple[Step, ...]:
    """Parse a list of step mappings.

    Args:
        raw: Value of a steps / setup / teardown list (may be ``None``).
        location: Dotted location used in error messages.

    Returns:
        A tuple of typed steps (empty when ``raw`` is falsy).

    Raises:
        SuiteValidationError: If ``raw`` is present but not a list, or any
            entry is malformed.
    """
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise SuiteValidationError(f"{location} must be a list of steps")
    return tuple(
        _parse_step(item, f"{location}[{index}]")
        for index, item in enumerate(raw)
    )


def _parse_step(raw: Any, location: str) -> Step:
    """Parse and validate a single step mapping into a typed step.

    Args:
        raw: A single step mapping.
        location: Dotted location used in error messages.

    Returns:
        A :class:`~model.PromptStep`, :class:`~model.ToolStep` or
        :class:`~model.CallbackStep`.

    Raises:
        SuiteValidationError: If the step has zero or multiple action keys,
            an unknown modifier, or a malformed value.
    """
    if not isinstance(raw, dict):
        raise SuiteValidationError(f"{location} must be a mapping")

    present_actions = _ACTION_KEYS.intersection(raw)
    if len(present_actions) == 0:
        raise SuiteValidationError(
            f"{location} must contain exactly one of {sorted(_ACTION_KEYS)}"
        )
    if len(present_actions) > 1:
        raise SuiteValidationError(
            f"{location} has multiple action keys {sorted(present_actions)}; "
            "a step must contain exactly one"
        )

    action = next(iter(present_actions))
    allowed = _ALLOWED_MODIFIERS[action] | {action, "artifact", "note"}
    unknown = set(raw) - allowed
    if unknown:
        raise SuiteValidationError(
            f"{location} has unknown key(s) {sorted(unknown)} for a "
            f"'{action}' step; allowed: {sorted(allowed)}"
        )

    if action == "prompt":
        step: Step = _parse_prompt_step(raw, location)
    elif action == "tool":
        step = _parse_tool_step(raw, location)
    elif action == "shell":
        step = _parse_shell_step(raw, location)
    elif action == "optional":
        step = _parse_optional_step(raw, location)
    else:
        step = _parse_call_step(raw, location)

    artifacts = _parse_artifacts(raw.get("artifact"), location)
    if artifacts:
        step = dataclasses.replace(step, artifacts=artifacts)
    note = _parse_note(raw.get("note"), location)
    if note:
        step = dataclasses.replace(step, note=note)
    return step


def _parse_note(raw: Any, location: str) -> str:
    """Validate and normalise a step's optional ``note`` modifier.

    Args:
        raw: Value of the step's ``note`` key (may be ``None``).
        location: Dotted location used in error messages.

    Returns:
        The trimmed note string (empty when ``raw`` is absent).

    Raises:
        SuiteValidationError: If ``note`` is present but not a string.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise SuiteValidationError(f"{location}.note must be a string")
    return raw.strip()


def _parse_artifacts(raw: Any, location: str) -> tuple[str, ...]:
    """Parse a step's ``artifact`` modifier into a tuple of labels.

    Accepts a single label (``artifact: console screenshot``) or a list of
    labels. Each label names a file the operator is asked to attach after the
    step runs.

    Args:
        raw: Value of the step's ``artifact`` key (may be ``None``).
        location: Dotted location used in error messages.

    Returns:
        A tuple of non-empty label strings (empty when ``raw`` is absent).

    Raises:
        SuiteValidationError: If a label is not a non-empty string.
    """
    if raw is None:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    labels: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise SuiteValidationError(
                f"{location}.artifact entries must be non-empty strings"
            )
        labels.append(item.strip())
    return tuple(labels)


def _derive_name(explicit: Any, fallback: str) -> str:
    """Return an explicit step name or one derived from the fallback text.

    The full, untruncated text is kept so consumers (console, reports) can
    truncate for display while still exposing the complete text on demand.

    Args:
        explicit: Value of the step's ``name`` key (may be absent/None).
        fallback: Text to derive a name from when no explicit name is given.

    Returns:
        A single-line step name (whitespace collapsed).
    """
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return " ".join(fallback.split())


def _parse_prompt_step(raw: dict[str, Any], location: str) -> PromptStep:
    """Build a :class:`~model.PromptStep`."""
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise SuiteValidationError(f"{location}.prompt must be a non-empty string")
    store = raw.get("store")
    if store is not None and (not isinstance(store, str) or not store.strip()):
        raise SuiteValidationError(f"{location}.store must be a variable name")
    return PromptStep(
        name=_derive_name(raw.get("name"), prompt),
        prompt=prompt.strip(),
        store=store.strip() if isinstance(store, str) else None,
    )


def _parse_tool_step(raw: dict[str, Any], location: str) -> ToolStep:
    """Build a :class:`~model.ToolStep`."""
    tool = raw.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        raise SuiteValidationError(f"{location}.tool must be a non-empty string")
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        raise SuiteValidationError(f"{location}.command is required for a tool step")
    save_output = raw.get("save_output")
    if save_output is not None and (
        not isinstance(save_output, str) or not save_output.strip()
    ):
        raise SuiteValidationError(f"{location}.save_output must be a variable name")
    return ToolStep(
        name=_derive_name(raw.get("name"), f"{tool} {command}"),
        tool=tool.strip(),
        command=command.strip(),
        save_output=save_output.strip() if isinstance(save_output, str) else None,
    )


def _parse_shell_step(raw: dict[str, Any], location: str) -> ShellStep:
    """Build a :class:`~model.ShellStep`."""
    command = raw.get("shell")
    if not isinstance(command, str) or not command.strip():
        raise SuiteValidationError(
            f"{location}.shell must be a non-empty command string"
        )
    save_output = raw.get("save_output")
    if save_output is not None and (
        not isinstance(save_output, str) or not save_output.strip()
    ):
        raise SuiteValidationError(f"{location}.save_output must be a variable name")
    return ShellStep(
        name=_derive_name(raw.get("name"), command),
        command=command.strip(),
        save_output=save_output.strip() if isinstance(save_output, str) else None,
    )


def _parse_optional_step(raw: dict[str, Any], location: str) -> OptionalStep:
    """Build an :class:`~model.OptionalStep` from a YAML mapping.

    The ``optional`` value is a non-empty list of nested steps. An explicit
    ``message`` is the question shown to the operator and the group's label;
    otherwise both are derived from the nested steps.
    """
    nested = raw.get("optional")
    if not isinstance(nested, list) or not nested:
        raise SuiteValidationError(
            f"{location}.optional must be a non-empty list of steps"
        )
    message = raw.get("message")
    if message is not None and (not isinstance(message, str) or not message.strip()):
        raise SuiteValidationError(f"{location}.message must be a non-empty string")
    steps = _parse_steps(nested, f"{location}.optional")
    child_names = ", ".join(step.name for step in steps)
    label = _derive_name(message, f"optional: {child_names}")
    prompt = message.strip() if isinstance(message, str) and message.strip() else ""
    return OptionalStep(name=label, steps=steps, prompt=prompt)


def _parse_call_step(raw: dict[str, Any], location: str) -> CallStep:
    """Build a :class:`~model.CallStep`.

    Validates that the ``call`` target is a ``module.function`` reference
    with exactly one dot so the handler can split it unambiguously.
    """
    target = raw.get("call")
    if not isinstance(target, str) or not target.strip():
        raise SuiteValidationError(f"{location}.call must be a non-empty string")
    target = target.strip()
    module_name, sep, func_name = target.partition(".")
    if not sep or not module_name or not func_name or "." in func_name:
        raise SuiteValidationError(
            f"{location}.call must be 'module.function' (got {target!r})"
        )
    args = raw.get("args", {})
    if args and not isinstance(args, dict):
        raise SuiteValidationError(f"{location}.args must be a mapping")
    return CallStep(
        name=_derive_name(raw.get("name"), target),
        target=target,
        args=dict(args) if args else {},
    )
