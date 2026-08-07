/**
 * Structure pane renderer: the living force graph of the codebase.
 *
 * Draw order matters — theme hulls sit behind everything, then containment
 * links, then an additive glow pass, then node cores, then labels and pulses on
 * top. Removed nodes are kept as dim crimson ghosts rather than dropped, so a
 * reviewer can still see what was deleted.
 */

import {
  COLORS,
  blinkAlpha,
  breathe,
  clamp,
  drawLabel,
  drawPulses,
  glowDot,
  hashUnit,
  heat,
  isChanged,
  rgba,
  ringDot,
} from './effects.js';
import { HOT_MS } from '../state.js';

const THEME_COLORS = ['#38bdf8', '#a06bff', '#3ddc84', '#f5a623', '#ff5fa2', '#7ee6ff'];

export class StructureRenderer {
  constructor(scene, layout, store) {
    this.scene = scene;
    this.layout = layout;
    this.store = store;
  }

  draw(now) {
    const { scene, layout, store } = this;
    const ctx = scene.begin();
    const view = scene.viewBounds();
    const highlight = store.highlightSet(store.selected || store.hover);

    this._drawThemes(ctx, now, highlight);
    this._drawLinks(ctx, view, highlight);
    this._drawNodes(ctx, now, view, highlight);

    scene.end();
    this._drawOverlay(ctx);
  }

  // ── AI change themes, drawn as soft hulls behind their members ──────

