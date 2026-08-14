// conv.js — the manager's conversation.
//
// The cockpit's version of this is `transcript.js`, and it is three times longer because it
// renders screenshots, taps, flow-graph steps and filed findings. None of those can arrive
// here. The ecosystem manager's session has **no device server registered at all** (see
// ecosystem_tools.py) and no `record_finding`, so its whole event vocabulary is: it spoke, it
// called a tool, it is thinking, it is blocked, it finished. This file is smaller than the
// cockpit's because the tier is smaller, not because anything was left out.
//
// What it can still do is change the shape of the product — create_module and update_module
// reach into the apps — so structure events call back into the board.

import { renderChatMarkdown } from '../markdown.js';
import { api, eco } from './api.js';

const SLUG = 'main';   // the supervisor project's manager module

const el = {
  log: document.getElementById('chatLog'),
  jump: document.getElementById('chatJump'),
  input: document.getElementById('chatInput'),
  send: document.getElementById('chatSend'),
  form: document.getElementById('chatForm'),
  state: document.getElementById('agentState'),
  model: document.getElementById('agentModel'),
  picker: document.getElementById('agentModelPicker'),
  stop: document.getElementById('agentStopBtn'),
  working: document.getElementById('agentWorking'),
  workingText: document.getElementById('agentWorkingText'),
  workingTimer: document.getElementById('agentWorkingTimer'),
  blocked: document.getElementById('agentBlocked'),
  blockedLabel: document.getElementById('agentBlockedLabel'),
  blockedQuestion: document.getElementById('agentBlockedQuestion'),
  blockedDone: document.getElementById('agentBlockedDone'),
  trace: document.getElementById('traceToggle'),
};

const conv = { busy: false, pinned: true, workTimer: null, workStarted: 0 };

let onStructureChange = () => {};

function base() {
  return `/agent/${encodeURIComponent(eco.supervisor)}/${SLUG}`;
}

// -- log ------------------------------------------------------------------------------
function scrollToEnd(force) {
  if (force || conv.pinned) {
    el.log.scrollTop = el.log.scrollHeight;
    el.jump.hidden = true;
  } else {
    el.jump.hidden = false;
  }
}

function append(kind, text) {
  const row = document.createElement('div');
  row.className = 'chat-entry ' + kind;
  // The agent writes markdown; renderChatMarkdown escapes first and then renders the subset
  // it uses. Everything else is textContent — a tool summary can contain a bug title, which
  // came from an app under test and is not ours to trust as markup.
  if (kind === 'agent') row.innerHTML = renderChatMarkdown(text || '');
  else row.textContent = text || '';
  el.log.appendChild(row);
  scrollToEnd();
  return row;
}

// -- state ----------------------------------------------------------------------------
function setState(label, cls) {
  el.state.textContent = label;
  el.state.className = 'agent-state' + (cls ? ' ' + cls : '');
}

