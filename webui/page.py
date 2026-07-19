"""The self-contained HTML page served by the live web UI.

The page has no external assets: inline CSS and JS only. It discovers channels
by polling ``/channels`` and opens one ``EventSource`` per channel against
``/stream/<name>``, appending streamed text to an auto-scrolling terminal-style
pane. A "Clear" button clears the current view, and channels that accept input
(``{"input": true}`` in ``/channels``) get an input field that POSTs to
``/input/<name>``. The page is intentionally generic — it knows nothing about
what a channel carries or what its input does.
"""

from __future__ import annotations

from string import Template

from ..theme import WEB_DEFAULTS, Theme, css_variables

# Shared CSS for chrome common to every page (header/buttons/tabs/console
# panes/input rows), expressed in terms of the theme's CSS custom properties
# so every page stays in sync when themed. Control backgrounds/hover shades
# and tinted badges are derived from the small palette via ``color-mix()``
# rather than exposed as extra theme fields, keeping the suite-facing config
# small while still retinting consistently.
_CHROME_CSS = """
  * { box-sizing: border-box; }
  body {
    margin: 0; height: 100vh; display: flex; flex-direction: column;
    background: var(--bg); color: var(--fg); font-family: var(--font);
  }
  header {
    padding: 8px 12px; background: var(--surface); border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 12px; flex: 0 0 auto;
  }
  header h1 { font-size: 14px; margin: 0; font-weight: 600; }
  header .status { font-size: 12px; color: var(--muted); margin-left: auto; }
  button {
    background: color-mix(in srgb, var(--surface) 50%, var(--border) 50%);
    color: var(--fg); border: 1px solid var(--border);
    border-radius: 6px; padding: 4px 10px; font: inherit; font-size: 12px;
    cursor: pointer;
  }
  button:hover { background: var(--border); }
  button.primary {
    background: color-mix(in srgb, var(--success) 70%, black 30%);
    border-color: var(--success);
  }
  button.primary:hover { background: color-mix(in srgb, var(--success) 85%, black 15%); }
  button.danger {
    background: color-mix(in srgb, var(--danger) 55%, black 45%);
    border-color: var(--danger);
  }
  button.danger:hover { background: color-mix(in srgb, var(--danger) 70%, black 30%); }
  #tabs { display: flex; gap: 4px; flex: 0 0 auto;
          background: var(--surface); padding: 0 8px; border-bottom: 1px solid var(--border); }
  #tabs button {
    background: transparent; color: var(--muted); border: none; border-radius: 0;
    padding: 8px 14px; font-size: 13px; border-bottom: 2px solid transparent;
  }
  #tabs button:hover { color: var(--fg); background: transparent; }
  #tabs button.active { color: var(--accent); border-bottom-color: var(--accent); }
  #panes { flex: 1 1 auto; position: relative; overflow: hidden; }
  .channel { position: absolute; inset: 0; display: none; flex-direction: column; }
  .channel.active { display: flex; }
  pre.pane {
    flex: 1 1 auto; margin: 0; padding: 12px; overflow: auto;
    white-space: pre-wrap; word-break: break-word; font-size: 13px; line-height: 1.4;
    font-family: var(--mono-font);
  }
  form.inputrow {
    flex: 0 0 auto; display: flex; align-items: center; gap: 8px;
    padding: 8px 12px; background: var(--surface); border-top: 1px solid var(--border);
  }
  form.inputrow .prompt { color: var(--accent); font-weight: 700; }
  form.inputrow input {
    flex: 1 1 auto; background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px;
    font: inherit; font-size: 13px;
  }
  form.inputrow input:focus { outline: none; border-color: var(--accent); }
  #empty { padding: 24px; color: var(--muted); }
"""

# Banner shown for engine-published "notices" (e.g. an unhandled exception),
# so a failure is visible in the browser instead of only in the CLI/log —
# appended to _CHROME_CSS (below) and duplicated in the artifact page, which
# doesn't otherwise use _CHROME_CSS.
_NOTICE_CSS = """
  #notices { flex: 0 0 auto; }
  .notice {
    padding: 8px 12px; font-size: 13px; display: flex; gap: 10px; align-items: center;
    background: color-mix(in srgb, var(--danger) 25%, var(--bg) 75%);
    border-bottom: 1px solid var(--danger); color: var(--fg);
  }
  .notice .tag { font-weight: 700; color: var(--danger); flex: 0 0 auto; white-space: nowrap; }
  .notice .msg { flex: 1 1 auto; word-break: break-word; }
  .notice button {
    flex: 0 0 auto; background: transparent; border: 1px solid var(--danger);
    color: var(--fg); line-height: 1; padding: 2px 8px;
  }
  .notice button:hover { background: color-mix(in srgb, var(--danger) 30%, transparent); }
  .notice.warning {
    background: color-mix(in srgb, var(--warning) 25%, var(--bg) 75%);
    border-bottom-color: var(--warning);
  }
  .notice.warning .tag { color: var(--warning); }
  .notice.warning button { border-color: var(--warning); }
"""

_CHROME_CSS += _NOTICE_CSS

