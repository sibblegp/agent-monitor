/**
 * Review notes: everything the AI has said about a comparison, in one place.
 *
 * The narrative tab is a running commentary you read as work happens. This is
 * the opposite view of the same material — the whole change, settled, grouped
 * by file, for the moment before you open a PR. The comparison is chosen here
 * and deliberately does not disturb what the graph panes are showing.
 */

import { api } from '../platform.js';
import { CHANGED, store } from '../state.js';

const el = {
  panel: document.getElementById('review-panel'),
  base: document.getElementById('review-base'),
  refresh: document.getElementById('review-refresh'),
  body: document.getElementById('review-body'),
};

const STATUS_COLOR = {
  added: 'var(--added)',
  modified: 'var(--modified)',
  signature_changed: 'var(--signature)',
  removed: 'var(--removed)',
  renamed: 'var(--modified)',
};

let loading = 0; // ticket of the newest in-flight load
let lastKey = null; // comparison the current notes describe
let lastSource = null; // mode|ref the graph panes were showing

function text(tag, cls, value) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (value != null) node.textContent = value;
  return node;
}

function statusDot(status) {
  const dot = text('span', 'rf-dot');
  dot.style.background = STATUS_COLOR[status] || 'var(--text-faint)';
  return dot;
}

function stat(added, removed) {
  const parts = [];
  if (added) parts.push(`+${added}`);
  if (removed) parts.push(`−${removed}`);
  return parts.join(' ');
}

/** Jump to a node in both graph panes, the same as clicking it there. */
function reveal(id) {
  if (!id || !store.nodes.has(id)) return;
  store.emit('focus-request', id);
}

function renderGroup(group) {
  const wrap = text('div', 'review-file');

  const head = text('div', 'rf-head');
  head.appendChild(statusDot(group.status));
  const path = text('span', 'rf-path', group.path);
  path.title = group.path;
  head.appendChild(path);
  const counts = stat(group.added, group.removed);
  if (counts) head.appendChild(text('span', 'rf-stat', counts));
  head.addEventListener('click', () => reveal(`file:${group.path}`));
  wrap.appendChild(head);

  // A file with no parsed symbols carries its own note instead of children.
  if (group.text) wrap.appendChild(text('div', 'rf-text', group.text));

  if (group.items.length) {
    const list = text('ul', 'review-items');
    for (const item of group.items) {
      const li = document.createElement('li');
      li.appendChild(text('span', 'ri-name', item.name));
      if (item.risk) {
        const badge = text('span', `ri-risk ri-risk-${item.risk}`, item.risk);
        if (item.reason) badge.title = item.reason;
        li.appendChild(badge);
      }
      li.appendChild(
        text('span', 'ri-text', item.text ? ` — ${item.text}` : ` — ${item.status}`)
      );
      li.addEventListener('click', () => reveal(item.id));
      list.appendChild(li);
    }
    wrap.appendChild(list);
  }
  return wrap;
}

function render(data) {
  el.body.replaceChildren();

  if (data.error && !data.groups?.length) {
    el.body.appendChild(text('div', 'review-empty', data.error));
    return;
  }
  if (!data.groups?.length) {
    const empty = data.against
      ? `Nothing differs from ${data.against}.`
      : data.mode === 'live'
        ? 'No uncommitted changes. Pick a branch above to review this one against it.'
        : 'This comparison contains no changes.';
    el.body.appendChild(text('div', 'review-empty', empty));
    return;
  }

  if (data.note) el.body.appendChild(text('div', 'rv-summary', data.note));

  if (data.themes?.length) {
    const themes = text('div', 'review-themes');
    for (const theme of data.themes) {
      themes.appendChild(text('span', 'review-theme', theme.name));
    }
    el.body.appendChild(themes);
  }

  const counts = data.counts || {};
  const stats = text('div', 'review-stats');
  stats.appendChild(text('span', null, `${counts.files || 0} files`));
  if (counts.symbols) stats.appendChild(text('span', null, `${counts.symbols} symbols`));
  const churn = stat(counts.added, counts.removed);
  if (churn) stats.appendChild(text('span', null, churn));
  el.body.appendChild(stats);

  // The AI layer is additive: say so plainly rather than silently showing less.
  if (data.error) el.body.appendChild(text('div', 'review-empty', data.error));
  if (data.pending) {
    el.body.appendChild(
      text(
        'div',
        'review-pending',
        `${data.pending} more symbol${data.pending === 1 ? '' : 's'} not described yet — ` +
          'press ↻ to continue.'
      )
    );
  }
  if (data.describing) {
    el.body.appendChild(
      text('div', 'review-pending', `describing ${data.describing} more…`)
    );
  }

  for (const group of data.groups) el.body.appendChild(renderGroup(group));
}

/**
 * Build the live view's notes from what the live pipeline has already said.
 *
 * While live, every changed symbol is being annotated anyway to fill the hover
 * tooltips — so the notes are a re-presentation of data already in the store,
 * not a second analysis. Nothing is requested, nothing is paid for twice, and
 * entries appear exactly as the agent's work is described.
 */
