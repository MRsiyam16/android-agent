// ecosystem.js — the view across projects that are one product.
//
// Every other view in this dashboard is scoped to the open project, because until now a
// project was the largest thing the system knew about. This is the one that is not: five
// Metaesthetics apps share a backend, so the same defect gets filed once per app by agents
// that cannot see each other, and 131 filed defects turned out to be 98 distinct ones.
//
// It is a pill plus a sheet rather than a new tab or rail, for the reason written at the top
// of dashboard.html: one workspace. The pill sits with the outcome pills because it is the
// same kind of claim one level up — those say "this project's verdict", this says "this
// product's". It is deliberately read-only: editing membership is the manager tier's job,
// and a cockpit that can also rewrite what it is reporting is harder to trust.

import { agentFetch } from './chat.js';

const NAME = 'metaesthetics';   // the only ecosystem so far; the endpoints are already plural

const pill = document.getElementById('ecosystemPill');
const pillCount = document.getElementById('ecosystemCount');
const backdrop = document.getElementById('ecosystemBackdrop');
const list = document.getElementById('ecosystemList');
const sub = document.getElementById('ecosystemSub');

// Confidence is rendered everywhere a cluster is, because a cluster is a hypothesis about
// somebody else's backend made from the outside — dropping the qualifier would turn "these
// three look like one defect" into "these three are one defect", which is not what was shown.
const CONFIDENCE_TITLE = {
  confirmed: 'The findings’ own evidence says so',
  likely: 'Same mechanism and area, nothing decisive',
  tentative: 'Same shape, possibly separate implementations — check before filing as one',
};

let summary = null;

/** Headline numbers for the pill. Silent on failure, like the outcome pills. */
async function refreshEcosystem() {
  let data;
  try {
    data = await agentFetch(`/ecosystems/${encodeURIComponent(NAME)}`);
  } catch {
    return;   // no ecosystem tagged yet, or a blip — either way leave the pill alone
  }
  summary = data;
  const distinct = (data.clusters && data.clusters.distinct) || 0;
  pillCount.textContent = distinct;
  pill.classList.toggle('has-items', distinct > 0);
  pill.hidden = false;
  const absorbed = (data.clusters && data.clusters.absorbed) || 0;
  pill.title = `${data.apps} apps · ${data.defects} filed defects · ${distinct} distinct `
    + `(${absorbed} duplicates absorbed)`;
}

function chip(text, className, title) {
  const el = document.createElement('span');
  el.className = className;
  el.textContent = text;
  if (title) el.title = title;
  return el;
}

function clusterCard(cluster) {
  const card = document.createElement('div');
  card.className = 'eco-cluster';

  const head = document.createElement('div');
  head.className = 'eco-cluster-head';
  head.append(
    chip(`${cluster.size}×`, 'eco-size'),
    chip(cluster.scope === 'cross-app' ? 'cross-app' : 'single-app',
         `eco-scope scope-${cluster.scope}`,
         cluster.scope === 'cross-app'
           ? 'Filed separately by more than one app — no single project could see this'
           : 'Every report came from one app'),
    chip(cluster.confidence, `eco-confidence conf-${cluster.confidence}`,
         CONFIDENCE_TITLE[cluster.confidence] || ''),
  );
  const title = document.createElement('span');
  title.className = 'eco-cluster-title';
  title.textContent = cluster.title || cluster.id;
  head.appendChild(title);
  if (cluster.resolved) {
    head.appendChild(chip('✓ Resolved', 'eco-resolved',
                          'Every report of this defect is closed'));
  }
  card.appendChild(head);

  if (cluster.root) {
    const root = document.createElement('div');
    root.className = 'eco-cluster-root';
    root.textContent = cluster.root;
    card.appendChild(root);
  }

  const roles = document.createElement('div');
  roles.className = 'eco-roles';
  (cluster.roles || []).forEach((role) => roles.appendChild(chip(role, 'eco-role')));
  card.appendChild(roles);

  (cluster.members || []).forEach((m) => {
    const row = document.createElement('div');
    row.className = 'eco-member';
    row.append(chip(m.role, 'eco-member-role'));
    const ref = document.createElement('span');
    ref.className = 'eco-member-ref';
    ref.textContent = `${m.module_title || m.module} · ${m.finding}`;
    const text = document.createElement('span');
    text.className = 'eco-member-title';
    text.textContent = m.title || '';
    row.append(ref, text);
    if (m.resolved) row.appendChild(chip('✓', 'eco-member-resolved', 'Closed'));
    if (m.issue_url) {
      const link = document.createElement('a');
      link.className = 'eco-member-link';
      link.href = m.issue_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = '↗';
      link.title = 'Open the tracked issue';
      row.appendChild(link);
    }
    card.appendChild(row);
  });

  // An orphan is a member whose finding is no longer on disk. Shown rather than dropped: a
  // cluster that shrinks quietly is how a wrong saving becomes believable.
  (cluster.orphans || []).forEach((o) => {
    const row = document.createElement('div');
    row.className = 'eco-member eco-orphan';
    row.textContent = `${o.package} · ${o.module} · ${o.finding} — no longer on disk`;
    card.appendChild(row);
  });

  return card;
}

