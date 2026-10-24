/* Query builder, SQL console + transactions, routines, views, normalizer, access control, history, export. */

/* ---------------------------------------------------------------- Query builder */
const Q = {table: '', alias: '', distinct: false, joins: [], columns: [], where: [], group_by: [], having: [], order_by: [], limit: '', result: null};
const OPS = ['=', '!=', '<', '>', '<=', '>=', 'LIKE', 'NOT LIKE', 'IN', 'NOT IN', 'BETWEEN', 'IS NULL', 'IS NOT NULL', 'IN_SUB', 'NOT_IN_SUB', 'CMP_SUB', 'EXISTS_SUB', 'NOT_EXISTS_SUB'];
const OP_LABEL = {IN_SUB: 'IN (subquery)', NOT_IN_SUB: 'NOT IN (subquery)', CMP_SUB: 'compare with (subquery)', EXISTS_SUB: 'EXISTS (subquery)', NOT_EXISTS_SUB: 'NOT EXISTS (subquery)'};
const AGGS = [['', '(none)'], ['COUNT', 'COUNT'], ['COUNT_DISTINCT', 'COUNT DISTINCT'], ['SUM', 'SUM'], ['AVG', 'AVG'], ['MIN', 'MIN'], ['MAX', 'MAX']];

function qAliases() {
  const out = [];
  if (Q.table) out.push({alias: Q.alias || Q.table, table: Q.table});
  Q.joins.forEach(j => { if (j.table) out.push({alias: j.alias || j.table, table: j.table}); });
  return out;
}
function suggestJoin(j, idx) {
  /* pick the ON columns from a foreign key between the new table and an earlier one */
  const aliases = qAliases(); const mine = aliases[idx + 1]; if (!mine) return;
  const tb = tableSpec(j.table); if (!tb) return;
  for (let k = 0; k <= idx; k++) {
    const ta = tableSpec(aliases[k].table); if (!ta) continue;
    const a = ta.columns.find(c => c.fk && c.fk.table === j.table);
    if (a) { j.left = aliases[k].alias + '.' + a.name; j.right = mine.alias + '.' + a.fk.column; return; }
    const b = tb.columns.find(c => c.fk && c.fk.table === aliases[k].table);
    if (b) { j.left = mine.alias + '.' + b.name; j.right = aliases[k].alias + '.' + b.fk.column; return; }
  }
}
function qRefs(upTo) {
  const refs = [];
  qAliases().slice(0, upTo === undefined ? undefined : upTo).forEach(a => { const t = tableSpec(a.table); if (t) t.columns.forEach(c => refs.push(a.alias + '.' + c.name)); });
  return refs;
}
/* Suggest ON columns from declared foreign keys between the new table and the tables before it. */
function suggestJoin(j, idx) {
  const prev = qAliases().slice(0, idx + 1);
  const nb = tableSpec(j.table); const nalias = j.alias || j.table;
  if (!nb) return null;
  for (const a of prev.slice().reverse()) {
    const ta = tableSpec(a.table); if (!ta) continue;
    for (const c of ta.columns) if (c.fk && c.fk.table === j.table) return [a.alias + '.' + c.name, nalias + '.' + c.fk.column];
    for (const c of nb.columns) if (c.fk && c.fk.table === a.table) return [nalias + '.' + c.name, a.alias + '.' + c.fk.column];
  }
  return null;
}

function sel(options, value, onchange, label) {
  const s = el('select', {'aria-label': label || ''}, options.map(o => { const [v, l] = Array.isArray(o) ? o : [o, o]; return el('option', {value: v, selected: v === value}, l); }));
  s.onchange = () => onchange(s.value); return s;
}
function txt(value, onchange, ph, size) {
  const i = el('input', {value: value || '', placeholder: ph || '', size: size || 12, 'aria-label': ph || ''}); i.oninput = () => onchange(i.value); return i;
}
function section(title, hint, add, kids) {
  return el('div', {class: 'card', style: 'padding:12px 14px'}, el('div', {class: 'row between'}, el('div', {}, el('strong', {}, title), hint ? el('span', {class: 'muted small'}, '  ' + hint) : null), add), kids);
}

