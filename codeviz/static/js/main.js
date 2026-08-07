/**
 * Boot + wiring + the single shared animation loop.
 *
 * Both panes are driven from one requestAnimationFrame tick so their pulses,
 * blinks, and particle drift stay in phase — two independent loops visibly
 * beat against each other.
 */

import { ApiError, api, isElectron, onShellCommand } from './platform.js';
import { store } from './state.js';
import { Live } from './ws.js';
import { Scene, boundsOf } from './render/scene.js';
import { ForceLayout } from './layout/force.js';
import { LayeredLayout } from './layout/layered.js';
import { StructureRenderer } from './render/structure.js';
import { FlowRenderer } from './render/flow.js';
import { chooseDirectory, initOpenDialog, openInAppBrowser } from './ui/openDialog.js';
import { renderFeed, renderLegend, setLiveState } from './ui/feed.js';
import { hideTooltip, showTooltip } from './ui/inspector.js';

// ── element refs ──────────────────────────────────────────────────────

const el = {
  repoButton: document.getElementById('repo-button'),
  repoName: document.getElementById('repo-name'),
  repoBranch: document.getElementById('repo-branch'),
  sourceSeg: document.getElementById('source-seg'),
  filterSeg: document.getElementById('filter-seg'),
  depth: document.getElementById('depth'),
  depthOut: document.getElementById('depth-out'),
  stats: document.getElementById('stats'),
  pause: document.getElementById('pause'),
  fit: document.getElementById('fit'),
  settingsButton: document.getElementById('settings-button'),
  settingsDialog: document.getElementById('settings-dialog'),
  aiToggle: document.getElementById('ai-toggle'),
  aiLabel: document.getElementById('ai-label'),
  banner: document.getElementById('banner'),
  splitter: document.getElementById('splitter'),
  panes: document.getElementById('panes'),
  paneStructure: document.getElementById('pane-structure'),
  paneFlow: document.getElementById('pane-flow'),
  structureSub: document.getElementById('structure-sub'),
  flowSub: document.getElementById('flow-sub'),
  structureEmpty: document.getElementById('structure-empty'),
  flowEmpty: document.getElementById('flow-empty'),
  refDialog: document.getElementById('ref-dialog'),
  refTitle: document.getElementById('ref-title'),
  refList: document.getElementById('ref-list'),
  searchBar: document.getElementById('search-bar'),
  searchInput: document.getElementById('search-input'),
  searchResults: document.getElementById('search-results'),
};

// ── scenes, layouts, renderers ────────────────────────────────────────

const forceLayout = new ForceLayout(store);
const layeredLayout = new LayeredLayout(store);

const structureScene = new Scene(document.getElementById('canvas-structure'), {
  onHover: (id, ev) => handleHover(id, ev),
  onPick: (id) => handlePick(id, 'structure'),
  onDouble: (id) => id && forceLayout.toggle(id),
});

const flowScene = new Scene(document.getElementById('canvas-flow'), {
  onHover: (id, ev) => handleHover(id, ev),
  onPick: (id) => handlePick(id, 'flow'),
});

const structureRenderer = new StructureRenderer(structureScene, forceLayout, store);
const flowRenderer = new FlowRenderer(flowScene, layeredLayout, store);

let needsFit = true; // rough fit as soon as anything exists
let settleFit = 0; // deadline for the second fit, once the force sim calms down

// ── interaction ───────────────────────────────────────────────────────

function handleHover(id, ev) {
  if (store.hover === id && id) {
    if (ev) showTooltip(store, id, ev);
    return;
  }
  store.hover = id;
  if (id && ev) showTooltip(store, id, ev);
  else hideTooltip();
}

function handlePick(id, pane) {
  if (!id) {
    store.selected = null;
    dirty = true;
    return;
  }
  // Clicking a file toggles its expansion; clicking anything else focuses it
  // in *both* panes, which is the fastest way to answer "where does this live
  // / what does it call?".
  const node = store.nodes.get(id);
  if (pane === 'structure' && node?.kind === 'file') {
    forceLayout.toggle(id);
    dirty = true;
    return;
  }
  store.selected = store.selected === id ? null : id;
  dirty = true;
}

