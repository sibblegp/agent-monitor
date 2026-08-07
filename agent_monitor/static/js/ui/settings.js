/**
 * Settings panel: API key entry, model choice, and the cost meter.
 *
 * The key never travels back from the server — the panel only ever displays a
 * masked hint, so a compromised page can't read it out of the DOM.
 */

import { api } from '../platform.js';

const el = {
  dialog: document.getElementById('settings-dialog'),
  key: document.getElementById('api-key'),
  keyStatus: document.getElementById('key-status'),
  remember: document.getElementById('remember-key'),
  model: document.getElementById('model'),
  cost: document.getElementById('cost-meter'),
  toggle: document.getElementById('ai-toggle'),
  label: document.getElementById('ai-label'),
  diag: document.getElementById('diag'),
  save: document.getElementById('settings-save'),
  cancel: document.getElementById('settings-cancel'),
  dirty: document.getElementById('settings-dirty'),
};

let state = null;
let onChange = null;

function fmtUsd(n) {
  if (!n) return '$0.00';
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

/**
 * Reset every field to what the server currently holds.
 *
 * Kept separate from `renderSettings` because status pushes arrive on the
 * websocket at any moment: repopulating the form on one of those would wipe a
 * half-typed key out from under the user.
 */
function loadForm() {
  if (!state) return;
  el.model.value = state.model || 'claude-sonnet-4-6';
  el.remember.checked = !!state.remember_key;
  el.key.value = '';
  el.key.placeholder = state.has_key ? state.key_hint : 'sk-ant-…';
  el.keyStatus.textContent = state.has_key
    ? state.key_source === 'env'
      ? 'Using ANTHROPIC_API_KEY from the environment.'
      : state.key_source === 'stored'
        ? 'Using the key saved on this machine.'
        : 'Using the key entered this session.'
    : 'No key set. AI insights stay off until one is provided.';
  markClean();
}

/** What Save would send: only the fields that actually differ. */
function pendingPatch() {
  if (!state) return {};
  const patch = {};
  const key = el.key.value.trim();
  if (key) {
    patch.api_key = key;
    patch.remember = el.remember.checked;
  } else if (el.remember.checked !== !!state.remember_key) {
    // Un-ticking "remember" with no new key means: forget the stored one.
    patch.api_key = '';
    patch.remember = el.remember.checked;
  }
  if (el.model.value !== (state.model || 'claude-sonnet-4-6')) patch.model = el.model.value;
  return patch;
}

function markDirty() {
  el.dirty.hidden = Object.keys(pendingPatch()).length === 0;
}

function markClean() {
  el.dirty.hidden = true;
}

export function renderSettings(settings, aiStatus) {
  state = settings;
  // Only adopt server values into the form when nothing is being edited.
  if (settings && el.dirty.hidden) loadForm();

  const usage = aiStatus?.usage;
  if (usage?.requests) {
    el.cost.textContent =
      `${usage.requests} request${usage.requests === 1 ? '' : 's'} · ` +
      `${usage.input_tokens.toLocaleString()} in / ${usage.output_tokens.toLocaleString()} out · ` +
      fmtUsd(usage.cost_usd);
  } else {
    el.cost.textContent = 'No requests yet this session.';
  }

  if (aiStatus?.error) {
    el.diag.innerHTML = `<h4>Last AI error</h4><p class="muted small"></p>`;
    el.diag.querySelector('p').textContent = aiStatus.error;
  } else {
    el.diag.textContent = '';
  }

  setToggleState(aiStatus);
}

function setToggleState(aiStatus) {
  const enabled = !!aiStatus?.enabled;
  const busy = !!aiStatus?.busy;
  el.toggle.classList.toggle('is-on', enabled);
  el.toggle.classList.toggle('is-busy', busy);
  el.label.textContent = busy
    ? 'AI insights — analysing…'
    : enabled
      ? 'AI insights — on'
      : 'AI insights — off';
  el.toggle.title = aiStatus?.disabled_reason
    ? `AI unavailable: ${aiStatus.disabled_reason}`
    : 'Optional AI insights (off by default)';
}

async function push(patch) {
  try {
    const result = await api.settings(patch);
    markClean(); // so renderSettings adopts the saved values into the form
    renderSettings(result, result.ai);
    onChange?.(result);
    return true;
  } catch (err) {
    el.keyStatus.textContent = err.message;
    return false;
  }
}

async function save() {
  const patch = pendingPatch();
  if (!Object.keys(patch).length) {
    el.dialog.hidden = true;
    return;
  }
  el.save.disabled = true;
  const ok = await push(patch);
  el.save.disabled = false;
  if (ok) el.dialog.hidden = true;
}

function cancel() {
  loadForm(); // discard staged edits so reopening starts from the saved state
  el.dialog.hidden = true;
}

export function initSettings(handler) {
  onChange = handler;

  el.toggle.addEventListener('click', async () => {
    const turningOn = !el.toggle.classList.contains('is-on');
    // Be explicit about what leaves the machine before the first call.
    if (turningOn && !state?.has_key) {
      el.dialog.hidden = false;
      el.key.focus();
      el.keyStatus.textContent = 'Add an API key to enable AI insights.';
      return;
    }
    // The top-bar toggle is a switch, not part of the form — it applies at once.
    push({ ai_enabled: turningOn });
  });

  // Nothing below applies until Save; they only stage.
  el.key.addEventListener('input', markDirty);
  el.model.addEventListener('change', markDirty);
  el.remember.addEventListener('change', markDirty);

  el.key.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      save();
    } else if (ev.key === 'Escape') {
      ev.preventDefault();
      cancel();
    }
  });

  el.save.addEventListener('click', save);
  el.cancel.addEventListener('click', cancel);

  // Closing by ✕ or by clicking the backdrop is a cancel, not a silent keep.
  el.dialog.querySelector('[data-close]')?.addEventListener('click', cancel);
  el.dialog.addEventListener('mousedown', (ev) => {
    if (ev.target === el.dialog) cancel();
  });
}

export function updateAiStatus(aiStatus) {
  setToggleState(aiStatus);
  renderSettings(state, aiStatus);
}
