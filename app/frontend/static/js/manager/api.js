// api.js — the server calls this page makes, and the two DOM helpers everything else uses.
//
// Separate from the cockpit's modules on purpose. Those are welded to a page with a device
// frame and a flow graph in it: `chat.js` and `phone.js` look their elements up at module
// scope and bind listeners there, so importing either here would throw on the first missing
// id. The four files in this folder import nothing from ../ except markdown.js, which is a
// leaf and renders what the agent writes the same way on both pages.

/** Which ecosystem this page is showing. `?eco=` overrides; otherwise the first one on disk.
 *  Resolved rather than hardcoded so an install with a differently-named product still works. */
const eco = {
  name: new URLSearchParams(location.search).get('eco') || null,
  supervisor: null,   // the manager project's package — `members` deliberately omits it
  board: null,        // the last /board payload, so a view switch does not refetch
};

/** Every fetch on this page. Throws with the server's `detail` when there is one, so a
 *  failure reads as what went wrong rather than as a bare status code. */
async function api(url, options) {
  const resp = await fetch(url, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error((data && data.detail) || (resp.status + ' from ' + url));
  return data;
}

async function resolveEcosystem() {
  if (eco.name) return eco.name;
  const all = await api('/ecosystems');
  if (!all.length) throw new Error('No project is tagged into an ecosystem yet.');
  eco.name = all[0].name;
  return eco.name;
}

/** Re-read the whole board. One request, so the totals and the rows under them are counted
 *  from the same read of disk — see the route's own comment. */
async function loadBoard() {
  await resolveEcosystem();
  eco.board = await api(`/ecosystems/${encodeURIComponent(eco.name)}/board`);
  eco.supervisor = eco.board.supervisor;
  return eco.board;
}

// -- DOM ------------------------------------------------------------------------------
// textContent everywhere below. Every string on this page came from a finding an agent wrote
// about someone else's app, and innerHTML on that is how a bug title becomes markup.

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function chip(text, className, title) {
  const node = el('span', className, text);
  if (title) node.title = title;
  return node;
}

export { api, chip, el, eco, loadBoard, resolveEcosystem };
