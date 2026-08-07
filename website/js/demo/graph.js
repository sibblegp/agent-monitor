/**
 * Synthetic repo graph + a small force layout for the hero demo.
 *
 * The spring/charge constants are cribbed from the app's layout
 * (agent_monitor/static/js/layout/force.js), scaled down: ~40 nodes never
 * needs a spatial hash, so this runs all-pairs and stays tiny.
 */

import { mulberry32 } from './draw.js';

const RADII = { root: 13, dir: 9, file: 7, class: 6.5, method: 4.5, fn: 4.5 };

export function radiusFor(node) {
  return RADII[node.kind === 'function' ? 'fn' : node.kind] || 4.5;
}

export class DemoGraph {
  constructor(seed = 7) {
    this.rand = mulberry32(seed);
    this.nodes = new Map();
    this.links = [];
    this.alpha = 1;
  }

  add(id, kind, parent, label = null) {
    const p = this.nodes.get(parent);
    const angle = this.rand() * Math.PI * 2;
    const spread = p ? 26 : 200;
    const node = {
      id,
      kind,
      parent: parent || null,
      label,
      x: (p ? p.x : 0) + Math.cos(angle) * spread,
      y: (p ? p.y : 0) + Math.sin(angle) * spread,
      vx: 0,
      vy: 0,
      r: 0,
      status: 'unchanged',
      born: 0,
      ghost: false,
      pulses: [],
    };
    node.r = radiusFor(node);
    this.nodes.set(id, node);
    if (p) this.links.push({ a: parent, b: id });
    return node;
  }

  reheat(value = 0.7) {
    this.alpha = Math.max(this.alpha, value);
  }

  /** Sibling count under each parent, for orbit sizing. */
  _crowd() {
    const counts = new Map();
    for (const n of this.nodes.values()) {
      if (n.parent) counts.set(n.parent, (counts.get(n.parent) || 0) + 1);
    }
    return counts;
  }

  _linkDistance(child, siblings) {
    const crowd = Math.sqrt(Math.max(1, siblings));
    if (child.kind === 'dir') return 96 + crowd * 18;
    if (child.kind === 'file') return 62 + crowd * 12;
    return 26 + crowd * 10;
  }

  /** One physics tick; returns false once settled. */
  step() {
    if (this.alpha < 0.004) return false;
    const entries = [...this.nodes.values()];
    const crowd = this._crowd();

    for (const e of entries) {
      for (const other of entries) {
        if (other === e) continue;
        let dx = e.x - other.x;
        let dy = e.y - other.y;
        let d2 = dx * dx + dy * dy;
        if (d2 > 320 * 320) continue;
        if (d2 < 0.01) {
          dx = (this.rand() - 0.5) * 0.6;
          dy = (this.rand() - 0.5) * 0.6;
          d2 = dx * dx + dy * dy;
        }
        const dist = Math.sqrt(d2);
        const force = (620 + (e.r + other.r) * 40) / d2;
        e.vx += (dx / dist) * force * this.alpha;
        e.vy += (dy / dist) * force * this.alpha;
      }
    }

    for (const link of this.links) {
      const a = this.nodes.get(link.a);
      const b = this.nodes.get(link.b);
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.hypot(dx, dy) || 0.01;
      const target = this._linkDistance(b, crowd.get(link.a) || 1);
      const k = ((dist - target) / dist) * 0.085 * this.alpha;
      // Parents are heavier, so children do most of the moving.
      b.vx -= dx * k * 0.75;
      b.vy -= dy * k * 0.75;
      a.vx += dx * k * 0.25;
      a.vy += dy * k * 0.25;
    }

    for (const e of entries) {
      e.vx -= e.x * 0.0022 * this.alpha;
      e.vy -= e.y * 0.0022 * this.alpha;
      e.vx *= 0.84;
      e.vy *= 0.84;
      e.x += Math.max(-14, Math.min(14, e.vx));
      e.y += Math.max(-14, Math.min(14, e.vy));
    }

    this.alpha *= 0.976;
    return true;
  }
}

/** The little repo the hero agent "works on". */
export function buildRepo(seed = 7) {
  const g = new DemoGraph(seed);
  g.add('root', 'root', null, 'my-project');

  g.add('src', 'dir', 'root', 'src/');
  g.add('api', 'dir', 'root', 'api/');

  g.add('engine', 'file', 'src', 'engine.py');
  g.add('parser', 'file', 'src', 'parser.py');
  g.add('model', 'file', 'src', 'model.py');
  g.add('routes', 'file', 'api', 'routes.py');
  g.add('auth', 'file', 'api', 'auth.py');

  g.add('rescan', 'function', 'engine', 'rescan()');
  g.add('diff', 'function', 'engine', 'diff()');
  g.add('watch', 'function', 'engine', 'watch()');
  g.add('parse', 'function', 'parser', 'parse()');
  g.add('tokenize', 'function', 'parser', 'tokenize()');
  g.add('Symbol', 'class', 'model', 'Symbol');
  g.add('Symbol.hash', 'method', 'Symbol', 'hash()');
  g.add('Symbol.merge', 'method', 'Symbol', 'merge()');
  g.add('index', 'function', 'routes', 'index()');
  g.add('login', 'function', 'auth', 'login()');
  g.add('verify', 'function', 'auth', 'verify()');
  g.add('legacy', 'function', 'model', 'to_json()');

  return g;
}
