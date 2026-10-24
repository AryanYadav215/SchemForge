/* Workspace shell, Design tab (tables, constraints, relationships, ER diagram) and Data tab. */
const P = window.PROJECT;
const S = {schema: null, tab: null, table: null, view: null, meta: null, lastSql: [], dt: {page: 1, size: 25, sort: null, dir: 'asc', q: ''}};
const TABS = {};   // id -> {label, owner, render(panel)}
const URL_ = (path) => '/api/p/' + P.id + path;

function tableSpec(name) { return S.schema.tables.find(t => t.name === name); }
function can(obj, priv) { return P.owner || ((S.schema.can[obj] || []).includes(priv)); }
function typeLabel(c) {
  if (c.type === 'VARCHAR') return 'VARCHAR(' + (c.length || 100) + ')';
  if (c.type === 'DECIMAL') return 'DECIMAL(' + (c.precision || 10) + ',' + (c.scale || 2) + ')';
  return c.type;
}

/* ---------------------------------------------------------------- shell */
async function loadSchema(keep) {
  S.schema = await api('GET', URL_('/schema'));
  if (!S.schema.tables.find(t => t.name === S.table)) S.table = S.schema.tables.length ? S.schema.tables[0].name : null;
  renderSide();
  if (!keep) renderTabs();
  renderPanel();
}

function renderSide() {
  const side = document.getElementById('side');
  side.replaceChildren(el('h3', {}, 'Tables'));
  if (!S.schema.tables.length) side.append(el('div', {class: 'muted small', style: 'padding:4px 8px'}, P.owner ? 'No tables yet. Open the Design tab.' : 'Nothing has been shared with you yet.'));
  S.schema.tables.forEach(t => side.append(el('button', {class: 'item' + (S.table === t.name && !S.view ? ' active' : ''),
    onclick: () => { S.table = t.name; S.view = null; S.dt.page = 1; S.dt.q = ''; S.dt.sort = null; renderSide(); renderPanel(); }},
    el('span', {}, t.name), el('span', {class: 'muted small'}, t.rows === null || t.rows === undefined ? '' : t.rows))));
  if (S.schema.views.length) {
    side.append(el('h3', {}, 'Views'));
    S.schema.views.forEach(v => side.append(el('button', {class: 'item' + (S.view === v.name ? ' active' : ''),
      onclick: () => { S.view = v.name; S.tab = 'data'; renderTabs(); renderSide(); renderPanel(); }}, el('span', {}, v.name), el('span', {class: 'badge gray'}, 'view'))));
  }
}

function renderTabs() {
  const nav = document.getElementById('tabs');
  const ids = Object.keys(TABS).filter(id => P.owner || !TABS[id].owner);
  if (!S.tab || !ids.includes(S.tab)) S.tab = P.owner ? 'design' : 'data';
  nav.replaceChildren(...ids.map(id => el('button', {role: 'tab', class: S.tab === id ? 'active' : '', 'aria-selected': S.tab === id,
    onclick: () => { S.tab = id; if (id !== 'data') S.view = null; renderTabs(); renderSide(); renderPanel(); }}, TABS[id].label)));
}

async function renderPanel() {
  const panel = document.getElementById('panel');
  panel.replaceChildren();
  try { await TABS[S.tab].render(panel); }
  catch (e) { panel.replaceChildren(el('div', {class: 'msg err'}, e.message)); }
}

async function refresh() { await loadSchema(true); }

