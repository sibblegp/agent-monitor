/**
 * The running commentary panel.
 *
 * Append-only transcript of what the agent appears to be doing, newest first to
 * match the Activity feed beside it. Each entry is self-contained, because in a
 * newest-first list you can't rely on the reader having seen the one below.
 */

const list = document.getElementById('narrative-list');
const badge = document.getElementById('narrative-badge');

let entries = [];
let unseen = 0;
let active = false;
let onFocusSymbol = null;
let onEnableAi = null;

function ago(ts) {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

function shortPaths(paths) {
  if (!paths?.length) return '';
  const names = paths.map((p) => p.split('/').pop());
  if (names.length === 1) return names[0];
  if (names.length === 2) return names.join(', ');
  return `${names[0]}, ${names[1]} +${names.length - 2}`;
}

function buildEntry(entry, store) {
  const row = document.createElement('div');
  row.className = 'narrative-entry';

  const phase = document.createElement('span');
  phase.className = `nphase nphase-${entry.phase || 'unclear'}`;
  phase.textContent = entry.phase || 'unclear';

  const head = document.createElement('span');
  head.className = 'nhead';
  head.textContent = entry.headline;

  const detail = document.createElement('span');
  detail.className = 'ndetail';
  detail.textContent = entry.detail;

  const when = document.createElement('span');
  when.className = 'nwhen';
  when.textContent = ago(entry.at);

  row.append(phase, head, detail, when);
  row.title = (entry.paths || []).join('\n');

  // Clicking an entry jumps to the first symbol it talks about, so the
  // commentary is a way *into* the graph rather than a separate readout.
  const target = (entry.symbols || []).find((id) => store?.nodes?.has(id));
  if (target) {
    row.classList.add('is-clickable');
    row.addEventListener('click', () => onFocusSymbol?.(target));
  }
  return row;
}

function renderEmpty(aiEnabled, hasKey) {
  const box = document.createElement('div');
  box.className = 'narrative-empty';

  if (!hasKey) {
    box.innerHTML =
      'A running commentary of what the agent is doing, written as it works. ' +
      'Needs an Anthropic API key — add one in <button data-open-settings>Settings</button>.';
    box.querySelector('[data-open-settings]')?.addEventListener('click', () =>
      document.getElementById('settings-dialog').removeAttribute('hidden')
    );
    return box;
  }
  if (!aiEnabled) {
    box.innerHTML =
      'A running commentary of what the agent is doing, written as it works. ' +
      '<button data-enable>Turn on AI insights</button> to start the transcript.';
    box.querySelector('[data-enable]')?.addEventListener('click', () => onEnableAi?.());
    return box;
  }
  box.textContent =
    'Watching. The first entry appears when the agent makes its next change — ' +
    'existing changes in the tree are treated as the starting point, not as news.';
  return box;
}

export function renderNarrative(store, settings, aiStatus) {
  list.textContent = '';

  if (!entries.length) {
    list.append(renderEmpty(!!aiStatus?.enabled, !!settings?.has_key));
    return;
  }
  for (const entry of entries) {
    list.append(buildEntry(entry, store));
  }
}

export function addEntry(entry, store, settings, aiStatus) {
  entries.unshift(entry);
  if (entries.length > 300) entries.length = 300;
  if (!active) {
    unseen += 1;
    badge.textContent = String(unseen);
    badge.hidden = false;
  }
  renderNarrative(store, settings, aiStatus);
}

export function setEntries(list_, store, settings, aiStatus) {
  entries = Array.isArray(list_) ? [...list_] : [];
  renderNarrative(store, settings, aiStatus);
}

export function setActive(value) {
  active = value;
  if (active) {
    unseen = 0;
    badge.hidden = true;
  }
}

export function initNarrative({ onFocus, onEnable }) {
  onFocusSymbol = onFocus;
  onEnableAi = onEnable;
}

export function hasEntries() {
  return entries.length > 0;
}
