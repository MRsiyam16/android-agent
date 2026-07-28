// markdown.js — the two markdown renderers, extracted from the old dashboard.js IIFE.
//
// There were two, and they collided. Both were declared `function renderMarkdown` at the
// top level of the same closure, so the later one — the chat renderer below — silently
// replaced the richer note renderer for *every* caller, including the sticky notes that
// were written against it. That is why headings, blockquotes and code blocks have never
// worked inside a sticky note: `renderBlocks` has been unreachable.
//
// Splitting into modules forces the collision into the open, because a module may not
// declare the same name twice. They are named apart here, and notes.js deliberately still
// imports `renderChatMarkdown` — so this refactor changes nothing you can see. Switching
// notes to `renderNoteMarkdown` is the one-line fix, and it is a behaviour change, so it
// is left as a decision rather than smuggled in with a reorganisation.

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

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
function renderNoteMarkdown(src) {
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

function renderInline(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
}

function renderChatMarkdown(text) {
  const lines = escapeHtml(text).split('\n');
  let html = '';
  let inList = false;
  lines.forEach((raw) => {
    const line = raw.trimEnd();
    const bullet = /^\s*(?:[-*]|&#39;?•)\s+(.*)$/.exec(line);
    if (bullet) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += '<li>' + renderInline(bullet[1]) + '</li>';
      return;
    }
    if (inList) { html += '</ul>'; inList = false; }
    if (line.trim()) html += '<p>' + renderInline(line) + '</p>';
  });
  if (inList) html += '</ul>';
  return html || '<p></p>';
}


export { escapeHtml, mdInline, renderBlocks, renderChatMarkdown, renderInline,
         renderNoteMarkdown };
