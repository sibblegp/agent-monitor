/**
 * Streaming-text effect for the AI panes: types entries character by
 * character, like the product's narrative streams in.
 *
 * The full text lives in data-type-text; the container keeps an aria-label
 * with the complete content so screen readers never wait for the animation.
 */

const CHARS_PER_SEC = 44;

export function typeInto(container, { reduced = false } = {}) {
  const entries = [...container.querySelectorAll('[data-type-text]')];
  container.setAttribute(
    'aria-label',
    entries.map((el) => el.dataset.typeText).join(' ')
  );

  if (reduced) {
    for (const el of entries) {
      el.textContent = el.dataset.typeText;
      el.classList.add('typed');
    }
    return;
  }

  let index = 0;

  function typeNext() {
    if (index >= entries.length) return;
    const el = entries[index++];
    const text = el.dataset.typeText;
    el.classList.add('typing');
    const started = performance.now();

    function frame(now) {
      const count = Math.min(text.length, Math.floor(((now - started) / 1000) * CHARS_PER_SEC));
      el.textContent = text.slice(0, count);
      if (count < text.length) {
        requestAnimationFrame(frame);
      } else {
        el.classList.remove('typing');
        el.classList.add('typed');
        setTimeout(typeNext, 550);
      }
    }
    requestAnimationFrame(frame);
  }

  typeNext();
}
