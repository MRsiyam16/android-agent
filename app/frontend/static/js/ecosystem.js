// ecosystem.js — the one thing the cockpit still says about the product it is testing.
//
// This used to be a pill plus a whole sheet listing every cluster across every app. That
// belongs on /manager now: this page is built around one project — a project pill, a module
// list, a phone frame — and a view *across* projects could only ever be a sheet bolted over
// the top of it.
//
// What is left is the one fact that changes how you read the screen you are on: some of what
// this app filed was also filed by another app, so it is not this app's bug to chase alone.
// It counts cross-app clusters **touching the open project**, not the ecosystem's total — a
// number about the whole product is not something the cockpit is the right place to show.

import { agentFetch } from './chat.js';
import { ui } from './state.js';

const pill = document.getElementById('ecosystemPill');
const label = document.getElementById('ecosystemLabel');
const count = document.getElementById('ecosystemCount');

/** How many cross-app clusters include a finding filed by the open project. */
async function refreshEcosystem() {
  if (!pill) return;
  let list;
  try {
    list = await agentFetch('/ecosystems');
  } catch {
    return;   // no ecosystem tagged, or a blip — either way leave the pill alone
  }
  // Which ecosystem the open project belongs to, if any. Derived from the listing rather than
  // hardcoded, so this works on an install whose product is not called metaesthetics.
  const mine = list.find((e) => (e.members || [])
    .some((m) => m.package === ui.currentPackage));
  if (!mine) { pill.hidden = true; return; }

  let clusters = [];
  try {
    clusters = await agentFetch(`/ecosystems/${encodeURIComponent(mine.name)}/clusters`);
  } catch {
    return;
  }
  const shared = clusters.filter((c) => c.scope === 'cross-app'
    && (c.members || []).some((m) => m.package === ui.currentPackage));

  pill.hidden = false;
  pill.href = '/manager';
  count.textContent = shared.length;
  label.textContent = 'Cross-app';
  pill.classList.toggle('has-items', shared.length > 0);
  pill.title = shared.length
    ? `${shared.length} of this app's defects were also filed by another app in ${mine.name}. `
      + 'Open the manager to see them.'
    : `This app is part of ${mine.name}. Nothing it filed has turned up in another app yet. `
      + 'Open the manager for the whole product.';
}

function initEcosystem() {
  if (!pill) return;
  // Deliberately no click handler: it is an ordinary link to another page, and a handler
  // would break middle-click and open-in-new-tab for no gain.
  refreshEcosystem();
}

export { initEcosystem, refreshEcosystem };
