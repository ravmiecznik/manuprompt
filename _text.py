"""Small text utilities shared across the package.

Kept dependency-free so any module (reporters, the web UI) can reuse it
without pulling in the rest of the package.
"""

from __future__ import annotations

import re

# Matches ANSI/VT100 escape sequences (e.g. colour codes) that terminals
# render but browsers / plain-text viewers show as literal garbage.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from ``text``.

    Args:
        text: Text possibly containing ANSI/VT100 escape sequences.

    Returns:
        The text with all escape sequences removed.
    """
    return _ANSI_RE.sub("", text)


# Tokeniser for the minimal RTF stripper: a control word (with optional numeric
# argument), a ``\'hh`` hex escape, a ``\x`` escaped symbol, a group brace, a
# (discarded) newline, or any other single character.
_RTF_TOKEN = re.compile(
    r"\\([a-z]{1,32})(-?\d{1,10})?[ ]?|\\'([0-9a-fA-F]{2})|\\([^a-z])|([{}])|[\r\n]+|(.)",
    re.IGNORECASE | re.DOTALL,
)

# Matches any lone UTF-16 surrogate code point, which cannot be UTF-8 encoded.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

# Control words that introduce a destination whose contents are not body text
# (font/colour tables, embedded objects, pictures, field instructions, …).
_RTF_DESTINATIONS = frozenset(
    {
        "colortbl", "fonttbl", "stylesheet", "info", "pict", "object", "objdata",
        "themedata", "colorschememapping", "latentstyles", "datastore", "generator",
        "filetbl", "listtable", "listoverridetable", "revtbl", "rsidtbl", "header",
        "headerf", "headerl", "headerr", "footer", "footerf", "footerl", "footerr",
        "footnote", "ftnsep", "ftnsepc", "ftncn", "aftnsep", "aftnsepc", "aftncn",
        "annotation", "atnid", "atnauthor", "fldinst", "field", "shppict", "nonshppict",
        "panose", "falt", "bkmkstart", "bkmkend", "pgptbl", "xmlnstbl", "xmlopen",
        "mmath", "do", "company", "operator", "author", "creatim", "revtim", "printim",
        "buptim", "title", "subject", "keywords", "comment", "doccomm",
    }
)

# Control words that map to a literal character in the extracted text.
_RTF_SPECIALS = {
    "par": "\n", "sect": "\n", "page": "\n", "line": "\n", "tab": "\t",
    "emdash": "—", "endash": "–", "bullet": "•",
    "lquote": "‘", "rquote": "’", "ldblquote": "“", "rdblquote": "”",
    "emspace": " ", "enspace": " ", "nbsp": " ",
}


def rtf_to_text(rtf: str) -> str:
    """Extract plain text from an RTF document (best-effort, dependency-free).

    A small RTF tokeniser that drops control words, skips non-text destination
    groups (font/colour tables, pictures, fields, …) and decodes ``\\'hh`` and
    ``\\uN`` characters. Rich formatting is **not** preserved — the result is a
    readable text rendering of the document body, suitable for an inline preview.

    Args:
        rtf: The RTF document text.

    Returns:
        The extracted plain text.
    """
    out: list[str] = []
    high = 0  # pending UTF-16 high surrogate from a prior \\uN, or 0
    stack: list[tuple[int, bool]] = []
    ignorable = False  # inside a destination group whose text is discarded
    ucskip = 1         # unicode chars to skip after a \\uN
    curskip = 0        # remaining chars to skip for the current \\uN

    for match in _RTF_TOKEN.finditer(rtf):
        word, arg, hexcode, symbol, brace, char = match.groups()
        if brace:
            curskip = 0
            if brace == "{":
                stack.append((ucskip, ignorable))
            elif stack:
                ucskip, ignorable = stack.pop()
        elif symbol is not None:  # \\x control symbol
            curskip = 0
            if ord(symbol) in (10, 13):
                # A backslash before a line ending is \par (macOS / Cocoa RTF
                # uses it to separate lines); render it as a newline.
                if not ignorable:
                    out.append(chr(10))
            elif symbol == "~":
                out.append(" ")
            elif symbol in "{}\\":
                if not ignorable:
                    out.append(symbol)
            elif symbol == "*":
                ignorable = True
        elif word:
            curskip = 0
            if word in _RTF_DESTINATIONS:
                ignorable = True
            elif word == "uc":
                ucskip = int(arg) if arg else 1
            elif word == "u":
                code = int(arg) if arg else 0
                if code < 0:
                    code += 0x10000
                if not ignorable:
                    # Non-BMP characters arrive as a UTF-16 surrogate pair (two
                    # consecutive control words); combine them. Emitting lone
                    # surrogates would make the text impossible to UTF-8 encode.
                    if 0xD800 <= code <= 0xDBFF:
                        high = code
                    elif 0xDC00 <= code <= 0xDFFF and high:
                        out.append(
                            chr(0x10000 + (high - 0xD800) * 0x400 + (code - 0xDC00))
                        )
                        high = 0
                    elif code <= 0x10FFFF:
                        high = 0
                        out.append(chr(code))
                curskip = ucskip
            elif not ignorable and word in _RTF_SPECIALS:
                out.append(_RTF_SPECIALS[word])
        elif hexcode:
            if curskip > 0:
                curskip -= 1
            elif not ignorable:
                out.append(bytes([int(hexcode, 16)]).decode("latin-1"))
        elif char:
            if curskip > 0:
                curskip -= 1
            elif not ignorable:
                out.append(char)
    # Drop any unpaired surrogate so the result is always UTF-8 encodable.
    return _LONE_SURROGATE.sub("", "".join(out))
