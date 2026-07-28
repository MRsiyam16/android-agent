// socket.js — extracted verbatim from the old dashboard.js IIFE.

import { handleAgentEvent } from './transcript.js';

function connectWs() {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(proto + '://' + window.location.host + '/ws');

  ws.onopen = () => setConnState('online');
  ws.onclose = () => { setConnState('offline'); setTimeout(connectWs, 2000); };
  ws.onerror = () => setConnState('offline');

  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }

    if (msg.type === 'history') {
      (msg.items || []).forEach(ingest);
      setSessionLabel(ui.currentSessionId);
      network.fit({ animation: false });
      scheduleRenderOverlay();
      restoreSavedAnnotations();
    } else if (msg.type === 'telemetry') {
      ingest(msg.payload);
      setSessionLabel(ui.currentSessionId);
    } else if (msg.type === 'clear') {
      resetGraph();
    } else if (msg.type === 'status') {
      showStatus(msg.message, msg.level);
      if (msg.popup) showLockPopup(msg.message, msg.level);
      else if (msg.level === 'ok') hideLockPopup();
    } else if (msg.type && msg.type.startsWith('agent_')) {
      handleAgentEvent(msg);
    }
  };
}

// ---------------------------------------------------------------------------
// Dock: zoom, fit, settings popover, observe-flow, fullscreen, live preview,
// save/import, clear
// ---------------------------------------------------------------------------
// Moved into main.js's boot sequence: with 63 import cycles in this graph, a call
// at module scope can run while a module it depends on is still initialising.

export { connectWs };
