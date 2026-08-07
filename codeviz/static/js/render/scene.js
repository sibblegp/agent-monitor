/**
 * Canvas plumbing shared by both panes: DPR-correct sizing, a pan/zoom camera,
 * and hit-testing against whatever the renderer last drew.
 */

import { clamp, lerp } from './effects.js';

export class Camera {
  constructor() {
    this.x = 0;
    this.y = 0;
    this.scale = 1;
    this.targetX = 0;
    this.targetY = 0;
    this.targetScale = 1;
  }

  /**
   * Ease toward the target each frame so fits and zooms glide.
   * Returns true while still moving. Snaps when close enough — an asymptotic
   * lerp never quite arrives, and the leftover sub-pixel drift reads as a
   * permanent shimmer.
   */
  step() {
    const k = 0.18;
    const dx = this.targetX - this.x;
    const dy = this.targetY - this.y;
    const ds = this.targetScale - this.scale;

    if (Math.abs(dx) < 0.05 && Math.abs(dy) < 0.05 && Math.abs(ds) < 0.0002) {
      this.x = this.targetX;
      this.y = this.targetY;
      this.scale = this.targetScale;
      return false;
    }

    this.x += dx * k;
    this.y += dy * k;
    this.scale += ds * k;
    return true;
  }

  apply(ctx) {
    ctx.translate(this.x, this.y);
    ctx.scale(this.scale, this.scale);
  }

  toWorld(px, py) {
    return { x: (px - this.x) / this.scale, y: (py - this.y) / this.scale };
  }

  toScreen(wx, wy) {
    return { x: wx * this.scale + this.x, y: wy * this.scale + this.y };
  }

  /** Zoom about a screen-space anchor so the point under the cursor stays put. */
  zoomAt(px, py, factor) {
    const next = clamp(this.targetScale * factor, 0.08, 6);
    const before = this.toWorld(px, py);
    this.targetScale = next;
    const afterX = before.x * next + this.targetX;
    const afterY = before.y * next + this.targetY;
    this.targetX += px - afterX;
    this.targetY += py - afterY;
  }

  panBy(dx, dy) {
    this.targetX += dx;
    this.targetY += dy;
    this.x += dx;
    this.y += dy;
  }

  /** Frame a world-space bounding box inside `width`×`height` pixels. */
  fitTo(box, width, height, padding = 70) {
    if (!box || !isFinite(box.minX) || box.maxX <= box.minX) return;
    const w = Math.max(1, box.maxX - box.minX);
    const h = Math.max(1, box.maxY - box.minY);
    const scale = clamp(
      Math.min((width - padding * 2) / w, (height - padding * 2) / h),
      0.08,
      2.2
    );
    this.targetScale = scale;
    this.targetX = width / 2 - ((box.minX + box.maxX) / 2) * scale;
    this.targetY = height / 2 - ((box.minY + box.maxY) / 2) * scale;
  }

  jumpToTarget() {
    this.x = this.targetX;
    this.y = this.targetY;
    this.scale = this.targetScale;
  }
}

export class Scene {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {{onPick:(id:string|null, ev:MouseEvent)=>void, onHover:(id:string|null, ev:MouseEvent)=>void}} handlers
   */
  constructor(canvas, handlers = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d', { alpha: false });
    this.camera = new Camera();
    this.width = 0;
    this.height = 0;
    this.dpr = 1;
    this.handlers = handlers;
    /** @type {Array<{id:string,x:number,y:number,r:number}>} filled by the renderer each frame */
    this.hitboxes = [];
    /** Timestamp of the last deliberate user input, so auto-focus can yield. */
    this.lastInteraction = 0;
    this._dragging = false;
    this._moved = 0;
    this._last = { x: 0, y: 0 };

    this.resize();
    this._bind();
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    this.dpr = Math.min(window.devicePixelRatio || 1, 2.5);
    this.width = Math.max(1, rect.width);
    this.height = Math.max(1, rect.height);
    this.canvas.width = Math.round(this.width * this.dpr);
    this.canvas.height = Math.round(this.height * this.dpr);
  }

