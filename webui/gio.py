"""WebGIO — a generic, domain-agnostic browser input/output surface.

:class:`WebGIO` (General Input/Output) wraps a
:class:`~webui.server.LiveServer` and hands out :class:`Channel` handles. A
producer obtains a channel by name and:

* **outputs** text/bytes to it (``write`` / ``feed``), streamed live to the
  browser, and
* optionally **accepts input** from the browser by registering a handler
  (``on_input``) — e.g. a command typed by the operator.

WebGIO knows nothing about what the text means or what input does; it only
transports bytes and routes typed input back to the producer's handler. This
is the library-side contract producers align to (a device console, a build
log, a REPL, …) — WebGIO never reaches into the producer itself.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..theme import Theme
from .server import LiveServer


@dataclass(frozen=True)
class FileUpload:
    """A file uploaded by the operator from the browser.

    Attributes:
        name: Original filename as reported by the browser (may be empty).
        data: Raw file contents.
    """

    name: str
    data: bytes


class Channel:
    """A bidirectional handle for one named I/O channel.

    Output is written with :meth:`write` / :meth:`feed`; browser input is
    received by registering a handler with :meth:`on_input`.

    Attributes:
        name: The channel name.
    """

    def __init__(self, server: LiveServer, name: str) -> None:
        """Bind the channel to its server.

        Args:
            server: The backing live server.
            name: Channel name.
        """
        self.name = name
        self._server = server

    def write(self, text: str) -> None:
        """Append text to the channel's output stream.

        Args:
            text: Text to stream to connected browsers.
        """
        self._server.append(self.name, text)

    def feed(self, data: bytes) -> None:
        """Append raw bytes to the channel, decoded as UTF-8.

        Undecodable bytes are replaced rather than raising, so a noisy or
        binary stream never breaks the producer.

        Args:
            data: Raw bytes to stream.
        """
        if data:
            self.write(data.decode("utf-8", errors="replace"))

    def on_input(self, handler: Callable[[str], None]) -> None:
        """Register a handler for input submitted from the browser.

        Registering a handler makes the channel show an input field in the
        page. Each submission calls ``handler(text)``; handlers run on the
        server's request thread and should return promptly. Multiple handlers
        may be registered and are all invoked.

        Args:
            handler: Callable invoked with the submitted text.
        """
        self._server.register_input_handler(self.name, handler)


class WebGIO:
    """A browser-served I/O surface hosting one or more named channels.

    Attributes:
        logger: Logger for diagnostics.
    """

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 0,
        link_host: str | None = None,
        title: str = "ManuPrompt live console",
        password: str | None = None,
        theme: Theme | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialise the surface (does not start the server).

        Args:
            host: Interface to bind (``0.0.0.0`` to be reachable from the host
                under container host-networking).
            port: TCP port; ``0`` lets the OS choose a free one.
            link_host: Hostname used in the printed URL. Defaults to
                ``localhost``; set this when the browser reaches the server via
                a different address than the bind interface.
            title: Page title shown in the browser.
            password: When set, every page/stream/upload request requires HTTP
                Basic credentials whose password matches (the username is
                ignored). ``None`` or empty leaves the surface open.
            theme: Colour/font overrides (see :mod:`theme`) applied to every
                page this surface serves. ``None`` keeps the built-in dark
                terminal look.
            logger: Logger for diagnostics.
        """
        self.logger = logger or logging.getLogger("manuprompt.webui")
        self._link_host = link_host or "localhost"
        self._server = LiveServer(
            host=host,
            port=port,
            title=title,
            password=password,
            theme=theme,
            logger=self.logger,
        )
        self._channels: dict[str, Channel] = {}
        self._url: str | None = None

    @property
    def protected(self) -> bool:
        """Return whether the surface requires a password."""
        return self._server.protected

    def start(self) -> str:
        """Start the server and return the operator-facing URL.

        Returns:
            The URL to open in a browser.
        """
        _host, port = self._server.start()
        self._url = f"http://{self._link_host}:{port}/"
        return self._url

    @property
    def url(self) -> str:
        """Return the surface URL.

        Raises:
            RuntimeError: If accessed before :meth:`start`.
        """
        if self._url is None:
            raise RuntimeError("WebGIO.start() has not been called")
        return self._url

    @property
    def title(self) -> str:
        """Return the surface's display name."""
        return self._server.title

    @property
    def artifact_url(self) -> str:
        """Return the URL of the drag-drop artifact-upload page.

        Raises:
            RuntimeError: If accessed before :meth:`start`.
        """
        return self.url + "artifact"

    @property
    def session_url(self) -> str:
        """Return the URL of the interactive session page.

        This is the primary page in web-prompt mode: it shows the current
        operator prompt, an embedded live console, and links to the standalone
        console and artifact pages.

        Raises:
            RuntimeError: If accessed before :meth:`start`.
        """
        return self.url + "session"

    def request_files(self, label: str) -> list[FileUpload]:
        """Ask the operator to upload file(s) in the browser, and wait.

        Opens a pending request shown on the artifact page (``/artifact``) and
        blocks until the operator finishes it — having dropped **any number** of
        files — or skips it (or the surface is stopped). This is the drag-drop
        alternative to typing a filesystem path.

        Args:
            label: Human-readable name of the files being collected.

        Returns:
            The uploaded :class:`FileUpload` objects in upload order; empty if
            the operator skipped or the surface was stopped while waiting.
        """
        request_id, result = self._server.open_file_request(label)
        try:
            outcome = result.get()
        finally:
            self._server.close_file_request(request_id)
        return [FileUpload(name=name, data=data) for name, data in outcome]

    def ask(self, kind: str, payload: dict) -> dict | None:
        """Show an interactive prompt in the browser and block for the answer.

        Registers a pending prompt (rendered on the session page) and waits
        until the operator answers it there, or the surface is stopped. This is
        the blocking primitive the :class:`~webui.prompter.WebPrompter` builds
        on to run operator prompts in the browser.

        Args:
            kind: Prompt kind (``verdict`` / ``input`` / ``confirm`` /
                ``start_case`` / ``review``).
            payload: JSON-serialisable data the page needs to render the prompt.

        Returns:
            The operator's answer dict, or ``None`` if the surface was stopped
            while waiting.
        """
        prompt_id, result = self._server.open_prompt(kind, payload)
        try:
            return result.get()
        finally:
            self._server.close_prompt(prompt_id)

    def notify(self, message: str, *, level: str = "error") -> None:
        """Publish a persistent banner notice shown on every served page.

        Use this for failures the operator should see in the browser even
        though they aren't tied to a specific channel or the current test
        case's live step list — e.g. an unhandled exception during suite
        setup/teardown (see :meth:`~webui.server.LiveServer.add_notice`).

        Args:
            message: Human-readable text shown in the banner.
            level: Visual severity, ``"error"`` or ``"warning"``.
        """
        self._server.add_notice(message, level=level)

    def set_session(self, state: dict | None) -> None:
        """Publish (or clear) the live case/step progress for the session page.

        Args:
            state: An opaque snapshot of the current case and its steps (see
                :meth:`LiveServer.set_session`), or ``None``/empty to clear it.
        """
        self._server.set_session(state)

    def mount_dir(self, prefix: str, directory: Path) -> None:
        """Serve a static directory's files under ``prefix`` (e.g. a report bundle).

        Args:
            prefix: URL path prefix (e.g. ``/report``).
            directory: Directory whose contents are served.
        """
        self._server.mount_dir(prefix, directory)

    def add_download(self, url_path: str, file_path: Path) -> None:
        """Serve a single file as a browser download at ``url_path``.

        Args:
            url_path: Exact URL path (e.g. ``/report.zip``).
            file_path: File served as a ``Content-Disposition: attachment``.
        """
        self._server.add_download(url_path, file_path)

    def channel(self, name: str) -> Channel:
        """Return the channel named ``name``, creating it on first use.

        Args:
            name: Channel name chosen by the producer.

        Returns:
            A :class:`Channel` handle for that name.
        """
        channel = self._channels.get(name)
        if channel is None:
            self._server.create_channel(name)
            channel = Channel(self._server, name)
            self._channels[name] = channel
        return channel

    def stop(self) -> None:
        """Stop the server and disconnect all browsers."""
        self._server.stop()