# JS shared by every page to poll and render engine-published notices (see
# _NOTICE_CSS above). Assumes a `<div id="notices">` exists in the body.
_NOTICE_JS = """
const noticesEl = document.getElementById('notices');
const dismissedNotices = new Set();
function renderNotices(list) {
  noticesEl.innerHTML = '';
  for (const n of list) {
    if (dismissedNotices.has(n.id)) continue;
    const div = document.createElement('div');
    div.className = 'notice' + (n.level === 'warning' ? ' warning' : '');
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = n.level === 'warning' ? '\\u26a0 warning' : '\\u26a0 error';
    const msg = document.createElement('span');
    msg.className = 'msg';
    msg.textContent = n.message;
    const close = document.createElement('button');
    close.textContent = '\\u00d7';
    close.title = 'Dismiss';
    close.onclick = () => { dismissedNotices.add(n.id); div.remove(); };
    div.appendChild(tag); div.appendChild(msg); div.appendChild(close);
    noticesEl.appendChild(div);
  }
}
async function pollNotices() {
  try {
    renderNotices(await (await fetch('notices', {cache: 'no-store'})).json());
  } catch (e) { /* transient; retry on next tick */ }
}
pollNotices();
setInterval(pollNotices, 2000);
"""

_PAGE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>
  :root {
    color-scheme: dark;
$theme_vars
  }
$chrome_css
</style>
</head>
<body>
<header>
  <h1>$title</h1>
  <button id="clear" title="Clear the current view">Clear</button>
  <span class="status" id="status">connecting&hellip;</span>
</header>
<div id="notices"></div>
<div id="tabs"></div>
<div id="panes"><div id="empty">Waiting for channels&hellip;</div></div>
<script>
const tabsEl = document.getElementById('tabs');
const panesEl = document.getElementById('panes');
const emptyEl = document.getElementById('empty');
const statusEl = document.getElementById('status');
const clearBtn = document.getElementById('clear');
const channels = new Map();  // name -> {tab, wrap, pane, source, hasInput}
let active = null;

function selectChannel(name) {
  active = name;
  for (const [n, c] of channels) {
    const on = n === name;
    c.tab.classList.toggle('active', on);
    c.wrap.classList.toggle('active', on);
  }
}

function atBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
}

function addInputRow(c, name) {
  if (c.hasInput) return;
  c.hasInput = true;
  const form = document.createElement('form');
  form.className = 'inputrow';
  const prompt = document.createElement('span');
  prompt.className = 'prompt';
  prompt.textContent = '›';
  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = 'Type a command and press Enter';
  input.autocomplete = 'off';
  form.appendChild(prompt);
  form.appendChild(input);
  form.onsubmit = (ev) => {
    ev.preventDefault();
    const value = input.value;
    if (!value) return;
    input.value = '';
    fetch('input/' + encodeURIComponent(name), {method: 'POST', body: value})
      .catch(() => { statusEl.textContent = 'input failed'; });
  };
  c.wrap.appendChild(form);
}

function addChannel(meta) {
  const name = meta.name;
  if (channels.has(name)) {
    if (meta.input) addInputRow(channels.get(name), name);
    return;
  }
  if (emptyEl) emptyEl.style.display = 'none';

  const tab = document.createElement('button');
  tab.textContent = name;
  tab.onclick = () => selectChannel(name);
  tabsEl.appendChild(tab);

  const wrap = document.createElement('div');
  wrap.className = 'channel';
  const pane = document.createElement('pre');
  pane.className = 'pane';
  wrap.appendChild(pane);
  panesEl.appendChild(wrap);

  const source = new EventSource('stream/' + encodeURIComponent(name));
  source.onmessage = (ev) => {
    const stick = atBottom(pane);
    pane.appendChild(document.createTextNode(ev.data));
    if (stick) pane.scrollTop = pane.scrollHeight;
  };
  source.onopen = () => { statusEl.textContent = 'connected'; };
  source.onerror = () => { statusEl.textContent = 'reconnecting…'; };

  const c = {tab, wrap, pane, source, hasInput: false};
  channels.set(name, c);
  if (meta.input) addInputRow(c, name);
  if (active === null) selectChannel(name);
}

clearBtn.onclick = () => {
  const c = channels.get(active);
  if (c) c.pane.textContent = '';
};

async function pollChannels() {
  try {
    const resp = await fetch('channels', {cache: 'no-store'});
    for (const meta of await resp.json()) addChannel(meta);
  } catch (e) { /* transient; retry on next tick */ }
}

pollChannels();
setInterval(pollChannels, 2000);
"""
    + _NOTICE_JS
    + """
