// status.js — extracted verbatim from the old dashboard.js IIFE.

import { ui } from './state.js';
import { statusBanner, statusMessage } from './feed.js';

function showStatus(message, level) {
  statusBanner.className = 'status-banner visible level-' + (level || 'info');
  statusMessage.textContent = message;
  clearTimeout(ui.statusHideTimer);
  if (level === 'ok') ui.statusHideTimer = setTimeout(hideStatus, 6000);
}
function hideStatus() { statusBanner.classList.remove('visible'); }

// ---------------------------------------------------------------------------
// Lock / alert popup — a blocking modal for conditions that need the user to
// go do something physical on the device (e.g. unlock it), not just glance at
// the thin status banner.
// ---------------------------------------------------------------------------
const lockPopupBackdrop = document.getElementById('lockPopupBackdrop');
const lockPopup = document.getElementById('lockPopup');
const lockPopupIcon = document.getElementById('lockPopupIcon');
const lockPopupMessage = document.getElementById('lockPopupMessage');
const lockPopupDismiss = document.getElementById('lockPopupDismiss');

function showLockPopup(message, level) {
  lockPopup.className = 'lock-popup level-' + (level || 'warning');
  lockPopupIcon.textContent = level === 'error' ? '⚠️' : '🔒';
  lockPopupMessage.textContent = message;
  lockPopupBackdrop.classList.add('open');
  if (window.Notification && Notification.permission === 'default') {
    Notification.requestPermission();
  }
  if (window.Notification && Notification.permission === 'granted' && document.hidden) {
    new Notification('QA Tester AI', { body: message });
  }
}
function hideLockPopup() { lockPopupBackdrop.classList.remove('open'); }

lockPopupDismiss.addEventListener('click', hideLockPopup);
lockPopupBackdrop.addEventListener('click', (evt) => {
  if (evt.target === lockPopupBackdrop) hideLockPopup();
});

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
// One pill carries both the socket and the phone. They were separate, and "Live" told you
// the browser had a socket — which is not the question anyone was asking. What you want to
// know before starting a run is *which phone am I about to drive*, and a green dot next to
// the word "Live" answered that with something that was true even with no device attached.
// The session id used to have its own pill in the top bar. That row now carries things you
// act on — the outcome counts, the phone — and a hex session id was never one of them. It
// stays reachable as a tooltip on the device pill for when a log line needs matching up.
let sessionLabel = null;
function setSessionLabel(id) { sessionLabel = id || null; }

const devicePill = document.getElementById('devicePill');
const deviceNameEl = document.getElementById('deviceName');
let connState = 'connecting';
let deviceLabel = null;

function renderDevicePill() {
  devicePill.classList.toggle('online', connState === 'online' && Boolean(deviceLabel));
  if (connState === 'offline') { deviceNameEl.textContent = 'Disconnected'; return; }
  if (connState !== 'online') { deviceNameEl.textContent = 'Connecting…'; return; }
  deviceNameEl.textContent = deviceLabel || 'No device';
}

function setConnState(state) {
  connState = state;
  renderDevicePill();
}

// Polled rather than pushed: a phone can be unplugged at any moment and nothing in the
// server would think to broadcast that. 10s is often enough to notice, rare enough that an
// idle dashboard is not running adb in a loop.
async function refreshDeviceInfo() {
  try {
    const resp = await fetch('/device/info');
    const info = await resp.json();
    deviceLabel = info.label || null;
    devicePill.title = [
      info.serial ? `${info.label}\nserial: ${info.serial}` : 'No device detected — check the cable and `adb devices`',
      info.count > 1 ? `${info.count} devices attached` : '',
      sessionLabel ? `session: ${sessionLabel}` : '',
    ].filter(Boolean).join('\n');
  } catch {
    deviceLabel = null;   // the server is the only way to know; assume nothing on failure
  }
  renderDevicePill();
}

// Moved into main.js's boot sequence: with 63 import cycles in this graph, a call
// at module scope can run while a module it depends on is still initialising.

export { connState, deviceLabel, deviceNameEl, devicePill, hideLockPopup, hideStatus, lockPopup, lockPopupBackdrop, lockPopupDismiss, lockPopupIcon, lockPopupMessage, refreshDeviceInfo, renderDevicePill, sessionLabel, setConnState, setSessionLabel, showLockPopup, showStatus };
