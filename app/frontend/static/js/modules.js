// modules.js — extracted verbatim from the old dashboard.js IIFE.

import { agent, agentEl, agentFetch, appendChat, loadModelPicker } from './chat.js';
import { loadSecretNames } from './compose.js';
import { warmModule } from './main.js';
import { refreshOutcomes } from './outcomes.js';
import { applyPlatformUI } from './phone.js';
import { loadTranscript } from './transcript.js';

// package -> "android" | "ios" | "web", refreshed every time the project list loads.
// main.js's project-select handler reads this too, so it lives here rather than as a local —
// the project list is the one source of truth for a project's platform.
const projectPlatforms = new Map();

function platformOf(pkg) {
  return projectPlatforms.get(pkg) || 'android';
}

async function loadAgentProjects() {
  let projects = [];
  try {
    projects = await agentFetch('/projects');
  } catch (err) {
    appendChat('error', 'Could not list projects: ' + err.message);
    return;
  }
  agentEl.project.innerHTML = '';
  if (!projects.length) {
    const opt = document.createElement('option');
    opt.textContent = 'No projects yet — create one in the Projects tab';
    opt.value = '';
    agentEl.project.appendChild(opt);
    return;
  }
  projectPlatforms.clear();
  projects.forEach((p) => {
    const opt = document.createElement('option');
    opt.value = p.package;
    opt.textContent = p.package;
    agentEl.project.appendChild(opt);
    projectPlatforms.set(p.package, (p.platform || 'android').toLowerCase());
  });
  agent.package = agent.package && projects.some((p) => p.package === agent.package)
    ? agent.package
    : projects[0].package;
  agentEl.project.value = agent.package;
  agent.platform = platformOf(agent.package);
  applyPlatformUI(agent.platform);
  await loadModules();
}

// The project's manager module, under either of its names. A project created before the
// manager existed keeps its interview under `onboarding` — the slug is the folder name, so
// renaming it would mean moving the transcript. Mirrors `store.is_main_slug`.
const MAIN_SLUGS = ['main', 'onboarding'];
const isMainModule = (mod) => MAIN_SLUGS.includes(mod.slug);

function moduleBadges(mod) {
  const badges = [];
  // First, and instead of the status badge: "approved" is meaningless for the module that
  // does the approving, and what a reader needs to know about this row is that it is not a
  // test suite — it files nothing, so an empty findings count on it is not a gap.
  if (isMainModule(mod)) badges.push('<span class="agent-badge manager">manager</span>');
  else badges.push(`<span class="agent-badge ${mod.status}">${mod.status}</span>`);
  if (mod.finding_count) {
    badges.push(`<span class="agent-badge defects">${mod.finding_count} finding${mod.finding_count === 1 ? '' : 's'}</span>`);
  }
  return badges.join(' ');
}

