# ManuPrompt — Solution Specification

> Snapshot specification of the `manuprompt` framework. This document
> is written so another agent (or developer) can add features, modify
> behaviour, or fix problems without re-reading the whole codebase first. It
> describes **what exists today** and the **contracts** that new work must keep.

---

## 1. Purpose and scope

**ManuPrompt** is a generic, YAML-driven, **human-in-the-loop** test
runner. A test suite is described declaratively in YAML (setup/teardown, test
cases, ordered steps, prompts). The engine drives the suite, prompting the
operator for manual steps and automating the rest (running tools/shell
commands, calling project glue code), and produces a machine-readable JSON
result plus a self-contained HTML report.

It is **not** specific to any product. The only place
project-specific behaviour lives is in `call:` glue modules shipped next to a
suite YAML (see §9). The core package has **no hardware or third-party
dependency** and runs standalone.

Design goals (in priority order):

1. **Suite authors write YAML, not Python.** The common case (prompt the
   operator, run a tool, record a verdict) needs no code.
2. **Core stays domain-agnostic.** Project-specific logic plugs in via the
   `call` seam only.
3. **Extensibility without touching the engine.** New step kinds register a
   handler; the engine dispatches purely by step type (Open/Closed).
4. **Robust evidence.** Results are persisted incrementally; teardown always
   runs; reports are shareable artifacts.
5. **Good operator UX.** Coloured console prompts, a startup plan of cases to
   run, per-step output, per-case start banner (step preview + run/skip),
   end-of-case review with repeat / stop-and-save, optional steps,
   skip-with-reason, file attachments.

---

## 2. Architecture overview

Layered, single-responsibility modules. Data flows **YAML → model → engine →
results → reporters**, with the **prompter** as the operator I/O boundary and
**handlers** as the per-step-kind strategy.

```
                     ┌────────────┐
   suite.yml ──────► │  loader    │ ── parses & validates ──► Suite (model)
                     └────────────┘
                                                                │
                     ┌────────────┐                             ▼
   CLI / api.py      │  engine    │ ◄── drives phases ──── Suite (model)
   wires everything  └────────────┘
        │                  │  per step
        │                  ▼
        │            get_handler(step)  ──►  StepHandler.execute(step, ctx, phase)
        │                  │                        │  prompts / runs
        │                  │                        ▼
        │                  │                  Prompter (console)   RunContext (ctx)
        │                  ▼
        │            SuiteResult ── to_dict() ──► JSON reporter (incremental)
        ▼                                     └─► HTML reporter (on demand)
   exit code
```

Key principle: **the engine knows only the *shape* of a suite** (suite setup →
per-case setup/steps/teardown → suite teardown) and **how to record
outcomes**. It does not know what any step *does* — that is the handler's job.

---

## 3. Module map (`manuprompt/`)