function compileLiveReview() {
  const changes = store.changes;
  if (!changes) return { mode: 'live', groups: [], counts: {} };

  const groups = new Map();
  for (const file of changes.files || []) {
    groups.set(file.path, {
      path: file.path,
      status: file.status,
      added: file.added || 0,
      removed: file.removed || 0,
      items: [],
    });
  }

  const covered = new Set();
  for (const symbol of changes.symbols || []) {
    if (!CHANGED.has(symbol.status)) continue;
    const group = groups.get(symbol.path);
    if (!group) continue;
    covered.add(symbol.path);
    const node = store.nodes.get(symbol.id);
    group.items.push({
      id: symbol.id,
      name: symbol.qualname,
      kind: symbol.kind,
      status: symbol.status,
      text: node?.summary || null,
      risk: node?.risk || null,
      reason: node?.risk_reason || null,
    });
  }

  // A file nothing was parsed out of carries its own note, as it does server-side.
  for (const [path, group] of groups) {
    if (covered.has(path)) continue;
    const node = store.nodes.get(`file:${path}`);
    group.text = node?.summary || null;
    group.risk = node?.risk || null;
    group.reason = node?.risk_reason || null;
  }

  const list = [...groups.values()].sort((a, b) => a.path.localeCompare(b.path));
  const described = list.reduce(
    (n, g) => n + (g.text ? 1 : 0) + g.items.filter((i) => i.text).length,
    0
  );
  const total = list.reduce((n, g) => n + (g.items.length || 1), 0);

  return {
    mode: 'live',
    against: null,
    note: store.ai?.review_note || '',
    themes: store.ai?.themes || [],
    error: null,
    // Not a cap — just work still in flight, which fills in on its own.
    describing: Math.max(0, total - described),
    counts: {
      files: list.length,
      symbols: list.reduce((n, g) => n + g.items.length, 0),
      added: list.reduce((n, g) => n + g.added, 0),
      removed: list.reduce((n, g) => n + g.removed, 0),
      described,
    },
    groups: list,
  };
}

/** True when the panel is showing the live view rather than a fixed comparison. */
function showingLive() {
  return !el.base.value && store.meta?.mode === 'live';
}

export async function loadReview({ force = false } = {}) {
  const against = el.base.value || '';

  // Live notes are free to build and always current — no fetch, no cache check.
  if (showingLive()) {
    lastKey = against;
    render(compileLiveReview());
    return;
  }

  if (!force && lastKey === against && el.body.childElementCount) return;

  // A cold review takes tens of seconds. Refusing to start a second one while
  // the first is running meant changing the comparison mid-load did nothing
  // visible; instead the newest request wins and the stale answer is dropped.
  const ticket = ++loading;
  el.body.replaceChildren(
    text('div', 'review-empty', 'Reading the diff…' + (against ? ` (vs ${against})` : ''))
  );
  try {
    const data = await api.review(against);
    if (ticket !== loading) return;
    lastKey = against;
    render(data);
  } catch (err) {
    if (ticket !== loading) return;
    el.body.replaceChildren(text('div', 'review-empty', err.message));
  }
}

/**
 * Label for the default option, which reviews whatever the graph panes show.
 *
 * Reviewing the working tree while you're looking at a commit would answer a
 * question you didn't ask, so the subject follows the view — and the label has
 * to say which it is.
 */
function currentViewLabel() {
  const meta = store.meta;
  const ref = meta?.ref;
  if (meta?.mode === 'commit') return `This commit${ref ? ` (${ref.slice(0, 7)})` : ''}`;
  if (meta?.mode === 'branch') return `Branch ${ref || ''}`.trim();
  if (meta?.mode === 'range') return `Range ${ref || ''}`.trim();
  return 'Uncommitted work (vs HEAD)';
}

function relabelCurrent() {
  const option = el.base.options[0];
  if (option && option.value === '') option.text = currentViewLabel();
}

/** Fill the comparison picker. Called whenever a repo opens. */
export async function refreshBranches() {
  const current = el.base.value;
  try {
    const { branches = [], current: head } = await api.branches();
    el.base.replaceChildren();
    el.base.appendChild(new Option(currentViewLabel(), ''));
    for (const name of branches) {
      // Comparing a branch with itself is always empty; don't offer it.
      if (name === head) continue;
      el.base.appendChild(new Option(`This branch vs ${name}`, name));
    }
    el.base.value = [...el.base.options].some((o) => o.value === current) ? current : '';
  } catch {
    /* no branches is not an error worth showing */
  }
}

export function initReview() {
  el.base.addEventListener('change', () => loadReview({ force: true }));
  el.refresh.addEventListener('click', () => loadReview({ force: true }));

  // Live notes are a view onto the store, so they follow it. Annotations
  // arrive in batches as the agent works; re-rendering on each is what makes
  // entries appear as the work is described rather than in one lump later.
  store.on((kind) => {
    if (kind !== 'ai' && kind !== 'snapshot') return;
    if (el.panel.hidden || !showingLive()) return;
    render(compileLiveReview());
  });
}

/** Notes describe a diff, so they go stale when the diff moves. */
export function markReviewStale() {
  lastKey = null;
  relabelCurrent();

  const source = `${store.meta?.mode}|${store.meta?.ref || ''}`;
  const moved = lastSource !== null && lastSource !== source;
  lastSource = source;

  // Switching to a commit is a deliberate act, so a visible panel should follow
  // it. Live edits arrive here too and must *not* each trigger an AI call —
  // hence only on an actual change of subject.
  if (moved && !el.panel.hidden && !el.base.value) loadReview({ force: true });
}