function condRow(c, i, list, depthLabel, tableName) {
  const refs = tableName ? (tableSpec(tableName) ? tableSpec(tableName).columns.map(x => x.name) : []) : qRefs();
  if (!c.col || !refs.includes(c.col)) c.col = refs[0] || '';
  const row = el('div', {class: 'brow'},
    i > 0 ? sel(['AND', 'OR'], c.conj || 'AND', v => c.conj = v, 'AND/OR') : el('span', {class: 'muted small', style: 'width:42px'}, depthLabel || 'WHERE'),
    sel(refs, c.col, v => c.col = v, 'Column'),
    sel((tableName ? OPS.filter(o => !o.includes('SUB')) : OPS).map(o => [o, OP_LABEL[o] || o]), c.op || '=', v => { c.op = v; drawQuery(); }, 'Operator'));
  const op = c.op || '=';
  if (op.endsWith('SUB')) {
    c.sub = c.sub || {table: S.schema.tables[0] ? S.schema.tables[0].name : '', column: '', agg: '', where: []};
    if (op === 'CMP_SUB') row.append(sel(['=', '!=', '<', '>', '<=', '>='], c.cmp || '=', v => c.cmp = v, 'Comparison'));
    row.append(subForm(c.sub, op));
  } else if (!['IS NULL', 'IS NOT NULL'].includes(op)) {
    row.append(txt(c.value, v => c.value = v, op === 'IN' || op === 'NOT IN' ? 'a, b, c' : 'value'));
    if (op === 'BETWEEN') row.append('and', txt(c.value2, v => c.value2 = v, 'value'));
  }
  row.append(el('button', {class: 'ghost danger icon', 'aria-label': 'Remove condition', onclick: () => { list.splice(i, 1); drawQuery(); }}, '×'));
  return row;
}

function subForm(sub, op) {
  const tnames = S.schema.tables.map(t => t.name);
  const tcols = () => (tableSpec(sub.table) ? tableSpec(sub.table).columns.map(c => c.name) : []);
  if (!tcols().includes(sub.column)) sub.column = tcols()[0] || '';
  const needCol = !op.includes('EXISTS');
  const box = el('div', {class: 'sub'}, el('span', {class: 'small muted'}, 'SELECT '),
    needCol ? sel(op === 'CMP_SUB' ? AGGS : [['', '(column)']], sub.agg || '', v => sub.agg = v, 'Aggregate') : null,
    needCol ? sel(tcols(), sub.column, v => sub.column = v, 'Sub column') : el('span', {class: 'mono small'}, '1'),
    el('span', {class: 'small muted'}, ' FROM '), sel(tnames, sub.table, v => { sub.table = v; sub.where = []; sub.correlate = null; drawQuery(); }, 'Sub table'));
  const w = (sub.where && sub.where[0]) || null;
  const wrow = el('div', {class: 'row', style: 'margin-top:6px'}, el('span', {class: 'small muted'}, 'WHERE (optional)'));
  if (w) {
    wrow.append(sel(tcols(), w.col || tcols()[0], v => w.col = v, 'Column'), sel(['=', '!=', '<', '>', '<=', '>=', 'LIKE'], w.op || '=', v => w.op = v, 'Op'), txt(w.value, v => w.value = v, 'value'),
      el('button', {class: 'ghost danger icon', onclick: () => { sub.where = []; drawQuery(); }}, '×'));
  } else wrow.append(el('button', {class: 'small', onclick: () => { sub.where = [{col: tcols()[0], op: '=', value: ''}]; drawQuery(); }}, '+ condition'));
  const corr = sub.correlate;
  const crow = el('div', {class: 'row', style: 'margin-top:6px'}, el('label', {class: 'opt'}, el('input', {type: 'checkbox', checked: !!corr, onchange: (e) => { sub.correlate = e.target.checked ? {inner: tcols()[0], outer: qRefs()[0]} : null; drawQuery(); }}), 'Correlated: link to the outer row'));
  if (corr) crow.append(sel(tcols(), corr.inner, v => corr.inner = v, 'Inner'), '=', sel(qRefs(), corr.outer, v => corr.outer = v, 'Outer'));
  box.append(wrow, crow);
  return box;
}

function queryPayload() {
  const copy = JSON.parse(JSON.stringify(Q)); delete copy.result;
  copy.where = copy.where.map(c => { if (c.sub && c.sub.where) c.sub.where = c.sub.where.filter(w => w.col); return c; });
  copy.columns = copy.columns.filter(c => c.col);
  return copy;
}

async function runQuery() {
  const out = document.getElementById('q-out'); out.replaceChildren(el('div', {class: 'muted'}, 'Running…'));
  try {
    const r = await api('POST', URL_('/query'), queryPayload());
    Q.result = r;
    out.replaceChildren(sqlBox(r.sql), el('div', {style: 'height:10px'}), renderTable(r.columns, r.rows, {empty: 'The query returned no rows.'}),
      el('div', {class: 'muted small', style: 'margin-top:6px'}, r.rows.length + ' row(s)' + (r.truncated ? ' (first 1000 shown)' : '')));
  } catch (e) { out.replaceChildren(el('div', {class: 'msg err'}, e.message)); }
}

