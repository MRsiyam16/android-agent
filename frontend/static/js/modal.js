// modal.js — extracted verbatim from the old dashboard.js IIFE.

import { escapeHtml } from './markdown.js';
import { modalBackdrop } from './screens.js';
import { PLACEHOLDER_IMG, nodeMeta } from './state.js';
import { screenshotSrc } from './util.js';

function openModal(hash) {
  const meta = nodeMeta.get(hash);
  if (!meta) return;
  document.getElementById('modalImage').src = screenshotSrc(meta.screenshot) || PLACEHOLDER_IMG;
  document.getElementById('modalPackage').textContent = meta.package || 'Unknown package';
  document.getElementById('modalActivity').textContent = meta.activity || '';
  document.getElementById('modalScreenName').textContent = meta.screenNumber
    ? `#${meta.screenNumber} ${meta.screenName || ''}`
    : (meta.screenName || '—');
  document.getElementById('modalHash').textContent = hash;
  document.getElementById('modalElementCount').textContent = (meta.elements || []).length;
  document.getElementById('modalAction').textContent = meta.action ? ('click: ' + meta.action.label) : 'Entry / start state';

  const list = document.getElementById('modalElements');
  list.innerHTML = '';
  (meta.elements || []).slice(0, 40).forEach((el) => {
    const row = document.createElement('div');
    row.className = 'modal-element-row';
    row.innerHTML = `<span class="lbl">${escapeHtml(el.label || '')}</span><span class="coord">${el.x}, ${el.y}</span>`;
    list.appendChild(row);
  });

  modalBackdrop.classList.add('open');
}

// Deferred: this reads a binding imported from a module that imports this one back,
// so at evaluation time it may still be in its temporal dead zone. main.js calls it.
function initModal() {
  document.getElementById('modalClose').addEventListener('click', () => modalBackdrop.classList.remove('open'));
  modalBackdrop.addEventListener('click', (e) => { if (e.target === modalBackdrop) modalBackdrop.classList.remove('open'); });
}

// ---------------------------------------------------------------------------

export { initModal, openModal };
