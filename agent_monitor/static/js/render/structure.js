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
  glowNode,
  hashUnit,
  heat,
  isChanged,
  rgba,
  ringShape,
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
    const spotlight = highlight ? 0 : store.spotlight;

    // Screen-space occupancy grid, reset each frame. Labels claim cells in
    // priority order so a dense cluster shows the changed symbols' names
    // rather than whichever node happened to be drawn last.
    this._labelCells = new Set();
    this._labelQueue = [];

    this._drawThemes(ctx, now, highlight);
    this._drawLinks(ctx, view, highlight);
    this._drawNodes(ctx, now, view, highlight, spotlight);
    this._flushLabels(ctx);

    scene.end();
    this._drawOverlay(ctx);
  }

  /** Draw queued labels highest-priority first, skipping any that collide. */
  _flushLabels(ctx) {
    const scale = this.scene.camera.scale;
    const CELL_W = 74;
    const CELL_H = 15;

    this._labelQueue.sort((a, b) => b.priority - a.priority);

    for (const item of this._labelQueue) {
      const screen = this.scene.camera.toScreen(item.x, item.y);
      // Off-screen labels shouldn't consume cells that on-screen ones need.
      if (
        screen.x < -80 ||
        screen.y < -20 ||
        screen.x > this.scene.width + 80 ||
        screen.y > this.scene.height + 20
      ) {
        continue;
      }

      const width = Math.max(1, Math.round((item.text.length * item.size * 0.62) / CELL_W));
      const col = Math.floor(screen.x / CELL_W);
      const row = Math.floor(screen.y / CELL_H);

      let free = true;
      for (let c = col - Math.floor(width / 2); c <= col + Math.ceil(width / 2); c++) {
        if (this._labelCells.has(`${c},${row}`)) {
          free = false;
          break;
        }
      }
      // Always-show labels (hover, selection, search) ignore collisions —
      // the thing you're pointing at must be readable.
      if (!free && !item.force) continue;

      for (let c = col - Math.floor(width / 2); c <= col + Math.ceil(width / 2); c++) {
        this._labelCells.add(`${c},${row}`);
      }

      drawLabel(ctx, item.x, item.y, item.text, {
        color: item.color,
        alpha: item.alpha,
        size: item.size / scale > 22 ? item.size : item.size,
      });
    }
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

  _drawNodes(ctx, now, view, highlight, spotlight = 0) {
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
      // Only the latest change burst animates; older changes stay legible
      // through colour alone.
      const recent = store.isRecent(node.id, now);
      const hot = recent ? heat(fx, now, HOT_MS) : 0;
      const dimmed = highlight && !highlight.has(node.id);

      let alpha = dimmed ? 0.16 : 1;
      // Everything that didn't just change fades back for a moment, so a
      // single small edit still reads instantly in a crowded graph.
      if (spotlight > 0 && !recent) alpha *= 1 - spotlight * 0.62;
      let color = this._colorFor(node);
      let radius = entry.r;

      if (node.status === 'removed') {
        alpha *= 0.5;
      } else if (recent && node.status === 'modified') {
        radius *= breathe(now, 1.2, 0.06);
      } else if (recent && node.status === 'added') {
        alpha *= blinkAlpha(fx, now);
      }

      // Entry-point functions get a cyan halo so the flow roots are findable
      // in the structure pane too.
      if (node.is_entry && !changed && node.kind !== 'file') {
        ringShape(ctx, node.kind, entry.x, entry.y, radius + 3.5, COLORS.entry, {
          alpha: alpha * 0.45,
          width: 1,
        });
      }

      // Containers only ever carry a *rolled-up* status from their children, so
      // they must not glow like a real change — otherwise one changed method
      // lights up its class, file, directory, and the repo root all at once.
      const isContainer = node.kind === 'root' || node.kind === 'dir';
      const glow = isContainer
        ? 0.08
        : changed
          ? 0.34 + hot * 0.95
          : node.kind === 'file'
            ? 0.1
            : 0.04;
      glowNode(ctx, node.kind, entry.x, entry.y, radius, color, {
        alpha,
        glow: dimmed ? 0 : glow,
      });

      // A file that's expanded gets a faint containing ring, so you can tell
      // "this file has its guts showing" from "this is a lone file node".
      if (node.kind === 'file' && layout.expanded.has(node.id)) {
        ringShape(ctx, node.kind, entry.x, entry.y, radius + 2.5, color, {
          alpha: alpha * 0.35,
          width: 1,
        });
      }

      if (node.risk === 'high' && !dimmed) {
        ringShape(ctx, node.kind, entry.x, entry.y, radius + 5.5, COLORS.removed, {
          alpha: 0.5,
          width: 1.2,
        });
      }

      drawPulses(ctx, fx, entry.x, entry.y, radius, now);

      scene.hitboxes.push({ id: node.id, x: entry.x, y: entry.y, r: radius });

      // Queue the label instead of drawing it now, so collisions can be
      // resolved globally by priority once every node is placed.
      const pinned =
        store.hover === node.id || store.selected === node.id || store.searchHits.has(node.id);
      const labelWorth =
        pinned ||
        changed ||
        node.kind === 'root' ||
        node.kind === 'dir' ||
        (showAllLabels && node.kind === 'file');

      if (labelWorth && !dimmed) {
        const size = node.kind === 'root' ? 12 : node.kind === 'dir' ? 10.5 : 10;
        const priority =
          (pinned ? 1000 : 0) +
          (changed ? 500 : 0) +
          (node.kind === 'root' ? 400 : node.kind === 'dir' ? 300 : 0) +
          Math.min(99, node.size);
        this._labelQueue.push({
          x: entry.x,
          y: entry.y - radius - 9,
          text: node.name,
          color: changed ? color : COLORS.dim,
          alpha: clamp(0.92, 0, 1),
          size,
          priority,
          force: pinned,
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