function appRow(member) {
  const row = document.createElement('div');
  row.className = 'eco-app';
  row.append(chip(member.role, 'eco-role'), chip(member.platform, 'eco-platform'));
  const pkg = document.createElement('span');
  pkg.className = 'eco-app-pkg';
  pkg.textContent = member.package;
  row.appendChild(pkg);
  return row;
}

async function openEcosystem() {
  await refreshEcosystem();
  list.innerHTML = '';
  if (!summary) {
    sub.textContent = 'No ecosystem is tagged yet.';
    backdrop.classList.add('open');
    return;
  }

  const c = summary.clusters || {};
  sub.textContent = `${summary.apps} apps · ${c.filed || 0} filed defects → `
    + `${c.distinct || 0} distinct (${c.absorbed || 0} absorbed by ${c.clusters || 0} `
    + `clusters, ${c.cross_app || 0} of them cross-app)`;

  const apps = document.createElement('div');
  apps.className = 'eco-apps';
  (summary.members || []).forEach((m) => apps.appendChild(appRow(m)));
  list.appendChild(apps);

  let clusters = [];
  try {
    clusters = await agentFetch(`/ecosystems/${encodeURIComponent(NAME)}/clusters`);
  } catch {
    const err = document.createElement('div');
    err.className = 'outcome-empty';
    err.textContent = 'Could not load the clusters.';
    list.appendChild(err);
    backdrop.classList.add('open');
    return;
  }

  if (!clusters.length) {
    const empty = document.createElement('div');
    empty.className = 'outcome-empty';
    empty.textContent = 'No duplicate defects found across these apps yet.';
    list.appendChild(empty);
  }

  // The API already sorts cross-app first; the heading just names the boundary so the two
  // groups do not read as one long list where the interesting half is buried.
  let lastScope = null;
  clusters.forEach((cluster) => {
    if (cluster.scope !== lastScope) {
      lastScope = cluster.scope;
      const heading = document.createElement('div');
      heading.className = 'eco-group-title';
      heading.textContent = cluster.scope === 'cross-app'
        ? 'Cross-app — one defect, filed by more than one app'
        : 'Single-app — one defect, filed by more than one module';
      list.appendChild(heading);
    }
    list.appendChild(clusterCard(cluster));
  });

  backdrop.classList.add('open');
}

function initEcosystem() {
  if (!pill) return;
  pill.addEventListener('click', openEcosystem);
  document.getElementById('ecosystemClose')
    .addEventListener('click', () => backdrop.classList.remove('open'));
  backdrop.addEventListener('click', (e) => {
    if (e.target === backdrop) backdrop.classList.remove('open');
  });
  refreshEcosystem();
}

export { initEcosystem, openEcosystem, refreshEcosystem };
