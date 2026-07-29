// tools.js — extracted verbatim from the old dashboard.js IIFE.

import { scheduleAutoSave } from './board.js';
import { openCommentComposer } from './comments.js';
import { addTextNoteAt, closeTextFormatMenu } from './notes.js';
import { graphWrap, hitTestNode, network, nodeDomRect, scheduleRenderOverlay, selectionBox, toolOverlay } from './render.js';
import { renderScreenList } from './screens.js';
import { nodesData, textNotes, ui } from './state.js';

function setTool(tool) {
  ui.currentTool = tool;
  document.querySelectorAll('.dock-btn[data-tool]').forEach((b) => b.classList.toggle('active', b.dataset.tool === tool));
  graphWrap.dataset.tool = tool;
  if (tool !== 'select') { ui.selectedIds = new Set(); scheduleRenderOverlay(); }
}
document.querySelectorAll('.dock-btn[data-tool]').forEach((btn) => {
  btn.addEventListener('click', () => setTool(btn.dataset.tool));
});

window.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) return;
  // Delete removes the selected notes. Guarded by the check above, so it can never fire
  // while a caption is being typed into. Graph screens are deliberately left alone —
  // they come from telemetry and would simply reappear on the next run.
  if (e.key === 'Delete' || e.key === 'Backspace') {
    const doomed = [...ui.selectedIds].filter((id) => textNotes.has(id));
    if (doomed.length) {
      e.preventDefault();
      doomed.forEach((id) => textNotes.delete(id));
      ui.selectedIds = new Set([...ui.selectedIds].filter((id) => !doomed.includes(id)));
      closeTextFormatMenu();
      scheduleRenderOverlay();
      scheduleAutoSave();
      return;
    }
  }
  if (e.key === 'v' || e.key === 'V') setTool('pan');
  if (e.key === 's' || e.key === 'S') setTool('select');
  if (e.key === 't' || e.key === 'T') setTool('text');
  if (e.key === 'n' || e.key === 'N') setTool('sticky');
  if (e.key === 'c' || e.key === 'C') setTool('comment');
});

let dragState = null;

// -- selection helpers, shared by graph nodes and notes ---------------------
// `selectedIds` holds both kinds; `textNotes.has(id)` is what tells them apart. A note's
// position lives on the note itself (cx/cy, canvas coords) rather than in vis.js, so a
// group drag has to move each kind through its own channel.
function noteDomRect(id) {
  const el = document.querySelector(`.text-note[data-note-id="${CSS.escape(id)}"]`);
  if (!el) return null;
  const base = toolOverlay.getBoundingClientRect();
  const r = el.getBoundingClientRect();
  return { left: r.left - base.left, top: r.top - base.top, width: r.width, height: r.height };
}

function hitTestNote(x, y) {
  const els = Array.from(document.querySelectorAll('#textNotesLayer .text-note'));
  // Last painted wins, matching what sits visually on top.
  for (let i = els.length - 1; i >= 0; i--) {
    const id = els[i].dataset.noteId;
    const r = noteDomRect(id);
    if (r && x >= r.left && x <= r.left + r.width && y >= r.top && y <= r.top + r.height) return id;
  }
  return null;
}

function beginGroupDrag(x, y) {
  const origins = new Map();
  ui.selectedIds.forEach((id) => {
    const note = textNotes.get(id);
    if (note) { origins.set(id, { x: note.cx, y: note.cy, isNote: true }); return; }
    const pos = network.getPositions([id])[id];
    if (pos) origins.set(id, { x: pos.x, y: pos.y, isNote: false });
  });
  dragState = { mode: 'group', startX: x, startY: y, origins, movedNote: false };
}

// Notes sit above the tool overlay (z-index 20 vs 15), so with the select tool active a
// mousedown on a note never reaches the overlay at all — which is why neither the
// marquee nor the move tool could pick a note up, and why grabbing a sticky's header
// dragged that one note instead of the selection. Intercept in the capture phase and
// route it through the same path the graph nodes use.
document.getElementById('textNotesLayer').addEventListener('mousedown', (e) => {
  if (ui.currentTool !== 'select') return;
  const noteEl = e.target.closest('.text-note');
  if (!noteEl) return;
  e.preventDefault();
  e.stopPropagation();
  const base = toolOverlay.getBoundingClientRect();
  const x = e.clientX - base.left, y = e.clientY - base.top;
  const id = noteEl.dataset.noteId;
  if (!ui.selectedIds.has(id)) { ui.selectedIds = new Set([id]); scheduleRenderOverlay(); }
  beginGroupDrag(x, y);
}, true);

