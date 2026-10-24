"""Engine: everything that touches a *user-built* database.

Every project is a separate SQLite file.  The designer UI produces a JSON
"table spec"; this module turns specs into DDL, keeps the specs in a hidden
metadata table (_sf_tables) so constraints round-trip exactly, performs ALTER
operations (by rebuilding tables where SQLite cannot alter in place), CRUD with
type validation, audit triggers, privilege enforcement (via SQLite's
authorizer, i.e. GRANT/REVOKE semantics) and server-side transactions with
SAVEPOINTs.
"""
import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

import db as platform

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
TYPES = ["INT", "VARCHAR", "TEXT", "DECIMAL", "FLOAT", "DATE", "DATETIME", "BOOLEAN"]
FK_ACTIONS = ["NO ACTION", "CASCADE", "SET NULL", "RESTRICT"]
CHECK_OPS = [">", ">=", "<", "<=", "=", "!=", "BETWEEN", "IN", "LIKE"]
PRIVS = ["SELECT", "INSERT", "UPDATE", "DELETE"]
RESERVED_PREFIX = "_"


class DesignError(Exception):
    """A user-facing validation / SQL error."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def ident(name, what="name"):
    if not isinstance(name, str) or not IDENT_RE.match(name):
        raise DesignError(
            f"Invalid {what} '{name}': use letters, digits and underscores, "
            "starting with a letter (max 40 characters).")
    return name


def q(name):
    return '"' + ident(name) + '"'


def user_ident(name, what="name"):
    """Identifier a user may create (cannot start with underscore)."""
    ident(name, what)
    if name.startswith(RESERVED_PREFIX):
        raise DesignError(f"{what.capitalize()} '{name}' is reserved (names starting with _ are internal).")
    if name.lower().startswith("sqlite"):
        raise DesignError(f"{what.capitalize()} '{name}' is reserved.")
    return name


def literal(v):
    v = str(v).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", v):
        return v
    return "'" + v.replace("'", "''") + "'"


def sql_type(c):
    t = (c.get("type") or "").upper()
    if t not in TYPES:
        raise DesignError(f"Unknown data type '{t}'.")
    if t == "VARCHAR":
        n = int(c.get("length") or 100)
        if not 1 <= n <= 4000:
            raise DesignError("VARCHAR length must be between 1 and 4000.")
        return f"VARCHAR({n})"
    if t == "DECIMAL":
        p = int(c.get("precision") or 10)
        s = int(c.get("scale") or 2)
        if not (1 <= p <= 38 and 0 <= s <= p):
            raise DesignError("DECIMAL needs 1<=precision<=38 and 0<=scale<=precision.")
        return f"DECIMAL({p},{s})"
    return t


def default_sql(d):
    d = str(d).strip()
    if d.upper() in ("CURRENT_TIMESTAMP", "CURRENT_DATE", "NULL"):
        return d.upper()
    return literal(d)


def check_sql(colname, ck):
    op = ck.get("op")
    val = str(ck.get("value", "")).strip()
    if op not in CHECK_OPS or val == "":
        raise DesignError(f"Incomplete CHECK constraint on {colname}.")
    c = q(colname)
    if op == "BETWEEN":
        parts = [p.strip() for p in val.split(",")]
        if len(parts) != 2:
            raise DesignError("BETWEEN needs two values: low,high")
        return f"CHECK ({c} BETWEEN {literal(parts[0])} AND {literal(parts[1])})"
    if op == "IN":
        parts = [p.strip() for p in val.split(",") if p.strip()]
        return f"CHECK ({c} IN ({', '.join(literal(p) for p in parts)}))"
    return f"CHECK ({c} {op} {literal(val)})"


# --------------------------------------------------------------------------
# spec -> DDL
# --------------------------------------------------------------------------
def normalise_spec(spec):
    """Validate a table spec coming from the browser and fill defaults."""
    name = user_ident(spec.get("name", ""), "table name")
    cols = spec.get("columns") or []
    if not cols:
        raise DesignError("A table needs at least one column.")
    seen = set()
    out = []
    for c in cols:
        cn = user_ident(c.get("name", ""), "column name")
        if cn.lower() in seen:
            raise DesignError(f"Duplicate column '{cn}'.")
        seen.add(cn.lower())
        nc = {
            "name": cn,
            "type": (c.get("type") or "").upper(),
            "length": c.get("length"), "precision": c.get("precision"), "scale": c.get("scale"),
            "pk": bool(c.get("pk")), "auto": bool(c.get("auto")),
            "notnull": bool(c.get("notnull")), "unique": bool(c.get("unique")),
            "default": (c.get("default") or "") if c.get("default") not in (None,) else "",
            "check": c.get("check") if c.get("check") and c["check"].get("op") else None,
            "fk": c.get("fk") if c.get("fk") and c["fk"].get("table") else None,
        }
        sql_type(nc)  # validates
        if nc["fk"]:
            ident(nc["fk"]["table"], "referenced table")
            ident(nc["fk"]["column"], "referenced column")
            if nc["fk"].get("on_delete", "NO ACTION") not in FK_ACTIONS:
                raise DesignError("Bad ON DELETE action.")
        if nc["pk"]:
            nc["notnull"] = True
        out.append(nc)
    pks = [c for c in out if c["pk"]]
    for c in out:
        if c["auto"] and not (c["pk"] and len(pks) == 1 and c["type"] == "INT"):
            raise DesignError("AUTO INCREMENT is only allowed on a single INT primary key column.")
    return {
        "name": name,
        "columns": out,
        "indexes": spec.get("indexes") or [],
        "audit": bool(spec.get("audit")),
    }


def create_table_sql(spec, name=None):
    name = name or spec["name"]
    pks = [c for c in spec["columns"] if c["pk"]]
    lines = []
    for c in spec["columns"]:
        t = sql_type(c)
        if c["auto"]:
            lines.append(f"  {q(c['name'])} INTEGER PRIMARY KEY AUTOINCREMENT")
            continue
        parts = [q(c["name"]), t]
        if c["notnull"]:
            parts.append("NOT NULL")
        if c["unique"] and not c["pk"]:
            parts.append("UNIQUE")
        if c["default"] not in ("", None):
            parts.append("DEFAULT " + default_sql(c["default"]))
        if c["check"]:
            parts.append(check_sql(c["name"], c["check"]))
        lines.append("  " + " ".join(parts))
    if pks and not (len(pks) == 1 and pks[0]["auto"]):
        lines.append("  PRIMARY KEY (" + ", ".join(q(c["name"]) for c in pks) + ")")
    for c in spec["columns"]:
        fk = c["fk"]
        if fk:
            od = fk.get("on_delete", "NO ACTION")
            lines.append(f"  FOREIGN KEY ({q(c['name'])}) REFERENCES {q(fk['table'])} ({q(fk['column'])}) ON DELETE {od}")
    return f"CREATE TABLE {q(name)} (\n" + ",\n".join(lines) + "\n)"


def index_sql(table, ix):
    u = "UNIQUE " if ix.get("unique") else ""
    cols = ", ".join(q(c) for c in ix["columns"])
    return f"CREATE {u}INDEX {q(ix['name'])} ON {q(table)} ({cols})"


def audit_trigger_sql(spec):
    t = spec["name"]
    cols = [c["name"] for c in spec["columns"]]
    new = ", ".join(f"'{c}', NEW.{q(c)}" for c in cols)
    old = ", ".join(f"'{c}', OLD.{q(c)}" for c in cols)
    base = "INSERT INTO _audit_log (table_name, action, row_ref, old_data, new_data) VALUES "
    return [
        f"CREATE TRIGGER {q('trg_' + t + '_audit_ins')} AFTER INSERT ON {q(t)} BEGIN "
        f"{base}('{t}', 'INSERT', NEW.rowid, NULL, json_object({new})); END",
        f"CREATE TRIGGER {q('trg_' + t + '_audit_upd')} AFTER UPDATE ON {q(t)} BEGIN "
        f"{base}('{t}', 'UPDATE', NEW.rowid, json_object({old}), json_object({new})); END",
        f"CREATE TRIGGER {q('trg_' + t + '_audit_del')} AFTER DELETE ON {q(t)} BEGIN "
        f"{base}('{t}', 'DELETE', OLD.rowid, json_object({old}), NULL); END",
    ]


# --------------------------------------------------------------------------
# connections, metadata
# --------------------------------------------------------------------------
META_DDL = """
CREATE TABLE IF NOT EXISTS _sf_tables (table_name TEXT PRIMARY KEY, spec TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS _audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL, action TEXT NOT NULL, row_ref INTEGER,
    old_data TEXT, new_data TEXT,
    changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""


class GuardedConn(sqlite3.Connection):
    """Remembers the target table of the statement being prepared, so the
    authorizer can tell the statement's own table from tables touched only
    by foreign-key actions (ON DELETE CASCADE / SET NULL)."""
    top = None

    def execute(self, *a, **k):
        self.top = None
        return super().execute(*a, **k)


def open_project(project_id, create=False):
    path = platform.project_path(project_id)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=5, factory=GuardedConn)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(META_DDL)
    return conn


