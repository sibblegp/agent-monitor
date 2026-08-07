/**
 * Hero canvas: plays the scripted agent-working loop on the synthetic repo.
 *
 * Owns no rAF loop itself — main.js drives `tick(now)` only while the canvas
 * is on screen and the tab is visible.
 */

import {
  COLORS, rgba, clamp, glowNode, drawPulses, drawImplosion,
  drawEdgeParticles, strokeBezier, drawLabel, breathe, blinkAlpha,
} from './draw.js';
import { buildRepo } from './graph.js';
import { SCENARIO, LOOP_MS } from './scenario.js';

const HOT_MS = 9000;
const SPOTLIGHT_MS = 1800;

function nodeColor(node) {
  if (node.ghost) return COLORS.removed;
  if (node.status !== 'unchanged' && COLORS[node.status]) return COLORS[node.status];
  if (node.kind === 'dir') return COLORS.dir;
  if (node.kind === 'root') return COLORS.root;
  if (node.kind === 'file') return COLORS.file;
  return COLORS.unchanged;
}

export class HeroDemo {
  constructor(canvas, captionEl) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.captionEl = captionEl;
    this.dpr = 1;
    this.width = 0;
    this.height = 0;
    this._resize = this._resize.bind(this);
    this.observer = new ResizeObserver(this._resize);
    this.observer.observe(canvas);
    this._resize();
    this._startLoop(performance.now());
  }

  _resize() {
    const rect = this.canvas.getBoundingClientRect();
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.width = Math.max(1, rect.width);
    this.height = Math.max(1, rect.height);
    this.canvas.width = Math.round(this.width * this.dpr);
    this.canvas.height = Math.round(this.height * this.dpr);
  }

  _startLoop(now) {
    this.graph = buildRepo(7);
    this.graph.reheat(1);
    this.loopStart = now;
    this.applied = new Set();
    this.flows = [];        // {from, to, born}
    this.implosions = [];   // {x, y, r, start}
    this.spotlight = { until: 0, id: null };
    this.activeLabel = null;
    this.fading = 0;
  }

  _apply(event, now) {
    const g = this.graph;
    if (event.type === 'modify' || event.type === 'signature') {
      const node = g.nodes.get(event.id);
      if (node) {
        node.status = event.type === 'signature' ? 'signature_changed' : 'modified';
        node.hotUntil = now + HOT_MS;
        node.pulses.push({ start: now, kind: node.status, x: node.x, y: node.y, radius: node.r });
      }
      this._focus(event.id, now);
    } else if (event.type === 'add') {
      const node = g.add(event.id, event.kind, event.parent, event.label);
      node.status = 'added';
      node.born = now;
      node.hotUntil = now + HOT_MS;
      node.pulses.push({ start: now, kind: 'added', x: node.x, y: node.y, radius: node.r });
      g.reheat(0.5);
      this._focus(event.id, now);
    } else if (event.type === 'remove') {
      const node = g.nodes.get(event.id);
      if (node) {
        this.implosions.push({ x: node.x, y: node.y, r: node.r, start: now });
        node.ghost = true;
        node.status = 'removed';
        node.hotUntil = now + HOT_MS;
      }
      this._focus(event.id, now);
    } else if (event.type === 'flow') {
      this.flows.push({ from: event.from, to: event.to, born: now });
    } else if (event.type === 'fade') {
      this.fading = now;
    }
    if (event.caption && this.captionEl) {
      const cls = { modify: 'modified', signature: 'signature', add: 'added', remove: 'removed' }[event.type] || 'idle';
      this.captionEl.textContent = event.caption;
      this.captionEl.dataset.state = cls;
    }
  }

  _focus(id, now) {
    this.spotlight = { until: now + SPOTLIGHT_MS, id };
    this.activeLabel = { id, until: now + 5200 };
  }

  /** Advance and draw one frame. */
  tick(now) {
    const loopT = now - this.loopStart;
    if (loopT >= LOOP_MS) this._startLoop(now);
    for (const event of SCENARIO) {
      if (event.at <= now - this.loopStart && !this.applied.has(event)) {
        this.applied.add(event);
        this._apply(event, now);
      }
    }

    this.graph.step();
    this._draw(now);
  }

  /** One static, mid-scenario frame for prefers-reduced-motion. */
  renderStatic() {
    const now = performance.now();
    this._startLoop(now - 21000);
    for (const event of SCENARIO) {
      if (event.at <= 21000 && event.type !== 'fade') {
        this.applied.add(event);
        this._apply(event, now - 21000 + event.at);
      }
    }
    for (const node of this.graph.nodes.values()) node.pulses.length = 0;
    this.implosions.length = 0;
    this.spotlight.until = 0;
    for (let i = 0; i < 260; i++) this.graph.step();
    if (this.captionEl) this.captionEl.textContent = 'added  class Cache  src/model.py  +48';
    this._draw(now, { still: true });
  }

  _view() {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of this.graph.nodes.values()) {
      minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x);
      minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
    }
    const pad = 56;
    const w = Math.max(1, maxX - minX + pad * 2);
    const h = Math.max(1, maxY - minY + pad * 2);
    const scale = clamp(Math.min(this.width / w, this.height / h), 0.3, 1.6);
    return {
      scale,
      x: this.width / 2 - ((minX + maxX) / 2) * scale,
      y: this.height / 2 - ((minY + maxY) / 2) * scale,
    };
  }

  _draw(now, { still = false } = {}) {
    const ctx = this.ctx;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.width, this.height);

    const view = this._view();
    ctx.translate(view.x, view.y);
    ctx.scale(view.scale, view.scale);

    const spot = still ? 0 : clamp((this.spotlight.until - now) / SPOTLIGHT_MS, 0, 1);
    const fade = this.fading ? clamp((now - this.fading) / 2200, 0, 1) : 0;

    const dimFor = (node) => {
      let alpha = 1;
      if (spot > 0 && node.id !== this.spotlight.id) alpha *= 1 - spot * 0.62;
      return alpha;
    };

    // Containment edges.
    ctx.lineWidth = 1;
    for (const link of this.graph.links) {
      const a = this.graph.nodes.get(link.a);
      const b = this.graph.nodes.get(link.b);
      if (!a || !b) continue;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.strokeStyle = rgba(COLORS.edge, (b.ghost ? 0.25 : 0.55) * (1 - spot * 0.4));
      ctx.stroke();
    }

    // Call-flow edges with particles.
    for (const flow of this.flows) {
      const a = this.graph.nodes.get(flow.from);
      const b = this.graph.nodes.get(flow.to);
      if (!a || !b) continue;
      const age = clamp((now - flow.born) / 600, 0, 1);
      const life = fade ? (1 - fade) : 1;
      strokeBezier(ctx, a, b, COLORS.entry, 0.22 * age * life, 1);
      if (!still && life > 0.05) {
        drawEdgeParticles(ctx, a, b, now, {
          color: COLORS.entry,
          seed: (flow.born % 1000) / 1000,
          density: 3,
          speed: 1.7,
        });
      }
    }

    // Nodes.
    for (const node of this.graph.nodes.values()) {
      const color = nodeColor(node);
      let alpha = dimFor(node);
      let radius = node.r;
      let glow = 0;

      if (node.hotUntil && now < node.hotUntil) {
        const heat = (node.hotUntil - now) / HOT_MS;
        glow = 0.34 + heat * 0.95;
      }
      if (fade > 0) glow *= 1 - fade;

      if (node.ghost) {
        alpha *= 0.5;
        glow = 0;
      } else if (node.status === 'modified' && !still) {
        radius *= breathe(now, 1.2, 0.07);
      } else if (node.status === 'added' && node.born && !still) {
        alpha *= blinkAlpha(node.born, now);
      }

      glowNode(ctx, node.kind, node.x, node.y, radius, color, { alpha, glow });
      if (!still) drawPulses(ctx, node.pulses, now);
    }

    // Implosions.
    for (let i = this.implosions.length - 1; i >= 0; i--) {
      const imp = this.implosions[i];
      const t = (now - imp.start) / 900;
      if (t >= 1) {
        this.implosions.splice(i, 1);
        continue;
      }
      drawImplosion(ctx, imp.x, imp.y, imp.r, t);
    }

    // Label on the symbol currently being worked on.
    const active = this.activeLabel && this.graph.nodes.get(this.activeLabel.id);
    if (active && (still || now < this.activeLabel.until)) {
      const left = clamp((this.activeLabel.until - now) / 900, 0, 1);
      drawLabel(ctx, active.x, active.y - active.r - 14, active.label || active.id, {
        color: nodeColor(active),
        alpha: still ? 1 : Math.min(1, left * 2),
        size: 11,
      });
    }
  }
}
