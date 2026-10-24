/* Small DOM + fetch helpers shared by every page. No framework, no innerHTML with user data. */
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k === 'class') e.className = v;
    else if (k.startsWith('on') && typeof v === 'function') e[k] = v;
    else if (k === 'value') e.value = v;
    else if (k === 'checked' || k === 'disabled' || k === 'selected') e[k] = !!v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  kids.flat(Infinity).forEach(k => {
    if (k === null || k === undefined || k === false) return;
    e.append(k.nodeType ? k : document.createTextNode(String(k)));
  });
  return e;
}

function csrf() {
  const m = document.querySelector('meta[name=csrf]');
  return m ? m.content : '';
}

async function api(method, url, body) {
  const opt = {method, headers: {'X-CSRF': csrf()}, credentials: 'same-origin'};
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { /* not json */ }
  if (r.status === 401) { location.href = '/login'; throw new Error('Please sign in.'); }
  if (!r.ok) throw new Error((data && data.error) || ('Request failed (' + r.status + ')'));
  return data;
}

function toast(msg, kind) {
  const box = document.getElementById('toasts');
  const t = el('div', {class: 'toast' + (kind === 'err' ? ' err' : '')}, msg);
  box.append(t);
  setTimeout(() => t.remove(), kind === 'err' ? 7000 : 3500);
}

function fmtCell(v) {
  if (v === null || v === undefined) return el('td', {class: 'null'}, 'NULL');
  const isNum = typeof v === 'number';
  return el('td', {class: isNum ? 'num' : '', title: String(v)}, String(v));
}

/* columns: [name], rows: [[...]] (arrays) or [{...}] with keys=columns */
function renderTable(columns, rows, opts) {
  opts = opts || {};
  const head = el('tr', {}, columns.map(c => {
    const th = el('th', {class: opts.onSort ? 'sortable' : ''}, c,
      opts.sort === c ? (opts.dir === 'desc' ? ' ▾' : ' ▴') : '');
    if (opts.onSort) { th.tabIndex = 0; th.onclick = () => opts.onSort(c); th.onkeydown = (e) => { if (e.key === 'Enter') opts.onSort(c); }; }
    return th;
  }), opts.actions ? el('th', {}, '') : null);
  const body = rows.map(r => {
    const vals = Array.isArray(r) ? r : columns.map(c => r[c]);
    return el('tr', {}, vals.map(fmtCell), opts.actions ? el('td', {class: 'actions'}, opts.actions(r)) : null);
  });
  if (!rows.length) body.push(el('tr', {}, el('td', {colspan: columns.length + (opts.actions ? 1 : 0), class: 'empty'}, opts.empty || 'No rows')));
  return el('div', {class: 'tbl-wrap'}, el('table', {class: 't'}, el('thead', {}, head), el('tbody', {}, body)));
}

function sqlBox(text) {
  const kw = /\b(SELECT|DISTINCT|FROM|WHERE|GROUP BY|HAVING|ORDER BY|LIMIT|INNER|LEFT|RIGHT|FULL|CROSS|JOIN|ON|AS|AND|OR|NOT|IN|EXISTS|BETWEEN|LIKE|IS|NULL|CREATE|TABLE|VIEW|INDEX|UNIQUE|TRIGGER|PRIMARY KEY|FOREIGN KEY|REFERENCES|DEFAULT|CHECK|INSERT|INTO|VALUES|UPDATE|SET|DELETE|DROP|ALTER|ADD|COLUMN|RENAME|TO|COUNT|SUM|AVG|MIN|MAX|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|GRANT|REVOKE|ON DELETE|CASCADE|CONSTRAINT|AUTOINCREMENT|INTEGER|VARCHAR|DECIMAL|DATE|BOOLEAN|PROCEDURE|FUNCTION|CURSOR|LOOP|END|DECLARE|RETURN|RETURNS|AFTER|BEFORE|FOR EACH ROW|EXECUTE|OPEN|CLOSE|FETCH|IF|THEN|ELSE|ELSIF|WITH|UNION|INTERSECT|EXCEPT|ALL)\b/g;
  const pre = el('pre', {class: 'sqlbox'});
  let last = 0, m;
  while ((m = kw.exec(text))) {
    if (m.index > last) pre.append(text.slice(last, m.index));
    pre.append(el('span', {class: 'kw'}, m[0]));
    last = m.index + m[0].length;
  }
  pre.append(text.slice(last));
  return pre;
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('Copied'); }
  catch (e) {
    const ta = el('textarea', {style: 'position:fixed;left:-999px'}); ta.value = text; document.body.append(ta); ta.select();
    try { document.execCommand('copy'); toast('Copied'); } catch (e2) { toast('Copy failed', 'err'); }
    ta.remove();
  }
}

/* modal: build(bodyNode) populates; onOk() may throw to show error. */
function openDialog(title, body, o) {
  o = o || {};
  const dlg = document.getElementById('dlg');
  const inner = document.getElementById('dlg-inner');
  const err = el('div', {class: 'msg err hidden', role: 'alert'});
  const close = () => dlg.close();
  const ok = el('button', {class: 'primary', onclick: async () => {
    err.classList.add('hidden');
    ok.disabled = true;
    try { const keep = await o.onOk(); if (keep !== false) close(); }
    catch (e) { err.textContent = e.message; err.classList.remove('hidden'); }
    finally { ok.disabled = false; }
  }}, o.okText || 'Save');
  inner.replaceChildren(
    el('div', {class: 'dhead'}, el('h2', {style: 'margin:0'}, title), el('button', {class: 'ghost', 'aria-label': 'Close', onclick: close}, '×')),
    el('div', {class: 'dbody'}, body, err),
    el('div', {class: 'dfoot'}, el('button', {onclick: close}, 'Cancel'), o.onOk ? ok : null));
  dlg.showModal();
  return {close, err};
}