</script>
</body>
</html>
"""
)


def render_page(title: str, theme: Theme | None = None) -> str:
    """Render the live web-UI page.

    Args:
        title: Title shown in the browser tab and page header.
        theme: Colour/font overrides (see :mod:`theme`); unset fields keep the
            page's built-in dark terminal look.

    Returns:
        A complete, self-contained HTML document.
    """
    return _PAGE.substitute(
        title=title,
        theme_vars=css_variables(theme or Theme(), WEB_DEFAULTS),
        chrome_css=_CHROME_CSS,
    )


_ARTIFACT_PAGE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title — artifacts</title>
<style>
  :root {
    color-scheme: dark;
$theme_vars
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; background: var(--bg); color: var(--fg);
    font-family: var(--font);
  }
  header {
    padding: 8px 12px; background: var(--surface); border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 12px;
  }
  header h1 { font-size: 14px; margin: 0; font-weight: 600; }
  header .status { font-size: 12px; color: var(--muted); margin-left: auto; }
  #requests { padding: 16px; display: flex; flex-direction: column; gap: 16px; }
  #idle { color: var(--muted); }
  .request {
    border: 1px solid var(--border); border-radius: 8px; background: var(--surface);
    padding: 16px; display: flex; flex-direction: column; gap: 12px;
  }
  .request .label { font-size: 14px; font-weight: 600; }
  .drop {
    border: 2px dashed var(--border); border-radius: 8px; padding: 28px 16px;
    text-align: center; color: var(--muted); cursor: pointer; transition: all .12s;
  }
  .drop:hover { border-color: var(--accent); color: var(--fg); }
  .drop.over {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 15%, var(--bg) 85%);
    color: var(--fg);
  }
  .row { display: flex; gap: 8px; align-items: center; }
  button {
    background: color-mix(in srgb, var(--surface) 50%, var(--border) 50%);
    color: var(--fg); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 12px; font: inherit; font-size: 13px;
    cursor: pointer;
  }
  button:hover { background: var(--border); }
  button.primary {
    background: color-mix(in srgb, var(--success) 70%, black 30%);
    border-color: var(--success);
  }
  button.primary:hover { background: color-mix(in srgb, var(--success) 85%, black 15%); }
  .row { margin-top: auto; }
  .done { color: var(--success); } .fail { color: var(--danger); }
  .files { list-style: none; margin: .2rem 0 0; padding: 0; }
  .files li { font-size: 13px; color: var(--muted); padding: .15rem 0; }
  .files li.ok::before { content: "\\2713 "; color: var(--success); }
  .files li.fail::before { content: "\\2717 "; color: var(--danger); }
$notice_css
</style>
</head>
<body>
<header>
  <h1>$title — artifacts</h1>
  <span class="status" id="status">waiting for requests&hellip;</span>
</header>
<div id="notices"></div>
<div id="requests"><div id="idle">No artifact requested right now. This page will update when a step asks for one.</div></div>
<script>
const reqEl = document.getElementById('requests');
const idleEl = document.getElementById('idle');
const statusEl = document.getElementById('status');
const cards = new Map();  // id -> {card, upload}
let activeId = null;      // card paste (Ctrl+V) targets

function addCard(req) {
  if (cards.has(req.id)) return;
  if (idleEl) idleEl.style.display = 'none';
  const id = req.id;
  activeId = id;
  const inflight = [];  // upload promises, so Done waits for them to land

  const card = document.createElement('div');
  card.className = 'request';
  card.onclick = () => { activeId = id; };
  const label = document.createElement('div');
  label.className = 'label';
  label.textContent = 'Attach: ' + req.label;

  const drop = document.createElement('div');
  drop.className = 'drop';
  drop.textContent = 'Drag files here, click to choose, or paste (Ctrl+V) — several allowed';
  const input = document.createElement('input');
  input.type = 'file';
  input.multiple = true;
  input.style.display = 'none';

  const files = document.createElement('ul');
  files.className = 'files';

  function upload(file) {
    const li = document.createElement('li');
    li.textContent = file.name + ' …';
    files.appendChild(li);
    const p = fetch('artifact/' + encodeURIComponent(id), {
      method: 'POST', body: file, headers: {'X-Filename': encodeURIComponent(file.name)}
    }).then((r) => {
      li.textContent = file.name; li.className = r.ok ? 'ok' : 'fail';
    }).catch(() => { li.textContent = file.name + ' (failed)'; li.className = 'fail'; });
    inflight.push(p);
  }
  function uploadAll(list) { for (const f of list) upload(f); }

  drop.onclick = () => { activeId = id; input.click(); };
  input.onchange = () => uploadAll(input.files);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = (e) => {
    e.preventDefault(); drop.classList.remove('over');
    uploadAll(e.dataTransfer.files);
  };

  const row = document.createElement('div');
  row.className = 'row';
  const done = document.createElement('button');
  done.className = 'primary';
  done.textContent = 'Done';
  done.onclick = () => {
    done.disabled = true; drop.textContent = 'Finishing…';
    // Wait for every upload to land before finishing, else files are lost.
    Promise.allSettled(inflight).then(() => {
      fetch('artifact/' + encodeURIComponent(id) + '/done', {method: 'POST'});
      drop.textContent = 'Done';
    });
  };
  const skip = document.createElement('button');
  skip.textContent = 'Skip';
  skip.onclick = () => {
    done.disabled = true;
    fetch('artifact/' + encodeURIComponent(id) + '/skip', {method: 'POST'});
    drop.textContent = 'Skipped';
  };
  row.appendChild(done);
  row.appendChild(skip);

  card.appendChild(label);
  card.appendChild(drop);
  card.appendChild(input);
  card.appendChild(files);
  card.appendChild(row);
  reqEl.appendChild(card);
  cards.set(id, {card, upload});
}

// Paste (Ctrl+V) anywhere routes clipboard content to the active request:
// pasted files/images upload directly; pasted text becomes a .txt file.
function activeEntry() {
  if (activeId && cards.has(activeId)) return cards.get(activeId);
  return cards.size === 1 ? [...cards.values()][0] : null;
}
function extFor(type) { return (type && type.split('/')[1]) || 'bin'; }
document.addEventListener('paste', (e) => {
  const entry = activeEntry();
  if (!entry || !e.clipboardData) return;
  let handled = false;
  for (const item of e.clipboardData.items) {
    if (item.kind === 'file') {
      const f = item.getAsFile();
      if (f) {
        const named = f.name ? f : new File([f], 'pasted.' + extFor(f.type), {type: f.type});
        entry.upload(named); handled = true;
      }
    }
  }
  if (!handled) {
    const text = e.clipboardData.getData('text/plain');
    if (text) {
      entry.upload(new File([text], 'pasted.txt', {type: 'text/plain'}));
      handled = true;
    }
  }
  if (handled) e.preventDefault();
});

async function poll() {
  try {
    const pending = await (await fetch('artifacts/pending', {cache: 'no-store'})).json();
    statusEl.textContent = 'connected';
    const ids = new Set(pending.map((r) => r.id));
    for (const req of pending) addCard(req);
    // Remove cards whose request is no longer pending (finished/skipped).
    for (const [id, c] of cards) {
      if (!ids.has(id)) {
        setTimeout(() => { c.card.remove(); }, 1200);
        cards.delete(id);
      }
    }
    if (cards.size === 0 && idleEl) idleEl.style.display = '';
  } catch (e) { statusEl.textContent = 'reconnecting…'; }
}

poll();
setInterval(poll, 1000);
"""
    + _NOTICE_JS
    + """
</script>
</body>
</html>
"""
)