store.on((kind, payload) => {
  if (kind === 'focus-request') {
    store.selected = payload;
    centerOn(payload);
  }
});

function centerOn(id) {
  const s = forceLayout.get(id);
  if (s) {
    structureScene.camera.targetX = structureScene.width / 2 - s.x * structureScene.camera.targetScale;
    structureScene.camera.targetY = structureScene.height / 2 - s.y * structureScene.camera.targetScale;
  }
  const f = layeredLayout.get(id);
  if (f) {
    flowScene.camera.targetX = flowScene.width / 2 - f.tx * flowScene.camera.targetScale;
    flowScene.camera.targetY = flowScene.height / 2 - f.ty * flowScene.camera.targetScale;
  }
}

// ── the shared loop ───────────────────────────────────────────────────

/**
 * Set whenever something that affects the picture changes but isn't itself
 * animated (hover, selection, filter, expansion). Lets the loop idle at zero
 * work when the graph is at rest, which matters for a window that sits open
 * next to an agent all day.
 */
let dirty = true;
export function markDirty() {
  dirty = true;
}

let lastHover = null;
let lastSelected = null;

function frame(now) {
  forceLayout.sync();
  layeredLayout.sync();

  const simMoving = forceLayout.step();
  const flowMoving = layeredLayout.step();

  const camMoving = structureScene.camera.step() | flowScene.camera.step();

  if (store.hover !== lastHover || store.selected !== lastSelected) {
    lastHover = store.hover;
    lastSelected = store.selected;
    dirty = true;
  }

  const animating =
    simMoving || flowMoving || camMoving || store.hasLiveActivity || store.flowIsAnimating;

  if (!animating && !dirty && !needsFit && !settleFit) {
    requestAnimationFrame(frame);
    return;
  }
  dirty = false;

  if (needsFit && forceLayout.points().length) {
    fitAll(true);
    needsFit = false;
    // The first fit runs against a layout that hasn't relaxed yet, so it
    // always frames the wrong box. Schedule a second one for when the
    // simulation has calmed (or give up after 4s if it never fully settles).
    settleFit = now + 4000;
  }

  if (settleFit && (forceLayout.alpha < 0.05 || now > settleFit)) {
    settleFit = 0;
    fitAll(false);
    updateChrome();
  }

  structureRenderer.draw(now);
  flowRenderer.draw(now);

  requestAnimationFrame(frame);
}

/** Don't hijack the view if the user touched either pane this recently. */
const IDLE_BEFORE_AUTOFOCUS_MS = 4000;
/** Keep this much world-space context around the change, so it isn't a blind zoom. */
const FOCUS_MIN_EXTENT = 460;

function userIsDriving() {
  const now = performance.now();
  return (
    now - structureScene.lastInteraction < IDLE_BEFORE_AUTOFOCUS_MS ||
    now - flowScene.lastInteraction < IDLE_BEFORE_AUTOFOCUS_MS
  );
}

function padBox(box, minExtent) {
  if (!box) return null;
  const cx = (box.minX + box.maxX) / 2;
  const cy = (box.minY + box.maxY) / 2;
  const halfW = Math.max((box.maxX - box.minX) / 2, minExtent / 2);
  const halfH = Math.max((box.maxY - box.minY) / 2, minExtent / 2);
  return { minX: cx - halfW, minY: cy - halfH, maxX: cx + halfW, maxY: cy + halfH };
}

/**
 * Glide both panes to frame whatever just changed.
 *
 * This is the point of the whole tool: an edit landing off-screen or two pixels
 * wide is an edit you don't notice. Skipped entirely while the user is panning,
 * zooming, or inspecting, because yanking the view out from under someone is
 * worse than missing one update.
 */
function focusRecentChange() {
  if (!store.recent.size || userIsDriving()) return;

  const structurePoints = [];
  const flowPoints = [];
  for (const id of store.recent) {
    const s = forceLayout.get(id);
    if (s) structurePoints.push({ x: s.x, y: s.y });
    const f = layeredLayout.get(id);
    if (f) flowPoints.push({ x: f.tx, y: f.ty });
    // Include the parent so a lone new symbol is framed with its file.
    const parent = store.nodes.get(id)?.parent;
    const ps = parent && forceLayout.get(parent);
    if (ps) structurePoints.push({ x: ps.x, y: ps.y });
  }

  const sBox = padBox(boundsOf(structurePoints), FOCUS_MIN_EXTENT);
  if (sBox) {
    structureScene.camera.fitTo(sBox, structureScene.width, structureScene.height, 90);
  }
  const fBox = padBox(boundsOf(flowPoints), FOCUS_MIN_EXTENT);
  if (fBox) {
    flowScene.camera.fitTo(fBox, flowScene.width, flowScene.height, 90);
  }
  dirty = true;
}

