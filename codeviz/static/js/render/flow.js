/**
 * Flow pane renderer: entry points on the left, calls fanning right into
 * external packages.
 *
 * Edges are cubic beziers so parallel calls stay distinguishable, and each one
 * carries drifting particles in the direction of the call — denser on edges
 * touching something that changed, which is what makes a live agent run read as
 * "activity flowing through here".
 */

import {
  COLORS,
  blinkAlpha,
  breathe,
  clamp,
  drawEdgeParticles,
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

function bezier(a, b) {
  const dx = Math.max(40, (b.x - a.x) * 0.5);
  return {
    seed: 0,
    c1x: a.x + dx,
    c1y: a.y,
    c2x: b.x - dx,
    c2y: b.y,
    at(t) {
      const u = 1 - t;
      return {
        x: u * u * u * a.x + 3 * u * u * t * this.c1x + 3 * u * t * t * this.c2x + t * t * t * b.x,
        y: u * u * u * a.y + 3 * u * u * t * this.c1y + 3 * u * t * t * this.c2y + t * t * t * b.y,
      };
    },
  };
}

export class FlowRenderer {
  constructor(scene, layout, store) {
    this.scene = scene;
    this.layout = layout;
    this.store = store;
  }

  draw(now) {
    const { scene, layout, store } = this;
    const ctx = scene.begin();
    const highlight = store.highlightSet(store.selected || store.hover);
    const view = scene.viewBounds(240);

    this._drawEdges(ctx, now, view, highlight);
    this._drawNodes(ctx, now, view, highlight);
    this._drawLayerCaptions(ctx);

    scene.end();
  }

  _drawEdges(ctx, now, view, highlight) {
    const { layout, store } = this;

    for (const edge of layout.edges) {
      const a = layout.get(edge.src);
      const b = layout.get(edge.dst);
      if (!a || !b) continue;
      if (Math.max(a.x, b.x) < view.minX || Math.min(a.x, b.x) > view.maxX) continue;

      const srcNode = store.nodes.get(edge.src);
      const dstNode = store.nodes.get(edge.dst);
      const touched = isChanged(srcNode) || isChanged(dstNode);
      const dimmed = highlight && !(highlight.has(edge.src) && highlight.has(edge.dst));
      // Particles ride only edges attached to a change from the *latest*
      // update. Drifting dots on every edge is ambient noise that competes
      // with the one thing you're meant to look at.
      const live = store.isRecent(edge.src, now) || store.isRecent(edge.dst, now);

      const color = touched
        ? COLORS[isChanged(srcNode) ? srcNode.status : dstNode.status] || COLORS.modified
        : '#2b3644';
      const alpha = dimmed ? 0.06 : touched ? 0.5 : 0.2;

      const path = bezier(a, b);
      path.seed = hashUnit(edge.id);

      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.bezierCurveTo(path.c1x, path.c1y, path.c2x, path.c2y, b.x, b.y);
      ctx.strokeStyle = rgba(color, alpha);
      ctx.lineWidth = clamp(0.6 + Math.log2(1 + (edge.count || 1)) * 0.55, 0.6, 3.2);
      ctx.stroke();

      if (live && !dimmed && this.scene.camera.scale > 0.28) {
        drawEdgeParticles(ctx, path, now, { color, density: 3, speed: 1.7 });
      }
    }
  }

  _drawNodes(ctx, now, view, highlight) {
    const { layout, store, scene } = this;
    const showLabels = scene.camera.scale > 0.45;

    for (const entry of layout.points()) {
      const node = entry.node;
      if (!node) continue;
      if (entry.x < view.minX || entry.x > view.maxX) continue;

      const fx = store.fxOf(node.id);
      const changed = isChanged(node);
      const recent = store.isRecent(node.id, now);
      const hot = recent ? heat(fx, now, HOT_MS) : 0;
      const dimmed = highlight && !highlight.has(node.id);

      let alpha = dimmed ? 0.14 : 1;
      let radius = entry.r;
      const color = changed
        ? COLORS[node.status] || COLORS.modified
        : node.kind === 'external'
          ? COLORS.external
          : node.is_entry
            ? COLORS.entry
            : '#4d5a6e';

      if (node.status === 'removed') alpha *= 0.5;
      else if (recent && node.status === 'modified') radius *= breathe(now, 1.2, 0.07);
      else if (recent && node.status === 'added') alpha *= blinkAlpha(fx, now);

      // External packages read as hollow rings — they're not your code.
      if (node.kind === 'external') {
        ringDot(ctx, entry.x, entry.y, radius, COLORS.external, { alpha: alpha * 0.85, width: 1.3 });
      } else {
        const glow = changed ? 0.6 + hot * 0.9 : node.is_entry ? 0.35 : 0.08;
        glowDot(ctx, entry.x, entry.y, radius, color, { alpha, glow: dimmed ? 0 : glow });
      }

      if (node.is_entry && node.kind !== 'external') {
        ringDot(ctx, entry.x, entry.y, radius + 4, COLORS.entry, {
          alpha: alpha * 0.5,
          width: 1,
        });
      }

      if (node.risk === 'high' && !dimmed) {
        ringDot(ctx, entry.x, entry.y, radius + 6, COLORS.removed, { alpha: 0.45, width: 1.1 });
      }

      drawPulses(ctx, fx, entry.x, entry.y, radius, now);
      scene.hitboxes.push({ id: node.id, x: entry.x, y: entry.y, r: radius });

      const worth = changed || store.hover === node.id || store.selected === node.id || node.is_entry;
      if ((showLabels || worth) && !dimmed) {
        drawLabel(ctx, entry.x, entry.y - radius - 8, node.name, {
          color: changed ? color : COLORS.dim,
          alpha: worth ? 0.95 : 0.6,
          size: 9.5,
        });
      }
    }
  }

  _drawLayerCaptions(ctx) {
    const layers = this.layout.layers;
    if (!layers?.length) return;
    ctx.save();
    ctx.setTransform(this.scene.dpr, 0, 0, this.scene.dpr, 0, 0);

    const caption = (index) => {
      if (index === 0) return 'entry';
      if (index === layers.length - 1 && layers.length > 1) return 'external';
      return null;
    };

    for (let i = 0; i < layers.length; i++) {
      const text = caption(i);
      if (!text || !layers[i].length) continue;
      const first = this.layout.get(layers[i][0]);
      if (!first) continue;
      const screen = this.scene.camera.toScreen(first.x, 0);
      if (screen.x < 30 || screen.x > this.scene.width - 30) continue;
      drawLabel(ctx, screen.x, this.scene.height - 14, text, {
        color: COLORS.faint,
        alpha: 0.8,
        size: 9.5,
        bg: false,
      });
    }
    ctx.restore();
  }
}