function saveViewDialog() {
  if (!Q.result) { toast('Run the query first', 'err'); return; }
  const name = el('input', {placeholder: 'view_name', maxlength: 40});
  openDialog('Save query as a view', el('div', {}, el('label', {class: 'field'}, el('span', {}, 'View name'), name), sqlBox(Q.result.sql),
    el('p', {class: 'muted small'}, 'A view is a saved query that behaves like a read-only table. You can GRANT access to it without exposing the underlying tables.')), {
    okText: 'Create view', onOk: async () => { await api('POST', URL_('/views'), {name: name.value.trim(), sql: Q.result.sql}); await refresh(); toast('View created'); }});
}

function drawQuery() {
  const root = document.getElementById('q-form'); if (!root) return;
  const tnames = S.schema.tables.map(t => t.name);
  root.replaceChildren();
  root.append(section('FROM', 'start from one table', null, el('div', {class: 'brow'},
    sel(tnames, Q.table, v => { Q.table = v; Q.joins = []; Q.columns = []; Q.where = []; Q.group_by = []; Q.having = []; Q.order_by = []; drawQuery(); }, 'Table'),
    el('span', {class: 'muted small'}, 'AS'), txt(Q.alias, v => { Q.alias = v; }, 'alias (optional)', 12),
    el('button', {class: 'small', onclick: drawQuery}, 'Apply alias'))));
  root.append(section('JOIN', 'combine with other tables (use an alias for a self-join)', el('button', {class: 'small', onclick: () => {
    Q.joins.push({type: 'INNER', table: tnames[0], alias: '', left: '', right: ''}); drawQuery(); }}, '+ Join'),
    Q.joins.map((j, i) => {
      const refs = qRefs(i + 2);
      const fk = (() => { const main = tableSpec(j.table); return null; })();
      const row = el('div', {class: 'brow'}, sel(['INNER', 'LEFT', 'RIGHT', 'FULL', 'CROSS'], j.type, v => { j.type = v; drawQuery(); }, 'Join type'), 'JOIN',
        sel(tnames, j.table, v => { j.table = v; j.manual = false; drawQuery(); }, 'Join table'), 'AS', txt(j.alias, v => j.alias = v, 'alias', 8));
      if (j.type !== 'CROSS') {
        if (!j.manual) { const sg = suggestJoin(j, i); if (sg) { j.left = sg[0]; j.right = sg[1]; } }
        if (!j.manual) suggestJoin(j, i);
        if (!refs.includes(j.left)) j.left = refs[0] || '';
        if (!refs.includes(j.right)) j.right = refs[refs.length - 1] || '';
        row.append('ON', sel(refs, j.left, v => { j.left = v; j.manual = true; }, 'Left column'), '=', sel(refs, j.right, v => { j.right = v; j.manual = true; }, 'Right column'));
      }
      row.append(el('button', {class: 'small', onclick: drawQuery}, 'Apply'), el('button', {class: 'ghost danger icon', 'aria-label': 'Remove join', onclick: () => { Q.joins.splice(i, 1); drawQuery(); }}, '×'));
      return row;
    })));
  const refs = qRefs();
  root.append(section('SELECT', 'columns to show; leave empty for all', el('button', {class: 'small', onclick: () => { Q.columns.push({col: refs[0] || '', agg: '', alias: ''}); drawQuery(); }}, '+ Column'),
    [el('label', {class: 'opt'}, el('input', {type: 'checkbox', checked: Q.distinct, onchange: (e) => Q.distinct = e.target.checked}), 'DISTINCT'),
    Q.columns.map((c, i) => el('div', {class: 'brow'}, sel(AGGS, c.agg, v => c.agg = v, 'Aggregate'),
      sel(c.agg === 'COUNT' ? ['*', ...refs] : refs, c.col, v => c.col = v, 'Column'), 'AS', txt(c.alias, v => c.alias = v, 'name (optional)', 12),
      el('button', {class: 'ghost danger icon', 'aria-label': 'Remove column', onclick: () => { Q.columns.splice(i, 1); drawQuery(); }}, '×')))]));
  root.append(section('WHERE', 'filter rows - includes subqueries', el('button', {class: 'small', onclick: () => { Q.where.push({conj: 'AND', col: refs[0] || '', op: '=', value: ''}); drawQuery(); }}, '+ Condition'),
    Q.where.map((c, i) => condRow(c, i, Q.where))));
  root.append(section('GROUP BY / HAVING', 'summarise rows', el('div', {class: 'row'},
    el('button', {class: 'small', onclick: () => { Q.group_by.push(refs[0] || ''); drawQuery(); }}, '+ Group by'),
    el('button', {class: 'small', onclick: () => { Q.having.push({agg: 'COUNT', col: '*', op: '>=', value: '1'}); drawQuery(); }}, '+ Having')),
    [Q.group_by.map((g, i) => el('div', {class: 'brow'}, 'GROUP BY', sel(refs, g, v => Q.group_by[i] = v, 'Group column'),
      el('button', {class: 'ghost danger icon', 'aria-label': 'Remove', onclick: () => { Q.group_by.splice(i, 1); drawQuery(); }}, '×'))),
    Q.having.map((h, i) => el('div', {class: 'brow'}, 'HAVING', sel(AGGS.slice(1), h.agg, v => h.agg = v, 'Aggregate'), sel(['*', ...refs], h.col, v => h.col = v, 'Column'),
      sel(['=', '!=', '<', '>', '<=', '>='], h.op, v => h.op = v, 'Op'), txt(h.value, v => h.value = v, 'value', 8),
      el('button', {class: 'ghost danger icon', 'aria-label': 'Remove', onclick: () => { Q.having.splice(i, 1); drawQuery(); }}, '×')))]));
  const outNames = Q.columns.filter(c => c.alias).map(c => c.alias);
  root.append(section('ORDER BY / LIMIT', '', el('button', {class: 'small', onclick: () => { Q.order_by.push({col: refs[0] || '', dir: 'asc'}); drawQuery(); }}, '+ Order by'),
    [Q.order_by.map((o, i) => el('div', {class: 'brow'}, 'ORDER BY', sel([...refs, ...outNames], o.col, v => o.col = v, 'Order column'), sel([['asc', 'ascending'], ['desc', 'descending']], o.dir, v => o.dir = v, 'Direction'),
      el('button', {class: 'ghost danger icon', 'aria-label': 'Remove', onclick: () => { Q.order_by.splice(i, 1); drawQuery(); }}, '×'))),
    el('div', {class: 'brow'}, 'LIMIT', txt(Q.limit, v => Q.limit = v, 'rows', 6))]));
}

