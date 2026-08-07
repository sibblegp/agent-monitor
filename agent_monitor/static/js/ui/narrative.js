/**
 * The running commentary pane.
 *
 * Append-only transcript of what the agent appears to be doing, newest first.
 * The entry currently being written streams in at the top with a caret, so you
 * can read it as it arrives rather than waiting for the whole thing.
 */

const list = document.getElementById('narrative-list');
const sub = document.getElementById('narrative-sub');

let entries = [];
let streaming = null; // {id, headline, detail, paths, count}
let onFocusSymbol = null;
let onEnableAi = null;
let store = null;
let settings = null;
let aiStatus = null;

function ago(ts) {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

function fileNames(paths) {
  if (!paths?.length) return '';
  const names = paths.map((p) => p.split('/').pop());
  return names.length > 3 ? `${names.slice(0, 3).join(', ')} +${names.length - 3}` : names.join(', ');
}

function buildEntry(entry, { isStreaming = false } = {}) {
  const row = document.createElement('div');
  row.className = 'narrative-entry' + (isStreaming ? ' is-streaming' : '');

  const meta = document.createElement('div');
  meta.className = 'nmeta';

  const phase = document.createElement('span');
  phase.className = `nphase nphase-${entry.phase || 'unclear'}`;
  phase.textContent = isStreaming ? 'writing' : entry.phase || 'unclear';
  meta.append(phase);

  if (!isStreaming) {
    const when = document.createElement('span');
    when.className = 'nwhen';
    when.textContent = ago(entry.at);
    meta.append(when);
  }
  row.append(meta);

  const head = document.createElement('span');
  head.className = 'nhead';
  head.textContent = entry.headline || '…';
  row.append(head);

  const detail = document.createElement('span');
  detail.className = 'ndetail';
  detail.textContent = entry.detail || '';
  row.append(detail);

  if (entry.paths?.length) {
    const paths = document.createElement('span');
    paths.className = 'npaths';
    paths.textContent = fileNames(entry.paths);
    paths.title = entry.paths.join('\n');
    row.append(paths);
  }

  // Clicking an entry jumps to the first symbol it talks about, so the
  // commentary is a way *into* the graph rather than a separate readout.
  const target = (entry.symbols || []).find((id) => store?.nodes?.has(id));
  if (target && !isStreaming) {
    row.classList.add('is-clickable');
    row.addEventListener('click', () => onFocusSymbol?.(target));
  }
  return row;
}

function renderEmpty() {
  const box = document.createElement('div');
  box.className = 'narrative-empty';

  if (!settings?.has_key) {
    box.innerHTML =
      'A running commentary of what the agent is doing, written as it works.<br><br>' +
      'Needs an Anthropic API key — add one in <button data-open-settings>Settings</button>.';
    box.querySelector('[data-open-settings]')?.addEventListener('click', () =>
      document.getElementById('settings-dialog').removeAttribute('hidden')
    );
    return box;
  }
  if (!aiStatus?.enabled) {
    box.innerHTML =
      'A running commentary of what the agent is doing, written as it works.<br><br>' +
      '<button data-enable>Turn on AI insights</button> to start the transcript.';
    box.querySelector('[data-enable]')?.addEventListener('click', () => onEnableAi?.());
    return box;
  }
  box.textContent = 'Watching. The next change the agent makes appears here.';
  return box;
}

export function renderNarrative(nextStore, nextSettings, nextAiStatus) {
  if (nextStore) store = nextStore;
  if (nextSettings) settings = nextSettings;
  if (nextAiStatus) aiStatus = nextAiStatus;

  list.textContent = '';

  if (streaming) list.append(buildEntry(streaming, { isStreaming: true }));

  if (!entries.length && !streaming) {
    list.append(renderEmpty());
  } else {
    for (const entry of entries) list.append(buildEntry(entry));
  }

  const total = entries.length + (streaming ? 1 : 0);
  sub.textContent = total ? `${entries.length} entr${entries.length === 1 ? 'y' : 'ies'}` : '';
}

/** Partial text while the model is still writing the current entry. */
export function applyDelta(message) {
  if (message.state === 'start') {
    streaming = { id: message.id, headline: '', detail: '', paths: message.paths || [], phase: 'unclear' };
  } else if (message.state === 'delta') {
    if (!streaming || streaming.id !== message.id) {
      streaming = { id: message.id, headline: '', detail: '', paths: [], phase: 'unclear' };
    }
    streaming.headline = message.headline ?? streaming.headline;
    streaming.detail = message.detail ?? streaming.detail;
  } else if (message.state === 'abort') {
    streaming = null;
  }
  renderNarrative();
  list.scrollTop = 0;
}

export function addEntry(entry) {
  // The finished entry replaces whatever was streaming.
  streaming = null;
  entries.unshift(entry);
  if (entries.length > 300) entries.length = 300;
  renderNarrative();
  list.scrollTop = 0;
}

export function setEntries(next) {
  entries = Array.isArray(next) ? [...next] : [];
  streaming = null;
  renderNarrative();
}

export function initNarrative({ onFocus, onEnable }) {
  onFocusSymbol = onFocus;
  onEnableAi = onEnable;
}