function fitAll(immediate = false) {
  const structurePoints = forceLayout.points();
  if (structurePoints.length) {
    structureScene.camera.fitTo(
      boundsOf(structurePoints),
      structureScene.width,
      structureScene.height
    );
    if (immediate) structureScene.camera.jumpToTarget();
  }
  const flowPoints = layeredLayout.points().map((p) => ({ x: p.tx, y: p.ty }));
  if (flowPoints.length) {
    flowScene.camera.fitTo(boundsOf(flowPoints), flowScene.width, flowScene.height, 60);
    if (immediate) flowScene.camera.jumpToTarget();
  }
}

// ── chrome updates ────────────────────────────────────────────────────

function updateChrome() {
  const meta = store.meta;
  const repo = meta?.repo;

  el.repoName.textContent = repo ? repo.name : 'No repository';
  el.repoBranch.textContent = repo?.branch || '';
  el.repoButton.title = repo ? `${repo.root}${repo.scope ? `/${repo.scope}` : ''}` : 'Open a repository (Ctrl+O)';

  if (meta) {
    const changedFiles = meta.changed_files ?? 0;
    const changedSymbols = meta.changed_symbols ?? 0;
    el.stats.innerHTML =
      `<span><b>${meta.counts?.file ?? 0}</b> files</span>` +
      `<span><b>${(meta.counts?.function ?? 0) + (meta.counts?.method ?? 0)}</b> fns</span>` +
      `<span><b>${changedFiles}</b> changed</span>` +
      `<span><b>${changedSymbols}</b> symbols</span>` +
      `<span>${meta.scan_ms}ms</span>`;
  }

  const visibleStructure = forceLayout.points().length;
  const skipped = forceLayout.autoExpandSkipped || 0;
  el.structureSub.textContent = repo
    ? `${visibleStructure} nodes` +
      (forceLayout.expanded.size ? ` · ${forceLayout.expanded.size} expanded` : '') +
      // Never silently hold something back — say so, and how to see it.
      (skipped ? ` · ${skipped} more changed files collapsed (click to open)` : '')
    : '';
  el.flowSub.textContent = repo
    ? `${layeredLayout.nodes.length} nodes · ${layeredLayout.edges.length} calls` +
      (layeredLayout.truncated ? ` · showing top ${layeredLayout.nodes.length} of ${layeredLayout.nodes.length + layeredLayout.truncated}` : '')
    : '';

  el.structureEmpty.hidden = !!repo;
  el.structureEmpty.textContent = 'Open a repository to see its structure.';
  el.flowEmpty.hidden = !repo || layeredLayout.nodes.length > 0;
  el.flowEmpty.textContent = repo
    ? store.filter === 'changes'
      ? 'No call flow around the current changes.'
      : 'No resolvable calls in this scope yet.'
    : '';

  if (meta?.warnings?.length) banner(meta.warnings[0], 'warn');
}

let bannerTimer = null;
function banner(text, kind = 'info', ms = 5200) {
  el.banner.textContent = text;
  el.banner.className = `banner is-${kind}`;
  el.banner.hidden = false;
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => {
    el.banner.hidden = true;
  }, ms);
}

// ── data flow ─────────────────────────────────────────────────────────

function applySnapshot(payload, { refit = false } = {}) {
  store.applySnapshot(payload, { animate: true });
  store.seedEventsFromChanges();
  dirty = true;
  // `refit` marks a deliberate context switch (new repo, new mode, new filter),
  // which is the only time a full re-layout is wanted. A live edit must not
  // re-run the whole simulation, or the entire picture drifts because one
  // function appeared.
  forceLayout.sync(refit);
  layeredLayout.sync(refit);
  if (refit) needsFit = true;
  updateChrome();
  renderFeed(store);

  // Bring the change on screen. Deferred a beat so the layout has placed any
  // newly-arrived nodes before we try to frame them.
  if (!refit && store.recent.size) {
    setTimeout(focusRecentChange, 90);
  }
}

