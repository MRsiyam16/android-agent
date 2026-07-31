// board.js — extracted verbatim from the old dashboard.js IIFE.

import { escapeHtml } from './markdown.js';
import { emptyState } from './comments.js';
import { resetGraph, updateStats } from './feed.js';
import { relayoutAll } from './layout.js';
import { startProjectMain } from './main.js';
import { network, scheduleRenderOverlay } from './render.js';
import { comments, edgeMeta, nodeMeta, nodeStatus, nodesData, sectionOrder, sections, textNotes, ui } from './state.js';
import { refreshOutcomes, refreshSectionNotes } from './outcomes.js';
import { setSessionLabel, showStatus } from './status.js';
import { CARD_MAX_W, requestScaled } from './util.js';

function buildProjectBlob() {
  return {
    version: 1,
    savedAt: new Date().toISOString(),
    // Stamped so the server can reject a save aimed at the wrong project rather than
    // silently taking it. Without this the check on the server has nothing to compare.
    package: ui.boardPackage,
    sessionId: ui.currentSessionId,
    nodes: Array.from(nodeMeta.entries()).map(([hash, meta]) => ({ hash, ...meta })),
    edges: Array.from(edgeMeta.entries()).map(([id, meta]) => ({ id, ...meta })),
    sectionOrder,
    sections: Array.from(sections.entries()).map(([name, list]) => ({ name, list })),
    notes: Array.from(textNotes.values()),
    comments: Array.from(comments.values()),
    nodeStatus: Array.from(nodeStatus.entries()).map(([hash, s]) => ({ hash, ...s })),
    // Screen positions are the arrangement itself: sections dragged into parallel
    // columns are a hand-made layout that `relayoutAll` cannot reproduce, so without
    // this a reload silently collapses the board back into one column.
    nodePositions: buildNodePositions(),
  };
}

function buildNodePositions() {
  const out = [];
  try {
    const ids = [];
    nodesData.forEach((n) => ids.push(n.id));
    if (!ids.length) return out;
    const pos = network.getPositions(ids);
    ids.forEach((id) => {
      const p = pos[id];
      if (p) out.push({ hash: id, x: Math.round(p.x), y: Math.round(p.y) });
    });
  } catch { /* a layout we cannot read must not block the save */ }
  return out;
}

// Put saved screen positions back. Returns how many were applied so callers can tell
// whether to fall back to the automatic layout.
function applyNodePositions(list) {
  if (!Array.isArray(list) || !list.length) return 0;
  const updates = [];
  list.forEach((p) => {
    if (!p || typeof p.x !== 'number' || typeof p.y !== 'number') return;
    if (!nodesData.get(p.hash)) return;
    updates.push({ id: p.hash, x: p.x, y: p.y, fixed: { x: true, y: true } });
  });
  if (updates.length) { nodesData.update(updates); scheduleRenderOverlay(); }
  return updates.length;
}

// Download-as-file and import-from-file are gone from the dock. The board lives in its
// project folder on disk and is saved there; a second, divergent copy in ~/Downloads was
// one more thing that could be loaded into the wrong project.

// -- Projects (server-persisted, one per app package) --
function scheduleAutoSave() {
  if (!ui.boardPackage) return;
  clearTimeout(ui.autoSaveTimer);
  ui.autoSaveTimer = setTimeout(persistProjectToServer, 2000);
}

