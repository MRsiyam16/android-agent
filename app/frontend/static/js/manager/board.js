// board.js — the fleet board: five apps at once, and the defects that belong to more than one.
//
// Four views behind one canvas, chosen from the left rail. They are views and not pages
// because the question you are answering is always the same one — where does this product
// stand — and only the depth changes: the whole product, one app, the groupings, the queue.

import { api, chip, el, eco, loadBoard } from './api.js';

const canvas = document.getElementById('canvas');
const canvasTitle = document.getElementById('canvasTitle');
const canvasSub = document.getElementById('canvasSub');
const appNav = document.getElementById('appNav');
const railFoot = document.getElementById('railFoot');
const ecoName = document.getElementById('ecoName');
const ecoHeadline = document.getElementById('ecoHeadline');

// Rendered wherever a cluster is, because a cluster is a hypothesis about someone else's
// backend made from the outside. Dropping the qualifier turns "these three look like one
// defect" into "these three are one defect", which is not what was shown.
const CONFIDENCE_TITLE = {
  confirmed: 'Something in the findings discriminates — a shared token, an identical error',
  likely: 'Same mechanism and area, nothing decisive',
  tentative: 'Same shape, possibly separate implementations — check before filing as one',
};

const KINDS = ['bug', 'warning', 'suggestion', 'pass'];

let view = { name: 'overview', arg: null };
const cache = {};   // lazily-fetched lists, dropped wholesale on refresh

async function cached(key, url) {
  if (!cache[key]) cache[key] = await api(url);
  return cache[key];
}

function ecoUrl(path) {
  return `/ecosystems/${encodeURIComponent(eco.name)}${path}`;
}

// -- small pieces ---------------------------------------------------------------------
function tally(counts, only) {
  const row = el('div', 'tally');
  (only || KINDS).forEach((kind) => {
    const n = counts[kind] || 0;
    if (!n) return;
    row.appendChild(chip(`${n} ${kind}${n === 1 ? '' : 's'}`, `tally-chip k-${kind}`));
  });
  if (!row.children.length) row.appendChild(chip('nothing filed', 'tally-chip k-none'));
  return row;
}

function clusterCard(cluster) {
  const card = el('div', 'cluster');

  const head = el('div', 'cluster-head');
  head.append(
    chip(`${cluster.size}×`, 'cluster-size'),
    chip(cluster.scope === 'cross-app' ? 'cross-app' : 'single-app',
         `cluster-scope scope-${cluster.scope}`,
         cluster.scope === 'cross-app'
           ? 'Filed separately by more than one app — no single project could see this'
           : 'Every report came from one app'),
    chip(cluster.confidence, `cluster-confidence conf-${cluster.confidence}`,
         CONFIDENCE_TITLE[cluster.confidence] || ''),
    el('span', 'cluster-title', cluster.title || cluster.id));
  if (cluster.resolved) {
    head.appendChild(chip('✓ Resolved', 'cluster-resolved',
                          'Every report of this defect is closed'));
  }
  card.appendChild(head);

  if (cluster.root) card.appendChild(el('div', 'cluster-root', cluster.root));

  (cluster.members || []).forEach((m) => {
    const row = el('div', 'member');
    row.append(chip(m.role, 'member-role'),
               el('span', 'member-ref', `${m.module_title || m.module} · ${m.finding}`),
               el('span', 'member-title', m.title || ''));
    if (m.resolved) row.appendChild(chip('✓', 'member-resolved', 'Closed'));
    if (m.issue_url) {
      const link = el('a', 'member-link', '↗');
      link.href = m.issue_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.title = 'Open the tracked issue';
      row.appendChild(link);
    }
    card.appendChild(row);
  });

  // An orphan is a member whose finding is no longer on disk. Shown rather than dropped: a
  // cluster that shrinks quietly is how a wrong grouping becomes believable.
  (cluster.orphans || []).forEach((o) => {
    card.appendChild(el('div', 'member orphan',
                        `${o.package} · ${o.module} · ${o.finding} — no longer on disk`));
  });

  return card;
}

