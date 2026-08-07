/**
 * Scroll reveal: elements with [data-reveal] slide in once when first seen.
 * Reveal-once, native scrolling untouched — no scroll-jacking.
 */

export function initReveal({ onReveal } = {}) {
  const targets = document.querySelectorAll('[data-reveal]');
  if (!('IntersectionObserver' in window)) {
    targets.forEach((el) => el.classList.add('in-view'));
    return;
  }
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        entry.target.classList.add('in-view');
        observer.unobserve(entry.target);
        if (onReveal) onReveal(entry.target);
      }
    },
    { threshold: 0.15, rootMargin: '0px 0px -8% 0px' }
  );
  targets.forEach((el) => observer.observe(el));
}
