// transcript.js — extracted verbatim from the old dashboard.js IIFE.

import { ui } from './state.js';
import { agent, agentEl, agentFetch, appendChat, scrollChatToEnd, setAgentBusy, setAgentModel, setAgentState, startWorking, syncComposerEnabled } from './chat.js';
import { refreshFrame, sendToAgent } from './compose.js';
import { loadModules, renderModuleHighlight } from './modules.js';
import { refreshOutcomes, refreshSectionNotes } from './outcomes.js';
import { chatLog } from './phone.js';

async function loadTranscript() {
  if (!agent.package || !agent.slug) return;
  // Two loads can be in flight at once (switching project and module in quick succession).
  // Without this guard each clears the log and then appends its own history, so the two
  // interleave and you end up reading one module's conversation under another's name.
  const token = ++ui.transcriptRequest;
  const forPackage = agent.package;
  const forSlug = agent.slug;
  chatLog.innerHTML = '';
  let data;
  try {
    data = await agentFetch(`/agent/${encodeURIComponent(forPackage)}/${forSlug}/chat`);
  } catch (err) {
    if (token === ui.transcriptRequest) appendChat('error', 'Could not load the transcript: ' + err.message);
    return;
  }
  if (token !== ui.transcriptRequest) return;   // a newer selection won; discard this one
  chatLog.innerHTML = '';
  (data.messages || []).forEach((m) => {
    if (m.role === 'user') appendChat('user', m.text || '');
    else if (m.role === 'agent') appendChat('agent', m.text || '');
    else if (m.role === 'error') appendChat('error', m.text || '');
    else if (m.role === 'tool') appendChat('tool', m.summary || m.tool || '');
    else if (m.role === 'shot' && m.path) appendShot(m.path, m.note);
  });
  if (!(data.messages || []).length) {
    appendChat('agent', 'Tell me what to test in this module and I will plan the cases, run '
      + 'them on the phone, and report what I actually observe. I will stop and ask only if '
      + 'I hit something I cannot get past on my own.');
  }
  refreshOutcomes();
  refreshSectionNotes();
  setAgentBusy(!!data.busy);
  if (data.blocked) showBlocked(data.blocked);
  else hideBlocked();
  if (data.parked) setAgentState('parked', 'parked');
  renderModuleHighlight();
  ui.chatPinned = true;
  scrollChatToEnd(true);
}

// What the tick sends, per kind of question. A credential has no default — there is a value
// only the user knows — so it gets no button at all rather than one that would answer wrongly.
const DONE_REPLIES = {
  approval: { label: '✓ Approve', text: 'Approved — go ahead.' },
  question: {
    label: '✓ Done',
    // Phrased as a report of an action rather than a bare "done": the agent is resuming a
    // tool call with this as the reply, and "done" alone leaves it guessing what was done.
    text: 'Done — I have done what you asked on the phone. Carry on from here, and check the '
      + 'current screen yourself rather than assuming what it is.',
  },
};

function showBlocked(question) {
  agentEl.blocked.classList.add('open');
  agentEl.blockedLabel.textContent = question.kind === 'credential'
    ? 'The agent needs a credential'
    : question.kind === 'approval'
      ? 'The agent is waiting for your approval'
      : 'The agent needs an answer';
  agentEl.blockedQuestion.textContent = question.question || '';
  const reply = DONE_REPLIES[question.kind === 'approval' ? 'approval' : 'question'];
  const offerDone = question.kind !== 'credential';
  agentEl.blockedDone.classList.toggle('hidden', !offerDone);
  agentEl.blockedDone.disabled = false;
  if (offerDone) {
    agentEl.blockedDone.textContent = reply.label;
    agentEl.blockedDone.dataset.reply = reply.text;
  }
  setAgentState('blocked', 'blocked');
  syncComposerEnabled();   // the run is still busy; being blocked is what re-opens the composer
  agentEl.input.focus();
}

function hideBlocked() {
  agentEl.blocked.classList.remove('open');
  syncComposerEnabled();
}

// Deferred out of module scope for the reason main.js explains: this reads `sendToAgent`
// across an import cycle, so registering it here at evaluation time can hit a dead zone.
function initBlockedDone() {
  agentEl.blockedDone.addEventListener('click', async () => {
    const text = agentEl.blockedDone.dataset.reply;
    if (!text || agentEl.blockedDone.disabled) return;
    // Disabled, not hidden: a second click while the first is in flight would post the same
    // answer twice, and the second one arrives after the tool has resumed — where it is no
    // longer an answer but a new instruction mid-turn.
    agentEl.blockedDone.disabled = true;
    await sendToAgent(text);   // hides the box itself, and unblocks the parked tool
  });
}

function appendShot(path, note) {
  const wrap = document.createElement('div');
  wrap.className = 'chat-shot';
  const img = document.createElement('img');
  img.src = '/agent/shot?path=' + encodeURIComponent(path);
  img.alt = note || 'screenshot';
  img.addEventListener('click', () => {
    document.getElementById('shotLightboxImg').src = img.src;
    document.getElementById('shotLightbox').classList.add('open');
  });
  wrap.appendChild(img);
  if (note) {
    const cap = document.createElement('div');
    cap.className = 'chat-shot-note';
    cap.textContent = note;
    wrap.appendChild(cap);
  }
  chatLog.appendChild(wrap);
  scrollChatToEnd();
}

