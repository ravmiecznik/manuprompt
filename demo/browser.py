"""Playwright glue for the manuprompt web-UI demo.

This module stands in for the project-specific browser-automation glue that a
real suite would provide (see ``SPECIFICATION.md`` §9 and the ``call`` step).
It drives a real Chromium browser via Playwright against a public page
(http://uitestingplayground.com/select) — the only third-party dependency
this demo has (see ``demo/README.md`` for install steps).

The demo suite invokes these functions with ``call: browser.<function>``. Each
receives the run context (``ctx``) and uses only its public, domain-agnostic
API: ``ctx.resources`` (run-scoped object store), ``ctx.add_teardown``
(cleanup), and optionally ``ctx.web_gio`` (live console stream). Screenshots,
the session video, and the captured browser console log are returned as file
paths; the suite YAML's ``artifact:`` label is what attaches them to the step
in the report.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

# Keys in ctx.resources for the live Playwright objects.
_PAGE_KEY = "playwright_page"
_CONTEXT_KEY = "playwright_context"
_VIDEO_KEY = "playwright_video"
_CONSOLE_PATH_KEY = "playwright_console_path"
_CONSOLE_FILE_KEY = "playwright_console_file"

# WebGIO channel / artifact stem for page console / pageerror capture when
# ``stream_console`` is enabled on :func:`launch`.
_CONSOLE_CHANNEL = "browser console"
_CONSOLE_LOG_NAME = "browser-console.log"

# Default capture size when recording. Playwright otherwise scales the
# viewport down to fit 800x800, which makes on-page text hard to read.
_DEFAULT_VIDEO_SIZE = {"width": 1920, "height": 1080}


def launch(
    ctx,
    url: str,
    headless: bool = True,
    record_video: bool = False,
    video_size: dict | None = None,
    stream_console: bool = True,
) -> None:
    """Start Chromium, open ``url``, and cache the page for later steps.

    When ``record_video`` is true, Playwright records the browser context to a
    ``.webm`` under the run's artifacts directory. Call :func:`stop_video` in
    suite teardown (with an ``artifact:`` label) to finalize and attach it.

    Recording uses a fixed viewport and matching ``record_video_size`` (default
    1920×1080) so the video is not downscaled to Playwright's 800×800 default.

    When ``stream_console`` is true, page ``console.*`` messages and uncaught
    JS exceptions are written to ``browser-console.log`` under the run's
    artifacts directory. Call :func:`stop_console` in suite teardown (with an
    ``artifact:`` label) to attach that file. The same lines are also streamed
    live to a WebGIO channel named ``browser console`` when ``ctx.web_gio`` is
    available.

    Registers teardown to close the context/browser and stop Playwright when
    the run ends (including on error or operator stop).

    Args:
        ctx: The run context.
        url: The page to open.
        headless: Run without a visible window (set ``false`` in the suite
            YAML to watch Chromium drive the page instead).
        record_video: Enable Playwright context video recording.
        video_size: Optional ``{width, height}`` for viewport and video
            resolution when recording (defaults to 1920×1080).
        stream_console: Capture page console / pageerror events to a log file
            (and stream them to WebGIO when the web surface is enabled).
    """
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=headless)

    context_kwargs: dict = {}
    size = dict(video_size) if video_size else dict(_DEFAULT_VIDEO_SIZE)
    if record_video:
        video_dir = Path(ctx.artifacts_dir) / "playwright-video"
        video_dir.mkdir(parents=True, exist_ok=True)
        context_kwargs["record_video_dir"] = str(video_dir)
        # Match viewport and video size — Playwright otherwise scales the
        # viewport down to fit 800×800, which blurs on-page text.
        context_kwargs["viewport"] = size
        context_kwargs["record_video_size"] = size
    elif video_size:
        context_kwargs["viewport"] = size

    context = browser.new_context(**context_kwargs)
    page = context.new_page()
    if stream_console:
        _attach_console_stream(ctx, page)
    page.goto(url)

    ctx.resources[_PAGE_KEY] = page
    ctx.resources[_CONTEXT_KEY] = context
    if record_video:
        if page.video is None:
            raise RuntimeError("Playwright did not start video recording")
        ctx.resources[_VIDEO_KEY] = page.video

    # Teardowns run in reverse registration order: context → browser → stop.
    ctx.add_teardown(playwright.stop)
    ctx.add_teardown(browser.close)
    ctx.add_teardown(lambda: _close_context(ctx))
    ctx.logger.info(
        "Opened %s (headless=%s, record_video=%s, stream_console=%s%s)",
        url,
        headless,
        record_video,
        stream_console,
        f", size={size['width']}x{size['height']}" if record_video else "",
    )


def _attach_console_stream(ctx, page) -> None:
    """Capture Playwright console / pageerror events to a file and WebGIO.

    Always writes ``browser-console.log`` under the run's artifacts directory.
    When ``ctx.web_gio`` is available, the same lines are streamed to a live
    channel. Handlers may run on Playwright's own threads; writes are
    lock-guarded.

    Args:
        ctx: The run context.
        page: The Playwright page to listen on.
    """
    log_path = Path(ctx.artifacts_dir) / _CONSOLE_LOG_NAME
    log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
    ctx.resources[_CONSOLE_PATH_KEY] = log_path
    ctx.resources[_CONSOLE_FILE_KEY] = log_file
    ctx.add_teardown(lambda: _close_console_file(ctx))

    channel = ctx.web_gio.channel(_CONSOLE_CHANNEL) if ctx.web_gio is not None else None
    lock = threading.Lock()

    def _emit(line: str) -> None:
        with lock:
            log_file.write(line)
            log_file.flush()
            if channel is not None:
                channel.write(line)

    def _on_console(msg) -> None:
        loc = msg.location
        where = ""
        if loc and loc.get("url"):
            line = loc.get("lineNumber")
            where = f" ({loc['url']}"
            if line is not None:
                where += f":{line}"
            where += ")"
        _emit(f"[{msg.type}] {msg.text}{where}\n")

    def _on_pageerror(err) -> None:
        _emit(f"[pageerror] {err}\n")

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    _emit("(listening for page console / pageerror)\n")


def stop_console(ctx) -> str:
    """Finalize the captured console log and return its path.

    Flushes and closes the log file started by :func:`launch` with
    ``stream_console: true``. The suite should declare ``artifact:`` on this
    step so the returned file is attached to the report.

    Args:
        ctx: The run context.

    Returns:
        Path to the captured ``browser-console.log`` file.

    Raises:
        RuntimeError: If :func:`launch` was not called with ``stream_console``.
    """
    path = ctx.resources.get(_CONSOLE_PATH_KEY)
    if path is None:
        raise RuntimeError(
            "No console capture; call browser.launch with stream_console: true first"
        )
    _close_console_file(ctx)
    ctx.logger.info("Stopped console capture -> %s", path)
    return str(path)


def stop_video(ctx) -> str:
    """Finalize the Playwright recording and return the ``.webm`` path.

    Closes the browser context (required for Playwright to flush the video).
    The suite should declare ``artifact:`` on this step so the returned file is
    attached to the report.

    Args:
        ctx: The run context.

    Returns:
        Path to the recorded ``.webm`` file.

    Raises:
        RuntimeError: If :func:`launch` was not called with ``record_video``.
    """
    video = ctx.resources.get(_VIDEO_KEY)
    if video is None:
        raise RuntimeError(
            "No video recording; call browser.launch with record_video: true first"
        )
    # Path is known while the page/context is still open; the file is complete
    # only after the context closes.
    path = video.path()
    _close_context(ctx)
    ctx.resources.pop(_VIDEO_KEY, None)
    ctx.logger.info("Stopped video recording -> %s", path)
    return str(path)


def _close_console_file(ctx) -> None:
    """Close the console log file handle if still open."""
    handle = ctx.resources.pop(_CONSOLE_FILE_KEY, None)
    if handle is not None and not handle.closed:
        handle.close()


def _close_context(ctx) -> None:
    """Close the Playwright context (and page) if still open."""
    ctx.resources.pop(_PAGE_KEY, None)
    context = ctx.resources.pop(_CONTEXT_KEY, None)
    if context is not None:
        context.close()


def _page(ctx):
    """Return the cached Playwright page, or raise if ``launch`` wasn't run."""
    page = ctx.resources.get(_PAGE_KEY)
    if page is None:
        raise RuntimeError("No open browser page; run 'call: browser.launch' first")
    return page


