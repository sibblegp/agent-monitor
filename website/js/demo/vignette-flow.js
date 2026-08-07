/**
 * Small canvas vignettes for the feature sections:
 *
 * - FlowVignette: a compact left-to-right call DAG with particles riding the
 *   edges, one node periodically pulsing amber (feature 1).
 * - ClusterVignette: a class hexagon with its method diamonds, one of them
 *   double-pulsing pink on a signature change (feature 3).
 */

import {
  COLORS, rgba, glowNode, drawPulses, drawEdgeParticles, strokeBezier,
  drawLabel, breathe,
} from './draw.js';

class Vignette {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.dpr = 1;
    this.width = 0;
    this.height = 0;
    this.observer = new ResizeObserver(() => this._resize());
    this.observer.observe(canvas);
    this._resize();
  }

  _resize() {
    const rect = this.canvas.getBoundingClientRect();
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.width = Math.max(1, rect.width);
    this.height = Math.max(1, rect.height);
    this.canvas.width = Math.round(this.width * this.dpr);
    this.canvas.height = Math.round(this.height * this.dpr);
  }

  _begin() {
    const ctx = this.ctx;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.width, this.height);
    return ctx;
  }

  /** Unit coords (0..1) to canvas pixels with padding. */
  _pt(u, v, pad = 26) {
    return {
      x: pad + u * (this.width - pad * 2),
      y: pad + v * (this.height - pad * 2),
    };
  }
}

export class FlowVignette extends Vignette {
  constructor(canvas) {
    super(canvas);
    this.pulses = [];
    this.lastPulse = -Infinity;
    // entry → fns → externals, layered like the flow pane.
    this.spec = [
      { id: 'main', kind: 'function', u: 0.04, v: 0.5, color: COLORS.entry, label: 'main', entry: true },
      { id: 'serve', kind: 'function', u: 0.36, v: 0.22, color: COLORS.unchanged, label: 'serve()' },
      { id: 'rescan', kind: 'function', u: 0.38, v: 0.62, color: COLORS.modified, label: 'rescan()', hot: true },
      { id: 'render', kind: 'method', u: 0.64, v: 0.42, color: COLORS.unchanged, label: 'render()' },
      { id: 'watch', kind: 'function', u: 0.6, v: 0.84, color: COLORS.unchanged, label: 'watch()' },
      { id: 'fastapi', kind: 'external', u: 0.94, v: 0.2, color: COLORS.external, label: 'fastapi' },
      { id: 'watchdog', kind: 'external', u: 0.94, v: 0.74, color: COLORS.external, label: 'watchdog' },
    ];
    this.edges = [
      ['main', 'serve'], ['main', 'rescan'], ['serve', 'render'],
      ['rescan', 'render'], ['rescan', 'watch'], ['serve', 'fastapi'], ['watch', 'watchdog'],
    ];
  }

  tick(now) {
    const ctx = this._begin();
    const at = Object.fromEntries(this.spec.map((s) => [s.id, { ...this._pt(s.u, s.v), spec: s }]));

    for (const [fromId, toId] of this.edges) {
      const a = at[fromId];
      const b = at[toId];
      const hot = fromId === 'rescan' || toId === 'rescan';
      strokeBezier(ctx, a, b, hot ? COLORS.modified : COLORS.edge, hot ? 0.3 : 0.55, 1);
      drawEdgeParticles(ctx, a, b, now, {
        color: b.spec.kind === 'external' ? COLORS.external : COLORS.entry,
        seed: (fromId.length * 0.13 + toId.length * 0.31) % 1,
        density: hot ? 3 : 2,
        speed: hot ? 1.7 : 1.1,
      });
    }

    // The changed node pulses every few seconds — work is happening here.
    const target = at.rescan;
    if (now - this.lastPulse > 4200) {
      this.lastPulse = now;
      this.pulses.push({ start: now, kind: 'modified', x: target.x, y: target.y, radius: 7 });
    }

    for (const s of this.spec) {
      const p = at[s.id];
      const radius = s.hot ? 7 * breathe(now, 1.2, 0.07) : s.entry ? 6.5 : 6;
      glowNode(ctx, s.kind, p.x, p.y, radius, s.color, {
        glow: s.hot ? 0.9 : s.entry ? 0.45 : 0,
        hollow: s.kind === 'external',
      });
      if (s.entry) {
        ctx.beginPath();
        ctx.arc(p.x, p.y, radius + 4, 0, Math.PI * 2);
        ctx.strokeStyle = rgba(COLORS.entry, 0.45);
        ctx.lineWidth = 1;
        ctx.stroke();
      }
      drawLabel(ctx, p.x, p.y + 18, s.label, {
        color: s.hot ? COLORS.modified : COLORS.dim,
        size: 10,
      });
    }
    drawPulses(ctx, this.pulses, now);
  }

  renderStatic() {
    this.tick(0);
  }
}

export class ClusterVignette extends Vignette {
  constructor(canvas) {
    super(canvas);
    this.pulses = [];
    this.lastPulse = -Infinity;
  }

  tick(now) {
    const ctx = this._begin();
    const hub = this._pt(0.5, 0.42);
    const methods = [
      { p: this._pt(0.16, 0.24), label: 'parse()', color: COLORS.unchanged, kind: 'method' },
      { p: this._pt(0.84, 0.2), label: 'walk()', color: COLORS.unchanged, kind: 'method' },
      { p: this._pt(0.2, 0.78), label: 'symbols()', color: COLORS.signature_changed, kind: 'method', hot: true },
      { p: this._pt(0.82, 0.8), label: 'hash()', color: COLORS.added, kind: 'method', fresh: true },
    ];

    for (const m of methods) {
      ctx.beginPath();
      ctx.moveTo(hub.x, hub.y);
      ctx.lineTo(m.p.x, m.p.y);
      ctx.strokeStyle = rgba(COLORS.edge, 0.55);
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    const target = methods[2];
    if (now - this.lastPulse > 5200) {
      this.lastPulse = now;
      this.pulses.push({
        start: now, kind: 'signature_changed', x: target.p.x, y: target.p.y, radius: 6,
      });
    }

    glowNode(ctx, 'class', hub.x, hub.y, 11, COLORS.file, { glow: 0.2 });
    drawLabel(ctx, hub.x, hub.y - 26, 'PyParser', { color: COLORS.dim, size: 10.5 });

    for (const m of methods) {
      const radius = m.hot ? 6 * breathe(now, 1.2, 0.07) : 5.5;
      glowNode(ctx, m.kind, m.p.x, m.p.y, radius, m.color, { glow: m.hot || m.fresh ? 0.85 : 0 });
      drawLabel(ctx, m.p.x, m.p.y + 18, m.label, {
        color: m.hot || m.fresh ? m.color : COLORS.faint,
        size: 10,
      });
    }
    drawPulses(ctx, this.pulses, now);
  }

  renderStatic() {
    this.tick(0);
  }
}
