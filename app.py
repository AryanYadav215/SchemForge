"""SchemaForge - build your own database without writing SQL.

Run:  python app.py      then open http://127.0.0.1:5000
"""
import os
import re
import secrets
import shutil
import sqlite3
from functools import wraps

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import db
import engine as E
import exporter
import normalize
import querybuilder as QB

app = Flask(__name__)
os.makedirs(db.DATA_DIR, exist_ok=True)
_key_file = os.path.join(db.DATA_DIR, "secret.key")
if not os.path.exists(_key_file):
    with open(_key_file, "w") as f:
        f.write(secrets.token_hex(32))
with open(_key_file) as _f:
    app.secret_key = _f.read().strip()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                  MAX_CONTENT_LENGTH=5 * 1024 * 1024)
db.init()


# --------------------------------------------------------------------------
# request helpers
# --------------------------------------------------------------------------
def pdb():
    if "pdb" not in g:
        g.pdb = db.connect()
    return g.pdb


@app.teardown_appcontext
def close_pdb(_):
    c = g.pop("pdb", None)
    if c:
        c.close()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def csrf_guard():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.headers.get("X-CSRF") or request.form.get("csrf")
        if not sent or sent != session.get("csrf"):
            if request.path.startswith("/api/"):
                return jsonify(error="Session expired - reload the page."), 400
            abort(400)


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return pdb().execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def api(fn):
    """JSON API wrapper: login required, DesignError -> 400 JSON."""
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("uid"):
            return jsonify(error="Please sign in."), 401
        try:
            return fn(*a, **kw)
        except E.DesignError as e:
            return jsonify(error=str(e)), 400
        except sqlite3.Error as e:
            return jsonify(error=E.friendly_error(e)), 400
    return wrapper


def access(project_id):
    """Return (project_row, is_owner, grants set) or abort 404."""
    uid = session["uid"]
    p = pdb().execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if not p:
        abort(404)
    if p["owner_id"] == uid:
        return p, True, set()
    m = pdb().execute("SELECT 1 FROM project_members WHERE project_id=? AND user_id=?",
                      (project_id, uid)).fetchone()
    if not m:
        abort(404)
    rows = pdb().execute("SELECT table_name, privilege FROM grants WHERE project_id=? AND user_id=?",
                         (project_id, uid)).fetchall()
    return p, False, {(r["table_name"].lower(), r["privilege"]) for r in rows}


def owner_only(project_id):
    p, is_owner, grants = access(project_id)
    if not is_owner:
        raise E.DesignError("Only the project owner can do this.")
    return p


def log(project_id, kind, statements):
    if isinstance(statements, str):
        statements = [statements]
    for s in statements:
        pdb().execute("INSERT INTO activity_log(project_id, user_id, kind, statement) VALUES (?,?,?,?)",
                      (project_id, session["uid"], kind, s[:4000]))
    pdb().commit()


class UserConn:
    """Context manager: the user's open transaction connection if any, else a
    fresh authorised connection that is closed afterwards."""

    def __init__(self, project_id, allow_ddl=False):
        self.pid = project_id
        self.key = (project_id, session["uid"])
        self.allow_ddl = allow_ddl

    def __enter__(self):
        tx = E.tx_get(self.key)
        if tx:
            self.tx = True
            tx.touched = __import__("time").time()
            return tx.conn
        self.tx = False
        p, is_owner, grants = access(self.pid)
        self.conn = E.user_conn(self.pid, is_owner, grants, self.allow_ddl)
        return self.conn

    def __exit__(self, *exc):
        if not self.tx:
            self.conn.close()


def designer(project_id):
    owner_only(project_id)
    return E.open_project(project_id)


