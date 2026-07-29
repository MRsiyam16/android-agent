// sectionnotes.js — one note per section, in the gutter to its left.
//
// A section on this board is a test case, and its screens are the states the case passed
// through. What the *steps* were is already written down — a journey step's label becomes
// the screen's name ("15. Send Reset Link with \"not-an-email\" → blocked: …") — but it is
// written down inside the node headers, one line per screen, at a zoom where you can only
// read one of them at a time. Reading a case therefore meant zooming to each screen in turn
// and holding the order in your head.
//
// So the same labels are also printed once, in order, beside the row: the case, what was
// walked, and how it ended. That is the whole feature — no new data, the same run facts
// laid out where they can be read as a sentence rather than a scavenger hunt.
//
// Derived, never authored. A section note has no id, is not saved, and cannot be edited or
// dragged; it is recomputed from `sections`, `nodeMeta` and `nodeStatus` on every frame that
// moves. That is deliberate — a note about a run that can drift out of step with the run is
// worse than no note, and this board already has authored sticky notes (notes.js) for
// anything a person wants to say in their own words. The two are separate layers on purpose.

import { escapeHtml } from './markdown.js';
import { network, nodeDomRect } from './render.js';
import { nodeMeta, nodeStatus, sections, ui } from './state.js';

// Canvas units, so the note scales with the screens instead of sitting at a fixed pixel size
// that swamps the board when you zoom out. CARD_W is 135; a note a little wider than a screen
// reads as a column beside the row rather than a label stuck to it.
const NOTE_W = 200;
const GUTTER = 34;

// Below this the note is a grey smear — the same reasoning as LABEL_VISIBILITY_SCALE in
// render.js, but a lower bar, because a paragraph goes unreadable well before a one-line
// header does.
const NOTE_VISIBILITY_SCALE = 0.5;

// A section is laid out over ceil(n / NODES_PER_ROW) rows with most of a row's gap after it,
// so the note has roughly that much height to work with before it reaches the next section.
// Ten steps is comfortably inside that for every section this board has ever held; past it
// the note says how many it left out rather than growing until it collides, and rather than
// clipping, which would hide the overflow instead of reporting it.
const MAX_STEPS = 12;

// Strip the step number a journey label carries ("15. Send Reset Link…"): the note prints
// its own ordinal, and two numbers on one line reads as a mistake. Only a leading integer
// followed by a dot — a label that genuinely starts with a number keeps it.
//
// Then the case tag, if the step repeats one: labels here read "[L1] Tapped Login with empty
// fields" under a heading that already says L1, and in a column this narrow that prefix costs
// a line of its own. Dropped only when the tag actually matches this section's name, so a
// step that opens with a bracket for its own reasons is left alone.
function stepText(name, section = '') {
  const text = String(name || '').replace(/^\s*\d+\.\s*/, '').trim();
  const tagged = text.match(/^\[([^\]]{1,12})\]\s*(.+)$/);
  if (!tagged) return text;
  const tag = tagged[1].trim().toLowerCase();
  return section.toLowerCase().startsWith(tag) ? tagged[2].trim() : text;
}

// The module prefix is already the heading above the row, so repeating it in the note is
// noise: "auth-login-password-reset / Login — wrong password" becomes "Login — wrong password".
function caseTitle(name) {
  const cut = name.lastIndexOf(' / ');
  return cut === -1 ? name : name.slice(cut + 3);
}

/** The steps of one section, in board order, with anything unnamed dropped. */
function sectionSteps(hashes, section = '') {
  return hashes
    .map((hash) => stepText(nodeMeta.get(hash)?.screenName, section))
    .filter(Boolean);
}

/** The worst outcome filed against any screen in this section, if any. */
function sectionOutcome(hashes) {
  let worst = null;
  hashes.forEach((hash) => {
    const status = nodeStatus.get(hash);
    if (!status) return;
    if (!worst || (status.level === 'fail' && worst.level !== 'fail')) worst = status;
  });
  return worst;
}

function renderSectionNotes() {
  const layer = document.getElementById('sectionNotes');
  layer.innerHTML = '';
  if (!ui.showSectionNotes || ui.observeMode) return;
  const scale = network.getScale();
  if (scale < NOTE_VISIBILITY_SCALE) return;

  sections.forEach((hashes, name) => {
    if (!hashes.length) return;
    // Anchored to the section's first screen rather than to the layout grid: a board that
    // was dragged into a custom arrangement keeps its saved positions, and a note placed
    // from `sectionLayout` would sit where the screens used to be.
    const rect = nodeDomRect(hashes[0]);
    if (!rect) return;

    const steps = sectionSteps(hashes, name);
    if (!steps.length) return;
    const outcome = sectionOutcome(hashes);

    const el = document.createElement('div');
    el.className = 'section-note' + (outcome ? ' has-' + outcome.level : '');
    el.style.width = (NOTE_W * scale) + 'px';
    el.style.left = (rect.left - (NOTE_W + GUTTER) * scale) + 'px';
    el.style.top = rect.top + 'px';
    // One knob for everything inside, so the note keeps its proportions at any zoom the way
    // a screen card does. 11px at scale 1 is the body size the rest of the board uses.
    el.style.fontSize = (11 * scale) + 'px';

    const shown = steps.slice(0, MAX_STEPS);
    const hidden = steps.length - shown.length;
    el.innerHTML =
      `<div class="section-note-title">${escapeHtml(caseTitle(name))}</div>`
      + `<ol class="section-note-steps">`
      + shown.map((s) => `<li>${escapeHtml(s)}</li>`).join('')
      + `</ol>`
      + (hidden
        ? `<div class="section-note-more">+${hidden} more step${hidden === 1 ? '' : 's'} — `
          + `open the screens to read them</div>`
        : '')
      + (outcome
        ? `<div class="section-note-outcome">${escapeHtml(outcome.summary || outcome.badge)}</div>`
        : '');
    layer.appendChild(el);
  });
}

export { NOTE_VISIBILITY_SCALE, NOTE_W, caseTitle, renderSectionNotes, sectionOutcome, sectionSteps, stepText };
