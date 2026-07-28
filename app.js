(function () {
  'use strict';

  const canvasEl = document.getElementById('canvas');
  const canvasAreaEl = document.getElementById('canvasArea');
  const screensEl = document.getElementById('screens');
  const stickyNotesEl = document.getElementById('stickyNotes');
  const arrowPathsEl = document.getElementById('arrowPaths');
  const arrowLabelsEl = document.getElementById('arrowLabels');
  const markersEl = document.getElementById('markers');
  const textBlocksEl = document.getElementById('textBlocks');
  const toolSelectBtn = document.getElementById('toolSelect');
  const toolNoteBtn = document.getElementById('toolNote');
  const toolTextBtn = document.getElementById('toolText');
  const zoomPctEl = document.getElementById('zoomPct');
  const zoomInBtn = document.getElementById('zoomIn');
  const zoomOutBtn = document.getElementById('zoomOut');
  const fitScreenBtn = document.getElementById('fitScreen');

  const screensCfg = [
    { id: 'login', name: 'Login', meta: 'iOS · tested 2h ago', status: 'working', placeholder: 'Login screen' },
    { id: 'home', name: 'Home feed', meta: 'iOS · tested 2h ago', status: 'working', placeholder: 'Home feed screen' },
    { id: 'search', name: 'Search results', meta: 'iOS · tested 1d ago', status: 'working', placeholder: 'Search results screen' },
    { id: 'product', name: 'Product detail', meta: 'iOS · tested 2h ago', status: 'working', placeholder: 'Product detail screen' },
    { id: 'cart', name: 'Cart', meta: 'iOS · tested 3h ago', status: 'working', placeholder: 'Cart screen' },
    { id: 'checkout', name: 'Checkout', meta: 'broken', status: 'broken', placeholder: 'Checkout screen', badge: 'Bug #142' },
    { id: 'confirmation', name: 'Order confirmation', meta: 'untested', status: 'untested', placeholder: 'Order confirmation screen' },
  ];

  const entryLeft = { dx: 0, dy: 250 };
  const entryTop = { dx: 110, dy: 56 };

  const arrowDefs = [
    { id: 'a1', from: 'login', fromOff: { dx: 90, dy: 390 }, to: 'home', toOff: entryLeft, vertical: false, label: 'Tap: Sign in' },
    { id: 'a2', from: 'home', fromOff: { dx: 160, dy: 180 }, to: 'product', toOff: entryLeft, vertical: false, label: 'Tap: Product card' },
    { id: 'a3', from: 'home', fromOff: { dx: 30, dy: 40 }, to: 'search', toOff: entryTop, vertical: true, label: 'Tap: Search icon' },
    { id: 'a4', from: 'product', fromOff: { dx: 110, dy: 390 }, to: 'cart', toOff: entryLeft, vertical: false, label: 'Tap: Add to cart' },
    { id: 'a5', from: 'cart', fromOff: { dx: 110, dy: 390 }, to: 'checkout', toOff: entryLeft, vertical: false, label: 'Tap: Checkout' },
    { id: 'a6', from: 'checkout', fromOff: { dx: 110, dy: 390 }, to: 'confirmation', toOff: entryTop, vertical: true, label: 'Tap: Place order — crashes', broken: true },
  ];

  const state = {
    positions: {
      login: { x: 80, y: 220 },
      home: { x: 420, y: 220 },
      search: { x: 420, y: 760 },
      product: { x: 760, y: 220 },
      cart: { x: 1100, y: 220 },
      checkout: { x: 1440, y: 220 },
      confirmation: { x: 1440, y: 760 },
    },
    selectedId: null,
    tool: 'select',
    stickyNotes: [
      {
        id: 'note-1',
        x: 1670,
        y: 220,
        theme: 'default',
        title: 'Note Title',
        openMenu: false,
        content: `<div class="note-section-title">WHY TWO WITNESSES</div><p>So each result is checked twice:<br>1. the live preview read BEFORE '=' (cannot be stale)<br>2. the value after '='</p><p>Both must match, or a calculation that never ran could pass.</p>`
      }
    ],
    textBlocks: [],
  };

  let drag = null; // { id, kind, startX, startY, origX, origY, moved }

  function mkPath(sx, sy, tx, ty, vertical) {
    return vertical
      ? `M${sx},${sy} C${sx},${sy + 40} ${tx},${ty - 40} ${tx},${ty}`
      : `M${sx},${sy} C${sx + 50},${sy} ${tx - 50},${ty} ${tx},${ty}`;
  }

  function render() {
    // ----- SCREENS -----
    screensEl.innerHTML = '';
    const byId = {};
    screensCfg.forEach((cfg) => {
      const pos = state.positions[cfg.id];
      byId[cfg.id] = { ...cfg, x: pos.x, y: pos.y };

      const selected = state.selectedId === cfg.id;
      let dotColor = '#22c55e', metaColor = 'var(--ink-muted)';
      if (cfg.status === 'broken') { dotColor = '#ff5577'; metaColor = '#ff5577'; }
      if (cfg.status === 'untested') { dotColor = 'var(--ink-muted)'; }

      const el = document.createElement('div');
      el.className = 'screen' + (selected ? ' selected' : '');
      el.style.left = pos.x + 'px';
      el.style.top = pos.y + 'px';

      let frameBorder = '1px solid var(--hairline)';
      let frameOpacity = 1;
      if (cfg.status === 'broken') frameBorder = '1px solid #ff5577';
      if (cfg.status === 'untested') { frameBorder = '1px dashed var(--hairline)'; frameOpacity = 0.6; }
      let frameShadow = 'var(--elevation-float)';
      if (cfg.status === 'broken') frameShadow = 'inset 0 0.5px rgba(255,255,255,.10), 0 10px 30px rgba(255,85,119,.15)';
      if (selected) { frameBorder = '2px solid #0099ff'; frameShadow += ', 0 0 0 4px rgba(0,153,255,0.15)'; }

      el.innerHTML = `
        <div class="screen-head">
          <span class="screen-dot" style="background:${dotColor};"></span>
          <div class="screen-meta-col">
            <span class="screen-name">${cfg.name}</span>
            <span class="screen-meta" style="color:${metaColor};">${cfg.meta}</span>
          </div>
        </div>
        <div class="screen-frame" style="border:${frameBorder}; box-shadow:${frameShadow}; opacity:${frameOpacity};">
          ${cfg.badge ? `<div class="screen-badge">${cfg.badge}</div>` : ''}
          <div class="screen-img">${cfg.placeholder}</div>
        </div>
      `;

      el.addEventListener('mousedown', (e) => startDrag(cfg.id, 'screen', e));
      screensEl.appendChild(el);
    });

    // ----- ARROWS -----
    arrowPathsEl.innerHTML = '';
    arrowLabelsEl.innerHTML = '';
    markersEl.innerHTML = '';

    arrowDefs.forEach((a) => {
      const s = byId[a.from], t = byId[a.to];
      const sx = s.x + a.fromOff.dx, sy = s.y + a.fromOff.dy;
      const tx = t.x + a.toOff.dx, ty = t.y + a.toOff.dy;
      const stroke = a.broken ? '#ff5577' : '#0099ff';
      const dash = a.broken ? '5 4' : '';
      const markerUrl = a.broken ? 'url(#arrowRed)' : 'url(#arrowBlue)';

      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', mkPath(sx, sy, tx, ty, a.vertical));
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', stroke);
      path.setAttribute('stroke-width', '2');
      if (dash) path.setAttribute('stroke-dasharray', dash);
      path.setAttribute('marker-end', markerUrl);
      arrowPathsEl.appendChild(path);

      const labelX = (sx + tx) / 2;
      const labelY = (sy + ty) / 2 - (a.vertical ? 0 : 18);
      const label = document.createElement('div');
      label.className = 'arrow-label';
      label.style.left = labelX + 'px';
      label.style.top = labelY + 'px';
      label.style.background = a.broken ? 'rgba(255,85,119,0.15)' : 'var(--surface-2)';
      label.style.color = a.broken ? '#ff5577' : 'var(--ink-muted)';
      label.style.border = a.broken ? '1px solid rgba(255,85,119,0.4)' : 'none';
      label.textContent = a.label;
      arrowLabelsEl.appendChild(label);

      const marker = document.createElement('div');
      marker.className = 'tap-marker';
      marker.style.left = sx + 'px';
      marker.style.top = sy + 'px';
      marker.innerHTML = `
        <div class="ring" style="background:${a.broken ? 'rgba(255,85,119,0.4)' : 'rgba(0,153,255,0.4)'};"></div>
        <div class="dot" style="background:${a.broken ? '#ff5577' : '#0099ff'};"></div>
      `;
      markersEl.appendChild(marker);
    });

    // ----- STICKY NOTES -----
    if (stickyNotesEl) {
      stickyNotesEl.innerHTML = '';
      state.stickyNotes.forEach((note) => {
        const selected = state.selectedId === note.id;
        const el = document.createElement('div');
        el.className = 'sticky-note' + (selected ? ' selected' : '');
        el.style.left = note.x + 'px';
        el.style.top = note.y + 'px';

        el.innerHTML = `
          <div class="sticky-note-inner">
            <div class="sticky-note-header theme-${note.theme || 'default'}">
              <div class="sticky-note-title" contenteditable="true" spellcheck="false">${note.title}</div>
              <div class="sticky-note-actions">
                <button class="sticky-note-btn menu-btn" title="Options">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="6" x2="21" y2="6"></line><line x1="3" y1="12" x2="21" y2="12"></line><line x1="3" y1="18" x2="21" y2="18"></line></svg>
                </button>
                <button class="sticky-note-btn close-btn" title="Delete note">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                </button>
                ${note.openMenu ? `
                  <div class="sticky-note-menu">
                    <div class="menu-item" data-action="theme-default"><span style="width:10px;height:10px;border-radius:50%;background:#dedede;display:inline-block;"></span> Default Gray</div>
                    <div class="menu-item" data-action="theme-yellow"><span style="width:10px;height:10px;border-radius:50%;background:#fef08a;display:inline-block;"></span> Yellow</div>
                    <div class="menu-item" data-action="theme-cyan"><span style="width:10px;height:10px;border-radius:50%;background:#a5f3fc;display:inline-block;"></span> Cyan</div>
                    <div class="menu-item" data-action="theme-mint"><span style="width:10px;height:10px;border-radius:50%;background:#bbf7d0;display:inline-block;"></span> Mint</div>
                    <div class="menu-item" data-action="theme-purple"><span style="width:10px;height:10px;border-radius:50%;background:#e9d5ff;display:inline-block;"></span> Purple</div>
                    <div class="menu-item" data-action="duplicate">Duplicate</div>
                    <div class="menu-item" data-action="delete" style="color:var(--danger);">Delete</div>
                  </div>
                ` : ''}
              </div>
            </div>
            <div class="sticky-note-body" contenteditable="true" spellcheck="false">${note.content}</div>
          </div>
        `;

        const headerEl = el.querySelector('.sticky-note-header');
        const titleEl = el.querySelector('.sticky-note-title');
        const bodyEl = el.querySelector('.sticky-note-body');
        const menuBtn = el.querySelector('.menu-btn');
        const closeBtn = el.querySelector('.close-btn');

        headerEl.addEventListener('mousedown', (e) => {
          if (e.target === titleEl || e.target.closest('.sticky-note-actions')) return;
          state.selectedId = note.id;
          startDrag(note.id, 'stickyNote', e);
        });

        el.addEventListener('mousedown', () => {
          if (state.selectedId !== note.id) {
            state.selectedId = note.id;
            render();
          }
        });

        titleEl.addEventListener('blur', (e) => {
          note.title = e.target.innerText;
        });

        bodyEl.addEventListener('blur', (e) => {
          note.content = e.target.innerHTML;
        });

        menuBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          state.stickyNotes.forEach((n) => { if (n.id !== note.id) n.openMenu = false; });
          note.openMenu = !note.openMenu;
          render();
        });

        closeBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          state.stickyNotes = state.stickyNotes.filter((n) => n.id !== note.id);
          render();
        });

        if (note.openMenu) {
          const menuEl = el.querySelector('.sticky-note-menu');
          if (menuEl) {
            menuEl.addEventListener('click', (e) => {
              e.stopPropagation();
              const item = e.target.closest('.menu-item');
              if (!item) return;
              const action = item.dataset.action;
              if (action.startsWith('theme-')) {
                note.theme = action.replace('theme-', '');
                note.openMenu = false;
                render();
              } else if (action === 'duplicate') {
                const dup = {
                  ...note,
                  id: 'note-' + Date.now(),
                  x: note.x + 30,
                  y: note.y + 30,
                  openMenu: false
                };
                note.openMenu = false;
                state.stickyNotes.push(dup);
                render();
              } else if (action === 'delete') {
                state.stickyNotes = state.stickyNotes.filter((n) => n.id !== note.id);
                render();
              }
            });
          }
        }

        stickyNotesEl.appendChild(el);
      });
    }

    // ----- TEXT BLOCKS -----
    textBlocksEl.innerHTML = '';
    state.textBlocks.forEach((t) => {
      const el = document.createElement('div');
      el.className = 'text-block';
      el.style.left = t.x + 'px';
      el.style.top = t.y + 'px';
      el.innerHTML = `
        <div class="text-block-drag">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><circle cx="8" cy="6" r="1.6"></circle><circle cx="8" cy="12" r="1.6"></circle><circle cx="8" cy="18" r="1.6"></circle><circle cx="16" cy="6" r="1.6"></circle><circle cx="16" cy="12" r="1.6"></circle><circle cx="16" cy="18" r="1.6"></circle></svg>
        </div>
        <div class="text-block-content" contenteditable="true" style="font-size:${t.fontSize}px;">${t.text}</div>
        <div class="text-block-delete">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </div>
      `;
      el.querySelector('.text-block-drag').addEventListener('mousedown', (e) => startDrag(t.id, 'text', e));
      el.querySelector('.text-block-content').addEventListener('blur', (e) => {
        t.text = e.target.innerText;
      });
      el.querySelector('.text-block-delete').addEventListener('click', () => {
        state.textBlocks = state.textBlocks.filter((tb) => tb.id !== t.id);
        render();
      });
      textBlocksEl.appendChild(el);
    });
  }

  function startDrag(id, kind, e) {
    e.stopPropagation();
    e.preventDefault();
    let origin;
    if (kind === 'screen') origin = state.positions[id];
    else if (kind === 'stickyNote') origin = state.stickyNotes.find((n) => n.id === id);
    else origin = state.textBlocks.find((t) => t.id === id);

    if (!origin) return;
    drag = { id, kind, startX: e.clientX, startY: e.clientY, origX: origin.x, origY: origin.y, moved: false };
  }

  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.startX, dy = e.clientY - drag.startY;
    if (Math.abs(dx) > 4 || Math.abs(dy) > 4) drag.moved = true;
    if (!drag.moved) return;
    if (drag.kind === 'screen') {
      state.positions[drag.id] = { x: drag.origX + dx, y: drag.origY + dy };
    } else if (drag.kind === 'stickyNote') {
      const note = state.stickyNotes.find((n) => n.id === drag.id);
      if (note) { note.x = drag.origX + dx; note.y = drag.origY + dy; }
    } else {
      const t = state.textBlocks.find((tb) => tb.id === drag.id);
      if (t) { t.x = drag.origX + dx; t.y = drag.origY + dy; }
    }
    render();
  });

  window.addEventListener('mouseup', () => {
    if (!drag) return;
    if (!drag.moved && drag.kind === 'screen') {
      state.selectedId = state.selectedId === drag.id ? null : drag.id;
      render();
    }
    drag = null;
  });

  canvasEl.addEventListener('mousedown', (e) => {
    if (e.target !== canvasEl && e.target !== canvasAreaEl) return;
    state.stickyNotes.forEach((n) => { n.openMenu = false; });
    
    if (state.tool === 'note') {
      const rect = canvasEl.getBoundingClientRect();
      const x = (e.clientX - rect.left) / (zoom / 100);
      const y = (e.clientY - rect.top) / (zoom / 100);
      state.stickyNotes.push({
        id: 'note-' + Date.now(),
        x: Math.round(x),
        y: Math.round(y),
        theme: 'default',
        title: 'Note Title',
        openMenu: false,
        content: '<p>Type note details here...</p>'
      });
      state.tool = 'select';
      updateToolButtons();
      render();
    } else if (state.tool === 'text') {
      const rect = canvasEl.getBoundingClientRect();
      const x = (e.clientX - rect.left) / (zoom / 100);
      const y = (e.clientY - rect.top) / (zoom / 100);
      state.textBlocks.push({ id: 'text-' + Date.now(), x: Math.round(x), y: Math.round(y), text: 'Heading', fontSize: 28 });
      state.tool = 'select';
      updateToolButtons();
      render();
    } else if (state.selectedId) {
      state.selectedId = null;
      render();
    }
  });

  function updateToolButtons() {
    toolSelectBtn.classList.toggle('active', state.tool === 'select');
    if (toolNoteBtn) toolNoteBtn.classList.toggle('active', state.tool === 'note');
    if (toolTextBtn) toolTextBtn.classList.toggle('active', state.tool === 'text');
    canvasEl.classList.toggle('tool-text', state.tool === 'text');
    canvasEl.classList.toggle('tool-note', state.tool === 'note');
  }

  toolSelectBtn.addEventListener('click', () => { state.tool = 'select'; updateToolButtons(); });
  if (toolNoteBtn) toolNoteBtn.addEventListener('click', () => { state.tool = 'note'; updateToolButtons(); });
  if (toolTextBtn) toolTextBtn.addEventListener('click', () => { state.tool = 'text'; updateToolButtons(); });

  // ----- ZOOM (visual scale of the canvas) -----
  let zoom = 100;
  function applyZoom() {
    canvasEl.style.transform = `scale(${zoom / 100})`;
    canvasEl.style.transformOrigin = '0 0';
    zoomPctEl.textContent = zoom + '%';
  }
  zoomInBtn.addEventListener('click', () => { zoom = Math.min(200, zoom + 10); applyZoom(); });
  zoomOutBtn.addEventListener('click', () => { zoom = Math.max(30, zoom - 10); applyZoom(); });
  fitScreenBtn.addEventListener('click', () => { zoom = 100; applyZoom(); canvasAreaEl.scrollTo(0, 0); });

  // ----- MINIMAP -----
  function renderMinimap() {
    const mm = document.getElementById('minimap');
    mm.innerHTML = '<div class="mm-view"></div>';
    const scaleX = 100 / 2200, scaleY = 100 / 1350;
    screensCfg.forEach((cfg) => {
      const pos = state.positions[cfg.id];
      const dot = document.createElement('div');
      dot.className = 'mm-dot';
      dot.style.left = (pos.x * scaleX) + '%';
      dot.style.top = (pos.y * scaleY) + '%';
      dot.style.background = cfg.status === 'broken' ? '#ff5577' : cfg.status === 'untested' ? 'var(--ink-muted)' : '#22c55e';
      mm.appendChild(dot);
    });
  }

  // ----- RECORDING TIMER -----
  let elapsed = 0;
  setInterval(() => {
    elapsed++;
    const mm = String(Math.floor(elapsed / 60)).padStart(2, '0');
    const ss = String(elapsed % 60).padStart(2, '0');
    document.getElementById('recTimer').textContent = `${mm}:${ss}`;
  }, 1000);

  render();
  renderMinimap();
  updateToolButtons();
  applyZoom();
})();
