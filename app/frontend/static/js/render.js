// render.js — extracted verbatim from the old dashboard.js IIFE.

import { escapeHtml } from './markdown.js';
import { renderComments } from './comments.js';
import { openModal } from './modal.js';
import { renderTextNotes } from './notes.js';
import { flowLevel, renderSectionNotes } from './sectionnotes.js';
import { CARD_H, CARD_W, nodeHeaderLabel, resetCardAspect, sectionLayout, setCardAspect } from './sections.js';
import { PLACEHOLDER_IMG, cardElements, edgeMeta, nodeMeta, nodeStatus, nodesData, sections, ui } from './state.js';
import { cardTargetWidth, requestScaled, shotAspect, truncate } from './util.js';

// The node box vis lays out and hit-tests by. Read the note in layout.js: a node is an
// invisible transparent image, and vis sizes it to that image's native pixels, so this
// is how the card's shape is declared.
function cardBoxImage() {
  const c = document.createElement('canvas');
  c.width = CARD_W;
  c.height = CARD_H;
  return c.toDataURL('image/png');
}
let TRANSPARENT_PLACEHOLDER = cardBoxImage();

const container = document.getElementById('network');
const network = new vis.Network(container, { nodes: nodesData }, {
  layout: { improvedLayout: false },
  physics: false,
  interaction: { hover: false, dragNodes: false, zoomView: true, dragView: true, selectable: false },
  nodes: {
    shape: 'image',
    image: TRANSPARENT_PLACEHOLDER,
    shapeProperties: { useImageSize: true },
    borderWidth: 0,
  },
});

// ---------------------------------------------------------------------------
// Card shape
//
// 9:20 is the default and it is right for every device this harness has run against, so
// the common path never gets here. It exists for the other ones — a 16:9 phone or a
// tablet — where leaving the box at 9:20 would letterbox every screen instead of cropping
// it. Better than a crop, but still not the screen.
//
// Once per board, off the first screenshot that decodes. Not per screenshot: a project
// tested across two devices has two ratios in it, and re-shaping on each would leave the
// board reflowing under the reader. The odd ones out letterbox, which is what `contain`
// in graph.css is for.
//
// Re-measuring is why this is once-only rather than free. vis fixes a node's size when
// its image loads and then never revisits it: handing setOptions a new default image does
// nothing to nodes that already exist (measured — the box stayed 150x300), so each node
// has to be given the new image by hand, and vis reports a 50x50 box for the frame or two
// its decode takes. vis redraws when the image lands, and that redraw drives our own
// overlay through the afterDrawing hook below, so the cards catch up on their own.
//
// Node *positions* are deliberately left alone. A live run relayouts on every new screen,
// so it picks the new width up by itself; a loaded board's positions were saved by hand
// and are not ours to recompute — relayoutAll here would throw away the arrangement the
// board was dragged into. The cost is that a board loaded from an unusually shaped device
// sits at the spacing the old width implied, which is a slightly wider or tighter gutter
// and nothing more.
let aspectAdopted = false;

function adoptShotAspect(hash) {
  if (aspectAdopted) return;
  const ratio = shotAspect.get(hash);
  if (!ratio) return;
  aspectAdopted = true;
  if (!setCardAspect(ratio)) return;   // already the right shape — nothing to reflow
  reshapeNodeBoxes();
}

function reshapeNodeBoxes() {
  TRANSPARENT_PLACEHOLDER = cardBoxImage();
  network.setOptions({ nodes: { image: TRANSPARENT_PLACEHOLDER } });
  const ids = nodesData.getIds();
  if (ids.length) nodesData.update(ids.map((id) => ({ id, image: TRANSPARENT_PLACEHOLDER })));
}

/** Called from resetGraph, after the nodes are gone and before the next board arrives. */
function resetCardShape() {
  aspectAdopted = false;
  if (resetCardAspect()) reshapeNodeBoxes();
}

let renderQueued = false;
function scheduleRenderOverlay() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; renderOverlay(); });
}
network.on('afterDrawing', scheduleRenderOverlay);
window.addEventListener('resize', scheduleRenderOverlay);

