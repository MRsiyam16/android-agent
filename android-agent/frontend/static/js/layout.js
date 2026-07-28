// layout.js — extracted verbatim from the old dashboard.js IIFE.

import { scheduleRenderOverlay } from './render.js';
import { CARD_H, CARD_W, NODES_PER_ROW, sectionLayout } from './sections.js';
import { nodesData, sectionOrder, sections, ui } from './state.js';

function relayoutAll() {
  const scale = ui.connectorPct / 100;
  const spacingX = CARD_W * 1.35 * scale;
  const spacingY = (CARD_H + 60) * scale;
  const updates = [];
  let cursorRow = 0;

  sectionOrder.forEach((name) => {
    const list = sections.get(name) || [];
    if (!list.length) return;
    list.forEach((hash, i) => {
      const col = i % NODES_PER_ROW;
      const rowWithin = Math.floor(i / NODES_PER_ROW);
      updates.push({ id: hash, x: col * spacingX, y: (cursorRow + rowWithin) * spacingY });
    });
    const rowsUsed = Math.ceil(list.length / NODES_PER_ROW);
    sectionLayout.set(name, { startRow: cursorRow, rowCount: rowsUsed });
    cursorRow += rowsUsed + 0.9;
  });

  if (updates.length) nodesData.update(updates.filter((u) => nodesData.get(u.id)));
  scheduleRenderOverlay();
}

// ---------------------------------------------------------------------------
// vis-network setup — nodes are fully invisible "custom" shapes that exist
// purely to give us layout/pan/zoom math (getBoundingBox/canvasToDOM). The
// actual visible card is a synced native <img> DOM element (see renderOverlay),
// which is what fixes both the blur (native img rendering, no canvas re-bake)
// and the cutoff (vis's built-in image-node sizing forces a square box that
// distorted our 1:2 aspect screenshots).
// ---------------------------------------------------------------------------
// A fixed-size fully-transparent PNG. Using shape:'image' + useImageSize:true makes
// vis-network size (and hit-test / getBoundingBox) the node to this image's exact
// native pixel dimensions, preserving the 1:2 card aspect ratio — unlike shape:'custom',
// whose ctxRenderer nodeDimensions turned out NOT to be honored by getBoundingBox().

export { relayoutAll };