// A reload rebuilds the graph from telemetry history, which carries no annotations — and
// the ingest that follows schedules an autosave. Without this, that autosave writes a
// note-less blob over the saved project and silently destroys every text note and comment
// pin the user had added. Pull them back before the autosave fires.
async function restoreSavedAnnotations() {
  // Keyed on the board's owner, not the on-screen app: pulling annotations by whatever
  // package the current frame happens to be would graft one project's notes onto another's
  // board the moment a run wandered out of its own app.
  if (!ui.boardPackage || textNotes.size || comments.size || nodeStatus.size) return;
  try {
    const resp = await fetch(`/projects/${encodeURIComponent(ui.boardPackage)}/flow-graph`);
    if (!resp.ok) return;
    const saved = await resp.json();
    (saved.notes || []).forEach((n) => textNotes.set(n.id, n));
    (saved.comments || []).forEach((c) => comments.set(c.id, c));
    // Defect markings are rebuilt from the same saved blob as the annotations —
    // telemetry alone cannot say which screens a reviewer judged broken.
    (saved.nodeStatus || []).forEach((s) => nodeStatus.set(s.hash, s));
    // Telemetry replay lays the graph out automatically; a saved arrangement must win
    // over that, or every reload throws away however the board was actually organised.
    const placed = applyNodePositions(saved.nodePositions);
    if (placed) network.fit({ animation: false });
    if (textNotes.size || comments.size || nodeStatus.size) scheduleRenderOverlay();
  } catch { /* annotations are a nicety; never break the graph over them */ }
}

async function persistProjectToServer() {
  // Saves to the board's owner, never to whatever is merely selected or on screen.
  if (!ui.boardPackage) return;
  try {
    const resp = await fetch(`/projects/${encodeURIComponent(ui.boardPackage)}/flow-graph`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildProjectBlob()),
    });
    // A 409 means the server caught a save that would have overwritten another project.
    // Silence here would leave the user believing the board was saved, so it is loud.
    if (resp.status === 409) {
      const detail = await resp.json().catch(() => ({}));
      showStatus(detail.detail || 'Refused a save that would overwrite another project.',
        'error');
    }
  } catch (err) {
    // Best-effort — the live dashboard/telemetry keeps working even if this fails.
    console.warn('Could not auto-save project:', err);
  }
}

