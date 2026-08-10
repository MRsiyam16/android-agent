// compose.js — extracted verbatim from the old dashboard.js IIFE.

import { ui } from './state.js';
import { agent, agentEl, agentFetch, appendChat, scrollChatToEnd, setAgentModel } from './chat.js';
import { loadAgentProjects } from './modules.js';
import { chatLog, phonePlaceholder, setPhoneFrame } from './phone.js';
import { hidePresets } from './presets.js';
import { chatImageInput, hideBlocked, renderAttachments } from './transcript.js';

function stageImage(file) {
  if (!file || !file.type.startsWith('image/')) return;
  const reader = new FileReader();
  reader.onload = () => {
    ui.pendingAttachments.push({ name: file.name || 'image', dataUrl: String(reader.result) });
    renderAttachments();
  };
  reader.readAsDataURL(file);
}

// Deferred: reads a binding imported from a module that imports this one back, so at
// evaluation time it may still be in its temporal dead zone. main.js calls it.
function initComposerAttachments() {
  document.getElementById('chatAttachBtn')
    .addEventListener('click', () => chatImageInput.click());
  chatImageInput.addEventListener('change', () => {
    Array.from(chatImageInput.files || []).forEach(stageImage);
    chatImageInput.value = '';
  });

  // Paste and drag-drop, because that is how a reference screenshot actually arrives.
  agentEl.input.addEventListener('paste', (e) => {
    const items = Array.from((e.clipboardData && e.clipboardData.items) || []);
    const images = items.filter((it) => it.type.startsWith('image/'));
    if (!images.length) return;
    e.preventDefault();
    images.forEach((it) => stageImage(it.getAsFile()));
  });
  agentEl.input.addEventListener('dragover', (e) => e.preventDefault());
  agentEl.input.addEventListener('drop', (e) => {
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || [])
      .filter((f) => f.type.startsWith('image/'));
    if (!files.length) return;
    e.preventDefault();
    files.forEach(stageImage);
  });
}

async function uploadAttachments() {
  const paths = [];
  for (const att of ui.pendingAttachments) {
    const data = await agentFetch(
      `/agent/${encodeURIComponent(agent.package)}/${agent.slug}/attachment`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data_url: att.dataUrl }) });
    paths.push(data.path);
  }
  return paths;
}

async function sendToAgent(text) {
  if (!agent.package || !agent.slug) {
    appendChat('error', 'Pick a project and a module first.');
    return;
  }
  // Before the upload, not after the POST. The suggestion strip is only for a module with
  // nothing in it, and sending anything — typed or from a preset — ends that; leaving it up
  // through a multi-second attachment upload invites a second click on a preset whose
  // instruction is already on its way.
  hidePresets();
  const staged = ui.pendingAttachments.slice();
  let paths = [];
  try {
    paths = await uploadAttachments();
  } catch (err) {
    appendChat('error', 'Could not attach the image: ' + err.message);
    return;   // sending without it would leave the agent referring to a file that is not there
  }
  ui.pendingAttachments = [];
  renderAttachments();

  appendChat('user', text);
  staged.forEach((att) => appendChatImage(att.dataUrl));
  hideBlocked();

  // The paths ride along as a plain instruction rather than a separate channel: the agent
  // has Read, and telling it where the file is *is* the interface.
  const body = paths.length
    ? `${text}\n\nReference image${paths.length > 1 ? 's' : ''} the user attached — `
      + `Read ${paths.length > 1 ? 'these paths' : 'this path'} to look at `
      + `${paths.length > 1 ? 'them' : 'it'}:\n` + paths.map((p) => `- ${p}`).join('\n')
    : text;
  try {
    await agentFetch(`/agent/${encodeURIComponent(agent.package)}/${agent.slug}/message`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: body }),
    });
  } catch (err) {
    appendChat('error', err.message);
  }
}

function appendChatImage(src) {
  const img = document.createElement('img');
  img.className = 'chat-msg-image';
  img.src = src;
  img.alt = 'Attached reference';
  img.addEventListener('click', () => {
    document.getElementById('shotLightboxImg').src = src;
    document.getElementById('shotLightbox').classList.add('open');
  });
  chatLog.appendChild(img);
  scrollChatToEnd();
}

// The old manual command parser survives behind a `/` prefix, so the phone can still be
// driven by hand without going through the agent.
async function sendCommand(command) {
  appendChat('cmd', '/' + command);
  try {
    const data = await agentFetch('/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command }),
    });
    appendChat('ok', 'Executed: ' + data.command);
    if (data.screenshot_b64) setPhoneFrame(data.screenshot_b64);
  } catch (err) {
    appendChat('error', err.message);
  }
}

// `resync` is the "match device" ask: it costs iOS an extra WDA round trip to re-read
// window_size (see /device/frame's docstring), so it's only worth paying for on an
// explicit press of Refresh — not on the 2.5s "Follow live" poll while the agent runs.
async function refreshFrame(resync = false) {
  const params = new URLSearchParams();
  if (agent.package && agent.slug) { params.set('package', agent.package); params.set('slug', agent.slug); }
  if (resync) params.set('resync', 'true');
  const query = params.toString() ? `?${params}` : '';
  try {
    const data = await agentFetch('/device/frame' + query);
    if (data.screenshot_b64) setPhoneFrame(data.screenshot_b64, { w: data.width, h: data.height });
  } catch (err) {
    phonePlaceholder.textContent = err.message;
  }
}

async function loadSecretNames() {
  if (!agent.package) return;
  try {
    const data = await agentFetch('/agent/' + encodeURIComponent(agent.package) + '/secrets');
    agentEl.secretNames.textContent = (data.names || []).length
      ? 'Stored: ' + data.names.join(', ')
      : 'None stored. The agent will ask when it needs one.';
  } catch {
    agentEl.secretNames.textContent = '';
  }
}

function initAgent() {
  if (agent.ready) { refreshFrame(); return; }
  agent.ready = true;
  loadAgentProjects().then(refreshFrame);
  // Follow-live is on by default, so kick the poller off rather than waiting for a toggle.
  agentEl.liveToggle.dispatchEvent(new Event('change'));

  fetch('/agent/status').then((r) => r.json()).then((s) => {
    // A placeholder until a live session reports the real subscription type below.
    document.getElementById('statAuth').textContent = 'auth subscription';
    if (s.cheap_tier && s.stepper_model) {
      document.getElementById('agentMeter').hidden = false;
      document.getElementById('meterStepperRow').hidden = false;
      agentEl.meterStepper.textContent = s.stepper_model.split('/').pop();
    }
    // A session pre-warmed at startup already knows its model — adopt that rather than
    // showing "connecting…" until the first message.
    const live = (s.sessions || []).find((x) => x.model);
    if (live) setAgentModel(live.model, live.model_label, live.subscription);
  }).catch(() => {});
}

export { initComposerAttachments, appendChatImage, initAgent, loadSecretNames, refreshFrame, sendCommand, sendToAgent, stageImage, uploadAttachments };
