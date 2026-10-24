"""Normalisation checker.

The designer declares functional dependencies (FDs) such as
    student_id -> name, dept
and this module computes closures, candidate keys, the highest normal form
(1NF/2NF/3NF/BCNF), the offending dependencies, and a lossless decomposition
(3NF synthesis from a minimal cover; BCNF by repeated splitting).
"""
from itertools import combinations
import re


def parse_fd_side(s):
    return frozenset(x.strip() for x in re.split(r"[,\s]+", s.strip()) if x.strip())


def closure(attrs, fds):
    res = set(attrs)
    changed = True
    while changed:
        changed = False
        for lhs, rhs in fds:
            if lhs <= res and not rhs <= res:
                res |= rhs
                changed = True
    return frozenset(res)


def candidate_keys(attrs, fds):
    attrs = frozenset(attrs)
    if len(attrs) > 14:
        raise ValueError("Too many attributes (max 14) for the key search.")
    # attributes that never appear on a right-hand side must be in every key
    on_rhs = set().union(*[r for _, r in fds]) if fds else set()
    must = frozenset(a for a in attrs if a not in on_rhs)
    rest = sorted(attrs - must)
    keys = []
    for size in range(0, len(rest) + 1):
        for extra in combinations(rest, size):
            cand = must | frozenset(extra)
            if any(k <= cand for k in keys):
                continue
            if closure(cand, fds) == attrs:
                keys.append(cand)
    return keys


def minimal_cover(fds):
    # 1. singleton right sides
    g = [(l, frozenset([a])) for l, r in fds for a in r]
    # 2. remove extraneous left attributes
    out = []
    for l, r in g:
        l = set(l)
        for a in sorted(l):
            if len(l) > 1 and r <= closure(l - {a}, g):
                l.remove(a)
        out.append((frozenset(l), r))
    # 3. remove redundant FDs
    res = list(dict.fromkeys(out))
    i = 0
    while i < len(res):
        rest = res[:i] + res[i + 1:]
        if res[i][1] <= closure(res[i][0], rest):
            res = rest
        else:
            i += 1
    return res


def project_fds(sub, fds):
    """FDs holding on attribute subset `sub` (via closures)."""
    out = []
    sub = sorted(sub)
    for size in range(1, len(sub)):
        for lhs in combinations(sub, size):
            lhs = frozenset(lhs)
            rhs = (closure(lhs, fds) & frozenset(sub)) - lhs
            if rhs:
                out.append((lhs, rhs))
    return out


def three_nf_synthesis(attrs, fds):
    cover = minimal_cover(fds)
    groups = {}
    for l, r in cover:
        groups.setdefault(l, set()).update(r)
    rels = [frozenset(l | r) for l, r in groups.items()]
    keys = candidate_keys(attrs, fds)
    if keys and not any(k <= rel for rel in rels for k in keys):
        rels.append(sorted(keys, key=lambda k: (len(k), sorted(k)))[0])
    covered = set().union(*rels) if rels else set()
    missing = set(attrs) - covered
    if missing:
        rels.append(frozenset(missing))
    rels = [r for r in dict.fromkeys(rels) if not any(r < o for o in rels)]
    return rels


def bcnf_decompose(attrs, fds):
    attrs = frozenset(attrs)
    result = [attrs]
    done = False
    while not done:
        done = True
        for rel in list(result):
            sub_fds = project_fds(rel, fds)
            for lhs, rhs in sub_fds:
                if closure(lhs, sub_fds) != rel and not rhs <= lhs:
                    r1 = frozenset(lhs | (closure(lhs, fds) & rel))
                    r2 = frozenset(lhs | (rel - r1))
                    if r1 == rel or r2 == rel:
                        continue
                    result.remove(rel)
                    result += [r1, r2]
                    done = False
                    break
            if not done:
                break
    return [r for r in dict.fromkeys(result) if not any(r < o for o in result)]


def analyse(attrs, raw_fds):
    """raw_fds: list of (lhs_string, rhs_string)."""
    attrs = frozenset(attrs)
    fds = []
    problems = []
    for l, r in raw_fds:
        L, R = parse_fd_side(l), parse_fd_side(r)
        bad = (L | R) - attrs
        if bad:
            problems.append(f"FD {','.join(sorted(L))} -> {','.join(sorted(R))} mentions unknown column(s): {', '.join(sorted(bad))}")
            continue
        if L and R:
            fds.append((L, R))
    keys = candidate_keys(attrs, fds)
    prime = set().union(*keys) if keys else set()
    violations = {"2NF": [], "3NF": [], "BCNF": []}
    for l, r in fds:
        for a in sorted(r - l):
            if l >= attrs or closure(l, fds) == attrs:
                continue                      # l is a superkey -> fine
            fd_txt = f"{', '.join(sorted(l))} -> {a}"
            violations["BCNF"].append(fd_txt)
            if a in prime:
                continue
            if any(l < k for k in keys):
                violations["2NF"].append(fd_txt + "   (partial dependency on part of a key)")
            else:
                violations["3NF"].append(fd_txt + "   (transitive dependency)")
    if violations["2NF"]:
        level = "1NF"
    elif violations["3NF"]:
        level = "2NF"
    elif violations["BCNF"]:
        level = "3NF"
    else:
        level = "BCNF"
    rels3 = three_nf_synthesis(attrs, fds)
    relsb = bcnf_decompose(attrs, fds)
    cover = minimal_cover(fds)

    def fmt(rels):
        out = []
        for rel in sorted(rels, key=lambda r: sorted(r)):
            sub = project_fds(rel, fds)
            ks = candidate_keys(rel, sub) if len(rel) <= 14 else []
            out.append({"attributes": sorted(rel),
                        "key": sorted(sorted(ks, key=lambda k: (len(k), sorted(k)))[0]) if ks else sorted(rel)})
        return out

    return {
        "candidate_keys": [sorted(k) for k in keys],
        "prime": sorted(prime),
        "highest_normal_form": level,
        "violations": violations,
        "minimal_cover": [f"{', '.join(sorted(l))} -> {', '.join(sorted(r))}" for l, r in cover],
        "decomposition_3nf": fmt(rels3),
        "decomposition_bcnf": fmt(relsb),
        "warnings": problems,
    }


def repeating_group_hint(colnames):
    """Heuristic 1NF smell: phone1, phone2, phone3 style columns."""
    stems = {}
    for c in colnames:
        m = re.match(r"^(.*?)[_]?(\d+)$", c)
        if m:
            stems.setdefault(m.group(1), []).append(c)
    return {k: v for k, v in stems.items() if len(v) > 1 and k}


def migration_sql(table, parts, spec_cols):
    """CREATE + INSERT...SELECT DISTINCT statements for a proposed decomposition."""
    types = {c["name"]: c for c in spec_cols}
    out = []
    for i, p in enumerate(parts, start=1):
        nm = f"{table}_part{i}"
        cols = []
        for a in p["attributes"]:
            t = types[a]["type"]
            cols.append(f'  "{a}" {t}' + (" NOT NULL" if a in p["key"] else ""))
        out.append(f'CREATE TABLE "{nm}" (\n' + ",\n".join(cols) +
                   f',\n  PRIMARY KEY ({", ".join(chr(34) + k + chr(34) for k in p["key"])})\n);')
        attrs = ", ".join(f'"{a}"' for a in p["attributes"])
        out.append(f'INSERT INTO "{nm}" ({attrs}) SELECT DISTINCT {attrs} FROM "{table}";')
    return "\n".join(out)