async function fetchProjects() {
  refreshSaveProjectBtn();
  const list = document.getElementById('projectsList');
  const empty = document.getElementById('projectsEmpty');
  let projects = [];
  try {
    const resp = await fetch('/projects');
    projects = await resp.json();
  } catch (err) {
    showStatus('Could not load projects: ' + err.message, 'error');
    return;
  }
  list.querySelectorAll('.project-row').forEach((el) => el.remove());
  empty.classList.toggle('hidden', projects.length > 0);
  document.getElementById('projectCount').textContent = projects.length;
  projects.forEach((p) => {
    const row = document.createElement('div');
    row.className = 'project-row' + (p.package === ui.currentPackage ? ' active' : '');
    const lastRun = p.last_run_at
      ? new Date(p.last_run_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
      : 'never';
    row.innerHTML = `
      <div class="project-row-package">${escapeHtml(p.package)}</div>
      <div class="project-row-meta">last tested: ${escapeHtml(lastRun)}</div>
      <div class="project-row-stats">
        <div class="stat-pill">${p.state_count || 0} states</div>
        <div class="stat-pill">${p.edge_count || 0} transitions</div>
      </div>`;
    // Where it lives is worth surfacing once a project can live anywhere: two projects with
    // similar package names in different folders are otherwise indistinguishable here.
    if (p.root) row.title = p.root;
    row.addEventListener('click', () => openProject(p.package));
    list.appendChild(row);
  });
}

// The top bar names the open project, and is the only place the chrome does.
function setProjectPill(pkg) {
  const pill = document.getElementById('projectPill');
  document.getElementById('projectPillName').textContent = pkg || 'No project open';
  pill.classList.toggle('live', Boolean(pkg));
  pill.title = pkg ? `${pkg} — click to switch project` : 'Click to pick a project';
}

async function openProject(pkg) {
  // A debounced autosave from the outgoing board must not survive the switch: it would
  // fire after the retarget and write the old board's screens under the new project's
  // name. Cancel it, then flush the old board to its own project while we still know
  // which one that is.
  clearTimeout(ui.autoSaveTimer);
  if (ui.boardPackage && ui.boardPackage !== pkg) await persistProjectToServer();
  ui.currentPackage = pkg;
  // Disowned until the incoming board actually loads. Anything that autosaves in the gap
  // would otherwise be writing the previous project's screens into this one's file.
  ui.boardPackage = null;
  setProjectPill(pkg);
  // One click now means "work on this app": the board loads *and* the agent is pointed at
  // it. Splitting those across two tabs is what used to let the graph and the agent end up
  // on different packages without anything saying so.
  pointAgentAt(pkg);
  document.querySelectorAll('.project-row').forEach((row) => {
    row.classList.toggle('active',
      row.querySelector('.project-row-package')?.textContent === pkg);
  });
  try {
    const resp = await fetch(`/projects/${encodeURIComponent(pkg)}/flow-graph`);
    if (resp.status === 404) {
      resetGraph();
      // An empty board legitimately belongs to pkg — a run started now should save here.
      ui.boardPackage = pkg;
      showStatus(`Project "${pkg}" has no saved runs yet — start exploring to populate it.`, 'info');
      return;
    }
    const project = await resp.json();
    loadProject(project);
    ui.boardPackage = pkg;
    // After boardPackage is set, or the fetch keys off whichever project the agent
    // happens to be pointed at rather than the board that just loaded.
    //
    // Both, and this is the only place both are guaranteed correct. `markFindingScreens`
    // can only outline a screen that is already on the board — it drops a finding whose
    // node it does not recognise, which is right when a module's steps were never saved and
    // catastrophic one moment earlier, when `nodeMeta` is simply still empty. Boot reaches
    // refreshOutcomes through loadModules long before the flow graph arrives, so every
    // linked finding was being discarded and the board came up with 26 findings pointing at
    // screens and not one outline on it.
    refreshOutcomes();
    refreshSectionNotes();
  } catch (err) {
    showStatus('Could not open project: ' + err.message, 'error');
  }
}

// Drives the agent's own (hidden) project select, so its module list, transcript and
// findings all follow the board. Declared here and safe to call before the agent has
// loaded: the select is simply empty until then, and initAgent picks the value back up.
function pointAgentAt(pkg) {
  const select = document.getElementById('agentProject');
  if (!select || !pkg || select.value === pkg) return;
  if (![...select.options].some((o) => o.value === pkg)) {
    select.appendChild(new Option(pkg, pkg));
  }
  select.value = pkg;
  select.dispatchEvent(new Event('change'));
}

// Explicit save of the open board. Autosave already runs on a 2s debounce, but it is
// invisible and easy to distrust — and it does not fire at all if the last change was
// a pan or zoom. This is the button you press before closing the laptop.
const saveProjectBtn = document.getElementById('saveProjectBtn');

// Looks the element up rather than closing over the const above: this is called from
// fetchProjects, which is declared earlier in the file, so a closure would risk a
// temporal-dead-zone throw if the projects view ever renders during startup.
function refreshSaveProjectBtn() {
  const btn = document.getElementById('saveProjectBtn');
  if (!btn) return;
  const ready = Boolean(ui.boardPackage) && nodesData.length > 0;
  btn.disabled = !ready;
  btn.textContent = ready ? `Save "${ui.boardPackage}"` : 'Save current project';
}

if (saveProjectBtn) {
  saveProjectBtn.addEventListener('click', async () => {
    if (!ui.boardPackage) return;
    const original = saveProjectBtn.textContent;
    saveProjectBtn.disabled = true;
    saveProjectBtn.textContent = 'Saving…';
    try {
      const blob = buildProjectBlob();
      const resp = await fetch(`/projects/${encodeURIComponent(ui.boardPackage)}/flow-graph`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(blob),
      });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({}));
        throw new Error(detail.detail || `server said ${resp.status}`);
      }
      saveProjectBtn.textContent = 'Saved ✓';
      showStatus(
        `Saved ${blob.nodePositions.length} screen positions, ${blob.notes.length} notes `
        + `and ${blob.comments.length} comment pins to "${ui.boardPackage}".`, 'info');
      fetchProjects();
      setTimeout(() => { saveProjectBtn.textContent = original; refreshSaveProjectBtn(); }, 2000);
    } catch (err) {
      saveProjectBtn.textContent = 'Save failed';
      showStatus('Could not save project: ' + err.message, 'error');
      setTimeout(() => { saveProjectBtn.textContent = original; refreshSaveProjectBtn(); }, 2500);
    }
  });
}

