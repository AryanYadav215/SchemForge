"""SQL generation for user-designed tables, in several dialects.

The app itself runs on SQLite, but the same table definitions can be exported as
Oracle, MySQL or PostgreSQL scripts, so nothing here is tied to one vendor.
"""
import re

DIALECTS = ("sqlite", "oracle", "mysql", "postgres")
TYPES = ("INTEGER", "VARCHAR", "DECIMAL", "DATE", "TIMESTAMP", "BOOLEAN", "TEXT")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,29}$")
RESERVED = {
    "select", "from", "where", "table", "index", "view", "order", "group", "by", "insert",
    "update", "delete", "create", "drop", "alter", "user", "values", "null", "and", "or",
    "not", "primary", "foreign", "key", "check", "default", "unique", "join", "on", "as",
    "in", "is", "like", "between", "having", "limit", "union", "set", "into", "trigger",
}


def valid_name(name):
    return bool(name) and bool(NAME_RE.match(name)) and name.lower() not in RESERVED \
        and not name.lower().startswith(("sqlite_", "_sf"))


def q(name):
    """Quote an identifier. Names are validated before they ever reach here."""
    return '"%s"' % name.replace('"', '""')


def col_type(col, dialect):
    t, n = col["data_type"], col.get("length")
    if dialect == "oracle":
        return {"INTEGER": "NUMBER(10)", "VARCHAR": "VARCHAR2(%d)" % (n or 100),
                "DECIMAL": "NUMBER(%d,2)" % (n or 10), "DATE": "DATE",
                "TIMESTAMP": "TIMESTAMP", "BOOLEAN": "NUMBER(1)", "TEXT": "CLOB"}[t]
    if dialect == "mysql":
        return {"INTEGER": "INT", "VARCHAR": "VARCHAR(%d)" % (n or 100),
                "DECIMAL": "DECIMAL(%d,2)" % (n or 10), "DATE": "DATE",
                "TIMESTAMP": "DATETIME", "BOOLEAN": "TINYINT(1)", "TEXT": "TEXT"}[t]
    if dialect == "postgres":
        return {"INTEGER": "INTEGER", "VARCHAR": "VARCHAR(%d)" % (n or 100),
                "DECIMAL": "NUMERIC(%d,2)" % (n or 10), "DATE": "DATE",
                "TIMESTAMP": "TIMESTAMP", "BOOLEAN": "BOOLEAN", "TEXT": "TEXT"}[t]
    return {"INTEGER": "INTEGER", "VARCHAR": "VARCHAR(%d)" % (n or 100),
            "DECIMAL": "DECIMAL(%d,2)" % (n or 10), "DATE": "DATE",
            "TIMESTAMP": "TIMESTAMP", "BOOLEAN": "BOOLEAN", "TEXT": "TEXT"}[t]


def default_sql(col):
    d = col.get("default_value")
    if d is None or d == "":
        return None
    up = d.strip().upper()
    if up in ("CURRENT_TIMESTAMP", "CURRENT_DATE", "NULL"):
        return up
    if re.fullmatch(r"-?\d+(\.\d+)?", d.strip()):
        return d.strip()
    return "'%s'" % d.replace("'", "''")


def create_table_sql(table, cols, ref_names, dialect="sqlite"):
    """table: dict(name). cols: ordered list of column dicts.
    ref_names: {ref_table_id: table name} used to render REFERENCES."""
    pks = [c for c in cols if c["is_pk"]]
    lines = []
    single_auto_pk = len(pks) == 1 and pks[0]["is_autoinc"] and pks[0]["data_type"] == "INTEGER"
    for c in cols:
        if dialect == "sqlite" and single_auto_pk and c["is_pk"]:
            lines.append("  %s INTEGER PRIMARY KEY AUTOINCREMENT" % q(c["name"]))
            continue
        parts = [q(c["name"]), col_type(c, dialect)]
        if c["is_autoinc"] and c["is_pk"] and dialect != "sqlite":
            parts.append({"oracle": "GENERATED ALWAYS AS IDENTITY",
                          "mysql": "AUTO_INCREMENT",
                          "postgres": "GENERATED ALWAYS AS IDENTITY"}[dialect])
        d = default_sql(c)
        if d and not (c["is_autoinc"] and c["is_pk"]):
            if dialect == "oracle":
                parts.insert(2, "DEFAULT " + d)
            else:
                parts.append("DEFAULT " + d)
        if c["not_null"] or c["is_pk"]:
            parts.append("NOT NULL")
        if c["is_unique"] and not c["is_pk"]:
            parts.append("UNIQUE")
        if c.get("check_expr"):
            parts.append("CHECK (%s)" % c["check_expr"])
        lines.append("  " + " ".join(parts))
    if pks and not single_auto_pk or (pks and dialect != "sqlite"):
        lines.append("  PRIMARY KEY (%s)" % ", ".join(q(c["name"]) for c in pks))
    for c in cols:
        if c.get("ref_table_id"):
            ref = ref_names.get(c["ref_table_id"])
            if ref:
                od = c.get("on_delete") or "RESTRICT"
                if dialect == "oracle" and od == "RESTRICT":
                    od_sql = ""   # Oracle has no RESTRICT keyword (it is the default)
                else:
                    od_sql = " ON DELETE " + od
                lines.append("  FOREIGN KEY (%s) REFERENCES %s(%s)%s"
                             % (q(c["name"]), q(ref), q(c["ref_column"]), od_sql))
    return "CREATE TABLE %s (\n%s\n)" % (q(table["name"]), ",\n".join(lines))


