// notes.js — extracted verbatim from the old dashboard.js IIFE.

import { scheduleAutoSave } from './board.js';
// Deliberately the *chat* renderer, not renderNoteMarkdown. The two collided in the old
// single-closure file and this is the one that actually won, so importing it keeps notes
// rendering exactly as they do today. See the header of markdown.js.
import { escapeHtml, renderChatMarkdown as renderMarkdown } from './markdown.js';
import { network, scheduleRenderOverlay } from './render.js';
import { textNotes, ui } from './state.js';
import { setTool } from './tools.js';

function addTextNoteAt(domX, domY, kind = 'text') {
  const canvasPos = network.DOMtoCanvas({ x: domX, y: domY });
  const id = 'note-' + Math.random().toString(36).slice(2, 10);
  const isSticky = kind === 'sticky';
  textNotes.set(id, {
    id, kind, cx: canvasPos.x, cy: canvasPos.y,
    text: isSticky ? 'Write in **Markdown**.' : 'Heading',
    fontSize: isSticky ? 10 : 24,
    ...(isSticky ? { title: 'Note', color: 'slate', w: 130 }
                 : { color: 'white', weight: 700 }),
  });
  setTool('pan');
  scheduleRenderOverlay();
  scheduleAutoSave();
  // Drop straight into typing with the formatting menu already open, so a new caption
  // can be styled without hunting for a control first.
  if (!isSticky) {
    // The overlay render is debounced, so the element does not exist yet. Wait for it
    // rather than firing one hopeful rAF — a single frame loses the race and the caption
    // silently opens unfocused, with no menu.
    let tries = 0;
    const grabFocus = () => {
      const el = document.querySelector(`.text-note[data-note-id="${CSS.escape(id)}"]`);
      const content = el && el.querySelector('.text-note-content');
      if (content) {
        content.focus();
        document.execCommand('selectAll', false, null);
      } else if (++tries < 60) {
        requestAnimationFrame(grabFocus);
      }
    };
    requestAnimationFrame(grabFocus);
  }
}

// Header colours are for triage at a glance — red for bugs, amber for flaky, and so on.
// Values come from the design tokens so notes stay in the same palette as the rest of the UI.
const STICKY_COLORS = [
  { key: 'slate',  label: 'Neutral', css: '#dedede',              ink: '#121214' },
  { key: 'red',    label: 'Bug',     css: '#fef08a',              ink: '#713f12' },
  { key: 'orange', label: 'Flaky',   css: '#fed7aa',              ink: '#431407' },
  { key: 'green',  label: 'Passing', css: '#bbf7d0',              ink: '#14532d' },
  { key: 'blue',   label: 'Info',    css: '#a5f3fc',              ink: '#164e63' },
  { key: 'purple', label: 'Idea',    css: '#e9d5ff',              ink: '#581c87' },
];

// -- bare text captions (the T tool) ---------------------------------------
// Ink colours, not fills: a caption sits directly on the dark board, so these are
// chosen to stay legible against it rather than to tint a card.
const TEXT_COLORS = [
  { key: 'white',  label: 'Default', css: '#f4f4f5' },
  { key: 'muted',  label: 'Muted',   css: '#a1a1aa' },
  { key: 'blue',   label: 'Blue',    css: '#38bdf8' },
  { key: 'green',  label: 'Green',   css: '#4ade80' },
  { key: 'amber',  label: 'Amber',   css: '#fbbf24' },
  { key: 'red',    label: 'Red',     css: '#f87171' },
  { key: 'purple', label: 'Purple',  css: '#c084fc' },
];
const TEXT_WEIGHTS = [
  { w: 400, label: 'Regular' }, { w: 600, label: 'Semi' },
  { w: 700, label: 'Bold' }, { w: 800, label: 'Heavy' },
];
// Captions double as board headings, so the range runs from footnote to poster.
const TEXT_SIZE_MIN = 6, TEXT_SIZE_MAX = 200;
const TEXT_SIZE_PRESETS = [12, 16, 24, 32, 48, 72, 120];

