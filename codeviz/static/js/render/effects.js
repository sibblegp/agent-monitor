/**
 * Shared visual language: colors, easing, glow, pulses, particles.
 *
 * Everything is drawn on a near-black field with an additive pass for glow, so
 * changed nodes read as light sources rather than filled circles.
 */

export const COLORS = {
  added: '#3ddc84',
  modified: '#f5a623',
  signature_changed: '#ff5fa2',
  removed: '#ff4d5e',
  renamed: '#7ee6ff',
  moved: '#3a4354',
  unchanged: '#3a4354',
  external: '#a06bff',
  entry: '#38bdf8',
  dir: '#46536b',
  text: '#e6edf3',
  dim: '#8b95a3',
  faint: '#5b6673',
  bg: '#0a0c10',
};

export const LABELS = {
  added: 'added',
  modified: 'modified',
  signature_changed: 'signature changed',
  removed: 'removed',
  renamed: 'renamed',
  unchanged: 'unchanged',
  external: 'external',
};

/** Color for a node, accounting for kind and change status. */
export function nodeColor(node) {
  if (node.status && node.status !== 'unchanged' && COLORS[node.status]) {
    return COLORS[node.status];
  }
  if (node.kind === 'external') return COLORS.external;
  if (node.is_entry) return COLORS.entry;
  if (node.kind === 'dir' || node.kind === 'root') return COLORS.dir;
  return COLORS.unchanged;
}

export function isChanged(node) {
  return node.status && node.status !== 'unchanged' && node.status !== 'moved';
}

// ── easing ────────────────────────────────────────────────────────────

export const ease = {
  outCubic: (t) => 1 - Math.pow(1 - t, 3),
  outQuint: (t) => 1 - Math.pow(1 - t, 5),
  inOutSine: (t) => -(Math.cos(Math.PI * t) - 1) / 2,
  outBack: (t) => {
    const c1 = 1.70158;
    const c3 = c1 + 1;
    return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
  },
};

export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
export const lerp = (a, b, t) => a + (b - a) * t;

/** Convert #rrggbb + alpha to an rgba() string. */
export function rgba(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return `rgba(${r},${g},${b},${alpha})`;
}

// ── time-based intensity ──────────────────────────────────────────────

/**
 * 1 → 0 over HOT_MS after a node was touched. Drives the fading "recently
 * changed" glow that distinguishes *just now* from *earlier this session*.
 */
export function heat(fx, now, hotMs) {
  if (!fx.hotUntil || now > fx.hotUntil) return 0;
  return clamp((fx.hotUntil - now) / hotMs, 0, 1);
}

/** Slow breathing used on modified nodes. */
export function breathe(now, hz = 1.2, depth = 0.28) {
  return 1 - depth + depth * (0.5 + 0.5 * Math.sin((now / 1000) * Math.PI * 2 * hz));
}

/** Three quick blinks when a node first appears. */
export function blinkAlpha(fx, now) {
  if (!fx.blinks) return 1;
  const elapsed = now - fx.born;
  const period = 190;
  if (elapsed > period * fx.blinks * 2) {
    fx.blinks = 0;
    return 1;
  }
  return Math.floor(elapsed / period) % 2 === 0 ? 1 : 0.32;
}

// ── drawing primitives ────────────────────────────────────────────────

export function glowDot(ctx, x, y, radius, color, { alpha = 1, glow = 1 } = {}) {
  if (glow > 0) {
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    const grad = ctx.createRadialGradient(x, y, 0, x, y, radius * (3.2 + glow * 2.4));
    grad.addColorStop(0, rgba(color, 0.42 * glow * alpha));
    grad.addColorStop(0.45, rgba(color, 0.12 * glow * alpha));
    grad.addColorStop(1, rgba(color, 0));
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(x, y, radius * (3.2 + glow * 2.4), 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fillStyle = rgba(color, alpha);
  ctx.fill();
}

export function ringDot(ctx, x, y, radius, color, { alpha = 1, width = 1.4 } = {}) {
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.strokeStyle = rgba(color, alpha);
  ctx.lineWidth = width;
  ctx.stroke();
}

/**
 * Expanding pulse rings. Mutates `fx.pulses`, dropping finished ones.
 * `signature_changed` gets a double ring so an API break reads differently
 * from an ordinary body edit at a glance.
 */
export function drawPulses(ctx, fx, x, y, radius, now) {
  if (!fx.pulses.length) return;
  const DURATION = 620;
  ctx.save();
  ctx.globalCompositeOperation = 'lighter';
  for (let i = fx.pulses.length - 1; i >= 0; i--) {
    const pulse = fx.pulses[i];
    const t = (now - pulse.start) / DURATION;
    if (t >= 1) {
      fx.pulses.splice(i, 1);
      continue;
    }
    const color = COLORS[pulse.kind] || COLORS.modified;
    const rings = pulse.kind === 'signature_changed' ? 2 : 1;
    for (let r = 0; r < rings; r++) {
      const shifted = clamp(t - r * 0.18, 0, 1);
      if (shifted <= 0) continue;
      const eased = ease.outCubic(shifted);
      ctx.beginPath();
      ctx.arc(x, y, radius + eased * 34, 0, Math.PI * 2);
      ctx.strokeStyle = rgba(color, (1 - shifted) * 0.5);
      ctx.lineWidth = 2 - shifted * 1.4;
      ctx.stroke();
    }
  }
  ctx.restore();
}

/** Crimson implosion for a node that's going away. */
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

/** Particles travelling along a call edge, denser when the edge is hot. */
export function drawEdgeParticles(ctx, path, now, { color, density = 1, speed = 1 }) {
  const count = Math.max(1, Math.round(density * 2));
  ctx.save();
  ctx.globalCompositeOperation = 'lighter';
  for (let i = 0; i < count; i++) {
    const phase = ((now * 0.00016 * speed + i / count + path.seed) % 1 + 1) % 1;
    const point = path.at(phase);
    if (!point) continue;
    const fade = Math.sin(phase * Math.PI);
    ctx.beginPath();
    ctx.arc(point.x, point.y, 1.5 + fade * 0.9, 0, Math.PI * 2);
    ctx.fillStyle = rgba(color, 0.16 + fade * 0.55);
    ctx.fill();
  }
  ctx.restore();
}

/** Deterministic pseudo-random in [0,1) from a string — stable across frames. */
export function hashUnit(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 100000) / 100000;
}

/** Rounded label chip with a readable backing plate. */
export function drawLabel(ctx, x, y, text, { color = COLORS.text, alpha = 1, size = 10.5, bg = true } = {}) {
  if (alpha <= 0.02) return;
  ctx.font = `${size}px ui-monospace, "SF Mono", Menlo, Consolas, monospace`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  if (bg) {
    const w = ctx.measureText(text).width + 8;
    const h = size + 6;
    ctx.fillStyle = rgba('#0a0c10', 0.62 * alpha);
    ctx.beginPath();
    ctx.roundRect(x - w / 2, y - h / 2, w, h, 4);
    ctx.fill();
  }
  ctx.fillStyle = rgba(color, alpha);
  ctx.fillText(text, x, y);
}
