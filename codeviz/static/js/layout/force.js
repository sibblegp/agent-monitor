/**
 * Force-directed layout for the structure pane.
 *
 * Files collapse to a single node by default so a large repo stays readable;
 * a file that changed auto-expands into a ring of its classes and functions,
 * which is what puts the changed region at the visual center of attention.
 *
 * Repulsion uses a spatial hash rather than an all-pairs loop, so this stays
 * linear-ish in the node count instead of quadratic.
 */

import { clamp } from '../render/effects.js';

const CELL = 90;

export function radiusFor(node) {
  if (node.kind === 'root') return 16;
  if (node.kind === 'dir') return clamp(6 + Math.sqrt(node.size) * 0.35, 7, 20);
  if (node.kind === 'file') return clamp(4 + Math.sqrt(node.size) * 0.75, 5, 20);
  if (node.kind === 'external') return 5.5;
  return clamp(3 + Math.sqrt(node.size) * 0.9, 3.5, 15);
}

export class ForceLayout {
  constructor(store) {
    this.store = store;
    this.pos = new Map(); // id -> {x,y,vx,vy,r,node}
    this.visible = [];
    this.links = [];
    this.expanded = new Set();
    this.manualCollapse = new Set(); // user overrode an auto-expand
    this.alpha = 1;
    this.version = -1;
  }

  /**
   * Which files should show their symbols right now.
   *
   * Changed files auto-expand, but only the busiest AUTO_EXPAND_CAP of them.
   * On a first commit — or any sweeping refactor — every file is "changed", and
   * expanding all of them at once buries the signal in a wall of nodes. The cap
   * keeps attention on the files with the most churn; the rest stay collapsed
   * and can be opened by clicking.
   */
  computeExpansion() {
    const AUTO_EXPAND_CAP = 12;
    const next = new Set();
    const candidates = [];

    for (const node of this.store.nodes.values()) {
      if (node.kind !== 'file') continue;
      if (this.manualCollapse.has(node.id)) continue;
      // A file the user opened by hand stays open regardless of the cap.
      if (this.expanded.has(node.id)) next.add(node.id);
      if (node.status && node.status !== 'unchanged') candidates.push(node);
    }

    candidates.sort(
      (a, b) => (b.added || 0) + (b.removed || 0) - ((a.added || 0) + (a.removed || 0))
    );

    this.autoExpandSkipped = Math.max(0, candidates.length - AUTO_EXPAND_CAP);
    for (const node of candidates.slice(0, AUTO_EXPAND_CAP)) next.add(node.id);

    this.expanded = next;
  }

  toggle(id) {
    const node = this.store.nodes.get(id);
    if (!node || node.kind !== 'file') return false;
    if (this.expanded.has(id)) {
      this.expanded.delete(id);
      this.manualCollapse.add(id);
    } else {
      this.expanded.add(id);
      this.manualCollapse.delete(id);
    }
    this.sync(true);
    return true;
  }

  /** True when a node should participate in the layout. */
  _isShown(node) {
    if (node.kind === 'external') return false; // externals live in the flow pane
    if (!this.store.isVisible(node)) return false;
    if (node.kind === 'root' || node.kind === 'dir' || node.kind === 'file') return true;
    // symbol: only when its owning file is expanded
    const file = this._fileOf(node);
    return file ? this.expanded.has(file) : false;
  }

  _fileOf(node) {
    let cursor = node;
    let guard = 0;
    while (cursor && guard++ < 12) {
      if (cursor.kind === 'file') return cursor.id;
      cursor = this.store.nodes.get(cursor.parent);
    }
    return null;
  }

  /** Reconcile the layout with the current graph. Cheap when nothing changed. */
  sync(force = false) {
    if (!force && this.version === this.store.version) return;
    this.version = this.store.version;
    this.computeExpansion();

    const shown = [];
    for (const node of this.store.nodes.values()) {
      if (this._isShown(node)) shown.push(node);
    }
    const ids = new Set(shown.map((n) => n.id));

    // Distinguish "the graph actually changed shape" from "only statuses
    // changed". During live editing the latter is the common case, and
    // reheating hard for it makes the whole layout lurch on every keystroke.
    const sameShape =
      ids.size === this.pos.size && [...ids].every((id) => this.pos.has(id));

    for (const id of [...this.pos.keys()]) {
      if (!ids.has(id)) this.pos.delete(id);
    }

    for (const node of shown) {
      let entry = this.pos.get(node.id);
      if (!entry) {
        // Spawn at the parent so new nodes visibly fly out of their container.
        const parent = this.pos.get(node.parent);
        const angle = Math.random() * Math.PI * 2;
        const spread = parent ? 26 : 220;
        entry = {
          x: (parent?.x ?? 0) + Math.cos(angle) * spread,
          y: (parent?.y ?? 0) + Math.sin(angle) * spread,
          vx: 0,
          vy: 0,
        };
        this.pos.set(node.id, entry);
      }
      entry.node = node;
      entry.r = radiusFor(node);
    }

    this.visible = shown;

    // Containment links only — call edges are the flow pane's business.
    this.links = [];
    const siblings = new Map();
    for (const node of shown) {
      if (node.parent && this.pos.has(node.parent)) {
        siblings.set(node.parent, (siblings.get(node.parent) || 0) + 1);
      }
    }
    for (const node of shown) {
      if (node.parent && this.pos.has(node.parent)) {
        this.links.push({ a: node.parent, b: node.id, node, siblings: siblings.get(node.parent) || 1 });
      }
    }

    this.reheat(sameShape ? 0.05 : 0.85);
  }