  _drawThemes(ctx, now, highlight) {
    const themes = this.store.ai?.themes;
    if (!themes?.length) return;

    themes.forEach((theme, index) => {
      const points = [];
      for (const id of theme.members || []) {
        const entry = this.layout.get(id);
        if (entry) points.push(entry);
      }
      if (points.length < 2) return;

      let minX = Infinity;
      let minY = Infinity;
      let maxX = -Infinity;
      let maxY = -Infinity;
      for (const p of points) {
        minX = Math.min(minX, p.x - p.r);
        minY = Math.min(minY, p.y - p.r);
        maxX = Math.max(maxX, p.x + p.r);
        maxY = Math.max(maxY, p.y + p.r);
      }
      const pad = 26;
      const color = THEME_COLORS[index % THEME_COLORS.length];
      const alpha = highlight ? 0.05 : 0.1;

      ctx.save();
      ctx.beginPath();
      ctx.roundRect(minX - pad, minY - pad, maxX - minX + pad * 2, maxY - minY + pad * 2, 20);
      ctx.fillStyle = rgba(color, alpha);
      ctx.fill();
      ctx.strokeStyle = rgba(color, alpha * 2.6);
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 5]);
      ctx.stroke();
      ctx.restore();

      drawLabel(ctx, (minX + maxX) / 2, minY - pad - 8, theme.name, {
        color,
        alpha: 0.85,
        size: 10,
      });
    });
  }

  // ── containment links ──────────────────────────────────────────────

  _drawLinks(ctx, view, highlight) {
    ctx.lineWidth = 1;
    for (const link of this.layout.links) {
      const a = this.layout.get(link.a);
      const b = this.layout.get(link.b);
      if (!a || !b) continue;
      if (
        Math.max(a.x, b.x) < view.minX ||
        Math.min(a.x, b.x) > view.maxX ||
        Math.max(a.y, b.y) < view.minY ||
        Math.min(a.y, b.y) > view.maxY
      ) {
        continue;
      }

      const changed = isChanged(link.node);
      let alpha = changed ? 0.3 : 0.11;
      if (highlight && !(highlight.has(link.a) && highlight.has(link.b))) alpha *= 0.25;

      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.strokeStyle = rgba(changed ? COLORS[link.node.status] || COLORS.dim : '#2a3442', alpha);
      ctx.stroke();
    }
  }

  // ── nodes ──────────────────────────────────────────────────────────

  _drawNodes(ctx, now, view, highlight) {
    const { layout, store, scene } = this;
    const scale = scene.camera.scale;
    const showAllLabels = scale > 1.15;

    for (const entry of layout.points()) {
      const node = entry.node;
      if (!node) continue;
      if (
        entry.x + entry.r < view.minX ||
        entry.x - entry.r > view.maxX ||
        entry.y + entry.r < view.minY ||
        entry.y - entry.r > view.maxY
      ) {
        continue;
      }

      const fx = store.fxOf(node.id);
      const changed = isChanged(node);
      const hot = heat(fx, now, HOT_MS);
      const dimmed = highlight && !highlight.has(node.id);

      let alpha = dimmed ? 0.16 : 1;
      let color = this._colorFor(node);
      let radius = entry.r;

      if (node.status === 'removed') {
        alpha *= 0.5;
      } else if (node.status === 'modified') {
        radius *= breathe(now, 1.2, 0.06);
      } else if (node.status === 'added') {
        alpha *= blinkAlpha(fx, now);
      }

      // Entry-point functions get a cyan halo so the flow roots are findable
      // in the structure pane too.
      if (node.is_entry && !changed && node.kind !== 'file') {
        ringDot(ctx, entry.x, entry.y, radius + 3.5, COLORS.entry, {
          alpha: alpha * 0.45,
          width: 1,
        });
      }

      const glow = changed ? 0.55 + hot * 0.9 : node.kind === 'file' ? 0.12 : 0.05;
      glowDot(ctx, entry.x, entry.y, radius, color, { alpha, glow: dimmed ? 0 : glow });

      // A file that's expanded gets a faint containing ring, so you can tell
      // "this file has its guts showing" from "this is a lone file node".
      if (node.kind === 'file' && layout.expanded.has(node.id)) {
        ringDot(ctx, entry.x, entry.y, radius + 2.5, color, { alpha: alpha * 0.3, width: 1 });
      }

      if (node.risk === 'high' && !dimmed) {
        ringDot(ctx, entry.x, entry.y, radius + 5.5, COLORS.removed, { alpha: 0.5, width: 1.2 });
      }

      drawPulses(ctx, fx, entry.x, entry.y, radius, now);

      scene.hitboxes.push({ id: node.id, x: entry.x, y: entry.y, r: radius });

      const labelWorth =
        changed ||
        node.kind === 'root' ||
        node.kind === 'dir' ||
        store.hover === node.id ||
        store.selected === node.id ||
        store.searchHits.has(node.id) ||
        (showAllLabels && node.kind === 'file');

      if (labelWorth && !dimmed) {
        const size = node.kind === 'root' ? 12 : node.kind === 'dir' ? 10.5 : 10;
        drawLabel(ctx, entry.x, entry.y - radius - 9, node.name, {
          color: changed ? color : COLORS.dim,
          alpha: clamp(dimmed ? 0.2 : 0.92, 0, 1),
          size,
        });
      }
    }
  }

  _colorFor(node) {
    if (node.status && node.status !== 'unchanged' && COLORS[node.status]) {
      return COLORS[node.status];
    }
    if (node.kind === 'root') return '#7f8ea3';
    if (node.kind === 'dir') return COLORS.dir;
    if (node.kind === 'file') return '#4d5a6e';
    if (node.is_entry) return COLORS.entry;
    return COLORS.unchanged;
  }

  // ── screen-space overlay (not affected by the camera) ───────────────

  _drawOverlay(ctx) {
    const notice = this.store.meta?.truncated_notice;
    if (!notice) return;
    ctx.save();
    ctx.setTransform(this.scene.dpr, 0, 0, this.scene.dpr, 0, 0);
    drawLabel(ctx, this.scene.width / 2, this.scene.height - 16, notice, {
      color: COLORS.modified,
      alpha: 0.9,
      size: 10.5,
    });
    ctx.restore();
  }
}

/** Stable jitter so repeated layouts of the same node look the same. */
export function jitterFor(id) {
  return hashUnit(id) * Math.PI * 2;
}
