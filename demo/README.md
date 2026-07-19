# ManuPrompt demo

A tour of ManuPrompt driving a **real browser** with
[Playwright](https://playwright.dev/) against a public UI-testing playground
page — [uitestingplayground.com/select](http://uitestingplayground.com/select)
— so this demo needs network access and a downloaded browser binary, but no
project of your own. Copy this whole checkout elsewhere and it still runs, as
long as those two things are available (see "Requirements" below).

## What it shows

| File | Role |
|------|------|
| `demo-suite.yml` | The suite: setup/teardown, two test cases mixing `call:` and `prompt:` steps. |
| `browser.py` | Playwright glue driven via `call: browser.*` — launches Chromium (optional session video + live page-console stream persisted as an artifact), drives the page, returns screenshot/video/console-log paths. No Selenium, no project dependency. |

The suite exercises: launching a browser and opening a page
(`call: browser.launch` with `record_video: true` and `stream_console: true`),
selecting a `<select>` option by its visible text (`select_by_text`) and
separately by its `value` attribute (`select_by_value`), an automated
assertion on the page's own status label (`expect_text`, a `call:` step that
fails the step when the check doesn't hold), screenshot steps with explicit
`artifact:` labels (glue returns the PNG path; the engine attaches it), an
operator `prompt:` verdict, and suite teardown that attaches the captured
page console log (`browser.stop_console` + `artifact:`) and stops the
Playwright recording (`browser.stop_video` + `artifact:`) so both appear in
the report. With `stream_console`, page `console.*` messages and uncaught JS
exceptions are written to `browser-console.log` and also appear live on the
WebGIO **browser console** tab.

## Requirements

- Python 3.12+
- PyYAML — the core package's only dependency: `pip install pyyaml`
- Playwright, plus its Chromium browser binary:

  ```bash
  pip install -r manuprompt/demo/requirements.txt
  playwright install chromium
  ```

- Network access to `uitestingplayground.com` (the page this demo drives).

## Run it

**Browser mode (default)** — prompts are answered in a web page; at the end
you can view and download a self-contained report `.zip`:

```bash
# from the parent directory of this checkout (named `manuprompt`)
cd ..
python -m manuprompt manuprompt/demo/demo-suite.yml
```

Open the printed session link. Answer each step inline (verdict keys: `P`/`F`/
`Enter`), then click **Stop & save report** (or proceed through the last case)
to open/download the report — each screenshot step shows its image inline.

**Terminal mode** — answer prompts on the console instead:

```bash
python -m manuprompt --cli-mode manuprompt/demo/demo-suite.yml
```

By default Chromium runs headless; set `headless: false` on the
`browser.launch` step in `demo-suite.yml` to watch it drive the page instead.

## Run it standalone (copied elsewhere)

Copy the whole checkout and run it from its parent directory, same as above:

```bash
cp -r manuprompt /somewhere/manuprompt
cd /somewhere
pip install pyyaml
pip install -r manuprompt/demo/requirements.txt
playwright install chromium
python -m manuprompt manuprompt/demo/demo-suite.yml
```

## Use it as a template

Copy `demo/` to a new directory, point `call:` at your own Playwright glue
module (or another browser-automation library) and target page, and edit
`demo-suite.yml`. See `../SPECIFICATION.md` for the full suite schema, the
`call` seam (§9 / project glue), and the web surface.