/* ---------------------------------------------------------------- ER diagram */
function drawER(schema) {
  const NS = 'http://www.w3.org/2000/svg';
  const mk = (t, a, txt) => { const e = document.createElementNS(NS, t); for (const k in a || {}) e.setAttribute(k, a[k]); if (txt !== undefined) e.textContent = txt; return e; };
  const tables = schema.tables;
  if (!tables.length) return el('div', {class: 'empty'}, 'Your ER diagram appears here once you create tables.');
  const W = 210, ROW = 20, HEAD = 28, GX = 90, GY = 50, PAD = 24;
  const perRow = Math.max(1, Math.ceil(Math.sqrt(tables.length * 1.2)));
  const pos = {};
  let rowTop = PAD, i = 0;
  while (i < tables.length) {
    const slice = tables.slice(i, i + perRow);
    const h = Math.max(...slice.map(t => HEAD + t.columns.length * ROW + 6));
    slice.forEach((t, k) => { pos[t.name] = {x: PAD + k * (W + GX), y: rowTop, h: HEAD + t.columns.length * ROW + 6}; });
    rowTop += h + GY; i += perRow;
  }
  const width = PAD * 2 + perRow * (W + GX) - GX, height = rowTop - GY + PAD;
  const svg = mk('svg', {width, height, viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': 'Entity relationship diagram'});
  const edges = mk('g'); const boxes = mk('g'); let edgeNo = 0;
  svg.append(edges, boxes);
  tables.forEach(t => {
    const p = pos[t.name];
    boxes.append(mk('rect', {class: 'box', x: p.x, y: p.y, width: W, height: p.h, rx: 6}));
    boxes.append(mk('path', {class: 'head', d: `M${p.x},${p.y + HEAD} V${p.y + 6} Q${p.x},${p.y} ${p.x + 6},${p.y} H${p.x + W - 6} Q${p.x + W},${p.y} ${p.x + W},${p.y + 6} V${p.y + HEAD} Z`}));
    boxes.append(mk('text', {class: 'htxt', x: p.x + 10, y: p.y + 19}, t.name));
    t.columns.forEach((c, k) => {
      const y = p.y + HEAD + 15 + k * ROW;
      if (c.pk) boxes.append(mk('text', {class: 'pk', x: p.x + 8, y}, 'PK'));
      else if (c.fk) boxes.append(mk('text', {class: 'fk', x: p.x + 8, y}, 'FK'));
      boxes.append(mk('text', {class: 'ctxt', x: p.x + 30, y}, c.name));
      boxes.append(mk('text', {class: 'ctype', x: p.x + W - 8, y, 'text-anchor': 'end'}, typeLabel(c)));
    });
    t.columns.forEach((c, k) => {
      if (!c.fk) return;
      const a = p, b = pos[c.fk.table];
      if (!b) return;
      const ay = a.y + HEAD + 10 + k * ROW;
      const parentTable = tableSpec(c.fk.table);
      const pkIdx = parentTable ? parentTable.columns.findIndex(x => x.name === c.fk.column) : 0;
      const by = b.y + HEAD + 10 + Math.max(0, pkIdx) * ROW;
      const oneToOne = c.unique || (t.columns.filter(x => x.pk).length === 1 && c.pk);
      const off = ((edgeNo++) % 5 - 2) * 9;
      let d, lx1, lx2, ly1, ly2;
      if (c.fk.table === t.name) {
        const x = a.x + W; d = `M${x},${ay} h28 v${-(ay - by) || 22} h-28`; lx1 = x + 6; ly1 = ay - 4; lx2 = x + 6; ly2 = by - 4;
      } else if (a.x === b.x) {
        const x = a.x + W; d = `M${x},${ay} h30 V${by} h-30`; lx1 = x + 6; ly1 = ay - 4; lx2 = x + 6; ly2 = by - 4;
      } else if (a.x > b.x) {
        const x1 = a.x, x2 = b.x + W; const mid = (x1 + x2) / 2 + ((edgeNo++ % 5) - 2) * 7;
        d = `M${x1},${ay} H${mid} V${by} H${x2}`; lx1 = x1 - 14; ly1 = ay - 5; lx2 = x2 + 5; ly2 = by - 5;
      } else {
        const x1 = a.x + W, x2 = b.x; const mid = (x1 + x2) / 2 + ((edgeNo++ % 5) - 2) * 7;
        d = `M${x1},${ay} H${mid} V${by} H${x2}`; lx1 = x1 + 5; ly1 = ay - 5; lx2 = x2 - 14; ly2 = by - 5;
      }
      edges.append(mk('path', {class: 'rel', d}));
      edges.append(mk('text', {class: 'card-lbl', x: lx1, y: ly1}, oneToOne ? '1' : 'N'));
      edges.append(mk('text', {class: 'card-lbl', x: lx2, y: ly2}, '1'));
    });
  });
  return el('div', {class: 'er'}, svg);
}

/* ---------------------------------------------------------------- column editor */
function colEditor(col, opts) {
  opts = opts || {};
  col = col || {name: '', type: 'VARCHAR', length: 100};
  const meta = S.meta;
  const f = {};
  f.name = el('input', {value: col.name, placeholder: 'column_name', 'aria-label': 'Column name', maxlength: 40});
  f.type = el('select', {'aria-label': 'Data type'}, meta.types.map(t => el('option', {value: t, selected: t === col.type}, t)));
  f.len = el('input', {value: col.type === 'DECIMAL' ? (col.precision || 10) + ',' + (col.scale || 2) : (col.length || ''), size: 6, 'aria-label': 'Length'});
  const chk = (key, label) => { const i = el('input', {type: 'checkbox', checked: !!col[key]}); f[key] = i; return el('label', {class: 'opt'}, i, label); };
  f.def = el('input', {value: col.default || '', placeholder: 'default', size: 10, 'aria-label': 'Default value'});
  f.ckop = el('select', {'aria-label': 'Check operator'}, el('option', {value: ''}, 'no check'), meta.check_ops.map(o => el('option', {value: o, selected: col.check && col.check.op === o}, o)));
  f.ckval = el('input', {value: col.check ? col.check.value : '', placeholder: 'value (a,b for BETWEEN/IN)', size: 16, 'aria-label': 'Check value'});
  f.fkt = el('select', {'aria-label': 'References table'}, el('option', {value: ''}, 'no foreign key'),
    S.schema.tables.filter(t => !opts.hideTable || t.name !== opts.hideTable || true).map(t => el('option', {value: t.name, selected: col.fk && col.fk.table === t.name}, t.name)));
  f.fkc = el('select', {'aria-label': 'References column'});
  f.fka = el('select', {'aria-label': 'On delete'}, meta.fk_actions.map(a => el('option', {value: a, selected: col.fk && col.fk.on_delete === a}, 'ON DELETE ' + a)));
  const fillFk = () => {
    const t = tableSpec(f.fkt.value);
    f.fkc.replaceChildren(...(t ? t.columns.filter(c => c.pk || c.unique).map(c => el('option', {value: c.name, selected: col.fk && col.fk.column === c.name}, c.name)) : []));
    f.fkc.disabled = f.fka.disabled = !t;
  };
  f.fkt.onchange = fillFk; fillFk();
  const sync = () => {
    f.len.classList.toggle('hidden', !['VARCHAR', 'DECIMAL'].includes(f.type.value));
    f.len.placeholder = f.type.value === 'DECIMAL' ? 'p,s' : 'len';
    if (f.type.value !== 'INT') { f.auto.checked = false; }
    f.auto.parentElement.classList.toggle('hidden', f.type.value !== 'INT');
  };
  const node = el('div', {class: 'card', style: 'padding:10px 12px;margin-bottom:10px'},
    el('div', {class: 'row'}, f.name, f.type, f.len,
      chk('pk', 'PRIMARY KEY'), chk('auto', 'AUTO INCREMENT'), chk('notnull', 'NOT NULL'), chk('unique', 'UNIQUE')),
    el('div', {class: 'row', style: 'margin-top:8px'}, f.def, f.ckop, f.ckval, f.fkt, f.fkc, f.fka),
    opts.onRemove ? el('div', {style: 'margin-top:6px'}, el('button', {class: 'ghost danger small', onclick: opts.onRemove}, 'Remove column')) : null);
  f.type.onchange = sync; sync();
  const get = () => {
    const c = {name: f.name.value.trim(), type: f.type.value, pk: f.pk.checked, auto: f.auto.checked, notnull: f.notnull.checked,
      unique: f.unique.checked, default: f.def.value.trim(), check: null, fk: null};
    if (c.type === 'VARCHAR') c.length = parseInt(f.len.value || '100', 10);
    if (c.type === 'DECIMAL') { const [p, s] = (f.len.value || '10,2').split(','); c.precision = parseInt(p, 10); c.scale = parseInt(s || '0', 10); }
    if (f.ckop.value) c.check = {op: f.ckop.value, value: f.ckval.value};
    if (f.fkt.value) c.fk = {table: f.fkt.value, column: f.fkc.value, on_delete: f.fka.value};
    return c;
  };
  return {node, get};
}

function showDdl(res) {
  S.lastSql = res.sql || [];
  (res.warnings || []).forEach(w => toast(w, 'err'));
}

/* ---------------------------------------------------------------- Design tab */
function newTableDialog() {
  const name = el('input', {placeholder: 'table_name', maxlength: 40, 'aria-label': 'Table name'});
  const list = el('div');
  const audit = el('input', {type: 'checkbox'});
  const editors = [];
  const add = (c) => {
    const ed = colEditor(c, {onRemove: () => { editors.splice(editors.indexOf(ed), 1); ed.node.remove(); }});
    editors.push(ed); list.append(ed.node);
  };
  add({name: 'id', type: 'INT', pk: true, auto: true});
  add({name: '', type: 'VARCHAR', length: 100});
  const body = el('div', {},
    el('label', {class: 'field'}, el('span', {}, 'Table name'), name),
    el('p', {class: 'muted small'}, 'Tip: give every table a PRIMARY KEY. Tick several PRIMARY KEY boxes for a composite key. Use the foreign-key dropdowns to link to another table.'),
    list, el('button', {onclick: () => add()}, '+ Add column'),
    el('label', {class: 'opt', style: 'margin-top:12px'}, audit, 'Keep an audit trail (trigger logs every insert, update, delete)'));
  openDialog('New table', body, {okText: 'Create table', onOk: async () => {
    const res = await api('POST', URL_('/tables'), {name: name.value.trim(), columns: editors.map(e => e.get()), audit: audit.checked});
    showDdl(res); S.table = name.value.trim(); await refresh(); toast('Table created');
  }});
}

function relationshipDialog() {
  const tables = S.schema.tables.map(t => t.name);
  if (tables.length < 1) { toast('Create a table first', 'err'); return; }
  const kind = el('select', {}, el('option', {value: '1:N'}, 'One-to-many (1:N)'), el('option', {value: '1:1'}, 'One-to-one (1:1)'), el('option', {value: 'M:N'}, 'Many-to-many (M:N)'));
  const parent = el('select', {}, tables.map(t => el('option', {value: t}, t)));
  const child = el('select', {}, tables.map((t, i) => el('option', {value: t, selected: i === Math.min(1, tables.length - 1)}, t)));
  const action = el('select', {}, S.meta.fk_actions.map(a => el('option', {value: a}, a)));
  const junction = el('input', {placeholder: 'optional, e.g. enrollment'});
  const hint = el('p', {class: 'muted small'});
  const upd = () => {
    const k = kind.value;
    hint.textContent = k === '1:N' ? `Each ${child.value} row belongs to one ${parent.value}; a foreign key column is added to ${child.value}.`
      : k === '1:1' ? `Each ${parent.value} has at most one ${child.value}: foreign key plus UNIQUE on ${child.value}.`
      : `A junction table links ${parent.value} and ${child.value}; its primary key is the pair of foreign keys.`;
    junction.parentElement.classList.toggle('hidden', k !== 'M:N');
    action.parentElement.classList.toggle('hidden', k === 'M:N');
  };
  const body = el('div', {},
    el('label', {class: 'field'}, el('span', {}, 'Type'), kind),
    el('label', {class: 'field'}, el('span', {}, 'Parent table (the "one" side)'), parent),
    el('label', {class: 'field'}, el('span', {}, 'Child table (the "many" side)'), child),
    el('label', {class: 'field'}, el('span', {}, 'When the parent row is deleted'), action),
    el('label', {class: 'field'}, el('span', {}, 'Junction table name'), junction), hint);
  [kind, parent, child].forEach(s => s.onchange = upd); upd();
  openDialog('Add relationship', body, {okText: 'Create relationship', onOk: async () => {
    const res = await api('POST', URL_('/relationships'), {kind: kind.value, parent: parent.value, child: child.value, on_delete: action.value, junction: junction.value.trim()});
    showDdl(res); await refresh(); toast('Relationship created');
  }});
}

function columnDialog(table, col) {
  const ed = colEditor(col);
  openDialog(col ? `Edit column ${col.name}` : `Add column to ${table}`, el('div', {}, ed.node,
    col ? el('p', {class: 'muted small'}, 'Changing a column rebuilds the table safely and copies your rows across.') : null), {
    okText: col ? 'Save column' : 'Add column', onOk: async () => {
      const res = col ? await api('PUT', URL_('/tables/' + table + '/columns/' + col.name), ed.get()) : await api('POST', URL_('/tables/' + table + '/columns'), ed.get());
      showDdl(res); await refresh(); toast('Saved');
    }});
}

TABS.design = {label: 'Design', owner: true, render(panel) {
  panel.append(el('div', {class: 'row between', style: 'margin-bottom:12px'},
    el('div', {}, el('h2', {style: 'margin:0'}, 'Design'), el('div', {class: 'muted small'}, 'Tables, constraints and relationships. Every change is real DDL, shown below.')),
    el('div', {class: 'row'}, el('button', {onclick: relationshipDialog}, 'Add relationship'), el('button', {class: 'primary', onclick: newTableDialog}, 'New table'))));
  panel.append(drawER(S.schema));
  const t = S.table && tableSpec(S.table);
  if (t) panel.append(tableCard(t));
  if (S.lastSql.length) panel.append(el('div', {class: 'card'}, el('div', {class: 'row between'}, el('h3', {}, 'Last SQL executed'),
    el('button', {class: 'small', onclick: () => copyText(S.lastSql.join('\n'))}, 'Copy')), sqlBox(S.lastSql.join('\n\n'))));
}};

function tableCard(t) {
  const rows = t.columns.map(c => {
    const cons = [];
    if (c.pk) cons.push(el('span', {class: 'badge warn'}, 'PK'));
    if (c.auto) cons.push(el('span', {class: 'badge gray'}, 'AUTO'));
    if (c.notnull && !c.pk) cons.push(el('span', {class: 'badge gray'}, 'NOT NULL'));
    if (c.unique) cons.push(el('span', {class: 'badge gray'}, 'UNIQUE'));
    if (c.check) cons.push(el('span', {class: 'badge gray'}, 'CHECK ' + c.check.op + ' ' + c.check.value));
    if (c.default !== '' && c.default !== null) cons.push(el('span', {class: 'badge gray'}, 'DEFAULT ' + c.default));
    if (c.fk) cons.push(el('span', {class: 'badge'}, '→ ' + c.fk.table + '.' + c.fk.column + ' (' + (c.fk.on_delete || 'NO ACTION') + ')'));
    return el('tr', {}, el('td', {class: 'mono'}, c.name), el('td', {class: 'mono'}, typeLabel(c)), el('td', {}, el('div', {class: 'pill-row'}, cons)),
      el('td', {class: 'actions'}, el('button', {class: 'small', onclick: () => columnDialog(t.name, c)}, 'Edit'), ' ',
        el('button', {class: 'small danger', onclick: async () => {
          if (!confirm(`Drop column ${c.name}? Its data will be lost.`)) return;
          try { showDdl(await api('DELETE', URL_('/tables/' + t.name + '/columns/' + c.name))); await refresh(); } catch (e) { toast(e.message, 'err'); }
        }}, 'Drop')));
  });
  const idx = (t.indexes || []).map(i => el('span', {class: 'badge gray'}, (i.unique ? 'UNIQUE ' : '') + i.name + ' (' + i.columns.join(', ') + ') ',
    el('a', {href: '#', title: 'Drop index', onclick: async (e) => { e.preventDefault(); try { showDdl(await api('DELETE', URL_('/tables/' + t.name + '/indexes/' + i.name))); await refresh(); } catch (ex) { toast(ex.message, 'err'); } }}, '×')));
  return el('div', {class: 'card', style: 'margin-top:16px'},
    el('div', {class: 'row between'}, el('h2', {style: 'margin:0'}, t.name, ' ', el('span', {class: 'muted small'}, t.rows + ' rows')),
      el('div', {class: 'row'},
        el('button', {onclick: () => columnDialog(t.name)}, 'Add column'),
        el('button', {onclick: () => indexDialog(t)}, 'Add index'),
        el('button', {onclick: async () => { try { await api('POST', URL_('/tables/' + t.name + '/audit'), {on: !t.audit}); await refresh(); toast(t.audit ? 'Audit trail off' : 'Audit trail on'); } catch (e) { toast(e.message, 'err'); } }},
          t.audit ? 'Audit trail: on' : 'Audit trail: off'),
        el('button', {onclick: () => renameDialog(t)}, 'Rename'),
        el('button', {class: 'danger', onclick: async () => {
          if (!confirm(`Drop table ${t.name} and all ${t.rows} rows?`)) return;
          try { showDdl(await api('DELETE', URL_('/tables/' + t.name))); await refresh(); toast('Table dropped'); } catch (e) { toast(e.message, 'err'); }
        }}, 'Drop table'))),
    el('div', {class: 'tbl-wrap', style: 'margin-top:12px'}, el('table', {class: 't'},
      el('thead', {}, el('tr', {}, el('th', {}, 'Column'), el('th', {}, 'Type'), el('th', {}, 'Constraints'), el('th', {}, ''))), el('tbody', {}, rows))),
    idx.length ? el('div', {class: 'pill-row', style: 'margin-top:10px'}, el('span', {class: 'muted small'}, 'Indexes:'), idx) : null);
}

function renameDialog(t) {
  const name = el('input', {value: t.name});
  openDialog('Rename table', el('label', {class: 'field'}, el('span', {}, 'New name'), name), {okText: 'Rename', onOk: async () => {
    showDdl(await api('POST', URL_('/tables/' + t.name + '/rename'), {name: name.value.trim()})); S.table = name.value.trim(); await refresh();
  }});
}

function indexDialog(t) {
  const name = el('input', {value: 'idx_' + t.name + '_', maxlength: 40});
  const uq = el('input', {type: 'checkbox'});
  const boxes = t.columns.map(c => ({c, i: el('input', {type: 'checkbox'})}));
  openDialog('Add index on ' + t.name, el('div', {},
    el('label', {class: 'field'}, el('span', {}, 'Index name'), name),
    el('div', {class: 'pill-row'}, boxes.map(b => el('label', {class: 'opt'}, b.i, b.c.name))),
    el('label', {class: 'opt', style: 'margin-top:10px'}, uq, 'UNIQUE'),
    el('p', {class: 'muted small'}, 'Indexes make lookups and joins on these columns faster.')), {okText: 'Create index', onOk: async () => {
    showDdl(await api('POST', URL_('/tables/' + t.name + '/indexes'), {name: name.value.trim(), columns: boxes.filter(b => b.i.checked).map(b => b.c.name), unique: uq.checked})); await refresh();
  }});
}

/* ---------------------------------------------------------------- Data tab */
function inputFor(col, value, fkOptions) {
  const v = value === null || value === undefined ? '' : value;
  if (col.fk) {
    const s = el('select', {'aria-label': col.name}, el('option', {value: ''}, '(none)'),
      (fkOptions || []).map(o => el('option', {value: o.id, selected: String(o.id) === String(v)}, o.label ? o.id + ' – ' + o.label : o.id)));
    return s;
  }
  switch (col.type) {
    case 'INT': return el('input', {type: 'number', step: 1, value: v, placeholder: col.auto ? 'auto' : ''});
    case 'FLOAT': case 'DECIMAL': return el('input', {type: 'number', step: 'any', value: v});
    case 'DATE': return el('input', {type: 'date', value: v});
    case 'DATETIME': return el('input', {type: 'datetime-local', value: v ? String(v).replace(' ', 'T').slice(0, 16) : ''});
    case 'BOOLEAN': return el('select', {}, el('option', {value: ''}, '(none)'), el('option', {value: '1', selected: String(v) === '1'}, 'Yes'), el('option', {value: '0', selected: String(v) === '0'}, 'No'));
    case 'TEXT': return el('textarea', {style: 'min-height:70px', value: v});
    default: return el('input', {type: 'text', value: v, maxlength: col.length || 100});
  }
}

async function rowDialog(t, row) {
  const opts = {};
  for (const c of t.columns) if (c.fk) { try { opts[c.name] = (await api('GET', URL_('/fk/' + t.name + '/' + c.name))).options; } catch (e) { opts[c.name] = []; } }
  const fields = t.columns.map(c => ({c, i: inputFor(c, row ? row[c.name] : (c.default !== '' ? c.default : ''), opts[c.name])}));
  const body = el('div', {}, fields.map(f => el('label', {class: 'field'},
    el('span', {}, f.c.name, ' ', el('span', {class: 'muted'}, typeLabel(f.c) + (f.c.notnull && !f.c.auto ? ' · required' : '') + (f.c.pk ? ' · key' : ''))), f.i)));
  openDialog(row ? 'Edit row' : 'Add row to ' + t.name, body, {okText: row ? 'Save changes' : 'Add row', onOk: async () => {
    const vals = {}; fields.forEach(f => { vals[f.c.name] = f.i.value; });
    if (row) await api('PUT', URL_('/rows/' + t.name + '/' + row._rid_), vals); else await api('POST', URL_('/rows/' + t.name), vals);
    await refresh(); toast(row ? 'Row updated' : 'Row added');
  }});
}

function importDialog(t) {
  const file = el('input', {type: 'file', accept: '.csv,text/csv'});
  openDialog('Import CSV into ' + t.name, el('div', {}, el('p', {class: 'muted small'}, 'The first line must contain column names that exist in this table. The import is all-or-nothing: if any row fails, nothing is added.'), file), {
    okText: 'Import', onOk: async () => {
      if (!file.files[0]) throw new Error('Choose a CSV file.');
      const fd = new FormData(); fd.append('file', file.files[0]);
      const r = await api('POST', URL_('/import/' + t.name), fd); await refresh(); toast(r.inserted + ' rows imported');
    }});
}

TABS.data = {label: 'Data', owner: false, async render(panel) {
  if (S.view) {
    const r = await api('GET', URL_('/view-rows/' + S.view));
    panel.append(el('div', {class: 'row between', style: 'margin-bottom:10px'}, el('h2', {style: 'margin:0'}, S.view, ' ', el('span', {class: 'badge gray'}, 'view (read-only)'))),
      renderTable(r.columns, r.rows), r.truncated ? el('div', {class: 'muted small'}, 'First 200 rows shown.') : null);
    return;
  }
  const t = S.table && tableSpec(S.table);
  if (!t) { panel.append(el('div', {class: 'card empty'}, P.owner ? 'Create a table in the Design tab, then add data here.' : 'No tables have been shared with you yet.')); return; }
  const d = S.dt;
  let r;
  try { r = await api('GET', URL_('/rows/' + t.name + '?' + new URLSearchParams({page: d.page, size: d.size, sort: d.sort || '', dir: d.dir, q: d.q}))); }
  catch (e) { panel.append(el('div', {class: 'msg err'}, e.message)); return; }
  const pages = Math.max(1, Math.ceil(r.total / r.size));
  const search = el('input', {type: 'search', placeholder: 'Search all columns', value: d.q, 'aria-label': 'Search'});
  search.onkeydown = (e) => { if (e.key === 'Enter') { d.q = search.value; d.page = 1; renderPanel(); } };
  panel.append(el('div', {class: 'row between', style: 'margin-bottom:10px'},
    el('h2', {style: 'margin:0'}, t.name, ' ', el('span', {class: 'muted small'}, r.total + ' rows')),
    el('div', {class: 'row'}, search,
      can(t.name, 'INSERT') ? el('button', {onclick: () => importDialog(t)}, 'Import CSV') : null,
      can(t.name, 'INSERT') ? el('button', {class: 'primary', onclick: () => rowDialog(t)}, 'Add row') : null)));
  panel.append(renderTable(r.columns, r.rows, {sort: d.sort, dir: d.dir, empty: 'No rows yet.',
    onSort: (c) => { if (d.sort === c) d.dir = d.dir === 'asc' ? 'desc' : 'asc'; else { d.sort = c; d.dir = 'asc'; } renderPanel(); },
    actions: (row) => [
      can(t.name, 'UPDATE') ? el('button', {class: 'small', onclick: () => rowDialog(t, row)}, 'Edit') : null, ' ',
      can(t.name, 'DELETE') ? el('button', {class: 'small danger', onclick: async () => {
        if (!confirm('Delete this row?')) return;
        try { await api('DELETE', URL_('/rows/' + t.name + '/' + row._rid_)); await refresh(); toast('Row deleted'); } catch (e) { toast(e.message, 'err'); }
      }}, 'Delete') : null]}));
  panel.append(el('div', {class: 'row between', style: 'margin-top:10px'},
    el('span', {class: 'muted small'}, 'Page ' + r.page + ' of ' + pages),
    el('div', {class: 'row'}, el('button', {disabled: r.page <= 1, onclick: () => { d.page--; renderPanel(); }}, 'Previous'),
      el('button', {disabled: r.page >= pages, onclick: () => { d.page++; renderPanel(); }}, 'Next'))));
}};
/* ---------------------------------------------------------------- boot */
async function init() {
  try { S.meta = await api('GET', '/api/meta'); await loadSchema(); }
  catch (e) { document.getElementById('panel').replaceChildren(el('div', {class: 'msg err'}, e.message)); }
}
window.addEventListener('load', init);
