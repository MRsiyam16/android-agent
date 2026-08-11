// chat.js — extracted verbatim from the old dashboard.js IIFE.

import { ui } from './state.js';
import { renderChatMarkdown as renderMarkdown } from './markdown.js';
import { chatLog } from './phone.js';

function scrollChatToEnd(force) {
  const jump = document.getElementById('chatJump');
  // Only auto-scroll when the reader is already at the bottom, so scrolling back through a
  // run is not yanked away every time the agent speaks.
  if (force || ui.chatPinned) {
    chatLog.scrollTop = chatLog.scrollHeight;
    if (jump) jump.hidden = true;
  } else if (jump) {
    jump.hidden = false;
  }
}

// Deferred: this reads a binding imported from a module that imports this one back,
// so at evaluation time it may still be in its temporal dead zone. main.js calls it.
function initChatScroll() {
  chatLog.addEventListener('scroll', () => {
    const atBottom = chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight < 40;
    ui.chatPinned = atBottom;
    const jump = document.getElementById('chatJump');
    if (jump && atBottom) jump.hidden = true;
  });
}

// The agent writes markdown. Render the subset it uses — bold, italics, inline code and
// bullets — after escaping, so agent-authored text can never inject markup.
function appendChat(kind, text) {
  const row = document.createElement('div');
  row.className = 'chat-entry ' + kind;
  if (kind === 'agent') row.innerHTML = renderMarkdown(text);
  else row.textContent = text;
  chatLog.appendChild(row);
  scrollChatToEnd();
  return row;
}

// ---------------------------------------------------------------------------
// Agent tab
//
// The agent runs server-side and can work for many minutes, so POSTing a message
// returns immediately and everything it does arrives over the WebSocket. The chat is
// therefore rendered from events, and reloaded from the transcript on disk when you
// switch modules — the browser is a view, never the source of truth.
// ---------------------------------------------------------------------------
const agentEl = {
  project: document.getElementById('agentProject'),
  subproject: document.getElementById('agentSubproject'),
  state: document.getElementById('agentState'),
  stop: document.getElementById('agentStopBtn'),
  blocked: document.getElementById('agentBlocked'),
  blockedLabel: document.getElementById('agentBlockedLabel'),
  blockedQuestion: document.getElementById('agentBlockedQuestion'),
  blockedDone: document.getElementById('agentBlockedDone'),
  moduleList: document.getElementById('agentModuleList'),
  secretNames: document.getElementById('agentSecretNames'),
  meterStepper: document.getElementById('meterStepper'),
  liveToggle: document.getElementById('liveFrameToggle'),
  traceToggle: document.getElementById('traceToggle'),
  input: document.getElementById('chatInput'),
  send: document.getElementById('chatSend'),
  model: document.getElementById('agentModel'),
  working: document.getElementById('agentWorking'),
  workingText: document.getElementById('agentWorkingText'),
  workingTimer: document.getElementById('agentWorkingTimer'),
  jump: document.getElementById('chatJump'),
};

const agent = {
  ready: false,
  package: null,
  slug: null,
  // "android" | "ios" | "web" — which device-frame chrome and controls apply. Kept in sync
  // with the project list's own `platform` field (see modules.js's `projectPlatforms` map)
  // whenever the selected project changes.
  platform: 'android',
  busy: false,
  taps: 0,
  shots: 0,
  liveTimer: null,
  workTimer: null,
  workStarted: 0,
  loadedKey: null,   // "<package>/<slug>" currently rendered in the chat log
};

function setAgentState(label, cls) {
  agentEl.state.textContent = label;
  agentEl.state.className = 'agent-state' + (cls ? ' ' + cls : '');
}

function prettyModel(id) {
  if (!id) return null;
  // "claude-opus-5[1m]" → "Opus 5 · 1M"; "claude-sonnet-4-6" → "Sonnet 4.6".
  // The exact id always stays in the tooltip, so the short form can never mislead.
  const ctx = /\[(\d+m)\]/i.exec(id);
  const m = /claude-([a-z]+)-?(\d+)?-?(\d+)?/.exec(id);
  if (!m) return id;
  const name = m[1].charAt(0).toUpperCase() + m[1].slice(1);
  const version = [m[2], m[3]].filter(Boolean).join('.');
  return `${name}${version ? ' ' + version : ''}${ctx ? ' · ' + ctx[1].toUpperCase() : ''}`;
}

