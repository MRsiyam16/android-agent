// main.js — boot for the manager dashboard.
//
// Four modules, one direction of dependency: api ← board, api ← conv, both ← here. The
// cockpit's graph has 63 cycles and has to initialise everything at once because of it; this
// one is a line, so ordinary imports work and this file is short.

import { eco, loadBoard } from './api.js';
import { initBoard, refresh, renderRail, show } from './board.js';
import { append, attachConv, handleAgentEvent, initConv } from './conv.js';

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
    if (msg.type && msg.type.startsWith('agent_')) handleAgentEvent(msg);
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
// The board's refresh is what the conversation calls when the manager changes the shape of the
// product — creating a module in an app, or saving a cluster — so the left rail is not still
// showing the counts from before the turn.
initConv(() => { refresh().catch(() => { /* the chat is usable with a stale board */ }); });
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