function textInk(key) {
  return (TEXT_COLORS.find((c) => c.key === key) || TEXT_COLORS[0]).css;
}
function textSize(note) {
  const n = parseInt(note.fontSize, 10);
  return Math.min(TEXT_SIZE_MAX, Math.max(TEXT_SIZE_MIN, Number.isFinite(n) ? n : 24));
}
function textContentStyle(note) {
  return `font-size:${textSize(note)}px;color:${textInk(note.color)};`
       + `font-weight:${note.weight || 400};line-height:1.15;`;
}

// Notes are laid out at 2x and scaled back down.
const STICKY_DESIGN_SCALE = 2;
const STICKY_WIDTHS = [
  { key: 'mini',   label: 'Mini',   w: 110 },
  { key: 'small',  label: 'Small',  w: 130 },
  { key: 'medium', label: 'Medium', w: 160 },
  { key: 'large',  label: 'Large',  w: 220 },
];
const STICKY_MIN_W = 80, STICKY_MIN_H = 60;
const stickyColor = (key) => STICKY_COLORS.find((c) => c.key === key) || STICKY_COLORS[0];
const stickyW = (note) => {
  if (typeof note.w === 'number') return note.w;
  const found = STICKY_WIDTHS.find((w) => w.key === note.width);
  return found ? found.w : 130;
};