def render_artifact_page(title: str, theme: Theme | None = None) -> str:
    """Render the drag-drop artifact-upload page.

    Args:
        title: Title shown in the browser tab and page header.
        theme: Colour/font overrides (see :mod:`theme`); unset fields keep the
            page's built-in dark terminal look.

    Returns:
        A complete, self-contained HTML document.
    """
    return _ARTIFACT_PAGE.substitute(
        title=title,
        theme_vars=css_variables(theme or Theme(), WEB_DEFAULTS),
        notice_css=_NOTICE_CSS,
    )


_SESSION_PAGE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title — session</title>
<style>
  :root {
    color-scheme: dark;
$theme_vars
  }
$chrome_css
  header nav { display: flex; gap: 8px; }
  header nav a {
    color: var(--muted); text-decoration: none; font-size: 12px;
    padding: 4px 10px; border: 1px solid var(--border); border-radius: 6px;
  }
  header nav a:hover {
    color: var(--fg);
    background: color-mix(in srgb, var(--surface) 50%, var(--border) 50%);
  }
  header nav a.pending {
    color: var(--warning); border-color: var(--warning);
    background: color-mix(in srgb, var(--warning) 20%, var(--bg) 80%);
    font-weight: 600;
  }
  main { flex: 1 1 auto; display: flex; flex-direction: column; overflow: hidden; }
  #casezone { flex: 0 0 auto; padding: 12px; overflow: auto; height: 45%; }
  #split {
    flex: 0 0 7px; background: color-mix(in srgb, var(--surface) 50%, var(--border) 50%);
    cursor: row-resize;
    border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
  }
  #split:hover, #split.dragging { background: color-mix(in srgb, var(--accent) 40%, transparent); }
  #idlecase { color: var(--muted); padding: 8px; }
  #casetitle { font-size: 15px; font-weight: 600; margin: 0 0 2px; }
  #phasebar { font-size: 12px; color: var(--muted); margin: 0 0 10px; }
  ol#steplist { list-style: none; margin: 0; padding: 0; }
  #steplist li {
    display: flex; flex-direction: column; gap: 6px; padding: 6px 8px;
    border-radius: 6px; font-size: 14px; border: 1px solid transparent;
  }
  #steplist li .head { display: flex; gap: 10px; align-items: baseline; }
  #steplist li.expandable .head { cursor: pointer; }
  #steplist li.expandable:hover { background: var(--surface); }
  #steplist li .ico { flex: 0 0 18px; text-align: center; }
  #steplist li .name { flex: 1 1 auto; word-break: break-word; }
  #steplist li.expandable { background: color-mix(in srgb, var(--surface) 60%, var(--bg) 40%); }
  #steplist li .outbadge {
    flex: 0 0 auto; font-size: 11px; color: var(--accent); white-space: nowrap;
    border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border) 65%);
    border-radius: 10px; padding: 1px 8px;
    background: color-mix(in srgb, var(--accent) 15%, var(--bg) 85%);
  }
  #steplist li.expandable:hover {
    background: color-mix(in srgb, var(--surface) 75%, var(--bg) 25%);
  }
  #steplist li.expandable:hover .outbadge {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 25%, var(--bg) 75%);
  }
  #steplist li .detail { color: var(--muted); font-size: 12px; }
  #steplist li.optional .name { font-style: italic; }
  #steplist li.pending { color: var(--muted); }
  #steplist li.pending .ico { color: var(--muted); }
  #steplist li.pass .ico { color: var(--success); }
  #steplist li.fail .ico, #steplist li.error .ico { color: var(--danger); }
  #steplist li.ack .ico { color: var(--warning); }
  #steplist li.skip { color: var(--muted); }
  #steplist li.active {
    background: color-mix(in srgb, var(--accent) 15%, var(--bg) 85%);
    border-color: var(--accent); gap: 8px; padding: 12px;
  }
  #steplist li.active .ico { color: var(--accent); }
  pre.stepout {
    margin: 4px 0 2px; padding: 10px; background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; max-height: 320px; overflow: auto; white-space: pre-wrap;
    word-break: break-word; font-size: 12px; line-height: 1.4; font-family: var(--mono-font);
  }
  .prompttext { font-size: 15px; white-space: pre-wrap; word-break: break-word; }
  .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .controls input {
    flex: 1 1 auto; min-width: 180px; background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px;
    font: inherit; font-size: 13px;
  }
  .controls input:focus { outline: none; border-color: var(--accent); }
  #cardzone { padding: 0 4px; }
  .card {
    border: 1px solid var(--accent); border-radius: 8px; background: var(--surface);
    padding: 16px; display: flex; flex-direction: column; gap: 12px;
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 20%, transparent);
  }
  .card .kind { font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
                color: var(--muted); }
  .card .text { font-size: 15px; white-space: pre-wrap; word-break: break-word; }
  .card .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .card input {
    flex: 1 1 auto; min-width: 200px; background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px;
    font: inherit; font-size: 13px;
  }
  .card input:focus { outline: none; border-color: var(--accent); }
  .steps { list-style: decimal; margin: 0; padding-left: 24px;
           color: color-mix(in srgb, var(--fg) 80%, var(--muted) 20%);
           font-size: 13px; max-height: 160px; overflow: auto; }
  .steps li { padding: 1px 0; }
  .results { list-style: none; margin: 0; padding: 0; font-size: 13px;
             max-height: 220px; overflow: auto; }
  .results li { padding: 2px 0; }
  .tag { display: inline-block; min-width: 52px; text-align: center;
         border-radius: 4px; padding: 0 6px; margin-right: 8px; font-size: 11px; }
  .tag.pass { background: color-mix(in srgb, var(--success) 25%, var(--bg) 75%); color: var(--success); }
  .tag.fail, .tag.error {
    background: color-mix(in srgb, var(--danger) 25%, var(--bg) 75%); color: var(--danger);
  }
  .tag.ack { background: color-mix(in srgb, var(--warning) 25%, var(--bg) 75%); color: var(--warning); }
  .tag.skip { background: color-mix(in srgb, var(--surface) 50%, var(--border) 50%); color: var(--muted); }
  .caselist { list-style: none; margin: 0; padding: 0; font-size: 13px;
              max-height: 280px; overflow: auto; display: flex; flex-direction: column; gap: 2px; }
  .caserow { display: flex; align-items: center; gap: 8px; padding: 3px 0; cursor: pointer; }
  .card .caserow input { flex: 0 0 auto; width: auto; }
  a.btnlink {
    background: color-mix(in srgb, var(--success) 70%, black 30%);
    border: 1px solid var(--success); color: #fff;
    border-radius: 6px; padding: 6px 12px; font-size: 13px; text-decoration: none;
  }
  a.btnlink:hover { background: color-mix(in srgb, var(--success) 85%, black 15%); }
  a.btnlink.secondary {
    background: color-mix(in srgb, var(--surface) 50%, var(--border) 50%);
    border-color: var(--border); color: var(--fg);
  }
  a.btnlink.secondary:hover { background: var(--border); }
  #consolezone { flex: 1 1 auto; display: flex; flex-direction: column;
                 border-top: 1px solid var(--border); overflow: hidden; }
  #emptycon { padding: 24px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>$title</h1>
  <nav>
    <a href="/" target="_blank" rel="noopener">Console</a>
    <a id="artifactlink" href="artifact" target="_blank" rel="noopener">Artifacts</a>
  </nav>
  <span class="status" id="status">connecting&hellip;</span>