def index_sql(table_name, col_name):
    return "CREATE INDEX %s ON %s(%s)" % (q("idx_%s_%s" % (table_name, col_name)),
                                          q(table_name), q(col_name))


def audit_trigger_sql(table_name, col_names):
    """SQLite AFTER INSERT/UPDATE/DELETE triggers writing JSON snapshots to _sf_audit."""
    def obj(prefix):
        return "json_object(%s)" % ", ".join("'%s', %s.%s" % (c, prefix, q(c)) for c in col_names)
    t = table_name
    return [
        "CREATE TRIGGER %s AFTER INSERT ON %s BEGIN\n  INSERT INTO _sf_audit(table_name, action, new_data)\n"
        "  VALUES ('%s', 'INSERT', %s);\nEND" % (q("trg_%s_ins" % t), q(t), t, obj("NEW")),
        "CREATE TRIGGER %s AFTER UPDATE ON %s BEGIN\n  INSERT INTO _sf_audit(table_name, action, old_data, new_data)\n"
        "  VALUES ('%s', 'UPDATE', %s, %s);\nEND" % (q("trg_%s_upd" % t), q(t), t, obj("OLD"), obj("NEW")),
        "CREATE TRIGGER %s AFTER DELETE ON %s BEGIN\n  INSERT INTO _sf_audit(table_name, action, old_data)\n"
        "  VALUES ('%s', 'DELETE', %s);\nEND" % (q("trg_%s_del" % t), q(t), t, obj("OLD")),
    ]


AUDIT_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS _sf_audit (\n"
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "  changed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
    "  table_name TEXT NOT NULL, action TEXT NOT NULL,\n"
    "  old_data TEXT, new_data TEXT)"
)


def cursor_report_sql(table_name, num_col, dialect):
    """A small stored-procedure demo (cursor loop) for server databases."""
    t, c = table_name, num_col
    if dialect == "oracle":
        return (
            "CREATE OR REPLACE PROCEDURE sp_report_%(t)s IS\n"
            "  CURSOR cur IS SELECT %(c)s FROM %(t)s;\n"
            "  v_val   %(t)s.%(c)s%%TYPE;\n"
            "  v_total NUMBER := 0;\n"
            "  v_rows  NUMBER := 0;\n"
            "BEGIN\n"
            "  OPEN cur;\n"
            "  LOOP\n"
            "    FETCH cur INTO v_val;\n"
            "    EXIT WHEN cur%%NOTFOUND;\n"
            "    v_total := v_total + NVL(v_val, 0);\n"
            "    v_rows  := v_rows + 1;\n"
            "  END LOOP;\n"
            "  CLOSE cur;\n"
            "  DBMS_OUTPUT.PUT_LINE('%(t)s: ' || v_rows || ' rows, total %(c)s = ' || v_total);\n"
            "END;\n/" % {"t": t, "c": c})
    if dialect == "postgres":
        return (
            "CREATE OR REPLACE PROCEDURE sp_report_%(t)s() LANGUAGE plpgsql AS $$\n"
            "DECLARE\n"
            "  cur CURSOR FOR SELECT %(c)s FROM %(t)s;\n"
            "  v_val %(t)s.%(c)s%%TYPE;\n"
            "  v_total NUMERIC := 0;\n"
            "  v_rows  INTEGER := 0;\n"
            "BEGIN\n"
            "  OPEN cur;\n"
            "  LOOP\n"
            "    FETCH cur INTO v_val;\n"
            "    EXIT WHEN NOT FOUND;\n"
            "    v_total := v_total + COALESCE(v_val, 0);\n"
            "    v_rows  := v_rows + 1;\n"
            "  END LOOP;\n"
            "  CLOSE cur;\n"
            "  RAISE NOTICE '%(t)s: % rows, total %(c)s = %', v_rows, v_total;\n"
            "END $$;" % {"t": t, "c": c})
    if dialect == "mysql":
        return (
            "DELIMITER //\n"
            "CREATE PROCEDURE sp_report_%(t)s()\n"
            "BEGIN\n"
            "  DECLARE done INT DEFAULT 0;\n"
            "  DECLARE v_val DECIMAL(18,2);\n"
            "  DECLARE v_total DECIMAL(18,2) DEFAULT 0;\n"
            "  DECLARE v_rows INT DEFAULT 0;\n"
            "  DECLARE cur CURSOR FOR SELECT %(c)s FROM %(t)s;\n"
            "  DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;\n"
            "  OPEN cur;\n"
            "  read_loop: LOOP\n"
            "    FETCH cur INTO v_val;\n"
            "    IF done = 1 THEN LEAVE read_loop; END IF;\n"
            "    SET v_total = v_total + IFNULL(v_val, 0);\n"
            "    SET v_rows = v_rows + 1;\n"
            "  END LOOP;\n"
            "  CLOSE cur;\n"
            "  SELECT v_rows AS row_count, v_total AS total_%(c)s;\n"
            "END //\n"
            "DELIMITER ;" % {"t": t, "c": c})
    return None
