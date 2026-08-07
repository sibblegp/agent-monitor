/** Activity ticker + legend along the bottom edge. */

import { COLORS, LABELS, nodePath } from '../render/effects.js';

const list = document.getElementById('feed-list');
const legend = document.getElementById('legend');
const liveDot = document.getElementById('live-dot');

const STATUS_ITEMS = [
  ['added', 'added'],
  ['modified', 'modified'],
  ['signature_changed', 'signature'],
  ['removed', 'removed'],
];

const KIND_ITEMS = [
  ['dir', 'directory'],
  ['file', 'file'],
  ['class', 'class'],
  ['function', 'function'],
  ['method', 'method'],
  ['external', 'external'],
];

/** Draw a node silhouette into a tiny canvas so the legend matches the graph. */
function shapeSwatch(kind, color, { hollow = false } = {}) {
  const size = 14;
  const canvas = document.createElement('canvas');
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = size * dpr;
  canvas.height = size * dpr;
  canvas.style.width = `${size}px`;
  canvas.style.height = `${size}px`;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  nodePath(ctx, kind, size / 2, size / 2, 4.2);
  if (hollow) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.3;
    ctx.stroke();
  } else {
    ctx.fillStyle = color;
    ctx.fill();
  }
  return canvas;
}

export function renderLegend() {
  legend.textContent = '';

  const group = (title) => {
    const heading = document.createElement('div');
    heading.className = 'legend-title';
    heading.textContent = title;
    legend.append(heading);
  };

  group('change');
  for (const [key, label] of STATUS_ITEMS) {
    const row = document.createElement('div');
    const swatch = document.createElement('i');
    swatch.style.background = COLORS[key];
    swatch.style.boxShadow = `0 0 6px ${COLORS[key]}`;
    const text = document.createElement('span');
    text.textContent = label;
    row.append(swatch, text);
    legend.append(row);
  }

  group('kind');
  for (const [kind, label] of KIND_ITEMS) {
    const row = document.createElement('div');
    const color = kind === 'external' ? COLORS.external : kind === 'dir' ? COLORS.dir : '#6b7a90';
    row.append(shapeSwatch(kind, color, { hollow: kind === 'external' }));
    const text = document.createElement('span');
    text.textContent = label;
    row.append(text);
    legend.append(row);
  }
}

function ago(ts) {
  const seconds = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

/** Rendered rows, keyed by identity, so we can reconcile instead of rebuilding. */
const rendered = new Map();

/** A row's identity — changes only when the entry itself meaningfully changes. */
function keyOf(event) {
  return `${event.id}|${event.status}|${event.added}|${event.removed}`;
}

export function renderFeed(store) {
  const events = store.events.slice(0, 60);

  if (!events.length) {
    rendered.clear();
    list.textContent = '';
    const li = document.createElement('li');
    li.className = 'feed-empty';
    li.textContent = store.meta?.repo
      ? 'No changes in the current view — edit a file and it will appear here.'
      : 'Open a repository to begin.';
    list.append(li);
    return;
  }

  const desired = events.map(keyOf);
  const desiredSet = new Set(desired);

  // Drop rows that are gone.
  for (const [key, row] of rendered) {
    if (!desiredSet.has(key)) {
      row.li.remove();
      rendered.delete(key);
    }
  }
  // The empty-state placeholder isn't tracked in `rendered`.
  list.querySelector('.feed-empty')?.remove();

  // Create only the rows that are actually new; existing rows keep their DOM
  // node, so their entry animation doesn't replay on every update.
  events.forEach((event, index) => {
    const key = desired[index];
    let row = rendered.get(key);
    if (!row) {
      row = buildRow(store, event);
      rendered.set(key, row);
      // Animate in only on first appearance.
      row.li.classList.add('is-new');
      row.li.addEventListener(
        'animationend',
        () => row.li.classList.remove('is-new'),
        { once: true }
      );
    }
    row.time.textContent = ago(event.at);
    // Place in order without disturbing nodes already in position.
    const current = list.children[index];
    if (current !== row.li) list.insertBefore(row.li, current || null);
  });
}

function buildRow(store, event) {
  const li = document.createElement('li');
  li.title = `${event.path}${event.line ? `:${event.line}` : ''}`;

  const dot = document.createElement('span');
  dot.className = 'fdot';
  dot.style.background = COLORS[event.status] || COLORS.unchanged;
  dot.style.boxShadow = `0 0 6px ${COLORS[event.status] || COLORS.unchanged}`;

  const status = document.createElement('span');
  status.textContent = LABELS[event.status] || event.status;
  status.style.color = COLORS[event.status] || COLORS.dim;

  const name = document.createElement('span');
  name.className = 'fname';
  name.textContent = event.kind === 'file' ? event.name : `${event.name}()`;

  const path = document.createElement('span');
  path.className = 'fpath';
  path.textContent = event.path;

  li.append(dot, status, name, path);

  if (event.added || event.removed) {
      const delta = document.createElement('span');
      delta.className = 'fdelta';
      if (event.added) {
        const add = document.createElement('span');
        add.className = 'fadd';
        add.textContent = `+${event.added}`;
        delta.append(add);
      }
      if (event.removed) {
        const rem = document.createElement('span');
        rem.className = 'frem';
        rem.textContent = ` -${event.removed}`;
        delta.append(rem);
      }
      li.append(delta);
  }

  const node = store.nodes.get(event.id);
  if (node?.risk) {
      const risk = document.createElement('span');
      risk.className = `frisk frisk-${node.risk}`;
      risk.textContent = node.risk;
      risk.title = node.risk_reason || '';
      li.append(risk);
  }
  if (node?.summary) {
      const summary = document.createElement('span');
      summary.className = 'fai';
      summary.textContent = `— ${node.summary}`;
      li.append(summary);
  }

  const when = document.createElement('span');
  when.style.color = COLORS.faint;
  when.style.marginLeft = 'auto';
  when.textContent = ago(event.at);
  li.append(when);

  li.addEventListener('click', () => store.emit('focus-request', event.id));
  return { li, time: when };
}

export function setLiveState(state) {
  liveDot.className = 'dot-live';
  if (state === 'live') liveDot.classList.add('is-live');
  else if (state === 'paused') liveDot.classList.add('is-paused');
}
