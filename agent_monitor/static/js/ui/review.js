/**
 * Review notes: everything the AI has said about a comparison, in one place.
 *
 * The narrative tab is a running commentary you read as work happens. This is
 * the opposite view of the same material — the whole change, settled, grouped
 * by file, for the moment before you open a PR. The comparison is chosen here
 * and deliberately does not disturb what the graph panes are showing.
 */

import { api } from '../platform.js';
import { store } from '../state.js';

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

let loading = false;
let lastKey = null; // comparison the current notes describe

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
    el.body.appendChild(
      text(
        'div',
        'review-empty',
        data.against
          ? `Nothing differs from ${data.against}.`
          : 'No uncommitted changes. Pick a branch above to review this one against it.'
      )
    );
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

  for (const group of data.groups) el.body.appendChild(renderGroup(group));
}

export async function loadReview({ force = false } = {}) {
  const against = el.base.value || '';
  if (loading) return;
  if (!force && lastKey === against && el.body.childElementCount) return;

  loading = true;
  el.body.replaceChildren(text('div', 'review-empty', 'Reading the diff…'));
  try {
    const data = await api.review(against);
    lastKey = against;
    render(data);
  } catch (err) {
    el.body.replaceChildren(text('div', 'review-empty', err.message));
  } finally {
    loading = false;
  }
}

/** Fill the comparison picker. Called whenever a repo opens. */
export async function refreshBranches() {
  const current = el.base.value;
  try {
    const { branches = [], current: head } = await api.branches();
    el.base.replaceChildren();
    el.base.appendChild(new Option('Uncommitted work (vs HEAD)', ''));
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
}

/** Notes describe a diff, so they go stale when the diff moves. */
export function markReviewStale() {
  lastKey = null;
}
