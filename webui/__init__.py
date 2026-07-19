"""Generic browser-served input/output for the manuprompt runner.

:class:`WebGIO` (General Input/Output) is a domain-agnostic surface: producers
write text/bytes to named :class:`Channel` handles (streamed to a browser over
Server-Sent Events) and may register input handlers so the operator can type
back to the producer. It carries no knowledge of what is written or what input
means, and is the foundation for richer interactive web front-ends.
"""

from __future__ import annotations

from .gio import Channel, FileUpload, WebGIO
from .prompter import WebPrompter
from .server import LiveServer

__all__ = ["Channel", "FileUpload", "LiveServer", "WebGIO", "WebPrompter"]
