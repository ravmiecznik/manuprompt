"""Exception types raised by ManuPrompt.

All exceptions inherit from :class:`ManuPromptError` so callers can catch the
whole family with a single ``except`` clause.
"""

from __future__ import annotations


class ManuPromptError(Exception):
    """Base class for every error raised by ManuPrompt."""


class SuiteValidationError(ManuPromptError):
    """Raised when a YAML suite document does not match the schema.

    Carries a human-readable message that points at the offending part of
    the document (suite name, test-case id, step index) so authors can fix
    the file without reading a traceback.
    """


class VariableError(ManuPromptError):
    """Raised when a ``${...}`` reference cannot be resolved.

    This covers both an unknown variable name and a variable that is
    declared but still holds ``None`` (e.g. a ``store`` target that has not
    been filled in by an earlier step).
    """


class CallError(ManuPromptError):
    """Raised when a ``call`` step cannot be resolved or invoked.

    This covers a malformed target (not ``module.function``), a missing
    module file in the suite directory, a missing or non-callable function
    in that module, and import errors raised while loading the module.
    """