async function openRepo(path) {
  if (!path) return;
  try {
    banner(`Opening ${path}…`, 'info', 2000);
    await api.open(path);
    const payload = await api.snapshot();
    applySnapshot(payload, { refit: true });
    banner(`Opened ${store.meta?.repo?.name ?? path}`, 'info', 2200);
  } catch (err) {
    banner(err instanceof ApiError ? err.message : String(err), 'error', 7000);
  }
}

async function setMode(mode, ref) {
  try {
    await api.setMode(mode, ref);
    const payload = await api.snapshot();
    applySnapshot(payload, { refit: true });
  } catch (err) {
    banner(err instanceof ApiError ? err.message : String(err), 'error', 7000);
  }
}

// ── controls ──────────────────────────────────────────────────────────

function selectSeg(container, attr, value) {
  for (const button of container.querySelectorAll('.seg-btn')) {
    button.classList.toggle('is-active', button.dataset[attr] === value);
  }
}

el.repoButton.addEventListener('click', async () => {
  const path = await chooseDirectory(store.meta?.repo?.root);
  if (path) openRepo(path);
});

el.sourceSeg.addEventListener('click', async (ev) => {
  const button = ev.target.closest('.seg-btn');
  if (!button) return;
  const mode = button.dataset.mode;

  if (mode === 'live') {
    selectSeg(el.sourceSeg, 'mode', 'live');
    setMode('live', null);
    return;
  }
  const chosen = await pickRef(mode);
  if (!chosen) return;
  selectSeg(el.sourceSeg, 'mode', mode);
  setMode(mode, chosen);
});

el.filterSeg.addEventListener('click', (ev) => {
  const button = ev.target.closest('.seg-btn');
  if (!button) return;
  setFilter(button.dataset.filter);
});

function setFilter(value) {
  store.filter = value;
  dirty = true;
  selectSeg(el.filterSeg, 'filter', value);
  store.version += 1; // force both layouts to reconcile
  forceLayout.sync(true);
  layeredLayout.sync(true);
  needsFit = true;
  updateChrome();
}

el.depth.addEventListener('input', () => {
  store.depth = Number(el.depth.value);
  el.depthOut.textContent = el.depth.value;
});

el.pause.addEventListener('click', togglePause);
function togglePause() {
  store.paused = !store.paused;
  el.pause.classList.toggle('is-active', store.paused);
  el.pause.textContent = store.paused ? '▶' : '⏸';
  el.pause.title = store.paused ? 'Resume live updates (space)' : 'Pause live updates (space)';
  setLiveState(store.paused ? 'paused' : 'live');
  // Tell the backend too, so a paused window stops analysing rather than
  // analysing and throwing the result away.
  api.pause(store.paused).catch(() => {});
}

el.fit.addEventListener('click', () => fitAll());

el.settingsButton.addEventListener('click', () => {
  el.settingsDialog.hidden = false;
});

for (const dialog of document.querySelectorAll('.modal')) {
  dialog.querySelector('[data-close]')?.addEventListener('click', () => {
    dialog.hidden = true;
  });
  dialog.addEventListener('mousedown', (ev) => {
    if (ev.target === dialog) dialog.hidden = true;
  });
}

// ── commit / branch picker ────────────────────────────────────────────

let resolveRef = null;

