/**
 * Site entry point: one shared rAF loop drives every canvas, each ticking
 * only while on screen; everything stops when the tab is hidden — the same
 * "no work when nothing is visible" ethic as the product.
 */

import { initReveal } from './reveal.js';
import { typeInto } from './typing.js';
import { HeroDemo } from './demo/hero.js';
import { FlowVignette, ClusterVignette } from './demo/vignette-flow.js';

const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

// ── shared animation scheduler ──────────────────────────────────────────

const animated = []; // {el, demo, visible}
let rafId = null;

function pump(now) {
  rafId = null;
  let any = false;
  for (const item of animated) {
    if (!item.visible) continue;
    item.demo.tick(now);
    any = true;
  }
  if (any) rafId = requestAnimationFrame(pump);
}

function wake() {
  if (rafId === null && !document.hidden && !reducedMotion.matches) {
    rafId = requestAnimationFrame(pump);
  }
}

function register(el, demo) {
  const item = { el, demo, visible: false };
  animated.push(item);
  new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        item.visible = entry.isIntersecting;
        if (item.visible) wake();
      }
    },
    { threshold: 0.05 }
  ).observe(el);
}

document.addEventListener('visibilitychange', wake);

// ── canvases ────────────────────────────────────────────────────────────

function initCanvases() {
  const heroCanvas = document.getElementById('hero-canvas');
  const caption = document.getElementById('hero-caption');
  const demos = [];

  if (heroCanvas) demos.push([heroCanvas, new HeroDemo(heroCanvas, caption)]);
  const flow = document.getElementById('flow-canvas');
  if (flow) demos.push([flow, new FlowVignette(flow)]);
  const cluster = document.getElementById('cluster-canvas');
  if (cluster) demos.push([cluster, new ClusterVignette(cluster)]);

  const applyMode = () => {
    if (reducedMotion.matches) {
      if (rafId !== null) cancelAnimationFrame(rafId);
      rafId = null;
      // One static, fully-colored frame each — the picture without the motion.
      requestAnimationFrame(() => demos.forEach(([, demo]) => demo.renderStatic()));
    } else {
      wake();
    }
  };

  demos.forEach(([el, demo]) => register(el, demo));
  reducedMotion.addEventListener('change', applyMode);
  applyMode();
}

// ── typing panes ────────────────────────────────────────────────────────

function initTyping() {
  const panes = document.querySelectorAll('[data-typing]');
  if (!('IntersectionObserver' in window)) {
    panes.forEach((pane) => typeInto(pane, { reduced: true }));
    return;
  }
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        observer.unobserve(entry.target);
        typeInto(entry.target, { reduced: reducedMotion.matches });
      }
    },
    { threshold: 0.35 }
  );
  panes.forEach((pane) => observer.observe(pane));
}

// ── chrome ──────────────────────────────────────────────────────────────

function initTopbar() {
  const bar = document.querySelector('.topbar');
  if (!bar) return;
  let ticking = false;
  const update = () => {
    bar.classList.toggle('scrolled', window.scrollY > 40);
    ticking = false;
  };
  window.addEventListener(
    'scroll',
    () => {
      if (!ticking) {
        ticking = true;
        requestAnimationFrame(update);
      }
    },
    { passive: true }
  );
  update();
}

function initCopyButtons() {
  document.querySelectorAll('[data-copy]').forEach((button) => {
    const original = button.textContent;
    button.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.copy);
        button.textContent = 'Copied';
        button.classList.add('copied');
        setTimeout(() => {
          button.textContent = original;
          button.classList.remove('copied');
        }, 1600);
      } catch {
        button.textContent = 'Press ⌘C';
        setTimeout(() => (button.textContent = original), 1600);
      }
    });
  });
}

// ── go ──────────────────────────────────────────────────────────────────

initReveal();
initCanvases();
initTyping();
initTopbar();
initCopyButtons();
