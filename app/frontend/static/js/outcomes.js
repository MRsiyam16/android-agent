// outcomes.js — extracted verbatim from the old dashboard.js IIFE.

import { agent, agentFetch } from './chat.js';
import { OUTCOME_KINDS } from './modules.js';
import { scheduleRenderOverlay } from './render.js';
import { renderScreenList } from './screens.js';
import { setSectionNotes } from './sectionnotes.js';
import { nodeMeta, nodeStatus, ui } from './state.js';

/** The agent's board notes for the open project. Same cadence as the pills. */
async function refreshSectionNotes() {
  const pkg = ui.boardPackage || agent.package;
  if (!pkg) return;
  const token = ++ui.noteRequest;
  let data;
  try {
    data = await agentFetch(`/projects/${encodeURIComponent(pkg)}/notes`);
  } catch {
    return;   // as with the counts, a blip must not blank notes that were right a moment ago
  }
  if (token !== ui.noteRequest) return;
  if (pkg !== (ui.boardPackage || agent.package)) return;
  setSectionNotes(data.notes);
  scheduleRenderOverlay();   // the connector colours come from these too
}

async function refreshOutcomes() {
  // Keyed on the board's project, so the pills describe the app you are looking at.
  const pkg = ui.boardPackage || agent.package;
  if (!pkg) return;
  // Opening the dashboard starts two independent chains that both end here: `initAgent`
  // points the agent at the *first* project in the list, while `/agent/status` opens the
  // last-used one. Both called this without a token, so whichever response landed second
  // wrote the pills — and an empty project answering after the real one is how a module
  // with 13 findings displayed 0/0/0/0. Same guard loadTranscript already uses.
  const token = ++ui.outcomeRequest;
  let data;
  try {
    data = await agentFetch(`/projects/${encodeURIComponent(pkg)}/outcomes`);
  } catch {
    return;   // a transient failure must not blank counts that were correct a second ago
  }
  if (token !== ui.outcomeRequest) return;          // a newer project's request won
  if (pkg !== (ui.boardPackage || agent.package)) return;   // the board moved under us
  ui.outcomeData = data;
  markFindingScreens(data);
  Object.entries(OUTCOME_KINDS).forEach(([kind, spec]) => {
    const count = (data.counts && data.counts[kind]) || 0;
    document.getElementById(spec.el).textContent = count;
    document.querySelector(`.outcome-pill[data-kind="${kind}"]`)
      .classList.toggle('has-items', count > 0);
  });
}

// How an outcome shows on the screen it was judged from. A pass is deliberately absent:
// most cases pass, and outlining every one of them would leave the board a wall of colour
// with nothing standing out — which is the whole job of the outline. See the note in
// graph.css: a coloured screen always means something is actually wrong here.
const SCREEN_MARKS = {
  bug: { level: 'fail', badge: 'BUG', rank: 3 },
  warning: { level: 'warn', badge: 'WARN', rank: 2 },
  suggestion: { level: 'warn', badge: 'NOTE', rank: 1 },
};

/**
 * Outline the screens that findings were filed against.
 *
 * Derived from the findings every time rather than read back from the saved board: the
 * findings are what a run actually wrote, and a status baked into flow-graph.json is a
 * second copy that goes stale the moment a finding is retracted or re-filed.
 *
 * A finding carries `node` only if the module posted journey steps while it ran — the
 * agent's own screenshots are evidence, not flow-graph nodes, and a finding filed by a
 * module that never called journey_step has no screen on the board to point at. Those are
 * skipped rather than guessed at: putting a red outline on approximately the right screen
 * is worse than putting it on none, because the outline is the claim.
 */
// Only the entries this function put there. `nodeStatus` is shared — a saved board restores
// its own, and a reporting run writes them directly — so clearing the whole map to rebuild
// would quietly delete somebody else's work every time the pills refreshed.
let markedByFindings = new Set();

