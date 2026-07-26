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

      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', d);
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', '#0099ff');
      path.setAttribute('stroke-width', '2');
      path.setAttribute('marker-end', 'url(#arrowBlue)');
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
    if (e.key === 'v' || e.key === 'V') setTool('pan');
    if (e.key === 's' || e.key === 'S') setTool('select');
    if (e.key === 't' || e.key === 'T') setTool('text');
    if (e.key === 'c' || e.key === 'C') setTool('comment');
  });

  let dragState = null;

  toolOverlay.addEventListener('mousedown', (e) => {
    const rect = toolOverlay.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;

    if (currentTool === 'select') {
      const hitId = hitTestNode(x, y);
      if (hitId) {
        if (!selectedIds.has(hitId)) { selectedIds = new Set([hitId]); scheduleRenderOverlay(); }
        const origins = new Map();
        selectedIds.forEach((id) => {
          const pos = network.getPositions([id])[id];
          if (pos) origins.set(id, { x: pos.x, y: pos.y });
        });
        dragState = { mode: 'group', startX: x, startY: y, origins };
      } else {
        dragState = { mode: 'marquee', startX: x, startY: y };
        selectionBox.classList.add('visible');
        Object.assign(selectionBox.style, { left: x + 'px', top: y + 'px', width: '0px', height: '0px' });
      }
    } else if (currentTool === 'text') {
      addTextNoteAt(x, y);
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
      dragState.origins.forEach((orig, id) => updates.push({ id, x: orig.x + dx, y: orig.y + dy }));
      nodesData.update(updates);
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
      nodesData.forEach((n) => {
        const r = nodeDomRect(n.id);
        if (!r) return;
        const overlap = !(r.left > bx2 || r.left + r.width < bx1 || r.top > by2 || r.top + r.height < by1);
        if (overlap) newSel.add(n.id);
      });
      selectedIds = newSel;
      scheduleRenderOverlay();
    }
    dragState = null;
  });

  // ---------------------------------------------------------------------------
  // Text notes
  // ---------------------------------------------------------------------------
  function addTextNoteAt(domX, domY) {
    const canvasPos = network.DOMtoCanvas({ x: domX, y: domY });
    const id = 'note-' + Math.random().toString(36).slice(2, 10);
    textNotes.set(id, { id, cx: canvasPos.x, cy: canvasPos.y, text: 'Note', fontSize: 16 });
    setTool('pan');
    scheduleRenderOverlay();
    scheduleAutoSave();
  }

  function renderTextNotes() {
    const layer = document.getElementById('textNotesLayer');
    layer.innerHTML = '';
    textNotes.forEach((note) => {
      const pos = network.canvasToDOM({ x: note.cx, y: note.cy });
      const el = document.createElement('div');
      el.className = 'text-note';
      el.style.left = pos.x + 'px';
      el.style.top = pos.y + 'px';
      el.innerHTML = `
        <div class="text-note-drag">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><circle cx="8" cy="6" r="1.6"></circle><circle cx="8" cy="12" r="1.6"></circle><circle cx="8" cy="18" r="1.6"></circle><circle cx="16" cy="6" r="1.6"></circle><circle cx="16" cy="12" r="1.6"></circle><circle cx="16" cy="18" r="1.6"></circle></svg>
        </div>
        <div class="text-note-content" contenteditable="true" style="font-size:${note.fontSize}px;">${escapeHtml(note.text)}</div>
        <div class="text-note-delete">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </div>
      `;
      el.querySelector('.text-note-content').addEventListener('blur', (e) => { note.text = e.target.innerText; scheduleAutoSave(); });
      el.querySelector('.text-note-delete').addEventListener('click', () => { textNotes.delete(note.id); scheduleRenderOverlay(); scheduleAutoSave(); });
      el.querySelector('.text-note-drag').addEventListener('mousedown', (e) => startNoteDrag(note, e));
      layer.appendChild(el);
    });
  }

  function startNoteDrag(note, e) {
    e.preventDefault();
    e.stopPropagation();
    const startClientX = e.clientX, startClientY = e.clientY;
    const startCx = note.cx, startCy = note.cy;
    function onMove(ev) {
      const scale = network.getScale();
      note.cx = startCx + (ev.clientX - startClientX) / scale;
      note.cy = startCy + (ev.clientY - startClientY) / scale;
      scheduleRenderOverlay();
    }
    function onUp() {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
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
    };
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
    if (!currentPackage || textNotes.size || comments.size) return;
    try {
      const resp = await fetch(`/projects/${encodeURIComponent(currentPackage)}/flow-graph`);
      if (!resp.ok) return;
      const saved = await resp.json();
      (saved.notes || []).forEach((n) => textNotes.set(n.id, n));
      (saved.comments || []).forEach((c) => comments.set(c.id, c));
      if (textNotes.size || comments.size) scheduleRenderOverlay();
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
    relayoutAll();
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
