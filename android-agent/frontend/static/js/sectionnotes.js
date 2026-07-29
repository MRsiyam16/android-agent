// sectionnotes.js — the notes the agent pins to the board, one per test case.
//
// A finding is a verdict in a fixed shape — expected, actual, evidence — and it lives in a
// list you open. A note is the agent's own prose about a case, pinned in the gutter beside
// that case's screens, which is where someone looks when they are trying to understand a run
// rather than audit it. Green if the case passed, amber for a warning or a suggestion, red
// for a bug, and the case's connectors take the same colour (see flowLevel, used by
// renderConnectors) so the shape of a run reads from across the room.
//
// Written by the agent through the `add_note` device tool and stored per module beside its
// findings; the page only ever reads them. That is why they are not in `textNotes` with the
// sticky notes you write yourself: the board file is written by the browser's autosave, so a
// note the agent filed with no tab open would be lost, and one filed with a tab open would
// race the autosave for the same file. Yours are authored on the board and saved with it;
// these belong to the run.

import { escapeHtml, renderChatMarkdown } from './markdown.js';
import { network, nodeDomRect } from './render.js';
import { nodeMeta, sections, ui } from './state.js';

// Canvas units, so a note scales with the screens instead of sitting at a fixed pixel size
// that swamps the board when you zoom out. CARD_W is 135; a note a little wider than a
// screen reads as a column beside the row rather than a label stuck to it.
const NOTE_W = 210;
const GUTTER = 34;

// Below this a paragraph is a grey smear. A higher bar than LABEL_VISIBILITY_SCALE in
// render.js, which only has to keep one line of heading legible.
const NOTE_VISIBILITY_SCALE = 0.5;

// The three colours of the whole feature, shared with the connectors. Amber covers both
// warning and suggestion: they differ in what you should do about them, not in how much of
// your attention they deserve on a first read of the board.
const NOTE_LEVELS = {
  bug: { level: 'fail', rank: 3 },
  warning: { level: 'warn', rank: 2 },
  suggestion: { level: 'warn', rank: 1 },
  pass: { level: 'ok', rank: 0 },
};

// section name -> the note pinned to it. Rebuilt wholesale from the server on every load and
// on every agent_note event; never mutated by the page.
const sectionNotes = new Map();

function setSectionNotes(notes) {
  sectionNotes.clear();
  (notes || []).forEach((note) => {
    if (note && note.section) sectionNotes.set(note.section, note);
  });
}

/**
 * The status of the flow a screen belongs to, for colouring its connectors.
 *
 * Deliberately the *case's* level rather than the screen's own. A bug is found on one screen
 * but it is the flow that reached it that a reader wants to trace, and colouring only the
 * last hop leaves a red arrow at the end of a chain of blue ones — which reads as "something
 * went wrong at the last step" when the truth is "this whole case failed".
 */
function flowLevel(hash) {
  const section = nodeMeta.get(hash)?.section;
  if (!section) return null;
  const note = sectionNotes.get(section);
  if (!note) return null;
  const spec = NOTE_LEVELS[note.kind];
  return spec && spec.level !== 'ok' ? spec.level : null;
}

// The module prefix is already the heading above the row, so repeating it in the note is
// noise: "auth-login-password-reset / Login — wrong password" becomes "Login — wrong password".
function caseTitle(name) {
  const cut = name.lastIndexOf(' / ');
  return cut === -1 ? name : name.slice(cut + 3);
}

function renderSectionNotes() {
  const layer = document.getElementById('sectionNotes');
  layer.innerHTML = '';
  if (!ui.showSectionNotes || ui.observeMode || !sectionNotes.size) return;
  const scale = network.getScale();
  if (scale < NOTE_VISIBILITY_SCALE) return;

  sections.forEach((hashes, name) => {
    const note = sectionNotes.get(name);
    if (!note || !hashes.length) return;
    // Anchored to the section's first screen rather than to the layout grid: a board dragged
    // into a custom arrangement keeps its saved positions, and a note placed off the grid
    // would sit where the screens used to be.
    const rect = nodeDomRect(hashes[0]);
    if (!rect) return;

    const spec = NOTE_LEVELS[note.kind] || NOTE_LEVELS.pass;
    const el = document.createElement('div');
    el.className = `section-note level-${spec.level}`;
    el.style.width = (NOTE_W * scale) + 'px';
    el.style.left = (rect.left - (NOTE_W + GUTTER) * scale) + 'px';
    el.style.top = rect.top + 'px';
    // One knob for everything inside. Sizes in the stylesheet are in em, so this single
    // number scales the padding, the rules and the indents together and the note keeps its
    // proportion to the screens beside it.
    el.style.fontSize = (11 * scale) + 'px';

    el.innerHTML =
      `<div class="section-note-head">`
      + `<span class="section-note-kind">${escapeHtml(note.kind)}</span>`
      + `<span class="section-note-title">${escapeHtml(note.title || caseTitle(name))}</span>`
      + `</div>`
      + `<div class="section-note-body markdown">${renderChatMarkdown(note.text || '')}</div>`;
    layer.appendChild(el);
  });
}

export { NOTE_LEVELS, NOTE_VISIBILITY_SCALE, NOTE_W, caseTitle, flowLevel, renderSectionNotes,
         sectionNotes, setSectionNotes };