</header>
<div id="notices"></div>
<main>
  <section id="casezone">
    <div id="idlecase">No test running. This panel shows the current test's steps and moves the highlight as it runs.</div>
    <div id="casebox" style="display:none">
      <h2 id="casetitle"></h2>
      <div id="phasebar"></div>
      <ol id="steplist"></ol>
    </div>
    <div id="cardzone"></div>
  </section>
  <div id="split" title="Drag to resize"></div>
  <section id="consolezone">
    <div id="tabs"></div>
    <div id="panes"><div id="emptycon">Waiting for console output&hellip;</div></div>
  </section>
</main>
<script>
const statusEl = document.getElementById('status');
const idleCase = document.getElementById('idlecase');
const caseBox = document.getElementById('casebox');
const caseTitle = document.getElementById('casetitle');
const phaseBar = document.getElementById('phasebar');
const stepList = document.getElementById('steplist');
const cardZone = document.getElementById('cardzone');

function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
function btn(text, cls, onclick) {
  const b = document.createElement('button');
  b.textContent = text; if (cls) b.className = cls; b.onclick = onclick;
  return b;
}
function answer(id, body) {
  fetch('prompt/' + encodeURIComponent(id), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  }).then((r) => {
    if (!r.ok) { statusEl.textContent = 'answer rejected'; }
    else { lastSig = null; render(); }  // force a fresh pull after answering
  }).catch(() => { statusEl.textContent = 'answer failed'; });
}

const PHASES = {test_setup: 'Running test setup…', step: 'Running steps',
                test_teardown: 'Running test teardown…', review: 'Review the result'};
const ICONS = {pass: '✓', fail: '✗', error: '✗', ack: '●', skip: '–',
               active: '▶', pending: '○'};
const KINDS = {verdict: 'Verdict', input: 'Input', confirm: 'Confirm',
               start_case: 'Next test', review: 'Review',
               select_cases: 'Select tests to run'};

// ---- Inline controls attached to the active step -------------------------
function controlsFor(p) {
  const wrap = document.createElement('div'); wrap.className = 'controls';
  if (p.kind === 'verdict') {
    const note = document.createElement('input');
    note.type = 'text'; note.className = 'notefield';
    note.placeholder = 'Optional note'; note.autocomplete = 'off';
    // Enter while typing a note submits an acknowledge with that note.
    note.onkeydown = (e) => {
      if (e.key === 'Enter') { e.preventDefault(); answer(p.id, {verdict: 'ack', note: note.value}); }
    };
    wrap.appendChild(btn('Pass (P)', 'primary', () => answer(p.id, {verdict: 'pass', note: note.value})));
    wrap.appendChild(btn('Fail (F)', 'danger', () => answer(p.id, {verdict: 'fail', note: note.value})));
    wrap.appendChild(btn('Acknowledge (Enter)', '', () => answer(p.id, {verdict: 'ack', note: note.value})));
    wrap.appendChild(note);
  } else if (p.kind === 'input') {
    const value = document.createElement('input');
    value.type = 'text'; value.placeholder = 'Type a value'; value.autocomplete = 'off';
    const form = document.createElement('form'); form.className = 'controls';
    form.onsubmit = (e) => { e.preventDefault(); answer(p.id, {value: value.value}); };
    form.appendChild(value);
    form.appendChild(btn('Submit', 'primary', () => answer(p.id, {value: value.value})));
    setTimeout(() => value.focus(), 0);
    return form;
  } else if (p.kind === 'confirm') {
    wrap.appendChild(btn('Yes', 'primary', () => answer(p.id, {confirm: true})));
    wrap.appendChild(btn('No', '', () => answer(p.id, {confirm: false})));
  }
  return wrap;
}

