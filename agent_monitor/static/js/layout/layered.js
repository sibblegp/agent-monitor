/**
 * Layered (Sugiyama-lite) layout for the call-flow pane.
 *
 * Entry points anchor layer 0 on the left; each call hop moves one layer right,
 * ending at external packages. Cycles are handled by demoting back-edges rather
 * than failing, and node order inside each layer is settled with barycenter
 * sweeps to cut crossings.
 *
 * Positions are *targets* — the renderer tweens toward them, so a graph change
 * slides nodes into place instead of teleporting them.
 */

import { clamp } from '../render/effects.js';

const LAYER_GAP = 210;
const ROW_GAP = 30;
const MAX_NODES = 1200;

export function flowRadiusFor(node) {
  if (node.kind === 'external') return 5;
  if (node.kind === 'file') return 6;
  return clamp(3.5 + Math.sqrt(node.size) * 0.7, 4, 12);
}

export class LayeredLayout {
  constructor(store) {
    this.store = store;
    this.pos = new Map(); // id -> {x, y, tx, ty, r, node}
    this.nodes = [];
    this.edges = [];
    this.layers = [];
    this.version = -1;
    this.signature = '';
    this.truncated = 0;
  }

  /** Which call edges are in play under the current filter. */
  _collect() {
    const { store } = this;
    // In "changes", the flow pane may reach `depth` call hops past the changed
    // set — an edge counts as long as both of its endpoints are in that reach.
    const reach = store.flowNodes();
    const edges = [];
    for (const edge of store.edges.values()) {
      if (edge.kind !== 'calls') continue;
      const src = store.nodes.get(edge.src);
      const dst = store.nodes.get(edge.dst);
      if (!src || !dst) continue;
      if (reach && !(reach.has(src.id) && reach.has(dst.id))) continue;
      edges.push(edge);
    }

    const degree = new Map();
    for (const edge of edges) {
      degree.set(edge.src, (degree.get(edge.src) || 0) + 1);
      degree.set(edge.dst, (degree.get(edge.dst) || 0) + 1);
    }

    let ids = [...degree.keys()];
    this.truncated = 0;
    if (ids.length > MAX_NODES) {
      // Keep what matters: changed symbols first, then entry points, then hubs.
      const score = (id) => {
        const node = store.nodes.get(id);
        if (!node) return -1;
        let value = degree.get(id) || 0;
        if (node.status && node.status !== 'unchanged') value += 10000;
        if (node.is_entry) value += 500;
        return value;
      };
      ids.sort((a, b) => score(b) - score(a));
      this.truncated = ids.length - MAX_NODES;
      ids = ids.slice(0, MAX_NODES);
    }

    const keep = new Set(ids);
    return {
      ids,
      edges: edges.filter((e) => keep.has(e.src) && keep.has(e.dst)),
    };
  }

  sync(force = false) {
    const { store } = this;
    const signature = `${store.version}|${store.filter}|${store.depth}`;
    if (!force && signature === this.signature) return;
    this.signature = signature;

    const { ids, edges } = this._collect();
    this.edges = edges;

    const outgoing = new Map();
    const incoming = new Map();
    for (const id of ids) {
      outgoing.set(id, []);
      incoming.set(id, []);
    }
    for (const edge of edges) {
      outgoing.get(edge.src).push(edge.dst);
      incoming.get(edge.dst).push(edge.src);
    }

    const layer = this._assignLayers(ids, outgoing, incoming);
    const ordered = this._orderLayers(ids, layer, outgoing, incoming);

    // ── target positions ─────────────────────────────────────────────
    const alive = new Set(ids);
    for (const id of [...this.pos.keys()]) {
      if (!alive.has(id)) this.pos.delete(id);
    }

    this.layers = ordered;
    ordered.forEach((rows, layerIndex) => {
      const height = (rows.length - 1) * ROW_GAP;
      rows.forEach((id, rowIndex) => {
        const node = this.store.nodes.get(id);
        if (!node) return;
        const tx = layerIndex * LAYER_GAP;
        const ty = rowIndex * ROW_GAP - height / 2;
        let entry = this.pos.get(id);
        if (!entry) {
          // New nodes slide in from the left edge of their layer.
          entry = { x: tx - 60, y: ty, tx, ty, r: flowRadiusFor(node), node };
          this.pos.set(id, entry);
        }
        entry.tx = tx;
        entry.ty = ty;
        entry.r = flowRadiusFor(node);
        entry.node = node;
        entry.layer = layerIndex;
      });
    });

    this.nodes = ids.map((id) => this.pos.get(id)).filter(Boolean);
  }