// -- New project ---------------------------------------------------------------------
// The old "+ New" was a button next to a text field that most people never noticed was
// required, so pressing it with an empty field did nothing at all and read as broken.
// It is now a dialog that asks for both things it needs.
const newProjectSheet = document.getElementById('newProjectBackdrop');
const newProjectPackage = document.getElementById('newProjectPackage');
const newProjectRoot = document.getElementById('newProjectRoot');
const newProjectNote = document.getElementById('newProjectNote');
const NEW_PROJECT_NOTE = newProjectNote.innerHTML;

function openNewProjectSheet() {
  newProjectPackage.value = '';
  newProjectRoot.value = '';
  newProjectNote.innerHTML = NEW_PROJECT_NOTE;
  newProjectNote.classList.remove('error');
  newProjectSheet.classList.add('open');
  newProjectPackage.focus();
}
function closeNewProjectSheet() { newProjectSheet.classList.remove('open'); }

document.getElementById('newProjectBtn').addEventListener('click', openNewProjectSheet);
document.getElementById('newProjectCancel').addEventListener('click', closeNewProjectSheet);
newProjectSheet.addEventListener('click', (e) => {
  if (e.target === newProjectSheet) closeNewProjectSheet();
});

document.getElementById('newProjectBrowse').addEventListener('click', async () => {
  const btn = document.getElementById('newProjectBrowse');
  const original = btn.textContent;
  // The dialog is modal on the server's thread, so this can sit here for as long as the
  // user takes to decide. Saying so beats a button that looks dead for half a minute.
  btn.textContent = 'Choose…';
  btn.disabled = true;
  try {
    const resp = await fetch('/ui/pick-folder', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `server said ${resp.status}`);
    if (data.path) newProjectRoot.value = data.path;
  } catch (err) {
    newProjectNote.textContent =
      `${err.message} — you can type the folder path in by hand instead.`;
    newProjectNote.classList.add('error');
  } finally {
    btn.textContent = original;
    btn.disabled = false;
  }
});