// ---- Standalone cards for the meta prompts (next test / review) ----------
function buildCard(p) {
  const card = document.createElement('div');
  card.className = 'card';
  const kind = document.createElement('div');
  kind.className = 'kind'; kind.textContent = KINDS[p.kind] || p.kind;
  card.appendChild(kind);

  if (p.kind === 'start_case' || p.kind === 'review') {
    const title = document.createElement('div');
    title.className = 'text'; title.textContent = p.case_id + '  ' + p.name;
    card.appendChild(title);
  }
  if (p.text) {
    const text = document.createElement('div');
    text.className = 'text'; text.textContent = p.text;
    card.appendChild(text);
  }

  if (p.kind === 'start_case') {
    if (p.steps && p.steps.length) {
      const ol = document.createElement('ol'); ol.className = 'steps';
      for (const s of p.steps) { const li = document.createElement('li'); li.textContent = s; ol.appendChild(li); }
      card.appendChild(ol);
    }
    const row = document.createElement('div'); row.className = 'row';
    row.appendChild(btn('Run', 'primary', () => answer(p.id, {run: true})));
    row.appendChild(btn('Skip this test', '', () => answer(p.id, {run: false})));
    card.appendChild(row);
  } else if (p.kind === 'review') {
    const ul = document.createElement('ul'); ul.className = 'results';
    for (const s of (p.steps || [])) {
      const li = document.createElement('li');
      const tag = document.createElement('span');
      const oc = (s.outcome || '').toLowerCase();
      tag.className = 'tag ' + oc; tag.textContent = oc.toUpperCase();
      li.appendChild(tag);
      li.appendChild(document.createTextNode(s.name));
      if (s.detail) { const d = document.createElement('span'); d.textContent = '  (' + s.detail + ')'; li.appendChild(d); }
      ul.appendChild(li);
    }
    card.appendChild(ul);
    const overall = document.createElement('div');
    overall.className = 'text';
    overall.innerHTML = 'Result: <span class="tag ' + esc((p.outcome||'').toLowerCase()) + '">' + esc((p.outcome||'').toUpperCase()) + '</span>';
    card.appendChild(overall);
    const row = document.createElement('div'); row.className = 'row';
    row.appendChild(btn('Proceed', 'primary', () => answer(p.id, {decision: 'proceed'})));
    row.appendChild(btn('Repeat test', '', () => answer(p.id, {decision: 'repeat'})));
    row.appendChild(btn('Stop & save report', 'danger', () => answer(p.id, {decision: 'stop'})));
    card.appendChild(row);
  } else if (p.kind === 'select_cases') {
    const list = document.createElement('div'); list.className = 'caselist';
    const boxes = [];
    for (const c of (p.cases || [])) {
      const row = document.createElement('label'); row.className = 'caserow';
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.checked = true; cb.value = c.id;
      boxes.push(cb);
      const label = document.createElement('span'); label.textContent = c.id + '  ' + c.name;
      row.appendChild(cb); row.appendChild(label);
      list.appendChild(row);
    }
    card.appendChild(list);
    const toggles = document.createElement('div'); toggles.className = 'row';
    toggles.appendChild(btn('Select all', '', () => { boxes.forEach((b) => { b.checked = true; }); }));
    toggles.appendChild(btn('Select none', '', () => { boxes.forEach((b) => { b.checked = false; }); }));
    card.appendChild(toggles);
    const row = document.createElement('div'); row.className = 'row';
    row.appendChild(btn('Start run', 'primary', () =>
      answer(p.id, {selected: boxes.filter((b) => b.checked).map((b) => b.value)})
    ));
    card.appendChild(row);
  } else {
    // Fallback: a step-kind prompt with no active case (e.g. run-anyway confirm).
    card.appendChild(controlsFor(p));
  }
  return card;
}

// ---- Step list -----------------------------------------------------------
let expanded = new Set();   // indices of steps whose tool output is expanded
let expandedCase = null;    // case id the expanded-set belongs to

