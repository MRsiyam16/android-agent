// screens.js — extracted verbatim from the old dashboard.js IIFE.

import { escapeHtml } from './markdown.js';
import { screenListEl, screenSearchEl, screensEmptyEl, thumbObserver } from './board.js';
import { openModal } from './modal.js';
import { network, scheduleRenderOverlay } from './render.js';
import { nodeMeta, nodeStatus, nodesData, sectionOrder, sections, ui } from './state.js';
import { CARD_MAX_W, scaledCache } from './util.js';

function focusScreen(hash) {
  if (!nodesData.get(hash)) return;
  network.selectNodes([hash]);
  network.focus(hash, { scale: Math.max(network.getScale(), 0.55), animation: { duration: 380 } });
  ui.selectedIds = new Set([hash]);
  scheduleRenderOverlay();
  renderScreenList();
}

let lastRevealed = null;

function renderScreenList() {
  const needle = ui.screenFilter.trim().toLowerCase();
  screenListEl.querySelectorAll('.screen-row, .screen-section-label').forEach((el) => el.remove());

  let shown = 0;
  sectionOrder.forEach((sectionName) => {
    const hashes = (sections.get(sectionName) || []).filter((hash) => {
      const meta = nodeMeta.get(hash);
      if (!meta) return false;
      if (!needle) return true;
      return [meta.screenName, meta.activity, sectionName, String(meta.screenNumber || '')]
        .filter(Boolean).join(' ').toLowerCase().includes(needle);
    });
    if (!hashes.length) return;

    const label = document.createElement('div');
    label.className = 'screen-section-label';
    label.textContent = sectionName;
    screenListEl.appendChild(label);

    hashes.forEach((hash) => {
      const meta = nodeMeta.get(hash);
      // Same status the canvas colours its connectors by, so a screen carrying a defect
      // is visible in the index without opening it.
      const status = nodeStatus.get(hash);
      const row = document.createElement('div');
      row.className = 'screen-row' + (ui.selectedIds.has(hash) ? ' active' : '')
        + (status?.level === 'fail' ? ' flagged' : '');
      // Starts blank and fills in once the row scrolls into view — see thumbObserver.
      row.innerHTML = `
        <img class="screen-thumb" data-hash="${hash}" alt="">
        <div class="screen-row-main">
          <div class="screen-row-name">${escapeHtml(meta.screenName || 'Screen')}</div>
          <div class="screen-row-sub">${escapeHtml((meta.activity || '').split('.').pop() || '')}</div>
        </div>
        <div class="screen-row-num">${meta.screenNumber ? '#' + meta.screenNumber : ''}</div>`;
      row.addEventListener('click', () => focusScreen(hash));
      row.addEventListener('dblclick', () => openModal(hash));
      screenListEl.appendChild(row);

      const thumb = row.querySelector('.screen-thumb');
      const cachedThumb = scaledCache.get(`${hash}@${CARD_MAX_W}`);
      if (cachedThumb) thumb.src = cachedThumb;
      else if (meta.screenshot) thumbObserver.observe(thumb);
      shown += 1;
    });
  });

  // Reveal the selected screen — but only when the selection actually changed. Doing it on
  // every render would yank the list back during a live run, which re-renders on each
  // telemetry post and would make the panel impossible to read while exploring.
  const selected = ui.selectedIds.size === 1 ? [...selectedIds][0] : null;
  if (selected && selected !== lastRevealed) {
    screenListEl.querySelector('.screen-row.active')
      ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  lastRevealed = selected;

  document.getElementById('screenCount').textContent = nodesData.length;
  screensEmptyEl.classList.toggle('hidden', shown > 0);
  if (!shown && needle) {
    screensEmptyEl.classList.remove('hidden');
    screensEmptyEl.textContent = `No screen matches “${ui.screenFilter}”.`;
  } else if (!shown) {
    screensEmptyEl.textContent =
      'Screens appear here as they are discovered. Click one to centre it on the canvas.';
  }
}

// Deferred: this reads a binding imported from a module that imports this one back,
// so at evaluation time it may still be in its temporal dead zone. main.js calls it.
function initScreenSearch() {
  screenSearchEl.addEventListener('input', () => {
    ui.screenFilter = screenSearchEl.value;
    renderScreenList();
  });
}

// ---------------------------------------------------------------------------
// Modal
// ---------------------------------------------------------------------------
const modalBackdrop = document.getElementById('modalBackdrop');

export { initScreenSearch, focusScreen, lastRevealed, modalBackdrop, renderScreenList };
