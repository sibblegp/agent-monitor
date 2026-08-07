/**
 * The client-side store.
 *
 * Holds the graph plus per-node animation bookkeeping (`fx`). Layout positions
 * live in the layout modules, because the same node sits at different
 * coordinates in the structure pane and the flow pane.
 */

export const CHANGED = new Set(['added', 'modified', 'signature_changed', 'removed', 'renamed']);

/**
 * How long a just-changed node keeps its glow.
 *
 * Animation is strictly event-driven: only nodes that changed in the *latest*
 * update move. Everything else — including changes from earlier in the session
 * — is drawn statically and identified by colour alone. Ambient motion competes
 * with the thing you actually want noticed.
 */
export const HOT_MS = 9000;

function makeFx() {
  return {
    born: 0,
    hotUntil: 0,
    pulses: [], // {start, kind}
    blinks: 0,
    dying: 0, // timestamp when removal started
    lastStatus: 'unchanged',
  };
}

export class Store {
  constructor() {
    this.nodes = new Map();
    this.edges = new Map();
    this.meta = null;
    this.changes = null;
    this.ai = null;
    this.focusNodes = new Set();
    this.focusEdges = new Set();
    this.fx = new Map();

    this.filter = 'all'; // all | changes
    this.depth = 2;
    this.paused = false;
    this.hover = null;
    this.selected = null;
    this.searchHits = new Set();

    this.events = []; // activity feed entries
    this.version = 0; // bumped whenever the graph shape changes
    this.listeners = new Set();

    /** Nodes that changed in the most recent update — the only ones that animate. */
    this.recent = new Set();
    this.recentAt = 0;
    /** First snapshot establishes the baseline and deliberately animates nothing. */
    this.hasBaseline = false;
  }

  /** True while `id` is part of the latest change burst. */
  isRecent(id, now) {
    return this.recent.has(id) && now - this.recentAt < HOT_MS;
  }

  get hasLiveActivity() {
    return this.recent.size > 0 && performance.now() - this.recentAt < HOT_MS;
  }

  on(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  emit(kind, payload) {
    for (const fn of this.listeners) fn(kind, payload);
  }

  fxOf(id) {
    let fx = this.fx.get(id);
    if (!fx) {
      fx = makeFx();
      this.fx.set(id, fx);
    }
    return fx;
  }

  /** Replace the whole graph (initial load, mode switch, repo switch). */
  applySnapshot(payload, { animate = false } = {}) {
    const now = performance.now();
    const previous = this.nodes;

    this.nodes = new Map(payload.nodes.map((n) => [n.id, n]));
    this.edges = new Map(payload.edges.map((e) => [e.id, e]));
    this.meta = payload.meta || null;
    this.changes = payload.changes || null;
    this.ai = payload.ai || null;
    this.focusNodes = new Set(payload.focus?.nodes || []);
    this.focusEdges = new Set(payload.focus?.edges || []);

    // Drop fx for nodes that no longer exist, keep it for survivors so their
    // glow timers carry across a rescan.
    for (const id of [...this.fx.keys()]) {
      if (!this.nodes.has(id)) this.fx.delete(id);
    }

    // Only nodes whose status changed *in this update* animate. On the very
    // first snapshot nothing animates at all — pre-existing changes aren't
    // "work happening now", they're just the state of the tree.
    const firstLoad = !this.hasBaseline;
    const justChanged = new Set();

    for (const [id, node] of this.nodes) {
      const fx = this.fxOf(id);
      const isNew = !previous.has(id);
      if (fx.born === 0) fx.born = animate && isNew && !firstLoad ? now : now - 5000;

      if (!firstLoad && CHANGED.has(node.status) && fx.lastStatus !== node.status) {
        justChanged.add(id);
        fx.hotUntil = now + HOT_MS;
        fx.pulses.push({ start: now, kind: node.status });
        if (node.status === 'added') fx.blinks = 3;
      }
      fx.lastStatus = node.status;
    }

    if (justChanged.size) {
      this.recent = justChanged;
      this.recentAt = now;
    }
    this.hasBaseline = true;

    this.version += 1;
    this.emit('snapshot', payload);
  }

  /** Merge AI annotations onto existing nodes without touching graph shape. */
  applyAi(payload) {
    this.ai = payload;
    for (const item of payload.summaries || []) {
      const node = this.nodes.get(item.id);
      if (node) node.summary = item.text;
    }
    for (const item of payload.risk || []) {
      const node = this.nodes.get(item.id);
      if (node) {
        node.risk = item.level;
        node.risk_reason = item.reason;
      }
    }
    for (const theme of payload.themes || []) {
      for (const id of theme.members || []) {
        const node = this.nodes.get(id);
        if (node) node.theme = theme.name;
      }
    }
    this.emit('ai', payload);
  }

  /** Nodes visible under the current filter. */
  isVisible(node) {
    if (this.filter === 'all') return true;
    return this.focusNodes.has(node.id);
  }

  isEdgeVisible(edge) {
    if (this.filter === 'all') return true;
    return this.focusEdges.has(edge.id);
  }

  childrenOf(id) {
    const out = [];
    for (const node of this.nodes.values()) if (node.parent === id) out.push(node);
    return out;
  }

  /** Walk up the containment chain. */
  ancestors(id) {
    const out = [];
    let cursor = this.nodes.get(id);
    while (cursor?.parent) {
      out.push(cursor.parent);
      cursor = this.nodes.get(cursor.parent);
    }
    return out;
  }

  /** Direct call neighbours of a node, both directions. */
  callNeighbours(id) {
    const out = new Set();
    for (const edge of this.edges.values()) {
      if (edge.kind !== 'calls') continue;
      if (edge.src === id) out.add(edge.dst);
      else if (edge.dst === id) out.add(edge.src);
    }
    return out;
  }

  /**
   * The set to keep bright when something is focused: the node, its call
   * neighbours, and its containment ancestors. Everything else dims.
   */
  highlightSet(id) {
    if (!id) return null;
    const set = new Set([id, ...this.callNeighbours(id), ...this.ancestors(id)]);
    for (const node of this.nodes.values()) if (node.parent === id) set.add(node.id);
    return set;
  }

  pushEvent(entry) {
    this.events.unshift({ ...entry, at: Date.now() });
    if (this.events.length > 200) this.events.length = 200;
    this.emit('event', entry);
  }

  /** Build feed entries from a fresh changeset. */
  seedEventsFromChanges() {
    if (!this.changes) return;
    this.events = [];
    const symbols = (this.changes.symbols || []).filter((s) => CHANGED.has(s.status));
    const source = symbols.length ? symbols : this.changes.files || [];
    for (const item of source.slice(0, 60)) {
      this.events.push({
        at: Date.now(),
        id: item.id || `file:${item.path}`,
        status: item.status,
        name: item.name || item.path.split('/').pop(),
        path: item.path,
        added: item.added || 0,
        removed: item.removed || 0,
        kind: item.kind || 'file',
      });
    }
    this.emit('event', null);
  }
}

export const store = new Store();