TABS.query = {label: 'Query builder', owner: false, render(panel) {
  if (!S.schema.tables.length) { panel.append(el('div', {class: 'card empty'}, 'Nothing to query yet.')); return; }
  if (!tableSpec(Q.table)) { Q.table = S.table || S.schema.tables[0].name; Q.joins = []; Q.columns = []; Q.where = []; Q.group_by = []; Q.having = []; Q.order_by = []; }
  panel.append(el('div', {class: 'row between', style: 'margin-bottom:10px'}, el('div', {}, el('h2', {style: 'margin:0'}, 'Query builder'),
    el('div', {class: 'muted small'}, 'Build a SELECT visually. The SQL it generates is shown with the results.')),
    el('div', {class: 'row'}, P.owner ? el('button', {onclick: saveViewDialog}, 'Save as view') : null, el('button', {class: 'primary', onclick: runQuery}, 'Run query'))));
  panel.append(el('div', {id: 'q-form', class: 'grid'}), el('div', {id: 'q-out', style: 'margin-top:16px'}));
  drawQuery();
  if (Q.result) document.getElementById('q-out').append(sqlBox(Q.result.sql), el('div', {style: 'height:10px'}), renderTable(Q.result.columns, Q.result.rows));
}};

/* ---------------------------------------------------------------- SQL console + TCL */
async function txStatus() { return api('GET', URL_('/tx')); }

function resultNodes(results) {
  return results.map(r => el('div', {style: 'margin-top:12px'}, sqlBox(r.sql), r.kind === 'rows'
    ? [el('div', {style: 'height:6px'}), renderTable(r.columns, r.rows, {empty: 'No rows.'}), r.truncated ? el('div', {class: 'muted small'}, 'First 500 rows shown.') : null]
    : el('div', {class: 'msg ok'}, (r.rowcount >= 0 ? r.rowcount : 0) + ' row(s) affected')));
}

