// main.js — boot for the manager dashboard.
//
// Four modules, one direction of dependency: api ← board, api ← conv, both ← here. The
// cockpit's graph has 63 cycles and has to initialise everything at once because of it; this
// one is a line, so ordinary imports work and this file is short.

import { eco, loadBoard } from './api.js';
import { initBoard, isOurs, renderCampaignStrip, renderRail, scheduleRefresh,
         show } from './board.js';
import { append, attachConv, handleAgentEvent, initConv } from './conv.js';

// Events that change what this board shows. A tester driving a phone in another project emits
// dozens of `agent_tap` and `agent_screenshot` events a minute and none of them alter a single
// number here; these four do.
//
// This is why the socket handler and not `conv.js` decides: the conversation is the
// supervisor's alone and filters everything else out, but the *board* is about the whole
// product, so a finding filed by any of its apps has to reach it. The manager used only to
// learn about its own turns, which meant a run in the cockpit could fill an app with defects
// while this page sat showing the numbers from before it started.
const BOARD_EVENTS = new Set([
  'agent_finding',                 // a defect was filed — counts, bars and clusters all move
  'agent_subprojects_proposed',    // recon proposed modules — coverage denominators move
  'agent_subproject_updated',      // a module was approved or marked tested
  'agent_done',                    // end of a run: last chance to catch anything missed
  'agent_campaign',                // a sweep advanced a step, paused, or finished
]);

/** Live campaign progress, straight onto the topbar strip.
 *
 *  Not routed through `scheduleRefresh`: that debounces 1.5s and re-reads five projects'
 *  findings, which is right for a defect count and absurd for a progress bar. The event
 *  already carries the counts, so the indicator moves the instant a module starts.
 */
function handleCampaignEvent(msg) {
  if (msg.type !== 'agent_campaign') return;
  const summary = { live: 1, campaigns: [{ ...msg, id: msg.campaign }] };
  renderCampaignStrip(msg.status === 'done' || msg.status === 'stopped'
    ? { live: 0, campaigns: [] }
    : summary);
}

const conn = document.getElementById('conn');
const connLabel = document.getElementById('connLabel');
const railRight = document.getElementById('railRight');

function setConn(stateName) {
  conn.className = 'conn-indicator ' + stateName;
  connLabel.textContent = stateName;
}

// replay=false: the backlog exists to redraw the flow graph, and there is no graph here.
function connectWs() {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${window.location.host}/ws?replay=false`);
  ws.onopen = () => setConn('online');
  ws.onclose = () => { setConn('offline'); setTimeout(connectWs, 2000); };
  ws.onerror = () => setConn('offline');
  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }
    if (!msg.type || !msg.type.startsWith('agent_')) return;
    handleAgentEvent(msg);                       // the conversation: supervisor only
    handleCampaignEvent(msg);                    // the strip: immediate, not debounced
    if (BOARD_EVENTS.has(msg.type) && isOurs(msg.package)) scheduleRefresh();
  };
}

document.getElementById('toggleRightBtn').addEventListener('click', () => {
  railRight.classList.toggle('collapsed');
});
document.addEventListener('keydown', (e) => {
  if (e.key === ']' && e.target.tagName !== 'TEXTAREA' && e.target.tagName !== 'INPUT') {
    railRight.classList.toggle('collapsed');
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
initBoard();
// The conversation asks for a redraw when the manager changes the product — a module
// commissioned in an app, a cluster saved. Through the same debounce as the socket path, so
// the two do not each trigger their own read of five projects' findings.
initConv(() => scheduleRefresh());
connectWs();

loadBoard()
  .then(() => {
    renderRail();
    return show('overview');
  })
  .then(() => {
    if (!eco.supervisor) {
      append('notice', 'This ecosystem has no manager project, so there is nobody to talk to '
        + 'here yet. Create one with:  python ecosystem.py --tag <name> <name> supervisor');
      return null;
    }
    return attachConv();
  })
  .catch((err) => {
    document.getElementById('canvas').textContent = '';
    const box = document.createElement('div');
    box.className = 'canvas-warn';
    box.textContent = 'Could not load the product: ' + err.message;
    document.getElementById('canvas').appendChild(box);
  });