function setAgentModel(id, label, subscription) {
  const short = prettyModel(id);
  if (short) {
    agentEl.model.textContent = short;
    agentEl.model.title = [id && 'Model: ' + id, label, subscription && 'Auth: ' + subscription]
      .filter(Boolean).join('\n');
  }
  if (subscription) {
    // In the top bar because it is the difference between spending the subscription and
    // being billed per token — worth seeing without opening a rail.
    document.getElementById('statAuth').textContent = 'auth ' + subscription;
  }
}

// -- Model picker --------------------------------------------------------------------
// Populated from what the CLI reports it can run. Per module, because the model is fixed
// when a session connects and each module has its own session anyway — so recon can run
// somewhere cheap while the suite that files defects runs on something better.
async function loadModelPicker() {
  const picker = document.getElementById('agentModelPicker');
  if (!agent.package || !agent.slug) { picker.innerHTML = ''; return; }
  let data;
  try {
    data = await agentFetch(
      `/agent/${encodeURIComponent(agent.package)}/${agent.slug}/models`);
  } catch {
    return;   // the session may not be warm yet; the next selection will retry
  }
  picker.innerHTML = '';
  const auto = new Option('Default model', '');
  picker.appendChild(auto);
  (data.models || []).forEach((m) => {
    if (!m.value || m.value === 'default') return;
    picker.appendChild(new Option(m.label || m.value, m.value));
  });
  picker.value = data.requested || '';
  picker.title = data.current ? 'Currently running ' + data.current : 'Model for this module';
}

document.getElementById('agentModelPicker').addEventListener('change', async (e) => {
  const picker = e.target;
  const wanted = picker.value || null;
  picker.disabled = true;
  try {
    const data = await agentFetch(
      `/agent/${encodeURIComponent(agent.package)}/${agent.slug}/model`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: wanted }) });
    setAgentModel(data.model, data.model_label);
    if (!data.unchanged) {
      appendChat('notice', `Switched to ${prettyModel(data.model) || 'the default model'}. `
        + 'The conversation was resumed, so I still know where we were.');
    }
  } catch (err) {
    appendChat('error', err.message);
    await loadModelPicker();   // put the control back to what is actually in force
  } finally {
    picker.disabled = false;
  }
});

function tickWorkTimer() {
  const secs = Math.floor((Date.now() - agent.workStarted) / 1000);
  agentEl.workingTimer.textContent =
    `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`;
}

function startWorking(text) {
  agentEl.workingText.textContent = text || 'Thinking…';
  agentEl.working.classList.add('open');
  if (!agent.workTimer) {
    agent.workStarted = Date.now();
    tickWorkTimer();
    agent.workTimer = setInterval(tickWorkTimer, 1000);
  }
}

function stopWorking() {
  agentEl.working.classList.remove('open');
  if (agent.workTimer) { clearInterval(agent.workTimer); agent.workTimer = null; }
}

// Sending mid-run would just be rejected, so don't offer it — unless the agent is parked on
// a question, which is exactly when a reply is needed.
//
// Its own function because both inputs move independently. This used to live inside
// setAgentBusy, which only runs on a busy event — so when the agent went on to *block*, the
// composer was still disabled from the last `setAgentBusy(true)` and nothing recomputed it.
// The box said "the agent needs an answer" above a Send button that would not send, and the
// only way to answer was to press Stop first and end the run you were trying to help.
function syncComposerEnabled() {
  const blocked = agentEl.blocked.classList.contains('open');
  const locked = agent.busy && !blocked;
  agentEl.send.disabled = locked;
  agentEl.input.placeholder = locked
    ? 'The agent is working — press Stop to redirect it'
    : blocked
      ? 'Answer the agent, or press ✓ if you have done what it asked'
      : 'Tell the agent what to test — e.g. “test the login module: empty submit, wrong password, valid login, session persistence”';
}

function setAgentBusy(busy) {
  agent.busy = busy;
  agentEl.stop.disabled = !busy;
  const blocked = agentEl.blocked.classList.contains('open');
  syncComposerEnabled();
  if (busy) { setAgentState('working', 'busy'); startWorking(agentEl.workingText.textContent); }
  else {
    stopWorking();
    if (!blocked) setAgentState('idle', '');
  }
}

async function agentFetch(url, options) {
  const resp = await fetch(url, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error((data && data.detail) || (resp.status + ' from ' + url));
  return data;
}

export { initChatScroll, agent, agentEl, agentFetch, appendChat, loadModelPicker, prettyModel, scrollChatToEnd, setAgentBusy, setAgentModel, setAgentState, startWorking, stopWorking, syncComposerEnabled, tickWorkTimer };
