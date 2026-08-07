/**
 * Drawing vocabulary ported from the app (agent_monitor/static/js/render/effects.js)
 * so the hero demo looks like the real product, not an artist's impression.
 */

export const COLORS = {
  added: '#3ddc84',
  modified: '#f5a623',
  signature_changed: '#ff5fa2',
  removed: '#ff4d5e',
  unchanged: '#3a4354',
  external: '#a06bff',
  entry: '#38bdf8',
  dir: '#46536b',
  file: '#4d5a6e',
  root: '#7f8ea3',
  text: '#e6edf3',
  dim: '#8b95a3',
  faint: '#5b6673',
  bg: '#0a0c10',
  edge: '#2a3442',
};

export const ease = {
  outCubic: (t) => 1 - Math.pow(1 - t, 3),
  inOutSine: (t) => -(Math.cos(Math.PI * t) - 1) / 2,
};

export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

export function rgba(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

/** Deterministic PRNG so the demo plays the same on every load. */
export function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Slow breathing used on modified nodes. */
export function breathe(now, hz = 1.2, depth = 0.07) {
  return 1 - depth + depth * (0.5 + 0.5 * Math.sin((now / 1000) * Math.PI * 2 * hz));
}

/** Three quick blinks when a node first appears. */
export function blinkAlpha(born, now, blinks = 3) {
  const elapsed = now - born;
  const period = 190;
  if (elapsed > period * blinks * 2) return 1;
  return Math.floor(elapsed / period) % 2 === 0 ? 1 : 0.32;
}

/**
 * Per-kind silhouettes:
 *   root / dir  rounded square    class     hexagon
 *   file        folded document   method    diamond
 *   function    circle            external  circle, drawn hollow
 */
export function nodePath(ctx, kind, x, y, r) {
  ctx.beginPath();
  switch (kind) {
    case 'root':
    case 'dir': {
      const s = r * 1.72;
      ctx.roundRect(x - s / 2, y - s / 2, s, s, Math.max(2, r * 0.3));
      break;
    }
    case 'file': {
      const w = r * 1.55;
      const h = r * 1.95;
      const fold = Math.max(2, r * 0.5);
      const l = x - w / 2;
      const t = y - h / 2;
      ctx.moveTo(l, t);
      ctx.lineTo(l + w - fold, t);
      ctx.lineTo(l + w, t + fold);
      ctx.lineTo(l + w, t + h);
      ctx.lineTo(l, t + h);
      ctx.closePath();
      break;
    }
    case 'class': {
      const rr = r * 1.18;
      for (let i = 0; i < 6; i++) {
        const a = (Math.PI / 3) * i - Math.PI / 6;
        const px = x + Math.cos(a) * rr;
        const py = y + Math.sin(a) * rr;
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      }
      ctx.closePath();
      break;
    }
    case 'method': {
      const d = r * 1.38;
      ctx.moveTo(x, y - d);
      ctx.lineTo(x + d, y);
      ctx.lineTo(x, y + d);
      ctx.lineTo(x - d, y);
      ctx.closePath();
      break;
    }
    default:
      ctx.arc(x, y, r, 0, Math.PI * 2);
  }
}

/** Filled node in its kind's shape, with an optional additive glow. */
export function glowNode(ctx, kind, x, y, radius, color, { alpha = 1, glow = 1, hollow = false } = {}) {
  if (glow > 0.01) {
    const reach = Math.min(radius * (2.2 + glow * 1.6), 46);
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    const grad = ctx.createRadialGradient(x, y, 0, x, y, reach);
    grad.addColorStop(0, rgba(color, 0.34 * glow * alpha));
    grad.addColorStop(0.45, rgba(color, 0.09 * glow * alpha));
    grad.addColorStop(1, rgba(color, 0));
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(x, y, reach, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  nodePath(ctx, kind, x, y, radius);
  if (hollow) {
    ctx.strokeStyle = rgba(color, alpha);
    ctx.lineWidth = 1.4;
    ctx.stroke();
  } else {
    ctx.fillStyle = rgba(color, alpha);
    ctx.fill();
  }
}

/**
 * Expanding pulse rings; `signature_changed` gets a double ring so an API
 * break reads differently from a body edit. Mutates `pulses` in place.
 */
export function drawPulses(ctx, pulses, now) {
  if (!pulses.length) return;
  const DURATION = 950;
  ctx.save();
  ctx.globalCompositeOperation = 'lighter';
  for (let i = pulses.length - 1; i >= 0; i--) {
    const pulse = pulses[i];
    const t = (now - pulse.start) / DURATION;
    if (t >= 1) {
      pulses.splice(i, 1);
      continue;
    }
    const color = COLORS[pulse.kind] || COLORS.modified;
    const rings = pulse.kind === 'signature_changed' ? 2 : 1;
    for (let r = 0; r < rings; r++) {
      const shifted = clamp(t - r * 0.18, 0, 1);
      if (shifted <= 0) continue;
      const eased = ease.outCubic(shifted);
      ctx.beginPath();
      ctx.arc(pulse.x, pulse.y, pulse.radius + eased * 58, 0, Math.PI * 2);
      ctx.strokeStyle = rgba(color, (1 - shifted) * 0.75);
      ctx.lineWidth = 2.6 - shifted * 1.8;
      ctx.stroke();
    }
  }
  ctx.restore();
}

/** Crimson implosion for a node that's going away. t in [0,1]. */
export function drawImplosion(ctx, x, y, radius, t) {
  const eased = ease.outCubic(t);
  ctx.save();
  ctx.globalCompositeOperation = 'lighter';
  ctx.beginPath();
  ctx.arc(x, y, radius * (1 - eased) + 1, 0, Math.PI * 2);
  ctx.fillStyle = rgba(COLORS.removed, (1 - t) * 0.9);
  ctx.fill();
  ctx.beginPath();
  ctx.arc(x, y, radius + eased * 22, 0, Math.PI * 2);
  ctx.strokeStyle = rgba(COLORS.removed, (1 - t) * 0.45);
  ctx.lineWidth = 1.6;
  ctx.stroke();
  ctx.restore();
}

/** Point on a cubic bezier from a→b with horizontal control handles. */
export function bezierPoint(a, b, t) {
  const dx = (b.x - a.x) * 0.45;
  const x0 = a.x, y0 = a.y;
  const x1 = a.x + dx, y1 = a.y;
  const x2 = b.x - dx, y2 = b.y;
  const x3 = b.x, y3 = b.y;
  const u = 1 - t;
  return {
    x: u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
    y: u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3,
  };
}

export function strokeBezier(ctx, a, b, color, alpha, width = 1) {
  const dx = (b.x - a.x) * 0.45;
  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  ctx.bezierCurveTo(a.x + dx, a.y, b.x - dx, b.y, b.x, b.y);
  ctx.strokeStyle = rgba(color, alpha);
  ctx.lineWidth = width;
  ctx.stroke();
}

/** Particles travelling along a call edge. */
export function drawEdgeParticles(ctx, a, b, now, { color, seed = 0, density = 3, speed = 1.7 }) {
  const count = Math.max(1, Math.round(density));
  ctx.save();
  ctx.globalCompositeOperation = 'lighter';
  for (let i = 0; i < count; i++) {
    const phase = (((now * 0.00016 * speed + i / count + seed) % 1) + 1) % 1;
    const point = bezierPoint(a, b, phase);
    const fade = Math.sin(phase * Math.PI);
    ctx.beginPath();
    ctx.arc(point.x, point.y, 1.5 + fade * 0.9, 0, Math.PI * 2);
    ctx.fillStyle = rgba(color, 0.16 + fade * 0.55);
    ctx.fill();
  }
  ctx.restore();
}

/** Rounded label chip with a readable backing plate. */
export function drawLabel(ctx, x, y, text, { color = COLORS.text, alpha = 1, size = 10.5 } = {}) {
  if (alpha <= 0.02) return;
  ctx.font = `${size}px ui-monospace, "SF Mono", Menlo, Consolas, monospace`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  const w = ctx.measureText(text).width + 8;
  const h = size + 6;
  ctx.fillStyle = rgba('#0a0c10', 0.62 * alpha);
  ctx.beginPath();
  ctx.roundRect(x - w / 2, y - h / 2, w, h, 4);
  ctx.fill();
  ctx.fillStyle = rgba(color, alpha);
  ctx.fillText(text, x, y);
}