function nodeDomRect(id) {
  const box = network.getBoundingBox(id);
  if (!box) return null;
  const topLeft = network.canvasToDOM({ x: box.left, y: box.top });
  const bottomRight = network.canvasToDOM({ x: box.right, y: box.bottom });
  return {
    left: topLeft.x, top: topLeft.y,
    width: bottomRight.x - topLeft.x, height: bottomRight.y - topLeft.y,
  };
}

function hitTestNode(domX, domY) {
  let found = null;
  nodesData.forEach((n) => {
    if (found) return;
    const r = nodeDomRect(n.id);
    if (r && domX >= r.left && domX <= r.left + r.width && domY >= r.top && domY <= r.top + r.height) found = n.id;
  });
  return found;
}

const ANCHOR = {
  rightMid: { x: 1, y: 0.5 }, leftMid: { x: 0, y: 0.5 },
  bottomMid: { x: 0.5, y: 1 }, topMid: { x: 0.5, y: 0.02 },
};
function anchorPoint(rect, frac) {
  return { x: rect.left + rect.width * frac.x, y: rect.top + rect.height * frac.y };
}

// Text (headers, headings, connector labels, tap markers) is rendered at a fixed
// screen-pixel size — it never scales with zoom. Below this threshold there's
// simply no room for it: nodes shrink to tiny thumbnails while the fixed-size
// text would overlap into an unreadable blob, so we hide it entirely and show
// just the clean node/connector layout until the user zooms in far enough to read it.
const LABEL_VISIBILITY_SCALE = 0.35;
function labelsVisible() {
  return network.getScale() >= LABEL_VISIBILITY_SCALE;
}

// ---------------------------------------------------------------------------
// Master overlay render — node cards, headers, section headings, connectors,
// self-loops, comment pins, text notes. Everything synced every animation
// frame via network.canvasToDOM so it tracks pan/zoom exactly.
// ---------------------------------------------------------------------------
function renderOverlay() {
  renderNodeCards();
  renderHeadersAndHeadings();
  renderSectionNotes();
  renderConnectors();
  renderComments();
  renderTextNotes();
}

function renderNodeCards() {
  const cardsLayer = document.getElementById('nodeCards');
  const liveIds = new Set();

  nodesData.forEach((n) => {
    const rect = nodeDomRect(n.id);
    if (!rect) return;
    liveIds.add(n.id);

    let card = cardElements.get(n.id);
    if (!card) {
      card = document.createElement('div');
      card.className = 'node-card';
      const img = document.createElement('img');
      card.appendChild(img);
      card.addEventListener('click', (e) => onNodeCardClick(n.id, e));
      cardsLayer.appendChild(card);
      cardElements.set(n.id, card);
    }
    card.style.left = rect.left + 'px';
    card.style.top = rect.top + 'px';
    card.style.width = rect.width + 'px';
    card.style.height = rect.height + 'px';
    card.classList.toggle('selected', ui.selectedIds.has(n.id));

    const status = nodeStatus.get(n.id);
    card.classList.toggle('status-fail', status?.level === 'fail');
    card.classList.toggle('status-warn', status?.level === 'warn');
    if (status) {
      card.dataset.statusBadge = status.badge || status.level.toUpperCase();
      card.title = status.summary || '';
    } else {
      delete card.dataset.statusBadge;
      card.removeAttribute('title');
    }

    const meta = nodeMeta.get(n.id);
    const img = card.querySelector('img');
    const hash = n.id;
    if (meta && meta.screenshot) {
      // Sized for how big the card actually is right now, not a fixed guess — see
      // cardTargetWidth. Zooming in to read a screen re-requests a sharper bucket instead
      // of the browser stretching the same small JPEG and blurring it.
      const targetW = cardTargetWidth(rect.width * (window.devicePixelRatio || 1));
      const scaled = requestScaled(hash, meta.screenshot, targetW, (url, decodedHash) => {
        adoptShotAspect(decodedHash);
        const live = cardElements.get(hash)?.querySelector('img');
        if (live && live.dataset.src !== url) {
          live.src = url; live.dataset.src = url; live.classList.remove('is-placeholder');
        }
      });
      // No cache hit yet at this bucket — keep whatever is already on screen (even a
      // smaller, softer copy) rather than flashing back to the placeholder tile; the
      // callback above swaps in the sharper copy the moment it decodes.
      if (scaled && img.dataset.src !== scaled) {
        img.src = scaled; img.dataset.src = scaled; img.classList.remove('is-placeholder');
      } else if (!img.dataset.src) {
        img.src = PLACEHOLDER_IMG; img.dataset.src = PLACEHOLDER_IMG;
        img.classList.add('is-placeholder');
      }
    } else if (img.dataset.src !== PLACEHOLDER_IMG) {
      img.src = PLACEHOLDER_IMG; img.dataset.src = PLACEHOLDER_IMG;
      img.classList.add('is-placeholder');
    }
  });

  cardElements.forEach((card, hash) => {
    if (!liveIds.has(hash)) { card.remove(); cardElements.delete(hash); }
  });
}