function tickTimer() {
  const secs = Math.floor((Date.now() - conv.workStarted) / 1000);
  el.workingTimer.textContent = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`;
}

function startWorking(text) {
  el.workingText.textContent = text || 'Thinking…';
  el.working.classList.add('open');
  if (!conv.workTimer) {
    conv.workStarted = Date.now();
    tickTimer();
    conv.workTimer = setInterval(tickTimer, 1000);
  }
}

function stopWorking() {
  el.working.classList.remove('open');
  if (conv.workTimer) { clearInterval(conv.workTimer); conv.workTimer = null; }
}

// Sending mid-run would be rejected, so it is not offered — unless the manager is parked on a
// question, which is exactly when a reply is needed. Recomputed on both busy and blocked
// changes rather than inside setBusy, which is the bug the cockpit had: going from busy to
// blocked left the composer disabled above a box asking for an answer.
function syncComposer() {
  const blocked = el.blocked.classList.contains('open');
  const locked = conv.busy && !blocked;
  el.send.disabled = locked;
  el.input.placeholder = locked
    ? 'The manager is working — press Stop to redirect it'
    : blocked
      ? 'Answer the manager, or press ✓ if you have done what it asked'
      : 'Ask about the product — e.g. “which defects are cross-app and still open?”';
}

function setBusy(busy) {
  conv.busy = busy;
  el.stop.disabled = !busy;
  syncComposer();
  if (busy) { setState('working', 'busy'); startWorking(el.workingText.textContent); }
  else {
    stopWorking();
    if (!el.blocked.classList.contains('open')) setState('idle', '');
  }
}

function showBlocked(question) {
  el.blocked.classList.add('open');
  el.blockedLabel.textContent = question.kind === 'approval'
    ? 'The manager is waiting for your approval'
    : 'The manager needs an answer';
  el.blockedQuestion.textContent = question.question || '';
  el.blockedDone.dataset.reply = question.kind === 'approval'
    ? 'Approved — go ahead.'
    : 'Done — carry on from here, and re-read the state rather than assuming it.';
  el.blockedDone.textContent = question.kind === 'approval' ? '✓ Approve' : '✓ Done';
  // A credential has a value only the user knows; a tick would answer it wrongly.
  el.blockedDone.classList.toggle('hidden', question.kind === 'credential');
  el.blockedDone.disabled = false;
  setState('blocked', 'blocked');
  syncComposer();
  el.input.focus();
}

function hideBlocked() {
  el.blocked.classList.remove('open');
  syncComposer();
}

// -- model ----------------------------------------------------------------------------
function prettyModel(id) {
  if (!id) return null;
  const ctx = /\[(\d+m)\]/i.exec(id);
  const m = /claude-([a-z]+)-?(\d+)?-?(\d+)?/.exec(id);
  if (!m) return id;
  const name = m[1].charAt(0).toUpperCase() + m[1].slice(1);
  const version = [m[2], m[3]].filter(Boolean).join('.');
  return `${name}${version ? ' ' + version : ''}${ctx ? ' · ' + ctx[1].toUpperCase() : ''}`;
}

function setModel(id, label, subscription) {
  const short = prettyModel(id);
  if (short) {
    el.model.textContent = short;
    el.model.title = [id && 'Model: ' + id, label, subscription && 'Auth: ' + subscription]
      .filter(Boolean).join('\n');
  }
}

async function loadModelPicker() {
  if (!eco.supervisor) return;
  let data;
  try { data = await api(`${base()}/models`); } catch { return; }
  el.picker.innerHTML = '';
  el.picker.appendChild(new Option('Default model', ''));
  (data.models || []).forEach((m) => {
    if (!m.value || m.value === 'default') return;
    el.picker.appendChild(new Option(m.label || m.value, m.value));
  });
  el.picker.value = data.requested || '';
  el.picker.title = data.current ? 'Currently running ' + data.current : 'Model for the manager';
}

// -- transcript -----------------------------------------------------------------------
async function loadTranscript() {
  if (!eco.supervisor) return;
  el.log.innerHTML = '';
  let data;
  try {
    data = await api(`${base()}/chat`);
  } catch (err) {
    append('error', 'Could not load the conversation: ' + err.message);
    return;
  }
  (data.messages || []).forEach((m) => {
    if (m.role === 'user') append('user', m.text);
    else if (m.role === 'agent') append('agent', m.text);
    else if (m.role === 'error') append('error', m.text);
    else if (m.role === 'tool') append('tool', m.summary || m.tool);
  });
  if (!(data.messages || []).length) {
    append('agent', 'I read across every app in this product. Ask me where it stands, which '
      + 'defects turned out to be one defect, or what is not covered — and I can commission a '
      + 'module in any of the apps for you to run.');
  }
  setBusy(!!data.busy);
  if (data.blocked) showBlocked(data.blocked);
  else hideBlocked();
  if (data.parked) setState('parked', 'parked');
  conv.pinned = true;
  scrollToEnd(true);
}

// -- events ---------------------------------------------------------------------------
function handleAgentEvent(msg) {
  // Events carry their module. A tester run in another app is streaming over the same socket,
  // and without this its taps and screenshots would scribble into the manager's conversation.
  if (msg.package && eco.supervisor && msg.package !== eco.supervisor) return;
  if (msg.slug && msg.slug !== SLUG) return;

  switch (msg.type) {
    case 'agent_text':
      append('agent', msg.text);
      if (conv.busy) startWorking('Working…');
      break;
    case 'agent_model':
    case 'agent_ready':
      setModel(msg.model, msg.model_label, msg.subscription);
      break;
    case 'agent_tool':
      append('tool', msg.summary || msg.tool);
      if (conv.busy) startWorking(msg.summary || msg.tool);
      break;
    case 'agent_tool_error':
      append('tool failed', msg.text).classList.add('failed');
      break;
    case 'agent_busy': setBusy(msg.busy); break;
    case 'agent_thinking': setState('thinking', 'busy'); startWorking('Thinking…'); break;
    case 'agent_blocked': showBlocked(msg); break;
    case 'agent_unblocked': hideBlocked(); break;
    case 'agent_notice': append('notice', msg.text); break;
    case 'agent_parked':
      append('notice', msg.text);
      setState('parked', 'parked');
      break;
    case 'agent_error': append('error', msg.message); break;
    // The manager commissions work in the apps, so the board it is described on has changed.
    case 'agent_subprojects_proposed':
    case 'agent_subproject_updated':
      onStructureChange();
      break;
    case 'agent_done': {
      const row = document.createElement('div');
      row.className = 'chat-done';
      row.textContent = `${msg.turns} turns · ${Math.round((msg.duration_ms || 0) / 1000)}s`;
      el.log.appendChild(row);
      hideBlocked();
      // Clusters may have been saved during the turn, so the counts on the left are stale.
      onStructureChange();
      scrollToEnd();
      break;
    }
    default: break;   // device events, from a tester run in another project
  }
}

// -- sending --------------------------------------------------------------------------
async function send(text) {
  if (!eco.supervisor) {
    append('error', 'This ecosystem has no manager project yet.');
    return;
  }
  append('user', text);
  hideBlocked();
  try {
    await api(`${base()}/message`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
  } catch (err) {
    append('error', err.message);
  }
}

async function warm() {
  if (!eco.supervisor) return;
  try {
    const data = await api(`${base()}/warm`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    setModel(data.model, data.model_label, data.subscription);
  } catch {
    /* warming is an optimisation; a real failure surfaces on the first message */
  }
}

function initConv(structureChanged) {
  onStructureChange = structureChanged || (() => {});

  el.form.addEventListener('submit', (e) => {
    e.preventDefault();
    const value = el.input.value.trim();
    if (!value) return;
    el.input.value = '';
    send(value);
  });
  el.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); el.form.requestSubmit(); }
  });

  el.log.addEventListener('scroll', () => {
    conv.pinned = el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 40;
    if (conv.pinned) el.jump.hidden = true;
  });
  el.jump.addEventListener('click', () => { conv.pinned = true; scrollToEnd(true); });

  el.trace.addEventListener('change', () => {
    el.log.classList.toggle('hide-trace', !el.trace.checked);
    scrollToEnd(true);
  });

  el.blockedDone.addEventListener('click', async () => {
    const text = el.blockedDone.dataset.reply;
    if (!text || el.blockedDone.disabled) return;
    // Disabled rather than hidden: a second click posts the same answer twice, and the second
    // arrives after the tool resumed — where it is a new instruction mid-turn, not an answer.
    el.blockedDone.disabled = true;
    await send(text);
  });

  el.stop.addEventListener('click', async () => {
    el.stop.disabled = true;
    el.stop.textContent = '■ Stopping…';
    try {
      const data = await api(`${base()}/stop`, { method: 'POST' });
      append('notice', data.stopped ? 'Stopped.' : 'Nothing was running.');
    } catch (err) {
      append('error', err.message);
    } finally {
      el.stop.textContent = '■ Stop';
      el.stop.disabled = !conv.busy;
    }
  });

  el.picker.addEventListener('change', async () => {
    const wanted = el.picker.value || null;
    el.picker.disabled = true;
    try {
      const data = await api(`${base()}/model`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: wanted }) });
      setModel(data.model, data.model_label);
      if (!data.unchanged) {
        append('notice', `Switched to ${prettyModel(data.model) || 'the default model'}. `
          + 'The conversation was resumed, so it still knows where we were.');
      }
    } catch (err) {
      append('error', err.message);
      await loadModelPicker();   // put the control back to what is actually in force
    } finally {
      el.picker.disabled = false;
    }
  });

  syncComposer();
}

/** Called once the board knows which project supervises this ecosystem. */
async function attachConv() {
  await warm();
  await Promise.all([loadTranscript(), loadModelPicker()]);
}

export { append, attachConv, handleAgentEvent, initConv, send };
