// viewport.js — extracted verbatim from the old dashboard.js IIFE.

import { refreshFrame } from './compose.js';
import { resetGraph } from './feed.js';
import { relayoutAll } from './layout.js';
import { container, graphWrap, network, scheduleRenderOverlay } from './render.js';
import { ui } from './state.js';

function updateZoomLabel() {
  document.getElementById('zoomPct').textContent = Math.round(network.getScale() * 100) + '%';
}
document.getElementById('zoomInBtn').addEventListener('click', () => {
  network.moveTo({ scale: network.getScale() * 1.2 });
  updateZoomLabel();
});
document.getElementById('zoomOutBtn').addEventListener('click', () => {
  network.moveTo({ scale: network.getScale() / 1.2 });
  updateZoomLabel();
});
document.getElementById('fitBtn').addEventListener('click', () => {
  network.fit({ animation: true });
  setTimeout(updateZoomLabel, 350);
});
// Deferred: this reads a binding imported from a module that imports this one back,
// so at evaluation time it may still be in its temporal dead zone. main.js calls it.
function initZoomLabel() {
  network.on('zoom', updateZoomLabel);
}

const settingsBtn = document.getElementById('settingsBtn');
const settingsPopover = document.getElementById('settingsPopover');
const dockEl = document.getElementById('dock');

function positionSettingsPopover() {
  // Anchor under the settings button specifically (not the dock's center) so it
  // stays correctly placed no matter where the button sits in the bar.
  const dockRect = dockEl.getBoundingClientRect();
  const btnRect = settingsBtn.getBoundingClientRect();
  const popW = settingsPopover.offsetWidth || 230;
  let left = (btnRect.left - dockRect.left) + btnRect.width / 2 - popW / 2;
  left = Math.max(4, Math.min(left, dockRect.width - popW - 4));
  settingsPopover.style.left = left + 'px';
}

settingsBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  const opening = !settingsPopover.classList.contains('open');
  settingsPopover.classList.toggle('open', opening);
  if (opening) positionSettingsPopover();
});
document.addEventListener('click', (e) => {
  if (!settingsPopover.contains(e.target) && e.target !== settingsBtn) settingsPopover.classList.remove('open');
});

const labelLengthSlider = document.getElementById('labelLengthSlider');
const labelLengthValue = document.getElementById('labelLengthValue');
labelLengthSlider.addEventListener('input', () => {
  ui.labelLength = parseInt(labelLengthSlider.value, 10);
  labelLengthValue.textContent = ui.labelLength;
  scheduleRenderOverlay();
});

const connectorLengthSlider = document.getElementById('connectorLengthSlider');
const connectorLengthValue = document.getElementById('connectorLengthValue');
connectorLengthSlider.addEventListener('input', () => {
  ui.connectorPct = parseInt(connectorLengthSlider.value, 10);
  connectorLengthValue.textContent = ui.connectorPct + '%';
  relayoutAll();
});

document.getElementById('resetLayoutBtn').addEventListener('click', () => {
  relayoutAll();
  network.fit({ animation: true });
});

document.getElementById('toggleTapMarkers').addEventListener('change', (e) => {
  ui.showTapMarkers = e.target.checked;
  scheduleRenderOverlay();
});
document.getElementById('toggleHeadings').addEventListener('change', (e) => {
  ui.showHeadings = e.target.checked;
  scheduleRenderOverlay();
});
document.getElementById('toggleGridBg').addEventListener('change', (e) => {
  container.classList.toggle('no-grid', !e.target.checked);
});
document.getElementById('toggleAutoFit').addEventListener('change', (e) => {
  ui.autoFitOnNewState = e.target.checked;
});

document.getElementById('observeBtn').addEventListener('click', (e) => {
  ui.observeMode = !ui.observeMode;
  graphWrap.classList.toggle('observe-mode', ui.observeMode);
  e.currentTarget.classList.toggle('active', ui.observeMode);
});

// Focus mode: give the canvas the whole window. Browser fullscreen alone no longer does
// that now the rails are part of the same view, so this collapses both and restores
// whatever was open when you come back out.
let focusModeRestore = null;
document.getElementById('fullscreenBtn').addEventListener('click', (e) => {
  if (!focusModeRestore) {
    focusModeRestore = { left: railOpen('left'), right: railOpen('right') };
    setRail('left', false);
    setRail('right', false);
    document.documentElement.requestFullscreen().catch(() => {});
    e.currentTarget.classList.add('active');
  } else {
    setRail('left', focusModeRestore.left);
    setRail('right', focusModeRestore.right);
    focusModeRestore = null;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    e.currentTarget.classList.remove('active');
  }
});

document.getElementById('clearBtn').addEventListener('click', async () => {
  try { await fetch('/clear', { method: 'POST' }); } catch { /* clear locally regardless */ }
  resetGraph();
});