def load_specs(conn):
    return {r["table_name"]: json.loads(r["spec"])
            for r in conn.execute("SELECT table_name, spec FROM _sf_tables ORDER BY rowid")}


def save_spec(conn, spec):
    conn.execute("INSERT OR REPLACE INTO _sf_tables(table_name, spec) VALUES (?,?)",
                 (spec["name"], json.dumps(spec)))


def row_count(conn, name):
    return conn.execute(f"SELECT COUNT(*) FROM {q(name)}").fetchone()[0]


def schema_json(conn):
    specs = load_specs(conn)
    out = []
    for name, s in specs.items():
        s = dict(s)
        s["rows"] = row_count(conn, name)
        out.append(s)
    views = [dict(name=r["name"], sql=r["sql"])
             for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY name")]
    return {"tables": out, "views": views}


def run_script(conn, statements, in_tx=True):
    """Run DDL atomically (SQLite DDL is transactional)."""
    try:
        if in_tx:
            conn.execute("BEGIN")
        for s in statements:
            conn.execute(s)
        if in_tx:
            conn.execute("COMMIT")
    except sqlite3.Error as e:
        if in_tx and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise DesignError(str(e))


def check_fk_targets(spec, specs):
    for c in spec["columns"]:
        fk = c["fk"]
        if not fk:
            continue
        target = spec if fk["table"] == spec["name"] else specs.get(fk["table"])
        if not target:
            raise DesignError(f"Column {c['name']} references unknown table '{fk['table']}'.")
        tcol = next((x for x in target["columns"] if x["name"] == fk["column"]), None)
        if not tcol:
            raise DesignError(f"Table {fk['table']} has no column '{fk['column']}'.")
        if not (tcol["pk"] or tcol["unique"]):
            raise DesignError(f"{fk['table']}.{fk['column']} must be a PRIMARY KEY or UNIQUE to be referenced.")


# --------------------------------------------------------------------------
# DDL operations
# --------------------------------------------------------------------------
def create_table(conn, raw):
    spec = normalise_spec(raw)
    specs = load_specs(conn)
    if spec["name"] in specs:
        raise DesignError(f"Table '{spec['name']}' already exists.")
    check_fk_targets(spec, specs)
    stmts = [create_table_sql(spec)]
    stmts += [index_sql(spec["name"], ix) for ix in spec["indexes"]]
    if spec["audit"]:
        stmts += audit_trigger_sql(spec)
    run_script(conn, stmts)
    save_spec(conn, spec)
    return stmts


def drop_table(conn, name):
    specs = load_specs(conn)
    if name not in specs:
        raise DesignError("No such table.")
    for other, s in specs.items():
        if other != name and any(c["fk"] and c["fk"]["table"] == name for c in s["columns"]):
            raise DesignError(f"Cannot drop '{name}': table '{other}' has a foreign key to it. Drop that first.")
    stmts = [f"DROP TABLE {q(name)}"]
    run_script(conn, stmts)
    conn.execute("DELETE FROM _sf_tables WHERE table_name=?", (name,))
    return stmts


def rename_table(conn, old, new):
    specs = load_specs(conn)
    user_ident(new, "table name")
    if old not in specs:
        raise DesignError("No such table.")
    if new in specs:
        raise DesignError(f"Table '{new}' already exists.")
    spec = specs[old]
    stmts = [f"ALTER TABLE {q(old)} RENAME TO {q(new)}"]
    run_script(conn, stmts)
    conn.execute("DELETE FROM _sf_tables WHERE table_name=?", (old,))
    spec["name"] = new
    save_spec(conn, spec)
    for other, s in specs.items():
        changed = False
        for c in s["columns"]:
            if c["fk"] and c["fk"]["table"] == old:
                c["fk"]["table"] = new
                changed = True
        if changed:
            save_spec(conn, s)
    if spec["audit"]:
        _recreate_audit(conn, spec, old_name=old)
    return stmts


def _drop_audit(conn, name):
    for suf in ("ins", "upd", "del"):
        conn.execute(f"DROP TRIGGER IF EXISTS {q('trg_' + name + '_audit_' + suf)}")


def _recreate_audit(conn, spec, old_name=None):
    _drop_audit(conn, old_name or spec["name"])
    _drop_audit(conn, spec["name"])
    for s in audit_trigger_sql(spec):
        conn.execute(s)


def set_audit(conn, name, on):
    specs = load_specs(conn)
    spec = specs.get(name)
    if not spec:
        raise DesignError("No such table.")
    spec["audit"] = bool(on)
    if on:
        _recreate_audit(conn, spec)
    else:
        _drop_audit(conn, name)
    save_spec(conn, spec)


def rebuild_table(conn, old_spec, new_spec, colmap):
    """SQLite 'table rebuild': create new table, copy rows, drop old, rename.
    colmap maps new column name -> old column name (or None for new columns)."""
    name = old_spec["name"]
    tmp = "_rebuild_" + name
    saved_views = [r["sql"] for r in conn.execute("SELECT sql FROM sqlite_master WHERE type='view'")]
    view_names = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")]
    conn.execute("PRAGMA foreign_keys = OFF")
    warnings = []
    try:
        conn.execute("BEGIN")
        for v in view_names:
            conn.execute(f"DROP VIEW {q(v)}")
        conn.execute(create_table_sql(new_spec, name=tmp))
        targets, sources = [], []
        for c in new_spec["columns"]:
            src = colmap.get(c["name"])
            if src:
                targets.append(q(c["name"]))
                sources.append(q(src))
        if targets:
            conn.execute(f"INSERT INTO {q(tmp)} ({', '.join(targets)}) SELECT {', '.join(sources)} FROM {q(name)}")
        _drop_audit(conn, name)
        conn.execute(f"DROP TABLE {q(name)}")
        conn.execute(f"ALTER TABLE {q(tmp)} RENAME TO {q(name)}")
        for ix in new_spec["indexes"]:
            conn.execute(index_sql(name, ix))
        if new_spec["audit"]:
            for s in audit_trigger_sql(new_spec):
                conn.execute(s)
        for vn, vsql in zip(view_names, saved_views):
            try:
                conn.execute("SAVEPOINT v")
                conn.execute(vsql)
                conn.execute("RELEASE v")
            except sqlite3.Error as e:
                conn.execute("ROLLBACK TO v")
                conn.execute("RELEASE v")
                warnings.append(f"View '{vn}' could not be kept: {e}")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise DesignError("Existing data would violate a foreign key after this change.")
        conn.execute("COMMIT")
    except (sqlite3.Error, DesignError) as e:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise DesignError(str(e))
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    save_spec(conn, new_spec)
    return warnings


def _col_index(spec, name):
    for i, c in enumerate(spec["columns"]):
        if c["name"] == name:
            return i
    raise DesignError(f"No such column '{name}'.")


def add_column(conn, table, col):
    specs = load_specs(conn)
    old = specs.get(table)
    if not old:
        raise DesignError("No such table.")
    new = json.loads(json.dumps(old))
    new["columns"].append(col)
    new = normalise_spec(new)
    check_fk_targets(new, specs)
    c = new["columns"][-1]
    if c["pk"]:
        raise DesignError("Cannot add a primary key column to an existing table; create a new table instead.")
    if c["notnull"] and c["default"] in ("", None) and row_count(conn, table) > 0:
        raise DesignError("The table already has rows: a NOT NULL column needs a DEFAULT value.")
    colmap = {x["name"]: x["name"] for x in old["columns"]}
    stmts = ["-- table rebuild: " + create_table_sql(new)]
    warn = rebuild_table(conn, old, new, colmap)
    return stmts, warn


def drop_column(conn, table, colname):
    specs = load_specs(conn)
    old = specs.get(table)
    if not old:
        raise DesignError("No such table.")
    if len(old["columns"]) == 1:
        raise DesignError("Cannot drop the only column.")
    i = _col_index(old, colname)
    if old["columns"][i]["pk"]:
        raise DesignError("Cannot drop a primary key column.")
    for other, s in specs.items():
        for c in s["columns"]:
            if c["fk"] and c["fk"]["table"] == table and c["fk"]["column"] == colname:
                raise DesignError(f"{other}.{c['name']} references this column.")
    new = json.loads(json.dumps(old))
    del new["columns"][i]
    new["indexes"] = [ix for ix in new["indexes"] if colname not in ix["columns"]]
    colmap = {x["name"]: x["name"] for x in new["columns"]}
    warn = rebuild_table(conn, old, new, colmap)
    return [f"ALTER TABLE {q(table)} DROP COLUMN {q(colname)}"], warn


def modify_column(conn, table, colname, newcol):
    specs = load_specs(conn)
    old = specs.get(table)
    if not old:
        raise DesignError("No such table.")
    i = _col_index(old, colname)
    new = json.loads(json.dumps(old))
    new["columns"][i] = newcol
    new = normalise_spec(new)
    check_fk_targets(new, specs)
    nn = new["columns"][i]["name"]
    colmap = {x["name"]: x["name"] for x in old["columns"]}
    colmap.pop(colname)
    colmap[nn] = colname
    if new["columns"][i]["notnull"] and not old["columns"][i]["notnull"]:
        nulls = conn.execute(f"SELECT COUNT(*) FROM {q(table)} WHERE {q(colname)} IS NULL").fetchone()[0]
        if nulls and new["columns"][i]["default"] in ("", None):
            raise DesignError(f"{nulls} existing row(s) have NULL here; set a DEFAULT first.")
    if nn != colname:
        for other, s in specs.items():
            changed = False
            for c in s["columns"]:
                if c["fk"] and c["fk"]["table"] == table and c["fk"]["column"] == colname:
                    c["fk"]["column"] = nn
                    changed = True
            if changed and other != table:
                save_spec(conn, s)
        new["indexes"] = [dict(ix, columns=[nn if c == colname else c for c in ix["columns"]]) for ix in new["indexes"]]
    warn = rebuild_table(conn, old, new, colmap)
    return ["-- table rebuild: " + create_table_sql(new)], warn


def create_index(conn, table, name, columns, unique=False):
    specs = load_specs(conn)
    spec = specs.get(table)
    if not spec:
        raise DesignError("No such table.")
    user_ident(name, "index name")
    names = [c["name"] for c in spec["columns"]]
    if not columns or any(c not in names for c in columns):
        raise DesignError("Pick existing columns for the index.")
    ix = {"name": name, "columns": columns, "unique": bool(unique)}
    run_script(conn, [index_sql(table, ix)])
    spec["indexes"].append(ix)
    save_spec(conn, spec)
    return [index_sql(table, ix)]


def drop_index(conn, table, name):
    specs = load_specs(conn)
    spec = specs.get(table)
    if not spec or name not in [i["name"] for i in spec["indexes"]]:
        raise DesignError("No such index.")
    run_script(conn, [f"DROP INDEX {q(name)}"])
    spec["indexes"] = [i for i in spec["indexes"] if i["name"] != name]
    save_spec(conn, spec)


def add_relationship(conn, kind, parent, child, on_delete="NO ACTION", junction=None):
    """kind: 1:N (FK on child), 1:1 (FK + UNIQUE on child), M:N (junction table)."""
    specs = load_specs(conn)
    if parent not in specs or child not in specs:
        raise DesignError("Pick two existing tables.")

    def single_pk(spec):
        pks = [c for c in spec["columns"] if c["pk"]]
        if len(pks) != 1:
            raise DesignError(f"Table '{spec['name']}' needs a single-column primary key for this relationship.")
        return pks[0]

    ppk = single_pk(specs[parent])
    if kind in ("1:N", "1:1"):
        fkname = f"{parent}_{ppk['name']}"
        if any(c["name"] == fkname for c in specs[child]["columns"]):
            raise DesignError(f"'{child}' already has a column '{fkname}'.")
        col = {"name": fkname, "type": ppk["type"], "length": ppk.get("length"),
               "precision": ppk.get("precision"), "scale": ppk.get("scale"),
               "notnull": False, "unique": kind == "1:1",
               "fk": {"table": parent, "column": ppk["name"], "on_delete": on_delete}}
        return add_column(conn, child, col)
    if kind == "M:N":
        cpk = single_pk(specs[child])
        jname = junction or f"{parent}_{child}"
        a, b = f"{parent}_{ppk['name']}", f"{child}_{cpk['name']}"
        if parent == child:
            b = f"related_{b}"
        spec = {"name": jname, "columns": [
            {"name": a, "type": ppk["type"], "pk": True, "length": ppk.get("length"),
             "fk": {"table": parent, "column": ppk["name"], "on_delete": "CASCADE"}},
            {"name": b, "type": cpk["type"], "pk": True, "length": cpk.get("length"),
             "fk": {"table": child, "column": cpk["name"], "on_delete": "CASCADE"}}]}
        return create_table(conn, spec), []
    raise DesignError("Unknown relationship type.")


# --------------------------------------------------------------------------
# validation + CRUD
# --------------------------------------------------------------------------
def coerce(col, raw):
    """Validate a form value against the column's declared type."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    t, v = col["type"], raw
    try:
        if t == "INT":
            if isinstance(v, bool):
                return int(v)
            f = Decimal(str(v).strip())
            if f != f.to_integral_value():
                raise ValueError
            return int(f)
        if t in ("FLOAT", "DECIMAL"):
            d = Decimal(str(v).strip())
            if t == "DECIMAL":
                scale = int(col.get("scale") or 2)
                prec = int(col.get("precision") or 10)
                if abs(d) >= Decimal(10) ** (prec - scale):
                    raise DesignError(f"'{col['name']}' is too large for DECIMAL({prec},{scale}).")
                return float(round(d, scale))
            return float(d)
        if t == "BOOLEAN":
            s = str(v).strip().lower()
            if s in ("1", "true", "yes", "on", "t"):
                return 1
            if s in ("0", "false", "no", "off", "f"):
                return 0
            raise ValueError
        if t == "DATE":
            return datetime.strptime(str(v).strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
        if t == "DATETIME":
            s = str(v).strip().replace("T", " ")
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
            raise ValueError
        s = str(v)
        if t == "VARCHAR":
            n = int(col.get("length") or 100)
            if len(s) > n:
                raise DesignError(f"'{col['name']}' is too long (max {n} characters).")
        return s
    except (ValueError, InvalidOperation):
        raise DesignError(f"'{col['name']}' must be a valid {t}.")


def _spec(conn, table):
    spec = load_specs(conn).get(table)
    if not spec:
        raise DesignError("No such table.")
    return spec


def list_rows(conn, table, page=1, size=25, sort=None, direction="asc", search=""):
    spec = _spec(conn, table)
    names = [c["name"] for c in spec["columns"]]
    where, params = "", []
    if search:
        tcols = [c["name"] for c in spec["columns"]]
        where = "WHERE " + " OR ".join(f"CAST({q(c)} AS TEXT) LIKE ?" for c in tcols)
        params = [f"%{search}%"] * len(tcols)
    order = ""
    if sort:
        if sort not in names:
            raise DesignError("Bad sort column.")
        order = f"ORDER BY {q(sort)} {'DESC' if direction == 'desc' else 'ASC'}"
    total = conn.execute(f"SELECT COUNT(*) FROM {q(table)} {where}", params).fetchone()[0]
    page, size = max(1, int(page)), min(200, max(1, int(size)))
    rows = conn.execute(
        f"SELECT rowid AS _rid_, * FROM {q(table)} {where} {order} LIMIT ? OFFSET ?",
        params + [size, (page - 1) * size]).fetchall()
    return {"columns": names, "rows": [dict(r) for r in rows], "total": total, "page": page, "size": size}


def _clean_values(spec, values, for_insert):
    out = {}
    for c in spec["columns"]:
        if c["name"] not in values:
            continue
        v = values[c["name"]]
        if for_insert and c["auto"] and (v is None or str(v).strip() == ""):
            continue
        val = coerce(c, v)
        if val is None and c["default"] not in ("", None) and for_insert:
            continue  # let the DEFAULT apply
        out[c["name"]] = val
    return out


def insert_row(conn, table, values):
    spec = _spec(conn, table)
    vals = _clean_values(spec, values, True)
    try:
        if vals:
            cols = ", ".join(q(k) for k in vals)
            marks = ", ".join("?" for _ in vals)
            sql = f"INSERT INTO {q(table)} ({cols}) VALUES ({marks})"
            cur = conn.execute(sql, list(vals.values()))
        else:
            sql = f"INSERT INTO {q(table)} DEFAULT VALUES"
            cur = conn.execute(sql)
        return cur.lastrowid, sql
    except sqlite3.Error as e:
        raise DesignError(friendly_error(e))


def update_row(conn, table, rid, values):
    spec = _spec(conn, table)
    vals = _clean_values(spec, values, False)
    if not vals:
        raise DesignError("Nothing to update.")
    sets = ", ".join(f"{q(k)} = ?" for k in vals)
    sql = f"UPDATE {q(table)} SET {sets} WHERE rowid = ?"
    try:
        cur = conn.execute(sql, list(vals.values()) + [int(rid)])
    except sqlite3.Error as e:
        raise DesignError(friendly_error(e))
    return cur.rowcount, sql


def delete_row(conn, table, rid):
    _spec(conn, table)
    sql = f"DELETE FROM {q(table)} WHERE rowid = ?"
    try:
        cur = conn.execute(sql, (int(rid),))
    except sqlite3.Error as e:
        raise DesignError(friendly_error(e))
    return cur.rowcount, sql


def fk_options(conn, table, col):
    """Values for a foreign-key dropdown: id plus a readable label."""
    spec = _spec(conn, table)
    c = next((x for x in spec["columns"] if x["name"] == col), None)
    if not c or not c["fk"]:
        raise DesignError("Not a foreign key column.")
    fk = c["fk"]
    target = _spec(conn, fk["table"])
    label = next((x["name"] for x in target["columns"]
                  if x["type"] in ("VARCHAR", "TEXT") and x["name"] != fk["column"]), None)
    sel = f"{q(fk['column'])} AS id" + (f", {q(label)} AS label" if label else ", NULL AS label")
    rows = conn.execute(f"SELECT {sel} FROM {q(fk['table'])} ORDER BY 1 LIMIT 500").fetchall()
    return [{"id": r["id"], "label": r["label"]} for r in rows]


def import_csv(conn, table, text):
    """Bulk-load CSV atomically (all rows or none)."""
    import csv
    import io
    spec = _spec(conn, table)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise DesignError("CSV is empty.")
    names = {c["name"] for c in spec["columns"]}
    unknown = [f for f in reader.fieldnames if f not in names]
    if unknown:
        raise DesignError("Unknown CSV columns: " + ", ".join(unknown))
    n = 0
    nested = conn.in_transaction
    conn.execute("SAVEPOINT csv_import" if nested else "BEGIN")
    try:
        for i, row in enumerate(reader, start=2):
            try:
                insert_row(conn, table, row)
            except DesignError as e:
                raise DesignError(f"CSV line {i}: {e}")
            n += 1
        conn.execute("RELEASE csv_import" if nested else "COMMIT")
    except Exception:
        if nested:
            conn.execute("ROLLBACK TO csv_import")
            conn.execute("RELEASE csv_import")
        elif conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return n


def friendly_error(e):
    m = str(e)
    if "UNIQUE constraint failed" in m:
        return "Duplicate value: " + m.split(":", 1)[1].strip() + " must be unique."
    if "NOT NULL constraint failed" in m:
        return m.split(":", 1)[1].strip() + " cannot be empty."
    if "FOREIGN KEY constraint failed" in m:
        return "Foreign key violation: the referenced row does not exist, or other rows still depend on this one."
    if "CHECK constraint failed" in m:
        return "CHECK constraint failed: " + m.split(":", 1)[1].strip()
    if "not authorized" in m or "prohibited" in m:
        return "Permission denied: you do not have the required privilege (ask the owner for a GRANT)."
    return m


# --------------------------------------------------------------------------
# privileges (GRANT / REVOKE) enforced through the SQLite authorizer
# --------------------------------------------------------------------------
_A = sqlite3
DDL_ACTIONS = {
    _A.SQLITE_CREATE_INDEX, _A.SQLITE_CREATE_TABLE, _A.SQLITE_CREATE_TEMP_INDEX,
    _A.SQLITE_CREATE_TEMP_TABLE, _A.SQLITE_CREATE_TEMP_TRIGGER, _A.SQLITE_CREATE_TEMP_VIEW,
    _A.SQLITE_CREATE_TRIGGER, _A.SQLITE_CREATE_VIEW, _A.SQLITE_DROP_INDEX, _A.SQLITE_DROP_TABLE,
    _A.SQLITE_DROP_TEMP_INDEX, _A.SQLITE_DROP_TEMP_TABLE, _A.SQLITE_DROP_TEMP_TRIGGER,
    _A.SQLITE_DROP_TEMP_VIEW, _A.SQLITE_DROP_TRIGGER, _A.SQLITE_DROP_VIEW, _A.SQLITE_ALTER_TABLE,
    _A.SQLITE_CREATE_VTABLE, _A.SQLITE_DROP_VTABLE,
}


def make_authorizer(is_owner, grants, allow_ddl=False, fk_reads=frozenset(), state=None):
    """grants: set of (table_lower, PRIVILEGE).  Direct writes to internal
    tables (names starting with _) are always refused, so the audit log and
    design metadata can only be changed by the app and by triggers.
    state: the GuardedConn, used to recognise foreign-key actions."""
    dml = {_A.SQLITE_INSERT: "INSERT", _A.SQLITE_UPDATE: "UPDATE", _A.SQLITE_DELETE: "DELETE"}

    def auth(action, a1, a2, dbname, source):
        if action in (_A.SQLITE_ATTACH, _A.SQLITE_DETACH, _A.SQLITE_PRAGMA):
            return _A.SQLITE_DENY
        if action in DDL_ACTIONS:
            return _A.SQLITE_OK if (is_owner and allow_ddl) else _A.SQLITE_DENY
        tbl = (a1 or "").lower()
        if source is None and action in dml and tbl.startswith("_"):
            return _A.SQLITE_DENY
        if is_owner:
            return _A.SQLITE_OK
        if source is not None:      # inside a view or trigger: definer's rights
            return _A.SQLITE_OK
        if action == _A.SQLITE_READ:
            if tbl == "_sf_tables":     # design metadata (names/columns only, no row data)
                return _A.SQLITE_OK
            if (tbl, (a2 or "").lower()) in fk_reads:   # referential-integrity checks need the FK column
                return _A.SQLITE_OK
            if tbl.startswith("_") or tbl.startswith("sqlite"):
                return _A.SQLITE_DENY
            return _A.SQLITE_OK if (tbl, "SELECT") in grants else _A.SQLITE_DENY
        if action in dml:
            if state is not None and state.top is not None and state.top != tbl:
                return _A.SQLITE_OK     # foreign-key action on another table
            ok = (tbl, dml[action]) in grants
            if ok and state is not None and state.top is None:
                state.top = tbl
            return _A.SQLITE_OK if ok else _A.SQLITE_DENY
        return _A.SQLITE_OK
    return auth


def user_conn(project_id, is_owner, grants, allow_ddl=False):
    conn = open_project(project_id)
    fk_reads = set()
    if not is_owner:
        write_on = {t for (t, p) in grants if p in ("INSERT", "UPDATE", "DELETE")}
        for tname, spec in load_specs(conn).items():
            for c in spec["columns"]:
                if c["fk"] and c["fk"]["table"].lower() in write_on:
                    fk_reads.add((tname.lower(), c["name"].lower()))
    conn.set_authorizer(make_authorizer(is_owner, grants, allow_ddl, frozenset(fk_reads), conn))
    return conn


# --------------------------------------------------------------------------
# SQL console + transactions (TCL)
# --------------------------------------------------------------------------
class Tx:
    def __init__(self, conn):
        self.conn = conn
        self.savepoints = []
        self.statements = 0
        self.touched = time.time()


_TX = {}
_TX_LOCK = threading.Lock()
TX_IDLE_SECONDS = 600


def _reap():
    now = time.time()
    for k in list(_TX):
        if now - _TX[k].touched > TX_IDLE_SECONDS:
            try:
                _TX[k].conn.execute("ROLLBACK")
                _TX[k].conn.close()
            except Exception:
                pass
            _TX.pop(k, None)


def tx_get(key):
    with _TX_LOCK:
        _reap()
        return _TX.get(key)


def tx_begin(key, conn):
    with _TX_LOCK:
        _reap()
        if key in _TX:
            raise DesignError("A transaction is already open.")
        conn.execute("BEGIN")
        _TX[key] = Tx(conn)
    return _TX[key]


def tx_end(key, commit):
    with _TX_LOCK:
        tx = _TX.pop(key, None)
    if not tx:
        raise DesignError("No open transaction.")
    try:
        tx.conn.execute("COMMIT" if commit else "ROLLBACK")
    finally:
        tx.conn.close()
    return tx.statements


def tx_discard(key):
    with _TX_LOCK:
        tx = _TX.pop(key, None)
    if tx:
        try:
            tx.conn.execute("ROLLBACK")
            tx.conn.close()
        except Exception:
            pass


def tx_savepoint(key, name):
    tx = tx_get(key)
    if not tx:
        raise DesignError("No open transaction.")
    ident(name, "savepoint name")
    tx.conn.execute(f"SAVEPOINT {q(name)}")
    tx.savepoints.append(name)
    return tx.savepoints


def tx_rollback_to(key, name):
    tx = tx_get(key)
    if not tx:
        raise DesignError("No open transaction.")
    if name not in tx.savepoints:
        raise DesignError("Unknown savepoint.")
    tx.conn.execute(f"ROLLBACK TO {q(name)}")
    tx.savepoints = tx.savepoints[: tx.savepoints.index(name) + 1]
    return tx.savepoints


def split_statements(text):
    """Split a script on semicolons that really end a statement (respects
    quotes, comments and CREATE TRIGGER ... BEGIN ... END; bodies)."""
    stmts, buf = [], ""
    for ch in text:
        buf += ch
        if ch == ";" and sqlite3.complete_statement(buf):
            if buf.strip().strip(";").strip():
                stmts.append(buf.strip())
            buf = ""
    if buf.strip().strip(";").strip():
        stmts.append(buf.strip())
    return stmts


CONTROL = re.compile(r"^\s*(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", re.I)


def run_sql(conn, text, params=None, max_rows=500):
    """Run one or more statements. Returns list of result dicts."""
    results = []
    for stmt in split_statements(text):
        if CONTROL.match(stmt):
            raise DesignError("Use the Begin / Commit / Rollback / Savepoint buttons for transaction control.")
        try:
            cur = conn.execute(stmt, params or {})
        except sqlite3.Error as e:
            raise DesignError(friendly_error(e))
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(max_rows + 1)
            results.append({"kind": "rows", "columns": cols,
                            "rows": [list(r) for r in rows[:max_rows]],
                            "truncated": len(rows) > max_rows, "sql": stmt})
        else:
            results.append({"kind": "count", "rowcount": cur.rowcount, "sql": stmt})
    return results


def statement_kind(stmt):
    w = stmt.strip().split(None, 1)[0].upper() if stmt.strip() else ""
    if w in ("CREATE", "DROP", "ALTER"):
        return "DDL"
    if w in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
        return "DML"
    if w in ("SELECT", "WITH"):
        return "QUERY"
    return "OTHER"


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------
def create_view(conn, name, select_sql, replace=False):
    user_ident(name, "view name")
    select_sql = select_sql.strip().rstrip(";")
    if not re.match(r"^\s*(SELECT|WITH)\b", select_sql, re.I):
        raise DesignError("A view must be defined by a SELECT statement.")
    try:
        conn.execute("BEGIN")
        if replace:
            conn.execute(f"DROP VIEW IF EXISTS {q(name)}")
        conn.execute(f"CREATE VIEW {q(name)} AS {select_sql}")
        conn.execute(f"SELECT * FROM {q(name)} LIMIT 0")
        conn.execute("COMMIT")
    except sqlite3.Error as e:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise DesignError(friendly_error(e))


def drop_view(conn, name):
    try:
        conn.execute(f"DROP VIEW {q(name)}")
    except sqlite3.Error as e:
        raise DesignError(str(e))
