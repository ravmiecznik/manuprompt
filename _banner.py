"""Framed, optionally coloured terminal banners.

Used to make an important URL (the live I/O surface, an artifact drop page)
stand out from surrounding log lines. Colour is applied only on a TTY with
``NO_COLOR`` unset; the text is always printed in plain form inside the frame
so it stays readable and greppable when colour is off.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

_RESET = "\033[0m"
_BORDER = "\033[1;33m"  # bold yellow frame
_HEADING = "\033[1;33m"  # bold yellow heading
_URL = "\033[1;4;36m"  # bold underlined cyan URL
_HINT = "\033[2m"  # dim hint


def supports_color(stream: TextIO) -> bool:
    """Return whether ANSI colour should be used for ``stream``.

    Args:
        stream: The stream a banner will be written to.

    Returns:
        ``True`` if the stream is an interactive TTY and ``NO_COLOR`` is unset.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    is_tty = getattr(stream, "isatty", None)
    return bool(is_tty and is_tty())


def box_url(heading: str, url: str, hint: str, stream: TextIO = sys.stdout) -> None:
    """Print a framed banner: a heading, the URL, and a one-line hint.

    Args:
        heading: Bold heading line (e.g. the surface name or artifact label).
        url: The URL to advertise, shown emphasised.
        hint: A short instruction shown dimmed under the URL.
        stream: Stream to write the banner to (defaults to stdout).
    """
    color = supports_color(stream)
    lines = [
        (heading, _HEADING),
        ("", None),
        (url, _URL),
        ("", None),
        (hint, _HINT),
    ]
    width = max(len(text) for text, _ in lines)
    pad = 3
    inner = width + pad * 2

    def paint(text: str, style: str | None) -> str:
        return f"{style}{text}{_RESET}" if (color and style) else text

    side = paint("│", _BORDER)
    out = [paint("┌" + "─" * inner + "┐", _BORDER)]
    for text, style in lines:
        body = paint(text.ljust(width), style)
        out.append(f"{side}{' ' * pad}{body}{' ' * pad}{side}")
    out.append(paint("└" + "─" * inner + "┘", _BORDER))

    stream.write("\n" + "\n".join(out) + "\n\n")
    stream.flush()


def numbered_list(heading: str, items: list[str], stream: TextIO = sys.stdout) -> None:
    """Print a heading followed by a numbered list.

    Used to show the operator which test cases a session will run. The heading
    is emphasised on a colour-capable TTY; items are always printed plainly so
    the list stays readable when colour is off.

    Args:
        heading: The list heading (e.g. ``"Test cases to run (3):"``).
        items: Lines to enumerate (already formatted, e.g. ``"DEMO001  five"``).
        stream: Stream to write to (defaults to stdout).
    """
    color = supports_color(stream)
    heading_line = f"{_HEADING}{heading}{_RESET}" if color else heading
    width = len(str(len(items)))
    body = [f"  {index:>{width}}. {item}" for index, item in enumerate(items, start=1)]
    if not items:
        body = ["  (none)"]
    stream.write("\n" + heading_line + "\n" + "\n".join(body) + "\n\n")
    stream.flush()
