"""Generic live input/output HTTP server (Server-Sent Events, stdlib only).

:class:`LiveServer` is a small threaded HTTP server that streams arbitrary
text over named *channels* to connected browsers using Server-Sent Events,
and routes input typed in the browser back to producer-registered handlers.
It is deliberately domain-agnostic: it transports whatever text a producer
writes via :meth:`LiveServer.append` and delivers input verbatim to handlers
registered via :meth:`LiveServer.register_input_handler`, imposing no schema
on either direction. The channel names, what they carry, and what input does
are entirely the producer's concern.

Extra GET request handling can be added via :meth:`LiveServer.register_route`
without changing the core.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import mimetypes
import queue
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .._text import strip_ansi
from ..theme import Theme
from .page import render_artifact_page, render_page, render_session_page

# Per-channel backlog cap (characters). New subscribers receive at most this
# much history before live frames; keeps memory bounded on chatty channels.
_BACKLOG_LIMIT = 512 * 1024

# Pushed to a subscriber queue to tell its streaming loop to end.
_SENTINEL = object()

# Type of an extra route handler: receives the handler instance and the parsed
# path, returns True if it handled the request, False to fall through.
RouteHandler = Callable[["_Handler", str], bool]

# Type of a channel input handler: receives the text submitted in the browser.
InputHandler = Callable[[str], None]


class _FileRequest:
    """A pending request for the operator to upload one file.

    Attributes:
        label: Human-readable name of the files being collected.
        files: ``(filename, data)`` pairs uploaded so far; one request may
            collect several before it is finished.
        result: Single-slot queue the final ``(filename, data)`` list is
            delivered to when the operator finishes (or an empty list on skip);
            the waiting producer blocks on it.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.files: list[tuple[str, bytes]] = []
        self.result: queue.Queue[list[tuple[str, bytes]]] = queue.Queue(maxsize=1)


class _PromptRequest:
    """A pending interactive prompt awaiting the operator's answer in the browser.

    Attributes:
        prompt_id: Unique id for this prompt.
        kind: Prompt kind (``verdict`` / ``input`` / ``confirm`` / ``start_case``
            / ``review``).
        payload: JSON-serialisable data the page needs to render the prompt.
        result: Single-slot queue the operator's answer (a dict) is delivered
            to; the waiting producer blocks on it (``None`` on shutdown).
    """

    def __init__(self, prompt_id: str, kind: str, payload: dict[str, Any]) -> None:
        self.prompt_id = prompt_id
        self.kind = kind
        self.payload = payload
        self.result: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1)


class _ChannelState:
    """Backlog buffer, live subscribers and input handlers for a channel.

    Attributes:
        buffer: Recent channel text (capped at :data:`_BACKLOG_LIMIT`).
        subscribers: Live SSE subscriber queues fed by :meth:`append`.
        input_handlers: Producer callbacks invoked with browser-submitted
            input; a non-empty list marks the channel as input-capable.
    """

    def __init__(self) -> None:
        self.buffer: str = ""
        self.subscribers: list[queue.Queue[Any]] = []
        self.input_handlers: list[InputHandler] = []