function onNodeCardClick(hash, e) {
  if (ui.currentTool !== 'pan') return; // select/comment tools handle their own mousedown on the overlay
  if (ui.spacePan) return;              // space is held for a pan, not to open anything
  e.stopPropagation();
  openModal(hash);
}

function renderHeadersAndHeadings() {
  const headersLayer = document.getElementById('nodeHeaders');
  const headingsLayer = document.getElementById('sectionHeadings');
  headersLayer.innerHTML = '';
  headingsLayer.innerHTML = '';
  if (!labelsVisible()) return;

  nodesData.forEach((n) => {
    const rect = nodeDomRect(n.id);
    if (!rect) return;
    const header = document.createElement('div');
    header.className = 'node-header';
    header.style.left = (rect.left + rect.width / 2) + 'px';
    header.style.top = rect.top + 'px';
    header.style.maxWidth = rect.width + 'px';
    header.innerHTML = `<span class="dot" style="background:#22c55e;"></span><span class="node-header-text">${escapeHtml(nodeHeaderLabel(n.id))}</span>`;
    headersLayer.appendChild(header);
  });

  if (!ui.showHeadings) return;
  sectionLayout.forEach((layout, name) => {
    const list = sections.get(name) || [];
    const firstHash = list[0];
    const rect = firstHash ? nodeDomRect(firstHash) : null;
    if (!rect) return;
    const heading = document.createElement('div');
    heading.className = 'section-heading';
    heading.style.left = rect.left + 'px';
    heading.style.top = (rect.top - 34) + 'px';
    heading.textContent = name;
    headingsLayer.appendChild(heading);
  });
}

function renderConnectors() {
  const svgPaths = document.getElementById('connectorPaths');
  const labelsLayer = document.getElementById('connectorLabels');
  const markersLayer = document.getElementById('connectorMarkers');
  svgPaths.innerHTML = '';
  labelsLayer.innerHTML = '';
  markersLayer.innerHTML = '';

  const selfLoopCount = new Map();

  edgeMeta.forEach((meta) => {
    if (meta.from === meta.to) {
      const rect = nodeDomRect(meta.from);
      if (!rect) return;
      const n = selfLoopCount.get(meta.from) || 0;
      selfLoopCount.set(meta.from, n + 1);
      drawSelfLoop(rect, meta, n, svgPaths, labelsLayer, markersLayer);
      return;
    }

    const srcRect = nodeDomRect(meta.from);
    const dstRect = nodeDomRect(meta.to);
    if (!srcRect || !dstRect) return;

    const srcCenter = { x: srcRect.left + srcRect.width / 2, y: srcRect.top + srcRect.height / 2 };
    const dstCenter = { x: dstRect.left + dstRect.width / 2, y: dstRect.top + dstRect.height / 2 };
    const vertical = Math.abs(dstCenter.y - srcCenter.y) > Math.abs(dstCenter.x - srcCenter.x);
    const forward = vertical ? dstCenter.y >= srcCenter.y : dstCenter.x >= srcCenter.x;

    const start = vertical
      ? anchorPoint(srcRect, forward ? ANCHOR.bottomMid : ANCHOR.topMid)
      : anchorPoint(srcRect, forward ? ANCHOR.rightMid : ANCHOR.leftMid);
    const end = vertical
      ? anchorPoint(dstRect, forward ? ANCHOR.topMid : ANCHOR.bottomMid)
      : anchorPoint(dstRect, forward ? ANCHOR.leftMid : ANCHOR.rightMid);

    const d = vertical
      ? `M${start.x},${start.y} C${start.x},${start.y + 40} ${end.x},${end.y - 40} ${end.x},${end.y}`
      : `M${start.x},${start.y} C${start.x + 50},${start.y} ${end.x - 50},${end.y} ${end.x},${end.y}`;

    // A connector is coloured by the *flow* it belongs to, not by the one screen it lands
    // on. Blue is a case that worked, amber one with a warning or a suggestion against it,
    // red one with a bug — so a whole failing chain reads as failing. Colouring only the
    // hop into the flagged screen left a red arrow at the end of a run of blue ones, which
    // says "the last step went wrong" when the truth is "this case failed".
    //
    // Falls back to the landing screen's own status, which is what marks a defect on a
    // board whose flows carry no note — an exploration run has no cases to colour.
    const level = flowLevel(meta.to) || flowLevel(meta.from) || nodeStatus.get(meta.to)?.level;
    const stroke = level === 'fail' ? '#f43f5e' : level === 'warn' ? '#f59e0b' : '#0099ff';
    const marker = level === 'fail' ? 'arrowRed'
      : level === 'warn' ? 'arrowAmber' : 'arrowBlue';

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', stroke);
    path.setAttribute('stroke-width', level ? '3' : '2');
    path.setAttribute('marker-end', `url(#${marker})`);
    svgPaths.appendChild(path);

    if (labelsVisible()) {
      const midX = (start.x + end.x) / 2;
      const midY = (start.y + end.y) / 2 - (vertical ? 0 : 14);
      const label = document.createElement('div');
      label.className = 'connector-label';
      label.style.left = midX + 'px';
      label.style.top = midY + 'px';
      label.textContent = truncate(meta.fullLabel, ui.labelLength);
      labelsLayer.appendChild(label);
    }

    if (ui.showTapMarkers && labelsVisible()) {
      const marker = document.createElement('div');
      marker.className = 'connector-tap-marker';
      marker.style.left = start.x + 'px';
      marker.style.top = start.y + 'px';
      marker.innerHTML = '<div class="ring"></div><div class="dot"></div>';
      markersLayer.appendChild(marker);
    }
  });
}

