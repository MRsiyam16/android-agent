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
const CARD_W = 150;
const CARD_H = 300;
const NODES_PER_ROW = 6;
const sectionLayout = new Map();


export { CARD_H, CARD_W, GENERIC_LABELS, NODES_PER_ROW, assignSection, isSectionTrigger, nodeHeaderLabel, sectionLayout };