  /** Longest-path layering, with cycle back-edges demoted instead of exploding. */
  _assignLayers(ids, outgoing, incoming) {
    const layer = new Map(ids.map((id) => [id, 0]));
    const indeg = new Map(ids.map((id) => [id, incoming.get(id).length]));
    const queue = ids.filter((id) => indeg.get(id) === 0);
    const seen = new Set(queue);

    while (queue.length) {
      const id = queue.shift();
      for (const next of outgoing.get(id) || []) {
        layer.set(next, Math.max(layer.get(next) || 0, (layer.get(id) || 0) + 1));
        indeg.set(next, indeg.get(next) - 1);
        if (indeg.get(next) <= 0 && !seen.has(next)) {
          seen.add(next);
          queue.push(next);
        }
      }
    }

    // Anything left is inside a cycle: place it after its deepest resolved
    // predecessor so the drawing still reads left-to-right.
    for (const id of ids) {
      if (seen.has(id)) continue;
      let best = 0;
      for (const pred of incoming.get(id) || []) {
        if (seen.has(pred)) best = Math.max(best, (layer.get(pred) || 0) + 1);
      }
      layer.set(id, best);
    }

    // Externals always sit at the far right — they're the leaves of any trace.
    let maxLayer = 0;
    for (const id of ids) maxLayer = Math.max(maxLayer, layer.get(id) || 0);
    for (const id of ids) {
      const node = this.store.nodes.get(id);
      if (node?.kind === 'external') layer.set(id, maxLayer);
    }

    return layer;
  }

  /** Two barycenter sweeps — cheap, and removes most crossings. */
  _orderLayers(ids, layer, outgoing, incoming) {
    const maxLayer = Math.max(0, ...ids.map((id) => layer.get(id) || 0));
    const layers = Array.from({ length: maxLayer + 1 }, () => []);
    for (const id of ids) layers[layer.get(id) || 0].push(id);

    for (const rows of layers) {
      rows.sort((a, b) => {
        const na = this.store.nodes.get(a);
        const nb = this.store.nodes.get(b);
        return (na?.path || '').localeCompare(nb?.path || '') || a.localeCompare(b);
      });
    }

    const indexIn = (rows) => new Map(rows.map((id, i) => [id, i]));

    for (let pass = 0; pass < 2; pass++) {
      for (let i = 1; i < layers.length; i++) {
        const previous = indexIn(layers[i - 1]);
        layers[i] = this._byBarycenter(layers[i], incoming, previous);
      }
      for (let i = layers.length - 2; i >= 0; i--) {
        const next = indexIn(layers[i + 1]);
        layers[i] = this._byBarycenter(layers[i], outgoing, next);
      }
    }

    return layers;
  }

  _byBarycenter(rows, adjacency, neighbourIndex) {
    const score = new Map();
    rows.forEach((id, fallback) => {
      const neighbours = (adjacency.get(id) || [])
        .map((n) => neighbourIndex.get(n))
        .filter((v) => v !== undefined);
      score.set(
        id,
        neighbours.length
          ? neighbours.reduce((a, b) => a + b, 0) / neighbours.length
          : fallback
      );
    });
    return [...rows].sort((a, b) => score.get(a) - score.get(b));
  }

  /** Ease every node toward its target. Returns true while still moving. */
  step() {
    let moving = false;
    for (const entry of this.pos.values()) {
      const dx = entry.tx - entry.x;
      const dy = entry.ty - entry.y;
      if (Math.abs(dx) > 0.4 || Math.abs(dy) > 0.4) moving = true;
      entry.x += dx * 0.16;
      entry.y += dy * 0.16;
    }
    return moving;
  }

  get(id) {
    return this.pos.get(id);
  }

  points() {
    return [...this.pos.values()];
  }
}