async function pickRef(mode) {
  el.refTitle.textContent = mode === 'commit' ? 'Pick a commit' : 'Pick a branch';
  el.refList.textContent = '';
  el.refDialog.hidden = false;

  try {
    if (mode === 'commit') {
      const { commits } = await api.commits();
      if (!commits.length) {
        el.refList.innerHTML = '<li class="d-empty">No commits yet</li>';
      }
      for (const commit of commits) {
        const li = document.createElement('li');
        li.innerHTML =
          `<span class="r-sha"></span> <span class="r-subject"></span>` +
          `<div class="r-meta"></div>`;
        li.querySelector('.r-sha').textContent = commit.short;
        li.querySelector('.r-subject').textContent = commit.subject;
        li.querySelector('.r-meta').textContent = `${commit.author} · ${commit.when}`;
        li.addEventListener('click', () => closeRef(commit.sha));
        el.refList.append(li);
      }
    } else {
      const { branches, current } = await api.branches();
      if (!branches.length) {
        el.refList.innerHTML = '<li class="d-empty">No branches</li>';
      }
      for (const name of branches) {
        const li = document.createElement('li');
        li.innerHTML = `<span class="r-subject"></span><div class="r-meta"></div>`;
        li.querySelector('.r-subject').textContent = name;
        li.querySelector('.r-meta').textContent = name === current ? 'current branch' : '';
        li.addEventListener('click', () => closeRef(name));
        el.refList.append(li);
      }
    }
  } catch (err) {
    el.refList.innerHTML = `<li class="d-empty">${err.message}</li>`;
  }

  return new Promise((resolve) => {
    resolveRef = resolve;
  });
}

function closeRef(value) {
  el.refDialog.hidden = true;
  const done = resolveRef;
  resolveRef = null;
  done?.(value ?? null);
}

el.refDialog.querySelector('[data-close]').addEventListener('click', () => closeRef(null));

// ── splitter ──────────────────────────────────────────────────────────

(function splitter() {
  let dragging = false;
  el.splitter.addEventListener('mousedown', (ev) => {
    dragging = true;
    el.splitter.classList.add('is-dragging');
    ev.preventDefault();
  });
  window.addEventListener('mousemove', (ev) => {
    if (!dragging) return;
    const rect = el.panes.getBoundingClientRect();
    const ratio = Math.max(0.15, Math.min(0.85, (ev.clientX - rect.left) / rect.width));
    el.paneStructure.style.flex = `0 0 ${ratio * 100}%`;
    el.paneFlow.style.flex = `1 1 auto`;
    resizeScenes();
  });
  window.addEventListener('mouseup', () => {
    dragging = false;
    el.splitter.classList.remove('is-dragging');
  });
})();

function resizeScenes() {
  structureScene.resize();
  flowScene.resize();
}

/**
 * Resizing the canvas alone leaves the camera framing the old viewport, so the
 * graph ignores newly available space. Re-fit after the resize settles, and
 * give the force sim a nudge so it actually spreads into the new area rather
 * than just being rescaled.
 */
let resizeTimer = null;
const observer = new ResizeObserver(() => {
  resizeScenes();
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    forceLayout.reheat(0.4);
    fitAll();
    updateChrome();
    dirty = true;
  }, 160);
});
observer.observe(el.paneStructure);
observer.observe(el.paneFlow);
window.addEventListener('resize', resizeScenes);

// ── search ────────────────────────────────────────────────────────────

let searchCursor = 0;

function openSearch() {
  el.searchBar.hidden = false;
  el.searchInput.value = '';
  el.searchInput.focus();
  renderSearch([]);
}

function closeSearch() {
  el.searchBar.hidden = true;
  store.searchHits = new Set();
}

function runSearch(query) {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];
  const hits = [];
  for (const node of store.nodes.values()) {
    if (node.kind === 'root' || node.kind === 'dir') continue;
    const hay = `${node.qualname || node.name} ${node.path || ''}`.toLowerCase();
    if (hay.includes(needle)) hits.push(node);
    if (hits.length > 60) break;
  }
  hits.sort((a, b) => {
    const aExact = (a.name || '').toLowerCase().startsWith(needle) ? 0 : 1;
    const bExact = (b.name || '').toLowerCase().startsWith(needle) ? 0 : 1;
    return aExact - bExact || (a.name || '').length - (b.name || '').length;
  });
  return hits.slice(0, 40);
}

function renderSearch(hits) {
  el.searchResults.textContent = '';
  store.searchHits = new Set(hits.map((h) => h.id));
  hits.forEach((node, index) => {
    const li = document.createElement('li');
    if (index === searchCursor) li.classList.add('is-cursor');
    const name = document.createElement('span');
    name.textContent = node.qualname || node.name;
    const path = document.createElement('span');
    path.className = 's-path';
    path.textContent = node.path || node.kind;
    li.append(name, path);
    li.addEventListener('click', () => {
      store.selected = node.id;
      centerOn(node.id);
      closeSearch();
    });
    el.searchResults.append(li);
  });
}