// Small Markdown subset, rendered locally rather than pulling in a library: the dashboard
// is used against devices on isolated networks, so a CDN script would simply fail there.
// The source is HTML-escaped first, so note text can never inject markup.
function renderTextNotes() {
  const layer = document.getElementById('textNotesLayer');

  // Rebuilding the layer while a note is being typed into would destroy the caret, and
  // renderOverlay runs on every pan/zoom frame. When something in here has focus, move
  // the existing elements instead of recreating them.
  if (layer.contains(document.activeElement)) {
    layer.querySelectorAll('.text-note').forEach((el) => {
      const note = textNotes.get(el.dataset.noteId);
      if (!note) return;
      const pos = network.canvasToDOM({ x: note.cx, y: note.cy });
      el.style.left = pos.x + 'px';
      el.style.top = pos.y + 'px';
      // Restyle in place as well as reposition. The formatting menu lives inside this
      // layer, so dragging its size slider puts focus *here* and takes this branch —
      // without this the caption would not change until focus left the slider, which
      // reads as a dead control.
      const content = note.kind !== 'sticky' && el.querySelector('.text-note-content');
      if (content) {
        content.setAttribute('style', textContentStyle(note));
        el.style.transformOrigin = 'top left';
        el.style.transform = `scale(${network.getScale()})`;
      }
    });
    return;
  }

  // A full rebuild only happens when focus is outside this layer — i.e. nothing is being
  // edited — so any open formatting menu is about to be destroyed with the rest of the
  // layer. Close it properly instead of leaving `textMenuState` pointing at a dead node.
  closeTextFormatMenu();
  layer.innerHTML = '';
  textNotes.forEach((note) => {
    const pos = network.canvasToDOM({ x: note.cx, y: note.cy });
    const el = document.createElement('div');
    const isSticky = note.kind === 'sticky';
    el.className = 'text-note kind-' + (isSticky ? 'sticky' : 'text')
      + (ui.selectedIds.has(note.id) ? ' selected' : '');
    el.dataset.noteId = note.id;
    el.style.left = pos.x + 'px';
    el.style.top = pos.y + 'px';

    if (isSticky) {
      const color = stickyColor(note.color);
      // Lay the note out at its canvas size, then scale the whole element by the current
      // zoom. Everything inside — text, padding, header, buttons — scales together, so a
      // note keeps its proportion to the screens instead of being a fixed screen size.
      el.style.width = (stickyW(note) * STICKY_DESIGN_SCALE) + 'px';
      if (note.h) el.style.height = (note.h * STICKY_DESIGN_SCALE) + 'px';
      el.style.transformOrigin = 'top left';
      el.style.transform = `scale(${network.getScale() / STICKY_DESIGN_SCALE})`;
      el.innerHTML = `
        <div class="sticky-head" style="background:${color.css};color:${color.ink};">
          <div class="sticky-title" contenteditable="true" spellcheck="false"
               style="font-size:${(note.fontSize || 12) * STICKY_DESIGN_SCALE}px;">${escapeHtml(note.title || 'Note Title')}</div>
          <div class="sticky-actions">
            <div class="sticky-btn sticky-menu-btn" title="Note settings">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="4" y1="7" x2="20" y2="7"></line><line x1="4" y1="12" x2="20" y2="12"></line><line x1="4" y1="17" x2="20" y2="17"></line></svg>
            </div>
            <div class="sticky-btn sticky-close" title="Delete note">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
            </div>
          </div>
        </div>
        <div class="sticky-body markdown" contenteditable="false" style="font-size:${(note.fontSize || 12) * STICKY_DESIGN_SCALE}px;">${renderMarkdown(note.text)}</div>
        <div class="sticky-resize" title="Drag to resize"></div>
      `;

      const title = el.querySelector('.sticky-title');
      title.addEventListener('blur', (e) => { note.title = e.target.innerText.trim() || 'Note'; scheduleAutoSave(); });
      title.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); } });

      const body = el.querySelector('.sticky-body');
      // Editing is opt-in via double-click. A single click has to stay harmless: the
      // body is what you click to read, select or drag a note past, and when a single
      // click armed the editor every stray click swapped rendered Markdown for raw
      // source — and then round-tripped it back through the save.
      body.addEventListener('dblclick', () => {
        if (body.isContentEditable) return;
        body.setAttribute('contenteditable', 'true');
        body.classList.add('editing');   // explicit class, not :focus — see dashboard.css
        body.textContent = note.text;
        body.focus();
      });
      body.addEventListener('blur', () => {
        if (!body.isContentEditable) return;
        // textContent, NEVER innerText. innerText is computed from the *rendered*
        // layout, so it returns whatever the current white-space setting produced —
        // by blur time the editing style is already going away, and every newline in
        // the note came back collapsed to a space. One click in and out permanently
        // flattened a note's Markdown and auto-saved the damage. textContent returns
        // the literal characters and is immune to all of that.
        note.text = body.textContent;
        body.setAttribute('contenteditable', 'false');
        body.classList.remove('editing');
        body.innerHTML = renderMarkdown(note.text);
        scheduleAutoSave();
      });

      el.querySelector('.sticky-close').addEventListener('click', () => {
        textNotes.delete(note.id); scheduleRenderOverlay(); scheduleAutoSave();
      });
      el.querySelector('.sticky-menu-btn').addEventListener('mousedown', (e) => {
        e.stopPropagation(); e.preventDefault(); openNoteMenu(note, e.currentTarget);
      });
      // The header doubles as the tab bar you drag the note by (holding mouse down anywhere on tab bar).
      el.querySelector('.sticky-head').addEventListener('mousedown', (e) => {
        if (e.target.closest('.sticky-btn')) return;
        const title = e.target.closest('.sticky-title');
        startNoteDrag(note, e, title);
      });
      el.querySelector('.sticky-resize').addEventListener('mousedown', (e) => {
        startNoteResize(note, el, e);
      });
    } else {
      // Font size is a *canvas* measurement, like a sticky's width: the element is laid
      // out at its true size and then scaled by the zoom. Without this a 120px heading
      // would still be 120 screen pixels at 13% zoom, dwarfing the whole board.
      el.style.transformOrigin = 'top left';
      el.style.transform = `scale(${network.getScale()})`;
      el.innerHTML = `
        <div class="text-note-drag">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><circle cx="8" cy="6" r="1.6"></circle><circle cx="8" cy="12" r="1.6"></circle><circle cx="8" cy="18" r="1.6"></circle><circle cx="16" cy="6" r="1.6"></circle><circle cx="16" cy="12" r="1.6"></circle><circle cx="16" cy="18" r="1.6"></circle></svg>
        </div>
        <div class="text-note-content" contenteditable="true" spellcheck="false"
             style="${textContentStyle(note)}">${escapeHtml(note.text)}</div>
        <div class="text-note-delete">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </div>
      `;
      const content = el.querySelector('.text-note-content');
      content.addEventListener('blur', (e) => { note.text = e.target.innerText; scheduleAutoSave(); });
      // Entering the box is what opens the formatting menu — no extra affordance to find.
      content.addEventListener('focus', () => openTextFormatMenu(note, el));
      content.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { e.preventDefault(); closeTextFormatMenu(); content.blur(); }
      });
      el.querySelector('.text-note-delete').addEventListener('click', () => { textNotes.delete(note.id); closeTextFormatMenu(); scheduleRenderOverlay(); scheduleAutoSave(); });
      el.querySelector('.text-note-drag').addEventListener('mousedown', (e) => startNoteDrag(note, e));
    }

    layer.appendChild(el);
  });
}

