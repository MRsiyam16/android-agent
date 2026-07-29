// phone.js — extracted verbatim from the old dashboard.js IIFE.

import { ui } from './state.js';

// Remote control panel
// ---------------------------------------------------------------------------
const phoneScreen = document.getElementById('phoneScreen');
const phonePlaceholder = document.getElementById('phonePlaceholder');
const overlayLayer = document.getElementById('overlayLayer');
const overlayToggle = document.getElementById('overlayToggle');
const chatLog = document.getElementById('chatLog');

function setPhoneFrame(b64) {
  if (!b64) return;
  phoneScreen.src = 'data:image/jpeg;base64,' + b64;
  phoneScreen.classList.add('loaded');
  phonePlaceholder.classList.add('hidden');
  drawOverlay();
}

function drawOverlay() {
  overlayLayer.innerHTML = '';
  if (!overlayToggle.checked || !ui.lastElements.length) return;
  const rect = phoneScreen.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const sx = rect.width / ui.lastDims.w;
  const sy = rect.height / ui.lastDims.h;
  ui.lastElements.forEach((el) => {
    if (!el.bounds) return;
    const [x1, y1, x2, y2] = el.bounds;
    const box = document.createElement('div');
    box.className = 'overlay-box';
    box.style.left = (x1 * sx) + 'px';
    box.style.top = (y1 * sy) + 'px';
    box.style.width = Math.max(2, (x2 - x1) * sx) + 'px';
    box.style.height = Math.max(2, (y2 - y1) * sy) + 'px';
    box.title = el.label || '';
    overlayLayer.appendChild(box);
  });
}
overlayToggle.addEventListener('change', drawOverlay);
window.addEventListener('resize', drawOverlay);

function ripple(px, py) {
  const dot = document.createElement('div');
  dot.className = 'tap-ripple';
  dot.style.left = px + 'px';
  dot.style.top = py + 'px';
  overlayLayer.appendChild(dot);
  setTimeout(() => dot.remove(), 650);
}

// Looks its own elements up rather than closing over `agentEl` / `agent`: this runs from
// appendChat, which is reachable before those consts are initialised.

export { chatLog, drawOverlay, overlayLayer, overlayToggle, phonePlaceholder, phoneScreen, ripple, setPhoneFrame };