TABS.sql = {label: 'SQL & transactions', owner: false, async render(panel) {
  const st = await txStatus();
  const t0 = S.schema.tables[0];
  const ta = el('textarea', {id: 'sql-text', rows: 6, spellcheck: 'false', placeholder: t0 ? `SELECT * FROM ${t0.name};` : 'SELECT 1;'});
  const out = el('div', {id: 'sql-out'});
  const bar = el('div', {class: 'tx-bar' + (st.open ? ' open' : '')});
  const spName = el('input', {placeholder: 'savepoint name', size: 14, value: 'sp' + (st.savepoints.length + 1)});
  const act = (name, body) => async () => {
    try { await api('POST', URL_('/tx/' + name), body ? body() : {}); await refresh(); toast(name.replace('_', ' ') + ' done'); }
    catch (e) { toast(e.message, 'err'); }
  };
  if (!st.open) bar.append(el('strong', {}, 'Autocommit mode'), el('span', {class: 'muted small'}, 'each run is saved immediately (a multi-statement script is all-or-nothing)'), el('span', {style: 'flex:1'}), el('button', {class: 'primary', onclick: act('begin')}, 'BEGIN transaction'));
  else {
    const rb = el('select', {'aria-label': 'Savepoint'}, st.savepoints.map(s => el('option', {value: s}, s)));
    bar.append(el('span', {class: 'badge warn'}, 'TRANSACTION OPEN'), el('span', {class: 'small'}, 'Changes are visible only to you until you COMMIT.'), el('span', {style: 'flex:1'}),
      spName, el('button', {onclick: act('savepoint', () => ({name: spName.value.trim()}))}, 'SAVEPOINT'),
      st.savepoints.length ? [rb, el('button', {onclick: act('rollback_to', () => ({name: rb.value}))}, 'ROLLBACK TO')] : null,
      el('button', {class: 'primary', onclick: act('commit')}, 'COMMIT'), el('button', {class: 'danger', onclick: act('rollback')}, 'ROLLBACK'));
  }
  const run = async () => {
    out.replaceChildren(el('div', {class: 'muted'}, 'Running…'));
    try {
      const r = await api('POST', URL_('/sql'), {sql: ta.value});
      out.replaceChildren(...resultNodes(r.results));
      const fresh = await api('GET', URL_('/schema')); S.schema = fresh; renderSide();
    } catch (e) { out.replaceChildren(el('div', {class: 'msg err'}, e.message)); }
  };
  ta.onkeydown = (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') run(); };
  const chips = t0 ? [
    ['SELECT', `SELECT * FROM ${t0.name};`],
    ['COUNT / GROUP BY', `SELECT ${t0.columns[t0.columns.length - 1].name}, COUNT(*) AS n\nFROM ${t0.name}\nGROUP BY ${t0.columns[t0.columns.length - 1].name}\nORDER BY n DESC;`],
    ['Subquery', `SELECT * FROM ${t0.name}\nWHERE ${t0.columns[0].name} IN (SELECT ${t0.columns[0].name} FROM ${t0.name});`],
    ['UNION', `SELECT ${t0.columns[0].name} FROM ${t0.name}\nUNION\nSELECT ${t0.columns[0].name} FROM ${t0.name};`],
    ['UPDATE', `UPDATE ${t0.name} SET ${t0.columns[t0.columns.length - 1].name} = ${t0.columns[t0.columns.length - 1].name}\nWHERE ${t0.columns[0].name} = 1;`],
    ['DELETE', `DELETE FROM ${t0.name} WHERE ${t0.columns[0].name} = -1;`]] : [];
  panel.append(el('h2', {}, 'SQL & transactions'),
    el('div', {class: 'muted small', style: 'margin-bottom:10px'}, 'Write SQL yourself. SELECT, INSERT, UPDATE and DELETE are allowed on tables you have privileges for. Change structure in the Design tab so the diagram stays in sync.'),
    bar, ta,
    el('div', {class: 'row between', style: 'margin-top:8px'}, el('div', {class: 'pill-row'}, chips.map(c => el('button', {class: 'small', onclick: () => { ta.value = c[1]; ta.focus(); }}, c[0]))),
      el('button', {class: 'primary', onclick: run}, 'Run  (Ctrl+Enter)')), out);
}};

/* ---------------------------------------------------------------- Routines */
TABS.routines = {label: 'Routines', owner: false, async render(panel) {
  const {routines} = await api('GET', URL_('/routines'));
  panel.append(el('h2', {}, 'Routines'), el('div', {class: 'muted small', style: 'margin-bottom:12px'},
    'A routine is a saved, parameterised batch of SQL (like a stored procedure). Use :name for parameters. A call runs as one atomic transaction - if any statement fails, everything is undone.'));
  if (P.owner) {
    const name = el('input', {placeholder: 'routine_name', maxlength: 40});
    const t0 = S.schema.tables[0];
    const body = el('textarea', {rows: 5, spellcheck: 'false', placeholder: t0 ? `UPDATE ${t0.name} SET ${t0.columns[t0.columns.length - 1].name} = :new_value WHERE ${t0.columns[0].name} = :id;\nSELECT * FROM ${t0.name} WHERE ${t0.columns[0].name} = :id;` : ''});
    panel.append(el('div', {class: 'card'}, el('h3', {}, 'New routine'), el('label', {class: 'field'}, el('span', {}, 'Name'), name), body,
      el('div', {class: 'row end', style: 'margin-top:8px'}, el('button', {class: 'primary', onclick: async () => {
        try { await api('POST', URL_('/routines'), {name: name.value.trim(), body: body.value}); toast('Routine saved'); renderPanel(); } catch (e) { toast(e.message, 'err'); }
      }}, 'Save routine'))));
  }
  if (!routines.length) panel.append(el('div', {class: 'card empty'}, 'No routines yet.'));
  routines.forEach(r => {
    const inputs = r.params.map(p => ({p, i: el('input', {placeholder: p, size: 12, 'aria-label': p})}));
    const out = el('div');
    panel.append(el('div', {class: 'card'}, el('div', {class: 'row between'}, el('h3', {style: 'margin:0'}, 'CALL ' + r.name + '(' + r.params.join(', ') + ')'),
      P.owner ? el('button', {class: 'small danger', onclick: async () => { if (confirm('Delete routine ' + r.name + '?')) { await api('DELETE', URL_('/routines/' + r.name)); renderPanel(); } }}, 'Delete') : null),
      el('div', {style: 'margin:8px 0'}, sqlBox(r.body)),
      el('div', {class: 'row'}, inputs.map(x => x.i), el('button', {class: 'primary', onclick: async () => {
        const params = {}; inputs.forEach(x => params[x.p] = x.i.value);
        try { const res = await api('POST', URL_('/routines/' + r.name + '/call'), {params}); out.replaceChildren(...resultNodes(res.results)); await refresh(); }
        catch (e) { out.replaceChildren(el('div', {class: 'msg err'}, e.message)); }
      }}, 'Run')), out));
  });
}};

/* ---------------------------------------------------------------- Views */
TABS.views = {label: 'Views', owner: false, async render(panel) {
  panel.append(el('h2', {}, 'Views'), el('div', {class: 'muted small', style: 'margin-bottom:12px'}, 'Saved queries that act like read-only tables. Create one from the Query builder ("Save as view") or by writing a SELECT here.'));
  if (P.owner) {
    const name = el('input', {placeholder: 'view_name', maxlength: 40});
    const t0 = S.schema.tables[0];
    const body = el('textarea', {rows: 4, spellcheck: 'false', placeholder: t0 ? `SELECT * FROM ${t0.name}` : 'SELECT ...'});
    panel.append(el('div', {class: 'card'}, el('h3', {}, 'New view'), el('label', {class: 'field'}, el('span', {}, 'Name'), name), body,
      el('div', {class: 'row end', style: 'margin-top:8px'}, el('button', {class: 'primary', onclick: async () => {
        try { await api('POST', URL_('/views'), {name: name.value.trim(), sql: body.value, replace: true}); toast('View saved'); await refresh(); } catch (e) { toast(e.message, 'err'); }
      }}, 'Create / replace view'))));
  }
  if (!S.schema.views.length) panel.append(el('div', {class: 'card empty'}, 'No views yet.'));
  S.schema.views.forEach(v => panel.append(el('div', {class: 'card'}, el('div', {class: 'row between'}, el('h3', {style: 'margin:0'}, v.name),
    el('div', {class: 'row'}, el('button', {class: 'small', onclick: () => { S.view = v.name; S.tab = 'data'; renderTabs(); renderSide(); renderPanel(); }}, 'Open'),
      P.owner ? el('button', {class: 'small danger', onclick: async () => { if (confirm('Drop view ' + v.name + '?')) { await api('DELETE', URL_('/views/' + v.name)); await refresh(); } }}, 'Drop') : null)),
    v.sql ? el('div', {style: 'margin-top:8px'}, sqlBox(v.sql)) : null)));
}};

/* ---------------------------------------------------------------- Normalize */
TABS.normalize = {label: 'Normalize', owner: true, async render(panel) {
  const t = S.table && tableSpec(S.table);
  panel.append(el('h2', {}, 'Normalization checker'), el('div', {class: 'muted small', style: 'margin-bottom:12px'},
    'Tell SchemaForge which columns determine which (functional dependencies). It finds candidate keys, the highest normal form, the offending dependencies, and a lossless decomposition.'));
  if (!t) { panel.append(el('div', {class: 'card empty'}, 'Pick a table first.')); return; }
  const [fdsR, an] = await Promise.all([api('GET', URL_('/fds/' + t.name)), api('GET', URL_('/normalize/' + t.name))]);
  const lhs = el('input', {placeholder: 'e.g. student_id', size: 24, 'aria-label': 'Determinant'});
  const rhs = el('input', {placeholder: 'e.g. student_name, dept', size: 28, 'aria-label': 'Dependent columns'});
  const add = async () => { try { await api('POST', URL_('/fds/' + t.name), {lhs: lhs.value, rhs: rhs.value}); renderPanel(); } catch (e) { toast(e.message, 'err'); } };
  rhs.onkeydown = (e) => { if (e.key === 'Enter') add(); };
  panel.append(el('div', {class: 'card'}, el('h3', {}, 'Functional dependencies for ', el('span', {class: 'mono'}, t.name)),
    el('div', {class: 'muted small'}, 'Columns: ' + t.columns.map(c => c.name).join(', ')),
    el('div', {class: 'row', style: 'margin:10px 0'}, lhs, '→', rhs, el('button', {class: 'primary', onclick: add}, 'Add')),
    fdsR.fds.length ? el('div', {class: 'pill-row'}, fdsR.fds.map(f => el('span', {class: 'badge gray', style: 'font-size:.85rem'}, f.lhs.replace(/,/g, ', ') + ' → ' + f.rhs.replace(/,/g, ', ') + ' ',
      el('a', {href: '#', title: 'Remove', onclick: async (e) => { e.preventDefault(); await api('DELETE', URL_('/fds/' + t.name + '/' + f.fd_id)); renderPanel(); }}, '×')))) : el('div', {class: 'muted small'}, 'No dependencies yet. Example: in a table (student_id, course_id, student_name, grade) add "student_id → student_name" and "student_id, course_id → grade".')));
  const nfClass = {BCNF: '', '3NF': '', '2NF': 'warn', '1NF': 'bad'}[an.highest_normal_form];
  const list = (arr) => arr.length ? el('ul', {style: 'margin:4px 0;padding-left:20px'}, arr.map(x => el('li', {class: 'mono small'}, x))) : el('div', {class: 'muted small'}, 'none');
  const parts = (ds) => el('div', {class: 'pill-row'}, ds.map(d => el('div', {class: 'card', style: 'padding:8px 12px;min-width:170px'},
    el('div', {class: 'small muted'}, 'relation'), el('div', {class: 'mono small'}, '(' + d.attributes.map(a => d.key.includes(a) ? a + '*' : a).join(', ') + ')'))));
  panel.append(el('div', {class: 'card'}, el('div', {class: 'row'}, el('h3', {style: 'margin:0'}, 'Result'), el('span', {class: 'badge ' + nfClass, style: 'font-size:.9rem'}, 'highest normal form: ' + an.highest_normal_form)),
    an.warnings.map(w => el('div', {class: 'msg warn'}, w)),
    Object.keys(an.repeating_groups).length ? el('div', {class: 'msg warn'}, '1NF smell: repeating column groups (' + Object.entries(an.repeating_groups).map(([k, v]) => v.join(', ')).join(' | ') + '). Move repeated values into their own table.') : null,
    el('dl', {class: 'kv', style: 'margin-top:10px'},
      el('dt', {}, 'Candidate keys'), el('dd', {}, an.candidate_keys.map(k => '{' + k.join(', ') + '}').join('  ') || '-'),
      el('dt', {}, 'Declared primary key'), el('dd', {}, '{' + an.declared_key.join(', ') + '}'),
      el('dt', {}, 'Prime attributes'), el('dd', {}, an.prime.join(', ') || '-'),
      el('dt', {}, 'Minimal cover'), el('dd', {}, list(an.minimal_cover)),
      el('dt', {}, 'Partial (2NF) problems'), el('dd', {}, list(an.violations['2NF'])),
      el('dt', {}, 'Transitive (3NF) problems'), el('dd', {}, list(an.violations['3NF'])),
      el('dt', {}, 'BCNF problems'), el('dd', {}, list(an.violations.BCNF)))));
  panel.append(el('div', {class: 'card'}, el('h3', {}, 'Suggested 3NF decomposition (lossless, dependency-preserving)'), parts(an.decomposition_3nf),
    el('div', {style: 'height:10px'}), sqlBox(an.migration_3nf), el('div', {class: 'row end', style: 'margin-top:6px'}, el('button', {class: 'small', onclick: () => copyText(an.migration_3nf)}, 'Copy SQL'))),
    el('div', {class: 'card'}, el('h3', {}, 'Suggested BCNF decomposition (lossless)'), parts(an.decomposition_bcnf),
      el('div', {style: 'height:10px'}), sqlBox(an.migration_bcnf), el('div', {class: 'row end', style: 'margin-top:6px'}, el('button', {class: 'small', onclick: () => copyText(an.migration_bcnf)}, 'Copy SQL'))));
}};

/* ---------------------------------------------------------------- Access */
TABS.access = {label: 'Access', owner: true, async render(panel) {
  const a = await api('GET', URL_('/access'));
  panel.append(el('h2', {}, 'Access control'), el('div', {class: 'muted small', style: 'margin-bottom:12px'},
    'Invite another SchemaForge user, then GRANT or REVOKE SELECT / INSERT / UPDATE / DELETE on individual tables and views. Privileges are enforced by the database engine, including inside the SQL console.'));
  const uname = el('input', {placeholder: 'their username', 'aria-label': 'Username'});
  panel.append(el('div', {class: 'card'}, el('h3', {}, 'Members'), el('div', {class: 'row', style: 'margin-bottom:8px'}, uname,
    el('button', {class: 'primary', onclick: async () => { try { await api('POST', URL_('/members'), {username: uname.value}); renderPanel(); } catch (e) { toast(e.message, 'err'); } }}, 'Add member')),
    a.members.length ? el('div', {class: 'pill-row'}, a.members.map(m => el('span', {class: 'badge gray', style: 'font-size:.85rem'}, m.username + ' ',
      el('a', {href: '#', onclick: async (e) => { e.preventDefault(); if (confirm('Remove ' + m.username + ' and all their privileges?')) { await api('DELETE', URL_('/members/' + m.username)); renderPanel(); } }}, '×')))) : el('div', {class: 'muted small'}, 'Nobody else has access yet.')));
  if (!a.members.length) return;
  const objs = [...S.schema.tables.map(t => t.name), ...S.schema.views.map(v => v.name)];
  const user = el('select', {'aria-label': 'User'}, a.members.map(m => el('option', {value: m.username}, m.username)));
  const obj = el('select', {'aria-label': 'Table or view'}, objs.map(o => el('option', {value: o}, o)));
  const priv = el('select', {'aria-label': 'Privilege'}, ['ALL', ...S.meta.privs].map(p => el('option', {value: p}, p)));
  const go = (revoke) => async () => { try { await api('POST', URL_('/grant'), {username: user.value, table: obj.value, privilege: priv.value, revoke}); renderPanel(); toast(revoke ? 'Revoked' : 'Granted'); } catch (e) { toast(e.message, 'err'); } };
  panel.append(el('div', {class: 'card'}, el('h3', {}, 'GRANT / REVOKE'), el('div', {class: 'row'}, priv, 'ON', obj, 'TO', user,
    el('button', {class: 'primary', onclick: go(false)}, 'Grant'), el('button', {class: 'danger', onclick: go(true)}, 'Revoke'))),
    el('div', {class: 'card'}, el('h3', {}, 'Current privileges'), a.grants.length ? renderTable(['username', 'table_name', 'privilege'], a.grants, {
      actions: (g) => el('button', {class: 'small danger', onclick: async () => { await api('POST', URL_('/grant'), {username: g.username, table: g.table_name, privilege: g.privilege, revoke: true}); renderPanel(); }}, 'Revoke')})
      : el('div', {class: 'muted small'}, 'No privileges granted. Members cannot see any table until you grant SELECT.')));
}};

/* ---------------------------------------------------------------- History */
TABS.history = {label: 'History & audit', owner: true, async render(panel) {
  const [act, aud] = await Promise.all([api('GET', URL_('/activity')), api('GET', URL_('/audit'))]);
  panel.append(el('h2', {}, 'History & audit'),
    el('div', {class: 'card'}, el('h3', {}, 'Audit trail (written by database triggers)'), el('div', {class: 'muted small', style: 'margin-bottom:8px'}, 'Turn on "Audit trail" for a table in the Design tab. Every insert, update and delete is recorded with the old and new values.'),
      renderTable(['audit_id', 'table_name', 'action', 'row_ref', 'old_data', 'new_data', 'changed_at'], aud.rows, {empty: 'No audited changes yet.'})),
    el('div', {class: 'card'}, el('h3', {}, 'Everything run on this database (DDL, DML, DCL, TCL)'),
      renderTable(['logged_at', 'username', 'kind', 'statement'], act.rows, {empty: 'Nothing yet.'})));
}};

/* ---------------------------------------------------------------- Export */
TABS.export = {label: 'Export', owner: true, async render(panel) {
  const dialect = el('select', {'aria-label': 'Dialect'}, S.meta.dialects.map(d => el('option', {value: d}, {sqlite: 'SQLite', postgres: 'PostgreSQL', mysql: 'MySQL / MariaDB', oracle: 'Oracle'}[d])));
  const data = el('input', {type: 'checkbox', checked: true}), extras = el('input', {type: 'checkbox', checked: true});
  const out = el('div');
  const qs = (dl) => '?' + new URLSearchParams({dialect: dialect.value, data: data.checked ? 1 : 0, extras: extras.checked ? 1 : 0, download: dl ? 1 : 0});
  const preview = async () => { try { const r = await api('GET', URL_('/export' + qs(false))); out.replaceChildren(sqlBox(r.sql), el('div', {class: 'row end', style: 'margin-top:8px'}, el('button', {onclick: () => copyText(r.sql)}, 'Copy'))); } catch (e) { out.replaceChildren(el('div', {class: 'msg err'}, e.message)); } };
  panel.append(el('h2', {}, 'Export'), el('div', {class: 'muted small', style: 'margin-bottom:12px'},
    'Take your database anywhere. The script contains CREATE TABLE statements with all constraints, indexes, data, views and privileges. With extras on it also includes an audit trigger, a cursor-based report procedure and a transaction demo written for the chosen database.'),
    el('div', {class: 'card'}, el('div', {class: 'row'}, el('label', {class: 'field', style: 'margin:0'}, el('span', {}, 'Target database'), dialect),
      el('label', {class: 'opt'}, data, 'Include data'), el('label', {class: 'opt'}, extras, 'Triggers, procedures, transaction demo'),
      el('button', {onclick: preview}, 'Preview'), el('button', {class: 'primary', onclick: () => { location.href = URL_('/export' + qs(true)); }}, 'Download .sql'),
      el('button', {onclick: () => { location.href = URL_('/dbfile'); }}, 'Download SQLite file'))), out);
  preview();
}};