function renderSteps(state, prompt) {
  const cid = state.case ? state.case.id : '';
  if (cid !== expandedCase) { expanded = new Set(); expandedCase = cid; }
  caseTitle.textContent = state.case ? (state.case.id + '  ' + state.case.name) : '';
  phaseBar.textContent = PHASES[state.phase] || '';
  stepList.textContent = '';
  const stepKind = prompt && (prompt.kind === 'verdict' || prompt.kind === 'input' || prompt.kind === 'confirm');
  (state.steps || []).forEach((s, i) => {
    const li = document.createElement('li');
    li.className = s.status + (s.optional ? ' optional' : '');
    const num = (i + 1) + '. ';
    const head = document.createElement('div'); head.className = 'head';
    const ico = document.createElement('span'); ico.className = 'ico';
    ico.textContent = s.status === 'active' ? ICONS.active : (ICONS[s.status] || ICONS.pending);
    const name = document.createElement('span'); name.className = 'name'; name.textContent = num + s.name;
    head.appendChild(ico); head.appendChild(name);

    if (s.status === 'active') {
      li.appendChild(head);
      if (stepKind && prompt.text) {
        const pt = document.createElement('div'); pt.className = 'prompttext'; pt.textContent = prompt.text;
        li.appendChild(pt);
      }
      if (stepKind) li.appendChild(controlsFor(prompt));
      stepList.appendChild(li);
      return;
    }

    if (s.detail) {
      const d = document.createElement('span'); d.className = 'detail';
      d.textContent = '(' + s.detail + ')'; head.appendChild(d);
    }
    const hasOutput = typeof s.output === 'string' && s.output.length > 0;
    if (hasOutput) {
      li.classList.add('expandable');
      const badge = document.createElement('span'); badge.className = 'outbadge';
      badge.textContent = (expanded.has(i) ? '▾' : '▸') + ' output';
      badge.title = 'Show the command output for this step';
      head.appendChild(badge);
      head.onclick = () => {
        if (expanded.has(i)) expanded.delete(i); else expanded.add(i);
        renderSteps(sessionState, pendingPrompt);   // local toggle, bypass poll gate
      };
    }
    li.appendChild(head);
    if (hasOutput && expanded.has(i)) {
      const pre = document.createElement('pre'); pre.className = 'stepout';
      pre.textContent = (s.command ? '$$ ' + s.command + '\\n\\n' : '') + s.output;
      li.appendChild(pre);
    }
    stepList.appendChild(li);
  });
}

// ---- Finished-session report view ----------------------------------------
function buildReport(p) {
  const card = document.createElement('div'); card.className = 'card';
  const kind = document.createElement('div'); kind.className = 'kind'; kind.textContent = 'Report';
  const text = document.createElement('div'); text.className = 'text';
  text.textContent = 'Session finished — result: ' + (p.outcome || '').toUpperCase()
    + '. Open the report in a new tab to read it, or download it as a self-contained .zip.';
  const row = document.createElement('div'); row.className = 'row';
  const open = document.createElement('a');
  open.className = 'btnlink'; open.href = p.report_url;
  open.target = '_blank'; open.rel = 'noopener'; open.textContent = '↗ Open report in new tab';
  const dl = document.createElement('a');
  dl.className = 'btnlink secondary'; dl.href = p.zip_url; dl.setAttribute('download', '');
  dl.textContent = '⬇ Download report (.zip)';
  const finish = btn('Finish session', 'danger', () => {
    finishing = true;
    answer(p.id, {});
    render();
  });
  row.appendChild(open); row.appendChild(dl); row.appendChild(finish);
  card.appendChild(kind); card.appendChild(text); card.appendChild(row);
  return card;
}

// ---- Reconcile session state + pending prompt into one view --------------
let sessionState = {};
let pendingPrompt = {};
let lastSig = null;
let finishing = false;   // operator clicked Finish → surface is shutting down

function signature() {
  const steps = (sessionState.steps || []).map((s) => s.status).join(',');
  const caseId = sessionState.case ? sessionState.case.id : '';
  return [pendingPrompt.id || '', pendingPrompt.kind || '', caseId,
          sessionState.phase || '', steps].join('|');
}

function render() {
  if (finishing) {
    idleCase.style.display = 'none';
    caseBox.style.display = 'none';
    cardZone.textContent = '';
    const done = document.createElement('div'); done.className = 'text';
    done.textContent = 'Report saved. You can close this tab.';
    cardZone.appendChild(done);
    return;
  }
  const sig = signature();
  if (sig === lastSig) return;   // nothing changed → keep any typed-in text
  lastSig = sig;
  const p = pendingPrompt;
  if (p && p.kind === 'finished') {
    idleCase.style.display = 'none';
    caseBox.style.display = 'none';
    cardZone.textContent = '';
    cardZone.appendChild(buildReport(p));
    return;
  }
  const meta = p && (p.kind === 'start_case' || p.kind === 'review' || p.kind === 'select_cases');
  const hasCase = sessionState.steps && sessionState.steps.length;
  cardZone.textContent = '';

  if (meta) {
    idleCase.style.display = 'none';
    caseBox.style.display = 'none';
    cardZone.appendChild(buildCard(p));
    return;
  }
  if (hasCase) {
    idleCase.style.display = 'none';
    caseBox.style.display = '';
    renderSteps(sessionState, p);
    return;
  }
  if (p && p.id) {           // step-kind prompt with no case → fallback card
    idleCase.style.display = 'none';
    caseBox.style.display = 'none';
    cardZone.appendChild(buildCard(p));
    return;
  }
  caseBox.style.display = 'none';
  idleCase.style.display = '';
}

async function pollSession() {
  try {
    const [state, prompt] = await Promise.all([
      fetch('session/state', {cache: 'no-store'}).then((r) => r.json()),
      fetch('prompts/pending', {cache: 'no-store'}).then((r) => r.json()),
    ]);
    sessionState = state || {};
    pendingPrompt = prompt || {};
    statusEl.textContent = 'connected';
    render();
  } catch (e) { statusEl.textContent = 'reconnecting…'; }
}

// ---- Embedded console (SSE, one tab per channel) -------------------------
const tabsEl = document.getElementById('tabs');
const panesEl = document.getElementById('panes');
const emptyConEl = document.getElementById('emptycon');
const channels = new Map();
let activeChan = null;

