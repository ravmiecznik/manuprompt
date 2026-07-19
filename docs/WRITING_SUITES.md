# Writing a test suite in YAML

This is a practical guide to the YAML format suite authors write — what
entries exist, what's required vs. optional, and how the file is executed.
For the engine's internals, contracts, and extension points, see
[`../SPECIFICATION.md`](../SPECIFICATION.md) (§4–§9). For a runnable example
covering every feature below, see [`../demo/`](../demo/).

## The shape of a suite file

A suite file is one YAML document with two top-level keys:

```yaml
suite:
  name: My Suite          # required
  # ...setup/teardown/tools/variables...

test_cases:
  - id: DEMO001            # required
    name: What this case checks
    steps:                 # required, non-empty
      - prompt: ...
```

`suite:` is a mapping describing the run as a whole. `test_cases:` is a list
of test cases; **cases are sorted by `id`** when the suite runs, so their
order in the file doesn't matter (the `-t` CLI flag can override the run
order — see the main [README](../README.md)).

## The `suite:` block

| Key | Required | Type | Meaning |
|---|---|---|---|
| `name` | **yes** | string | Suite name shown in reports/console. |
| `description` | no | string | Free text shown in reports. |
| `web_title` | no | string | Live-console display name (banner + browser title). Falls back to `name`. |
| `theme` | no | mapping | Per-suite colour/font override, layered on top of the project-wide `theme.yaml` (see [below](#theming-the-web-ui-and-report)). |
| `variables` | no | mapping | Suite-wide `name: value` pairs, available to every case via `${name}`. Any scalar; `null` is allowed (see [Variables](#variables-and-interpolation)). The special name `test_session_password` password-protects the live web session (see [below](#password-protecting-the-live-web-session)). |
| `test_environment` | no | mapping or list | Free-form environment description rendered in reports (device, tester, firmware version, ...). See below. |
| `tools` | no | mapping | `name: path` — binaries a `tool:` step can invoke, keyed by the name used in `tool:` steps. |
| `suite_setup` | no | list of steps | Runs once, before any test case. |
| `test_setup` | no | list of steps | Runs before **every** case, before that case's own `test_setup`. |
| `test_teardown` | no | list of steps | Runs after **every** case, after that case's own `test_teardown`. |
| `suite_teardown` | no | list of steps | Runs once, after all cases (even if the run was stopped early or a setup step failed). |

`test_environment` accepts either a mapping:

```yaml
test_environment:
  Target: http://uitestingplayground.com/select
  Tester: demo
```

or a list, whose items are single-key mappings or bare scalars (a scalar
becomes a pair with an empty label):

```yaml
test_environment:
  - Target: http://uitestingplayground.com/select
  - Free-text note with no label
```

Order is preserved and both forms render the same way in reports.

`variables` example:

```yaml
variables:
  playground_url: http://uitestingplayground.com/select
  expected_label: null        # not known yet; a step will fill it in with `store`
```

### Password-protecting the live web session

`test_session_password` is a special variable name: if you set it, the whole
browser surface — session page, live console, SSE streams, prompt answers,
artifact uploads, everything — requires a password before it responds to
**any** request:

```yaml
variables:
  test_session_password: secret
```

It's checked with HTTP Basic auth (the browser prompts once and reuses the
credentials); the username is ignored, only the password has to match, and
the comparison is constant-time. With no password set, the surface is open
to anyone who can reach the port.

This still applies in `--cli-mode` — the console/artifact web surface keeps
running unless you also pass `--no-web-console`, so the password still gates
it even though prompts themselves are answered in the terminal.

The environment variable `MANUPROMPT_WEB_PASSWORD` overrides this variable at
run time (e.g. from CI secrets) without editing the YAML — set it instead of
`test_session_password` if you don't want a real password committed to the
suite file.

### Theming the web UI and report

Colour/font theming is layered in two places, both optional:

1. **Project-wide — `theme.yaml`**, a plain YAML file living next to
   `theme.py` at the tool's own root (i.e. the root of wherever you've
   checked out/copied `manuprompt` — see [`../demo/README.md`](../demo/README.md)
   for why the whole checkout is expected to live at your project's root).
   This is the normal place to set a house style once, for every suite. It
   accepts two shapes:

   - **Flat** — the fields directly at the top level, as a single theme:

     ```yaml
     # theme.yaml
     accent: "#c678dd"
     success: "#98c379"
     danger: "#e06c75"
     warning: "#e5c07b"
     font_family: "Segoe UI, Helvetica Neue, Helvetica, Arial, sans-serif"
     ```

   - **Named presets** — define several palettes and pick one with an
     `apply:` key; switching themes is then just editing that one line:

     ```yaml
     # theme.yaml
     green:
       accent: "#3ecf9a"
       background: "#132420"
       # ...
     solarized:
       accent: "#268bd2"
       background: "#002b36"
       # ...
     apply: green   # <- the active preset
     ```

     Every preset is validated up front — even ones `apply:` doesn't
     currently select — so a typo in a preset you're not using yet is still
     caught immediately rather than surprising you later when you switch to
     it. `apply:` naming an undefined preset, or presets defined with no
     `apply:` key at all, both raise a clear error.

   See [`../theme.yaml`](../theme.yaml) for a live example with three presets
   (`green`/`blue`/`solarized`).

2. **Per-suite — `theme:`** in the suite's own YAML, overriding individual
   fields for just that suite on top of `theme.yaml`:

   ```yaml
   suite:
     theme:
       mono_font_family: "Cascadia Code, Consolas, Menlo, monospace"
   ```

Both apply to **both surfaces** at once — the live web UI (session/console/
artifact pages) and the HTML/Markdown report — each resolved against its own
base palette, so overriding only `accent`/`success`/`danger`/`warning`
retints both consistently while leaving the web UI dark and the report light,
as before. Every field in both layers is optional; with neither present,
everything renders exactly as it always has (see the full field list below).
The combined, effective theme is saved into the run's `result.json`, so
regenerating or merging a report later (`manuprompt report ...`) keeps the
suite's look even without the original `theme.yaml`/suite YAML.

The full field list:

```yaml
accent: "#c678dd"      # active tab, links, focus ring, primary highlight
success: "#98c379"     # pass
danger: "#e06c75"      # fail / error
warning: "#e5c07b"     # ack
background: "#1e1e2e"  # page background
surface: "#282838"     # panel/header/card background
foreground: "#f8f8f2"  # main text
muted: "#a0a0b0"       # secondary text, borders
border: "#3a3a4a"
font_family: "Segoe UI, Helvetica Neue, Helvetica, Arial, sans-serif"
mono_font_family: "Cascadia Code, Consolas, Menlo, monospace"
```

**Fonts** are plain CSS `font-family` values — a comma-separated fallback
list of font names, exactly like any CSS stylesheet. There is **no embedded
font file or web-font/CDN request**: the core package has no third-party
dependency and every page must stay a single, self-contained, offline-capable
document. This means:

- The browser tries each name in `font_family`/`mono_font_family` left to
  right and renders with the first one actually installed on the operator's
  machine, falling back to the next (and ultimately to the generic
  `sans-serif`/`monospace` keyword at the end of the list, which is always
  available).
- A font only renders if it's installed on the machine viewing the page —
  the same suite may render in slightly different fonts for different
  operators/OSes if you pick something not universally available. Stick to
  fonts that ship with the major OSes (e.g. `Segoe UI` on Windows, `Helvetica
  Neue`/`-apple-system` on macOS, `Ubuntu`/`Cantarell` on common Linux
  desktops) and always end the stack with a generic fallback
  (`sans-serif`/`monospace`), as in the example above.
- The defaults (`ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`
  for the web UI; `system-ui, sans-serif` for the report) already follow this
  pattern — each name is a close equivalent on a different OS, so *something*
  reasonable renders everywhere without asking the operator to install
  anything.

See [`../demo/demo-suite.yml`](../demo/demo-suite.yml) for a themed example,
and [`../SPECIFICATION.md`](../SPECIFICATION.md) §5.1 / `theme.py` for the
full field list and how the override is resolved.

## The `test_cases:` list

Each entry:

| Key | Required | Type | Meaning |
|---|---|---|---|
| `id` | **yes** | string | Case identifier (e.g. `DEMO002`). Used for sort order and the `-t`/`-k` CLI filters. |
| `name` | no | string | Human-readable title (falls back to `id` if omitted). |
| `description` | no | string | Free text shown next to the case in the UI and in reports. |
| `steps` | **yes**, non-empty | list of steps | The case body. |
| `skip` | no | string, or mapping with `reason` | Marks the case to be skipped by default (see below). |
| `variables` | no | mapping | Case-scoped variables; overlay/override suite `variables` for this case only. |
| `test_setup` | no | list of steps | Runs **after** the suite's `test_setup`, before `steps`. |
| `test_teardown` | no | list of steps | Runs **before** the suite's `test_teardown`, after `steps`. |

```yaml
test_cases:
  - id: DEMO002
    name: Test dropdown selection by value
    description: >
      Partially automated check that selecting a product version by value
      updates the status label.
    skip:
      reason: https://jira.example/DEMO-42   # a bare string works too: `skip: some reason`
    variables:
      expected_label: null
    steps:
      - prompt: Enter the expected status label to assert later.
        store: expected_label
```

A case marked `skip:` still shows up in the plan and the report; at run time
the operator is asked "run it anyway?" (default: no) instead of it running
silently.

## Steps

A step is a mapping with **exactly one action key** — that's what determines
its kind. Every step may also carry `artifact` and `note` (see below).

| Action key | Kind | Extra modifiers | What it does |
|---|---|---|---|
| `prompt:` | manual | `store`, `name` | Show text to the operator; record a PASS/FAIL/acknowledge verdict, or capture typed input. |
| `tool:` | automated | `command`, `save_output`, `name` | Run a binary from `suite.tools`, judged by exit code. |
| `shell:` | automated | `save_output`, `name` | Run an arbitrary shell command, judged by exit code. |
| `call:` | automated | `args`, `name` | Call `module.function` in a Python file next to the suite YAML. |
| `optional:` | control-flow | `message` | A group of nested steps the operator may opt into or skip. |

Only these five action keys and their listed modifiers (plus `artifact`,
`note`, and the implicit action key itself) are allowed on a step — anything
else raises a validation error naming the unexpected key.

### `prompt:` — ask the operator

```yaml
# Verdict: operator answers pass / fail / acknowledge
- prompt: Does the dropdown look correct in the screenshot?

# Captured input: free text saved into a variable for later ${interpolation}
- prompt: Enter the expected status label to assert later.
  store: expected_label
```

If `store` is set, the step captures whatever the operator types (no verdict
is asked); otherwise the operator gives a PASS/FAIL/acknowledge verdict with
an optional note.

### `tool:` — run a configured binary

```yaml
# suite.tools must define `curl: <path>`
- tool: curl
  command: -s -o /dev/null -w "%{http_code}" ${playground_url}
  save_output: http_status
```

`tool` names a key in `suite.tools`; `command` (required) is appended to that
binary's path. The step passes/fails by exit code. `save_output` captures
**stdout only** into a variable (stderr is still shown/logged, not stored).

### `shell:` — run an arbitrary command

```yaml
- shell: date +%Y-%m-%d
```

Runs verbatim through the shell (pipes, globs, redirects all work) — suites
are operator-authored and trusted, so this is a deliberate capability, not an
oversight. Same `save_output` behaviour as `tool:`.

### `call:` — project-specific glue code

```yaml
- call: browser.select_by_text
  args:
    selector: "#selectLanguage"
    text: Python
```

`call` is `module.function` (exactly one dot). `module` resolves to
`module.py` **next to the suite YAML**; `function` is called as
`function(ctx, **args)`, with any string values in `args` resolved for
`${var}` first. This is the *only* place project-specific code plugs in — see
[`../SPECIFICATION.md`](../SPECIFICATION.md) §9 for the full contract
(`ctx`'s API, return-value → outcome mapping, error handling).

To attach a file the call produces (e.g. a screenshot), declare `artifact:` on
the step and **return the file path** from the function — the label lives in
the YAML; glue only captures the file:

```yaml
- call: browser.screenshot
  artifact: language dropdown
```

For case-scoped evidence (session logs, etc.), glue can still call
`ctx.attach_log(label, path)`. Low-level `ctx.attach(label, path)` remains
available when a step needs to attach without returning a path.

### `optional:` — an operator-gated group

```yaml
- optional:
    - call: browser.screenshot
      artifact: optional snapshot
    - prompt: Does the extra snapshot look useful?
  message: Capture an extra screenshot before continuing?
```

The operator is asked `message` (default: yes). If they opt in, the nested
steps run in order in the current scope; if not, they're all recorded as
skipped. `optional:` groups can nest.

### `artifact:` and `note:` — allowed on any step

```yaml
- prompt: Does the dropdown look correct in the screenshot?
  artifact: operator snapshot        # one label — operator supplies the file
  # artifact: [operator snapshot, notes.txt]   # or several — each is a separate request
  note: Compare the status label under the Product Version dropdown.

# call that returns a file path — no operator prompt; the returned file is attached
- call: browser.screenshot
  artifact: language dropdown
```

- `artifact:` declares one or more labels for files stored under this step and
  rendered in the report (images inline, videos playable, other types as
  download links).
- `note:` is an author's annotation shown to the operator when the step runs
  and recorded in the report as a highlighted callout. Supports
  `${var}` like everything else.

#### How `artifact:` files are supplied

After the step's action finishes, each label is satisfied in order:

1. **Already attached during the step** — `ctx.attach`, or a `call:` that
   returned an existing file path (or list of paths). Those labels are done;
   the operator is not prompted for them.
2. **Otherwise the operator supplies the file(s):**

| Mode | How the operator supplies the file |
|---|---|
| **Browser surface running** (default, and also in `--cli-mode` unless `--no-web-console`) | Drag-drop / file picker / paste on the Artifacts page (`/artifact`). One request per label; a single label can accept **several** files, then Done (or Skip). |
| **No web console** (`--no-web-console`, or web surface otherwise unavailable) | Console prompt for a filesystem path (one file per label). Surrounding quotes and backslash-escaped spaces (e.g. from dragging a file into the terminal) are accepted. Empty input skips. |

So: with the live web UI up, collection prefers the browser even when prompts
themselves are answered in the terminal (`--cli-mode`). Use `--no-web-console`
for a fully headless/console path (important for non-interactive agents — see
the main [README](../README.md)).

For a `call:` with one `artifact:` label, returning one path or a list of
paths attaches all of those files under that label. With several labels,
paths are paired with labels in order; any leftover labels still ask the
operator.

### `name:` — override the auto-derived label

Every step kind accepts `name:` to override the label shown in
console/report; otherwise the label is derived from the step's own content
(e.g. the full prompt text, or `tool command`).

## Variables and interpolation

Write `${name}` inside any `prompt`, `command`, or string `call` argument; it
is substituted at run time. Variables come from three places, layered:

1. `suite.variables` — visible to every case.
2. A case's own `variables` — overlays/overrides the suite ones, for that
   case only.
3. Anything a step writes at run time: `prompt` + `store`, `tool`/`shell` +
   `save_output`, or `call` glue writing via `ctx.set_var`.

Referencing a variable that was never declared, or is still `null`, raises an
error for that step (recorded as `ERROR` in the report) — this is why
`variables: { expected_label: null }` is the idiom for "a later step fills this
in": it declares the name so `${expected_label}` doesn't look like a typo, and
fails loudly if something tries to read it before it's set.

Variables set during `suite_setup` are visible to every case. Anything a case
sets is scoped to that case only and does not leak into the next one.

## How a suite actually runs

```
[web mode only] operator picks which planned cases to run (all checked by default)
suite_setup                                  (once)
for each selected case, sorted by id:
    suite.test_setup  + case.test_setup       (blocking: a failure here skips `steps` and teardown-guarded cleanup still runs)
    case.steps
    case.test_teardown + suite.test_teardown
suite_teardown                                (once, even on early stop or a setup failure)
```

Practically, this means:

- **In the browser (the default), the very first thing the operator sees is
  a checklist of every case the suite is about to run**, all selected by
  default, with a button to start. Unchecking a case skips it — it still
  shows up in the report, marked `SKIP`, just as if the operator had declined
  it individually. In `--cli-mode` this screen doesn't appear — the console
  already prints the plan up front and lets the operator skip cases one at a
  time as they come up (see below).
- A **case's own** `test_setup`/`test_teardown` nests *inside* the suite's —
  suite setup/teardown always wraps the outside.
- If a setup phase fails or errors, the steps that depend on it are recorded
  as skipped, but teardown for that scope still runs.
- Before each selected case the operator sees a preview of its steps and can
  still run or skip it; after each case they see every step's outcome and can
  proceed, repeat the case, or stop the whole run early (teardown and the
  report still happen on stop).

See [`../SPECIFICATION.md`](../SPECIFICATION.md) §6 for the full execution
model (variable scoping, error boundaries, guaranteed teardown).

## Validation errors

The loader validates the whole file before running anything and raises a
message that names exactly where the problem is, e.g.:

```
test_cases[0] (DEMO002).steps[0] has unknown key(s) ['storee'] for a 'prompt' step; allowed: ['artifact', 'name', 'note', 'prompt', 'store']
```

Common causes: a typo in a modifier name, two action keys on one step (e.g.
both `prompt:` and `tool:`), a `call:` target without exactly one dot, or a
case with an empty `steps:` list.

## A complete example

The runnable reference is [`../demo/demo-suite.yml`](../demo/demo-suite.yml)
(plus [`../demo/browser.py`](../demo/browser.py) glue). A shortened form:

```yaml
suite:
  name: ManuPrompt Demo (Playwright Web UI)
  web_title: ManuPrompt Demo
  description: >
    Drive a public dropdown-testing page with Playwright: automated
    selection and assertion steps, then an operator wrap-up prompt.

  test_environment:
    Target: http://uitestingplayground.com/select
    Tester: demo

  variables:
    playground_url: http://uitestingplayground.com/select

  suite_setup:
    - call: browser.launch
      args:
        url: ${playground_url}
        headless: true
        record_video: true
      name: Launch Chromium and open the playground page

  suite_teardown:
    - call: browser.stop_video
      artifact: session recording
      name: Stop browser video recording and attach it
    - prompt: >
        Demo finished. Click "Stop & save report" (or proceed) to view and
        download the report.
      name: Wrap up the demo

test_cases:
  - id: DEMO001
    name: Test dropdown selection by visible text [Programming Language]
    steps:
      - call: browser.select_by_text
        args:
          selector: "#selectLanguage"
          text: Python
        name: Select "Python" in the Language dropdown
      - call: browser.expect_text
        args:
          selector: "#statusLanguage"
          expected: "Selected: Python (value: py)"
        name: Assert the statusLanguage label updated correctly
      - call: browser.screenshot
        artifact: language dropdown
        name: Attach a screenshot of the result

  - id: DEMO002
    name: Test dropdown selection by value [Product Version]
    skip:
      reason: https://jira.example/DEMO-42
    steps:
      - optional:
          - call: browser.screenshot
            artifact: optional snapshot
          - prompt: Does the extra snapshot look useful?
        message: Capture an extra screenshot before continuing?
      - call: browser.select_by_value
        args:
          selector: "#selectProduct"
          value: v2.1
        name: Select value "v2.1" in the Product Version dropdown
      - call: browser.screenshot
        artifact: product version dropdown
        name: Attach a screenshot of the result
      - call: browser.expect_text
        args:
          selector: "#statusProduct"
          expected: "Selected: Release 2.1 (value: v2.1)"
        name: Assert the statusProduct label updated correctly
```

For the full runnable suite (install steps, how each file fits), see
[`../demo/`](../demo/).