// -- caption formatting menu -----------------------------------------------
// Opens on focus of a text note. It must survive the note being re-rendered and must
// not steal focus from the caption, so every control commits on `input`/`click` and
// the menu closes only on a mousedown that lands outside both it and its note.
let textMenuState = null;

function closeTextFormatMenu() {
  if (!textMenuState) return;
  document.removeEventListener('mousedown', textMenuState.onDocDown, true);
  textMenuState.menu.remove();
  textMenuState = null;
}

function openTextFormatMenu(note, noteEl) {
  if (textMenuState && textMenuState.noteId === note.id) return;   // already open
  closeTextFormatMenu();

  const layer = document.getElementById('textNotesLayer');
  const layerRect = layer.getBoundingClientRect();
  const r = noteEl.getBoundingClientRect();

  const menu = document.createElement('div');
  menu.className = 'note-menu text-format-menu';
  menu.style.left = Math.max(4, r.left - layerRect.left) + 'px';
  menu.style.top = (r.bottom - layerRect.top + 8) + 'px';
  const size = textSize(note);
  menu.innerHTML = `
    <div class="note-menu-label">Colour</div>
    <div class="note-menu-swatches">
      ${TEXT_COLORS.map((c) => `
        <div class="note-swatch${(note.color || 'white') === c.key ? ' active' : ''}"
             data-tcolor="${c.key}" title="${c.label}" style="background:${c.css}"></div>`).join('')}
    </div>

    <div class="note-menu-label">Weight</div>
    <div class="note-menu-row">
      ${TEXT_WEIGHTS.map((w) => `
        <div class="note-menu-chip${(note.weight || 400) === w.w ? ' active' : ''}"
             data-tweight="${w.w}" style="font-weight:${w.w};">${w.label}</div>`).join('')}
    </div>

    <div class="note-menu-label" style="display:flex;justify-content:space-between;align-items:center;">
      <span>Text size</span>
      <span class="note-val-label" id="textSizeVal">${size}px</span>
    </div>
    <div class="note-menu-row">
      ${TEXT_SIZE_PRESETS.map((s) => `
        <div class="note-menu-chip${size === s ? ' active' : ''}" data-tsize="${s}">${s}</div>`).join('')}
    </div>
    <div class="note-slider-container">
      <input type="range" class="note-slider" id="textSizeSlider"
             min="${TEXT_SIZE_MIN}" max="${TEXT_SIZE_MAX}" step="1" value="${size}">
    </div>
    <div class="note-menu-hint">Esc to finish · Del removes the selected note</div>
  `;

  // Keep the caret in the caption when the menu is clicked, so typing can continue.
  menu.addEventListener('mousedown', (e) => {
    if (e.target.tagName !== 'INPUT') e.preventDefault();
    e.stopPropagation();
  });

  const sizeVal = menu.querySelector('#textSizeVal');
  const slider = menu.querySelector('#textSizeSlider');

  const applySize = (val) => {
    note.fontSize = Math.min(TEXT_SIZE_MAX, Math.max(TEXT_SIZE_MIN, val));
    sizeVal.textContent = note.fontSize + 'px';
    menu.querySelectorAll('[data-tsize]').forEach((c) =>
      c.classList.toggle('active', parseInt(c.dataset.tsize, 10) === note.fontSize));
    restyleNote(note);
    scheduleAutoSave();
  };

  slider.addEventListener('input', (e) => applySize(parseInt(e.target.value, 10)));

  menu.addEventListener('click', (e) => {
    const sw = e.target.closest('[data-tcolor]');
    const wt = e.target.closest('[data-tweight]');
    const sz = e.target.closest('[data-tsize]');
    if (sw) {
      note.color = sw.dataset.tcolor;
      menu.querySelectorAll('[data-tcolor]').forEach((s) =>
        s.classList.toggle('active', s.dataset.tcolor === note.color));
      restyleNote(note); scheduleAutoSave();
    } else if (wt) {
      note.weight = parseInt(wt.dataset.tweight, 10);
      menu.querySelectorAll('[data-tweight]').forEach((c) =>
        c.classList.toggle('active', parseInt(c.dataset.tweight, 10) === note.weight));
      restyleNote(note); scheduleAutoSave();
    } else if (sz) {
      slider.value = sz.dataset.tsize;
      applySize(parseInt(sz.dataset.tsize, 10));
    }
  });

  const onDocDown = (ev) => {
    const el = document.querySelector(`.text-note[data-note-id="${CSS.escape(note.id)}"]`);
    if (menu.contains(ev.target) || (el && el.contains(ev.target))) return;
    closeTextFormatMenu();
  };
  document.addEventListener('mousedown', onDocDown, true);

  layer.appendChild(menu);
  textMenuState = { noteId: note.id, menu, onDocDown };
}