// Deferred: reads a binding imported from a module that imports this one back, so at
// evaluation time it may still be in its temporal dead zone. main.js calls it.
function initToolOverlay() {
  toolOverlay.addEventListener('mousedown', (e) => {
    const rect = toolOverlay.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;

    if (ui.currentTool === 'select') {
      // Notes are hit-tested first: they render above the graph, so at a point covering
      // both, the note is what the user sees and is aiming at.
      const hitId = hitTestNote(x, y) || hitTestNode(x, y);
      if (hitId) {
        if (!ui.selectedIds.has(hitId)) {
          ui.selectedIds = new Set([hitId]);
          scheduleRenderOverlay();
          // Keep the Screens index in step with the canvas: selecting a card should show you
          // where it sits in the list, not leave the two views disagreeing.
          renderScreenList();
        }
        beginGroupDrag(x, y);
      } else {
        dragState = { mode: 'marquee', startX: x, startY: y };
        selectionBox.classList.add('visible');
        Object.assign(selectionBox.style, { left: x + 'px', top: y + 'px', width: '0px', height: '0px' });
      }
    } else if (ui.currentTool === 'text') {
      addTextNoteAt(x, y, 'text');
    } else if (ui.currentTool === 'sticky') {
      addTextNoteAt(x, y, 'sticky');
    } else if (ui.currentTool === 'comment') {
      const hitId = hitTestNode(x, y);
      if (hitId) openCommentComposer(hitId, x, y);
    }
  });
}

window.addEventListener('mousemove', (e) => {
  if (!dragState) return;
  const rect = toolOverlay.getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;

  if (dragState.mode === 'marquee') {
    const left = Math.min(x, dragState.startX), top = Math.min(y, dragState.startY);
    const w = Math.abs(x - dragState.startX), h = Math.abs(y - dragState.startY);
    Object.assign(selectionBox.style, { left: left + 'px', top: top + 'px', width: w + 'px', height: h + 'px' });
  } else if (dragState.mode === 'group') {
    const scale = network.getScale();
    const dx = (x - dragState.startX) / scale, dy = (y - dragState.startY) / scale;
    const updates = [];
    dragState.origins.forEach((orig, id) => {
      if (orig.isNote) {
        const note = textNotes.get(id);
        if (note) { note.cx = orig.x + dx; note.cy = orig.y + dy; dragState.movedNote = true; }
      } else {
        updates.push({ id, x: orig.x + dx, y: orig.y + dy });
      }
    });
    if (updates.length) nodesData.update(updates);
    scheduleRenderOverlay();
  }
});

window.addEventListener('mouseup', () => {
  if (!dragState) return;
  if (dragState.mode === 'marquee') {
    selectionBox.classList.remove('visible');
    const bx1 = parseFloat(selectionBox.style.left), by1 = parseFloat(selectionBox.style.top);
    const bx2 = bx1 + parseFloat(selectionBox.style.width), by2 = by1 + parseFloat(selectionBox.style.height);
    const newSel = new Set();
    const hits = (r) => r && !(r.left > bx2 || r.left + r.width < bx1
                               || r.top > by2 || r.top + r.height < by1);
    nodesData.forEach((n) => { if (hits(nodeDomRect(n.id))) newSel.add(n.id); });
    // Notes are part of the board, so a marquee has to sweep them up too.
    textNotes.forEach((note) => { if (hits(noteDomRect(note.id))) newSel.add(note.id); });
    ui.selectedIds = newSel;
    scheduleRenderOverlay();
  }
  // A note's position is only in memory until something saves it; node positions are
  // re-derived from the layout, so only a moved note needs persisting.
  if (dragState.mode === 'group' && dragState.movedNote) scheduleAutoSave();
  dragState = null;
});

// ---------------------------------------------------------------------------
// Text notes
// ---------------------------------------------------------------------------
// kind 'text' = a bare caption on the board (the T tool); kind 'sticky' = a note card
// sized to match a screen (the N tool). Legacy notes saved before stickies existed have
// no kind and were bare captions, so 'text' is the right default for them.
// Moved into main.js's boot sequence: with 63 import cycles in this graph, a call
// at module scope can run while a module it depends on is still initialising.

export { initToolOverlay, beginGroupDrag, dragState, hitTestNote, noteDomRect, setTool };