function findingRow(f) {
  const row = el('div', 'finding');
  row.append(chip(f.kind, `finding-kind k-${f.kind}`),
             chip(f.role || f.package, 'finding-app'),
             el('span', 'finding-ref', `${f.module_title || f.module_slug} · ${f.id}`),
             el('span', 'finding-title', f.title || ''));
  if (f.severity && f.severity !== 'none') {
    row.appendChild(chip(f.severity, `finding-sev sev-${f.severity}`));
  }
  if (f.resolved) row.appendChild(chip('✓', 'member-resolved', 'Closed'));
  return row;
}

function empty(text) {
  return el('div', 'canvas-empty', text);
}

function sectionTitle(text, note) {
  const wrap = el('div', 'section-head');
  wrap.appendChild(el('h2', 'section-title', text));
  if (note) wrap.appendChild(el('span', 'section-note', note));
  return wrap;
}

// -- views ----------------------------------------------------------------------------
function appCard(app) {
  const card = el('button', 'app-card');
  card.addEventListener('click', () => show('app', app.package));

  const head = el('div', 'app-card-head');
  head.append(el('span', 'app-role', app.role), chip(app.platform, 'app-platform'));
  card.appendChild(head);

  card.appendChild(el('div', 'app-pkg', app.package));

  const stat = el('div', 'app-stat');
  stat.append(el('b', null, String(app.defects)), el('span', null, ' defects'));
  card.appendChild(stat);

  const mods = el('div', 'app-modules',
                  `${app.modules} modules · ${app.modules_tested} tested`);
  card.appendChild(mods);

  // The distinction worth protecting on a board where low numbers look like good news: an app
  // with no defects may be one that works, or one nobody has run. Only one of those is good.
  if (app.never_run.length) {
    card.appendChild(chip(`${app.never_run.length} never run`, 'app-warn',
                          'These modules have filed nothing because nothing opened them, '
                          + 'not because they passed: ' + app.never_run.join(', ')));
  }
  return card;
}

function renderOverview() {
  const board = eco.board;
  canvasTitle.textContent = 'The whole product';
  const c = board.clusters;
  canvasSub.textContent =
    `${board.totals.apps} apps · ${c.filed} filed defects → ${c.distinct} distinct `
    + `(${c.absorbed} absorbed by ${c.clusters} clusters, ${c.cross_app} of them cross-app)`;

  const apps = el('div', 'app-grid');
  board.apps.forEach((app) => apps.appendChild(appCard(app)));
  canvas.appendChild(apps);

  canvas.appendChild(sectionTitle(
    `Cross-app defects (${board.cross_app.length})`,
    'One defect, filed separately by more than one app — no single project could see these'));
  if (!board.cross_app.length) {
    canvas.appendChild(empty('None found yet.'));
  } else {
    board.cross_app.forEach((cluster) => canvas.appendChild(clusterCard(cluster)));
  }

  if (c.orphans) {
    canvas.appendChild(el('div', 'canvas-warn',
      `${c.orphans} cluster member(s) point at findings that no longer exist — the distinct `
      + 'count above is wrong until those are fixed.'));
  }
}

async function renderApp(pkg) {
  const app = eco.board.apps.find((a) => a.package === pkg);
  if (!app) { canvas.appendChild(empty('That app is no longer in this ecosystem.')); return; }

  canvasTitle.textContent = app.role;
  canvasSub.textContent = `${app.package} · ${app.platform} · ${app.modules} modules `
    + `(${app.modules_tested} tested) · ${app.defects} defects`;

  const [modules, findings] = await Promise.all([
    cached('modules', ecoUrl('/modules')),
    cached('findings', ecoUrl('/findings')),
  ]);

  canvas.appendChild(sectionTitle('Modules'));
  const mods = modules.filter((m) => m.package === pkg);
  if (!mods.length) canvas.appendChild(empty('This app has no modules yet.'));
  mods.forEach((mod) => {
    const row = el('div', 'module-row');
    row.append(chip(mod.status, `module-status st-${mod.status}`),
               el('span', 'module-title', mod.title),
               el('span', 'module-slug', mod.slug));
    if (!mod.last_run_at) {
      row.appendChild(chip('never run', 'app-warn',
                           'Nothing has opened this module — its empty tally is absence of '
                           + 'testing, not evidence of health'));
    }
    row.appendChild(tally(mod.counts));
    canvas.appendChild(row);
  });

  const mine = findings.filter((f) => f.package === pkg);
  canvas.appendChild(sectionTitle(`Defects (${mine.length})`));
  if (!mine.length) canvas.appendChild(empty('Nothing filed against this app.'));
  mine.forEach((f) => canvas.appendChild(findingRow(f)));
}

