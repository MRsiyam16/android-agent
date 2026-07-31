// main.js — extracted verbatim from the old dashboard.js IIFE.

import { fetchProjects, openProject, setProjectPill } from './board.js';
import { agent, agentEl, agentFetch, appendChat, initChatScroll, scrollChatToEnd, setAgentModel } from './chat.js';
import { initAgent, initComposerAttachments, loadSecretNames, refreshFrame, sendCommand, sendToAgent } from './compose.js';
import { updateStats } from './feed.js';
import { loadModules, selectModule } from './modules.js';
import { chatLog, phoneScreen, ripple } from './phone.js';
import { initBlockedDone } from './transcript.js';
import { initToolOverlay, setTool } from './tools.js';
import { connectWs } from './socket.js';
import { initModal } from './modal.js';
import { initScreenSearch } from './screens.js';
import { ui } from './state.js';
import { refreshDeviceInfo, showStatus } from './status.js';
import { initZoomLabel, leftSidebar, restoreRailPrefs, rightSidebar, setRail, wireRailResize } from './viewport.js';

// Warming on selection means the CLI spawn happens while you are still typing.
async function warmModule() {
  if (!agent.package || !agent.slug) return;
  try {
    const data = await agentFetch(
      `/agent/${encodeURIComponent(agent.package)}/${agent.slug}/warm`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    setAgentModel(data.model, data.model_label, data.subscription);
  } catch {
    /* Warming is an optimisation; a failure surfaces on the first real message. */
  }
}

agentEl.project.addEventListener('change', () => {
  agent.package = agentEl.project.value;
  agent.slug = null;
  agent.loadedKey = null;
  loadModules();
});
agentEl.subproject.addEventListener('change', () => selectModule(agentEl.subproject.value));

agentEl.stop.addEventListener('click', async () => {
  if (!agent.package || !agent.slug) return;
  // Feedback before the round-trip. The server cancels the device first and then ends the
  // turn, which is fast but not instant, and a button that looks inert for a second is
  // what made people press it repeatedly and conclude it did nothing.
  agentEl.stop.disabled = true;
  agentEl.stop.textContent = '■ Stopping…';
  try {
    const data = await agentFetch(
      `/agent/${encodeURIComponent(agent.package)}/${agent.slug}/stop`, { method: 'POST' });
    appendChat('notice', data.stopped
      ? 'Stopped. The step in flight was cancelled.'
      : 'Nothing was running.');
  } catch (err) {
    appendChat('error', err.message);
  } finally {
    agentEl.stop.textContent = '■ Stop';
    agentEl.stop.disabled = !agent.busy;
  }
});

// Called straight after a project is created. Creates the project's manager module — Main —
// and starts it on the setup interview: it asks what you want out of the app before it asks
// to look at it, so the module breakdown answers your priorities rather than just enumerating
// screens. Main then stays open as the module you come back to for "add a module for X" and
// "where does this project stand".
async function startProjectMain(pkg) {
  try {
    const data = await agentFetch(`/agent/${encodeURIComponent(pkg)}/main`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    setRail('right', true);
    await loadModules();
    // The slug the server actually used, not a hardcoded 'main'. A project that predates the
    // manager keeps its interview under `onboarding`, and selecting a module that is not there
    // would leave the rail pointing at nothing.
    await selectModule(data.slug || 'main');
  } catch (err) {
    // The project itself was created; only the interview failed to start, and saying so is
    // better than leaving a project that looks half-made.
    showStatus('Project created, but the setup interview could not start: ' + err.message,
      'error');
  }
}

document.getElementById('agentReconBtn').addEventListener('click', async () => {
  if (!agent.package) { appendChat('error', 'Pick a project first.'); return; }
  try {
    await agentFetch(`/agent/${encodeURIComponent(agent.package)}/recon`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    await loadModules();
    await selectModule('recon');
    appendChat('notice', 'Recon started — the agent will explore the app and propose modules.');
  } catch (err) {
    appendChat('error', err.message);
  }
});

// The always-open two-field form that used to sit here is now the Add module sheet.

document.getElementById('agentSecretForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = document.getElementById('agentSecretName');
  const value = document.getElementById('agentSecretValue');
  if (!name.value.trim() || !value.value || !agent.package) return;
  try {
    await agentFetch('/agent/' + encodeURIComponent(agent.package) + '/secrets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.value.trim(), value: value.value }),
    });
    name.value = '';
    value.value = '';
    loadSecretNames();
  } catch (err) {
    appendChat('error', err.message);
  }
});

document.getElementById('chatForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const value = agentEl.input.value.trim();
  if (!value) return;
  agentEl.input.value = '';
  if (value.startsWith('/')) sendCommand(value.slice(1).trim());
  else sendToAgent(value);
});