document.getElementById('newProjectCreate').addEventListener('click', async () => {
  const pkg = newProjectPackage.value.trim();
  if (!pkg) {
    newProjectNote.textContent = 'An app package is required, e.g. com.example.app.';
    newProjectNote.classList.add('error');
    newProjectPackage.focus();
    return;
  }
  try {
    const body = { package: pkg };
    if (newProjectRoot.value.trim()) body.root = newProjectRoot.value.trim();
    const resp = await fetch('/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `server said ${resp.status}`);
    closeNewProjectSheet();
    await fetchProjects();
    await openProject(pkg);
    // Straight into the interview rather than leaving an empty board with no next step. This
    // is also what creates the project's Main module, so a project always has a manager.
    startProjectMain(pkg);
  } catch (err) {
    newProjectNote.textContent = err.message;
    newProjectNote.classList.add('error');
  }
});

// Load project — the counterpart to Save, and what the removed hint text used to occupy.
// Opening the Projects panel is the whole action: this is the one list of projects.
document.getElementById('loadProjectBtn').addEventListener('click', async () => {
  await fetchProjects();
  const panel = document.querySelector('.panel[data-panel="projects"]');
  if (panel) panel.classList.add('open');
  document.getElementById('projectsList').scrollIntoView({ block: 'nearest' });
  showStatus('Pick a project from the list above to load its board.', 'info');
});

function loadProject(project) {
  resetGraph();
  ui.currentSessionId = project.sessionId || null;
  (project.nodes || []).forEach((n) => {
    nodeMeta.set(n.hash, {
      package: n.package, activity: n.activity, elements: n.elements || [], screenshot: n.screenshot,
      action: n.action, section: n.section, screenName: n.screenName || '', screenNumber: n.screenNumber || null,
    });
    nodesData.update({ id: n.hash, x: 0, y: 0, fixed: { x: true, y: true } });
  });
  (project.sections || []).forEach((s) => sections.set(s.name, s.list.slice()));
  sectionOrder.push(...(project.sectionOrder || []));
  (project.edges || []).forEach((e) => edgeMeta.set(e.id, { from: e.from, to: e.to, fullLabel: e.fullLabel }));
  (project.notes || []).forEach((n) => textNotes.set(n.id, n));
  (project.comments || []).forEach((c) => comments.set(c.id, c));
  (project.nodeStatus || []).forEach((s) => nodeStatus.set(s.hash, s));
  // Lay out first so anything the save predates still lands somewhere sensible, then
  // let the saved arrangement overwrite it.
  relayoutAll();
  applyNodePositions(project.nodePositions);
  // vis hasn't drawn the new nodes yet on this frame, so comment pins (which need a
  // node's DOM rect) and text notes render into nothing and stay invisible until the
  // user happens to pan or resize. Re-render once the layout has actually settled.
  //
  // The fit is not cosmetic. A saved arrangement keeps the canvas coordinates it was
  // dragged into, and those are nowhere near the origin — the two boards saved with a
  // custom layout here start at x≈4300, while a freshly loaded page has its camera at
  // 0,0. Without moving the camera to the content, opening such a project loads all 39
  // screens correctly and shows you an empty canvas, which reads as "the project is
  // broken". Boards with no saved positions were laid out near the origin, which is why
  // this went unnoticed.
  // Deferred out of the draw callback on purpose: fit() moves the camera, and moving it
  // from inside vis's own afterDrawing handler re-enters the draw it is still finishing,
  // which leaves getBoundingBox() returning nothing and the overlay with no rects to
  // position cards by — a board that is loaded, fitted, and completely invisible.
  let framed = false;
  const frameBoard = () => {
    if (framed) return;
    framed = true;
    requestAnimationFrame(() => {
      network.fit({ animation: false });
      requestAnimationFrame(scheduleRenderOverlay);
    });
  };
  network.once('afterDrawing', frameBoard);
  setTimeout(frameBoard, 250);
  updateStats();
  if (nodesData.length) emptyState.classList.add('hidden');
  setSessionLabel(ui.currentSessionId);
  showStatus(`Loaded project — ${nodesData.length} states, ${edgeMeta.size} transitions.`, 'ok');
}

// ---------------------------------------------------------------------------
// Screens panel — the graph as a list you can jump from
//
// A 40-node board is quick to lose your place in: the names are on the cards, so finding
// one means panning around looking for it. This is the same data as an index, filterable,
// and clicking an entry flies the canvas to that screen instead of hunting for it.
// ---------------------------------------------------------------------------
const screenListEl = document.getElementById('screenList');
const screensEmptyEl = document.getElementById('screensEmpty');
const screenSearchEl = document.getElementById('screenSearch');

// Tiles reuse the shared downscaler above, and are only requested for rows actually on
// screen — a 39-screen project should not decode 39 phone frames to fill a panel that
// shows seven of them at a time.
const thumbObserver = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (!entry.isIntersecting) return;
    const el = entry.target;
    thumbObserver.unobserve(el);
    const hash = el.dataset.hash;
    const meta = nodeMeta.get(hash);
    if (!meta || !meta.screenshot) return;
    const apply = (url) => {
      // The row may have been rebuilt while this decoded, so address it by hash.
      screenListEl.querySelector(`.screen-thumb[data-hash="${hash}"]`)?.setAttribute('src', url);
    };
    const cached = requestScaled(hash, meta.screenshot, CARD_MAX_W, apply);
    if (cached) apply(cached);
  });
}, { root: screenListEl, rootMargin: '120px' });


export { NEW_PROJECT_NOTE, applyNodePositions, buildNodePositions, buildProjectBlob, closeNewProjectSheet, fetchProjects, loadProject, newProjectNote, newProjectPackage, newProjectRoot, newProjectSheet, openNewProjectSheet, openProject, persistProjectToServer, pointAgentAt, refreshSaveProjectBtn, restoreSavedAnnotations, saveProjectBtn, scheduleAutoSave, screenListEl, screenSearchEl, screensEmptyEl, setProjectPill, thumbObserver };