function appendFinding(f) {
  const box = document.createElement('div');
  box.className = 'chat-finding';
  const head = document.createElement('div');
  head.className = 'chat-finding-head';
  const kind = f.kind || 'bug';
  const sev = (kind === 'pass' || !f.severity || f.severity === 'none') ? '' : ` · ${f.severity}`;
  head.textContent = `${f.id} · ${kind}${sev} · ${f.title}`;
  box.appendChild(head);
  [['Expected', f.expected], ['Actual', f.actual]].forEach(([label, value]) => {
    if (!value) return;
    const row = document.createElement('div');
    row.className = 'chat-finding-row';
    const b = document.createElement('b');
    b.textContent = label + ': ';
    row.appendChild(b);
    row.appendChild(document.createTextNode(value));
    box.appendChild(row);
  });
  chatLog.appendChild(box);
  scrollChatToEnd();
}

function handleAgentEvent(msg) {
  // Events carry their module, so a background run cannot scribble into the chat you
  // are currently reading.
  if (msg.slug && agent.slug && msg.slug !== agent.slug) {
    if (msg.type === 'agent_finding' || msg.type === 'agent_subprojects_proposed') loadModules();
    return;
  }
  switch (msg.type) {
    case 'agent_text':
      appendChat('agent', msg.text);
      // Text between tool calls means it is still working, not that it finished.
      if (agent.busy) startWorking('Working…');
      break;
    case 'agent_model': setAgentModel(msg.model, msg.model_label, msg.subscription); break;
    case 'agent_ready':
      setAgentModel(msg.model, msg.model_label, msg.subscription);
      break;
    case 'agent_tool':
      appendChat('tool', msg.summary || msg.tool);
      if (agent.busy) startWorking(msg.summary || msg.tool);
      break;
    case 'agent_tool_error': {
      const row = appendChat('tool failed', msg.text);
      row.classList.add('failed');
      break;
    }
    case 'agent_busy': setAgentBusy(msg.busy); break;
    case 'agent_thinking':
      setAgentState('thinking', 'busy');
      startWorking('Thinking…');
      break;
    case 'agent_screenshot':
      agent.shots += 1;
      appendShot(msg.path, msg.note);
      updateMeters();
      break;
    case 'agent_tap':
      agent.taps += 1;
      updateMeters();
      if (agentEl.liveToggle.checked) refreshFrame();
      break;
    case 'agent_journey_step':
      appendChat('tool', `flow graph · ${msg.section} · ${msg.label}`);
      break;
    case 'agent_note':
      appendChat('tool', `note · ${msg.note.kind} · ${msg.note.section}`);
      refreshSectionNotes();
      break;
    case 'agent_finding':
      appendFinding(msg.finding);
      loadModules();
      refreshOutcomes();
      break;
    case 'agent_blocked': showBlocked(msg); break;
    case 'agent_unblocked': hideBlocked(); break;
    case 'agent_notice': appendChat('notice', msg.text); break;
    case 'agent_parked':
      appendChat('notice', msg.text);
      setAgentState('parked', 'parked');
      break;
    case 'agent_error': appendChat('error', msg.message); break;
    case 'agent_subprojects_proposed': loadModules(); break;
    case 'agent_subproject_updated': loadModules(); break;
    case 'agent_done': {
      const bits = [`${msg.turns} turns`, `${Math.round((msg.duration_ms || 0) / 1000)}s`,
        `${msg.taps} taps`, `${msg.findings} finding${msg.findings === 1 ? '' : 's'}`];
      if (msg.stepper_calls) bits.push(`${msg.stepper_calls} cheap calls ($${msg.stepper_cost_usd})`);
      const row = document.createElement('div');
      row.className = 'chat-done';
      row.textContent = bits.join(' · ');
      chatLog.appendChild(row);
      agent.taps = msg.taps || agent.taps;
      agent.shots = msg.shots || agent.shots;
      if (msg.stepper_calls) {
        document.getElementById('meterStepperRow').hidden = false;
        agentEl.meterStepper.textContent = `${msg.stepper_calls} calls · $${msg.stepper_cost_usd}`;
      }
      updateMeters();
      hideBlocked();
      scrollChatToEnd();
      break;
    }
    default: break;
  }
}

function updateMeters() {
  document.getElementById('statActions').textContent =
    `${agent.taps} taps · ${agent.shots} shots`;
}

// -- Chat image attachments -----------------------------------------------------------
// Staged locally, uploaded on send. Each becomes a file in the module's own shots/ folder
// and reaches the agent as a *path*, which it opens with Read — the same way it looks at
// its own evidence. Inlining the bytes into the message would bloat the transcript on disk
// and make the same image unreadable the second time it came up.
const attachmentsEl = document.getElementById('chatAttachments');
const chatImageInput = document.getElementById('chatImageInput');

function renderAttachments() {
  attachmentsEl.innerHTML = '';
  ui.pendingAttachments.forEach((att, i) => {
    const box = document.createElement('div');
    box.className = 'chat-attachment';
    const img = document.createElement('img');
    img.src = att.dataUrl;
    img.alt = att.name;
    img.title = att.name;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '✕';
    remove.title = 'Remove';
    remove.addEventListener('click', () => {
      ui.pendingAttachments.splice(i, 1);
      renderAttachments();
    });
    box.append(img, remove);
    attachmentsEl.appendChild(box);
  });
}

export { appendFinding, appendShot, attachmentsEl, chatImageInput, handleAgentEvent, hideBlocked, initBlockedDone, loadTranscript, renderAttachments, showBlocked, updateMeters };
