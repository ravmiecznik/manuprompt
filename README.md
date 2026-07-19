<p align="center">
  <img src="manuprompt.png" alt="ManuPrompt logo" width="420">
</p>

# ManuPrompt

**ManuPrompt** is a generic, YAML-driven, **human-in-the-loop** test
runner. A test suite is described declaratively in YAML (setup/teardown, test
cases, ordered steps, prompts). The engine drives the suite, prompting the
operator for manual steps and automating the rest (running tools/shell
commands, calling project glue code), and produces a machine-readable JSON
result plus self-contained HTML and Markdown reports.

It is **not** specific to any product or domain. Project-specific behaviour
plugs in through a single `call:` glue seam — the core package has no
hardware or third-party dependency and runs standalone.

## Why

- **Suite authors write YAML, not Python.** The common case (prompt the
  operator, run a tool, record a verdict) needs no code.
- **Core stays domain-agnostic.** Project-specific logic plugs in via the
  `call` seam only.
- **Extensible without touching the engine.** New step kinds register a
  handler; the engine dispatches purely by step type.
- **Robust evidence.** Results are persisted incrementally, teardown always
  runs, and reports are shareable, self-contained artifacts.
- **Good operator UX.** A browser-based live session (default) or coloured
  console prompts, an upfront checklist to pick which planned cases to run,
  per-step output, and end-of-case review with repeat / stop-and-save.

## How it works

```
suite.yml ──► loader ──► Suite (model) ──► engine ──► SuiteResult ──► JSON + HTML/Markdown reports
                                              │
                                     Prompter (operator I/O)
```

The engine only knows the *shape* of a suite (suite setup → per-case
setup/steps/teardown → suite teardown) and how to record outcomes. It doesn't
know what any step *does* — that's the handler's job, dispatched by step kind.

## Quick start

This package is standalone — it is not nested under a `lib/` namespace.
Run it as a module from the **parent directory** of this repo (the checkout
is named `manuprompt`):

```bash
cd ..   # parent directory of this checkout
python -m manuprompt path/to/suite.yml
```

This runs the suite with browser-based prompts by default, printing a live
session URL. Add `--cli-mode` to answer prompts in the terminal instead.

Every run writes `report.html` and `report.md` beside its `result.json`.
Regenerate or merge a report from saved JSON results without re-running; the
format follows the `-o` extension (`.html` or `.md`), and a directory (or no
`-o`) produces both:

```bash
python -m manuprompt report result.json -s suite.yml            # bundle: both
python -m manuprompt report result.json -s suite.yml -o out.md  # Markdown only
```

### Programmatic API

With this checkout on `sys.path` (typically: run from its parent directory):

```python
from manuprompt import load_suite, run_suite, generate_report

suite = load_suite("suite.yml")
result = run_suite(suite)
```

## Demo

A hardware-free (but not dependency-free) walkthrough lives in [demo/](demo/):
Playwright drives a real Chromium browser against a public UI-testing page,
in a two-case suite mixing automated `call:` steps (select a dropdown option,
assert a status label, attach a screenshot) with an operator `prompt:`
verdict. Beyond PyYAML it needs Playwright and a downloaded browser binary —
see [demo/README.md](demo/README.md) for the install steps.

```bash
cd ..
pip install -r manuprompt/demo/requirements.txt && playwright install chromium
python -m manuprompt manuprompt/demo/demo-suite.yml
```

See [demo/README.md](demo/README.md) for what each file does and how to use
`demo/` as a starting template for your own suite.

## Writing a suite

A suite is a YAML mapping with a `suite:` block and a `test_cases:` list:

```yaml
suite:
  name: ManuPrompt Demo (Playwright Web UI)
  variables:
    playground_url: http://uitestingplayground.com/select
  suite_setup:
    - call: browser.launch
      args:
        url: ${playground_url}
        headless: true

test_cases:
  - id: DEMO001
    name: Test dropdown selection by visible text
    steps:
      - call: browser.select_by_text
        args:
          selector: "#selectLanguage"
          text: Python
      - call: browser.expect_text
        args:
          selector: "#statusLanguage"
          expected: "Selected: Python (value: py)"
      - call: browser.screenshot
        artifact: language dropdown
```

Five step kinds ship out of the box: `prompt`, `tool`, `shell`, `call`, and
`optional`. See [docs/WRITING_SUITES.md](docs/WRITING_SUITES.md) for a full,
example-driven guide to every entry the YAML format supports, or
[SPECIFICATION.md](SPECIFICATION.md) for the engine's internals and
extension points.

## Documentation

- [docs/WRITING_SUITES.md](docs/WRITING_SUITES.md) — how to write a suite:
  every YAML entry, the five step kinds, variables, and how a run executes.
- [SPECIFICATION.md](SPECIFICATION.md) — full architecture, module map,
  execution model, and extension recipes for anyone (human or agent)
  modifying the engine itself.

## Developing with AI coding agents

This project is set up so an AI coding agent (e.g. Claude Code) can extend,
fix, or update it with minimal hand-holding:

- **[SPECIFICATION.md](SPECIFICATION.md) is the agent's map of the codebase.**
  It documents the module layout, data model, execution model, and every
  stable contract (`RunContext` in §8, the `call` glue seam in §9, the
  `Prompter` protocol in §10, the result/report shape in §12) that new work
  must keep. Point an agent at it instead of having it read the whole
  codebase cold — it's written for exactly that purpose.
- **Adding a step kind is a fixed, five-step recipe (§11):** model → loader →
  handler → registration → export. An agent can follow that recipe end to end
  without needing the implementation spelled out.
- **The demo is a self-verifying sandbox.** [demo/](demo/) has no hardware or
  credentials dependency, so an agent can make a change and then actually
  *run* it to check the change works, instead of only reading code or
  type-checking. It does need network access and Playwright's Chromium binary
  installed (see [demo/README.md](demo/README.md)) — run those installs once
  first. Every operator prompt has a documented, safe default at EOF (see
  `prompter.py`), so an agent can then run the demo **fully non-interactively**
  and inspect the resulting JSON/HTML report:

  ```bash
  cd ..
  python -m manuprompt --cli-mode --no-web-console \
      manuprompt/demo/demo-suite.yml --out-dir /tmp/agent-check < /dev/null
  ```

  (`--no-web-console` matters here: artifact steps prefer browser drag-drop
  over the console fallback whenever the web surface is running, even in
  `--cli-mode`, which would otherwise block forever waiting for a drop.)

A typical request to an agent: *"Read SPECIFICATION.md §5 and §11, then add an
`expect:` step modifier that fails a `tool`/`shell` step when stdout doesn't
match a regex. Add a step exercising it to `demo/demo-suite.yml` and run the
demo non-interactively to confirm it works."*

## Requirements

Python 3.12+. No third-party dependencies in the core package.
