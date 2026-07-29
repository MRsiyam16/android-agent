// sections.js — extracted verbatim from the old dashboard.js IIFE.

import { nodeMeta, sectionOrder, sections } from './state.js';

function nodeHeaderLabel(hash) {
  const meta = nodeMeta.get(hash);
  if (!meta) return hash.slice(0, 8);
  if (meta.screenName) return (meta.screenNumber ? `#${meta.screenNumber} ` : '') + meta.screenName;
  const base = meta.activity ? meta.activity.split('.').pop() : (meta.package || '').split('.').pop();
  return base || hash.slice(0, 8);
}

// ---------------------------------------------------------------------------
// Section classification
// ---------------------------------------------------------------------------
const GENERIC_LABELS = new Set([
  'relativelayout', 'framelayout', 'linearlayout', 'view', 'imageview',
  'textview', 'button', 'edittext', 'calculator input field', 'result preview',
  'unlabeled element',
]);

function isSectionTrigger(label) {
  if (!label) return false;
  const norm = label.trim().toLowerCase();
  if (GENERIC_LABELS.has(norm)) return false;
  if (/^[\d+\-×÷=%.()]+$/.test(norm)) return false;
  if (norm.length <= 1) return false;
  return /^[a-zA-Z][a-zA-Z\s]*$/.test(label.trim());
}

function assignSection(hash, sectionName) {
  if (!sections.has(sectionName)) {
    sections.set(sectionName, []);
    sectionOrder.push(sectionName);
  }
  const list = sections.get(sectionName);
  if (!list.includes(hash)) list.push(hash);
  const meta = nodeMeta.get(hash);
  if (meta) meta.section = sectionName;
}

// ---------------------------------------------------------------------------
// Layout — one row per section, wrapping within a section if it grows too wide.
// ---------------------------------------------------------------------------
// A card is a screen thumbnail, so its box has to carry the *screenshot's* aspect ratio.
// It was a flat 150x300 (1:2) while every device this harness has tested is 9:20 —
// 720x1600 and 1080x2400 both — and `object-fit: cover` settles that mismatch by filling
// the width and clipping whatever overflows. 10% of every screen was being cut away, 5%
// off each end: the status bar and app bar off the top, the bottom row of controls off
// the bottom. Screens were read, and defects judged, from a thumbnail that did not show
// all of the screen.
//
// The *height* is the fixed dimension and the width follows the ratio, not the other way
// round. Row pitch in relayoutAll is (CARD_H + 60), and every board saved before this
// change carries absolute node positions computed from that pitch; growing the height to
// fix the ratio would have pushed each saved row down into the header of the one below it.
const CARD_H = 300;
const DEFAULT_CARD_ASPECT = 9 / 20;
let cardAspect = DEFAULT_CARD_ASPECT;
let CARD_W = Math.round(CARD_H * cardAspect);
const NODES_PER_ROW = 6;
const sectionLayout = new Map();

/**
 * Adopt a real screenshot's width/height ratio as the card shape.
 * Returns true if the card box actually changed, which is the caller's cue to re-measure.
 */
function setCardAspect(ratio) {
  if (!Number.isFinite(ratio) || ratio <= 0) return false;
  if (Math.abs(ratio - cardAspect) < 0.005) return false;   // same shape, no reflow
  cardAspect = ratio;
  CARD_W = Math.round(CARD_H * ratio);
  return true;
}

/** Back to the 9:20 default — called when a board is cleared, before the next one loads. */
function resetCardAspect() {
  return setCardAspect(DEFAULT_CARD_ASPECT);
}


export { CARD_H, CARD_W, DEFAULT_CARD_ASPECT, GENERIC_LABELS, NODES_PER_ROW, assignSection, isSectionTrigger, nodeHeaderLabel, resetCardAspect, sectionLayout, setCardAspect };
