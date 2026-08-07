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
};

let state = null;
let onChange = null;

function fmtUsd(n) {
  if (!n) return '$0.00';
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

export function renderSettings(settings, aiStatus) {
  state = settings;
  if (settings) {
    el.model.value = settings.model || 'claude-sonnet-4-6';
    el.remember.checked = !!settings.remember_key;
    el.key.placeholder = settings.has_key ? settings.key_hint : 'sk-ant-…';
    el.keyStatus.textContent = settings.has_key
      ? settings.key_source === 'env'
        ? 'Using ANTHROPIC_API_KEY from the environment.'
        : settings.key_source === 'stored'
          ? 'Using the key saved on this machine.'
          : 'Using the key entered this session.'
      : 'No key set. AI insights stay off until one is provided.';
  }

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
    renderSettings(result, result.ai);
    onChange?.(result);
  } catch (err) {
    el.keyStatus.textContent = err.message;
  }
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
    if (turningOn && !window.__agentMonitorAiConsent) {
      const ok = confirm(
        'Enable AI insights?\n\n' +
          'The diffs of changed symbols in the current scope will be sent to the ' +
          'Anthropic API to generate summaries, risk flags, themes, and a review note.\n\n' +
          'Nothing else is sent, and the visualization works fully without this.'
      );
      if (!ok) return;
      window.__agentMonitorAiConsent = true;
    }
    push({ ai_enabled: turningOn });
  });

  el.key.addEventListener('change', () => {
    const value = el.key.value.trim();
    if (!value) return;
    el.key.value = '';
    push({ api_key: value, remember: el.remember.checked });
  });

  el.model.addEventListener('change', () => push({ model: el.model.value }));
  el.remember.addEventListener('change', () => {
    // Only meaningful alongside a key; re-sending an empty key would clear it.
    if (!el.remember.checked) push({ api_key: '', remember: false });
  });
}

export function updateAiStatus(aiStatus) {
  setToggleState(aiStatus);
  renderSettings(state, aiStatus);
}
