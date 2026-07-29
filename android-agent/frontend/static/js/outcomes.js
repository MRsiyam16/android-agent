// outcomes.js — extracted verbatim from the old dashboard.js IIFE.

import { agent, agentFetch } from './chat.js';
import { OUTCOME_KINDS } from './modules.js';
import { ui } from './state.js';

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
  Object.entries(OUTCOME_KINDS).forEach(([kind, spec]) => {
    const count = (data.counts && data.counts[kind]) || 0;
    document.getElementById(spec.el).textContent = count;
    document.querySelector(`.outcome-pill[data-kind="${kind}"]`)
      .classList.toggle('has-items', count > 0);
  });
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

export { openOutcomes, outcomeBackdrop, outcomeItem, outcomeList, refreshOutcomes };
