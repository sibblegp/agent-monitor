/** Activity ticker + legend along the bottom edge. */

import { COLORS, LABELS } from '../render/effects.js';

const list = document.getElementById('feed-list');
const legend = document.getElementById('legend');
const liveDot = document.getElementById('live-dot');

const LEGEND_ITEMS = [
  ['added', 'added'],
  ['modified', 'modified'],
  ['signature_changed', 'signature'],
  ['removed', 'removed'],
  ['external', 'external'],
  ['entry', 'entry point'],
];

export function renderLegend() {
  legend.textContent = '';
  for (const [key, label] of LEGEND_ITEMS) {
    const row = document.createElement('div');
    const swatch = document.createElement('i');
    swatch.style.background = COLORS[key];
    swatch.style.boxShadow = `0 0 6px ${COLORS[key]}`;
    const text = document.createElement('span');
    text.textContent = label;
    row.append(swatch, text);
    legend.append(row);
  }
}

function ago(ts) {
  const seconds = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

export function renderFeed(store) {
  list.textContent = '';

  if (!store.events.length) {
    const li = document.createElement('li');
    li.className = 'feed-empty';
    li.textContent = store.meta?.repo
      ? 'No changes in the current view — edit a file and it will appear here.'
      : 'Open a repository to begin.';
    list.append(li);
    return;
  }

  for (const event of store.events.slice(0, 60)) {
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
    list.append(li);
  }
}

export function setLiveState(state) {
  liveDot.className = 'dot-live';
  if (state === 'live') liveDot.classList.add('is-live');
  else if (state === 'paused') liveDot.classList.add('is-paused');
}