function markFindingScreens(data) {
  const worst = new Map();   // node id -> the most serious mark filed against it
  Object.values((data && data.buckets) || {}).forEach((bucket) => {
    ((bucket && bucket.modules) || []).forEach((mod) => {
      (mod.items || []).forEach((f) => {
        const mark = SCREEN_MARKS[f.kind];
        // `nodeMeta.has` because the node has to be on *this* board. A finding from a module
        // whose steps were never saved would otherwise leave a status keyed to nothing.
        if (!mark || !f.node || !nodeMeta.has(f.node)) return;
        const held = worst.get(f.node);
        if (held && held.mark.rank >= mark.rank) return;
        worst.set(f.node, { mark, finding: f, module: mod.title });
      });
    });
  });

  let changed = false;
  markedByFindings.forEach((node) => {
    if (!worst.has(node)) { nodeStatus.delete(node); changed = true; }
  });
  worst.forEach(({ mark, finding, module }, node) => {
    const next = {
      level: mark.level,
      badge: mark.badge,
      summary: `${finding.id} · ${module} — ${finding.title}`,
    };
    const held = nodeStatus.get(node);
    if (held && held.level === next.level && held.badge === next.badge
        && held.summary === next.summary) return;
    nodeStatus.set(node, next);
    changed = true;
  });
  markedByFindings = new Set(worst.keys());
  if (changed) { scheduleRenderOverlay(); renderScreenList(); }
}

const outcomeBackdrop = document.getElementById('outcomeBackdrop');
const outcomeList = document.getElementById('outcomeList');

function openOutcomes(kind) {
  const spec = OUTCOME_KINDS[kind];
  document.getElementById('outcomeTitle').textContent = spec.label;
  document.getElementById('outcomeSub').textContent = spec.blurb;
  outcomeList.innerHTML = '';
  const bucket = ui.outcomeData && ui.outcomeData.buckets && ui.outcomeData.buckets[kind];
  const modules = (bucket && bucket.modules) || [];
  if (!modules.length) {
    const empty = document.createElement('div');
    empty.className = 'outcome-empty';
    empty.textContent = `Nothing under ${spec.label.toLowerCase()} yet for this project.`;
    outcomeList.appendChild(empty);
  }
  modules.forEach((mod) => {
    const group = document.createElement('div');
    const title = document.createElement('div');
    title.className = 'outcome-module-title';
    title.textContent = `${mod.title} — ${mod.items.length}`;
    group.appendChild(title);
    mod.items.forEach((f) => group.appendChild(outcomeItem(f, kind)));
    outcomeList.appendChild(group);
  });
  outcomeBackdrop.classList.add('open');
}

function outcomeItem(f, kind) {
  const item = document.createElement('div');
  item.className = 'outcome-item';

  const head = document.createElement('div');
  head.className = 'outcome-item-head';
  const id = document.createElement('span');
  id.className = 'outcome-item-id';
  id.textContent = f.id || '';
  const title = document.createElement('span');
  title.className = 'outcome-item-title';
  title.textContent = f.title || '';
  head.append(id, title);
  // Severity is meaningless on a pass, and printing "none" next to every working case is
  // just noise in the column where the eye is looking for a real signal.
  if (kind !== 'pass' && f.severity && f.severity !== 'none') {
    const sev = document.createElement('span');
    sev.className = 'outcome-item-sev';
    sev.textContent = f.severity;
    head.appendChild(sev);
  }
  item.appendChild(head);

  [['Expected', f.expected], ['Actual', f.actual]].forEach(([label, value]) => {
    if (!value) return;
    const row = document.createElement('div');
    row.className = 'outcome-item-row';
    const b = document.createElement('b');
    b.textContent = label + ': ';
    row.append(b, document.createTextNode(value));
    item.appendChild(row);
  });

  if (Array.isArray(f.steps) && f.steps.length) {
    const row = document.createElement('div');
    row.className = 'outcome-item-row';
    const b = document.createElement('b');
    b.textContent = 'Steps: ';
    row.append(b, document.createTextNode(f.steps.join(' → ')));
    item.appendChild(row);
  }

  // The evidence screenshot is the point. Every incident this harness has learned from was
  // a verdict that looked right in text and wrong in the picture.
  if (f.evidence) {
    const img = document.createElement('img');
    img.src = '/agent/shot?path=' + encodeURIComponent(f.evidence);
    img.alt = 'Evidence';
    img.addEventListener('click', () => {
      document.getElementById('shotLightboxImg').src = img.src;
      document.getElementById('shotLightbox').classList.add('open');
    });
    item.appendChild(img);
  }
  return item;
}

document.querySelectorAll('.outcome-pill').forEach((pill) => {
  pill.addEventListener('click', async () => {
    await refreshOutcomes();
    openOutcomes(pill.dataset.kind);
  });
});
document.getElementById('outcomeClose')
  .addEventListener('click', () => outcomeBackdrop.classList.remove('open'));
outcomeBackdrop.addEventListener('click', (e) => {
  if (e.target === outcomeBackdrop) outcomeBackdrop.classList.remove('open');
});

export { markFindingScreens, openOutcomes, outcomeBackdrop, outcomeItem, outcomeList, refreshOutcomes, refreshSectionNotes };
