// board.js — the fleet board: five apps at once, and the defects that belong to more than one.
//
// Four views behind one canvas, chosen from the left rail. They are views and not pages
// because the question you are answering is always the same one — where does this product
// stand — and only the depth changes: the whole product, one app, the groupings, the queue.
//
// Two rules run through the rendering here:
//
//   * **Nothing shows a number it cannot stand behind.** An app nobody has surveyed gets a
//     hatched bar and no percentage, because "1 of 1 modules tested" is not 100% done.
//   * **Detail is opt-in.** Six clusters printing every member inline was 60 lines of the
//     overview and unreadable. A cluster collapses to its claim; the evidence is a click away.

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
const DEFECT_KINDS = ['bug', 'warning', 'suggestion'];

const NOT_SURVEYED =
  'Nothing has ever explored this app, so its module list is whatever happens to have been '
  + 'created — there is no denominator to be a fraction of. A percentage here would be '
  + 'measuring the list against itself.';

let view = { name: 'overview', arg: null };
let kindFilter = null;      // null = every defect kind
const cache = {};           // lazily-fetched lists, dropped wholesale on refresh

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

/** A coverage bar. `unknownApps` adds the third state — some amount nobody has scoped.
 *
 *  The unknown segment is a fixed slice, not a measured one, and says so on hover: we do not
 *  know how many modules an unsurveyed app has, and drawing a width for it would be inventing
 *  the very number the segment exists to say we lack.
 */
function coverageBar(cov, unknownApps) {
  const wrap = el('div', 'bar');
  const unknownShare = unknownApps && unknownApps.length ? 0.16 : 0;
  const known = 1 - unknownShare;

  if (cov.percent === null || cov.percent === undefined) {
    // Nothing measurable at all — the whole bar is the unknown state.
    const none = el('div', 'bar-unknown');
    none.style.width = '100%';
    none.title = NOT_SURVEYED;
    wrap.appendChild(none);
    return wrap;
  }

  const done = el('div', 'bar-done');
  done.style.width = `${(cov.percent / 100) * known * 100}%`;
  done.title = `${cov.tested} of ${cov.testable} modules tested`;
  wrap.appendChild(done);

  const todo = el('div', 'bar-todo');
  todo.style.width = `${(1 - cov.percent / 100) * known * 100}%`;
  todo.title = `${cov.testable - cov.tested} modules exist and have not been run`;
  wrap.appendChild(todo);

  if (unknownShare) {
    const unknown = el('div', 'bar-unknown');
    unknown.style.width = `${unknownShare * 100}%`;
    unknown.title = `Not to scale — ${unknownApps.join(', ')} `
      + (unknownApps.length === 1 ? 'has' : 'have') + ' never been surveyed, so how much work '
      + 'is behind this is unknown. It is drawn at a fixed width because inventing one would '
      + 'be claiming the number this segment exists to say we do not have.';
    wrap.appendChild(unknown);
  }
  return wrap;
}

const DEFECT_SEGMENTS = [
  ['resolved', 'seg-resolved', 'closed — the fix is in'],
  ['bug', 'seg-bug', 'open bugs'],
  ['warning', 'seg-warning', 'open warnings'],
  ['suggestion', 'seg-suggestion', 'open suggestions'],
];

/** A defect bar: what was found, and how much of it is fixed.
 *
 *  A *second* bar rather than more colours on the coverage one, because they count different
 *  things. Coverage is modules; this is findings. "6 of 13 tested" and "31 bugs" share no
 *  denominator, and segments that count different units cannot be read as one proportion.
 *
 *  `incomplete` — apps with modules nobody has run — adds the same hatched cap the coverage
 *  bar uses, meaning the defects nobody has looked for yet. It is the one honest way to say
 *  "and there is more" without inventing how much.
 */