  reheat(value = 0.9) {
    this.alpha = Math.max(this.alpha, value);
  }

  /**
   * Orbit radius grows with sibling count. A file with 20 methods needs a much
   * bigger ring than one with 2, otherwise dense files collapse into an
   * unreadable knot — which is exactly where the interesting changes are.
   */
  _linkDistance(link) {
    const kind = link.node.kind;
    const crowd = Math.sqrt(Math.max(1, link.siblings || 1));
    if (kind === 'dir') return 150 + crowd * 26;
    if (kind === 'file') return 90 + crowd * 16;
    return 30 + crowd * 13;
  }

  _linkStrength(link) {
    return link.node.kind === 'dir' ? 0.05 : 0.085;
  }

  /** One physics tick. */
  step() {
    if (this.alpha < 0.004 || this.visible.length === 0) return false;
    const entries = [...this.pos.values()];

    // ── repulsion via spatial hash ─────────────────────────────────
    const grid = new Map();
    for (const e of entries) {
      const key = `${Math.round(e.x / CELL)},${Math.round(e.y / CELL)}`;
      let bucket = grid.get(key);
      if (!bucket) grid.set(key, (bucket = []));
      bucket.push(e);
    }

    for (const e of entries) {
      const cx = Math.round(e.x / CELL);
      const cy = Math.round(e.y / CELL);
      for (let ox = -1; ox <= 1; ox++) {
        for (let oy = -1; oy <= 1; oy++) {
          const bucket = grid.get(`${cx + ox},${cy + oy}`);
          if (!bucket) continue;
          for (const other of bucket) {
            if (other === e) continue;
            let dx = e.x - other.x;
            let dy = e.y - other.y;
            let d2 = dx * dx + dy * dy;
            if (d2 > CELL * CELL * 4) continue;
            if (d2 < 0.01) {
              dx = (Math.random() - 0.5) * 0.6;
              dy = (Math.random() - 0.5) * 0.6;
              d2 = dx * dx + dy * dy;
            }
            const dist = Math.sqrt(d2);
            const force = (900 + (e.r + other.r) * 55) / d2;
            e.vx += (dx / dist) * force * this.alpha;
            e.vy += (dy / dist) * force * this.alpha;
          }
        }
      }
    }

    // ── containment springs ────────────────────────────────────────
    for (const link of this.links) {
      const a = this.pos.get(link.a);
      const b = this.pos.get(link.b);
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.hypot(dx, dy) || 0.01;
      const target = this._linkDistance(link);
      const k = ((dist - target) / dist) * this._linkStrength(link) * this.alpha;
      const fx = dx * k;
      const fy = dy * k;
      // Parents are heavier, so children do most of the moving.
      b.vx -= fx * 0.75;
      b.vy -= fy * 0.75;
      a.vx += fx * 0.25;
      a.vy += fy * 0.25;
    }

    // ── gravity toward origin, and collision relaxation ────────────
    for (const e of entries) {
      e.vx -= e.x * 0.0016 * this.alpha;
      e.vy -= e.y * 0.0016 * this.alpha;
      e.vx *= 0.84;
      e.vy *= 0.84;
      e.x += clamp(e.vx, -18, 18);
      e.y += clamp(e.vy, -18, 18);
    }

    for (const e of entries) {
      const cx = Math.round(e.x / CELL);
      const cy = Math.round(e.y / CELL);
      for (let ox = -1; ox <= 1; ox++) {
        for (let oy = -1; oy <= 1; oy++) {
          const bucket = grid.get(`${cx + ox},${cy + oy}`);
          if (!bucket) continue;
          for (const other of bucket) {
            if (other === e) continue;
            const dx = other.x - e.x;
            const dy = other.y - e.y;
            const min = e.r + other.r + 3;
            const d2 = dx * dx + dy * dy;
            if (d2 >= min * min || d2 === 0) continue;
            const dist = Math.sqrt(d2);
            const push = ((dist - min) / dist) * 0.5;
            e.x += dx * push;
            e.y += dy * push;
            other.x -= dx * push;
            other.y -= dy * push;
          }
        }
      }
    }

    this.alpha *= 0.976;
    return true;
  }

  get(id) {
    return this.pos.get(id);
  }

  points() {
    return [...this.pos.values()];
  }
}
