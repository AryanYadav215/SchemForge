"""Visual query builder: turns a JSON description into a parameterised SELECT.

Supports projection, DISTINCT, aggregates, WHERE (AND/OR), BETWEEN, LIKE, IN,
INNER / LEFT / RIGHT / FULL / CROSS / SELF joins, GROUP BY, HAVING, ORDER BY,
LIMIT, plus single-row, multi-row (IN) and correlated subqueries.
Every identifier is validated against the project's real schema and every
value is bound as a parameter, so the builder cannot be used for injection.
"""
import re

from engine import DesignError, q, ident

AGGS = {"": None, "COUNT": "COUNT", "SUM": "SUM", "AVG": "AVG", "MIN": "MIN", "MAX": "MAX",
        "COUNT_DISTINCT": "COUNT"}
CMP = {"=", "!=", "<", ">", "<=", ">="}
JOINS = {"INNER", "LEFT", "RIGHT", "FULL", "CROSS"}


def _col_ref(ref, amap, allow_star=False):
    if allow_star and ref in ("*", ""):
        return "*"
    if not isinstance(ref, str) or "." not in ref:
        raise DesignError(f"Column reference '{ref}' must look like alias.column.")
    alias, col = ref.split(".", 1)
    if alias not in amap:
        raise DesignError(f"Unknown table alias '{alias}'.")
    if allow_star and col == "*":
        return f"{q(alias)}.*"
    if col not in [c["name"] for c in amap[alias]["columns"]]:
        raise DesignError(f"Table '{amap[alias]['name']}' has no column '{col}'.")
    return f"{q(alias)}.{q(col)}"


def _agg_expr(agg, col_sql):
    if agg not in AGGS:
        raise DesignError(f"Unknown aggregate '{agg}'.")
    if not agg:
        return col_sql
    if agg == "COUNT_DISTINCT":
        return f"COUNT(DISTINCT {col_sql})"
    return f"{AGGS[agg]}({col_sql})"


_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def _scalar(v):
    """Bind numeric-looking text as a number (SQLite orders every number before any text)."""
    if v in ("", None):
        return None
    if isinstance(v, str) and _NUM.match(v.strip()):
        v = v.strip()
        return float(v) if "." in v else int(v)
    return v


def _condition(c, amap, specs, params, depth):
    op = c.get("op", "=")
    col = _col_ref(c.get("col"), amap)
    if op in CMP or op in ("LIKE", "NOT LIKE"):
        params.append(_scalar(c.get("value")))
        return f"{col} {op} ?"
    if op in ("IS NULL", "IS NOT NULL"):
        return f"{col} {op}"
    if op == "BETWEEN":
        params += [_scalar(c.get("value")), _scalar(c.get("value2"))]
        return f"{col} BETWEEN ? AND ?"
    if op in ("IN", "NOT IN"):
        vals = [v.strip() for v in str(c.get("value", "")).split(",") if v.strip()]
        if not vals:
            raise DesignError("IN needs a comma-separated list.")
        params.extend(_scalar(v) for v in vals)
        return f"{col} {op} ({', '.join('?' for _ in vals)})"
    if op in ("IN_SUB", "NOT_IN_SUB", "CMP_SUB", "EXISTS_SUB", "NOT_EXISTS_SUB"):
        if depth >= 2:
            raise DesignError("Subqueries can be nested only two levels deep.")
        sub = c.get("sub") or {}
        sql = _subquery(sub, amap, specs, params, depth + 1, need_column=op not in ("EXISTS_SUB", "NOT_EXISTS_SUB"))
        if op == "IN_SUB":
            return f"{col} IN ({sql})"
        if op == "NOT_IN_SUB":
            return f"{col} NOT IN ({sql})"
        if op == "EXISTS_SUB":
            return f"EXISTS ({sql})"
        if op == "NOT_EXISTS_SUB":
            return f"NOT EXISTS ({sql})"
        cmp_ = c.get("cmp", "=")
        if cmp_ not in CMP:
            raise DesignError("Bad comparison operator for subquery.")
        return f"{col} {cmp_} ({sql})"
    raise DesignError(f"Unsupported operator '{op}'.")


def _where_sql(conds, amap, specs, params, depth):
    parts = []
    for i, c in enumerate(conds):
        conj = "AND" if c.get("conj", "AND") != "OR" else "OR"
        sql = _condition(c, amap, specs, params, depth)
        parts.append((conj if i else "", sql))
    return " ".join(f"{cj} {s}".strip() for cj, s in parts)