async function renderClusters() {
  canvasTitle.textContent = 'Clusters';
  canvasSub.textContent = 'Findings judged to be one defect. Cross-app first, then largest.';
  const clusters = await cached('clusters', ecoUrl('/clusters'));
  if (!clusters.length) {
    canvas.appendChild(empty('No duplicates found yet — every filed defect is counted as '
                             + 'distinct.'));
    return;
  }
  let lastScope = null;
  clusters.forEach((cluster) => {
    if (cluster.scope !== lastScope) {
      lastScope = cluster.scope;
      canvas.appendChild(sectionTitle(
        cluster.scope === 'cross-app' ? 'Cross-app' : 'Single-app',
        cluster.scope === 'cross-app'
          ? 'Filed by more than one app'
          : 'Filed by more than one module of the same app'));
    }
    canvas.appendChild(clusterCard(cluster));
  });
}

async function renderUnclustered() {
  canvasTitle.textContent = 'Unclustered defects';
  canvasSub.textContent = 'Nothing has judged these against each other yet. Being here means '
    + 'unexamined, not unique.';
  const findings = await api(ecoUrl('/findings?unclustered=true'));
  if (!findings.length) {
    canvas.appendChild(empty('Every filed defect belongs to a cluster.'));
    return;
  }
  let lastRole = null;
  findings.forEach((f) => {
    if (f.role !== lastRole) {
      lastRole = f.role;
      canvas.appendChild(sectionTitle(f.role));
    }
    canvas.appendChild(findingRow(f));
  });
}

// -- shell ----------------------------------------------------------------------------
function markActive() {
  document.querySelectorAll('.nav-item').forEach((item) => {
    const isApp = item.dataset.view === 'app';
    item.classList.toggle('active', item.dataset.view === view.name
      && (!isApp || item.dataset.arg === view.arg));
  });
}

async function show(name, arg) {
  view = { name, arg: arg || null };
  markActive();
  canvas.innerHTML = '';
  canvasSub.textContent = '';
  try {
    if (name === 'app') await renderApp(arg);
    else if (name === 'clusters') await renderClusters();
    else if (name === 'unclustered') await renderUnclustered();
    else renderOverview();
  } catch (err) {
    canvas.appendChild(el('div', 'canvas-warn', 'Could not load this view: ' + err.message));
  }
}

function renderRail() {
  const board = eco.board;
  ecoName.textContent = board.name;
  ecoHeadline.textContent = `${board.totals.apps} apps · ${board.clusters.distinct} distinct `
    + `defects`;
  ecoHeadline.title = `${board.clusters.filed} filed, ${board.clusters.absorbed} of them `
    + `duplicates of something else`;

  appNav.innerHTML = '';
  board.apps.forEach((app) => {
    const item = el('button', 'nav-item');
    item.dataset.view = 'app';
    item.dataset.arg = app.package;
    item.append(el('span', 'nav-label', app.role));
    if (app.never_run.length) {
      item.appendChild(chip('⚠', 'nav-warn',
                            `${app.never_run.length} module(s) have never been run`));
    }
    item.appendChild(el('span', 'nav-count', String(app.defects)));
    item.addEventListener('click', () => show('app', app.package));
    appNav.appendChild(item);
  });

  document.getElementById('countClusters').textContent = board.clusters.clusters;
  document.getElementById('countUnclustered').textContent = board.unclustered;

  railFoot.textContent = board.supervisor
    ? 'Manager: ' + board.supervisor
    : 'This ecosystem has no manager project.';
  markActive();
}

/** Re-read everything and redraw whatever is on screen. */
async function refresh() {
  Object.keys(cache).forEach((k) => delete cache[k]);
  await loadBoard();
  renderRail();
  await show(view.name, view.arg);
}

function initBoard() {
  document.querySelectorAll('.nav-item[data-view]').forEach((item) => {
    if (item.dataset.view === 'app') return;   // wired per app in renderRail
    item.addEventListener('click', () => show(item.dataset.view));
  });
  document.getElementById('refreshBtn').addEventListener('click', refresh);
}

export { initBoard, refresh, renderRail, show };
