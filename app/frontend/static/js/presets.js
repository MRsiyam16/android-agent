// presets.js — the prewritten prompts offered on an empty module.
//
// Most of the time the same instruction is typed into module after module. Retyping it is
// not just tedious: the paraphrase drifts, so two modules that were meant to be tested the
// same way end up drawn differently on the board, and the difference reads as a finding.
//
// The wording lives server-side in agent/prompts.py, next to the tool rules it depends on
// (`case`, add_note's gutter placement, record_finding's `step` outline) — a preset that
// drifts from the prompt is a preset asking for a behaviour that no longer exists. This
// module only renders it.
//
// Shown on an empty transcript only, because that is the one moment where the answer to
// "what do I type" is the same every time. Mid-conversation the useful next message is
// never a template.
//
// Rendered *into the chat log*, under the greeting, rather than as a strip above the
// composer. That was the first design and it was wrong: as a sibling of the log it is a flex
// item competing for the panel's height, and five presets took 314px, collapsed the log to
// 120px and pushed the composer flush against the bottom of the viewport. Inside the log it
// costs no layout at all, and it scrolls away with the greeting it belongs to.

import { agent } from './chat.js';
import { sendToAgent } from './compose.js';
import { chatLog } from './phone.js';

// Built and removed rather than shown and hidden: `loadTranscript` clears the log with
// `innerHTML = ''` on every module switch, so an element declared in the HTML would be
// destroyed on the first switch and every later show would write into a detached node.
const PRESETS_ID = 'chatPresets';

// Fetched once per page load and cached. The list is static config, and re-fetching it on
// every module switch would put a request in front of a strip that is meant to be there
// before the reader has finished reading the greeting.
let cached = null;
let inFlight = null;

async function fetchPresets() {
  if (cached) return cached;
  // Share one request between concurrent callers. Switching modules quickly fires
  // loadTranscript several times, and without this each one starts its own fetch.
  if (!inFlight) {
    inFlight = fetch('/agent/prompt-presets')
      .then((r) => (r.ok ? r.json() : { presets: [] }))
      .then((data) => {
        cached = Array.isArray(data.presets) ? data.presets : [];
        return cached;
      })
      .catch(() => {
        // A failure here costs a convenience, not a capability — the composer still works.
        // Cached as empty so a dead endpoint is not re-requested on every module switch.
        cached = [];
        return cached;
      })
      .finally(() => { inFlight = null; });
  }
  return inFlight;
}

function build(presets) {
  const box = document.createElement('div');
  box.id = PRESETS_ID;
  box.className = 'chat-presets';

  const head = document.createElement('div');
  head.className = 'chat-presets-head';
  head.textContent = 'Or start from one of these';
  box.appendChild(head);

  presets.forEach((preset) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chat-preset';
    // The full text as the tooltip: these are long, and clicking one sends it — so there has
    // to be some way to read what you are about to send before you send it.
    btn.title = preset.text || '';

    const label = document.createElement('span');
    label.className = 'chat-preset-label';
    label.textContent = preset.label || preset.id || 'Prompt';
    btn.appendChild(label);

    if (preset.blurb) {
      const blurb = document.createElement('span');
      blurb.className = 'chat-preset-blurb';
      blurb.textContent = preset.blurb;
      btn.appendChild(blurb);
    }

    btn.addEventListener('click', () => {
      const text = String(preset.text || '').trim();
      if (!text) return;
      // Removed before the send, not after: sendToAgent awaits an upload and a POST, and a
      // strip that stays live through that can be clicked a second time — which would send
      // the same multi-minute instruction twice.
      hidePresets();
      sendToAgent(text);
    });
    box.appendChild(btn);
  });
  return box;
}

/** Offer the presets for the module currently on screen, if it has nothing in it yet. */
async function showPresets() {
  // Captured before the await and re-checked after. A module switch mid-fetch would
  // otherwise drop the strip onto whichever module won, empty transcript or not.
  const key = agent.loadedKey;
  const presets = await fetchPresets();
  if (key !== agent.loadedKey || !presets.length) return;
  hidePresets();   // never two strips, however many loads raced to get here
  chatLog.appendChild(build(presets));
}

function hidePresets() {
  const existing = document.getElementById(PRESETS_ID);
  if (existing) existing.remove();
}

export { hidePresets, showPresets };