// Apply style to the live element without a full re-render, which would blow away the
// caret mid-edit.
function restyleNote(note) {
  const el = document.querySelector(`.text-note[data-note-id="${CSS.escape(note.id)}"]`);
  const content = el && el.querySelector('.text-note-content');
  if (content) content.setAttribute('style', textContentStyle(note));
}

function openNoteMenu(note, anchorEl) {
  document.querySelectorAll('.note-menu').forEach((m) => m.remove());
  const rect = anchorEl.getBoundingClientRect();
  const layerRect = document.getElementById('textNotesLayer').getBoundingClientRect();

  const currentW = Math.round(stickyW(note));
  const currentSize = note.fontSize || 10;

  const menu = document.createElement('div');
  menu.className = 'note-menu';
  menu.style.left = (rect.left - layerRect.left) + 'px';
  menu.style.top = (rect.bottom - layerRect.top + 6) + 'px';
  menu.innerHTML = `
    <div class="note-menu-label">Colour</div>
    <div class="note-menu-swatches">
      ${STICKY_COLORS.map((c) => `
        <div class="note-swatch${(note.color || 'slate') === c.key ? ' active' : ''}"
             data-color="${c.key}" title="${c.label}" style="background:${c.css}"></div>`).join('')}
    </div>

    <div class="note-menu-label" style="display:flex;justify-content:space-between;align-items:center;">
      <span>Width</span>
      <span class="note-val-label" id="noteWidthVal">${currentW}px</span>
    </div>
    <div class="note-menu-row">
      ${STICKY_WIDTHS.map((w) => `
        <div class="note-menu-chip${currentW === w.w ? ' active' : ''}"
             data-width="${w.key}">${w.label}</div>`).join('')}
    </div>
    <div class="note-slider-container">
      <input type="range" class="note-slider" id="noteWidthSlider" min="80" max="260" step="5" value="${currentW}">
    </div>

    <div class="note-menu-label" style="display:flex;justify-content:space-between;align-items:center;margin-top:4px;">
      <span>Text size</span>
      <span class="note-val-label" id="noteSizeVal">${currentSize}px</span>
    </div>
    <div class="note-menu-row">
      ${[6, 8, 9, 10, 11, 12, 14].map((s) => `
        <div class="note-menu-chip${currentSize === s ? ' active' : ''}"
             data-size="${s}">${s}px</div>`).join('')}
    </div>
    <div class="note-slider-container">
      <input type="range" class="note-slider" id="noteSizeSlider" min="6" max="16" step="1" value="${currentSize}">
    </div>
  `;

  menu.addEventListener('mousedown', (e) => e.stopPropagation());

  const widthSlider = menu.querySelector('#noteWidthSlider');
  const widthVal = menu.querySelector('#noteWidthVal');
  const sizeSlider = menu.querySelector('#noteSizeSlider');
  const sizeVal = menu.querySelector('#noteSizeVal');

  widthSlider.addEventListener('input', (e) => {
    const val = parseInt(e.target.value, 10);
    note.w = val;
    delete note.width;
    delete note.h;
    widthVal.textContent = val + 'px';
    scheduleRenderOverlay();
    scheduleAutoSave();
  });

  sizeSlider.addEventListener('input', (e) => {
    const val = parseInt(e.target.value, 10);
    note.fontSize = val;
    sizeVal.textContent = val + 'px';
    scheduleRenderOverlay();
    scheduleAutoSave();
  });

  menu.addEventListener('click', (e) => {
    const swatch = e.target.closest('[data-color]');
    const widthChip = e.target.closest('[data-width]');
    const sizeChip = e.target.closest('[data-size]');

    if (swatch) {
      note.color = swatch.dataset.color;
      menu.remove();
      scheduleRenderOverlay();
      scheduleAutoSave();
    } else if (widthChip) {
      const preset = STICKY_WIDTHS.find((w) => w.key === widthChip.dataset.width);
      if (preset) {
        note.w = preset.w;
        delete note.width;
        delete note.h;
        widthSlider.value = preset.w;
        widthVal.textContent = preset.w + 'px';
        scheduleRenderOverlay();
        scheduleAutoSave();
      }
    } else if (sizeChip) {
      const sz = parseInt(sizeChip.dataset.size, 10);
      note.fontSize = sz;
      sizeSlider.value = sz;
      sizeVal.textContent = sz + 'px';
      scheduleRenderOverlay();
      scheduleAutoSave();
    }
  });

  document.getElementById('textNotesLayer').appendChild(menu);
  setTimeout(() => {
    const close = (ev) => {
      if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener('mousedown', close); }
    };
    document.addEventListener('mousedown', close);
  }, 0);
}