function drawSelfLoop(rect, meta, index, svgPaths, labelsLayer, markersLayer) {
  const baseX = rect.left + rect.width - 10;
  const baseY = rect.top + 10;
  const spread = 16 + index * 14;

  const p0 = { x: baseX, y: baseY };
  const p1 = { x: baseX + spread, y: baseY - spread * 0.6 };
  const p2 = { x: baseX + spread * 1.3, y: baseY + spread * 0.5 };

  const d = `M${p0.x},${p0.y} C${p1.x},${p1.y} ${p2.x},${p2.y} ${p0.x},${p0.y + 10}`;
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', d);
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', '#0099ff');
  path.setAttribute('stroke-width', '2');
  path.setAttribute('marker-end', 'url(#arrowBlue)');
  svgPaths.appendChild(path);

  if (labelsVisible()) {
    const label = document.createElement('div');
    label.className = 'connector-label';
    label.style.left = (p2.x + 10) + 'px';
    label.style.top = p2.y + 'px';
    label.textContent = truncate(meta.fullLabel, ui.labelLength);
    labelsLayer.appendChild(label);
  }

  if (ui.showTapMarkers && labelsVisible()) {
    const marker = document.createElement('div');
    marker.className = 'connector-tap-marker';
    marker.style.left = p0.x + 'px';
    marker.style.top = p0.y + 'px';
    marker.innerHTML = '<div class="ring"></div><div class="dot"></div>';
    markersLayer.appendChild(marker);
  }
}

// ---------------------------------------------------------------------------
// Tools: pan (default) / select (marquee + group drag) / text (sticky notes)
// / comment (pin a note to a spot on a screen)
// ---------------------------------------------------------------------------
const graphWrap = document.getElementById('graphWrap');
const toolOverlay = document.getElementById('toolOverlay');
const selectionBox = document.getElementById('selectionBox');


export { ANCHOR, LABEL_VISIBILITY_SCALE, TRANSPARENT_PLACEHOLDER, adoptShotAspect, anchorPoint, container, drawSelfLoop, graphWrap, hitTestNode, labelsVisible, network, nodeDomRect, onNodeCardClick, renderConnectors, renderHeadersAndHeadings, renderNodeCards, renderOverlay, renderQueued, resetCardShape, scheduleRenderOverlay, selectionBox, toolOverlay };