el.searchInput.addEventListener('input', () => {
  searchCursor = 0;
  renderSearch(runSearch(el.searchInput.value));
});

el.searchInput.addEventListener('keydown', (ev) => {
  const hits = runSearch(el.searchInput.value);
  if (ev.key === 'Escape') closeSearch();
  else if (ev.key === 'ArrowDown') {
    ev.preventDefault();
    searchCursor = Math.min(hits.length - 1, searchCursor + 1);
    renderSearch(hits);
  } else if (ev.key === 'ArrowUp') {
    ev.preventDefault();
    searchCursor = Math.max(0, searchCursor - 1);
    renderSearch(hits);
  } else if (ev.key === 'Enter') {
    const node = hits[searchCursor];
    if (node) {
      store.selected = node.id;
      centerOn(node.id);
      closeSearch();
    }
  }
});

// ── keyboard ──────────────────────────────────────────────────────────

document.addEventListener('keydown', (ev) => {
  const typing =
    ev.target instanceof HTMLInputElement ||
    ev.target instanceof HTMLSelectElement ||
    ev.target instanceof HTMLTextAreaElement;

  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'o') {
    ev.preventDefault();
    el.repoButton.click();
    return;
  }
  if (typing) return;

  switch (ev.key) {
    case ' ':
      ev.preventDefault();
      togglePause();
      break;
    case 'a':
      setFilter(store.filter === 'all' ? 'changes' : 'all');
      break;
    case 'f':
      fitAll();
      break;
    case '1':
      structureScene.canvas.focus();
      el.paneStructure.style.flex = '0 0 72%';
      resizeScenes();
      break;
    case '2':
      el.paneStructure.style.flex = '0 0 28%';
      resizeScenes();
      break;
    case '/':
      ev.preventDefault();
      openSearch();
      break;
    case 'Escape':
      store.selected = null;
      closeSearch();
      hideTooltip();
      break;
    default:
      break;
  }
});

// ── live connection ───────────────────────────────────────────────────

const live = new Live(
  (message) => {
    if (message.type === 'snapshot') {
      if (store.paused) return;
      applySnapshot(message, { refit: store.nodes.size === 0 });
    } else if (message.type === 'delta') {
      if (store.paused) return;
      applySnapshot(message, { refit: false });
    } else if (message.type === 'ai') {
      store.applyAi(message);
      renderFeed(store);
    } else if (message.type === 'status') {
      if (message.text) banner(message.text, message.level || 'info');
    } else if (message.type === 'idle') {
      updateChrome();
      renderFeed(store);
    }
  },
  (state) => setLiveState(state === 'open' && !store.paused ? 'live' : 'idle')
);

// ── boot ──────────────────────────────────────────────────────────────

async function boot() {
  initOpenDialog();
  renderLegend();
  setLiveState('idle');
  el.depthOut.textContent = el.depth.value;

  onShellCommand(async (command, payload) => {
    if (command === 'open') {
      const path = payload || (await chooseDirectory(store.meta?.repo?.root));
      if (path) openRepo(path);
    } else if (command === 'fit') fitAll();
    else if (command === 'refresh') api.refresh().catch(() => {});
    else if (command === 'settings') el.settingsDialog.hidden = false;
  });

  try {
    const state = await api.state();
    if (state.has_repo) {
      const payload = await api.snapshot();
      applySnapshot(payload, { refit: true });
    } else {
      updateChrome();
      renderFeed(store);
      // No repo yet — go straight to the picker, since that's the only
      // sensible next action.
      const path = await chooseDirectory();
      if (path) openRepo(path);
    }
  } catch (err) {
    banner(
      err instanceof ApiError && err.status === 403
        ? 'Auth token missing or invalid — reopen the app from its original URL.'
        : `Could not reach the backend: ${err.message}`,
      'error',
      15000
    );
  }

  live.connect();
  requestAnimationFrame(frame);
  setInterval(() => renderFeed(store), 15000); // keep relative timestamps fresh
}

boot();

// Expose a tiny surface for debugging from the devtools console.
window.__codeviz = { store, forceLayout, layeredLayout, structureScene, flowScene, isElectron };