| File | Responsibility | Notes for editors |
|---|---|---|
| `model.py` | Immutable dataclasses describing a suite (`Suite`, `TestCase`, `Step` + subclasses). | Frozen dataclasses. Adding a step kind = add a `Step` subclass here. |
| `loader.py` | Parse + validate YAML into the model. Raises `SuiteValidationError` with author-friendly messages. | All schema rules live here (`_ACTION_KEYS`, `_ALLOWED_MODIFIERS`). |
| `theme.py` | `Theme` — optional colour/font overrides shared by the web UI and the HTML report (`resolve`, `css_variables`, `WEB_DEFAULTS`/`REPORT_DEFAULTS`, `load_theme_file`, `effective_theme`). | Two layers: project-wide `theme.yaml` next to this module (flat, or named presets + `apply:` — see `load_theme_file`), then a suite's own `theme:` overriding per-field (`effective_theme`). No embedded fonts/CDN — CSS `font-family` stacks only. |
| `errors.py` | Exception hierarchy, all rooted at `ManuPromptError`. | `SuiteValidationError`, `VariableError`, `CallError`. |
| `context.py` | `RunContext` (`ctx`) — per-scope mutable state passed to handlers; `${var}` resolution. | The handler-facing API. Stable contract. |
| `engine.py` | `Engine` — orchestrates phases, scoping, error boundaries, skip/optional/repeat, artifact collection, teardown draining, incremental persistence. | The control-flow brain. |
| `prompter.py` | `Prompter` protocol + `ConsolePrompter` (coloured terminal I/O). | The operator I/O boundary; `WebPrompter` (browser) is the default front-end. |
| `_text.py` | Shared text helpers (`strip_ansi`, `rtf_to_text`). | Used by the HTML reporter and the web UI. |
| `_banner.py` | Framed, optionally-coloured terminal banner (`box_url`). | Used by `manuprompt` (live URL) and `engine` (artifact URL). |
| `webui/server.py` | `LiveServer` — threaded stdlib HTTP server: SSE output, `POST /input` routing, file-upload requests (`/artifact*`), interactive prompts (`/prompts/pending`, `/prompt/<id>`, `/session`), live step progress (`/session/state`), static mounts + downloads (report bundle at `/report`, `/report.zip`); GET route seam. | Domain-agnostic; knows nothing about what is streamed, input means, files are, or prompts/progress mean. |
| `webui/gio.py` | `WebGIO` facade + `Channel` (`write`/`feed` output, `on_input` input) + `request_files` (multi-file browser upload) + `ask` (blocking prompt) + `set_session` (live progress) + `notify` (error/warning banner, see §10.1) + `mount_dir`/`add_download` (serve a report bundle + zip) + `FileUpload`. | The producer-facing API; `ctx.web_gio`. |
| `webui/prompter.py` | `WebPrompter` — `Prompter` implementation over `WebGIO.ask`. | The default operator front-end (browser prompts). |
| `webui/page.py` | Self-contained live page (`render_page`), drag-drop upload page (`render_artifact_page`), interactive session page with the live step list (`render_session_page`). | No external assets. |
| `webui/__init__.py` | Exports `WebGIO`, `Channel`, `FileUpload`, `LiveServer`, `WebPrompter`. | |
| `results.py` | Result tree (`SuiteResult`/`TestCaseResult`/`StepResult`), `Outcome` enum, aggregation, `to_dict()`. | Single source of truth for reporters. JSON-serialisable; `SuiteResult.theme` carries the run's *effective* (already-layered) theme into the JSON. |
| `steps/base.py` | `StepHandler` protocol + handler **registry** (`register_handler`, `get_handler`, `now_iso`). | Registry decouples engine from handlers. |
| `steps/prompt.py` | Handler for `PromptStep` (verdict or input capture). | |
| `steps/tool.py` | Handler for `ToolStep` (resolve tool path, run via shell, per-tool log). | |
| `steps/shell.py` | Handler for `ShellStep` (arbitrary shell command). | |
| `steps/call.py` | Handler for `CallStep` — the project-glue seam (`module.function` from suite dir). | Imports + caches suite-local modules. |
| `steps/__init__.py` | Imports handler modules for their registration side effects; re-exports registry API. | **Register new handlers here** (import for side effect). |
| `reporting/json.py` | `write_json` (atomic), `load_result`. | Written after every step. |
| `reporting/html.py` | `write_html` — self-contained HTML (inline CSS), strips ANSI, renders artifacts. | No external assets. Themed via `data["theme"]` (see `theme.py`) resolved against `REPORT_DEFAULTS`. |
| `reporting/merge.py` | `merge_results` (union cases by id, conflict `Chooser`), `relocate_artifacts` (copy attachments next to the report, rebase paths), `collect_logs` (copy each run's loose log files into the bundle, name-agnostic). | Enables regenerating/merging reports from saved JSON. |
| `api.py` | High-level `load_suite` / `run_suite` / `generate_report` convenience wiring. | Default logger, default artifacts dir. |
| `cli.py` | `argparse` CLI: `python -m manuprompt [options] suite.yml`, plus the `report` subcommand. | `-t`/`-k` filters, `--out-dir`, `--var`, `--tool`, `-v`; `report` merges JSON → HTML. |
| `__main__.py` | Entrypoint → `cli.main`. | |
| `__init__.py` | Public API surface (`__all__`). | Keep exports in sync when adding public types. |

Example suite + glue lives **outside** the package core, under
`demo/` (see §14).

---

## 4. Data model (`model.py`)

All frozen dataclasses. `Step` is the base; concrete kinds are a discriminated
union dispatched by the YAML action key.

```python
class Step:                 # base
    name: str               # label for logs/reports (derived if not given)
    artifacts: tuple[str, ...] = ()   # labels of files to attach after the step
    note: str = ""          # authored annotation; shown to operator + in report

class PromptStep(Step):
    prompt: str = ""        # instruction shown to operator (supports ${var})
    store: str | None = None  # if set: capture typed input into this variable

class ToolStep(Step):
    tool: str = ""          # key into suite.tools
    command: str = ""       # args appended to the tool path (supports ${var})
    save_output: str | None = None   # capture stdout into this variable

class ShellStep(Step):
    command: str = ""       # arbitrary shell command (supports ${var})
    save_output: str | None = None

class CallStep(Step):
    target: str = ""        # "module.function" resolved against suite dir
    args: dict[str, Any] = {}   # kwargs; string values support ${var}

class OptionalStep(Step):   # control-flow container, not a leaf
    steps: tuple[Step, ...] = ()   # run only if operator opts in
    prompt: str = ""        # question; derived from child names if empty

class TestCase:
    id: str                 # e.g. "DEMO002"
    name: str
    steps: tuple[Step, ...]
    description: str = ""               # optional; shown in UI and reports
    variables: dict[str, Any] = {}      # case-scoped, overlay suite vars
    skip_reason: str = ""               # non-empty ⇒ marked to skip
    test_setup: tuple[Step, ...] = ()   # case-specific, runs AFTER suite test_setup
    test_teardown: tuple[Step, ...] = ()  # runs BEFORE suite test_teardown

class Suite:
    name: str
    description: str = ""
    variables: dict[str, Any] = {}
    test_environment: tuple[tuple[str, str], ...] = ()  # (label, value) pairs
    tools: dict[str, str] = {}          # name -> binary path / launcher
    suite_setup: tuple[Step, ...] = ()
    test_setup: tuple[Step, ...] = ()
    test_teardown: tuple[Step, ...] = ()
    suite_teardown: tuple[Step, ...] = ()
    test_cases: tuple[TestCase, ...] = ()
    web_title: str = ""                 # live-console display name (falls back to name)
    theme: Theme = Theme()              # per-suite colour/font override, layered on
                                         #   top of the project-wide theme.yaml — see
                                         #   theme.effective_theme() and theme.py
    source_dir: Path | None = None      # dir the YAML loaded from (call seam)
```

---

## 5. YAML schema reference

Top level is a mapping with a `suite:` key and a `test_cases:` list.

### 5.1 `suite:` block

```yaml
suite:
  name: My Suite               # REQUIRED, non-empty string
  description: ...             # optional
  web_title: ManuPrompt Demo      # optional; live-console display name (banner +
                               #   browser title); falls back to name
  theme:                        # optional; per-suite colour/font override, layered
                               #   on top of the project-wide theme.yaml (next to
                               #   theme.py); both apply to the web UI and the report
                               #   (each resolves against its own base palette); see
                               #   theme.py for the field list
    mono_font_family: "Cascadia Code, Consolas, Menlo, monospace"
  variables:                   # optional; name -> value (any scalar; null allowed)
    playground_url: http://uitestingplayground.com/select
    test_session_password: secret  # optional; if set, password-protects the
                               #   live web surface (HTTP Basic, §10.1)
  test_environment:            # optional; mapping OR list (see below)
    - Target: http://uitestingplayground.com/select
    - Tester: demo
  tools:                       # optional; name -> path/launcher (strings)
    curl: curl
  suite_setup:    [ <step>, ... ]   # run once before any case
  test_setup:     [ <step>, ... ]   # run before each case
  test_teardown:  [ <step>, ... ]   # run after each case
  suite_teardown: [ <step>, ... ]   # run once after all cases
```

`test_environment` accepts a mapping (`key: value`) or a list whose items are
either single-key mappings (`- key: value`) or plain scalars (`- free text`,
yields `("", "free text")`). Order is preserved; rendered in both reports.

### 5.2 `test_cases:` list

```yaml
test_cases:
  - id: DEMO002                 # REQUIRED, used for ordering & -t filter
    name: Human readable title # REQUIRED
    description: ...           # optional; shown next to the case in the UI and reports
    skip:                      # optional; string OR { reason: ... }
      reason: https://jira.example/DEMO-42
    variables:                 # optional; case-scoped (overlay suite vars)
      expected_label: null
    test_setup:    [ <step>, ... ]   # optional; runs after suite test_setup
    test_teardown: [ <step>, ... ]   # optional; runs before suite test_teardown
    steps:                     # REQUIRED, non-empty
      - <step>
      - ...
```

**Cases are sorted by `id` at load time** — YAML order does not matter. (The
CLI `-t` filter overrides this: selected cases run in the order the ids are
given — see §13.)

`skip:` may be a bare string (`skip: some reason`) or a mapping with `reason:`.

### 5.3 Steps

A step is a mapping with **exactly one action key**, plus allowed modifiers.
The `artifact` and `note` modifiers are allowed on **any** step. `note` is an
author-provided annotation (e.g. a reminder or extra instruction): it is shown
to the operator when the step runs and recorded on the step result for the
report (rendered as a highlighted callout in the Notes column). It supports
`${var}` resolution.

| Action key | Meaning | Allowed modifiers (besides `artifact`, `note`) |
|---|---|---|
| `prompt:` | Instruct operator; record PASS/FAIL/ack, or capture input. | `store`, `name` |
| `tool:` | Run a tool from `suite.tools`. | `command`, `save_output`, `name` |
| `shell:` | Run an arbitrary shell command verbatim. | `save_output`, `name` |
| `call:` | Call `module.function` from the suite dir. | `args`, `name` |
| `optional:` | Group of nested steps the operator may run or skip. | `message` |

Examples:

```yaml
# prompt: verdict (PASS/FAIL/ack)
- prompt: Does the dropdown look correct in the screenshot?

# prompt: capture input into a variable
- prompt: Enter the expected status label to assert later.
  store: expected_label

# tool: resolved against suite.tools, command appended
- tool: curl
  command: -s -o /dev/null -w "%{http_code}" ${playground_url}
  save_output: http_status

# shell: arbitrary command (pipes/globs/redirects work)
- shell: date +%Y-%m-%d

# call: project glue function in <suite_dir>/browser.py
- call: browser.select_by_text
  args:
    selector: "#selectLanguage"
    text: Python

# optional: nested group gated by an operator confirm
- optional:
    - call: browser.screenshot
      artifact: optional snapshot
  message: Capture an extra screenshot before continuing?

# artifact: attach file(s) after the step (any step kind)
- prompt: Does the dropdown look correct in the screenshot?
  artifact: operator snapshot      # single label
  # artifact: [ operator snapshot, notes.txt ]   # or several labels
# Each label is one drag-drop request; via the browser a single label can
# collect MULTIPLE files (e.g. one console log per device), saved with
# collision-free names. Console-path collection takes one file per label.

# note: author annotation shown to the operator and saved in the report (any step kind)
- prompt: Does the dropdown look correct in the screenshot?
  note: Compare the status label under the Product Version dropdown.
```

`name:` overrides the auto-derived step label. The label is otherwise derived
from the action content (full text, not truncated — the report/console
truncate visually but keep the full text in tooltips/data).

---

## 6. Execution model (`engine.py`)

### 6.1 Phases and order

```
suite_setup (once)
for each case (sorted by id; or in -t order when filtered):
    test_setup      = suite.test_setup  + case.test_setup
    steps           = case.steps
    test_teardown   = case.test_teardown + suite.test_teardown
suite_teardown (once)
```

All step results (including setup/teardown) are appended to the owning
`TestCaseResult.steps`, each tagged with its `phase`
(`suite_setup` / `test_setup` / `step` / `test_teardown` / `suite_teardown`).

### 6.2 Variable scoping

- A **fresh `RunContext`** is created per scope (suite setup, each case, suite
  teardown) so variables stored in one case do **not** leak into the next.
- Variables set during **suite_setup** are captured and visible to every case.
- Per case, the scope is `{**suite_vars, **case.variables}`.
- `resources`, the teardown registry, `artifacts_dir`, `suite_dir`, `prompter`,
  `logger`, and `result` are **shared by reference** across all scopes.

### 6.3 Blocking failures

- If a **setup** phase produces a `FAIL` or `ERROR`, the dependent steps are
  **skipped** (recorded as `SKIP`), but teardown still runs.
- If **suite_setup** has a blocking failure, **all cases are skipped**.

### 6.4 Error boundary

Every leaf step runs inside `try/except`. A handler that raises becomes an
`Outcome.ERROR` result (with the exception text) instead of aborting the run.

### 6.5 Upfront case selection (web mode)

Before suite setup runs — the very first thing the operator sees on the
session page — the engine asks once which of the suite's planned cases to
actually run: `Prompter.select_cases(cases)`, given every `(id, name, description)`
triple in suite order. Returns the set of ids to run, or `None` to run all.

- **`WebPrompter`** renders this as a standalone card listing every case with
  a checkbox, **all checked by default**, plus *Select all* / *Select none*
  toggles and a *Start run* button. Unchecking a case and starting the run
  skips it — its `TestCaseResult` is recorded exactly like an operator-skipped
  case (§6.6), with reason `"deselected by operator before the run started"`.
- **`ConsolePrompter`** always returns `None` (run all): the console flow
  already prints the full plan before the run starts (`run_suite`'s
  `_announce_plan`) and lets the operator skip cases one at a time via
  `start_case` below, so this avoids a second, redundant console prompt.

This is independent of (and runs after) any `-t`/`-k` CLI narrowing (§13) —
those flags decide *which cases the suite object contains at all*; this
prompt offers a further, per-run, interactive cut of whatever survived that.

### 6.6 Per-case start prompt and skip-with-reason

Before each case the engine announces it and decides whether to run it:

- A case **with `skip_reason`** tells the operator the reason and asks "Run it
  anyway?" (`_run_skipped_anyway`, **default: no**). If declined, it is recorded
  with its `skip_reason` and all steps as `SKIP`.
- **Otherwise** the operator gets a prominent **start-case banner** showing the
  case id and name, **a numbered preview of the case's steps**, and is asked to
  **run** (default / Enter / EOF) or **skip** it (`prompter.start_case`, which
  receives the step labels). Skipping records the case with reason
  "skipped by operator" and all steps as `SKIP`.

Either way the case is still listed in the report. This makes the start of each
case clearly visible when running a multi-case suite.

### 6.7 Optional groups

When an `OptionalStep` is reached, the operator is asked (`ask_confirm`,
**default: yes**). A marker result is recorded (`ACK` if opted in, else `SKIP`).
On opt-in, nested steps run in the current scope (nesting allowed). On decline,
nested steps are recorded as `SKIP`.

### 6.8 End-of-case review (proceed / repeat / stop)

After each case finishes, `prompter.review_case()` shows every step's status
and asks the operator to **proceed**, **repeat**, or **stop**:

- **proceed** (`CaseDecision.PROCEED`, default / EOF) — accept and continue to
  the next case.
- **repeat** (`CaseDecision.REPEAT`) — discard the previous attempt
  (`cases.pop()`) and re-run the case, so only the accepted attempt remains.
- **stop** (`CaseDecision.STOP`) — accept this case and end the session early:
  the engine breaks out of the case loop so **remaining cases are not run**, but
  `suite_teardown` still runs (so the HTML report is saved). Useful to wrap up a
  partial run with a report. `_run_case` returns whether STOP was chosen and
  `run()` breaks the loop on it.

### 6.9 Guaranteed teardown (resources)

The engine drains `ctx`-registered teardowns (see §8) in **reverse order**
inside a `finally`, so resources (e.g. a live console window, a serial handle)
are released on normal completion, on error, and on operator interruption.

### 6.10 Incremental persistence

After every step the engine calls `on_update(result)`; `run_suite` wires this to
`write_json`, so an interrupted run still leaves a usable partial `result.json`.

---

## 7. Variables and interpolation

- Syntax: `${name}` inside any prompt text, tool/shell command, or string
  `call` arg.
- Resolved by `RunContext.resolve(text)` at run time.
- A reference to an **undefined** variable, or one still holding `None`, raises
  `VariableError` (which the engine turns into an `ERROR` result for that step).
- Declaring a variable as `null` in YAML is the idiom for "a `store`/`save_output`
  step will fill this in later"; referencing it before it is set fails loudly.
- Writers of variables: `prompt` + `store`, `tool`/`shell` + `save_output`,
  and any `call` glue via `ctx.set_var`.

---

## 8. `RunContext` (the handler-facing API, `context.py`)

Passed as `ctx` to every handler and every `call` glue function.

```python
ctx.variables: dict[str, Any]      # current scope's variables
ctx.tools: dict[str, str]          # tool name -> path/launcher
ctx.artifacts_dir: Path            # run output dir (logs, attachments, reports)
ctx.prompter: Prompter             # operator I/O
ctx.logger: logging.Logger         # use this, not logging.getLogger
ctx.resources: dict[str, Any]      # run-scoped live objects (e.g. device handle)
ctx.suite_dir: Path                # dir the suite YAML loaded from
ctx.result: SuiteResult | None     # in-progress result (e.g. for a report step)
ctx.web_gio: WebGIO | None         # live browser I/O surface, or None if disabled
ctx.case_result: TestCaseResult | None  # case currently running (None in suite setup/teardown)

ctx.set_var(name, value)           # write a variable
ctx.add_teardown(cleanup)          # register zero-arg cleanup (drained reverse, in finally)
ctx.attach(label, path) -> str | None      # attach a file to the current step as an artifact
ctx.attach_log(label, path) -> str | None  # attach a file to the current case as a labelled log
ctx.resolve(text) -> str           # substitute ${var}; raises VariableError
```

**`variables` vs `resources`**: `variables` are string-interpolable, case-scoped
values; `resources` hold live Python objects that persist for the whole run and
must be cleaned up via `add_teardown`.

**`web_gio`** (see §10.1): a producer obtains a named channel
(`ctx.web_gio.channel("app console")`), streams output to it
(`channel.write(text)` / `channel.feed(bytes)`), and may accept browser input
by registering `channel.on_input(handler)`. It is `None` when the surface is
disabled — always guard for that. The surface is shared by reference across
scopes and stopped by the engine at end of run.

**`attach`**: `ctx.attach(label, path)` attaches a file to the **current step**
as an artifact — it copies the file into the run's `attachments/` and records
`{label, path}` on that step's result, which the report renders under the step
exactly like operator-supplied `artifact:` files (images inline, etc.). The
engine opens the attachment window for the duration of each leaf step; calling
`attach` outside a step no-ops with a warning.

Prefer declaring `artifact:` in the suite YAML and having a `call:` **return
the file path** (see §9) so the artifact label stays visible in the suite.
Use `ctx.attach` directly when glue must attach without returning a path.

**`attach_log`**: `ctx.attach_log(label, path)` attaches a file to the
**current test case** as a labelled log — it copies the file into the run's
`attachments/` and records `{label, path}` on `ctx.case_result.logs`, which the
report links beneath that test under Logs (see §12.3). It is domain-agnostic: the core
attaches whatever file the glue provides (e.g. a device console log holding only
that test's lines). It no-ops with a warning outside a case (`ctx.case_result`
is `None` during suite setup/teardown). Prefer returned-path + YAML `artifact:`
(or `ctx.attach`) when the file belongs to a specific step; use `attach_log`
for case-scoped evidence (e.g. a glue step that slices a per-test log file).

---

## 9. The `call` seam — project glue contract (`steps/call.py`)

This is the **only** place project-specific behaviour enters. A `call:` target
is `module.function` (exactly one dot). `module` resolves to
`<suite_dir>/module.py`; `function` is looked up in it and called as:

```python
function(ctx, **resolved_args)     # string args have ${var} resolved first
```

Rules:

- Glue modules are imported **lazily** (only when the first matching `call`
  runs) and **cached** per file path. The module is registered in `sys.modules`
  before exec so it can use its own dotted imports.
- Return-value → outcome: returning `False` ⇒ `Outcome.FAIL`; any other value
  (including `None`) ⇒ `Outcome.PASS`. Raising ⇒ `Outcome.ERROR` (engine
  boundary). The return repr (if not `None`) is stored as the step `output`,
  unless the value was consumed as an artifact file (below).
- **Artifacts from a returned path.** If the step declares `artifact:` and the
  function returns an existing file path (string/`Path`) or a list of them,
  those files are attached under the YAML labels via `ctx.attach` — no operator
  prompt for labels that were satisfied. One label + several paths puts all
  files under that label; several labels are paired with paths in order.
  Remaining unsatisfied labels still ask the operator (§12). This keeps the
  artifact name in the suite YAML while glue only produces the file (see the
  demo's `browser.screenshot`).
- Glue functions receive `ctx`, so they can read/write variables, log via
  `ctx.logger`, store live objects in `ctx.resources`, register cleanups via
  `ctx.add_teardown`, and read `ctx.result`.
- Errors resolving/invoking the target raise `CallError`.

This is how, e.g., browser automation (Playwright in `demo/browser.py`) is
integrated without the core depending on it. A report-generation function is
just another `call` (it uses `ctx.result` + `write_html`).

---

## 10. Prompter — operator I/O boundary (`prompter.py`)

The engine never touches the terminal directly; it goes through the `Prompter`
protocol. Two implementations ship: **`WebPrompter`** (`webui/prompter.py`),
which presents each prompt in a **web browser** and is the **default**, and
`ConsolePrompter` (coloured stdout/stdin), selected with `--cli-mode`. Implement
the protocol to provide another front-end or a scripted prompter for tests. The
engine calls the prompter **synchronously** (each call blocks until answered),
so any implementation may block waiting for the operator.

```python
class Prompter(Protocol):
    def ask_verdict(self, text: str) -> tuple[Outcome, str]: ...
        # returns (PASS|FAIL|ACK, notes)
    def ask_input(self, text: str) -> str: ...
        # free-text capture for prompt+store
    def ask_confirm(self, text: str, default: bool = True) -> bool: ...
        # yes/no; used by optional groups (default True) and skip (default False)
    def ask_artifact(self, label: str, error: str = "") -> str: ...
        # path to a file to attach; "" to skip; `error` shown when re-prompting
    def select_cases(self, cases: Sequence[tuple[str, str, str]]) -> set[str] | None: ...
        # called once, before suite setup; (id, name, description) triples for
        # every planned case; returns ids to run, or None to run all
        # (ConsolePrompter always returns None — see §6.5)
    def start_case(self, case_id: str, name: str, steps: Sequence[str],
                   description: str = "") -> bool: ...
        # announce the next case + preview its step labels (and optional
        # description); True = run, False = skip (default True / EOF)
    def review_case(self, case: TestCaseResult) -> CaseDecision: ...
        # PROCEED / REPEAT / STOP after a case finishes (STOP ends the
        # session early but still runs suite_teardown → report saved)
    def show_tool_output(self, command, stdout, stderr, label="") -> None: ...
        # display captured tool/shell output; `label` is the tool key
        # (or "shell") — the web UI streams each to its own named tab
```

Console UX details: prompt text is blue; `[p]ass` green, `[f]ail` red,
`[Enter] acknowledge`, with an optional note; setup/teardown prompts are
prefixed with a `[PHASE]` tag; tool stdout/stderr use dedicated colours; EOF on
a verdict acknowledges, on a confirm returns the default.

**Modes.** By default `run_suite` runs prompts in the browser (`WebPrompter`)
whenever a WebGIO surface is available; `--cli-mode` (or `run_suite(...,
cli_mode=True)`) runs them in the terminal (`ConsolePrompter`) instead. Passing
an explicit `prompter=` overrides both. When the WebGIO surface is disabled
(`--no-web-console` / `MANUPROMPT_NO_WEB_CONSOLE`) prompts fall back to the
console regardless of mode. In either mode the WebGIO surface still streams
console output and collects artifacts in the browser.

### 10.1 WebGIO — browser input/output surface (`webui/`)

A second operator surface complements the prompter: a live **input/output**
view served to a **web browser**, replacing the old X11/tmux terminal that
relied on display forwarding out of the run's container. **WebGIO** = "Web
General Input/Output."

- **`WebGIO`** (`webui/gio.py`) runs an embedded threaded HTTP server (stdlib
  `http.server`, **no third-party deps**) and hands out **channels**.
  `run_suite` starts one by default, announces its URL in a framed banner,
  exposes it as `ctx.web_gio`, and the engine `stop()`s it at end of run.
- **Domain-agnostic by contract.** The `webui` package transports arbitrary
  text/bytes over named channels and routes typed input back to producer
  handlers, imposing **no schema** on either direction; it never references
  devices, serial ports, or any producer concept. Producers **align to the `Channel`
  API** — output via `write(text)` / `feed(bytes)`, input via
  `on_input(handler)` — and the library never calls back into a producer
  except through a registered input handler. Channel names and producer
  behaviour (e.g. mirroring a process log into a tab) live only in suite glue.
- **Output: Server-Sent Events.** Each browser opens
  `EventSource("/stream/<channel>")`; the server replays the channel backlog
  (capped ring, ANSI stripped via `_text.strip_ansi`) then pushes live frames.
- **Input: `POST /input/<channel>`.** The request body is delivered verbatim to
  every handler registered via `Channel.on_input`. A channel with no handler is
  not input-capable (the page shows no input field, and a POST returns 404).
  Handlers run on the server's request thread and should return promptly.
- **Channel discovery.** `GET /channels` returns `[{"name","input"}, …]`; the
  page polls it to add tabs (and input fields) for channels created after load.
- **File upload (drag-drop artifacts).** `WebGIO.request_files(label)` opens a
  pending request and **blocks** until the operator finishes it (having dropped
  **any number** of files) or skips it on the dedicated drag-drop page
  (`GET /artifact`). Files can be **dropped, chosen via a picker, or pasted with
  Ctrl+V** (a pasted image uploads directly; pasted text becomes a `.txt` file;
  paste routes to the active request). The page polls `GET /artifacts/pending`
  (`[{"id","label","files":[…]}, …]`); each file POSTs its bytes to
  `POST /artifact/<id>` (original name in `X-Filename`) which **appends** it,
  `POST /artifact/<id>/done` finishes with all collected files, and `POST
  /artifact/<id>/skip` attaches none. The page waits for in-flight uploads
  before sending `done`. This is the drag-drop alternative to typing a
  filesystem path; the engine uses it for `artifact:` collection (see §12), so a
  single `artifact:` label can attach several files (saved with collision-free
  names). The primitive is generic — any producer can ask the browser for files.
- **Page UX.** Dark terminal theme, tab per channel, auto-scrolling output, a
  **Clear** button (clears the current view client-side; reloading replays the
  backlog), and an **input field** per input-capable channel (Enter submits).
  The separate `/artifact` page shows a drop zone per pending request that also
  accepts a file picker and clipboard paste (Ctrl+V).
- **Reachability.** The server binds `0.0.0.0`; under the CI/devcontainer's
  `--network host`, the printed `http://localhost:PORT/` is reachable on the
  host with no port mapping.
- **Port.** Defaults to a **fixed** `9999` so the URL is stable run-to-run;
  `LiveServer.start` falls back to an OS-chosen free port (logging a warning)
  if `9999` is busy, so a stale or parallel server never blocks a run.
- **Password (optional).** When the suite defines a `test_session_password`
  variable (or `MANUPROMPT_WEB_PASSWORD` is set), every request — pages, SSE
  streams, input, uploads — requires **HTTP Basic** credentials whose password
  matches (the username is ignored). The browser prompts once and reuses the
  credentials for all subsequent same-origin requests (including `EventSource`).
  Comparison is constant-time (`hmac.compare_digest`). With no password the
  surface is open.
- **Interactive prompts (`WebPrompter`).** `WebPrompter` (`webui/prompter.py`)
  implements the `Prompter` protocol over the surface and is the default
  front-end (§10). Each prompt call maps to `WebGIO.ask(kind, payload)`, which
  registers a **single pending prompt** on the server and **blocks** until the
  operator answers it in the browser (mirroring the `request_files` pattern).
  The engine is sequential, so at most one prompt is pending at a time; a new
  prompt abandons any stale one. `GET /prompts/pending` returns the active
  prompt as `{"id","kind", …payload}` (or `{}`); `POST /prompt/<id>` (JSON body)
  delivers the answer — 204 on a match, 409 if `<id>` is not the pending prompt.
  Prompt kinds: `verdict` → `{verdict: pass|fail|ack, note}`; `input` →
  `{value}`; `confirm` → `{confirm}`; `select_cases` (carries `text` and
  `cases: [{id, name, description}, …]` for every planned case, sent once
  before suite setup) → `{selected: [id, …]}`; `start_case` (carries
  `case_id`, `name`, `description`, `steps`) → `{run}`; `review` (carries
  the step results + overall `outcome`, plus `description`) →
  `{decision: proceed|repeat|stop}`. `ask_artifact` is a no-op in web mode
  (files come via `request_files`); `show_tool_output` streams to a channel
  named after the producer — a `tool` step's tool key (so each tool gets its own
  console tab named exactly as in the suite YAML) or `"shell"`. If the surface
  is stopped while a prompt waits,
  `ask` returns `None` and the prompter falls back to the same safe default the
  console uses at EOF (run/proceed/ack), so a run never hangs.
- **Session page (`GET /session`).** The primary page in web-prompt mode. It
  shows the running test case as an **always-visible ordered step list** whose
  highlight moves to the active step as the run progresses: completed steps show
  their outcome (✓/✗/●/–), the current step is highlighted, and the rest are
  pending. The operator prompt for the active step (verdict → Pass/Fail/Ack +
  note; input → value field; confirm → Yes/No) renders **inline on that step**.
  A verdict prompt has **keyboard shortcuts** — `P` pass, `F` fail, `A`/Enter
  acknowledge (ignored while the note field has focus, where Enter submits an
  acknowledge) — and an input prompt's value field is **auto-focused** so the
  operator can type without clicking.
  The per-case **meta** prompts — `select_cases` (a checkbox per planned case,
  all checked by default, plus *Select all*/*Select none* and *Start run*,
  shown once before anything else), `start_case` (Run/Skip with a step
  preview), and `review` (reviewed-step list + Proceed/Repeat/Stop) — render as
  a standalone card instead, since they bracket the case (or the whole run)
  rather than belonging to a step. A finished **command** step (tool/shell)
  whose result carried output
  is **expandable** — clicking it reveals the executed command line and its
  captured output inline (the expanded set is preserved across the poll-driven
  re-renders). The page also embeds the live console (same SSE channel tabs as
  `/`), separated from the step list by a **draggable horizontal handle** that
  resizes the split, and links to the standalone console (`/`) and artifact
  (`/artifact`) pages (flagging the latter when a file request is pending).
- **Live step progress (`GET /session/state`).** The engine publishes a
  snapshot — `{case:{id,name}, phase, steps:[{name,optional,status,detail}]}` —
  to `LiveServer.set_session` (via `WebGIO.set_session`) as it runs: the step
  skeleton is the case's planned steps (optional groups flattened to align 1:1
  with the results they produce), each resolved to its `Outcome` once run, with
  the executing/prompting step marked `active` during the `step` phase (none
  active during setup/teardown/review). A finished step that produced output
  also carries its `command` and `output` so the page can expand it. The session page polls this and
  re-renders only when the snapshot or pending prompt changes, so text typed
  into an active-step field is not clobbered by polling. Publishing is a no-op
  when the browser surface is disabled; it is cleared between cases and on stop.
- **Error notices (`GET /notices`).** The engine calls `WebGIO.notify(message,
  level="error"|"warning")` (→ `LiveServer.add_notice`) whenever a step raises,
  a teardown callable raises, or suite setup fails outright — cases where the
  failure would otherwise only appear in the CLI/log, e.g. an exception during
  suite setup happens *before* any case's live step list exists (no
  `set_session` snapshot to show it in). Every page (`/`, `/session`,
  `/artifact`) polls `/notices` and renders each as a dismissible banner at the
  top of the page (dismissal is client-side only; polling would otherwise bring
  a dismissed notice straight back). Notices accumulate until `stop()` clears
  them; they carry only a short summary (`_first_line` of the exception) and
  point the operator at the CLI/log for the full traceback.
- **Finished report (web-prompt mode).** When a session ends — the operator
  chooses *Stop & save report*, or the last case is accepted — `run_suite`
  keeps the surface alive (the engine's `stop_web_gio=False`) and calls
  `_present_report`: it renders a **self-contained bundle** (report HTML +
  relocated artifacts + collected logs + merged `result.json`) via
  `generate_report` into a temp directory, **zips** it, saves the zip as
  `report.zip` next to the run's results, then `mount_dir("/report", bundle)` +
  `add_download("/report.zip", …)`. It blocks on a `finished` prompt (payload
  `{report_url, zip_url, outcome, cases}`); the session page renders a report
  view — an **Open report in new tab** button (so it renders in its own native
  theme), a **Download report (.zip)** button, and a **Finish** button. Clicking Finish answers the
  prompt, which unblocks `run_suite` (Ctrl-C / stop unblocks too); it then stops
  the surface and removes the temp bundle. Static mounts serve the bundle's
  assets with path-traversal protection; the download sets
  `Content-Disposition: attachment`. Both are behind the surface password.
- **Finished report (non-web-prompt runs).** When prompts are *not* answered
  in the browser — `--cli-mode`, or the web surface disabled entirely via
  `MANUPROMPT_NO_WEB_CONSOLE` or an explicit `prompter=` — there is no session
  page to present a report on, so `run_suite` instead calls
  `generate_report([out_json], out_dir / "report.html", formats={"html","md"},
  logger=...)` directly after the run: plain `report.html` and `report.md`
  appear next to `result.json` in the run's own output directory (an in-place
  regeneration — see §12.4 — so no artifacts are copied). A failure to render
  logs a warning rather than failing the run (the JSON result is unaffected
  either way).
- **Extension seam.** `LiveServer.register_route` adds further GET endpoints;
  combined with `on_input` and the prompt subsystem above it hosts the
  interactive web front-end.
- **Name.** The surface's display name (banner heading + browser title) comes
  from `suite.web_title`, falling back to `suite.name`; `MANUPROMPT_WEB_TITLE` /
  `--web-title` override it. The startup banner is a framed, coloured (on a
  TTY) box advertising the URL.
- **Config.** Env: `MANUPROMPT_NO_WEB_CONSOLE` (disable), `MANUPROMPT_WEB_PORT`
  (preferred port; default `9999`, with busy-port fallback), `MANUPROMPT_WEB_HOST`
  (printed host; default `localhost`), `MANUPROMPT_WEB_TITLE` (display name),
  `MANUPROMPT_WEB_PASSWORD` (gate password; overrides the suite variable). CLI:
  `--web-port`, `--web-title`, `--no-web-console` (override env), and
  `--cli-mode` (answer prompts in the terminal instead of the browser).

---

## 11. Adding a new step kind (extension recipe)

The engine dispatches by type, so **you never edit the engine** to add a step
kind:

1. **Model** (`model.py`): add a frozen `Step` subclass with its fields.
2. **Loader** (`loader.py`): add the action key to `_ACTION_KEYS`, its modifiers
   to `_ALLOWED_MODIFIERS`, write a `_parse_<kind>_step`, and dispatch to it in
   `_parse_step`. (The `artifact` modifier is added automatically for all.)
3. **Handler** (`steps/<kind>.py`): implement a class with
   `execute(self, step, ctx, phase) -> StepResult` and decorate it with
   `@register_handler(<KindStep>)`. Use `now_iso()` for timestamps.
4. **Register** (`steps/__init__.py`): import the new handler module for its
   registration side effect.
5. **Export** (`__init__.py`): add the new step type to `__all__` if public.

A handler should: resolve `${var}` via `ctx.resolve`, perform its work, call
`ctx.prompter.show_tool_output(...)` if it produces output, optionally
`ctx.set_var(...)`, and return a `StepResult` with `name`, `kind`, `phase`,
`outcome`, and evidence (`detail`/`output`/`notes`). Let exceptions propagate —
the engine converts them to `ERROR`.

---

## 12. Results and reporting

### 12.1 Result tree (`results.py`)

`SuiteResult` → `cases: [TestCaseResult]` → `steps: [StepResult]`, plus
`suite_setup`/`suite_teardown` step lists on the suite. All are
JSON-serialisable via `to_dict()`. `TestCaseResult` also carries
`description` (from the suite YAML) and
`logs: [{"label","path"}]` — files attached to the case via `ctx.attach_log`
(e.g. a per-test device log), paths relative to `artifacts_dir`.

`Outcome`: `PASS`, `FAIL`, `ERROR`, `ACK`, `SKIP`. A sixth value,
`not-tested` (rendered **NOT-TESTED**), exists **only in reports** (it is not in
the live `Outcome` enum); it marks a planned case absent from the results (see
§12.4).

Aggregation (`aggregate_outcome`): a container is `ERROR` if any child errored,
else `FAIL` if any failed, else `PASS` if any passed, else `SKIP` if all
skipped, else `PASS` (empty). `SuiteResult.counts()` tallies case outcomes.

`StepResult` fields: `name`, `kind`, `phase`, `outcome`, `detail` (resolved
instruction/command), `output`, `note` (authored annotation from the step's
`note` modifier), `notes` (operator/runtime notes), `error`, `artifacts`
(`[{"label","path"}]`, path relative to `artifacts_dir` — from `ctx.attach`
during the step and/or operator `artifact:` collection afterwards),
`started_at`, `finished_at`.

### 12.2 JSON reporter (`reporting/json.py`)

`write_json(result, path)` writes **atomically** (temp file + `os.replace`)
because it is rewritten after every step. `load_result(path)` reads it back.

### 12.3 HTML reporter (`reporting/html.py`)

`write_html(result_dict, out_path)` renders a **self-contained** HTML file
(inline CSS, no external assets) from `SuiteResult.to_dict()`. It:

- strips ANSI escape codes from captured output (browsers show them as garbage);
- renders the `test_environment` block, summary/counts, per-case step tables;
- shows full step names on hover (CSS ellipsis + `title`);
- renders **artifacts** by type: images inline (clickable to full size); videos
  (`.mp4`/`.m4v`/`.mov`/`.webm`/`.ogv`) in a `<video>` player with a download
  fallback — the `<source>` carries **no MIME `type`** so the browser sniffs the
  content (a `video/quicktime` hint makes Chrome reject a playable `.mov`); `.rtf`
  as a link to a generated full-page **text view** opened in a new tab (a
  companion `<file>.rtf.html` written next to the file via `_text.rtf_to_text` —
  formatting is not preserved — plus a `.rtf` download); everything else as a
  download link. Paths are relative, so the report must live in `artifacts_dir`
  (attachments are copied to `artifacts_dir/attachments/`); `write_html` reads
  RTF files, and writes their view pages, relative to the report's own directory;
- renders each case's **`logs`** as a **"Logs"** link list beneath that test:
  the precise per-test files attached via `ctx.attach_log` (e.g. *session log*,
  persisted on the case) and, in a merge, the **render-only** loose files of the
  run chosen for that case (labelled by basename). A test thus links only **its
  own** run's logs. Entries are `{label, path}` dicts (a stray string is
  tolerated). See §12.4 for how persistence keeps merges from accumulating logs.

Report generation is typically a `suite_teardown` `call:` step (or the engine's
built-in report write) so the report reflects everything up to it.

### 12.3a Markdown reporter (`reporting/markdown.py`)

`write_markdown(result_dict, out_path)` renders the **same**
`SuiteResult.to_dict()` mapping to a GitHub-flavored Markdown file, for pasting
into pull requests, wikis or issues. It is deliberately **leaner** than the HTML
report — a results presentation, not the full evidence record: title + outcome,
description, a one-line summary of the non-zero counts, the `test_environment`
table, and a per-case section (heading + steps table + any artifacts). Each case
is a steps **table** (`# | Outcome | Step | Notes`); outcomes carry a leading
emoji plus the uppercase label (e.g. `✅ PASS`). **Captured tool output and
log-file links are omitted** (the JSON result and the HTML report remain the full
record) so the Markdown stays clean. The **Step** column merges what would
otherwise be two wide columns — the step name, with the resolved detail (a
command, or a prompt with variables substituted) on a second line (`<br>`) **only
when it differs** — keeping the table to a few content-sized columns so cells are
not squeezed into a couple of words. **Artifacts** are carried through as a
trailing column — added only when some step in that table has artifacts — holding
one Markdown link per file (multiple separated by `<br>`); keeping them in the row
(not a separate per-step block) avoids repeating the step name, and links rather
than inline `![]` image embeds never render as broken alt-text when the `.md` is
viewed away from its files. **Paths are relative**, identical to the HTML
reporter, so within a bundle `report.md` and `report.html` reference the same
relocated `attachments/` files.

Tables are plain **Markdown pipe-tables**, not HTML: they render consistently
across viewers (the renderer sizes each column to its content, so the long step
text gets the most room and `#`/outcome stay slim), whereas HTML tables' width
hints (`width` attribute, `<colgroup>`) are often stripped or mis-applied by
viewers such as GitHub. Readability therefore comes from *few, merged* columns
rather than width control. Cell text is escaped for pipe-tables — newlines become
`<br>`, `|` is escaped. This affects only the Markdown reporter —
`reporting/html.py` / `report.html` is unchanged.

### 12.4 Regenerating / merging reports from JSON (`reporting/merge.py`)

Because the JSON is the canonical record, a report can be regenerated later
from saved `result.json` file(s) without re-running anything, and **several
runs can be merged into one report**:

- `merge_results(sources, chooser=first_candidate) -> dict` — `sources` is a
  list of `(json_path, result_dict)`. Cases are unioned by `id`; when an `id`
  appears in more than one source, `chooser(case_id, candidates) -> int`
  selects which run's result to keep (`candidates` are `CaseCandidate`s with
  `source`, `outcome`, `started_at`, `step_count`, … ordered **most-recent-first**
  by `started_at`, so index `0` is the newest run), or returns `SKIP_CASE`
  to **drop the test from the merge** (no result kept; if a suite is supplied it
  then shows as `NOT-TESTED` via `apply_untested`). The merged document
  recomputes `outcome`/`counts`, spans earliest start → latest finish, unions
  `test_environment`, and (for >1 source) drops suite-level setup/teardown
  (they are run-specific, not per-case). A **single** source passes through
  unchanged except for artifact-path rebasing. Artifact paths in the returned
  dict are **absolute**, ready for relocation. Passing a `contributing` list
  (keyword-only) fills it, in input order, with the source paths that won at
  least one case — sources whose every case lost a conflict are omitted, which
  is how `generate_report` collects logs only from runs that landed in the
  merged JSON.
- `relocate_artifacts(data, output_dir, *, logger)` — rewrites artifact paths
  so the report at `output_dir` displays them. Copied artifacts are grouped into
  a **per-section subdirectory** of `output_dir/attachments/` named after the
  test-case id (or `suite_setup`/`suite_teardown` for suite-level phases), so
  same-named files in different cases never clash; duplicates within one section
  still get a `_N` suffix. A file already inside `output_dir` is re-expressed
  relatively (no copy); a file elsewhere is copied into its section
  subdirectory; a missing file is logged, its label suffixed `(missing)`, its
  path cleared. This is what makes a merged report (sourced from many run dirs)
  **self-contained**.
- `collect_logs(sources, output_dir, *, logger) -> list[dict]` — copies each
  run's **loose files** (tool logs, captured device console, anything a suite
  emitted into its run dir) into `output_dir/logs/<run>/`, preserving each run's
  relative layout. It is **name-agnostic** — files are selected by structure, not
  by recognising a specific log name: everything in a source's directory is
  copied **except** the loaded `result.json`, the `attachments/` tree (handled
  by `relocate_artifacts`), the `logs/` tree (a previous merge's own collected
  output — recursing would re-copy and deeply nest the whole log history on each
  re-merge), and any HTML report (rendered output, not a log, excluded by
  extension). A source whose directory *is* the output directory (in-place
  regeneration — e.g. a `--cli-mode` run's automatically written `report.html`,
  or `report -o` pointed at its own input dir) is not copied, but is still
  **listed**: its loose files already sit exactly where the report needs to
  link them, so `_list_run_logs` finds the same files in place instead of
  `_copy_run_logs` duplicating them under `logs/`. Returns a manifest
  `[{"source": <run name>, "files": [<relpath>, …]}]` with paths relative to
  `output_dir`, keyed by each source's `json_path`. Only the runs whose results
  actually landed in the merged document are passed in — see the `contributing`
  sink on `merge_results`; a source whose every case lost a conflict contributes
  no logs.
- **Where logs appear.** Two kinds, rendered beneath each test:
  - **Per-test logs** attached via `ctx.attach_log` (e.g. *session log*) — the
    **only** logs **persisted** on a case (`case["logs"]`, each flagged
    `attached`). They are rebased + **relocated** on merge and carried forward,
    so each test keeps exactly its own precise log through repeated re-merges.
  - **Run-level loose files** of the case's source run (e.g. `curl.log`,
    `session.log`) — added per case at **render time only** by `_attach_logs`
    (via `case_sources`); `generate_report` writes `result.json` *before* this
    step, so they are **never persisted** and cannot accumulate across merges.
    (A **single**-run report lists them in one global `data["logs"]` section.)
  - **On merge**, `_rebase_artifacts` keeps only genuine per-test logs
    (`_is_attached_log`: flagged `attached`, or — for pre-flag data — a
    descriptive label rather than a filename-like one) and drops legacy
    loose-file aggregates; combined with `collect_logs` skipping a prior
    bundle's `logs/` tree, this cleans already-bloated inputs and prevents
    re-accumulation.
- `apply_untested(data, planned)` — given the suite's full case list
  (`planned` = `[{"id","name"}]`), appends a `not-tested` case (id + name only,
  **no steps** — a case that never ran has no step results) for every planned id
  absent from `data["cases"]`, re-sorts by id, and recomputes
  `outcome`/`counts`. `not-tested` is neutral in aggregation (never fails a
  suite; an all-untested suite reports `not-tested`). The HTML reporter shows
  such a case as just its heading + badge (cases with no steps render no table).
- `generate_report(result_paths, output_path, *, suite_path=None, chooser=None,
  logger=None, save_json=False, formats=None)` (`api.py`) wires it together:
  `load_result` each → `merge_results` → (if `suite_path`) `apply_untested` →
  `relocate_artifacts(output_path.parent)` → (if `save_json`) write the merged
  `result.json` → `collect_logs(output_path.parent)` → render each requested
  format. Returns the merged mapping. `chooser` defaults to keeping the first
  candidate, i.e. the **most recent** run (groups are ordered newest-first).
- **Report format(s).** `formats` selects the renderer(s), all sharing one
  prepared bundle so artifacts/logs are relocated **once**. When `None` (default)
  a single format is inferred from `output_path`'s suffix (`.md`/`.markdown` →
  `write_markdown`, else `write_html`). When a set (subset of `{"html","md"}`)
  each format is written next to `output_path` under its own extension
  (`report.html`, `report.md`). Every automatic run report (`_write_local_report`
  in `--cli-mode`, `_present_report` in web mode) requests **both**.
- **Self-contained bundles.** With `save_json=True` plus artifact relocation and
  log collection, the output directory becomes an archivable bundle —
  `report.html` + `report.md` + merged `result.json` + `attachments/` + `logs/`
  — that no longer references the source run dirs. Re-running `report` on that
  `result.json` reproduces the report even after the originals are gone.

All of `merge_results`, `relocate_artifacts`, `collect_logs`, `apply_untested`,
`CaseCandidate`, `SKIP_CASE`, `write_result_dict` and `generate_report` are
re-exported from the package.

---

## 13. CLI and entry points

```
python -m manuprompt [options] path/to/suite.yml
```

Options (all precede the suite path, which is the final positional):

| Flag | Effect |
|---|---|
| `-t, --test ID` | Run only these case id(s), **in the order given** (overrides suite id-sort). Repeatable and/or comma-separated (`-t A,B -t C`); duplicates ignored. Unknown id ⇒ error. |
| `-k, --keyword REGEX` | Select cases whose **id or name** matches `REGEX` (case-insensitive, substring by default), e.g. `-k DEMO`. Applied after `-t` (so they compose; `-t` controls order). No match / invalid regex ⇒ error. |
| `--out-dir DIR` | Directory all run output goes to — `result.json`, collected logs, attachments and the HTML report. Default: a timestamped dir under `manuprompt-results/`. **Never overwrites**: if the dir exists, a `_N` suffix is appended so prior runs are preserved. (`--artifacts` is a deprecated alias.) |
| `--var NAME=VALUE` | Override/add a suite variable (repeatable). |
| `--tool NAME=PATH` | Override/add a tool path (repeatable). |
| `--web-port PORT` | Preferred port for the WebGIO surface (default `9999`, falls back to a free port if busy). Overrides `MANUPROMPT_WEB_PORT`. |
| `--web-title NAME` | Display name for the WebGIO surface. Overrides `MANUPROMPT_WEB_TITLE`. |
| `--no-web-console` | Disable the WebGIO surface for this run (prompts then fall back to the terminal). |
| `--cli-mode` | Answer operator prompts in the **terminal** (`ConsolePrompter`) instead of the browser. The WebGIO console/artifact surface stays available. Default: prompts are shown on the web **session page**. |
| `-v, --verbose` | Debug logging. |

At the start of a run, `run_suite` prints a numbered **plan** of the test cases
that will run, in execution order (reflecting `-t`/`-k` filtering; cases marked
to skip are flagged), so the operator sees the session up front.

Exit code: `1` if the suite outcome is `FAIL`/`ERROR`, else `0`; `2` on
load/validation error.

### 13.1 `report` subcommand — render/merge HTML/Markdown from JSON

```
python -m manuprompt report FILE [FILE ...] [-s SUITE.yml] [-o OUT] [-v]
```

(Re)generates an HTML and/or Markdown report from saved JSON result(s) without
re-running.
Several JSON files are **merged** by test-case id (see §12.4); when the same id
appears in more than one file, the operator is asked interactively which run's
result to keep — candidates are listed **most-recent-first** (so `[1]`, the
default, is the newest run) — or to press **`s`** to skip that test (exclude it
from the report; with a suite it then shows as NOT-TESTED). Artifacts are copied
next to the output so they display.

- Positional `FILE`s are the result `*.json` files, plus **optionally the suite
  `*.yml`/`*.yaml`** (auto-detected by extension). When a suite is given, cases
  defined there but absent from the results are shown as **NOT-TESTED**.
- `-s, --suite PATH` — explicit suite YAML (overrides a positional one).
- `-o, --output PATH` — an `.html`/`.htm` or `.md`/`.markdown` file (the
  **format is inferred from the extension**, rendering that one), or a
  **directory** to hold *both* `report.html` and `report.md`. Default (no `-o`):
  a **fresh, dedicated bundle directory** under the current dir —
  `manuprompt-report_<timestamp>/` for one result, `manuprompt-merged_<timestamp>/`
  when merging — holding both formats, so the new report never links back into a
  past run directory.
- **Bundles (self-contained, archivable).** Whenever the output directory is not
  an input's own directory (the default always; or any `-o` pointing elsewhere),
  the (merged) `result.json` and a copy of every artifact are written into it, so
  the directory stands alone — you can archive/share it and regenerate the report
  from its `result.json` even after the source runs are gone. Only an explicit
  `-o` pointing *into* an input's own directory regenerates in place (no copy,
  original `result.json` left untouched).
- On a conflict prompt (candidates listed newest-first): a number picks that
  candidate, `s` skips the test, and EOF / empty input keeps `[1]` (the newest).
- Exit code `0` on success, `2` on a read/render error (incl. no JSON given, or
  more than one YAML).

### 13.2 Programmatic API (`api.py`, re-exported from the package)

```python
from manuprompt import load_suite, run_suite, generate_report
suite = load_suite("demo/demo-suite.yml")
result = run_suite(suite)            # browser prompts (WebPrompter), timestamped dir
# result = run_suite(suite, cli_mode=True)   # terminal prompts (ConsolePrompter)

# regenerate / merge a report from saved JSON later:
generate_report(["manuprompt-results/runA/result.json",
                 "manuprompt-results/runB/result.json"],
                "merged-report.html")
```

`run_suite(suite, *, prompter=None, artifacts_dir=None, json_path=None, logger=None, web_gio=None)`.
`generate_report(result_paths, output_path, *, suite_path=None, chooser=None, logger=None)`.

---

## 14. Example suite + glue (`demo/`)

A hardware-free reference suite that drives a public UI-testing page with
Playwright:

- `demo-suite.yml` — suite definition (suite setup/teardown, two cases mixing
  automated `call:` steps with an operator wrap-up `prompt:`, screenshot and
  session-video artifacts).
- `browser.py` — project glue called via `call: browser.*`. Launches Chromium
  (optional session video), opens the playground URL, selects `<select>`
  options by text or value, asserts status labels (`expect_text` returns
  `False` to fail the step), and returns screenshot/video paths for YAML
  `artifact:` labels. Caches the Playwright session in `ctx.resources` and
  registers teardown to close the browser.
- `requirements.txt` / `README.md` — Playwright install steps and how to run
  the demo (browser or `--cli-mode`).

This directory demonstrates the intended split: **all third-party / domain
dependency is in the glue, none in the core.**

---

## 15. Conventions and constraints

- **Python 3.12**, max line length **100**, **Google-style docstrings** on all
  public functions/classes (`Args:`/`Returns:`/`Raises:`), **full type hints**.
- Prefer modern lowercase generics (`list[str]`) in new code.
- **Core has no project-glue dependency.** Do not import hardware libraries,
  browser drivers, or other suite-local tooling from `manuprompt/*`. Such code
  belongs in suite-local `call` glue (see `demo/browser.py`).
- Logging: handlers/glue use `ctx.logger`. The standalone default logger writes
  to stdout (the core intentionally does **not** depend on any external logging
  framework).
- Model dataclasses are **frozen**; build modified copies with
  `dataclasses.replace`.
- Suites are **operator-authored and trusted**, so `tool`/`shell` steps run via
  the shell (quoting/JSON payloads work) — this is a deliberate, accepted
  execution boundary, not an oversight.
- Lint: `pylint` per repo `.pylintrc`. (Two pre-existing `noqa: BLE001` and one
  `Iterable` import warning in the package are known and harmless.)

---

## 16. Known limitations and likely extension points

These are areas a follow-up agent may be asked to work on; none are bugs.

- **No automatic assertions.** `tool`/`shell` steps judge by exit code only;
  output validation is manual (operator prompt) or custom `call` glue. A
  declarative `expect:`/match modifier could be added as a step modifier.
- **`save_output` captures stdout only** (stderr is shown/logged but not
  stored).
- **Step artifacts** come from sources that share the same report rendering
  under the step: (1) operator-supplied via the YAML `artifact:` modifier —
  browser drag-drop when WebGIO is running (`request_files`, which accepts
  **multiple files per label**, see §10.1), or a typed filesystem path on the
  console otherwise (one file per label); (2) a `call:` that returns a file
  path while the step declares `artifact:` (§9); (3) glue calling
  `ctx.attach(label, path)` during the step (§8). Case-level files use
  `ctx.attach_log(label, path)` instead (Logs section, not step artifacts).
- **`OptionalStep` is the only control-flow container.** No loops/conditionals
  on variable values; add as new container step kinds if needed.
- **Two prompter front-ends.** The `Prompter` protocol is the seam for
  alternative front-ends. `WebPrompter` (browser, default) and `ConsolePrompter`
  (terminal, `--cli-mode`) both ship; a GUI or a fully non-interactive
  (scripted/CI) prompter is a further `Prompter` implementation.
- **No parallelism / no resume.** Runs are sequential and start fresh (JSON is
  overwritten per run dir).
- **HTML report is regenerated**, not appended; it is a snapshot of the result
  tree at the time the report step runs.

When extending, preserve the contracts in §8 (RunContext), §9 (call seam),
§10 (Prompter), §11 (handler registration), and §12 (result/`to_dict` shape the
reporters consume).