function startNoteResize(note, el, e) {
  e.preventDefault();
  e.stopPropagation();
  const startX = e.clientX, startY = e.clientY;
  const scale = network.getScale();
  const startW = stickyW(note);
  const startH = note.h || el.getBoundingClientRect().height / scale;

  function onMove(ev) {
    // Pointer travel is in screen pixels; the note's size is in canvas units.
    note.w = Math.max(STICKY_MIN_W, startW + (ev.clientX - startX) / scale);
    note.h = Math.max(STICKY_MIN_H, startH + (ev.clientY - startY) / scale);
    delete note.width;   // an explicit size supersedes any preset
    scheduleRenderOverlay();
  }
  function onUp() {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
    scheduleAutoSave();
  }
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}

function startNoteDrag(note, e, targetTitle = null) {
  e.stopPropagation();
  const startClientX = e.clientX, startClientY = e.clientY;
  const startCx = note.cx, startCy = note.cy;
  let moved = false;

  function onMove(ev) {
    const dx = ev.clientX - startClientX;
    const dy = ev.clientY - startClientY;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
      moved = true;
      if (targetTitle && document.activeElement === targetTitle) {
        targetTitle.blur();
      }
    }
    if (moved) {
      ev.preventDefault();
      const scale = network.getScale();
      note.cx = startCx + dx / scale;
      note.cy = startCy + dy / scale;
      scheduleRenderOverlay();
    }
  }
  function onUp() {
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', onUp);
    if (moved) scheduleAutoSave();
  }
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
}

// ---------------------------------------------------------------------------

export { STICKY_COLORS, STICKY_DESIGN_SCALE, STICKY_MIN_W, STICKY_WIDTHS, TEXT_COLORS, TEXT_SIZE_MIN, TEXT_SIZE_PRESETS, TEXT_WEIGHTS, addTextNoteAt, closeTextFormatMenu, openNoteMenu, openTextFormatMenu, renderTextNotes, restyleNote, startNoteDrag, startNoteResize, stickyColor, stickyW, textContentStyle, textInk, textMenuState, textSize };
