"""Colour/font theming shared by the web UI and the HTML report.

A :class:`Theme` is a small set of *overrides* — every field defaults to
``None``, meaning "use the surface's own built-in default". Everything else
keeps its default appearance, so an empty ``Theme()`` renders pixel-identical
to the hardcoded look that shipped before theming existed.

Two layers can set overrides, applied in order (later wins — see
:func:`effective_theme`):

1. **Project-wide** — ``theme.yaml``, next to this module (see
   :func:`package_theme_path`), applies to every suite. This is the normal
   place to set a house style once.
2. **Per-suite** — a suite's own ``theme:`` block (see :mod:`loader`),
   layered on top for that suite only.

The live web UI (:mod:`webui.page`) and the HTML report (:mod:`reporting.html`)
are visually distinct by design — a dark terminal console vs. a light,
shareable document — so each resolves the *same* combined override against its
own base palette (:data:`WEB_DEFAULTS` / :data:`REPORT_DEFAULTS`). Setting
``accent: "#ff6600"`` retints both; setting ``background`` only overrides the
one field, leaving the rest of that surface's palette untouched.

Only a CSS ``font-family`` value is accepted for fonts (a comma-separated
fallback stack of names the operator's OS/browser may already have) — no
embedded font files or external font-service requests, keeping every page a
single self-contained document with no network dependency.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import SuiteValidationError


@dataclass(frozen=True)
class Theme:
    """Optional colour/font overrides for a themed surface.

    Every field is ``None`` by default, meaning "inherit the surface's base
    default" (see :func:`resolve`). Colours are any valid CSS colour value
    (hex, ``rgb()``, a named colour, ...); the font fields are CSS
    ``font-family`` values (a fallback stack, e.g.
    ``"Segoe UI, Helvetica, Arial, sans-serif"``).

    Attributes:
        background: Page background.
        surface: Panel/header/card background (a shade off the page
            background, used for chrome like headers and cards).
        foreground: Main text colour.
        muted: Secondary/dim text colour (timestamps, hints, borders' text).
        border: Border colour for panels, inputs and dividers.
        accent: Primary interactive colour (active tab, links, focus ring,
            primary highlight).
        success: Colour for a passing/positive outcome.
        danger: Colour for a failing/negative outcome.
        warning: Colour for an acknowledged/neutral-caution outcome.
        font_family: General UI text font stack.
        mono_font_family: Font stack for terminal panes and captured
            command/tool output.
    """

    background: str | None = None
    surface: str | None = None
    foreground: str | None = None
    muted: str | None = None
    border: str | None = None
    accent: str | None = None
    success: str | None = None
    danger: str | None = None
    warning: str | None = None
    font_family: str | None = None
    mono_font_family: str | None = None

    def overrides(self) -> dict[str, str]:
        """Return only the fields that are actually set (non-``None``).

        This is the form persisted to the JSON result (see
        :meth:`~results.SuiteResult.to_dict`) and round-tripped back through
        :meth:`from_mapping`, so a saved run — and any report regenerated from
        it later — keeps the suite's theme without needing the original YAML.

        Returns:
            A mapping of field name to value, omitting unset fields.
        """
        return {
            key: value
            for key, value in dataclasses.asdict(self).items()
            if value is not None
        }

    @classmethod
    def from_mapping(cls, raw: dict[str, str] | None) -> "Theme":
        """Build a :class:`Theme` from a plain ``{field: value}`` mapping.

        Args:
            raw: A mapping as produced by :meth:`overrides` (or ``None``).
                Unknown keys are ignored so old/foreign data never raises.

        Returns:
            A :class:`Theme` with the given fields set.
        """
        if not raw:
            return cls()
        return cls(**{key: value for key, value in raw.items() if key in FIELDS})


FIELDS = frozenset(f.name for f in dataclasses.fields(Theme))

# Dark, terminal-style palette matching the web UI's original hardcoded look.
WEB_DEFAULTS = Theme(
    background="#0d1117",
    surface="#161b22",
    foreground="#e6edf3",
    muted="#8b949e",
    border="#30363d",
    accent="#58a6ff",
    success="#3fb950",
    danger="#f85149",
    warning="#d29922",
    font_family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    mono_font_family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
)

# Light, document-style palette matching the HTML report's original look.
# background/foreground use the CSS system colours ``Canvas``/``CanvasText``
# (CSS Color Module Level 4) rather than a hardcoded ``#fff``/``#000`` — these
# resolve to the browser/OS's actual default page colours, so an un-themed
# report keeps adapting to the reader's light/dark preference exactly as
# before, while still being an overridable CSS value like any other.
REPORT_DEFAULTS = Theme(
    background="Canvas",
    surface="color-mix(in srgb, Canvas 94%, CanvasText 6%)",
    foreground="CanvasText",
    muted="#666666",
    border="#dddddd",
    accent="#1565c0",
    success="#2e7d32",
    danger="#c62828",
    warning="#f9a825",
    font_family="system-ui, sans-serif",
    mono_font_family="ui-monospace, monospace",
)


def resolve(theme: Theme, base: Theme) -> Theme:
    """Fill ``theme``'s unset fields from ``base``.

    Args:
        theme: The (possibly partial) override, e.g. from a suite's
            ``theme:`` block. ``None`` is treated as an empty :class:`Theme`.
        base: The surface's built-in default (:data:`WEB_DEFAULTS` or
            :data:`REPORT_DEFAULTS`), which must have every field set.

    Returns:
        A fully-populated :class:`Theme` (no ``None`` fields).
    """
    override = theme.overrides() if theme is not None else {}
    return dataclasses.replace(base, **override)


def css_variables(theme: Theme, base: Theme) -> str:
    """Render ``theme`` (resolved against ``base``) as CSS custom properties.

    Args:
        theme: The (possibly partial) override.
        base: The surface's built-in default, as in :func:`resolve`.

    Returns:
        Newline-joined ``--name: value;`` declarations, meant to sit inside a
        ``:root { ... }`` rule.
    """
    resolved = resolve(theme, base)
    names = {
        "background": "--bg",
        "surface": "--surface",
        "foreground": "--fg",
        "muted": "--muted",
        "border": "--border",
        "accent": "--accent",
        "success": "--success",
        "danger": "--danger",
        "warning": "--warning",
        "font_family": "--font",
        "mono_font_family": "--mono-font",
    }
    lines = [
        f"    {var}: {getattr(resolved, field)};" for field, var in names.items()
    ]
    return "\n".join(lines)


def parse_theme_mapping(raw: Any, location: str) -> Theme:
    """Validate and parse a ``{field: value}`` mapping into a :class:`Theme`.

    Shared by the suite loader (``suite.theme``) and :func:`load_theme_file`
    (``theme.yaml``) so both apply the same rules.

    Args:
        raw: The mapping to parse (may be ``None``/empty).
        location: Dotted location used in error messages (e.g.
            ``"suite.theme"`` or a file path).

    Returns:
        A :class:`Theme` with the given fields set (empty when ``raw`` is
        absent).

    Raises:
        SuiteValidationError: If ``raw`` is present but not a mapping, has an
            unknown key, or a value is not a non-empty string.
    """
    if not raw:
        return Theme()
    if not isinstance(raw, dict):
        raise SuiteValidationError(f"{location} must be a mapping")
    unknown = set(raw) - FIELDS
    if unknown:
        raise SuiteValidationError(
            f"{location} has unknown key(s) {sorted(unknown)}; allowed: {sorted(FIELDS)}"
        )
    values: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise SuiteValidationError(
                f"{location}: {key!r} must be a non-empty string"
            )
        values[str(key)] = value.strip()
    return Theme(**values)


def package_theme_path() -> Path:
    """Return the path of the project-wide theme file, next to this module.

    This is the tool's own top-level directory — since a project typically
    checks out (or copies) the whole ``manuprompt`` tree in as a
    subdirectory (see the demo's README), this file sits at that project's
    root alongside ``model.py``, ``cli.py``, etc., rather than inside any one
    suite's own directory.

    Returns:
        The path ``theme.yaml`` is expected at (it need not exist).
    """
    return Path(__file__).resolve().parent / "theme.yaml"


def load_theme_file(path: Path | None = None) -> Theme:
    """Load the project-wide theme file into a :class:`Theme`.

    Two shapes are accepted:

    * **Flat** — the fields directly at the top level, as a single theme::

          accent: "#3ecf9a"
          background: "#132420"

    * **Named presets** — several named palettes, plus an ``apply:`` key
      naming the one currently in effect::

          green:
            accent: "#3ecf9a"
            background: "#132420"
          solarized:
            accent: "#859900"
            background: "#002b36"
          apply: solarized

      Every preset is validated (so a typo in an unused one is still caught),
      but only the one named by ``apply`` is returned. Switching themes is
      then just editing the ``apply:`` line.

    Args:
        path: Path to the theme YAML file. Defaults to
            :func:`package_theme_path`.

    Returns:
        A :class:`Theme` with the active fields set, or an empty
        :class:`Theme` when the file does not exist.

    Raises:
        SuiteValidationError: If the file exists but cannot be read/parsed,
            isn't a mapping, defines presets without an ``apply:`` key (or
            names one that isn't defined), or a preset/flat mapping fails
            :func:`parse_theme_mapping`'s validation.
    """
    theme_path = path if path is not None else package_theme_path()
    if not theme_path.is_file():
        return Theme()
    try:
        raw_text = theme_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SuiteValidationError(f"Cannot read theme file {theme_path}: {exc}") from exc
    try:
        document = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise SuiteValidationError(f"Invalid YAML in {theme_path}: {exc}") from exc
    if document is None:
        return Theme()
    if not isinstance(document, dict):
        raise SuiteValidationError(f"{theme_path} must be a mapping")
    if "apply" in document:
        return _parse_theme_presets(document, theme_path)
    nested = sorted(key for key, value in document.items() if isinstance(value, dict))
    if nested:
        raise SuiteValidationError(
            f"{theme_path} defines preset(s) {nested} but no 'apply' key "
            f"selects one to use (e.g. apply: {nested[0]})"
        )
    return parse_theme_mapping(document, str(theme_path))


def _parse_theme_presets(document: dict[str, Any], path: Path) -> Theme:
    """Resolve the named-presets shape of a theme file (see :func:`load_theme_file`).

    Args:
        document: The parsed YAML mapping, containing an ``apply`` key.
        path: The file's path, used in error messages.

    Returns:
        The :class:`Theme` for the preset named by ``apply``.

    Raises:
        SuiteValidationError: If ``apply`` isn't a non-empty string, names a
            preset that isn't defined, or any preset fails validation.
    """
    apply_name = document.get("apply")
    if not isinstance(apply_name, str) or not apply_name.strip():
        raise SuiteValidationError(
            f"{path}: 'apply' must be a string naming one of the presets defined here"
        )
    apply_name = apply_name.strip()
    presets: dict[str, Theme] = {
        str(key): parse_theme_mapping(value, f"{path} (preset {key!r})")
        for key, value in document.items()
        if key != "apply"
    }
    if apply_name not in presets:
        raise SuiteValidationError(
            f"{path}: apply: {apply_name!r} is not defined; "
            f"available presets: {sorted(presets)}"
        )
    return presets[apply_name]


def layer(*themes: Theme) -> Theme:
    """Combine override-only themes, later arguments taking precedence.

    Args:
        *themes: Themes to merge, in increasing priority order.

    Returns:
        A single :class:`Theme` with each field taken from the last theme
        (in argument order) that set it.
    """
    merged: dict[str, str] = {}
    for theme in themes:
        merged.update(theme.overrides())
    return Theme(**merged)


def effective_theme(suite_theme: Theme) -> Theme:
    """Return a suite's effective theme: ``theme.yaml``, then its own override.

    Args:
        suite_theme: The suite's own ``theme:`` block (a :class:`Suite`'s
            ``theme`` attribute — accepted directly, rather than the
            :class:`~model.Suite`, to avoid a circular import).

    Returns:
        The combined override (project-wide file, overridden per-field by
        the suite), ready to pass to :func:`resolve`/:func:`css_variables` or
        to persist via :meth:`Theme.overrides`.
    """
    return layer(load_theme_file(), suite_theme)