class LiveServer:
    """Threaded HTTP server streaming named text channels over SSE.

    Attributes:
        title: Page title shown to the operator.
        logger: Logger for diagnostics.
    """

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 0,
        title: str = "ManuPrompt live output",
        password: str | None = None,
        theme: Theme | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialise the server (does not start it).

        Args:
            host: Interface to bind. ``0.0.0.0`` makes it reachable from the
                host when the runner is containerised with host networking.
            port: TCP port to bind; ``0`` lets the OS pick a free port.
            title: Page title shown in the browser.
            password: When set, all requests require HTTP Basic credentials
                whose password matches (username ignored). ``None``/empty leaves
                the server open.
            theme: Colour/font overrides (see :mod:`theme`) applied to every
                served page. ``None`` keeps the built-in dark terminal look.
            logger: Logger for diagnostics; a null logger is used if omitted.
        """
        self.title = title
        self.password = password or None
        self.theme = theme or Theme()
        self.logger = logger or logging.getLogger("manuprompt.webui")
        self._host = host
        self._port = port
        self._channels: dict[str, _ChannelState] = {}
        self._order: list[str] = []
        self._routes: list[tuple[str, RouteHandler]] = []
        self._file_requests: dict[str, _FileRequest] = {}
        self._file_seq = 0
        self._prompt: _PromptRequest | None = None  # single pending prompt
        self._prompt_seq = 0
        self._session: dict[str, Any] = {}  # live case/step progress for /session
        self._notices: list[dict[str, Any]] = []  # persistent error/warning banners
        self._notice_seq = 0
        self._mounts: list[tuple[str, Path]] = []  # (url prefix, served directory)
        self._downloads: dict[str, Path] = {}  # url path -> single downloadable file
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def protected(self) -> bool:
        """Return whether requests require a password."""
        return self.password is not None

    def check_password(self, provided: str) -> bool:
        """Return whether ``provided`` matches the configured password.

        Uses a constant-time comparison. Always ``True`` when no password is
        configured.

        Args:
            provided: The candidate password from a request.

        Returns:
            ``True`` when access is allowed.
        """
        if self.password is None:
            return True
        return hmac.compare_digest(provided, self.password)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> tuple[str, int]:
        """Start serving in a background thread.

        The configured port is preferred so the URL is stable across runs. If
        it is already in use, the server falls back to an OS-chosen free port
        (the same mechanism as requesting port ``0``) rather than failing, so a
        stale or parallel server never blocks a run.

        Returns:
            The ``(host, port)`` actually bound (the configured port, or the
            fallback port when it was busy / when ``0`` was requested).
        """
        handler = self._make_handler()
        httpd = self._bind(handler)
        httpd.daemon_threads = True
        # Stash a back-reference so the handler can reach this server.
        httpd.live_server = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self._host, self._port = httpd.server_address[0], httpd.server_address[1]
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="manuprompt-webui", daemon=True
        )
        self._thread.start()
        self.logger.debug("Live server bound to %s:%d", self._host, self._port)
        return self._host, self._port

    def _bind(self, handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
        """Bind the HTTP server, falling back to a free port if needed.

        Args:
            handler: The request-handler class to serve with.

        Returns:
            A bound :class:`ThreadingHTTPServer`.

        Raises:
            OSError: If even the OS-chosen fallback port cannot be bound.
        """
        try:
            return ThreadingHTTPServer((self._host, self._port), handler)
        except OSError as exc:
            if self._port == 0:
                raise  # already asking the OS for any free port; nothing to retry
            self.logger.warning(
                "Port %d unavailable (%s); falling back to an OS-chosen free port",
                self._port,
                exc,
            )
            return ThreadingHTTPServer((self._host, 0), handler)

    def stop(self) -> None:
        """Stop the server, release subscribers and unblock file requests."""
        with self._lock:
            for state in self._channels.values():
                for sub in state.subscribers:
                    sub.put(_SENTINEL)
            for request in self._file_requests.values():
                request.result.put([])  # unblock any waiting producer (no files)
            self._file_requests.clear()
            if self._prompt is not None:
                self._prompt.result.put(None)  # unblock a waiting prompter
                self._prompt = None
            self._session = {}
            self._notices = []
            self._mounts = []
            self._downloads = {}
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    # -- channel API (used by WebGIO) --------------------------------------

    def create_channel(self, name: str) -> None:
        """Register a channel so it appears even before any text is written.

        Args:
            name: Channel name (free-form; chosen by the producer).
        """
        with self._lock:
            self._ensure_locked(name)

    def append(self, name: str, text: str) -> None:
        """Append ``text`` to a channel and push it to live subscribers.

        ANSI escape sequences are stripped so the text renders cleanly in a
        browser. The channel is created on first write.

        Args:
            name: Target channel name.
            text: Text to append.
        """
        if not text:
            return
        clean = strip_ansi(text)
        with self._lock:
            state = self._ensure_locked(name)
            state.buffer = (state.buffer + clean)[-_BACKLOG_LIMIT:]
            for sub in state.subscribers:
                sub.put(clean)

    def channels_meta(self) -> list[dict[str, Any]]:
        """Return per-channel metadata in creation order.

        Returns:
            A list of ``{"name": str, "input": bool}`` dicts, where ``input``
            indicates whether the channel accepts browser input.
        """
        with self._lock:
            return [
                {"name": name, "input": bool(self._channels[name].input_handlers)}
                for name in self._order
            ]

    def register_input_handler(self, name: str, handler: InputHandler) -> None:
        """Register a handler for input submitted to channel ``name``.

        The channel is created if needed; registering a handler makes it
        input-capable (the page shows an input field for it).

        Args:
            name: Target channel name.
            handler: Callable invoked with each submitted text value.
        """
        with self._lock:
            self._ensure_locked(name).input_handlers.append(handler)

    def dispatch_input(self, name: str, text: str) -> bool:
        """Deliver ``text`` to a channel's input handlers.

        Args:
            name: Target channel name.
            text: Submitted input.

        Returns:
            ``True`` if the channel had at least one input handler (and they
            were invoked), ``False`` if the channel accepts no input.
        """
        with self._lock:
            state = self._channels.get(name)
            handlers = list(state.input_handlers) if state else []
        if not handlers:
            return False
        for handler in handlers:
            try:
                handler(text)
            except Exception:  # noqa: BLE001 - one bad handler must not break others
                self.logger.exception("Input handler for channel %r raised", name)
        return True

    def register_route(self, prefix: str, handler: RouteHandler) -> None:
        """Register an extra GET route handler (extension seam).

        Routes are tried in registration order before the built-in ones, so a
        future interactive front-end can add endpoints without editing this
        class.

        Args:
            prefix: Path prefix the handler matches (e.g. ``/verdict``).
            handler: Callable invoked as ``handler(request, path)``; returns
                ``True`` if it handled the request.
        """
        self._routes.append((prefix, handler))

    # -- file-request API (browser drag-drop uploads) ----------------------

    def open_file_request(
        self, label: str
    ) -> tuple[str, "queue.Queue[list[tuple[str, bytes]]]"]:
        """Open a pending upload request and return its id and result queue.

        The request appears on the upload page and may collect **several**
        files (each via :meth:`deliver_file`) before the operator finishes it
        (:meth:`finish_file_request`) or skips it (:meth:`skip_file`). The
        caller blocks on the returned queue for the final list.

        Args:
            label: Human-readable name of the files being collected.

        Returns:
            A ``(request_id, result_queue)`` pair. The queue yields the list of
            ``(filename, data)`` pairs collected (empty on skip/shutdown).
        """
        with self._lock:
            self._file_seq += 1
            request_id = str(self._file_seq)
            request = _FileRequest(label)
            self._file_requests[request_id] = request
            return request_id, request.result

    def close_file_request(self, request_id: str) -> None:
        """Remove a file request (e.g. once its outcome has been consumed).

        Args:
            request_id: Id returned by :meth:`open_file_request`.
        """
        with self._lock:
            self._file_requests.pop(request_id, None)

    def pending_files(self) -> list[dict[str, Any]]:
        """Return pending file requests as ``{"id","label","files"}`` dicts.

        ``files`` is the list of filenames accepted so far, so the page can show
        what has been added while the operator keeps dropping more.
        """
        with self._lock:
            return [
                {
                    "id": rid,
                    "label": req.label,
                    "files": [name for name, _ in req.files],
                }
                for rid, req in self._file_requests.items()
            ]

    def deliver_file(self, request_id: str, filename: str, data: bytes) -> bool:
        """Add one uploaded file to a pending request (without finishing it).

        Args:
            request_id: Target request id.
            filename: Original filename (used for its extension).
            data: File contents.

        Returns:
            ``True`` if the request existed and the file was accepted.
        """
        with self._lock:
            request = self._file_requests.get(request_id)
            if request is None:
                return False
            request.files.append((filename, data))
            return True

    def finish_file_request(self, request_id: str) -> bool:
        """Finish a request, delivering every file collected so far.

        Args:
            request_id: Target request id.

        Returns:
            ``True`` if the request existed and was finished.
        """
        with self._lock:
            request = self._file_requests.pop(request_id, None)
            if request is None:
                return False
            request.result.put(list(request.files))
            return True

    def skip_file(self, request_id: str) -> bool:
        """Skip a pending request, discarding any files collected so far.

        Args:
            request_id: Target request id.

        Returns:
            ``True`` if the request existed and was skipped.
        """
        with self._lock:
            request = self._file_requests.pop(request_id, None)
            if request is None:
                return False
            request.result.put([])
            return True

    # -- prompt-request API (interactive web prompter) ---------------------

    def open_prompt(
        self, kind: str, payload: dict[str, Any]
    ) -> tuple[str, "queue.Queue[dict[str, Any] | None]"]:
        """Register a pending interactive prompt and return its id and queue.

        The engine is sequential, so at most one prompt is pending at a time; a
        new prompt replaces (and unblocks) any stale one. The caller blocks on
        the returned queue for the operator's answer.

        Args:
            kind: Prompt kind (see :class:`_PromptRequest`).
            payload: Data the page needs to render the prompt.

        Returns:
            A ``(prompt_id, result_queue)`` pair; the queue yields the answer
            dict, or ``None`` on shutdown.
        """
        with self._lock:
            self._prompt_seq += 1
            prompt_id = str(self._prompt_seq)
            if self._prompt is not None:
                self._prompt.result.put(None)  # abandon any stale prompt
            self._prompt = _PromptRequest(prompt_id, kind, payload)
            return prompt_id, self._prompt.result

    def close_prompt(self, prompt_id: str) -> None:
        """Clear the pending prompt if it is still ``prompt_id``."""
        with self._lock:
            if self._prompt is not None and self._prompt.prompt_id == prompt_id:
                self._prompt = None

    def pending_prompt(self) -> dict[str, Any] | None:
        """Return the current prompt as ``{"id","kind", **payload}``, or ``None``."""
        with self._lock:
            if self._prompt is None:
                return None
            return {"id": self._prompt.prompt_id, "kind": self._prompt.kind,
                    **self._prompt.payload}

    def answer_prompt(self, prompt_id: str, answer: dict[str, Any]) -> bool:
        """Deliver the operator's answer to the pending prompt.

        Args:
            prompt_id: The prompt being answered.
            answer: The operator's answer dict.

        Returns:
            ``True`` if it matched the pending prompt (and was delivered).
        """
        with self._lock:
            if self._prompt is None or self._prompt.prompt_id != prompt_id:
                return False
            self._prompt.result.put(answer)
            self._prompt = None
            return True

    def set_session(self, state: dict[str, Any] | None) -> None:
        """Publish (or clear) the live case/step progress for the session page.

        The producer (the engine) pushes an opaque snapshot describing the
        current test case and its ordered steps; the session page polls it via
        ``GET /session/state`` and renders the always-visible step list, moving
        the active-step highlight as the snapshot changes.

        Args:
            state: The progress snapshot, or ``None``/empty to clear it (e.g.
                between cases).
        """
        with self._lock:
            self._session = dict(state) if state else {}

    def session_state(self) -> dict[str, Any]:
        """Return the current session progress snapshot (``{}`` when none)."""
        with self._lock:
            return dict(self._session)

    def add_notice(self, message: str, *, level: str = "error") -> int:
        """Publish a persistent banner notice shown on every served page.

        Unlike channel output (which lives inside a specific tab the operator
        may not be looking at), notices render as a banner at the top of every
        page — so a failure the operator would otherwise only see in the
        CLI/log (e.g. an unhandled exception during suite setup, before any
        test case's live step list exists) is still visible in the browser.
        Notices accumulate (an operator may dismiss one in their own browser,
        client-side) until :meth:`stop`.

        Args:
            message: Human-readable text shown in the banner.
            level: Visual severity, ``"error"`` or ``"warning"``; only affects
                styling.

        Returns:
            The notice's id.
        """
        with self._lock:
            self._notice_seq += 1
            notice_id = self._notice_seq
            self._notices.append(
                {"id": notice_id, "level": level, "message": message}
            )
            return notice_id

    def notices(self) -> list[dict[str, Any]]:
        """Return every notice published so far (see :meth:`add_notice`)."""
        with self._lock:
            return list(self._notices)

    def mount_dir(self, prefix: str, directory: Path) -> None:
        """Serve the files under ``directory`` at URL ``prefix``.

        A generic static-file mount (used to serve a rendered report bundle and
        its assets). Requests whose path starts with ``prefix`` are served from
        ``directory``, resolved with path-traversal protection; ``prefix``
        itself serves ``index.html`` when present. Longer prefixes are matched
        first, so nested mounts behave predictably.

        Args:
            prefix: URL path prefix (e.g. ``/report``).
            directory: Directory whose contents are served.
        """
        with self._lock:
            self._mounts = [m for m in self._mounts if m[0] != prefix]
            self._mounts.append((prefix, Path(directory).resolve()))
            self._mounts.sort(key=lambda m: len(m[0]), reverse=True)

    def add_download(self, url_path: str, file_path: Path) -> None:
        """Serve a single file as an attachment download at ``url_path``.

        Args:
            url_path: Exact URL path (e.g. ``/report.zip``).
            file_path: File served with a ``Content-Disposition: attachment``
                header so the browser downloads (rather than renders) it.
        """
        with self._lock:
            self._downloads[url_path] = Path(file_path).resolve()

    def _lookup_mount(self, path: str) -> tuple[str, Path] | None:
        """Return the ``(prefix, directory)`` mount serving ``path``, if any."""
        with self._lock:
            for prefix, directory in self._mounts:
                if path == prefix or path.startswith(prefix + "/"):
                    return prefix, directory
        return None

    def _lookup_download(self, path: str) -> Path | None:
        """Return the file registered for download at ``path``, if any."""
        with self._lock:
            return self._downloads.get(path)

    # -- internals ---------------------------------------------------------

    def _ensure_locked(self, name: str) -> _ChannelState:
        """Return the state for ``name``, creating it. Caller holds the lock."""
        state = self._channels.get(name)
        if state is None:
            state = _ChannelState()
            self._channels[name] = state
            self._order.append(name)
        return state

    def _subscribe(self, name: str) -> tuple[str, queue.Queue[Any]]:
        """Register a subscriber and return the channel backlog + its queue."""
        sub: queue.Queue[Any] = queue.Queue()
        with self._lock:
            state = self._ensure_locked(name)
            backlog = state.buffer
            state.subscribers.append(sub)
        return backlog, sub

    def _unsubscribe(self, name: str, sub: queue.Queue[Any]) -> None:
        """Remove a subscriber queue from a channel."""
        with self._lock:
            state = self._channels.get(name)
            if state is not None and sub in state.subscribers:
                state.subscribers.remove(sub)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        """Build the request-handler class bound to this server."""
        return _Handler


def _sse_frame(text: str) -> bytes:
    """Encode ``text`` as a single SSE ``data:`` event preserving newlines.

    Line endings are normalised to ``\\n`` first: a raw ``\\r`` left inside a
    ``data:`` field is parsed by the browser's SSE reader as a field-line
    terminator, which corrupts or drops content (e.g. ``\\r\\n``-terminated
    device output collapses onto one line). After normalisation each source
    line becomes its own ``data:`` field; the browser rejoins them with
    newlines, so the appended text matches the source.

    Args:
        text: Text to encode.

    Returns:
        The encoded SSE frame as UTF-8 bytes.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    body = "".join(f"data: {line}\n" for line in text.split("\n"))
    return (body + "\n").encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    """HTTP request handler for :class:`LiveServer`.

    Reaches the owning server via the ``live_server`` attribute stashed on the
    ``ThreadingHTTPServer`` instance.
    """

    protocol_version = "HTTP/1.1"

    @property
    def _live(self) -> LiveServer:
        """Return the owning :class:`LiveServer`."""
        return self.server.live_server  # type: ignore[attr-defined]

    def log_message(  # noqa: A002  pylint: disable=redefined-builtin
        self, format: str, *args: Any
    ) -> None:
        """Route stdlib access logs to the server's logger at debug level."""
        self._live.logger.debug("webui %s", format % args)

    def _authorized(self) -> bool:
        """Enforce HTTP Basic auth when the server has a password.

        Validates the password from an ``Authorization: Basic`` header (the
        username is ignored). On failure, sends a ``401`` with a
        ``WWW-Authenticate`` challenge so the browser prompts, and returns
        ``False`` so the caller stops handling the request.

        Returns:
            ``True`` when the request may proceed.
        """
        if not self._live.protected:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode(
                    "utf-8", "replace"
                )
            except (binascii.Error, ValueError):
                decoded = ""
            _user, _, provided = decoded.partition(":")
            if self._live.check_password(provided):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ManuPrompt", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        """Dispatch a GET request to a route or a built-in endpoint."""
        if not self._authorized():
            return
        path = urlsplit(self.path).path
        for prefix, handler in self._live._routes:  # pylint: disable=protected-access
            if path.startswith(prefix) and handler(self, path):
                return
        if path == "/" or path == "/index.html":
            self._serve_page()
        elif path == "/session":
            self._serve_session_page()
        elif path == "/channels":
            self._serve_channels()
        elif path == "/artifact":
            self._serve_artifact_page()
        elif path == "/artifacts/pending":
            self._serve_pending_files()
        elif path == "/prompts/pending":
            self._serve_json(self._live.pending_prompt() or {})
        elif path == "/session/state":
            self._serve_json(self._live.session_state())
        elif path == "/notices":
            self._serve_json(self._live.notices())
        elif path.startswith("/stream/"):
            self._serve_stream(unquote(path[len("/stream/"):]))
        elif self._live._lookup_download(path) is not None:  # pylint: disable=protected-access
            self._serve_download(self._live._lookup_download(path))  # pylint: disable=protected-access
        elif self._live._lookup_mount(path) is not None:  # pylint: disable=protected-access
            self._serve_mounted(path)
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        """Dispatch a POST request (browser input, file upload or prompt answer)."""
        if not self._authorized():
            return
        path = urlsplit(self.path).path
        if path.startswith("/input/"):
            self._handle_input(unquote(path[len("/input/"):]))
        elif path.startswith("/prompt/"):
            self._handle_prompt_answer(path[len("/prompt/"):])
        elif path.startswith("/artifact/") and path.endswith("/done"):
            self._handle_file_action(path[len("/artifact/"):-len("/done")], "done")
        elif path.startswith("/artifact/") and path.endswith("/skip"):
            self._handle_file_action(path[len("/artifact/"):-len("/skip")], "skip")
        elif path.startswith("/artifact/"):
            self._handle_upload(path[len("/artifact/"):])
        else:
            self.send_error(404)

    def _handle_input(self, name: str) -> None:
        """Deliver a posted body to a channel's input handlers.

        Args:
            name: Channel name taken from the request path.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        if self._live.dispatch_input(name, body):
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404, "channel accepts no input")

    def _handle_prompt_answer(self, prompt_id: str) -> None:
        """Deliver a JSON answer body to the pending interactive prompt.

        Args:
            prompt_id: The prompt id taken from the request path.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            answer = json.loads(raw.decode("utf-8", errors="replace")) or {}
        except (ValueError, TypeError):
            answer = {}
        if isinstance(answer, dict) and self._live.answer_prompt(prompt_id, answer):
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(409, "no matching pending prompt")

    def _handle_upload(self, request_id: str) -> None:
        """Add one uploaded file body to a pending file request.

        The request stays open so more files can follow; the original filename
        is taken from the ``X-Filename`` header (used for its extension) and the
        body is the raw file content.

        Args:
            request_id: File-request id taken from the request path.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        data = self.rfile.read(length) if length else b""
        filename = unquote(self.headers.get("X-Filename", "") or "")
        if self._live.deliver_file(request_id, filename, data):
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404, "no such file request")

    def _handle_file_action(self, request_id: str, action: str) -> None:
        """Finish (``done``) or skip a pending file request.

        Args:
            request_id: File-request id taken from the request path.
            action: ``"done"`` to attach the collected files, ``"skip"`` to
                attach none.
        """
        resolve = self._live.finish_file_request if action == "done" else self._live.skip_file
        if resolve(request_id):
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404, "no such file request")

    def _serve_page(self) -> None:
        """Serve the self-contained HTML page."""
        self._serve_html(render_page(self._live.title, self._live.theme))

    def _serve_session_page(self) -> None:
        """Serve the interactive session page (prompts + console + artifacts)."""
        self._serve_html(render_session_page(self._live.title, self._live.theme))

    def _serve_html(self, html: str) -> None:
        """Serve an HTML document."""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_download(self, file_path: Path) -> None:
        """Serve a single file as an attachment download."""
        self._send_file(file_path, download=True)

    def _serve_mounted(self, path: str) -> None:
        """Serve a static file from the directory mounted for ``path``."""
        found = self._live._lookup_mount(path)  # pylint: disable=protected-access
        if found is None:
            self.send_error(404)
            return
        prefix, directory = found
        rel = path[len(prefix):].lstrip("/")
        target = (directory / rel).resolve() if rel else directory
        # Path-traversal guard: the resolved target must stay inside the mount.
        if target != directory and not target.is_relative_to(directory):
            self.send_error(403)
            return
        if target.is_dir():
            index = target / "index.html"
            report = target / "report.html"
            target = index if index.is_file() else report
        if not target.is_file():
            self.send_error(404)
            return
        self._send_file(target, download=False)

    def _send_file(self, target: Path, *, download: bool) -> None:
        """Send ``target``'s bytes with a guessed content type.

        Args:
            target: File to send.
            download: When ``True``, add a ``Content-Disposition: attachment``
                header so the browser downloads rather than renders it.
        """
        try:
            data = target.read_bytes()
        except OSError:
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if download:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{target.name}"'
            )
        self.end_headers()
        self.wfile.write(data)

    def _serve_channels(self) -> None:
        """Serve per-channel metadata as JSON."""
        self._serve_json(self._live.channels_meta())

    def _serve_artifact_page(self) -> None:
        """Serve the drag-drop artifact-upload page."""
        body = render_artifact_page(self._live.title, self._live.theme).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_pending_files(self) -> None:
        """Serve the pending file requests as JSON."""
        self._serve_json(self._live.pending_files())

    def _serve_json(self, payload: Any) -> None:
        """Serve ``payload`` as a no-store JSON response.

        Args:
            payload: A JSON-serialisable value.
        """
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self, name: str) -> None:
        """Stream a channel's backlog then live updates via SSE.

        Args:
            name: Channel name taken from the request path.
        """
        backlog, sub = self._live._subscribe(name)  # pylint: disable=protected-access
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            if backlog:
                self.wfile.write(_sse_frame(backlog))
                self.wfile.flush()
            while True:
                item = sub.get()
                if item is _SENTINEL:
                    break
                self.wfile.write(_sse_frame(item))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away; clean up below
        finally:
            self._live._unsubscribe(name, sub)  # pylint: disable=protected-access