agentEl.input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    document.getElementById('chatForm').requestSubmit();
  }
});

agentEl.jump.addEventListener('click', () => { ui.chatPinned = true; scrollChatToEnd(true); });

agentEl.traceToggle.addEventListener('change', () => {
  chatLog.classList.toggle('hide-trace', !agentEl.traceToggle.checked);
  scrollChatToEnd(true);
});

const lightbox = document.getElementById('shotLightbox');
lightbox.addEventListener('click', () => lightbox.classList.remove('open'));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') lightbox.classList.remove('open');
});

agentEl.liveToggle.addEventListener('change', () => {
  if (agent.liveTimer) { clearInterval(agent.liveTimer); agent.liveTimer = null; }
  // Only poll while the agent is actually working; a fixed interval on an idle phone is
  // just wasted ADB round trips.
  if (agentEl.liveToggle.checked) agent.liveTimer = setInterval(() => {
    if (agent.busy) refreshFrame();
  }, 2500);
});

document.querySelectorAll('.chip-btn[data-cmd]').forEach((btn) => {
  btn.addEventListener('click', () => sendCommand(btn.dataset.cmd));
});

document.getElementById('refreshFrameBtn').addEventListener('click', refreshFrame);

phoneScreen.addEventListener('click', (evt) => {
  const rect = phoneScreen.getBoundingClientRect();
  const px = evt.clientX - rect.left;
  const py = evt.clientY - rect.top;
  ripple(px, py);
  const devX = Math.round((px / rect.width) * ui.lastDims.w);
  const devY = Math.round((py / rect.height) * ui.lastDims.h);
  sendCommand(`tap ${devX} ${devY}`);
});

// ---------------------------------------------------------------------------
// Boot
//
// Everything is on screen at once now, so everything initialises at once. Under the tabs
// this work was deferred until you clicked into a view, which is why the agent used to
// take a moment to wake up the first time you spoke to it.
// ---------------------------------------------------------------------------
// These five used to run at the point their module's code appeared in the old single file.
// As modules they cannot: this graph has 63 import cycles, so a call at module scope can
// fire while a module it depends on is still initialising and hit a temporal-dead-zone
// error that only shows up in the browser. Everything imperative happens here instead,
// after every module has finished evaluating, in the order the old file ran them.
// Listener registrations that read across a cycle, deferred out of their own modules for
// the same reason. Each is a one-line init exported by the module that owns the handler.
initToolOverlay();
initZoomLabel();
initModal();
initScreenSearch();
initChatScroll();
initComposerAttachments();
initBlockedDone();

setTool('pan');
refreshDeviceInfo();
setInterval(refreshDeviceInfo, 10000);
connectWs();
wireRailResize('leftResize', leftSidebar, 'left', 210, 460);
wireRailResize('rightResize', rightSidebar, 'right', 300, 620);

restoreRailPrefs();
setProjectPill(ui.currentPackage);
updateStats();
fetchProjects();
initAgent();

// The board is per-package and the agent remembers the module you had open, so reopening
// the last project is what makes a reload continue rather than restart.
fetch('/agent/status')
  .then((r) => r.json())
  .then((s) => {
    if (s.last_opened && s.last_opened.package && !ui.currentPackage) {
      openProject(s.last_opened.package);
    }
  })
  .catch(() => { /* the dashboard is fully usable without the agent */ });

export { lightbox, startProjectMain, warmModule };