// -- Rails ------------------------------------------------------------------
//
// Replaces both the tab strip and the old live-preview sidebar. There is one view now, so
// "which tab am I on" becomes "which rails are open" — and the phone lives in the agent
// rail rather than in a second preview that polled the same device over the same ADB
// connection.
const RAIL_KEY = 'qa.rails.v1';
const leftSidebar = document.getElementById('leftSidebar');
const rightSidebar = document.getElementById('rightSidebar');
const toggleLeftBtn = document.getElementById('toggleLeftBtn');
const toggleRightBtn = document.getElementById('toggleRightBtn');

function loadRailPrefs() {
  try { return JSON.parse(localStorage.getItem(RAIL_KEY)) || {}; } catch { return {}; }
}
function saveRailPrefs(patch) {
  try {
    localStorage.setItem(RAIL_KEY, JSON.stringify({ ...loadRailPrefs(), ...patch }));
  } catch { /* private mode — the layout just won't persist, which is not worth failing on */ }
}

function setRail(which, open) {
  const rail = which === 'left' ? leftSidebar : rightSidebar;
  const btn = which === 'left' ? toggleLeftBtn : toggleRightBtn;
  rail.classList.toggle('collapsed', !open);
  // `pinned` overrides the responsive rules: asking for a rail on a narrow window should
  // get you the rail, not silently nothing.
  rail.classList.toggle('pinned', open);
  btn.classList.toggle('active', open);
  saveRailPrefs({ [which]: open });
  // vis-network measures its container on resize only; without this the canvas keeps the
  // old width and every overlay lands offset from the node it belongs to.
  requestAnimationFrame(() => { network.redraw(); scheduleRenderOverlay(); });
}

function railOpen(which) {
  return !(which === 'left' ? leftSidebar : rightSidebar).classList.contains('collapsed');
}

toggleLeftBtn.addEventListener('click', () => setRail('left', !railOpen('left')));
// The dock's phone button used to duplicate this. Two controls for one rail meant the
// dock button could read "off" while the rail was open; the top-bar toggle and `]` are now
// the only ways to move it.
toggleRightBtn.addEventListener('click', () => setRail('right', !railOpen('right')));

window.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === '[') { e.preventDefault(); setRail('left', !railOpen('left')); }
  if (e.key === ']') { e.preventDefault(); setRail('right', !railOpen('right')); }
});

// -- Rail resizing --
function wireRailResize(handleId, rail, which, min, max) {
  const handle = document.getElementById(handleId);
  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = rail.getBoundingClientRect().width;
    handle.classList.add('dragging');
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'ew-resize';
    function onMove(ev) {
      // The left rail grows as the pointer moves right; the right rail grows as it moves left.
      const delta = which === 'left' ? ev.clientX - startX : startX - ev.clientX;
      const width = Math.max(min, Math.min(max, startWidth + delta));
      rail.style.width = width + 'px';
    }
    function onUp() {
      handle.classList.remove('dragging');
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      saveRailPrefs({ [which + 'Width']: rail.getBoundingClientRect().width });
      network.redraw();
      scheduleRenderOverlay();
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  });
}

// -- Collapsible panels --
document.querySelectorAll('.panel > .panel-head').forEach((head) => {
  head.addEventListener('click', () => {
    const panel = head.parentElement;
    panel.classList.toggle('open');
    saveRailPrefs({ ['panel.' + panel.dataset.panel]: panel.classList.contains('open') });
  });
});
document.getElementById('phoneBlockToggle').addEventListener('click', () => {
  const block = document.getElementById('phoneBlock');
  block.classList.toggle('open');
  saveRailPrefs({ phoneBlock: block.classList.contains('open') });
  if (block.classList.contains('open')) refreshFrame();
});

// Clicking the project name in the top bar is a shortcut to the panel that changes it.
document.getElementById('projectPill').addEventListener('click', () => {
  if (!railOpen('left')) setRail('left', true);
  const panel = document.querySelector('.panel[data-panel="projects"]');
  panel.classList.add('open');
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
});

function restoreRailPrefs() {
  const prefs = loadRailPrefs();
  setRail('left', prefs.left !== false);      // both rails open on a first visit
  setRail('right', prefs.right !== false);
  if (prefs.leftWidth) leftSidebar.style.width = prefs.leftWidth + 'px';
  if (prefs.rightWidth) rightSidebar.style.width = prefs.rightWidth + 'px';
  document.querySelectorAll('.panel[data-panel]').forEach((panel) => {
    const saved = prefs['panel.' + panel.dataset.panel];
    if (saved !== undefined) panel.classList.toggle('open', saved);
  });
  if (prefs.phoneBlock !== undefined) {
    document.getElementById('phoneBlock').classList.toggle('open', prefs.phoneBlock);
  }
}

// -- Save / Import --
// Moved into main.js's boot sequence: with 63 import cycles in this graph, a call
// at module scope can run while a module it depends on is still initialising.

export { initZoomLabel, RAIL_KEY, connectorLengthSlider, connectorLengthValue, dockEl, focusModeRestore, labelLengthSlider, labelLengthValue, leftSidebar, loadRailPrefs, positionSettingsPopover, railOpen, restoreRailPrefs, rightSidebar, saveRailPrefs, setRail, settingsBtn, settingsPopover, toggleLeftBtn, toggleRightBtn, updateZoomLabel, wireRailResize };