def _subquery(sub, outer_amap, specs, params, depth, need_column=True):
    table = sub.get("table")
    if table not in specs:
        raise DesignError("Subquery needs a table.")
    alias = f"s{depth}"
    amap = {alias: specs[table]}
    if need_column:
        colname = sub.get("column")
        if colname not in [c["name"] for c in specs[table]["columns"]]:
            raise DesignError("Subquery column is not in that table.")
        sel = _agg_expr(sub.get("agg", ""), f"{q(alias)}.{q(colname)}")
    else:
        sel = "1"
    sql = f"SELECT {sel} FROM {q(table)} AS {q(alias)}"
    conds = []
    # outer-scope references allowed for correlation only through 'correlate'
    wh = []
    corr = sub.get("correlate")
    if corr:
        inner = corr.get("inner")
        if inner not in [c["name"] for c in specs[table]["columns"]]:
            raise DesignError("Correlation column missing in subquery table.")
        outer = _col_ref(corr.get("outer"), outer_amap)
        wh.append(f"{q(alias)}.{q(inner)} = {outer}")
    for c in sub.get("where") or []:
        c = dict(c)
        c["col"] = f"{alias}.{c.get('col')}"
        conds.append(c)
    if conds:
        wh.append("(" + _where_sql(conds, amap, specs, params, depth) + ")")
    if wh:
        sql += " WHERE " + " AND ".join(wh)
    return sql


def build_select(spec, specs):
    params = []
    base = spec.get("table")
    if base not in specs:
        raise DesignError("Pick a table to query.")
    base_alias = spec.get("alias") or base
    ident(base_alias, "alias")
    amap = {base_alias: specs[base]}
    from_sql = f"{q(base)} AS {q(base_alias)}"
    for j in spec.get("joins") or []:
        jt = j.get("type", "INNER")
        if jt not in JOINS:
            raise DesignError("Bad join type.")
        t = j.get("table")
        if t not in specs:
            raise DesignError("Join table not found.")
        a = j.get("alias") or t
        ident(a, "alias")
        if a in amap:
            raise DesignError(f"Alias '{a}' is used twice - give the self-join a different alias.")
        amap[a] = specs[t]
        if jt == "CROSS":
            from_sql += f" CROSS JOIN {q(t)} AS {q(a)}"
        else:
            left = _col_ref(j.get("left"), amap)
            right = _col_ref(j.get("right"), amap)
            from_sql += f" {jt} JOIN {q(t)} AS {q(a)} ON {left} = {right}"

    sel, aliases = [], []
    group = [_col_ref(g, amap) for g in spec.get("group_by") or []]
    for c in spec.get("columns") or []:
        agg = c.get("agg", "")
        if c.get("col") in ("*", "") and agg == "COUNT":
            expr = "COUNT(*)"
        else:
            expr = _agg_expr(agg, _col_ref(c.get("col"), amap, allow_star=not agg))
        al = c.get("alias")
        if al:
            ident(al, "column alias")
            expr += f" AS {q(al)}"
            aliases.append(al)
        sel.append(expr)
    if not sel:
        sel = ["*"]
    sql = "SELECT " + ("DISTINCT " if spec.get("distinct") else "") + ", ".join(sel) + " FROM " + from_sql

    where = spec.get("where") or []
    if where:
        sql += " WHERE " + _where_sql(where, amap, specs, params, 0)
    if group:
        sql += " GROUP BY " + ", ".join(group)
    having = spec.get("having") or []
    if having:
        hp = []
        for i, h in enumerate(having):
            if h.get("op") not in CMP:
                raise DesignError("HAVING needs a comparison operator.")
            colx = "*" if h.get("col") in ("*", "") else _col_ref(h.get("col"), amap)
            expr = "COUNT(*)" if colx == "*" else _agg_expr(h.get("agg") or "COUNT", colx)
            params.append(_scalar(h.get("value")))
            hp.append(("AND " if i else "") + f"{expr} {h['op']} ?")
        sql += " HAVING " + " ".join(hp)
    order = []
    for o in spec.get("order_by") or []:
        ref = o.get("col")
        d = "DESC" if o.get("dir") == "desc" else "ASC"
        if ref in aliases:
            order.append(f"{q(ref)} {d}")
        else:
            order.append(f"{_col_ref(ref, amap)} {d}")
    if order:
        sql += " ORDER BY " + ", ".join(order)
    lim = spec.get("limit")
    if lim not in (None, ""):
        sql += f" LIMIT {max(1, min(1000, int(lim)))}"
    return sql, params


def inline_params(sql, params):
    """Readable SQL for display only (never executed)."""
    out, it = [], iter(params)
    for ch in sql:
        if ch == "?":
            v = next(it)
            out.append("NULL" if v is None else (str(v) if isinstance(v, (int, float)) else "'" + str(v).replace("'", "''") + "'"))
        else:
            out.append(ch)
    return "".join(out)
