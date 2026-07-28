// feed.js — extracted verbatim from the old dashboard.js IIFE.

import { scheduleAutoSave } from './board.js';
import { emptyState } from './comments.js';
import { relayoutAll } from './layout.js';
import { network, scheduleRenderOverlay } from './render.js';
import { renderScreenList } from './screens.js';
import { assignSection, isSectionTrigger, sectionLayout } from './sections.js';
import { cardElements, comments, edgeMeta, nodeMeta, nodeStatus, nodesData, sectionOrder, sections, textNotes, ui } from './state.js';
import { hideStatus, setSessionLabel } from './status.js';

function ingest(record) {
  if (!record || !record.state_hash) return;
  if (record.session_id) ui.currentSessionId = record.session_id;
  if (record.package_name) ui.currentPackage = record.package_name;
  // Claim ownership only for an unowned board. `target_package` is the run's target;
  // `package_name` is merely the screen in front of us, so it is the last resort and
  // still may not *re*assign an owner.
  if (!ui.boardPackage) ui.boardPackage = record.target_package || record.package_name || null;

  if (Array.isArray(record.available_elements) && record.available_elements.length) {
    ui.lastElements = record.available_elements;
    const maxX = Math.max(1, ...record.available_elements.map((e) => (e.bounds && e.bounds[2]) || 0));
    const maxY = Math.max(1, ...record.available_elements.map((e) => (e.bounds && e.bounds[3]) || 0));
    ui.lastDims = { w: maxX, h: maxY };
  }

  const isNewNode = !nodesData.get(record.state_hash);
  const existingMeta = nodeMeta.get(record.state_hash) || {};
  const meta = {
    package: record.package_name || existingMeta.package || '',
    activity: record.activity_name || existingMeta.activity || '',
    elements: (record.available_elements && record.available_elements.length)
      ? record.available_elements
      : (existingMeta.elements || []),
    screenshot: record.screenshot_b64 || existingMeta.screenshot || '',
    action: record.executed_action || existingMeta.action || null,
    section: existingMeta.section,
    screenName: record.screen_name || existingMeta.screenName || '',
    screenNumber: record.screen_number || existingMeta.screenNumber || null,
  };
  nodeMeta.set(record.state_hash, meta);

  if (isNewNode) {
    let sectionName = 'Main';
    if (record.section) {
      // Scripted journeys group their own steps (e.g. "Calculation 1: 7 + 5"); trust
      // that over the keypad-label heuristic below, which can't infer test intent.
      sectionName = record.section;
    } else if (record.parent_state_hash) {
      const parentMeta = nodeMeta.get(record.parent_state_hash);
      const parentSection = (parentMeta && parentMeta.section) || 'Main';
      const actionLabel = record.executed_action ? record.executed_action.label : null;
      sectionName = isSectionTrigger(actionLabel) ? actionLabel.trim() : parentSection;
    }
    assignSection(record.state_hash, sectionName);
    nodesData.update({ id: record.state_hash, x: 0, y: 0, fixed: { x: true, y: true } });
    relayoutAll();
    if (ui.autoFitOnNewState) network.fit({ animation: true });
    scheduleAutoSave();
  }

  // Journey steps chain even without a tap (verdict/checkpoint steps carry no action);
  // without this the flow breaks into disconnected fragments at every checkpoint.
  if (record.parent_state_hash && (record.executed_action || record.step_label)) {
    const act = record.executed_action;
    const edgeId = record.parent_state_hash + '->' + record.state_hash + '->' +
      (act ? (act.x || 0) + ',' + (act.y || 0) : 'step');
    if (!edgeMeta.has(edgeId)) {
      edgeMeta.set(edgeId, {
        from: record.parent_state_hash,
        to: record.state_hash,
        fullLabel: act ? 'click: ' + (act.label || '?') : 'then',
      });
    }
  }

  updateStats();
  emptyState.classList.add('hidden');
  scheduleRenderOverlay();
}

function updateStats() {
  document.getElementById('statNodes').textContent = nodesData.length + ' states';
  document.getElementById('statEdges').textContent = edgeMeta.size + ' transitions';
  // The Screens panel is an index of the same graph, so it is rebuilt wherever the graph
  // changes — which is exactly everywhere this already runs (ingest, load, reset).
  renderScreenList();
}

function resetGraph() {
  nodesData.clear();
  nodeMeta.clear();
  edgeMeta.clear();
  sections.clear();
  sectionOrder.length = 0;
  sectionLayout.clear();
  cardElements.forEach((c) => c.remove());
  cardElements.clear();
  textNotes.clear();
  comments.clear();
  nodeStatus.clear();
  ui.selectedIds = new Set();
  ui.currentSessionId = null;
  ['connectorPaths', 'connectorLabels', 'connectorMarkers', 'nodeHeaders', 'sectionHeadings', 'commentsLayer', 'textNotesLayer']
    .forEach((id) => { document.getElementById(id).innerHTML = ''; });
  updateStats();
  emptyState.classList.remove('hidden');
  setSessionLabel(null);
  hideStatus();
}

// ---------------------------------------------------------------------------
// Status banner
// ---------------------------------------------------------------------------
const statusBanner = document.getElementById('statusBanner');
const statusMessage = document.getElementById('statusMessage');


export { ingest, resetGraph, statusBanner, statusMessage, updateStats };