async function loadModules() {
  if (!agent.package) return;
  let modules = [];
  try {
    modules = await agentFetch('/agent/' + encodeURIComponent(agent.package) + '/subprojects');
  } catch (err) {
    appendChat('error', 'Could not load modules: ' + err.message);
    return;
  }

  // Manager first, then the order the file has them in. It is the module you come back to
  // between suites, and on a project with a dozen modules it would otherwise sit wherever it
  // happened to be created.
  //
  // Sorted here, once, rather than in each loop below: `renderModuleHighlight` finds the
  // active card by the *index* of the slug in the dropdown, so the dropdown and the rail have
  // to be built from the same order or selecting one module highlights another.
  modules = modules.slice().sort((a, b) => isMainModule(b) - isMainModule(a));

  document.getElementById('moduleCount').textContent = modules.length;

  // Module dropdown in the chat header
  agentEl.subproject.innerHTML = '';
  modules.forEach((m) => {
    const opt = document.createElement('option');
    opt.value = m.slug;
    opt.textContent = m.title;
    agentEl.subproject.appendChild(opt);
  });

  // Right-rail list
  agentEl.moduleList.innerHTML = '';
  if (!modules.length) {
    const empty = document.createElement('div');
    empty.className = 'agent-empty';
    empty.textContent = 'No modules yet. Press Recon and the agent will explore the app and '
      + 'propose a breakdown for you to approve — or add one by hand below.';
    agentEl.moduleList.appendChild(empty);
  }
  modules.forEach((m) => {
    const card = document.createElement('div');
    card.className = 'agent-module' + (m.slug === agent.slug ? ' active' : '');
    card.innerHTML =
      `<div class="agent-module-head">`
      + `<div class="agent-module-title"></div>`
      + `<button class="module-row-menu" title="Module options">⋯</button>`
      + `</div>`
      + (m.scope ? `<div class="agent-module-scope"></div>` : '')
      + `<div class="agent-module-meta">${moduleBadges(m)}</div>`;
    card.querySelector('.agent-module-title').textContent = m.title;
    if (m.scope) card.querySelector('.agent-module-scope').textContent = m.scope;
    card.addEventListener('click', () => selectModule(m.slug));

    const menuBtn = card.querySelector('.module-row-menu');
    menuBtn.addEventListener('click', (e) => {
      e.stopPropagation();   // opening the menu is not also selecting the module
      openModuleMenu(m, menuBtn);
    });

    if (m.status === 'proposed') {
      const approve = document.createElement('button');
      approve.className = 'agent-module-approve';
      approve.textContent = '✓ Approve module';
      approve.addEventListener('click', async (e) => {
        e.stopPropagation();
        try {
          await agentFetch(`/agent/${encodeURIComponent(agent.package)}/subprojects/${m.slug}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'approved' }),
          });
          await loadModules();
        } catch (err) {
          appendChat('error', 'Could not approve: ' + err.message);
        }
      });
      card.appendChild(approve);
    }
    agentEl.moduleList.appendChild(card);
  });

  if (modules.length) {
    const stillThere = modules.some((m) => m.slug === agent.slug);
    await selectModule(stillThere ? agent.slug : modules[0].slug);
  } else {
    agent.slug = null;
  }
  loadSecretNames();
  refreshOutcomes();
}

// -- Module row menu ------------------------------------------------------------------
let openMenuEl = null;

function closeModuleMenu() {
  if (openMenuEl) { openMenuEl.remove(); openMenuEl = null; }
  document.querySelectorAll('.module-row-menu.open')
    .forEach((b) => b.classList.remove('open'));
}

function openModuleMenu(mod, anchor) {
  const wasOpen = anchor.classList.contains('open');
  closeModuleMenu();
  if (wasOpen) return;   // a second click on the same button closes it

  anchor.classList.add('open');
  const menu = document.createElement('div');
  menu.className = 'module-menu';

  const add = (label, handler, cls) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = label;
    if (cls) btn.className = cls;
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      closeModuleMenu();
      await handler();
    });
    menu.appendChild(btn);
  };

  add('Edit name & scope', () => openModuleSheet(mod));
  add('Open module', () => selectModule(mod.slug));
  if (mod.status === 'proposed') add('Approve', () => setModuleStatus(mod.slug, 'approved'));
  // No Delete on the manager. Every project has exactly one and the server recreates it under
  // the same slug, so removing it from the list does not remove it — it drops the row, and the
  // next project-level action puts it back with the interview transcript still underneath.
  // An option whose effect is "nothing you wanted" is worse than no option.
  if (!isMainModule(mod)) add('Delete module', () => deleteModule(mod), 'danger');

  // Positioned fixed against the button's viewport rect: the rail scrolls, and a menu
  // absolutely positioned inside it would scroll out of view or be clipped by the rail's
  // own overflow.
  const rect = anchor.getBoundingClientRect();
  menu.style.top = `${rect.bottom + 4}px`;
  menu.style.left = `${Math.max(8, rect.right - 150)}px`;
  document.body.appendChild(menu);
  openMenuEl = menu;
}

document.addEventListener('click', () => closeModuleMenu());
window.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModuleMenu(); });

async function setModuleStatus(slug, status) {
  try {
    await agentFetch(`/agent/${encodeURIComponent(agent.package)}/subprojects/${slug}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    await loadModules();
  } catch (err) {
    appendChat('error', err.message);
  }
}

async function deleteModule(mod) {
  // The server keeps the folder — transcript, findings and evidence all survive. Saying so
  // is the difference between a confirm people read and one they click through.
  const ok = window.confirm(
    `Remove "${mod.title}" from the module list?\n\n`
    + 'Its conversation, memory, findings and screenshots stay on disk, so this can be '
    + 'undone by adding a module with the same name.');
  if (!ok) return;
  try {
    await agentFetch(`/agent/${encodeURIComponent(agent.package)}/subprojects/${mod.slug}`,
      { method: 'DELETE' });
    if (agent.slug === mod.slug) { agent.slug = null; agent.loadedKey = null; }
    await loadModules();
  } catch (err) {
    appendChat('error', err.message);
  }
}

// -- Add / edit module sheet ----------------------------------------------------------
const moduleBackdrop = document.getElementById('moduleBackdrop');
const moduleSheetName = document.getElementById('moduleSheetName');
const moduleSheetScope = document.getElementById('moduleSheetScope');
let editingModule = null;

function openModuleSheet(mod) {
  editingModule = mod || null;
  document.getElementById('moduleSheetTitle').textContent = mod ? 'Edit module' : 'Add module';
  document.getElementById('moduleSheetSave').textContent = mod ? 'Save changes' : 'Add module';
  moduleSheetName.value = mod ? (mod.title || '') : '';
  moduleSheetScope.value = mod ? (mod.scope || '') : '';
  moduleBackdrop.classList.add('open');
  moduleSheetName.focus();
}
function closeModuleSheet() {
  moduleBackdrop.classList.remove('open');
  editingModule = null;
}

document.getElementById('addModuleBtn').addEventListener('click', () => {
  if (!agent.package) { appendChat('error', 'Open a project first.'); return; }
  openModuleSheet(null);
});
document.getElementById('moduleSheetCancel').addEventListener('click', closeModuleSheet);
moduleBackdrop.addEventListener('click', (e) => {
  if (e.target === moduleBackdrop) closeModuleSheet();
});

document.getElementById('moduleSheetSave').addEventListener('click', async () => {
  const title = moduleSheetName.value.trim();
  const scope = moduleSheetScope.value.trim();
  if (!title || !agent.package) return;
  try {
    if (editingModule) {
      // The slug is the folder name, so renaming changes the title only — moving the
      // transcript and evidence to a new folder to match a typo fix would be a far bigger
      // and far more destructive operation than the rename it looks like.
      await agentFetch(
        `/agent/${encodeURIComponent(agent.package)}/subprojects/${editingModule.slug}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, scope }),
        });
    } else {
      await agentFetch(`/agent/${encodeURIComponent(agent.package)}/subprojects`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, scope }),
      });
    }
    closeModuleSheet();
    await loadModules();
  } catch (err) {
    appendChat('error', err.message);
  }
});

async function selectModule(slug) {
  if (!slug) return;
  // Keyed on package *and* slug: two projects can both have a module called "arithmetic",
  // and comparing the slug alone would leave the previous project's transcript on screen.
  const key = agent.package + '/' + slug;
  const changed = agent.loadedKey !== key;
  agent.loadedKey = key;
  agent.slug = slug;
  agentEl.subproject.value = slug;
  document.querySelectorAll('.agent-module').forEach((el) => el.classList.remove('active'));
  if (changed) { await loadTranscript(); await warmModule(); loadModelPicker(); }
  else renderModuleHighlight();
}

function renderModuleHighlight() {
  const titles = Array.from(agentEl.subproject.options).map((o) => o.value);
  const index = titles.indexOf(agent.slug);
  const cards = document.querySelectorAll('.agent-module');
  if (index >= 0 && cards[index]) cards[index].classList.add('active');
}

// ---------------------------------------------------------------------------
// Outcomes — the four top-bar pills, gathered across every module
// ---------------------------------------------------------------------------
const OUTCOME_KINDS = {
  pass: { label: 'Working', el: 'countPass',
          blurb: 'Cases the agent ran and found behaving correctly.' },
  warning: { label: 'Warnings', el: 'countWarning',
             blurb: 'Not broken, but questionable — worth a human look.' },
  bug: { label: 'Bugs', el: 'countBug',
         blurb: 'Confirmed defects. Every one carries the screenshot it was judged from.' },
  suggestion: { label: 'Suggestions', el: 'countSuggestion',
                blurb: 'Nothing is wrong; these are places the app could be better.' },
};

// Declared before the function that assigns it: `let` is in a temporal dead zone until its
// declaration runs, and refreshOutcomes is reachable from startup paths.


export { MAIN_SLUGS, OUTCOME_KINDS, closeModuleMenu, closeModuleSheet, deleteModule, editingModule, isMainModule, loadAgentProjects, loadModules, moduleBackdrop, moduleBadges, moduleSheetName, moduleSheetScope, openMenuEl, openModuleMenu, openModuleSheet, platformOf, projectPlatforms, renderModuleHighlight, selectModule, setModuleStatus };
