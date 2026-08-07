/**
 * The "Open…" flow.
 *
 * In Electron this defers to the native OS folder dialog. In a browser it
 * drives the in-app directory browser below, which keeps full parity and also
 * works over SSH where no native dialog can appear.
 */

import { api, isElectron, pickDirectory } from '../platform.js';

const el = {
  modal: document.getElementById('open-dialog'),
  crumbs: document.getElementById('crumbs'),
  input: document.getElementById('path-input'),
  list: document.getElementById('dir-list'),
  recents: document.getElementById('recent-list'),
  hidden: document.getElementById('show-hidden'),
  confirm: document.getElementById('open-confirm'),
  hint: document.getElementById('open-hint'),
};

let current = null; // last listing payload
let cursor = -1;
let resolveChoice = null;
let filterText = '';
let filterTimer = null;

function close(result) {
  el.modal.hidden = true;
  const done = resolveChoice;
  resolveChoice = null;
  done?.(result ?? null);
}

async function navigate(path) {
  let data;
  try {
    data = await api.fs(path, el.hidden.checked);
  } catch (err) {
    el.hint.textContent = err.message;
    return;
  }
  current = data;
  cursor = -1;
  filterText = '';
  el.input.value = data.path;
  el.hint.textContent = data.error || (data.is_repo ? 'git repository' : '');
  renderCrumbs();
  renderList();
}

function renderCrumbs() {
  el.crumbs.textContent = '';
  if (!current) return;

  const sep = current.path.includes('\\') ? '\\' : '/';
  const parts = current.path.split(sep).filter(Boolean);
  const isAbsolutePosix = current.path.startsWith('/');

  const addButton = (label, target) => {
    const button = document.createElement('button');
    button.textContent = label;
    button.addEventListener('click', () => navigate(target));
    el.crumbs.append(button);
  };

  addButton('~', current.home);
  let accumulated = isAbsolutePosix ? '' : null;
  parts.forEach((part, index) => {
    const span = document.createElement('span');
    span.className = 'crumb-sep';
    span.textContent = sep;
    el.crumbs.append(span);
    accumulated = accumulated === null ? part : `${accumulated}${sep}${part}`;
    const target = isAbsolutePosix ? `/${parts.slice(0, index + 1).join('/')}` : accumulated;
    addButton(part, target);
  });
}

function visibleEntries() {
  if (!current) return [];
  if (!filterText) return current.entries;
  const needle = filterText.toLowerCase();
  return current.entries.filter((e) => e.name.toLowerCase().includes(needle));
}

function renderList() {
  el.list.textContent = '';
  const entries = visibleEntries();

  if (current?.parent) {
    const up = document.createElement('li');
    up.innerHTML = '<span class="d-ico">↰</span><span>..</span>';
    up.addEventListener('click', () => navigate(current.parent));
    el.list.append(up);
  }

  if (!entries.length) {
    const empty = document.createElement('li');
    empty.className = 'd-empty';
    empty.textContent = current?.error
      ? current.error
      : filterText
        ? `No folder matching "${filterText}"`
        : 'No sub-folders';
    el.list.append(empty);
    return;
  }

  entries.forEach((entry, index) => {
    const li = document.createElement('li');
    li.dataset.index = String(index);
    if (index === cursor) li.classList.add('is-cursor');
    li.innerHTML =
      `<span class="d-ico">${entry.is_repo ? '◆' : '▸'}</span>` +
      `<span class="d-name"></span>` +
      (entry.is_repo ? '<span class="d-repo">git</span>' : '');
    li.querySelector('.d-name').textContent = entry.name;
    li.addEventListener('click', () => navigate(entry.path));
    li.addEventListener('dblclick', () => close(entry.path));
    el.list.append(li);
  });
}

async function renderRecents() {
  let data;
  try {
    data = await api.recents();
  } catch {
    return;
  }
  el.recents.textContent = '';
  if (!data.recents.length) {
    const li = document.createElement('li');
    li.className = 'd-empty';
    li.textContent = 'Nothing yet';
    el.recents.append(li);
    return;
  }
  for (const entry of data.recents) {
    const li = document.createElement('li');
    if (!entry.exists) li.classList.add('is-missing');
    li.title = entry.exists ? entry.path : `${entry.path} (missing)`;
    const name = document.createElement('span');
    name.className = 'r-name';
    name.textContent = entry.name;
    const path = document.createElement('span');
    path.className = 'r-path';
    path.textContent = entry.path;
    li.append(name, path);
    if (entry.exists) li.addEventListener('click', () => close(entry.path));
    el.recents.append(li);
  }
}

function moveCursor(delta) {
  const entries = visibleEntries();
  if (!entries.length) return;
  cursor = Math.max(0, Math.min(entries.length - 1, cursor + delta));
  renderList();
  el.list.querySelector('.is-cursor')?.scrollIntoView({ block: 'nearest' });
}

/** Open the in-app browser and resolve with a chosen path (or null). */
export function openInAppBrowser(startPath) {
  el.modal.hidden = false;
  renderRecents();
  navigate(startPath || current?.path || null);
  setTimeout(() => el.list.focus(), 0);
  return new Promise((resolve) => {
    resolveChoice = resolve;
  });
}

/** Shell-appropriate directory picker: native dialog in Electron, else in-app. */
export function chooseDirectory(startPath) {
  return pickDirectory(() => openInAppBrowser(startPath));
}

export function initOpenDialog() {
  el.modal.querySelector('[data-close]').addEventListener('click', () => close(null));
  el.modal.addEventListener('mousedown', (ev) => {
    if (ev.target === el.modal) close(null);
  });

  el.confirm.addEventListener('click', () => close(current?.path || null));
  el.hidden.addEventListener('change', () => navigate(current?.path || null));

  el.input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      navigate(el.input.value.trim());
    }
  });

  // Type-to-filter and keyboard navigation on the list itself.
  el.list.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      moveCursor(1);
    } else if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      moveCursor(-1);
    } else if (ev.key === 'Enter') {
      ev.preventDefault();
      const entry = visibleEntries()[cursor];
      if (entry) navigate(entry.path);
      else close(current?.path || null);
    } else if (ev.key === 'Backspace') {
      ev.preventDefault();
      if (filterText) {
        filterText = filterText.slice(0, -1);
        renderList();
      } else if (current?.parent) {
        navigate(current.parent);
      }
    } else if (ev.key.length === 1 && !ev.ctrlKey && !ev.metaKey) {
      filterText += ev.key;
      cursor = 0;
      renderList();
      clearTimeout(filterTimer);
      filterTimer = setTimeout(() => {
        filterText = '';
        renderList();
      }, 1400);
    }
  });

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !el.modal.hidden) close(null);
  });

  // In Electron the in-app browser is only a fallback, so hint at the native one.
  if (isElectron) el.hint.dataset.shell = 'electron';
}