function defectBar(status, incomplete) {
  const wrap = el('div', 'bar');
  const total = DEFECT_SEGMENTS.reduce((n, [key]) => n + (status[key] || 0), 0);
  const unknownShare = incomplete && incomplete.length ? 0.16 : 0;
  const known = 1 - unknownShare;

  if (!total) {
    const none = el('div', unknownShare ? 'bar-unknown' : 'bar-clean');
    none.style.width = '100%';
    none.title = unknownShare
      ? 'Nothing filed, and there are modules nobody has run — this is absence of testing, '
        + 'not evidence of health.'
      : 'Nothing filed against an app whose modules have all been run.';
    wrap.appendChild(none);
    return wrap;
  }

  DEFECT_SEGMENTS.forEach(([key, className, label]) => {
    const n = status[key] || 0;
    if (!n) return;
    const seg = el('div', className);
    seg.style.width = `${(n / total) * known * 100}%`;
    seg.title = `${n} ${label}`;
    wrap.appendChild(seg);
  });

  if (unknownShare) {
    const unknown = el('div', 'bar-unknown');
    unknown.style.width = `${unknownShare * 100}%`;
    unknown.title = 'Not to scale — ' + incomplete.join(', ')
      + (incomplete.length === 1 ? ' has' : ' have')
      + ' modules nobody has run, so there are defects here that have not been found yet. '
      + 'Drawn at a fixed width because how many is exactly what is unknown.';
    wrap.appendChild(unknown);
  }
  return wrap;
}

function defectLegend(status, incomplete) {
  const legend = el('div', 'coverage-legend');
  DEFECT_SEGMENTS.forEach(([key, className, label]) => {
    const n = status[key] || 0;
    if (!n) return;
    legend.appendChild(chip(`${n} ${key === 'resolved' ? 'fixed' : key + (n === 1 ? '' : 's')}`,
                            `legend legend-${key}`, `${n} ${label}`));
  });
  if (incomplete && incomplete.length) {
    legend.appendChild(chip('? not yet found', 'legend legend-unknown',
                            incomplete.join(', ') + ' still have modules nobody has run.'));
  }
  return legend;
}

// Which disclosures are open, by key. Survives a redraw: the board refreshes itself whenever
// any module in the product files a finding, and a cluster you had expanded snapping shut
// under you — mid-read, because something unrelated happened in another app — is the thing
// that makes a live-updating page worse than a static one.
const openRows = new Set();

/** A collapsed row that reveals its detail on click. Built once, filled once, then toggled —
 *  so opening the same cluster twice does not re-render it. */
function disclosure(head, buildBody, openLabel, key) {
  const box = el('div', 'disc');
  const toggle = el('button', 'disc-head');
  toggle.append(head);
  const caret = el('span', 'disc-caret', '▸');
  const label = el('span', 'disc-label', openLabel || '');
  toggle.append(label, caret);
  const body = el('div', 'disc-body');
  body.hidden = true;
  let built = false;

  const setOpen = (open) => {
    if (open && !built) { buildBody(body); built = true; }
    body.hidden = !open;
    box.classList.toggle('open', open);
    caret.textContent = open ? '▾' : '▸';
    if (!key) return;
    if (open) openRows.add(key);
    else openRows.delete(key);
  };

  toggle.addEventListener('click', () => setOpen(body.hidden));
  if (key && openRows.has(key)) setOpen(true);
  box.append(toggle, body);
  return box;
}

function memberRow(m) {
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
  return row;
}

function clusterCard(cluster) {
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
  // The apps stay on the collapsed row: which apps a defect spans is the thing you scan for,
  // and it is the one detail that does not fit in the title.
  const roles = el('div', 'cluster-roles');
  (cluster.roles || []).forEach((r) => roles.appendChild(chip(r, 'member-role')));
  head.appendChild(roles);

  const orphanCount = (cluster.orphans || []).length;
  return disclosure(head, (body) => {
    if (cluster.root) body.appendChild(el('div', 'cluster-root', cluster.root));
    (cluster.members || []).forEach((m) => body.appendChild(memberRow(m)));
    // An orphan is a member whose finding is no longer on disk. Shown rather than dropped: a
    // cluster that shrinks quietly is how a wrong grouping becomes believable.
    (cluster.orphans || []).forEach((o) => {
      body.appendChild(el('div', 'member orphan',
                          `${o.package} · ${o.module} · ${o.finding} — no longer on disk`));
    });
  }, `${cluster.size} report${cluster.size === 1 ? '' : 's'}`
     + (orphanCount ? ` · ${orphanCount} orphaned` : ''), 'cluster:' + cluster.id);
}