  _touch() {
    this.lastInteraction = performance.now();
  }

  _bind() {
    const el = this.canvas;

    el.addEventListener('mousedown', (ev) => {
      this._dragging = true;
      this._moved = 0;
      this._last = { x: ev.clientX, y: ev.clientY };
      el.style.cursor = 'grabbing';
      this._touch();
    });

    window.addEventListener('mousemove', (ev) => {
      if (this._dragging) {
        const dx = ev.clientX - this._last.x;
        const dy = ev.clientY - this._last.y;
        this._moved += Math.abs(dx) + Math.abs(dy);
        this.camera.panBy(dx, dy);
        this._last = { x: ev.clientX, y: ev.clientY };
        this._touch();
        return;
      }
      if (ev.target !== el) return;
      const hit = this.pick(ev);
      el.style.cursor = hit ? 'pointer' : 'grab';
      if (hit) this._touch();
      this.handlers.onHover?.(hit, ev);
    });

    window.addEventListener('mouseup', () => {
      if (!this._dragging) return;
      this._dragging = false;
      el.style.cursor = 'grab';
    });

    el.addEventListener('click', (ev) => {
      this._touch();
      if (this._moved > 4) return; // that was a pan, not a click
      this.handlers.onPick?.(this.pick(ev), ev);
    });

    el.addEventListener('mouseleave', (ev) => this.handlers.onHover?.(null, ev));

    el.addEventListener(
      'wheel',
      (ev) => {
        ev.preventDefault();
        const rect = el.getBoundingClientRect();
        const factor = Math.pow(0.999, ev.deltaY);
        this.camera.zoomAt(ev.clientX - rect.left, ev.clientY - rect.top, factor);
        this._touch();
      },
      { passive: false }
    );

    el.addEventListener('dblclick', (ev) => {
      const hit = this.pick(ev);
      this.handlers.onDouble?.(hit, ev);
    });
  }

  /** Topmost hitbox under the pointer, or null. */
  pick(ev) {
    const rect = this.canvas.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const py = ev.clientY - rect.top;
    const world = this.camera.toWorld(px, py);
    let best = null;
    let bestDist = Infinity;
    for (let i = this.hitboxes.length - 1; i >= 0; i--) {
      const box = this.hitboxes[i];
      const dx = world.x - box.x;
      const dy = world.y - box.y;
      const dist = dx * dx + dy * dy;
      const reach = Math.max(box.r + 4 / this.camera.scale, 7 / this.camera.scale);
      if (dist <= reach * reach && dist < bestDist) {
        best = box.id;
        bestDist = dist;
      }
    }
    return best;
  }

  /** Clear and set up the transform for a frame. Returns the 2D context. */
  begin() {
    const ctx = this.ctx;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.fillStyle = '#0a0c10';
    ctx.fillRect(0, 0, this.width, this.height);
    ctx.save();
    this.camera.apply(ctx);
    this.hitboxes.length = 0;
    return ctx;
  }

  end() {
    this.ctx.restore();
  }

  /** Screen-space rect currently in view, for cheap culling. */
  viewBounds(margin = 120) {
    const a = this.camera.toWorld(-margin, -margin);
    const b = this.camera.toWorld(this.width + margin, this.height + margin);
    return { minX: a.x, minY: a.y, maxX: b.x, maxY: b.y };
  }
}

/** Bounding box helper over {x,y} entries. */
export function boundsOf(points) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of points) {
    if (!p || !isFinite(p.x) || !isFinite(p.y)) continue;
    if (p.x < minX) minX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.x > maxX) maxX = p.x;
    if (p.y > maxY) maxY = p.y;
  }
  if (!isFinite(minX)) return null;
  return { minX, minY, maxX, maxY };
}
