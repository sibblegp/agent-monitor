/** Hover tooltip describing whatever node the pointer is over. */

import { COLORS, LABELS, rgba } from '../render/effects.js';

const el = document.getElementById('tooltip');

const KIND_LABEL = {
  root: 'repository',
  dir: 'directory',
  file: 'file',
  class: 'class',
  function: 'function',
  method: 'method',
  external: 'external package',
};

export function hideTooltip() {
  el.hidden = true;
}

export function showTooltip(store, id, ev) {
  const node = store.nodes.get(id);
  if (!node) {
    hideTooltip();
    return;
  }

  const parts = [];
  const color = COLORS[node.status] || COLORS.dim;

  parts.push(`<div class="tt-name">${escape(node.qualname || node.name)}</div>`);

  if (node.signature) {
    parts.push(`<div class="tt-sig">${escape(truncate(node.signature, 120))}</div>`);
  }

  const badges = [];
  badges.push(
    `<span class="tt-badge" style="background:${rgba('#8b95a3', 0.16)};color:#b6c0cc">${
      KIND_LABEL[node.kind] || node.kind
    }</span>`
  );
  if (node.status && node.status !== 'unchanged') {
    badges.push(
      `<span class="tt-badge" style="background:${rgba(color, 0.18)};color:${color}">${
        LABELS[node.status] || node.status
      }</span>`
    );
  }
  if (node.is_entry) {
    badges.push(
      `<span class="tt-badge" style="background:${rgba(COLORS.entry, 0.16)};color:${COLORS.entry}">entry</span>`
    );
  }
  parts.push(`<div class="tt-row">${badges.join(' ')}</div>`);

  if (node.path) {
    const line = node.line ? `:${node.line}` : '';
    parts.push(`<div class="tt-path">${escape(node.path)}${line}</div>`);
  }

  const facts = [];
  if (node.size) facts.push(`${node.size} LOC`);
  if (node.added) facts.push(`<span style="color:${COLORS.added}">+${node.added}</span>`);
  if (node.removed) facts.push(`<span style="color:${COLORS.removed}">−${node.removed}</span>`);

  const callers = [];
  const callees = [];
  for (const edge of store.edges.values()) {
    if (edge.kind !== 'calls') continue;
    if (edge.dst === id) callers.push(edge.src);
    else if (edge.src === id) callees.push(edge.dst);
  }
  if (callers.length) facts.push(`${callers.length} caller${callers.length > 1 ? 's' : ''}`);
  if (callees.length) facts.push(`${callees.length} callee${callees.length > 1 ? 's' : ''}`);
  if (facts.length) parts.push(`<div class="tt-row">${facts.join(' · ')}</div>`);

  if (node.summary || node.risk) {
    let ai = '';
    if (node.risk) {
      const riskColor =
        node.risk === 'high' ? COLORS.removed : node.risk === 'medium' ? COLORS.modified : COLORS.added;
      ai += `<span class="tt-badge" style="background:${rgba(riskColor, 0.18)};color:${riskColor}">${
        node.risk
      } risk</span> `;
    }
    if (node.summary) ai += escape(node.summary);
    else if (node.risk_reason) ai += escape(node.risk_reason);
    parts.push(`<div class="tt-ai">${ai}</div>`);
  }

  if (node.kind === 'file') {
    parts.push(`<div class="tt-row" style="color:${COLORS.faint}">click to expand / collapse</div>`);
  }

  el.innerHTML = parts.join('');
  el.hidden = false;
  position(ev);
}

function position(ev) {
  const pad = 14;
  const rect = el.getBoundingClientRect();
  let x = ev.clientX + pad;
  let y = ev.clientY + pad;
  if (x + rect.width > window.innerWidth - 8) x = ev.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = ev.clientY - rect.height - pad;
  el.style.left = `${Math.max(8, x)}px`;
  el.style.top = `${Math.max(8, y)}px`;
}

function escape(text) {
  return String(text).replace(
    /[&<>"']/g,
    (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]
  );
}

function truncate(text, max) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
