(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  const nodesData = new vis.DataSet([]);
  const nodeMeta = new Map();      // state_hash -> { package, activity, elements, screenshot, action, section, _bakedFor }
  const edgeMeta = new Map();      // edgeId -> { from, to, fullLabel }
  const sections = new Map();      // sectionName -> [state_hash, ...] in first-seen order
  const sectionOrder = [];         // section names in first-seen order
  const cardElements = new Map();  // state_hash -> persistent DOM card element
  const textNotes = new Map();     // id -> { id, cx, cy, text, fontSize }
  const comments = new Map();      // id -> { id, hash, fracX, fracY, text }
  // state_hash -> { level: 'fail' | 'warn', badge, summary }. Written by a reporting run
  // (see test_yt_report.py) and persisted alongside the annotations, because it is a
  // judgement about the run rather than something telemetry can derive on its own.
  const nodeStatus = new Map();

  let labelLength = 22;
  let connectorPct = 100;
  let observeMode = false;
  let currentTool = 'pan';
  let selectedIds = new Set();
  let lastElements = [];
  let lastDims = { w: 1080, h: 1920 };
  let currentSessionId = null;
  let currentPackage = null;
  let autoSaveTimer = null;
  let showTapMarkers = true;
  let showHeadings = true;
  let autoFitOnNewState = false;

  const PLACEHOLDER_IMG =
    'data:image/svg+xml;utf8,' + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="150" height="300">' +
      '<rect width="100%" height="100%" rx="14" fill="#1c1c1c"/>' +
      '<text x="50%" y="55%" fill="#5c5c5c" font-size="11" font-family="sans-serif" text-anchor="middle">loading…</text>' +
      '</svg>'
    );

  function truncate(str, len) {
    if (!str) return '';
    return str.length > len ? str.slice(0, Math.max(1, len - 1)) + '…' : str;
  }

  function screenshotSrc(b64) {
    return b64 ? 'data:image/jpeg;base64,' + b64 : '';
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function nodeHeaderLabel(hash) {
    const meta = nodeMeta.get(hash);
    if (!meta) return hash.slice(0, 8);
    if (meta.screenName) return (meta.screenNumber ? `#${meta.screenNumber} ` : '') + meta.screenName;
    const base = meta.activity ? meta.activity.split('.').pop() : (meta.package || '').split('.').pop();
    return base || hash.slice(0, 8);
  }

  // ---------------------------------------------------------------------------
  // Section classification
  // ---------------------------------------------------------------------------
  const GENERIC_LABELS = new Set([
    'relativelayout', 'framelayout', 'linearlayout', 'view', 'imageview',
    'textview', 'button', 'edittext', 'calculator input field', 'result preview',
    'unlabeled element',
  ]);

  function isSectionTrigger(label) {
    if (!label) return false;
    const norm = label.trim().toLowerCase();
    if (GENERIC_LABELS.has(norm)) return false;
    if (/^[\d+\-×÷=%.()]+$/.test(norm)) return false;
    if (norm.length <= 1) return false;
    return /^[a-zA-Z][a-zA-Z\s]*$/.test(label.trim());
  }

  function assignSection(hash, sectionName) {
    if (!sections.has(sectionName)) {
      sections.set(sectionName, []);
      sectionOrder.push(sectionName);
    }
    const list = sections.get(sectionName);
    if (!list.includes(hash)) list.push(hash);
    const meta = nodeMeta.get(hash);
    if (meta) meta.section = sectionName;
  }

  // ---------------------------------------------------------------------------
  // Layout — one row per section, wrapping within a section if it grows too wide.
  // ---------------------------------------------------------------------------
  const CARD_W = 150;
  const CARD_H = 300;
  const NODES_PER_ROW = 6;
  const sectionLayout = new Map();

  function relayoutAll() {
    const scale = connectorPct / 100;
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
  const TRANSPARENT_PLACEHOLDER = (() => {
    const c = document.createElement('canvas');
    c.width = CARD_W;
    c.height = CARD_H;
    return c.toDataURL('image/png');
  })();

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
      card.classList.toggle('selected', selectedIds.has(n.id));

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
      const wantSrc = (meta && screenshotSrc(meta.screenshot)) || PLACEHOLDER_IMG;
      const img = card.querySelector('img');
      if (img.dataset.src !== wantSrc) { img.src = wantSrc; img.dataset.src = wantSrc; }
    });

    cardElements.forEach((card, hash) => {
      if (!liveIds.has(hash)) { card.remove(); cardElements.delete(hash); }
    });
  }

  function onNodeCardClick(hash, e) {
    if (currentTool !== 'pan') return; // select/comment tools handle their own mousedown on the overlay
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

    if (!showHeadings) return;
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

      // A connector inherits the status of the screen it lands on: the transition that
      // reaches a defective screen is the one a reader wants to trace back.
      const dstStatus = nodeStatus.get(meta.to);
      const stroke = dstStatus?.level === 'fail' ? '#f43f5e'
        : dstStatus?.level === 'warn' ? '#f59e0b' : '#0099ff';
      const marker = dstStatus?.level === 'fail' ? 'arrowRed'
        : dstStatus?.level === 'warn' ? 'arrowAmber' : 'arrowBlue';

      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', d);
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', stroke);
      path.setAttribute('stroke-width', dstStatus ? '3' : '2');
      path.setAttribute('marker-end', `url(#${marker})`);
      svgPaths.appendChild(path);

      if (labelsVisible()) {
        const midX = (start.x + end.x) / 2;
        const midY = (start.y + end.y) / 2 - (vertical ? 0 : 14);
        const label = document.createElement('div');
        label.className = 'connector-label';
        label.style.left = midX + 'px';
        label.style.top = midY + 'px';
        label.textContent = truncate(meta.fullLabel, labelLength);
        labelsLayer.appendChild(label);
      }

      if (showTapMarkers && labelsVisible()) {
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
      label.textContent = truncate(meta.fullLabel, labelLength);
      labelsLayer.appendChild(label);
    }

    if (showTapMarkers && labelsVisible()) {
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

  function setTool(tool) {
    currentTool = tool;
    document.querySelectorAll('.dock-btn[data-tool]').forEach((b) => b.classList.toggle('active', b.dataset.tool === tool));
    graphWrap.dataset.tool = tool;
    if (tool !== 'select') { selectedIds = new Set(); scheduleRenderOverlay(); }
  }
  document.querySelectorAll('.dock-btn[data-tool]').forEach((btn) => {
    btn.addEventListener('click', () => setTool(btn.dataset.tool));
  });
  setTool('pan');

  window.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) return;
    // Delete removes the selected notes. Guarded by the check above, so it can never fire
    // while a caption is being typed into. Graph screens are deliberately left alone —
    // they come from telemetry and would simply reappear on the next run.
    if (e.key === 'Delete' || e.key === 'Backspace') {
      const doomed = [...selectedIds].filter((id) => textNotes.has(id));
      if (doomed.length) {
        e.preventDefault();
        doomed.forEach((id) => textNotes.delete(id));
        selectedIds = new Set([...selectedIds].filter((id) => !doomed.includes(id)));
        closeTextFormatMenu();
        scheduleRenderOverlay();
        scheduleAutoSave();
        return;
      }
    }
    if (e.key === 'v' || e.key === 'V') setTool('pan');
    if (e.key === 's' || e.key === 'S') setTool('select');
    if (e.key === 't' || e.key === 'T') setTool('text');
    if (e.key === 'n' || e.key === 'N') setTool('sticky');
    if (e.key === 'c' || e.key === 'C') setTool('comment');
  });

  let dragState = null;

  // -- selection helpers, shared by graph nodes and notes ---------------------
  // `selectedIds` holds both kinds; `textNotes.has(id)` is what tells them apart. A note's
  // position lives on the note itself (cx/cy, canvas coords) rather than in vis.js, so a
  // group drag has to move each kind through its own channel.
  function noteDomRect(id) {
    const el = document.querySelector(`.text-note[data-note-id="${CSS.escape(id)}"]`);
    if (!el) return null;
    const base = toolOverlay.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    return { left: r.left - base.left, top: r.top - base.top, width: r.width, height: r.height };
  }

  function hitTestNote(x, y) {
    const els = Array.from(document.querySelectorAll('#textNotesLayer .text-note'));
    // Last painted wins, matching what sits visually on top.
    for (let i = els.length - 1; i >= 0; i--) {
      const id = els[i].dataset.noteId;
      const r = noteDomRect(id);
      if (r && x >= r.left && x <= r.left + r.width && y >= r.top && y <= r.top + r.height) return id;
    }
    return null;
  }

  function beginGroupDrag(x, y) {
    const origins = new Map();
    selectedIds.forEach((id) => {
      const note = textNotes.get(id);
      if (note) { origins.set(id, { x: note.cx, y: note.cy, isNote: true }); return; }
      const pos = network.getPositions([id])[id];
      if (pos) origins.set(id, { x: pos.x, y: pos.y, isNote: false });
    });
    dragState = { mode: 'group', startX: x, startY: y, origins, movedNote: false };
  }

  // Notes sit above the tool overlay (z-index 20 vs 15), so with the select tool active a
  // mousedown on a note never reaches the overlay at all — which is why neither the
  // marquee nor the move tool could pick a note up, and why grabbing a sticky's header
  // dragged that one note instead of the selection. Intercept in the capture phase and
  // route it through the same path the graph nodes use.
  document.getElementById('textNotesLayer').addEventListener('mousedown', (e) => {
    if (currentTool !== 'select') return;
    const noteEl = e.target.closest('.text-note');
    if (!noteEl) return;
    e.preventDefault();
    e.stopPropagation();
    const base = toolOverlay.getBoundingClientRect();
    const x = e.clientX - base.left, y = e.clientY - base.top;
    const id = noteEl.dataset.noteId;
    if (!selectedIds.has(id)) { selectedIds = new Set([id]); scheduleRenderOverlay(); }
    beginGroupDrag(x, y);
  }, true);

  toolOverlay.addEventListener('mousedown', (e) => {
    const rect = toolOverlay.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;

    if (currentTool === 'select') {
      // Notes are hit-tested first: they render above the graph, so at a point covering
      // both, the note is what the user sees and is aiming at.
      const hitId = hitTestNote(x, y) || hitTestNode(x, y);
      if (hitId) {
        if (!selectedIds.has(hitId)) { selectedIds = new Set([hitId]); scheduleRenderOverlay(); }
        beginGroupDrag(x, y);
      } else {
        dragState = { mode: 'marquee', startX: x, startY: y };
        selectionBox.classList.add('visible');
        Object.assign(selectionBox.style, { left: x + 'px', top: y + 'px', width: '0px', height: '0px' });
      }
    } else if (currentTool === 'text') {
      addTextNoteAt(x, y, 'text');
    } else if (currentTool === 'sticky') {
      addTextNoteAt(x, y, 'sticky');
    } else if (currentTool === 'comment') {
      const hitId = hitTestNode(x, y);
      if (hitId) openCommentComposer(hitId, x, y);
    }
  });

  window.addEventListener('mousemove', (e) => {
    if (!dragState) return;
    const rect = toolOverlay.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;

    if (dragState.mode === 'marquee') {
      const left = Math.min(x, dragState.startX), top = Math.min(y, dragState.startY);
      const w = Math.abs(x - dragState.startX), h = Math.abs(y - dragState.startY);
      Object.assign(selectionBox.style, { left: left + 'px', top: top + 'px', width: w + 'px', height: h + 'px' });
    } else if (dragState.mode === 'group') {
      const scale = network.getScale();
      const dx = (x - dragState.startX) / scale, dy = (y - dragState.startY) / scale;
      const updates = [];
      dragState.origins.forEach((orig, id) => {
        if (orig.isNote) {
          const note = textNotes.get(id);
          if (note) { note.cx = orig.x + dx; note.cy = orig.y + dy; dragState.movedNote = true; }
        } else {
          updates.push({ id, x: orig.x + dx, y: orig.y + dy });
        }
      });
      if (updates.length) nodesData.update(updates);
      scheduleRenderOverlay();
    }
  });

  window.addEventListener('mouseup', () => {
    if (!dragState) return;
    if (dragState.mode === 'marquee') {
      selectionBox.classList.remove('visible');
      const bx1 = parseFloat(selectionBox.style.left), by1 = parseFloat(selectionBox.style.top);
      const bx2 = bx1 + parseFloat(selectionBox.style.width), by2 = by1 + parseFloat(selectionBox.style.height);
      const newSel = new Set();
      const hits = (r) => r && !(r.left > bx2 || r.left + r.width < bx1
                                 || r.top > by2 || r.top + r.height < by1);
      nodesData.forEach((n) => { if (hits(nodeDomRect(n.id))) newSel.add(n.id); });
      // Notes are part of the board, so a marquee has to sweep them up too.
      textNotes.forEach((note) => { if (hits(noteDomRect(note.id))) newSel.add(note.id); });
      selectedIds = newSel;
      scheduleRenderOverlay();
    }
    // A note's position is only in memory until something saves it; node positions are
    // re-derived from the layout, so only a moved note needs persisting.
    if (dragState.mode === 'group' && dragState.movedNote) scheduleAutoSave();
    dragState = null;
  });

  // ---------------------------------------------------------------------------
  // Text notes
  // ---------------------------------------------------------------------------
  // kind 'text' = a bare caption on the board (the T tool); kind 'sticky' = a note card
  // sized to match a screen (the N tool). Legacy notes saved before stickies existed have
  // no kind and were bare captions, so 'text' is the right default for them.
  function addTextNoteAt(domX, domY, kind = 'text') {
    const canvasPos = network.DOMtoCanvas({ x: domX, y: domY });
    const id = 'note-' + Math.random().toString(36).slice(2, 10);
    const isSticky = kind === 'sticky';
    textNotes.set(id, {
      id, kind, cx: canvasPos.x, cy: canvasPos.y,
      text: isSticky ? 'Write in **Markdown**.' : 'Heading',
      fontSize: isSticky ? 10 : 24,
      ...(isSticky ? { title: 'Note', color: 'slate', w: 130 }
                   : { color: 'white', weight: 700 }),
    });
    setTool('pan');
    scheduleRenderOverlay();
    scheduleAutoSave();
    // Drop straight into typing with the formatting menu already open, so a new caption
    // can be styled without hunting for a control first.
    if (!isSticky) {
      // The overlay render is debounced, so the element does not exist yet. Wait for it
      // rather than firing one hopeful rAF — a single frame loses the race and the caption
      // silently opens unfocused, with no menu.
      let tries = 0;
      const grabFocus = () => {
        const el = document.querySelector(`.text-note[data-note-id="${CSS.escape(id)}"]`);
        const content = el && el.querySelector('.text-note-content');
        if (content) {
          content.focus();
          document.execCommand('selectAll', false, null);
        } else if (++tries < 60) {
          requestAnimationFrame(grabFocus);
        }
      };
      requestAnimationFrame(grabFocus);
    }
  }

  // Header colours are for triage at a glance — red for bugs, amber for flaky, and so on.
  // Values come from the design tokens so notes stay in the same palette as the rest of the UI.
  const STICKY_COLORS = [
    { key: 'slate',  label: 'Neutral', css: '#dedede',              ink: '#121214' },
    { key: 'red',    label: 'Bug',     css: '#fef08a',              ink: '#713f12' },
    { key: 'orange', label: 'Flaky',   css: '#fed7aa',              ink: '#431407' },
    { key: 'green',  label: 'Passing', css: '#bbf7d0',              ink: '#14532d' },
    { key: 'blue',   label: 'Info',    css: '#a5f3fc',              ink: '#164e63' },
    { key: 'purple', label: 'Idea',    css: '#e9d5ff',              ink: '#581c87' },
  ];

  // -- bare text captions (the T tool) ---------------------------------------
  // Ink colours, not fills: a caption sits directly on the dark board, so these are
  // chosen to stay legible against it rather than to tint a card.
  const TEXT_COLORS = [
    { key: 'white',  label: 'Default', css: '#f4f4f5' },
    { key: 'muted',  label: 'Muted',   css: '#a1a1aa' },
    { key: 'blue',   label: 'Blue',    css: '#38bdf8' },
    { key: 'green',  label: 'Green',   css: '#4ade80' },
    { key: 'amber',  label: 'Amber',   css: '#fbbf24' },
    { key: 'red',    label: 'Red',     css: '#f87171' },
    { key: 'purple', label: 'Purple',  css: '#c084fc' },
  ];
  const TEXT_WEIGHTS = [
    { w: 400, label: 'Regular' }, { w: 600, label: 'Semi' },
    { w: 700, label: 'Bold' }, { w: 800, label: 'Heavy' },
  ];
  // Captions double as board headings, so the range runs from footnote to poster.
  const TEXT_SIZE_MIN = 6, TEXT_SIZE_MAX = 200;
  const TEXT_SIZE_PRESETS = [12, 16, 24, 32, 48, 72, 120];

  function textInk(key) {
    return (TEXT_COLORS.find((c) => c.key === key) || TEXT_COLORS[0]).css;
  }
  function textSize(note) {
    const n = parseInt(note.fontSize, 10);
    return Math.min(TEXT_SIZE_MAX, Math.max(TEXT_SIZE_MIN, Number.isFinite(n) ? n : 24));
  }
  function textContentStyle(note) {
    return `font-size:${textSize(note)}px;color:${textInk(note.color)};`
         + `font-weight:${note.weight || 400};line-height:1.15;`;
  }

  // Notes are laid out at 2x and scaled back down.
  const STICKY_DESIGN_SCALE = 2;
  const STICKY_WIDTHS = [
    { key: 'mini',   label: 'Mini',   w: 110 },
    { key: 'small',  label: 'Small',  w: 130 },
    { key: 'medium', label: 'Medium', w: 160 },
    { key: 'large',  label: 'Large',  w: 220 },
  ];
  const STICKY_MIN_W = 80, STICKY_MIN_H = 60;
  const stickyColor = (key) => STICKY_COLORS.find((c) => c.key === key) || STICKY_COLORS[0];
  const stickyW = (note) => {
    if (typeof note.w === 'number') return note.w;
    const found = STICKY_WIDTHS.find((w) => w.key === note.width);
    return found ? found.w : 130;
  };

  // Small Markdown subset, rendered locally rather than pulling in a library: the dashboard
  // is used against devices on isolated networks, so a CDN script would simply fail there.
  // The source is HTML-escaped first, so note text can never inject markup.
  function mdInline(s) {
    // Pull inline code out first and re-insert at the end, so ** or _ inside a code span
    // is never treated as emphasis.
    const spans = [];
    let out = s.replace(/`([^`]+)`/g, (_, code) => `\u0000${spans.push(`<code>${code}</code>`) - 1}\u0000`);

    out = out
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
               '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      // Bare URLs, but not ones already inside an href="" from the line above.
      .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
               '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>')
      .replace(/~~(.+?)~~/g, '<del>$1</del>')
      .replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/__(.+?)__/g, '<strong>$1</strong>')
      .replace(/\*(?!\s)(.+?)(?<!\s)\*/g, '<em>$1</em>')
      .replace(/(^|[\s(])_(?!\s)(.+?)(?<!\s)_(?=[\s.,;:!?)]|$)/g, '$1<em>$2</em>')
      // Two trailing spaces = hard line break, as in standard Markdown.
      .replace(/ {2,}$/, '<br>');

    return out.replace(/\u0000(\d+)\u0000/g, (_, i) => spans[+i]);
  }

  // Escape once at the boundary; renderBlocks works on already-escaped text so nested
  // constructs (blockquotes) can recurse without double-escaping.
  function renderMarkdown(src) {
    return renderBlocks(escapeHtml(src || ''));
  }

  function renderBlocks(escaped) {
    const lines = escaped.replace(/\r/g, '').split('\n');
    let html = '';
    let i = 0;

    const isBlank = (l) => !l.trim();
    const listItem = (l) => l.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);

    // Lists are parsed as a unit so indentation can nest them, which the previous
    // line-at-a-time version could not express.
    function parseList(indent) {
      const first = listItem(lines[i]);
      const ordered = /\d/.test(first[2]);
      let out = ordered ? '<ol>' : '<ul>';
      let lastListIndex = -1;
      while (i < lines.length) {
        if (i === lastListIndex) break;   // same progress guard as the block loop
        lastListIndex = i;
        const m = listItem(lines[i]);
        if (!m || m[1].length < indent) break;
        if (m[1].length > indent) {
          // A nested list belongs inside the item above it, not beside it.
          const nested = parseList(m[1].length);
          out = out.endsWith('</li>') ? out.slice(0, -5) + nested + '</li>' : out + nested;
          continue;
        }
        if (/\d/.test(m[2]) !== ordered) break;
        let text = m[3];
        let cls = '';
        const task = text.match(/^\[([ xX])\]\s+(.*)$/);
        if (task) {
          cls = ' class="task"';
          text = `<span class="task-box">${task[1].toLowerCase() === 'x' ? '☑' : '☐'}</span> ${task[2]}`;
        }
        i++;
        // Continuation lines belong to the item they follow.
        const cont = [];
        while (i < lines.length && !isBlank(lines[i]) && !listItem(lines[i])
               && !/^(#{1,6}\s|&gt;|```|\||(-{3,}|\*{3,})\s*$)/.test(lines[i].trim())) {
          cont.push(lines[i].trim()); i++;
        }
        const body = cont.length ? text + ' ' + cont.join(' ') : text;
        out += `<li${cls}>${task ? body : mdInline(body)}</li>`;
      }
      return out + (ordered ? '</ol>' : '</ul>');
    }

    function parseTable() {
      const row = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const head = row(lines[i]); i += 2;               // skip the |---|---| separator
      let out = '<table><thead><tr>' + head.map((c) => `<th>${mdInline(c)}</th>`).join('') + '</tr></thead><tbody>';
      while (i < lines.length && lines[i].includes('|') && !isBlank(lines[i])) {
        out += '<tr>' + row(lines[i]).map((c) => `<td>${mdInline(c)}</td>`).join('') + '</tr>';
        i++;
      }
      return out + '</tbody></table>';
    }

    // Every branch below is expected to consume at least one line. If one ever fails to —
    // on some input I did not anticipate — this guard forces progress instead of spinning
    // forever and freezing the tab, which is exactly what an earlier version did.
    let lastIndex = -1;
    while (i < lines.length) {
      if (i === lastIndex) { html += `<p>${mdInline(lines[i])}</p>`; i++; continue; }
      lastIndex = i;

      const line = lines[i];

      if (isBlank(line)) { i++; continue; }

      let m;
      if ((m = line.match(/^\s*```\s*(\S*)/))) {
        i++;
        const buf = [];
        while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;
        const lang = m[1] ? ` class="lang-${m[1]}"` : '';
        html += `<pre><code${lang}>${buf.join('\n')}</code></pre>`;
        continue;
      }
      if ((m = line.match(/^\s*(#{1,6})\s+(.*)$/))) {
        const lvl = Math.min(m[1].length, 6);
        html += `<h${lvl}>${mdInline(m[2].replace(/\s+#+\s*$/, ''))}</h${lvl}>`;
        i++; continue;
      }
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { html += '<hr>'; i++; continue; }
      if (/^\s*&gt;/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*&gt;/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*&gt;\s?/, '')); i++;
        }
        html += `<blockquote>${renderBlocks(buf.join('\n'))}</blockquote>`;
        continue;
      }
      if (line.includes('|') && i + 1 < lines.length && /^\s*\|?[\s:-]*-[\s:|-]*$/.test(lines[i + 1])) {
        html += parseTable(); continue;
      }
      if (listItem(line)) { html += parseList(listItem(line)[1].length); continue; }

      // Paragraph: consecutive non-blank lines join with a soft break rather than each
      // becoming its own <p>, which is what made wrapped prose look broken before.
      const buf = [];
      while (i < lines.length && !isBlank(lines[i]) && !listItem(lines[i])
             && !/^\s*(#{1,6}\s|&gt;|```|(-{3,}|\*{3,}|_{3,})\s*$)/.test(lines[i])) {
        buf.push(lines[i]); i++;
      }
      html += `<p>${buf.map(mdInline).join('<br>')}</p>`;
    }
    return html;
  }

  function renderTextNotes() {
    const layer = document.getElementById('textNotesLayer');

    // Rebuilding the layer while a note is being typed into would destroy the caret, and
    // renderOverlay runs on every pan/zoom frame. When something in here has focus, move
    // the existing elements instead of recreating them.
    if (layer.contains(document.activeElement)) {
      layer.querySelectorAll('.text-note').forEach((el) => {
        const note = textNotes.get(el.dataset.noteId);
        if (!note) return;
        const pos = network.canvasToDOM({ x: note.cx, y: note.cy });
        el.style.left = pos.x + 'px';
        el.style.top = pos.y + 'px';
        // Restyle in place as well as reposition. The formatting menu lives inside this
        // layer, so dragging its size slider puts focus *here* and takes this branch —
        // without this the caption would not change until focus left the slider, which
        // reads as a dead control.
        const content = note.kind !== 'sticky' && el.querySelector('.text-note-content');
        if (content) {
          content.setAttribute('style', textContentStyle(note));
          el.style.transformOrigin = 'top left';
          el.style.transform = `scale(${network.getScale()})`;
        }
      });
      return;
    }

    // A full rebuild only happens when focus is outside this layer — i.e. nothing is being
    // edited — so any open formatting menu is about to be destroyed with the rest of the
    // layer. Close it properly instead of leaving `textMenuState` pointing at a dead node.
    closeTextFormatMenu();
    layer.innerHTML = '';
    textNotes.forEach((note) => {
      const pos = network.canvasToDOM({ x: note.cx, y: note.cy });
      const el = document.createElement('div');
      const isSticky = note.kind === 'sticky';
      el.className = 'text-note kind-' + (isSticky ? 'sticky' : 'text')
        + (selectedIds.has(note.id) ? ' selected' : '');
      el.dataset.noteId = note.id;
      el.style.left = pos.x + 'px';
      el.style.top = pos.y + 'px';

      if (isSticky) {
        const color = stickyColor(note.color);
        // Lay the note out at its canvas size, then scale the whole element by the current
        // zoom. Everything inside — text, padding, header, buttons — scales together, so a
        // note keeps its proportion to the screens instead of being a fixed screen size.
        el.style.width = (stickyW(note) * STICKY_DESIGN_SCALE) + 'px';
        if (note.h) el.style.height = (note.h * STICKY_DESIGN_SCALE) + 'px';
        el.style.transformOrigin = 'top left';
        el.style.transform = `scale(${network.getScale() / STICKY_DESIGN_SCALE})`;
        el.innerHTML = `
          <div class="sticky-head" style="background:${color.css};color:${color.ink};">
            <div class="sticky-title" contenteditable="true" spellcheck="false"
                 style="font-size:${(note.fontSize || 12) * STICKY_DESIGN_SCALE}px;">${escapeHtml(note.title || 'Note Title')}</div>
            <div class="sticky-actions">
              <div class="sticky-btn sticky-menu-btn" title="Note settings">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="4" y1="7" x2="20" y2="7"></line><line x1="4" y1="12" x2="20" y2="12"></line><line x1="4" y1="17" x2="20" y2="17"></line></svg>
              </div>
              <div class="sticky-btn sticky-close" title="Delete note">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
              </div>
            </div>
          </div>
          <div class="sticky-body markdown" contenteditable="false" style="font-size:${(note.fontSize || 12) * STICKY_DESIGN_SCALE}px;">${renderMarkdown(note.text)}</div>
          <div class="sticky-resize" title="Drag to resize"></div>
        `;

        const title = el.querySelector('.sticky-title');
        title.addEventListener('blur', (e) => { note.title = e.target.innerText.trim() || 'Note'; scheduleAutoSave(); });
        title.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); } });

        const body = el.querySelector('.sticky-body');
        // Editing is opt-in via double-click. A single click has to stay harmless: the
        // body is what you click to read, select or drag a note past, and when a single
        // click armed the editor every stray click swapped rendered Markdown for raw
        // source — and then round-tripped it back through the save.
        body.addEventListener('dblclick', () => {
          if (body.isContentEditable) return;
          body.setAttribute('contenteditable', 'true');
          body.classList.add('editing');   // explicit class, not :focus — see dashboard.css
          body.textContent = note.text;
          body.focus();
        });
        body.addEventListener('blur', () => {
          if (!body.isContentEditable) return;
          // textContent, NEVER innerText. innerText is computed from the *rendered*
          // layout, so it returns whatever the current white-space setting produced —
          // by blur time the editing style is already going away, and every newline in
          // the note came back collapsed to a space. One click in and out permanently
          // flattened a note's Markdown and auto-saved the damage. textContent returns
          // the literal characters and is immune to all of that.
          note.text = body.textContent;
          body.setAttribute('contenteditable', 'false');
          body.classList.remove('editing');
          body.innerHTML = renderMarkdown(note.text);
          scheduleAutoSave();
        });

        el.querySelector('.sticky-close').addEventListener('click', () => {
          textNotes.delete(note.id); scheduleRenderOverlay(); scheduleAutoSave();
        });
        el.querySelector('.sticky-menu-btn').addEventListener('mousedown', (e) => {
          e.stopPropagation(); e.preventDefault(); openNoteMenu(note, e.currentTarget);
        });
        // The header doubles as the tab bar you drag the note by (holding mouse down anywhere on tab bar).
        el.querySelector('.sticky-head').addEventListener('mousedown', (e) => {
          if (e.target.closest('.sticky-btn')) return;
          const title = e.target.closest('.sticky-title');
          startNoteDrag(note, e, title);
        });
        el.querySelector('.sticky-resize').addEventListener('mousedown', (e) => {
          startNoteResize(note, el, e);
        });
      } else {
        // Font size is a *canvas* measurement, like a sticky's width: the element is laid
        // out at its true size and then scaled by the zoom. Without this a 120px heading
        // would still be 120 screen pixels at 13% zoom, dwarfing the whole board.
        el.style.transformOrigin = 'top left';
        el.style.transform = `scale(${network.getScale()})`;
        el.innerHTML = `
          <div class="text-note-drag">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><circle cx="8" cy="6" r="1.6"></circle><circle cx="8" cy="12" r="1.6"></circle><circle cx="8" cy="18" r="1.6"></circle><circle cx="16" cy="6" r="1.6"></circle><circle cx="16" cy="12" r="1.6"></circle><circle cx="16" cy="18" r="1.6"></circle></svg>
          </div>
          <div class="text-note-content" contenteditable="true" spellcheck="false"
               style="${textContentStyle(note)}">${escapeHtml(note.text)}</div>
          <div class="text-note-delete">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
          </div>
        `;
        const content = el.querySelector('.text-note-content');
        content.addEventListener('blur', (e) => { note.text = e.target.innerText; scheduleAutoSave(); });
        // Entering the box is what opens the formatting menu — no extra affordance to find.
        content.addEventListener('focus', () => openTextFormatMenu(note, el));
        content.addEventListener('keydown', (e) => {
          if (e.key === 'Escape') { e.preventDefault(); closeTextFormatMenu(); content.blur(); }
        });
        el.querySelector('.text-note-delete').addEventListener('click', () => { textNotes.delete(note.id); closeTextFormatMenu(); scheduleRenderOverlay(); scheduleAutoSave(); });
        el.querySelector('.text-note-drag').addEventListener('mousedown', (e) => startNoteDrag(note, e));
      }

      layer.appendChild(el);
    });
  }

  // -- caption formatting menu -----------------------------------------------
  // Opens on focus of a text note. It must survive the note being re-rendered and must
  // not steal focus from the caption, so every control commits on `input`/`click` and
  // the menu closes only on a mousedown that lands outside both it and its note.
  let textMenuState = null;

  function closeTextFormatMenu() {
    if (!textMenuState) return;
    document.removeEventListener('mousedown', textMenuState.onDocDown, true);
    textMenuState.menu.remove();
    textMenuState = null;
  }

  function openTextFormatMenu(note, noteEl) {
    if (textMenuState && textMenuState.noteId === note.id) return;   // already open
    closeTextFormatMenu();

    const layer = document.getElementById('textNotesLayer');
    const layerRect = layer.getBoundingClientRect();
    const r = noteEl.getBoundingClientRect();

    const menu = document.createElement('div');
    menu.className = 'note-menu text-format-menu';
    menu.style.left = Math.max(4, r.left - layerRect.left) + 'px';
    menu.style.top = (r.bottom - layerRect.top + 8) + 'px';
    const size = textSize(note);
    menu.innerHTML = `
      <div class="note-menu-label">Colour</div>
      <div class="note-menu-swatches">
        ${TEXT_COLORS.map((c) => `
          <div class="note-swatch${(note.color || 'white') === c.key ? ' active' : ''}"
               data-tcolor="${c.key}" title="${c.label}" style="background:${c.css}"></div>`).join('')}
      </div>

      <div class="note-menu-label">Weight</div>
      <div class="note-menu-row">
        ${TEXT_WEIGHTS.map((w) => `
          <div class="note-menu-chip${(note.weight || 400) === w.w ? ' active' : ''}"
               data-tweight="${w.w}" style="font-weight:${w.w};">${w.label}</div>`).join('')}
      </div>

      <div class="note-menu-label" style="display:flex;justify-content:space-between;align-items:center;">
        <span>Text size</span>
        <span class="note-val-label" id="textSizeVal">${size}px</span>
      </div>
      <div class="note-menu-row">
        ${TEXT_SIZE_PRESETS.map((s) => `
          <div class="note-menu-chip${size === s ? ' active' : ''}" data-tsize="${s}">${s}</div>`).join('')}
      </div>
      <div class="note-slider-container">
        <input type="range" class="note-slider" id="textSizeSlider"
               min="${TEXT_SIZE_MIN}" max="${TEXT_SIZE_MAX}" step="1" value="${size}">
      </div>
      <div class="note-menu-hint">Esc to finish · Del removes the selected note</div>
    `;

    // Keep the caret in the caption when the menu is clicked, so typing can continue.
    menu.addEventListener('mousedown', (e) => {
      if (e.target.tagName !== 'INPUT') e.preventDefault();
      e.stopPropagation();
    });

    const sizeVal = menu.querySelector('#textSizeVal');
    const slider = menu.querySelector('#textSizeSlider');

    const applySize = (val) => {
      note.fontSize = Math.min(TEXT_SIZE_MAX, Math.max(TEXT_SIZE_MIN, val));
      sizeVal.textContent = note.fontSize + 'px';
      menu.querySelectorAll('[data-tsize]').forEach((c) =>
        c.classList.toggle('active', parseInt(c.dataset.tsize, 10) === note.fontSize));
      restyleNote(note);
      scheduleAutoSave();
    };

    slider.addEventListener('input', (e) => applySize(parseInt(e.target.value, 10)));

    menu.addEventListener('click', (e) => {
      const sw = e.target.closest('[data-tcolor]');
      const wt = e.target.closest('[data-tweight]');
      const sz = e.target.closest('[data-tsize]');
      if (sw) {
        note.color = sw.dataset.tcolor;
        menu.querySelectorAll('[data-tcolor]').forEach((s) =>
          s.classList.toggle('active', s.dataset.tcolor === note.color));
        restyleNote(note); scheduleAutoSave();
      } else if (wt) {
        note.weight = parseInt(wt.dataset.tweight, 10);
        menu.querySelectorAll('[data-tweight]').forEach((c) =>
          c.classList.toggle('active', parseInt(c.dataset.tweight, 10) === note.weight));
        restyleNote(note); scheduleAutoSave();
      } else if (sz) {
        slider.value = sz.dataset.tsize;
        applySize(parseInt(sz.dataset.tsize, 10));
      }
    });

    const onDocDown = (ev) => {
      const el = document.querySelector(`.text-note[data-note-id="${CSS.escape(note.id)}"]`);
      if (menu.contains(ev.target) || (el && el.contains(ev.target))) return;
      closeTextFormatMenu();
    };
    document.addEventListener('mousedown', onDocDown, true);

    layer.appendChild(menu);
    textMenuState = { noteId: note.id, menu, onDocDown };
  }

  // Apply style to the live element without a full re-render, which would blow away the
  // caret mid-edit.
  function restyleNote(note) {
    const el = document.querySelector(`.text-note[data-note-id="${CSS.escape(note.id)}"]`);
    const content = el && el.querySelector('.text-note-content');
    if (content) content.setAttribute('style', textContentStyle(note));
  }

  function openNoteMenu(note, anchorEl) {
    document.querySelectorAll('.note-menu').forEach((m) => m.remove());
    const rect = anchorEl.getBoundingClientRect();
    const layerRect = document.getElementById('textNotesLayer').getBoundingClientRect();

    const currentW = Math.round(stickyW(note));
    const currentSize = note.fontSize || 10;

    const menu = document.createElement('div');
    menu.className = 'note-menu';
    menu.style.left = (rect.left - layerRect.left) + 'px';
    menu.style.top = (rect.bottom - layerRect.top + 6) + 'px';
    menu.innerHTML = `
      <div class="note-menu-label">Colour</div>
      <div class="note-menu-swatches">
        ${STICKY_COLORS.map((c) => `
          <div class="note-swatch${(note.color || 'slate') === c.key ? ' active' : ''}"
               data-color="${c.key}" title="${c.label}" style="background:${c.css}"></div>`).join('')}
      </div>

      <div class="note-menu-label" style="display:flex;justify-content:space-between;align-items:center;">
        <span>Width</span>
        <span class="note-val-label" id="noteWidthVal">${currentW}px</span>
      </div>
      <div class="note-menu-row">
        ${STICKY_WIDTHS.map((w) => `
          <div class="note-menu-chip${currentW === w.w ? ' active' : ''}"
               data-width="${w.key}">${w.label}</div>`).join('')}
      </div>
      <div class="note-slider-container">
        <input type="range" class="note-slider" id="noteWidthSlider" min="80" max="260" step="5" value="${currentW}">
      </div>

      <div class="note-menu-label" style="display:flex;justify-content:space-between;align-items:center;margin-top:4px;">
        <span>Text size</span>
        <span class="note-val-label" id="noteSizeVal">${currentSize}px</span>
      </div>
      <div class="note-menu-row">
        ${[6, 8, 9, 10, 11, 12, 14].map((s) => `
          <div class="note-menu-chip${currentSize === s ? ' active' : ''}"
               data-size="${s}">${s}px</div>`).join('')}
      </div>
      <div class="note-slider-container">
        <input type="range" class="note-slider" id="noteSizeSlider" min="6" max="16" step="1" value="${currentSize}">
      </div>
    `;

    menu.addEventListener('mousedown', (e) => e.stopPropagation());

    const widthSlider = menu.querySelector('#noteWidthSlider');
    const widthVal = menu.querySelector('#noteWidthVal');
    const sizeSlider = menu.querySelector('#noteSizeSlider');
    const sizeVal = menu.querySelector('#noteSizeVal');

    widthSlider.addEventListener('input', (e) => {
      const val = parseInt(e.target.value, 10);
      note.w = val;
      delete note.width;
      delete note.h;
      widthVal.textContent = val + 'px';
      scheduleRenderOverlay();
      scheduleAutoSave();
    });

    sizeSlider.addEventListener('input', (e) => {
      const val = parseInt(e.target.value, 10);
      note.fontSize = val;
      sizeVal.textContent = val + 'px';
      scheduleRenderOverlay();
      scheduleAutoSave();
    });

    menu.addEventListener('click', (e) => {
      const swatch = e.target.closest('[data-color]');
      const widthChip = e.target.closest('[data-width]');
      const sizeChip = e.target.closest('[data-size]');

      if (swatch) {
        note.color = swatch.dataset.color;
        menu.remove();
        scheduleRenderOverlay();
        scheduleAutoSave();
      } else if (widthChip) {
        const preset = STICKY_WIDTHS.find((w) => w.key === widthChip.dataset.width);
        if (preset) {
          note.w = preset.w;
          delete note.width;
          delete note.h;
          widthSlider.value = preset.w;
          widthVal.textContent = preset.w + 'px';
          scheduleRenderOverlay();
          scheduleAutoSave();
        }
      } else if (sizeChip) {
        const sz = parseInt(sizeChip.dataset.size, 10);
        note.fontSize = sz;
        sizeSlider.value = sz;
        sizeVal.textContent = sz + 'px';
        scheduleRenderOverlay();
        scheduleAutoSave();
      }
    });

    document.getElementById('textNotesLayer').appendChild(menu);
    setTimeout(() => {
      const close = (ev) => {
        if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener('mousedown', close); }
      };
      document.addEventListener('mousedown', close);
    }, 0);
  }

  function startNoteResize(note, el, e) {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX, startY = e.clientY;
    const scale = network.getScale();
    const startW = stickyW(note);
    const startH = note.h || el.getBoundingClientRect().height / scale;

    function onMove(ev) {
      // Pointer travel is in screen pixels; the note's size is in canvas units.
      note.w = Math.max(STICKY_MIN_W, startW + (ev.clientX - startX) / scale);
      note.h = Math.max(STICKY_MIN_H, startH + (ev.clientY - startY) / scale);
      delete note.width;   // an explicit size supersedes any preset
      scheduleRenderOverlay();
    }
    function onUp() {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      scheduleAutoSave();
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  function startNoteDrag(note, e, targetTitle = null) {
    e.stopPropagation();
    const startClientX = e.clientX, startClientY = e.clientY;
    const startCx = note.cx, startCy = note.cy;
    let moved = false;

    function onMove(ev) {
      const dx = ev.clientX - startClientX;
      const dy = ev.clientY - startClientY;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
        moved = true;
        if (targetTitle && document.activeElement === targetTitle) {
          targetTitle.blur();
        }
      }
      if (moved) {
        ev.preventDefault();
        const scale = network.getScale();
        note.cx = startCx + dx / scale;
        note.cy = startCy + dy / scale;
        scheduleRenderOverlay();
      }
    }
    function onUp() {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      if (moved) scheduleAutoSave();
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  // ---------------------------------------------------------------------------
  // Comment pins
  // ---------------------------------------------------------------------------
  function openCommentComposer(hash, domX, domY, existingId) {
    document.querySelectorAll('.comment-popover').forEach((p) => p.remove());
    const existing = existingId ? comments.get(existingId) : null;

    const popover = document.createElement('div');
    popover.className = 'comment-popover';
    popover.style.left = domX + 'px';
    popover.style.top = domY + 'px';
    popover.innerHTML = `
      <textarea placeholder="Add a note about this spot…">${existing ? escapeHtml(existing.text) : ''}</textarea>
      <div class="comment-popover-actions">
        ${existing ? '<button class="danger" data-act="delete">Delete</button>' : ''}
        <button data-act="cancel">Cancel</button>
        <button class="primary" data-act="save">Save</button>
      </div>
    `;
    document.getElementById('commentsLayer').appendChild(popover);
    const textarea = popover.querySelector('textarea');
    textarea.focus();

    popover.querySelector('[data-act="cancel"]').addEventListener('click', () => popover.remove());
    const deleteBtn = popover.querySelector('[data-act="delete"]');
    if (deleteBtn) deleteBtn.addEventListener('click', () => { comments.delete(existingId); popover.remove(); scheduleRenderOverlay(); scheduleAutoSave(); });
    popover.querySelector('[data-act="save"]').addEventListener('click', () => {
      const text = textarea.value.trim();
      if (!text) { popover.remove(); return; }
      if (existing) {
        existing.text = text;
      } else {
        const rect = nodeDomRect(hash);
        const fracX = rect ? (domX - rect.left) / rect.width : 0.5;
        const fracY = rect ? (domY - rect.top) / rect.height : 0.5;
        const id = 'comment-' + Math.random().toString(36).slice(2, 10);
        comments.set(id, { id, hash, fracX, fracY, text });
      }
      popover.remove();
      scheduleRenderOverlay();
      scheduleAutoSave();
    });
  }

  function renderComments() {
    const layer = document.getElementById('commentsLayer');
    layer.querySelectorAll('.comment-pin').forEach((p) => p.remove());
    comments.forEach((c) => {
      const rect = nodeDomRect(c.hash);
      if (!rect) return;
      const pos = anchorPoint(rect, { x: c.fracX, y: c.fracY });
      const pin = document.createElement('div');
      pin.className = 'comment-pin';
      pin.style.left = pos.x + 'px';
      pin.style.top = pos.y + 'px';
      pin.textContent = '💬';
      pin.title = c.text;
      pin.addEventListener('click', (e) => { e.stopPropagation(); openCommentComposer(c.hash, pos.x, pos.y, c.id); });
      layer.appendChild(pin);
    });
  }

  // ---------------------------------------------------------------------------
  // Telemetry ingestion
  // ---------------------------------------------------------------------------
  const emptyState = document.getElementById('emptyState');

  function ingest(record) {
    if (!record || !record.state_hash) return;
    if (record.session_id) currentSessionId = record.session_id;
    if (record.package_name) currentPackage = record.package_name;

    if (Array.isArray(record.available_elements) && record.available_elements.length) {
      lastElements = record.available_elements;
      const maxX = Math.max(1, ...record.available_elements.map((e) => (e.bounds && e.bounds[2]) || 0));
      const maxY = Math.max(1, ...record.available_elements.map((e) => (e.bounds && e.bounds[3]) || 0));
      lastDims = { w: maxX, h: maxY };
    }

    const isNewNode = !nodesData.get(record.state_hash);
    const existingMeta = nodeMeta.get(record.state_hash) || {};
    const meta = {
      package: record.package_name || existingMeta.package || '',
      activity: record.activity_name || existingMeta.activity || '',
      elements: (record.available_elements && record.available_elements.length)
        ? record.available_elements
        : (existingMeta.elements || []),
      screenshot: record.screenshot_b64 || existingMeta.screenshot || '',
      action: record.executed_action || existingMeta.action || null,
      section: existingMeta.section,
      screenName: record.screen_name || existingMeta.screenName || '',
      screenNumber: record.screen_number || existingMeta.screenNumber || null,
    };
    nodeMeta.set(record.state_hash, meta);

    if (isNewNode) {
      let sectionName = 'Main';
      if (record.section) {
        // Scripted journeys group their own steps (e.g. "Calculation 1: 7 + 5"); trust
        // that over the keypad-label heuristic below, which can't infer test intent.
        sectionName = record.section;
      } else if (record.parent_state_hash) {
        const parentMeta = nodeMeta.get(record.parent_state_hash);
        const parentSection = (parentMeta && parentMeta.section) || 'Main';
        const actionLabel = record.executed_action ? record.executed_action.label : null;
        sectionName = isSectionTrigger(actionLabel) ? actionLabel.trim() : parentSection;
      }
      assignSection(record.state_hash, sectionName);
      nodesData.update({ id: record.state_hash, x: 0, y: 0, fixed: { x: true, y: true } });
      relayoutAll();
      if (autoFitOnNewState) network.fit({ animation: true });
      scheduleAutoSave();
    }

    // Journey steps chain even without a tap (verdict/checkpoint steps carry no action);
    // without this the flow breaks into disconnected fragments at every checkpoint.
    if (record.parent_state_hash && (record.executed_action || record.step_label)) {
      const act = record.executed_action;
      const edgeId = record.parent_state_hash + '->' + record.state_hash + '->' +
        (act ? (act.x || 0) + ',' + (act.y || 0) : 'step');
      if (!edgeMeta.has(edgeId)) {
        edgeMeta.set(edgeId, {
          from: record.parent_state_hash,
          to: record.state_hash,
          fullLabel: act ? 'click: ' + (act.label || '?') : 'then',
        });
      }
    }

    updateStats();
    emptyState.classList.add('hidden');
    scheduleRenderOverlay();
  }

  function updateStats() {
    document.getElementById('statNodes').textContent = nodesData.length + ' states';
    document.getElementById('statEdges').textContent = edgeMeta.size + ' transitions';
  }

  function resetGraph() {
    nodesData.clear();
    nodeMeta.clear();
    edgeMeta.clear();
    sections.clear();
    sectionOrder.length = 0;
    sectionLayout.clear();
    cardElements.forEach((c) => c.remove());
    cardElements.clear();
    textNotes.clear();
    comments.clear();
    nodeStatus.clear();
    selectedIds = new Set();
    currentSessionId = null;
    ['connectorPaths', 'connectorLabels', 'connectorMarkers', 'nodeHeaders', 'sectionHeadings', 'commentsLayer', 'textNotesLayer']
      .forEach((id) => { document.getElementById(id).innerHTML = ''; });
    updateStats();
    emptyState.classList.remove('hidden');
    document.getElementById('statSession').textContent = 'Session — none';
    hideStatus();
  }

  // ---------------------------------------------------------------------------
  // Status banner
  // ---------------------------------------------------------------------------
  const statusBanner = document.getElementById('statusBanner');
  const statusMessage = document.getElementById('statusMessage');
  let statusHideTimer = null;

  function showStatus(message, level) {
    statusBanner.className = 'status-banner visible level-' + (level || 'info');
    statusMessage.textContent = message;
    clearTimeout(statusHideTimer);
    if (level === 'ok') statusHideTimer = setTimeout(hideStatus, 6000);
  }
  function hideStatus() { statusBanner.classList.remove('visible'); }

  // ---------------------------------------------------------------------------
  // Lock / alert popup — a blocking modal for conditions that need the user to
  // go do something physical on the device (e.g. unlock it), not just glance at
  // the thin status banner.
  // ---------------------------------------------------------------------------
  const lockPopupBackdrop = document.getElementById('lockPopupBackdrop');
  const lockPopup = document.getElementById('lockPopup');
  const lockPopupIcon = document.getElementById('lockPopupIcon');
  const lockPopupMessage = document.getElementById('lockPopupMessage');
  const lockPopupDismiss = document.getElementById('lockPopupDismiss');

  function showLockPopup(message, level) {
    lockPopup.className = 'lock-popup level-' + (level || 'warning');
    lockPopupIcon.textContent = level === 'error' ? '⚠️' : '🔒';
    lockPopupMessage.textContent = message;
    lockPopupBackdrop.classList.add('open');
    if (window.Notification && Notification.permission === 'default') {
      Notification.requestPermission();
    }
    if (window.Notification && Notification.permission === 'granted' && document.hidden) {
      new Notification('QA Tester AI', { body: message });
    }
  }
  function hideLockPopup() { lockPopupBackdrop.classList.remove('open'); }

  lockPopupDismiss.addEventListener('click', hideLockPopup);
  lockPopupBackdrop.addEventListener('click', (evt) => {
    if (evt.target === lockPopupBackdrop) hideLockPopup();
  });

  // ---------------------------------------------------------------------------
  // WebSocket
  // ---------------------------------------------------------------------------
  const connIndicator = document.getElementById('connIndicator');
  const connLabel = document.getElementById('connLabel');

  function setConnState(state) {
    connIndicator.classList.remove('online', 'offline');
    if (state === 'online') { connIndicator.classList.add('online'); connLabel.textContent = 'Live'; }
    else if (state === 'offline') { connIndicator.classList.add('offline'); connLabel.textContent = 'Disconnected'; }
    else { connLabel.textContent = 'Connecting…'; }
  }

  function connectWs() {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(proto + '://' + window.location.host + '/ws');

    ws.onopen = () => setConnState('online');
    ws.onclose = () => { setConnState('offline'); setTimeout(connectWs, 2000); };
    ws.onerror = () => setConnState('offline');

    ws.onmessage = (evt) => {
      let msg;
      try { msg = JSON.parse(evt.data); } catch { return; }

      if (msg.type === 'history') {
        (msg.items || []).forEach(ingest);
        if (currentSessionId) document.getElementById('statSession').textContent = 'Session — ' + currentSessionId;
        network.fit({ animation: false });
        scheduleRenderOverlay();
        restoreSavedAnnotations();
      } else if (msg.type === 'telemetry') {
        ingest(msg.payload);
        if (currentSessionId) document.getElementById('statSession').textContent = 'Session — ' + currentSessionId;
      } else if (msg.type === 'clear') {
        resetGraph();
      } else if (msg.type === 'status') {
        showStatus(msg.message, msg.level);
        if (msg.popup) showLockPopup(msg.message, msg.level);
        else if (msg.level === 'ok') hideLockPopup();
      }
    };
  }
  connectWs();

  // ---------------------------------------------------------------------------
  // Dock: zoom, fit, settings popover, observe-flow, fullscreen, live preview,
  // save/import, clear
  // ---------------------------------------------------------------------------
  function updateZoomLabel() {
    document.getElementById('zoomPct').textContent = Math.round(network.getScale() * 100) + '%';
  }
  document.getElementById('zoomInBtn').addEventListener('click', () => {
    network.moveTo({ scale: network.getScale() * 1.2 });
    updateZoomLabel();
  });
  document.getElementById('zoomOutBtn').addEventListener('click', () => {
    network.moveTo({ scale: network.getScale() / 1.2 });
    updateZoomLabel();
  });
  document.getElementById('fitBtn').addEventListener('click', () => {
    network.fit({ animation: true });
    setTimeout(updateZoomLabel, 350);
  });
  network.on('zoom', updateZoomLabel);

  const settingsBtn = document.getElementById('settingsBtn');
  const settingsPopover = document.getElementById('settingsPopover');
  const dockEl = document.getElementById('dock');

  function positionSettingsPopover() {
    // Anchor under the settings button specifically (not the dock's center) so it
    // stays correctly placed no matter where the button sits in the bar.
    const dockRect = dockEl.getBoundingClientRect();
    const btnRect = settingsBtn.getBoundingClientRect();
    const popW = settingsPopover.offsetWidth || 230;
    let left = (btnRect.left - dockRect.left) + btnRect.width / 2 - popW / 2;
    left = Math.max(4, Math.min(left, dockRect.width - popW - 4));
    settingsPopover.style.left = left + 'px';
  }

  settingsBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const opening = !settingsPopover.classList.contains('open');
    settingsPopover.classList.toggle('open', opening);
    if (opening) positionSettingsPopover();
  });
  document.addEventListener('click', (e) => {
    if (!settingsPopover.contains(e.target) && e.target !== settingsBtn) settingsPopover.classList.remove('open');
  });

  const labelLengthSlider = document.getElementById('labelLengthSlider');
  const labelLengthValue = document.getElementById('labelLengthValue');
  labelLengthSlider.addEventListener('input', () => {
    labelLength = parseInt(labelLengthSlider.value, 10);
    labelLengthValue.textContent = labelLength;
    scheduleRenderOverlay();
  });

  const connectorLengthSlider = document.getElementById('connectorLengthSlider');
  const connectorLengthValue = document.getElementById('connectorLengthValue');
  connectorLengthSlider.addEventListener('input', () => {
    connectorPct = parseInt(connectorLengthSlider.value, 10);
    connectorLengthValue.textContent = connectorPct + '%';
    relayoutAll();
  });

  document.getElementById('resetLayoutBtn').addEventListener('click', () => {
    relayoutAll();
    network.fit({ animation: true });
  });

  document.getElementById('toggleTapMarkers').addEventListener('change', (e) => {
    showTapMarkers = e.target.checked;
    scheduleRenderOverlay();
  });
  document.getElementById('toggleHeadings').addEventListener('change', (e) => {
    showHeadings = e.target.checked;
    scheduleRenderOverlay();
  });
  document.getElementById('toggleGridBg').addEventListener('change', (e) => {
    container.classList.toggle('no-grid', !e.target.checked);
  });
  document.getElementById('toggleAutoFit').addEventListener('change', (e) => {
    autoFitOnNewState = e.target.checked;
  });

  document.getElementById('observeBtn').addEventListener('click', (e) => {
    observeMode = !observeMode;
    graphWrap.classList.toggle('observe-mode', observeMode);
    e.currentTarget.classList.toggle('active', observeMode);
  });

  document.getElementById('fullscreenBtn').addEventListener('click', (e) => {
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch(() => {});
      e.currentTarget.classList.add('active');
    } else {
      document.exitFullscreen().catch(() => {});
      e.currentTarget.classList.remove('active');
    }
  });

  document.getElementById('clearBtn').addEventListener('click', async () => {
    try { await fetch('/clear', { method: 'POST' }); } catch { /* clear locally regardless */ }
    resetGraph();
  });

  // -- Live preview sidebar --
  let livePollTimer = null;
  const liveSidebar = document.getElementById('liveSidebar');
  const livePreviewBtn = document.getElementById('livePreviewBtn');

  async function pollLiveFrame() {
    try {
      const resp = await fetch('/command', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command: 'screenshot' }),
      });
      const data = await resp.json().catch(() => ({}));
      if (resp.ok && data.screenshot_b64) {
        const img = document.getElementById('liveScreen');
        img.src = 'data:image/jpeg;base64,' + data.screenshot_b64;
        img.classList.add('loaded');
        document.getElementById('livePlaceholder').classList.add('hidden');
        drawLiveOverlay();
      }
    } catch { /* device may be briefly unreachable between polls */ }
  }

  function drawLiveOverlay() {
    const layer = document.getElementById('liveOverlayLayer');
    layer.innerHTML = '';
    if (!document.getElementById('liveOverlayToggle').checked || !lastElements.length) return;
    const img = document.getElementById('liveScreen');
    const rect = img.getBoundingClientRect();
    if (!rect.width) return;
    const sx = rect.width / lastDims.w, sy = rect.height / lastDims.h;
    lastElements.forEach((el) => {
      if (!el.bounds) return;
      const [x1, y1, x2, y2] = el.bounds;
      const box = document.createElement('div');
      box.className = 'overlay-box';
      box.style.left = (x1 * sx) + 'px';
      box.style.top = (y1 * sy) + 'px';
      box.style.width = Math.max(2, (x2 - x1) * sx) + 'px';
      box.style.height = Math.max(2, (y2 - y1) * sy) + 'px';
      layer.appendChild(box);
    });
  }

  livePreviewBtn.addEventListener('click', () => {
    const opening = !liveSidebar.classList.contains('open');
    liveSidebar.classList.toggle('open', opening);
    livePreviewBtn.classList.toggle('active', opening);
    if (opening) {
      pollLiveFrame();
      livePollTimer = setInterval(pollLiveFrame, 1800);
    } else if (livePollTimer) {
      clearInterval(livePollTimer);
      livePollTimer = null;
    }
  });
  document.getElementById('liveSidebarClose').addEventListener('click', () => livePreviewBtn.click());

  // -- Live sidebar resize (drag its left edge to make it narrower/wider) --
  const liveResizeHandle = document.getElementById('liveResizeHandle');
  liveResizeHandle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = liveSidebar.getBoundingClientRect().width;
    liveResizeHandle.classList.add('dragging');
    document.body.style.userSelect = 'none';

    function onMove(ev) {
      const next = startWidth + (startX - ev.clientX);
      const clamped = Math.max(200, Math.min(520, next));
      liveSidebar.style.width = clamped + 'px';
      drawLiveOverlay();
    }
    function onUp() {
      liveResizeHandle.classList.remove('dragging');
      document.body.style.userSelect = '';
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  });
  document.getElementById('liveOverlayToggle').addEventListener('change', drawLiveOverlay);

  // -- Save / Import --
  function buildProjectBlob() {
    return {
      version: 1,
      savedAt: new Date().toISOString(),
      sessionId: currentSessionId,
      nodes: Array.from(nodeMeta.entries()).map(([hash, meta]) => ({ hash, ...meta })),
      edges: Array.from(edgeMeta.entries()).map(([id, meta]) => ({ id, ...meta })),
      sectionOrder,
      sections: Array.from(sections.entries()).map(([name, list]) => ({ name, list })),
      notes: Array.from(textNotes.values()),
      comments: Array.from(comments.values()),
      nodeStatus: Array.from(nodeStatus.entries()).map(([hash, s]) => ({ hash, ...s })),
      // Screen positions are the arrangement itself: sections dragged into parallel
      // columns are a hand-made layout that `relayoutAll` cannot reproduce, so without
      // this a reload silently collapses the board back into one column.
      nodePositions: buildNodePositions(),
    };
  }

  function buildNodePositions() {
    const out = [];
    try {
      const ids = [];
      nodesData.forEach((n) => ids.push(n.id));
      if (!ids.length) return out;
      const pos = network.getPositions(ids);
      ids.forEach((id) => {
        const p = pos[id];
        if (p) out.push({ hash: id, x: Math.round(p.x), y: Math.round(p.y) });
      });
    } catch { /* a layout we cannot read must not block the save */ }
    return out;
  }

  // Put saved screen positions back. Returns how many were applied so callers can tell
  // whether to fall back to the automatic layout.
  function applyNodePositions(list) {
    if (!Array.isArray(list) || !list.length) return 0;
    const updates = [];
    list.forEach((p) => {
      if (!p || typeof p.x !== 'number' || typeof p.y !== 'number') return;
      if (!nodesData.get(p.hash)) return;
      updates.push({ id: p.hash, x: p.x, y: p.y, fixed: { x: true, y: true } });
    });
    if (updates.length) { nodesData.update(updates); scheduleRenderOverlay(); }
    return updates.length;
  }

  document.getElementById('saveBtn').addEventListener('click', () => {
    const project = buildProjectBlob();
    const blob = new Blob([JSON.stringify(project)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `android-agent-flow-${(currentSessionId || 'session')}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    if (currentPackage) persistProjectToServer();
  });

  // -- Projects (server-persisted, one per app package) --
  function scheduleAutoSave() {
    if (!currentPackage) return;
    clearTimeout(autoSaveTimer);
    autoSaveTimer = setTimeout(persistProjectToServer, 2000);
  }

  // A reload rebuilds the graph from telemetry history, which carries no annotations — and
  // the ingest that follows schedules an autosave. Without this, that autosave writes a
  // note-less blob over the saved project and silently destroys every text note and comment
  // pin the user had added. Pull them back before the autosave fires.
  async function restoreSavedAnnotations() {
    if (!currentPackage || textNotes.size || comments.size || nodeStatus.size) return;
    try {
      const resp = await fetch(`/projects/${encodeURIComponent(currentPackage)}/flow-graph`);
      if (!resp.ok) return;
      const saved = await resp.json();
      (saved.notes || []).forEach((n) => textNotes.set(n.id, n));
      (saved.comments || []).forEach((c) => comments.set(c.id, c));
      // Defect markings are rebuilt from the same saved blob as the annotations —
      // telemetry alone cannot say which screens a reviewer judged broken.
      (saved.nodeStatus || []).forEach((s) => nodeStatus.set(s.hash, s));
      // Telemetry replay lays the graph out automatically; a saved arrangement must win
      // over that, or every reload throws away however the board was actually organised.
      const placed = applyNodePositions(saved.nodePositions);
      if (placed) network.fit({ animation: false });
      if (textNotes.size || comments.size || nodeStatus.size) scheduleRenderOverlay();
    } catch { /* annotations are a nicety; never break the graph over them */ }
  }

  async function persistProjectToServer() {
    if (!currentPackage) return;
    try {
      await fetch(`/projects/${encodeURIComponent(currentPackage)}/flow-graph`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildProjectBlob()),
      });
    } catch (err) {
      // Best-effort — the live dashboard/telemetry keeps working even if this fails.
      console.warn('Could not auto-save project:', err);
    }
  }

  async function fetchProjects() {
    refreshSaveProjectBtn();
    const list = document.getElementById('projectsList');
    const empty = document.getElementById('projectsEmpty');
    let projects = [];
    try {
      const resp = await fetch('/projects');
      projects = await resp.json();
    } catch (err) {
      showStatus('Could not load projects: ' + err.message, 'error');
      return;
    }
    list.querySelectorAll('.project-row').forEach((el) => el.remove());
    empty.classList.toggle('hidden', projects.length > 0);
    projects.forEach((p) => {
      const row = document.createElement('div');
      row.className = 'project-row';
      const lastRun = p.last_run_at ? new Date(p.last_run_at).toLocaleString() : 'never';
      row.innerHTML = `
        <div class="project-row-main">
          <div class="project-row-package">${escapeHtml(p.package)}</div>
          <div class="project-row-meta">last tested: ${escapeHtml(lastRun)}</div>
        </div>
        <div class="project-row-stats">
          <div class="stat-pill">${p.state_count || 0} states</div>
          <div class="stat-pill">${p.edge_count || 0} transitions</div>
        </div>`;
      row.addEventListener('click', () => openProject(p.package));
      list.appendChild(row);
    });
  }

  async function openProject(pkg) {
    currentPackage = pkg;
    try {
      const resp = await fetch(`/projects/${encodeURIComponent(pkg)}/flow-graph`);
      if (resp.status === 404) {
        resetGraph();
        showStatus(`Project "${pkg}" has no saved runs yet — start exploring to populate it.`, 'info');
        return;
      }
      const project = await resp.json();
      loadProject(project);
      document.querySelector('.tab[data-tab="graph"]').click();
    } catch (err) {
      showStatus('Could not open project: ' + err.message, 'error');
    }
  }

  // Explicit save of the open board. Autosave already runs on a 2s debounce, but it is
  // invisible and easy to distrust — and it does not fire at all if the last change was
  // a pan or zoom. This is the button you press before closing the laptop.
  const saveProjectBtn = document.getElementById('saveProjectBtn');
  const saveProjectHint = document.getElementById('saveProjectHint');

  // Looks the element up rather than closing over the const above: this is called from
  // fetchProjects, which is declared earlier in the file, so a closure would risk a
  // temporal-dead-zone throw if the projects view ever renders during startup.
  function refreshSaveProjectBtn() {
    const btn = document.getElementById('saveProjectBtn');
    if (!btn) return;
    const ready = Boolean(currentPackage) && nodesData.length > 0;
    btn.disabled = !ready;
    btn.textContent = ready ? `Save "${currentPackage}"` : 'Save current project';
  }

  if (saveProjectBtn) {
    saveProjectBtn.addEventListener('click', async () => {
      if (!currentPackage) return;
      const original = saveProjectBtn.textContent;
      saveProjectBtn.disabled = true;
      saveProjectBtn.textContent = 'Saving…';
      try {
        const blob = buildProjectBlob();
        const resp = await fetch(`/projects/${encodeURIComponent(currentPackage)}/flow-graph`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(blob),
        });
        if (!resp.ok) throw new Error(`server said ${resp.status}`);
        saveProjectBtn.textContent = 'Saved ✓';
        if (saveProjectHint) {
          saveProjectHint.textContent =
            `Saved ${blob.nodePositions.length} screen positions, ${blob.notes.length} notes `
            + `and ${blob.comments.length} comment pins at ${new Date().toLocaleTimeString()}.`;
        }
        fetchProjects();
        setTimeout(() => { saveProjectBtn.textContent = original; refreshSaveProjectBtn(); }, 2000);
      } catch (err) {
        saveProjectBtn.textContent = 'Save failed';
        showStatus('Could not save project: ' + err.message, 'error');
        setTimeout(() => { saveProjectBtn.textContent = original; refreshSaveProjectBtn(); }, 2500);
      }
    });
  }

  document.getElementById('newProjectBtn').addEventListener('click', async () => {
    const input = document.getElementById('newProjectPackage');
    const pkg = input.value.trim();
    if (!pkg) return;
    try {
      await fetch('/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ package: pkg }),
      });
      input.value = '';
      fetchProjects();
    } catch (err) {
      showStatus('Could not create project: ' + err.message, 'error');
    }
  });

  const importFile = document.getElementById('importFile');
  document.getElementById('importBtn').addEventListener('click', () => importFile.click());
  importFile.addEventListener('change', () => {
    const file = importFile.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const project = JSON.parse(reader.result);
        loadProject(project);
      } catch (err) {
        showStatus('Could not read project file: ' + err.message, 'error');
      }
    };
    reader.readAsText(file);
    importFile.value = '';
  });

  function loadProject(project) {
    resetGraph();
    currentSessionId = project.sessionId || null;
    (project.nodes || []).forEach((n) => {
      nodeMeta.set(n.hash, {
        package: n.package, activity: n.activity, elements: n.elements || [], screenshot: n.screenshot,
        action: n.action, section: n.section, screenName: n.screenName || '', screenNumber: n.screenNumber || null,
      });
      nodesData.update({ id: n.hash, x: 0, y: 0, fixed: { x: true, y: true } });
    });
    (project.sections || []).forEach((s) => sections.set(s.name, s.list.slice()));
    sectionOrder.push(...(project.sectionOrder || []));
    (project.edges || []).forEach((e) => edgeMeta.set(e.id, { from: e.from, to: e.to, fullLabel: e.fullLabel }));
    (project.notes || []).forEach((n) => textNotes.set(n.id, n));
    (project.comments || []).forEach((c) => comments.set(c.id, c));
    (project.nodeStatus || []).forEach((s) => nodeStatus.set(s.hash, s));
    // Lay out first so anything the save predates still lands somewhere sensible, then
    // let the saved arrangement overwrite it.
    relayoutAll();
    applyNodePositions(project.nodePositions);
    // vis hasn't drawn the new nodes yet on this frame, so comment pins (which need a
    // node's DOM rect) and text notes render into nothing and stay invisible until the
    // user happens to pan or resize. Re-render once the layout has actually settled.
    network.once('afterDrawing', scheduleRenderOverlay);
    setTimeout(scheduleRenderOverlay, 250);
    updateStats();
    if (nodesData.length) emptyState.classList.add('hidden');
    if (currentSessionId) document.getElementById('statSession').textContent = 'Session — ' + currentSessionId;
    showStatus(`Loaded project — ${nodesData.length} states, ${edgeMeta.size} transitions.`, 'ok');
  }

  // ---------------------------------------------------------------------------
  // Tabs
  // ---------------------------------------------------------------------------
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
      document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById('view-' + tab.dataset.tab).classList.add('active');
      if (tab.dataset.tab === 'graph') { network.redraw(); scheduleRenderOverlay(); }
      if (tab.dataset.tab === 'projects') fetchProjects();
    });
  });

  // ---------------------------------------------------------------------------
  // Modal
  // ---------------------------------------------------------------------------
  const modalBackdrop = document.getElementById('modalBackdrop');

  function openModal(hash) {
    const meta = nodeMeta.get(hash);
    if (!meta) return;
    document.getElementById('modalImage').src = screenshotSrc(meta.screenshot) || PLACEHOLDER_IMG;
    document.getElementById('modalPackage').textContent = meta.package || 'Unknown package';
    document.getElementById('modalActivity').textContent = meta.activity || '';
    document.getElementById('modalScreenName').textContent = meta.screenNumber
      ? `#${meta.screenNumber} ${meta.screenName || ''}`
      : (meta.screenName || '—');
    document.getElementById('modalHash').textContent = hash;
    document.getElementById('modalElementCount').textContent = (meta.elements || []).length;
    document.getElementById('modalAction').textContent = meta.action ? ('click: ' + meta.action.label) : 'Entry / start state';

    const list = document.getElementById('modalElements');
    list.innerHTML = '';
    (meta.elements || []).slice(0, 40).forEach((el) => {
      const row = document.createElement('div');
      row.className = 'modal-element-row';
      row.innerHTML = `<span class="lbl">${escapeHtml(el.label || '')}</span><span class="coord">${el.x}, ${el.y}</span>`;
      list.appendChild(row);
    });

    modalBackdrop.classList.add('open');
  }

  document.getElementById('modalClose').addEventListener('click', () => modalBackdrop.classList.remove('open'));
  modalBackdrop.addEventListener('click', (e) => { if (e.target === modalBackdrop) modalBackdrop.classList.remove('open'); });

  // ---------------------------------------------------------------------------
  // Remote control panel
  // ---------------------------------------------------------------------------
  const phoneScreen = document.getElementById('phoneScreen');
  const phonePlaceholder = document.getElementById('phonePlaceholder');
  const overlayLayer = document.getElementById('overlayLayer');
  const overlayToggle = document.getElementById('overlayToggle');
  const chatLog = document.getElementById('chatLog');

  function setPhoneFrame(b64) {
    if (!b64) return;
    phoneScreen.src = 'data:image/jpeg;base64,' + b64;
    phoneScreen.classList.add('loaded');
    phonePlaceholder.classList.add('hidden');
    drawOverlay();
  }

  function drawOverlay() {
    overlayLayer.innerHTML = '';
    if (!overlayToggle.checked || !lastElements.length) return;
    const rect = phoneScreen.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const sx = rect.width / lastDims.w;
    const sy = rect.height / lastDims.h;
    lastElements.forEach((el) => {
      if (!el.bounds) return;
      const [x1, y1, x2, y2] = el.bounds;
      const box = document.createElement('div');
      box.className = 'overlay-box';
      box.style.left = (x1 * sx) + 'px';
      box.style.top = (y1 * sy) + 'px';
      box.style.width = Math.max(2, (x2 - x1) * sx) + 'px';
      box.style.height = Math.max(2, (y2 - y1) * sy) + 'px';
      box.title = el.label || '';
      overlayLayer.appendChild(box);
    });
  }
  overlayToggle.addEventListener('change', drawOverlay);
  window.addEventListener('resize', drawOverlay);

  function ripple(px, py) {
    const dot = document.createElement('div');
    dot.className = 'tap-ripple';
    dot.style.left = px + 'px';
    dot.style.top = py + 'px';
    overlayLayer.appendChild(dot);
    setTimeout(() => dot.remove(), 650);
  }

  function appendChat(kind, text) {
    const row = document.createElement('div');
    row.className = 'chat-entry ' + kind;
    row.textContent = text;
    chatLog.appendChild(row);
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  async function sendCommand(command) {
    appendChat('cmd', command);
    try {
      const resp = await fetch('/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        appendChat('error', (data && data.detail) || ('Command failed (' + resp.status + ')'));
        return;
      }
      appendChat('ok', 'Executed: ' + data.command);
      if (data.screenshot_b64) setPhoneFrame(data.screenshot_b64);
    } catch (err) {
      appendChat('error', 'Network error: ' + err.message);
    }
  }

  document.getElementById('chatForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const input = document.getElementById('chatInput');
    const value = input.value.trim();
    if (!value) return;
    input.value = '';
    sendCommand(value);
  });

  document.querySelectorAll('.chip-btn[data-cmd]').forEach((btn) => {
    btn.addEventListener('click', () => sendCommand(btn.dataset.cmd));
  });

  document.getElementById('refreshFrameBtn').addEventListener('click', () => sendCommand('screenshot'));

  phoneScreen.addEventListener('click', (evt) => {
    const rect = phoneScreen.getBoundingClientRect();
    const px = evt.clientX - rect.left;
    const py = evt.clientY - rect.top;
    ripple(px, py);
    const devX = Math.round((px / rect.width) * lastDims.w);
    const devY = Math.round((py / rect.height) * lastDims.h);
    sendCommand(`tap ${devX} ${devY}`);
  });

  updateStats();
})();