# --------------------------------------------------------------------------
# pages + auth
# --------------------------------------------------------------------------
@app.route("/")
def home():
    return redirect(url_for("dashboard") if session.get("uid") else url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    err = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", u):
            err = "Username: 3-30 letters, digits or underscores."
        elif len(pw) < 6:
            err = "Password must be at least 6 characters."
        else:
            try:
                cur = pdb().execute("INSERT INTO users(username, pw_hash) VALUES (?,?)",
                                    (u, generate_password_hash(pw)))
                pdb().commit()
                session.clear()
                session["uid"] = cur.lastrowid
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError:
                err = "That username is taken."
    return render_template("auth.html", mode="register", error=err)


@app.route("/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        row = pdb().execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        if row and check_password_hash(row["pw_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = row["user_id"]
            return redirect(url_for("dashboard"))
        err = "Wrong username or password."
    return render_template("auth.html", mode="login", error=err)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    return render_template("dashboard.html", user=user)


@app.route("/p/<int:pid>")
def workspace(pid):
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    p, is_owner, grants = access(pid)
    return render_template("workspace.html", user=user, project=p, is_owner=is_owner)


# --------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------
@app.route("/api/projects", methods=["GET"])
@api
def list_projects():
    uid = session["uid"]
    mine = pdb().execute("SELECT project_id, name, description, created_at FROM projects "
                         "WHERE owner_id=? ORDER BY created_at DESC", (uid,)).fetchall()
    shared = pdb().execute(
        "SELECT p.project_id, p.name, p.description, u.username AS owner FROM project_members m "
        "JOIN projects p ON p.project_id = m.project_id JOIN users u ON u.user_id = p.owner_id "
        "WHERE m.user_id=? ORDER BY p.name", (uid,)).fetchall()
    out = []
    for r in mine:
        d = dict(r)
        try:
            c = E.open_project(r["project_id"])
            d["tables"] = c.execute("SELECT COUNT(*) FROM _sf_tables").fetchone()[0]
            c.close()
        except Exception:
            d["tables"] = 0
        out.append(d)
    return jsonify(mine=out, shared=[dict(r) for r in shared])


@app.route("/api/projects", methods=["POST"])
@api
def create_project():
    d = (request.get_json(silent=True) or {})
    name = (d.get("name") or "").strip()
    if not 1 <= len(name) <= 60:
        raise E.DesignError("Give the database a name (up to 60 characters).")
    try:
        cur = pdb().execute("INSERT INTO projects(owner_id, name, description) VALUES (?,?,?)",
                            (session["uid"], name, (d.get("description") or "")[:300]))
        pdb().commit()
    except sqlite3.IntegrityError:
        raise E.DesignError("You already have a database with that name.")
    E.open_project(cur.lastrowid).close()
    return jsonify(project_id=cur.lastrowid)


@app.route("/api/projects/<int:pid>", methods=["DELETE"])
@api
def delete_project(pid):
    owner_only(pid)
    for (p_, u_) in list(E._TX):
        if p_ == pid:
            E.tx_discard((p_, u_))
    pdb().execute("DELETE FROM projects WHERE project_id=?", (pid,))
    pdb().commit()
    path = db.project_path(pid)
    for suffix in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# schema + designer (owner only for changes)
# --------------------------------------------------------------------------
@app.route("/api/p/<int:pid>/schema")
@api
def get_schema(pid):
    p, is_owner, grants = access(pid)
    c = E.open_project(pid)
    try:
        if is_owner:
            s = E.schema_json(c)
            s["can"] = {t["name"]: E.PRIVS for t in s["tables"]}
            s["can"].update({v["name"]: ["SELECT"] for v in s["views"]})
        else:
            specs = E.load_specs(c)
            views = [dict(name=r["name"], sql=None) for r in
                     c.execute("SELECT name FROM sqlite_master WHERE type='view'")]
            can = {}
            for (t, priv) in grants:
                can.setdefault(t, []).append(priv)
            s = {"tables": [dict(sp, rows=None) for n, sp in specs.items() if n.lower() in can],
                 "views": [v for v in views if v["name"].lower() in can],
                 "can": {n: can[n.lower()] for n in list(specs) + [v["name"] for v in views] if n.lower() in can}}
        s["is_owner"] = is_owner
        s["project"] = {"id": p["project_id"], "name": p["name"]}
        s["tx"] = bool(E.tx_get((pid, session["uid"])))
        return jsonify(s)
    finally:
        c.close()


def ddl_response(pid, stmts, warnings=None):
    log(pid, "DDL", stmts)
    return jsonify(ok=True, sql=stmts, warnings=warnings or [])


@app.route("/api/p/<int:pid>/tables", methods=["POST"])
@api
def api_create_table(pid):
    c = designer(pid)
    try:
        return ddl_response(pid, E.create_table(c, request.get_json(silent=True)))
    finally:
        c.close()


@app.route("/api/p/<int:pid>/tables/<t>", methods=["DELETE"])
@api
def api_drop_table(pid, t):
    c = designer(pid)
    try:
        stmts = E.drop_table(c, t)
    finally:
        c.close()
    pdb().execute("DELETE FROM grants WHERE project_id=? AND lower(table_name)=lower(?)", (pid, t))
    pdb().execute("DELETE FROM functional_deps WHERE project_id=? AND table_name=?", (pid, t))
    pdb().commit()
    return ddl_response(pid, stmts)


@app.route("/api/p/<int:pid>/tables/<t>/rename", methods=["POST"])
@api
def api_rename_table(pid, t):
    new = ((request.get_json(silent=True) or {})).get("name", "")
    c = designer(pid)
    try:
        stmts = E.rename_table(c, t, new)
    finally:
        c.close()
    pdb().execute("UPDATE grants SET table_name=? WHERE project_id=? AND lower(table_name)=lower(?)", (new, pid, t))
    pdb().execute("UPDATE functional_deps SET table_name=? WHERE project_id=? AND table_name=?", (new, pid, t))
    pdb().commit()
    return ddl_response(pid, stmts)


@app.route("/api/p/<int:pid>/tables/<t>/columns", methods=["POST"])
@api
def api_add_column(pid, t):
    c = designer(pid)
    try:
        stmts, warn = E.add_column(c, t, request.get_json(silent=True))
        return ddl_response(pid, stmts, warn)
    finally:
        c.close()


@app.route("/api/p/<int:pid>/tables/<t>/columns/<col>", methods=["PUT", "DELETE"])
@api
def api_change_column(pid, t, col):
    c = designer(pid)
    try:
        if request.method == "DELETE":
            stmts, warn = E.drop_column(c, t, col)
        else:
            stmts, warn = E.modify_column(c, t, col, request.get_json(silent=True))
        return ddl_response(pid, stmts, warn)
    finally:
        c.close()


@app.route("/api/p/<int:pid>/tables/<t>/indexes", methods=["POST"])
@api
def api_create_index(pid, t):
    d = (request.get_json(silent=True) or {})
    c = designer(pid)
    try:
        return ddl_response(pid, E.create_index(c, t, d.get("name", ""), d.get("columns") or [], d.get("unique")))
    finally:
        c.close()


@app.route("/api/p/<int:pid>/tables/<t>/indexes/<name>", methods=["DELETE"])
@api
def api_drop_index(pid, t, name):
    c = designer(pid)
    try:
        E.drop_index(c, t, name)
        return ddl_response(pid, [f'DROP INDEX "{name}"'])
    finally:
        c.close()


@app.route("/api/p/<int:pid>/tables/<t>/audit", methods=["POST"])
@api
def api_audit(pid, t):
    on = bool(((request.get_json(silent=True) or {})).get("on"))
    c = designer(pid)
    try:
        E.set_audit(c, t, on)
        return ddl_response(pid, [f"-- audit triggers {'created' if on else 'dropped'} on {t}"])
    finally:
        c.close()


@app.route("/api/p/<int:pid>/relationships", methods=["POST"])
@api
def api_relationship(pid):
    d = (request.get_json(silent=True) or {})
    c = designer(pid)
    try:
        res = E.add_relationship(c, d.get("kind"), d.get("parent"), d.get("child"),
                                 d.get("on_delete", "NO ACTION"), d.get("junction") or None)
        stmts, warn = res if isinstance(res, tuple) else (res, [])
        return ddl_response(pid, stmts, warn)
    finally:
        c.close()


@app.route("/api/p/<int:pid>/audit")
@api
def api_audit_log(pid):
    c = designer(pid)
    try:
        t = request.args.get("table")
        sql = "SELECT * FROM _audit_log"
        params = []
        if t:
            sql += " WHERE table_name = ?"
            params.append(t)
        sql += " ORDER BY audit_id DESC LIMIT 200"
        return jsonify(rows=[dict(r) for r in c.execute(sql, params)])
    finally:
        c.close()


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
@app.route("/api/p/<int:pid>/rows/<t>", methods=["GET"])
@api
def api_rows(pid, t):
    with UserConn(pid) as c:
        r = E.list_rows(c, t, request.args.get("page", 1), request.args.get("size", 25),
                        request.args.get("sort"), request.args.get("dir", "asc"),
                        request.args.get("q", "").strip())
        return jsonify(r)


@app.route("/api/p/<int:pid>/rows/<t>", methods=["POST"])
@api
def api_insert(pid, t):
    with UserConn(pid) as c:
        rid, sql = E.insert_row(c, t, (request.get_json(silent=True) or {}))
    log(pid, "DML", sql)
    return jsonify(ok=True, rowid=rid, sql=sql)


@app.route("/api/p/<int:pid>/rows/<t>/<int:rid>", methods=["PUT"])
@api
def api_update(pid, t, rid):
    with UserConn(pid) as c:
        n, sql = E.update_row(c, t, rid, (request.get_json(silent=True) or {}))
    log(pid, "DML", sql)
    return jsonify(ok=True, updated=n, sql=sql)


@app.route("/api/p/<int:pid>/rows/<t>/<int:rid>", methods=["DELETE"])
@api
def api_delete(pid, t, rid):
    with UserConn(pid) as c:
        n, sql = E.delete_row(c, t, rid)
    log(pid, "DML", sql)
    return jsonify(ok=True, deleted=n, sql=sql)


@app.route("/api/p/<int:pid>/fk/<t>/<col>")
@api
def api_fk(pid, t, col):
    with UserConn(pid) as c:
        return jsonify(options=E.fk_options(c, t, col))


@app.route("/api/p/<int:pid>/import/<t>", methods=["POST"])
@api
def api_import(pid, t):
    f = request.files.get("file")
    text = f.read().decode("utf-8-sig", "replace") if f else ((request.get_json(silent=True) or {})).get("csv", "")
    with UserConn(pid) as c:
        n = E.import_csv(c, t, text)
    log(pid, "DML", f"-- CSV import: {n} row(s) into {t}")
    return jsonify(ok=True, inserted=n)


@app.route("/api/p/<int:pid>/view-rows/<name>")
@api
def api_view_rows(pid, name):
    E.ident(name)
    with UserConn(pid) as c:
        res = E.run_sql(c, f'SELECT * FROM "{name}" LIMIT 200')[0]
        return jsonify(res)


# --------------------------------------------------------------------------
# queries: builder, console, views, routines
# --------------------------------------------------------------------------
def _specs(pid):
    c = E.open_project(pid)
    try:
        return E.load_specs(c)
    finally:
        c.close()


@app.route("/api/p/<int:pid>/query", methods=["POST"])
@api
def api_query(pid):
    access(pid)
    sql, params = QB.build_select((request.get_json(silent=True) or {}), _specs(pid))
    shown = QB.inline_params(sql, params)
    with UserConn(pid) as c:
        cur = c.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(1001)
    log(pid, "QUERY", shown)
    return jsonify(sql=shown, columns=cols, rows=[list(r) for r in rows[:1000]], truncated=len(rows) > 1000)


@app.route("/api/p/<int:pid>/sql", methods=["POST"])
@api
def api_sql(pid):
    text = ((request.get_json(silent=True) or {})).get("sql", "")
    if not text.strip():
        raise E.DesignError("Type a statement first.")
    results = []
    with UserConn(pid) as c:
        in_tx = bool(c.in_transaction)
        try:
            if not in_tx:
                c.execute("BEGIN")           # a script without BEGIN is still all-or-nothing
            results = E.run_sql(c, text)
            if not in_tx:
                c.execute("COMMIT")
        except Exception:
            if not in_tx and c.in_transaction:
                c.execute("ROLLBACK")
            raise
    for r in results:
        log(pid, E.statement_kind(r["sql"]), r["sql"])
    return jsonify(results=results, in_tx=in_tx)


@app.route("/api/p/<int:pid>/views", methods=["POST"])
@api
def api_create_view(pid):
    d = (request.get_json(silent=True) or {})
    c = designer(pid)
    try:
        E.create_view(c, d.get("name", ""), d.get("sql", ""), bool(d.get("replace")))
    finally:
        c.close()
    log(pid, "DDL", f'CREATE VIEW "{d["name"]}" AS {d["sql"]}')
    return jsonify(ok=True)


@app.route("/api/p/<int:pid>/views/<name>", methods=["DELETE"])
@api
def api_drop_view(pid, name):
    c = designer(pid)
    try:
        E.drop_view(c, name)
    finally:
        c.close()
    pdb().execute("DELETE FROM grants WHERE project_id=? AND lower(table_name)=lower(?)", (pid, name))
    pdb().commit()
    log(pid, "DDL", f'DROP VIEW "{name}"')
    return jsonify(ok=True)


PARAM_RE = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


@app.route("/api/p/<int:pid>/routines", methods=["GET"])
@api
def api_routines(pid):
    access(pid)
    rows = pdb().execute("SELECT name, body FROM routines WHERE project_id=? ORDER BY name", (pid,)).fetchall()
    return jsonify(routines=[dict(name=r["name"], body=r["body"],
                                  params=list(dict.fromkeys(PARAM_RE.findall(r["body"])))) for r in rows])


@app.route("/api/p/<int:pid>/routines", methods=["POST"])
@api
def api_save_routine(pid):
    owner_only(pid)
    d = (request.get_json(silent=True) or {})
    name = E.user_ident(d.get("name", ""), "routine name")
    body = (d.get("body") or "").strip()
    if not body:
        raise E.DesignError("A routine needs at least one SQL statement.")
    pdb().execute("INSERT INTO routines(project_id, name, body) VALUES (?,?,?) "
                  "ON CONFLICT(project_id, name) DO UPDATE SET body=excluded.body", (pid, name, body))
    pdb().commit()
    return jsonify(ok=True)


@app.route("/api/p/<int:pid>/routines/<name>", methods=["DELETE"])
@api
def api_delete_routine(pid, name):
    owner_only(pid)
    pdb().execute("DELETE FROM routines WHERE project_id=? AND name=?", (pid, name))
    pdb().commit()
    return jsonify(ok=True)


@app.route("/api/p/<int:pid>/routines/<name>/call", methods=["POST"])
@api
def api_call_routine(pid, name):
    access(pid)
    r = pdb().execute("SELECT body FROM routines WHERE project_id=? AND name=?", (pid, name)).fetchone()
    if not r:
        raise E.DesignError("No such routine.")
    names = list(dict.fromkeys(PARAM_RE.findall(r["body"])))
    given = ((request.get_json(silent=True) or {})).get("params", {})
    params = {n: QB._scalar(given.get(n)) for n in names}
    with UserConn(pid) as c:
        nested = c.in_transaction
        c.execute("SAVEPOINT routine_call" if nested else "BEGIN")   # a routine is atomic
        try:
            results = E.run_sql(c, r["body"], params)
            c.execute("RELEASE routine_call" if nested else "COMMIT")
        except Exception:
            if nested:
                c.execute("ROLLBACK TO routine_call")
                c.execute("RELEASE routine_call")
            elif c.in_transaction:
                c.execute("ROLLBACK")
            raise
    log(pid, "CALL", f"CALL {name}({', '.join(f'{k}={v!r}' for k, v in params.items())})")
    return jsonify(results=results)


# --------------------------------------------------------------------------
# transactions (TCL)
# --------------------------------------------------------------------------
def txkey(pid):
    return (pid, session["uid"])


def tx_state(pid):
    tx = E.tx_get(txkey(pid))
    return {"open": bool(tx), "savepoints": tx.savepoints if tx else []}


@app.route("/api/p/<int:pid>/tx", methods=["GET"])
@api
def api_tx_status(pid):
    access(pid)
    return jsonify(tx_state(pid))


@app.route("/api/p/<int:pid>/tx/<action>", methods=["POST"])
@api
def api_tx(pid, action):
    p, is_owner, grants = access(pid)
    key = txkey(pid)
    name = ((request.get_json(silent=True) or {})).get("name", "")
    if action == "begin":
        E.tx_begin(key, E.user_conn(pid, is_owner, grants))
        log(pid, "TCL", "BEGIN")
    elif action == "commit":
        E.tx_end(key, True)
        log(pid, "TCL", "COMMIT")
    elif action == "rollback":
        E.tx_end(key, False)
        log(pid, "TCL", "ROLLBACK")
    elif action == "savepoint":
        E.tx_savepoint(key, name)
        log(pid, "TCL", f"SAVEPOINT {name}")
    elif action == "rollback_to":
        E.tx_rollback_to(key, name)
        log(pid, "TCL", f"ROLLBACK TO {name}")
    else:
        abort(404)
    return jsonify(tx_state(pid))


# --------------------------------------------------------------------------
# access control: members, GRANT, REVOKE
# --------------------------------------------------------------------------
@app.route("/api/p/<int:pid>/access")
@api
def api_access(pid):
    owner_only(pid)
    members = pdb().execute("SELECT u.user_id, u.username FROM project_members m JOIN users u "
                            "ON u.user_id=m.user_id WHERE m.project_id=? ORDER BY u.username", (pid,)).fetchall()
    grants = pdb().execute("SELECT u.username, g.table_name, g.privilege FROM grants g JOIN users u "
                           "ON u.user_id=g.user_id WHERE g.project_id=? ORDER BY u.username, g.table_name",
                           (pid,)).fetchall()
    return jsonify(members=[dict(r) for r in members], grants=[dict(r) for r in grants])


@app.route("/api/p/<int:pid>/members", methods=["POST"])
@api
def api_add_member(pid):
    p = owner_only(pid)
    u = pdb().execute("SELECT user_id FROM users WHERE username=?",
                      (((request.get_json(silent=True) or {})).get("username", "").strip(),)).fetchone()
    if not u:
        raise E.DesignError("No user with that username (they must register first).")
    if u["user_id"] == p["owner_id"]:
        raise E.DesignError("That is you - the owner already has every privilege.")
    pdb().execute("INSERT OR IGNORE INTO project_members(project_id, user_id) VALUES (?,?)", (pid, u["user_id"]))
    pdb().commit()
    return jsonify(ok=True)


@app.route("/api/p/<int:pid>/members/<username>", methods=["DELETE"])
@api
def api_remove_member(pid, username):
    owner_only(pid)
    pdb().execute("DELETE FROM project_members WHERE project_id=? AND user_id="
                  "(SELECT user_id FROM users WHERE username=?)", (pid, username))
    pdb().commit()
    return jsonify(ok=True)


def _objects(pid):
    c = E.open_project(pid)
    try:
        names = list(E.load_specs(c))
        names += [r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='view'")]
        return names
    finally:
        c.close()


@app.route("/api/p/<int:pid>/grant", methods=["POST"])
@api
def api_grant(pid):
    owner_only(pid)
    d = (request.get_json(silent=True) or {})
    revoke = bool(d.get("revoke"))
    table = d.get("table")
    if table not in _objects(pid):
        raise E.DesignError("Pick an existing table or view.")
    privs = E.PRIVS if d.get("privilege") == "ALL" else [d.get("privilege")]
    if any(p not in E.PRIVS for p in privs):
        raise E.DesignError("Privilege must be SELECT, INSERT, UPDATE, DELETE or ALL.")
    u = pdb().execute("SELECT user_id FROM users WHERE username=?", (d.get("username"),)).fetchone()
    if not u:
        raise E.DesignError("Unknown user.")
    if not pdb().execute("SELECT 1 FROM project_members WHERE project_id=? AND user_id=?",
                         (pid, u["user_id"])).fetchone():
        raise E.DesignError("Add that user as a member first.")
    for pr in privs:
        if revoke:
            pdb().execute("DELETE FROM grants WHERE project_id=? AND user_id=? AND table_name=? AND privilege=?",
                          (pid, u["user_id"], table, pr))
        else:
            pdb().execute("INSERT OR IGNORE INTO grants(project_id, user_id, table_name, privilege, granted_by) "
                          "VALUES (?,?,?,?,?)", (pid, u["user_id"], table, pr, session["uid"]))
    pdb().commit()
    verb = "REVOKE" if revoke else "GRANT"
    prep = "FROM" if revoke else "TO"
    log(pid, "DCL", f'{verb} {", ".join(privs)} ON "{table}" {prep} {d.get("username")}')
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------
@app.route("/api/p/<int:pid>/fds/<t>", methods=["GET"])
@api
def api_fds(pid, t):
    owner_only(pid)
    rows = pdb().execute("SELECT fd_id, lhs, rhs FROM functional_deps WHERE project_id=? AND table_name=?",
                         (pid, t)).fetchall()
    return jsonify(fds=[dict(r) for r in rows])


@app.route("/api/p/<int:pid>/fds/<t>", methods=["POST"])
@api
def api_add_fd(pid, t):
    owner_only(pid)
    d = (request.get_json(silent=True) or {})
    specs = _specs(pid)
    if t not in specs:
        raise E.DesignError("No such table.")
    names = {c["name"] for c in specs[t]["columns"]}
    L, R = normalize.parse_fd_side(d.get("lhs", "")), normalize.parse_fd_side(d.get("rhs", ""))
    if not L or not R:
        raise E.DesignError("Both sides of a dependency need at least one column.")
    bad = (L | R) - names
    if bad:
        raise E.DesignError("Unknown column(s): " + ", ".join(sorted(bad)))
    pdb().execute("INSERT INTO functional_deps(project_id, table_name, lhs, rhs) VALUES (?,?,?,?)",
                  (pid, t, ",".join(sorted(L)), ",".join(sorted(R))))
    pdb().commit()
    return jsonify(ok=True)


@app.route("/api/p/<int:pid>/fds/<t>/<int:fid>", methods=["DELETE"])
@api
def api_del_fd(pid, t, fid):
    owner_only(pid)
    pdb().execute("DELETE FROM functional_deps WHERE project_id=? AND fd_id=?", (pid, fid))
    pdb().commit()
    return jsonify(ok=True)


@app.route("/api/p/<int:pid>/normalize/<t>")
@api
def api_normalize(pid, t):
    owner_only(pid)
    specs = _specs(pid)
    if t not in specs:
        raise E.DesignError("No such table.")
    cols = [c["name"] for c in specs[t]["columns"]]
    rows = pdb().execute("SELECT lhs, rhs FROM functional_deps WHERE project_id=? AND table_name=?",
                         (pid, t)).fetchall()
    try:
        res = normalize.analyse(cols, [(r["lhs"], r["rhs"]) for r in rows])
    except ValueError as e:
        raise E.DesignError(str(e))
    res["repeating_groups"] = normalize.repeating_group_hint(cols)
    res["migration_3nf"] = normalize.migration_sql(t, res["decomposition_3nf"], specs[t]["columns"])
    res["migration_bcnf"] = normalize.migration_sql(t, res["decomposition_bcnf"], specs[t]["columns"])
    res["declared_key"] = [c["name"] for c in specs[t]["columns"] if c["pk"]]
    return jsonify(res)


# --------------------------------------------------------------------------
# export, activity
# --------------------------------------------------------------------------
@app.route("/api/p/<int:pid>/export")
@api
def api_export(pid):
    p = owner_only(pid)
    dialect = request.args.get("dialect", "sqlite")
    if dialect not in exporter.DIALECTS:
        raise E.DesignError("Unknown dialect.")
    c = E.open_project(pid)
    try:
        specs = E.load_specs(c)
        include_data = request.args.get("data", "1") == "1"
        data = {t: [dict(r) for r in c.execute(f'SELECT * FROM "{t}"')] for t in specs} if include_data else {}
        views = [dict(sql=r["sql"]) for r in c.execute("SELECT sql FROM sqlite_master WHERE type='view'")]
    finally:
        c.close()
    gr = pdb().execute("SELECT u.username, g.table_name, g.privilege FROM grants g JOIN users u "
                       "ON u.user_id=g.user_id WHERE g.project_id=?", (pid,)).fetchall()
    text = exporter.export_sql(specs, data, dialect, views=views,
                               grants=[(r["username"], r["table_name"], r["privilege"]) for r in gr],
                               extras=request.args.get("extras", "1") == "1", include_data=include_data)
    if request.args.get("download") == "1":
        fn = re.sub(r"\W+", "_", p["name"]) + f"_{dialect}.sql"
        return Response(text, mimetype="text/plain",
                        headers={"Content-Disposition": f'attachment; filename="{fn}"'})
    return jsonify(sql=text)


@app.route("/api/p/<int:pid>/dbfile")
def api_dbfile(pid):
    if not session.get("uid"):
        abort(401)
    p, is_owner, _ = access(pid)
    if not is_owner:
        abort(403)
    c = E.open_project(pid)
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    dst = sqlite3.connect(tmp.name)
    c.backup(dst)
    dst.close()
    c.close()
    with open(tmp.name, "rb") as fh:
        data = fh.read()
    os.remove(tmp.name)
    fn = re.sub(r"\W+", "_", p["name"]) + ".sqlite"
    return Response(data, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.route("/api/p/<int:pid>/activity")
@api
def api_activity(pid):
    owner_only(pid)
    rows = pdb().execute("SELECT l.kind, l.statement, l.logged_at, u.username FROM activity_log l "
                         "JOIN users u ON u.user_id=l.user_id WHERE l.project_id=? "
                         "ORDER BY l.log_id DESC LIMIT 200", (pid,)).fetchall()
    return jsonify(rows=[dict(r) for r in rows])


@app.route("/api/meta")
def api_meta():
    return jsonify(types=E.TYPES, fk_actions=E.FK_ACTIONS, check_ops=E.CHECK_OPS, privs=E.PRIVS,
                   dialects=exporter.DIALECTS)


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("DEBUG") == "1")