def select_by_text(ctx, selector: str, text: str) -> None:
    """Choose a ``<select>`` option by its visible label text.

    Args:
        ctx: The run context.
        selector: CSS selector for the ``<select>`` element.
        text: The option's visible text (not its ``value`` attribute).
    """
    _page(ctx).select_option(selector, label=text)


def select_by_value(ctx, selector: str, value: str) -> None:
    """Choose a ``<select>`` option by its ``value`` attribute.

    Args:
        ctx: The run context.
        selector: CSS selector for the ``<select>`` element.
        value: The option's ``value`` attribute.
    """
    _page(ctx).select_option(selector, value=value)


def expect_text(ctx, selector: str, expected: str) -> bool:
    """Assert an element's text content equals ``expected``.

    Used as an automated ``call:`` check (returning ``False`` fails the step,
    matching the ``call`` seam's PASS/FAIL convention).

    Args:
        ctx: The run context.
        selector: CSS selector for the element to read.
        expected: The exact text expected.

    Returns:
        ``True`` if the element's text matches, ``False`` otherwise.
    """
    actual = _page(ctx).inner_text(selector).strip()
    ctx.logger.info("expect_text(%s): got %r, want %r", selector, actual, expected)
    return actual == expected.strip()


def screenshot(ctx) -> str:
    """Capture a full-page screenshot and return its filesystem path.

    The suite YAML should declare ``artifact: <label>`` on this step so the
    engine attaches the returned file under that label (inline in the report).
    Glue only produces the file; naming the artifact stays in the suite.

    Args:
        ctx: The run context.

    Returns:
        Path to a PNG file the engine will copy into step artifacts.
    """
    fd, tmp = tempfile.mkstemp(prefix="playwright_", suffix=".png")
    os.close(fd)
    _page(ctx).screenshot(path=tmp, full_page=True)
    return tmp