function selectChannel(name) {
  activeChan = name;
  for (const [n, c] of channels) {
    const on = n === name;
    c.tab.classList.toggle('active', on);
    c.wrap.classList.toggle('active', on);
  }
}
function atBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 40; }

function addInputRow(c, name) {
  if (c.hasInput) return;
  c.hasInput = true;
  const form = document.createElement('form');
  form.className = 'inputrow';
  const prompt = document.createElement('span');
  prompt.className = 'prompt'; prompt.textContent = '›';
  const input = document.createElement('input');
  input.type = 'text'; input.placeholder = 'Type a command and press Enter';
  input.autocomplete = 'off';
  form.appendChild(prompt); form.appendChild(input);
  form.onsubmit = (ev) => {
    ev.preventDefault();
    const value = input.value; if (!value) return;
    input.value = '';
    fetch('input/' + encodeURIComponent(name), {method: 'POST', body: value})
      .catch(() => { statusEl.textContent = 'input failed'; });
  };
  c.wrap.appendChild(form);
}

function addChannel(meta) {
  const name = meta.name;
  if (channels.has(name)) { if (meta.input) addInputRow(channels.get(name), name); return; }
  if (emptyConEl) emptyConEl.style.display = 'none';
  const tab = document.createElement('button');
  tab.textContent = name; tab.onclick = () => selectChannel(name);
  tabsEl.appendChild(tab);
  const wrap = document.createElement('div');
  wrap.className = 'channel';
  const pane = document.createElement('pre');
  pane.className = 'pane';
  wrap.appendChild(pane);
  panesEl.appendChild(wrap);
  const source = new EventSource('stream/' + encodeURIComponent(name));
  source.onmessage = (ev) => {
    const stick = atBottom(pane);
    pane.appendChild(document.createTextNode(ev.data));
    if (stick) pane.scrollTop = pane.scrollHeight;
  };
  const c = {tab, wrap, pane, source, hasInput: false};
  channels.set(name, c);
  if (meta.input) addInputRow(c, name);
  if (activeChan === null) selectChannel(name);
}

async function pollChannels() {
  try {
    const resp = await fetch('channels', {cache: 'no-store'});
    for (const meta of await resp.json()) addChannel(meta);
  } catch (e) { /* transient */ }
}

// ---- Artifact-request indicator (drop zone lives on the Artifacts page) --
const artifactLink = document.getElementById('artifactlink');
async function pollArtifacts() {
  try {
    const pending = await (await fetch('artifacts/pending', {cache: 'no-store'})).json();
    const n = Array.isArray(pending) ? pending.length : 0;
    artifactLink.classList.toggle('pending', n > 0);
    artifactLink.textContent = n > 0 ? ('Artifacts (' + n + ')') : 'Artifacts';
  } catch (e) { /* transient */ }
}

// ---- Draggable split between the step list and the console ---------------
const splitEl = document.getElementById('split');
const mainEl = document.querySelector('main');
const caseZone = document.getElementById('casezone');
let dragging = false;

splitEl.addEventListener('mousedown', (e) => {
  dragging = true; splitEl.classList.add('dragging');
  document.body.style.userSelect = 'none'; e.preventDefault();
});
document.addEventListener('mousemove', (e) => {
  if (!dragging) return;
  const top = mainEl.getBoundingClientRect().top;
  const max = mainEl.clientHeight - 120;   // leave room for the console
  let h = Math.max(80, Math.min(e.clientY - top, max));
  caseZone.style.height = h + 'px';
});
document.addEventListener('mouseup', () => {
  if (!dragging) return;
  dragging = false; splitEl.classList.remove('dragging');
  document.body.style.userSelect = '';
});

// ---- Keyboard shortcuts for the verdict prompt ---------------------------
// P = pass, F = fail, A / Enter = acknowledge. Ignored while the operator is
// typing in a field (so the optional-note input still receives the keys).
document.addEventListener('keydown', (e) => {
  const p = pendingPrompt;
  if (!p || p.kind !== 'verdict') return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  let verdict = null;
  const k = (e.key || '').toLowerCase();
  if (k === 'p') verdict = 'pass';
  else if (k === 'f') verdict = 'fail';
  else if (k === 'a' || e.key === 'Enter') verdict = 'ack';
  if (!verdict) return;
  e.preventDefault();
  const note = document.querySelector('.notefield');
  answer(p.id, {verdict: verdict, note: note ? note.value : ''});
});

pollSession(); setInterval(pollSession, 1000);
pollChannels(); setInterval(pollChannels, 2000);
pollArtifacts(); setInterval(pollArtifacts, 1500);
"""
    + _NOTICE_JS
    + """
</script>
</body>
</html>
"""
)


def render_session_page(title: str, theme: Theme | None = None) -> str:
    """Render the interactive session page.

    The page shows the current test case as an **always-visible ordered step
    list** whose highlight moves to the active step as the run progresses
    (driven by ``/session/state``); the operator prompt for the active step
    (verdict / input / confirm) is rendered inline on that step, while the
    per-case "next test" and "review" prompts appear as standalone cards. It
    also embeds the live console (one tab per channel) and links to the
    standalone console and artifact pages, flagging the latter when a file
    request is pending.

    Args:
        title: Title shown in the browser tab and page header.
        theme: Colour/font overrides (see :mod:`theme`); unset fields keep the
            page's built-in dark terminal look.

    Returns:
        A complete, self-contained HTML document.
    """
    return _SESSION_PAGE.substitute(
        title=title,
        theme_vars=css_variables(theme or Theme(), WEB_DEFAULTS),
        chrome_css=_CHROME_CSS,
    )