function findingRow(f) {
  const row = el('div', 'finding');
  row.append(chip(f.kind, `finding-kind k-${f.kind}`),
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

function countsOf(findings) {
  const out = {};
  findings.forEach((f) => { out[f.kind] = (out[f.kind] || 0) + 1; });
  return out;
}

/** The bug/warning/suggestion switch above a defect list. */
function kindFilterRow(findings, onChange) {
  const row = el('div', 'filters');
  const counts = countsOf(findings);
  const make = (kind, label) => {
    const n = kind ? (counts[kind] || 0) : findings.length;
    const btn = el('button', 'filter-btn' + (kindFilter === kind ? ' active' : ''),
                   `${label} ${n}`);
    if (kind) btn.classList.add(`k-${kind}`);
    btn.addEventListener('click', () => { kindFilter = kind; onChange(); });
    return btn;
  };
  row.append(make(null, 'All'));
  DEFECT_KINDS.forEach((k) => row.appendChild(make(k, k.charAt(0).toUpperCase() + k.slice(1))));
  return row;
}

function applyFilter(findings) {
  return kindFilter ? findings.filter((f) => f.kind === kindFilter) : findings;
}

// -- views ----------------------------------------------------------------------------
function appCard(app) {
  const card = el('button', 'app-card');
  card.addEventListener('click', () => show('app', app.package));

  const head = el('div', 'app-card-head');
  head.append(el('span', 'app-role', app.role), chip(app.platform, 'app-platform'));
  card.appendChild(head);

  const cov = app.coverage;
  const status = app.defect_status;
  const open = status.bug + status.warning + status.suggestion;
  const incomplete = (!cov.surveyed || app.never_run.length) ? [app.role] : null;

  // Open, not filed. The number that matters is what is still wrong, and a card headlining
  // "24 defects" reads worse after a fix week than before it.
  const stat = el('div', 'app-stat');
  stat.append(el('b', null, String(open)), el('span', null, ' open'));
  if (status.resolved) {
    stat.appendChild(chip(`${status.resolved} fixed`, 'app-fixed',
                          `${status.resolved} of this app's defects are closed in Blackcode`));
  }
  card.appendChild(stat);

  card.appendChild(defectBar(status, incomplete));
  card.appendChild(coverageBar(cov));
  card.appendChild(el('div', 'app-modules', cov.surveyed
    ? `${cov.tested} of ${cov.testable} modules tested`
    : `${cov.testable} module${cov.testable === 1 ? '' : 's'} · never surveyed`));

  // The distinction worth protecting on a board where low numbers look like good news: an app
  // with no defects may be one that works, or one nobody has run. Only one of those is good.
  if (!cov.surveyed) {
    card.appendChild(chip('coverage unknown', 'app-warn', NOT_SURVEYED));
  } else if (app.never_run.length) {
    card.appendChild(chip(`${app.never_run.length} never run`, 'app-warn',
                          'These modules have filed nothing because nothing opened them, '
                          + 'not because they passed: ' + app.never_run.join(', ')));
  }
  return card;
}

function panelHead(title, figure, unit) {
  const head = el('div', 'coverage-head');
  head.appendChild(el('span', 'coverage-title', title));
  const fig = el('span', 'coverage-figure', figure);
  if (unit) fig.appendChild(el('small', null, ' ' + unit));
  head.appendChild(fig);
  return head;
}

/** Two bars, side by side, each labelled with what it counts.
 *
 *  They answer different questions and are never the same shape: an app can be fully tested
 *  with everything still broken, or barely tested with the little it found already fixed.
 *  Reading one as the other is the mistake the labels exist to prevent.
 */
function coveragePanel(board) {
  const cov = board.totals.coverage;
  const status = board.totals.defect_status;
  const incomplete = board.totals.incomplete;
  const open = status.bug + status.warning + status.suggestion;

  const panel = el('div', 'coverage-panel two-up');

  const left = el('div', 'coverage-col');
  left.append(
    panelHead('Coverage', cov.percent === null ? '—' : `${cov.percent}%`, 'of modules'),
    coverageBar(cov, cov.unsurveyed));
  const covLegend = el('div', 'coverage-legend');
  covLegend.append(
    chip(`${cov.tested} tested`, 'legend legend-done'),
    chip(`${cov.testable - cov.tested} waiting`, 'legend legend-todo'));
  if (cov.unsurveyed.length) {
    covLegend.appendChild(chip(`? ${cov.unsurveyed.join(', ')}`, 'legend legend-unknown',
                               NOT_SURVEYED));
  }
  left.appendChild(covLegend);
  if (cov.unsurveyed.length) {
    left.appendChild(el('div', 'coverage-note',
      `Of the ${cov.testable} modules somebody has actually scoped. `
      + `${cov.unsurveyed.join(', ')} ${cov.unsurveyed.length === 1 ? 'is' : 'are'} not in `
      + 'that number at all, so this is a floor, not a score.'));
  }

  const right = el('div', 'coverage-col');
  right.append(
    panelHead('Defects', String(open), open === 1 ? 'still open' : 'still open'),
    defectBar(status, incomplete),
    defectLegend(status, incomplete));
  if (status.resolved) {
    right.appendChild(el('div', 'coverage-note',
      `${status.resolved} of ${status.resolved + open} filed defects are closed in Blackcode. `
      + 'Run `sync_issue_status` from the manager to re-check — a defect fixed weeks ago '
      + 'still counts as open here until somebody asks.'));
  }

  panel.append(left, right);
  return panel;
}

function renderOverview() {
  const board = eco.board;
  canvasTitle.textContent = 'The whole product';
  const c = board.clusters;
  canvasSub.textContent =
    `${board.totals.apps} apps · ${c.filed} filed defects → ${c.distinct} distinct `
    + `(${c.absorbed} absorbed by ${c.clusters} clusters, ${c.cross_app} of them cross-app)`;

  canvas.appendChild(coveragePanel(board));

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
  const cov = app.coverage;

  canvasTitle.textContent = app.role;
  canvasSub.textContent = `${app.package} · ${app.platform}`;

  const status = app.defect_status;
  const open = status.bug + status.warning + status.suggestion;
  const incomplete = (!cov.surveyed || app.never_run.length) ? [app.role] : null;

  const panel = el('div', 'coverage-panel two-up');
  const left = el('div', 'coverage-col');
  left.append(
    panelHead('Coverage', cov.percent === null ? 'unknown' : `${cov.percent}%`, 'of modules'),
    coverageBar(cov, cov.surveyed ? null : [app.role]),
    el('div', 'coverage-note', cov.surveyed
      ? `${cov.tested} of ${cov.testable} modules tested.`
      : NOT_SURVEYED));

  const right = el('div', 'coverage-col');
  right.append(panelHead('Defects', String(open), 'still open'),
               defectBar(status, incomplete),
               defectLegend(status, incomplete));
  panel.append(left, right);
  canvas.appendChild(panel);

  const [modules, findings] = await Promise.all([
    cached('modules', ecoUrl('/modules')),
    cached('findings', ecoUrl('/findings')),
  ]);

  // Untested first: the point of this list is what is left, not what is finished.
  const mods = modules.filter((m) => m.package === pkg)
    .sort((a, b) => (a.status === 'tested') - (b.status === 'tested'));
  canvas.appendChild(sectionTitle('Modules', `${mods.length} in this app`));
  if (!mods.length) canvas.appendChild(empty('This app has no modules yet.'));
  mods.forEach((mod) => {
    const row = el('div', 'module-row');
    row.append(chip(mod.status, `module-status st-${mod.status}`),
               el('span', 'module-title', mod.title));
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
  if (!mine.length) { canvas.appendChild(empty('Nothing filed against this app.')); return; }
  canvas.appendChild(kindFilterRow(mine, () => show('app', pkg)));
  applyFilter(mine).forEach((f) => canvas.appendChild(findingRow(f)));
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
      const group = clusters.filter((c) => c.scope === cluster.scope);
      canvas.appendChild(sectionTitle(
        cluster.scope === 'cross-app' ? 'Cross-app' : 'Single-app',
        `${group.length} · ${group.reduce((n, c) => n + c.size, 0)} reports · `
        + (cluster.scope === 'cross-app'
          ? 'filed by more than one app'
          : 'filed by more than one module of the same app')));
    }
    canvas.appendChild(clusterCard(cluster));
  });
}

async function renderUnclustered() {
  canvasTitle.textContent = 'Unclustered defects';
  canvasSub.textContent = 'Nothing has judged these against each other yet. Being here means '
    + 'unexamined, not unique.';
  const all = await api(ecoUrl('/findings?unclustered=true'));
  if (!all.length) {
    canvas.appendChild(empty('Every filed defect belongs to a cluster.'));
    return;
  }
  canvas.appendChild(kindFilterRow(all, () => show('unclustered')));
  const findings = applyFilter(all);

  // Grouped and collapsed. Eighty-two rows in one flat list is a list you scroll past, not
  // one you work through — and which app a defect came from is the first thing you sort by
  // when looking for a duplicate.
  const byRole = {};
  findings.forEach((f) => { (byRole[f.role] = byRole[f.role] || []).push(f); });
  Object.keys(byRole).sort().forEach((role) => {
    const rows = byRole[role];
    const head = el('div', 'group-head');
    head.append(el('span', 'group-role', role), tally(countsOf(rows), DEFECT_KINDS));
    canvas.appendChild(disclosure(head, (body) => {
      rows.forEach((f) => body.appendChild(findingRow(f)));
    }, `${rows.length}`, 'role:' + role));
  });
}

async function renderRetests() {
  canvasTitle.textContent = 'Re-tests';
  canvasSub.textContent = 'Runs the manager wants because something was marked fixed. They '
    + 'wait for you because it cannot see whether the fix reached the build under test — and '
    + '"closed" in a tracker covers fixed, duplicate and will-not-do.';

  const rows = await api(ecoUrl('/retests'));
  if (!rows.length) {
    canvas.appendChild(empty('Nothing queued. Run sync_issue_status from the manager to check '
                             + 'Blackcode for defects that have been closed.'));
    return;
  }

  const groups = [['pending', 'Waiting for you'], ['approved', 'Approved and started'],
                  ['dismissed', 'Dismissed']];
  groups.forEach(([status, label]) => {
    const mine = rows.filter((r) => r.status === status);
    if (!mine.length) return;
    canvas.appendChild(sectionTitle(label, `${mine.length}`));
    mine.forEach((entry) => canvas.appendChild(retestRow(entry)));
  });
}

// -- sweeps ---------------------------------------------------------------------------
//
// A campaign runs for hours, module by module, entirely between the manager's turns. This is
// where you find out what it is doing without asking it — every step, in order, with the one
// that is running now marked and the reason for any pause spelled out rather than implied.

const STEP_LABEL = {
  running: 'running now', done: 'done', failed: 'failed',
  skipped: 'skipped', pending: 'waiting',
};

async function renderCampaigns() {
  canvasTitle.textContent = 'Sweeps';
  canvasSub.textContent = 'A whole app tested module by module. Each module starts when the '
    + 'one before it finishes — nobody has to say "next". The manager is handed a turn only '
    + 'when a module fails, when one stops to ask you something, or when the sweep is done.';

  const rows = await api(ecoUrl('/campaigns'));
  if (!rows.length) {
    canvas.appendChild(empty('No app has been swept yet. Ask the manager to "test the clinic '
                             + 'web" and it will run every module in order.'));
    return;
  }
  rows.forEach((campaign) => canvas.appendChild(campaignCard(campaign)));
}

function campaignCard(campaign) {
  const box = el('div', 'campaign' + (campaign.status === 'running' ? ' live' : ''));

  const head = el('div', 'campaign-head');
  head.append(chip(campaign.role || campaign.package, 'member-role'),
              chip(campaign.status, 'campaign-status k-' + campaign.status),
              el('span', 'campaign-count',
                 `${campaign.finished}/${campaign.total} modules · ${campaign.findings} findings`));
  box.appendChild(head);
  if (campaign.goal) box.appendChild(el('div', 'campaign-goal', campaign.goal));

  // The pause reason, loud. A sweep that stopped for something only the user can clear is the
  // one state where saying nothing costs hours.
  if (campaign.blocked) {
    const warn = el('div', 'campaign-blocked');
    warn.appendChild(el('div', 'campaign-blocked-why',
                        `Paused — ${campaign.blocked.reason || 'needs a decision'}`
                        + (campaign.blocked.module ? ` at ${campaign.blocked.module}` : '')));
    if (campaign.blocked.question) {
      warn.appendChild(el('div', 'campaign-blocked-q', campaign.blocked.question));
    }
    box.appendChild(warn);
  }

  const list = el('div', 'campaign-steps');
  (campaign.steps || []).forEach((step) => {
    const row = el('div', 'campaign-step s-' + step.status);
    row.append(el('span', 'campaign-step-name', step.module),
               el('span', 'campaign-step-state', STEP_LABEL[step.status] || step.status));
    if (step.findings) {
      row.appendChild(el('span', 'campaign-step-findings', `${step.findings} findings`));
    }
    if (step.note) row.appendChild(el('span', 'campaign-step-note', step.note));
    list.appendChild(row);
  });
  box.appendChild(list);

  if (campaign.status === 'running' || campaign.status === 'paused') {
    const actions = el('div', 'campaign-actions');
    if (campaign.status === 'paused') {
      const resume = el('button', 'chip-btn', '▶ Resume');
      resume.addEventListener('click', () => campaignAction(campaign, 'resume', resume));
      actions.appendChild(resume);
    }
    const stop = el('button', 'chip-btn danger', '■ Stop sweep');
    stop.addEventListener('click', () => campaignAction(campaign, 'stop', stop));
    actions.appendChild(stop);
    box.appendChild(actions);
  }
  return box;
}

async function campaignAction(campaign, action, button) {
  button.disabled = true;
  try {
    await api(ecoUrl(`/campaigns/${encodeURIComponent(campaign.id)}/${action}`),
              { method: 'POST' });
    await refresh();
  } catch (err) {
    button.disabled = false;
    button.title = err.message;
  }
}

/** The topbar strip. Driven by the board payload and by live `agent_campaign` events, so it
 *  moves as a sweep moves rather than at the refresh debounce. */
function renderCampaignStrip(summary) {
  const strip = document.getElementById('campaignStrip');
  const text = document.getElementById('campaignText');
  const bar = document.getElementById('campaignBar');
  if (!strip) return;

  const live = (summary && summary.campaigns) || [];
  if (!live.length) {
    strip.hidden = true;
    return;
  }
  const first = live[0];
  const blocked = Boolean(first.blocked) || first.status === 'paused';
  strip.hidden = false;
  strip.classList.toggle('paused', blocked);
  text.textContent = live.length > 1
    ? `${live.length} sweeps running · ${first.role} ${first.finished}/${first.total}`
    : `${first.role} · ${first.finished}/${first.total}`
      + (first.current ? ` · ${first.current}` : '')
      + (blocked ? ' · needs you' : '');
  const pct = first.total ? Math.round((first.finished / first.total) * 100) : 0;
  bar.style.width = `${pct}%`;
  strip.title = blocked
    ? `Paused: ${(first.blocked || {}).reason || 'needs a decision'}`
    : `Testing ${first.role} — ${first.current || 'starting'}`;
}

function retestRow(entry) {
  const box = el('div', 'retest' + (entry.status === 'pending' ? ' pending' : ''));

  const head = el('div', 'retest-head');
  head.append(chip(entry.role || entry.package, 'member-role'),
              el('span', 'retest-ref', `${entry.module} · ${entry.finding}`),
              el('span', 'retest-title', entry.title || ''));
  if (entry.issue_url) {
    const link = el('a', 'member-link', `#${entry.issue_id} ↗`);
    link.href = entry.issue_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    head.appendChild(link);
  }
  box.appendChild(head);
  box.appendChild(el('div', 'retest-reason', entry.reason || ''));

  if (entry.status !== 'pending') {
    box.appendChild(el('div', 'retest-decided',
                       `${entry.status} ${entry.decided_at || ''}`));
    return box;
  }

  const actions = el('div', 'retest-actions');
  const approve = el('button', 'chip-btn accent', '▶ Approve & run');
  const dismiss = el('button', 'chip-btn', 'Dismiss');
  const note = el('span', 'retest-note', '');

  // Both disabled on the first click. Approving starts a real run on a real device; a double
  // click would send the module a second instruction mid-turn, which the lock refuses — so
  // the visible outcome would be an error about a run you successfully started.
  const lock = () => { approve.disabled = true; dismiss.disabled = true; };
  const unlock = () => { approve.disabled = false; dismiss.disabled = false; };

  approve.addEventListener('click', async () => {
    lock();
    note.textContent = 'starting…';
    try {
      await api(ecoUrl(`/retests/${encodeURIComponent(entry.id)}/approve`), { method: 'POST' });
      scheduleRefresh(300);
    } catch (err) {
      note.textContent = err.message;
      note.className = 'retest-note failed';
      unlock();
    }
  });
  dismiss.addEventListener('click', async () => {
    lock();
    try {
      await api(ecoUrl(`/retests/${encodeURIComponent(entry.id)}/dismiss`), { method: 'POST' });
      scheduleRefresh(300);
    } catch (err) {
      note.textContent = err.message;
      unlock();
    }
  });

  actions.append(approve, dismiss, note);
  box.appendChild(actions);
  return box;
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
  // Normalised first: the filter row re-renders by calling show() with no arg, and an
  // `undefined` that does not equal the stored `null` would read as a view change and clear
  // the filter the click just set.
  arg = arg || null;
  // Leaving a defect list drops its filter: a filter you cannot see is one you forget is on,
  // and the next list would silently open showing a third of itself.
  if (name !== view.name || arg !== view.arg) kindFilter = null;
  view = { name, arg };
  markActive();
  canvas.innerHTML = '';
  canvasSub.textContent = '';
  try {
    if (name === 'app') await renderApp(arg);
    else if (name === 'clusters') await renderClusters();
    else if (name === 'unclustered') await renderUnclustered();
    else if (name === 'retests') await renderRetests();
    else if (name === 'campaigns') await renderCampaigns();
    else renderOverview();
  } catch (err) {
    canvas.appendChild(el('div', 'canvas-warn', 'Could not load this view: ' + err.message));
  }
}

function renderRail() {
  const board = eco.board;
  ecoName.textContent = board.name;
  const cov = board.totals.coverage;
  ecoHeadline.textContent = `${board.totals.apps} apps · ${board.clusters.distinct} distinct `
    + 'defects';
  ecoHeadline.title = `${board.clusters.filed} filed, ${board.clusters.absorbed} of them `
    + `duplicates of something else. ${cov.tested} of ${cov.testable} scoped modules tested.`;

  appNav.innerHTML = '';
  board.apps.forEach((app) => {
    const item = el('button', 'nav-item nav-app');
    item.dataset.view = 'app';
    item.dataset.arg = app.package;

    const top = el('div', 'nav-app-top');
    top.appendChild(el('span', 'nav-label', app.role));
    if (!app.coverage.surveyed) {
      top.appendChild(chip('?', 'nav-warn', NOT_SURVEYED));
    } else if (app.never_run.length) {
      top.appendChild(chip('⚠', 'nav-warn',
                           `${app.never_run.length} module(s) have never been run`));
    }
    const status = app.defect_status;
    const open = status.bug + status.warning + status.suggestion;
    const incomplete = (!app.coverage.surveyed || app.never_run.length) ? [app.role] : null;

    const count = el('span', 'nav-count', String(open));
    count.title = `${open} open · ${status.resolved} fixed`;
    top.appendChild(count);
    item.appendChild(top);

    item.appendChild(defectBar(status, incomplete));
    item.appendChild(coverageBar(app.coverage));
    item.appendChild(el('div', 'nav-sub',
      (app.coverage.surveyed
        ? `${app.coverage.tested}/${app.coverage.testable} tested`
        : 'not surveyed')
      + (status.resolved ? ` · ${status.resolved} fixed` : '')));

    item.addEventListener('click', () => show('app', app.package));
    appNav.appendChild(item);
  });

  document.getElementById('countClusters').textContent = board.clusters.clusters;
  document.getElementById('countUnclustered').textContent = board.unclustered;

  // The pending count, not the total: the queue is a thing to act on, and a badge that keeps
  // climbing as you approve things is one you stop reading.
  const sweepPill = document.getElementById('countCampaigns');
  const sweeps = (board.campaigns || {}).live || 0;
  sweepPill.textContent = sweeps;
  sweepPill.classList.toggle('nav-count-live', sweeps > 0);
  sweepPill.title = sweeps
    ? `${sweeps} app(s) being tested module by module right now`
    : 'No sweep running';
  renderCampaignStrip(board.campaigns);

  const retestPill = document.getElementById('countRetests');
  const pending = (board.retests || {}).pending || 0;
  retestPill.textContent = pending;
  retestPill.classList.toggle('nav-count-due', pending > 0);
  retestPill.title = pending
    ? `${pending} re-test(s) waiting for you to approve or dismiss`
    : 'Nothing waiting';

  railFoot.textContent = board.supervisor
    ? 'Manager: ' + board.supervisor
    : 'This ecosystem has no manager project.';
  markActive();
}

/** Re-read everything and redraw whatever is on screen.
 *
 *  Called on the button and, more often, by itself: a module filing a finding in any app in
 *  this product refreshes the board. That makes scroll position and open rows part of the
 *  contract rather than a nicety — a page that jumps to the top every time some other agent
 *  files a bug is one you cannot read while anything is running.
 */
async function refresh() {
  const scroll = canvas.scrollTop;
  Object.keys(cache).forEach((k) => delete cache[k]);
  await loadBoard();
  renderRail();
  await show(view.name, view.arg);   // same view, so any filter you set survives the refresh
  canvas.scrollTop = scroll;
}

/** Coalesce a burst of events into one redraw.
 *
 *  A run files findings in clusters of several seconds. One refresh per finding would be a
 *  full board re-read per bug, and the board is five projects' findings files.
 */
let refreshTimer = null;

function scheduleRefresh(delay = 1500) {
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    refresh().catch(() => { /* a failed auto-refresh must not break the page */ });
  }, delay);
}

/** Whether an event from this project should redraw the board — i.e. it is one of ours. */
function isOurs(package_) {
  if (!package_ || !eco.board) return false;
  if (package_ === eco.board.supervisor) return true;
  return eco.board.apps.some((app) => app.package === package_);
}

function initBoard() {
  document.querySelectorAll('.nav-item[data-view]').forEach((item) => {
    if (item.dataset.view === 'app') return;   // wired per app in renderRail
    item.addEventListener('click', () => show(item.dataset.view));
  });
  document.getElementById('refreshBtn').addEventListener('click', refresh);
  // The strip is the only thing on screen while a sweep runs, so it has to be the way in.
  document.getElementById('campaignStrip').addEventListener('click', () => show('campaigns'));
}

export { initBoard, isOurs, refresh, renderCampaignStrip, renderRail,
         scheduleRefresh, show };
