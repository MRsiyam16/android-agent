// comments.js — extracted verbatim from the old dashboard.js IIFE.

import { scheduleAutoSave } from './board.js';
import { anchorPoint, nodeDomRect, scheduleRenderOverlay } from './render.js';
import { comments } from './state.js';

// Comment pins
// ---------------------------------------------------------------------------
function openCommentComposer(hash, domX, domY, existingId) {
  document.querySelectorAll('.comment-popover').forEach((p) => p.remove());
  const existing = existingId ? comments.get(existingId) : null;

  const popover = document.createElement('div');
  popover.className = 'comment-popover';
  popover.style.left = domX + 'px';
  popover.style.top = domY + 'px';
  popover.innerHTML = `
    <textarea placeholder="Add a note about this spot…">${existing ? escapeHtml(existing.text) : ''}</textarea>
    <div class="comment-popover-actions">
      ${existing ? '<button class="danger" data-act="delete">Delete</button>' : ''}
      <button data-act="cancel">Cancel</button>
      <button class="primary" data-act="save">Save</button>
    </div>
  `;
  document.getElementById('commentsLayer').appendChild(popover);
  const textarea = popover.querySelector('textarea');
  textarea.focus();

  popover.querySelector('[data-act="cancel"]').addEventListener('click', () => popover.remove());
  const deleteBtn = popover.querySelector('[data-act="delete"]');
  if (deleteBtn) deleteBtn.addEventListener('click', () => { comments.delete(existingId); popover.remove(); scheduleRenderOverlay(); scheduleAutoSave(); });
  popover.querySelector('[data-act="save"]').addEventListener('click', () => {
    const text = textarea.value.trim();
    if (!text) { popover.remove(); return; }
    if (existing) {
      existing.text = text;
    } else {
      const rect = nodeDomRect(hash);
      const fracX = rect ? (domX - rect.left) / rect.width : 0.5;
      const fracY = rect ? (domY - rect.top) / rect.height : 0.5;
      const id = 'comment-' + Math.random().toString(36).slice(2, 10);
      comments.set(id, { id, hash, fracX, fracY, text });
    }
    popover.remove();
    scheduleRenderOverlay();
    scheduleAutoSave();
  });
}

function renderComments() {
  const layer = document.getElementById('commentsLayer');
  layer.querySelectorAll('.comment-pin').forEach((p) => p.remove());
  comments.forEach((c) => {
    const rect = nodeDomRect(c.hash);
    if (!rect) return;
    const pos = anchorPoint(rect, { x: c.fracX, y: c.fracY });
    const pin = document.createElement('div');
    pin.className = 'comment-pin';
    pin.style.left = pos.x + 'px';
    pin.style.top = pos.y + 'px';
    pin.textContent = '💬';
    pin.title = c.text;
    pin.addEventListener('click', (e) => { e.stopPropagation(); openCommentComposer(c.hash, pos.x, pos.y, c.id); });
    layer.appendChild(pin);
  });
}

// ---------------------------------------------------------------------------
// Telemetry ingestion
// ---------------------------------------------------------------------------
const emptyState = document.getElementById('emptyState');


export { emptyState, openCommentComposer, renderComments };
