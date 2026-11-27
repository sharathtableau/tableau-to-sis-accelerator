"""Deterministic, read-only query rewrites used by proven fixes (no other local imports).

lod_grain_sql: an aggregate-of-FIXED query (as engine hoists it) evaluated once per (mark, LOD key)
instead of once per row. Used by v3_prove to TEST the hypothesis and by engine to APPLY a fix a
reviewer approved after the proof. Returns None for any other query shape.
"""
import re

# --------------------------------------------------------------------------- #
# SQL helpers (our own generated shape only; anything else -> not applicable)
# --------------------------------------------------------------------------- #
_KW = re.compile(r'\b(SELECT|FROM|WHERE|GROUP\s+BY|HAVING|QUALIFY|ORDER\s+BY|LIMIT)\b', re.I)
_W = re.compile(r'__W\d+__')


def _top_level(sql):
    """[(KEYWORD, start, end)] for clause keywords at paren depth 0, outside quotes."""
    out, depth, i, quote = [], 0, 0, None
    while i < len(sql):
        c = sql[i]
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        elif depth == 0:
            m = _KW.match(sql, i)
            if m and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == '_')):
                out.append((re.sub(r'\s+', ' ', m.group(1).upper()), m.start(), m.end()))
                i = m.end()
                continue
        i += 1
    return out


def _split_commas(text):
    parts, depth, cur, quote = [], 0, [], None
    for c in text:
        if quote:
            cur.append(c)
            if c == quote:
                quote = None
            continue
        if c in "'\"":
            quote = c
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        elif c == ',' and depth == 0:
            parts.append(''.join(cur).strip())
            cur = []
            continue
        cur.append(c)
    if ''.join(cur).strip():
        parts.append(''.join(cur).strip())
    return parts


def _alias(item):
    """(expression, alias or None) for one select item; alias = last top-level ' AS name'."""
    m = None
    for m in re.finditer(r'\s+AS\s+("[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)\s*$', item, re.I):
        pass
    if m:
        expr = item[:m.start()]
        if expr.count('(') == expr.count(')'):
            return expr.strip(), m.group(1)
    return item.strip(), None


def _norm(s):
    return re.sub(r'\s+', ' ', s or '').strip().upper()


def partition_keys(sql):
    """Ordered distinct PARTITION BY keys of every window in `sql`."""
    keys = []
    for m in re.finditer(r'PARTITION\s+BY\s+', sql or '', re.I):
        i, depth, start = m.end(), 0, m.end()
        while i < len(sql):
            c = sql[i]
            if c == '(':
                depth += 1
            elif c == ')':
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and re.match(r'ORDER\s+BY\b', sql[i:], re.I):
                break
            i += 1
        for k in _split_commas(sql[start:i]):
            if k and k not in keys:
                keys.append(k)
    return keys


def lod_grain_sql(sql):
    """Rewrite a hoisted aggregate-of-FIXED query so each FIXED value counts once per
    (group key, LOD key). Returns None when the query is not that shape."""
    if not sql or ';' in sql or not _W.search(sql) or 'PARTITION' not in sql.upper() or '__lod' in sql:
        return None                                   # (an already-rewritten query is never rewritten again)
    s = sql.strip()
    kws = _top_level(s)
    if not kws or kws[0][0] != 'SELECT' or kws[0][1] != 0:
        return None
    names = [k[0] for k in kws]
    if 'FROM' not in names or 'GROUP BY' not in names:
        return None
    f = names.index('FROM')
    head = s[kws[0][2]:kws[f][1]]
    nxt = kws[f + 1][1] if f + 1 < len(kws) else len(s)
    from_part = s[kws[f][2]:nxt].strip()
    where = ''
    i = f + 1
    if i < len(kws) and kws[i][0] == 'WHERE':
        where = s[kws[i][2]:(kws[i + 1][1] if i + 1 < len(kws) else len(s))].strip()
        i += 1
    if i >= len(kws) or kws[i][0] != 'GROUP BY':
        return None
    g_end = kws[i + 1][1] if i + 1 < len(kws) else len(s)
    group_tokens = _split_commas(s[kws[i][2]:g_end])
    rest = s[g_end:].strip()
    items = _split_commas(head)
    if not items or any(it.strip() == '*' for it in items):
        return None
    w_used = sorted(set(_W.findall(head)), key=lambda a: int(a[3:-2]))
    keys = partition_keys(from_part)
    if not w_used or not keys:
        return None
    gexprs = []
    for t in group_tokens:
        if t.isdigit():
            n = int(t)
            if not 1 <= n <= len(items):
                return None
            gexprs.append(_alias(items[n - 1])[0])
        else:
            gexprs.append(t)
    norm_g = [_norm(g) for g in gexprs]
    out_items = []
    for it in items:
        expr, al = _alias(it)
        if _norm(expr) in norm_g:
            j = norm_g.index(_norm(expr))
            out_items.append(f'__G{j}__' + (f' AS {al}' if al else ''))
        else:
            out_items.append(it)
    new_group = [t if t.isdigit() else f'__G{norm_g.index(_norm(t))}__' for t in group_tokens]
    for j, g in sorted(enumerate(gexprs), key=lambda p: -len(p[1])):
        rest = rest.replace(g, f'__G{j}__')
    inner = ', '.join([f'{g} AS __G{j}__' for j, g in enumerate(gexprs)] +
                      [f'{k} AS __K{j}__' for j, k in enumerate(keys)] + w_used)
    dedupe = f'SELECT DISTINCT {inner} FROM {from_part}' + (f' WHERE {where}' if where else '')
    out = (f'SELECT {", ".join(out_items)} FROM ({dedupe}) __lod GROUP BY {", ".join(new_group)}'
           + (f' {rest}' if rest else ''))
    return out if read_only(out) else None


def read_only(sql):
    t = (sql or '').strip()
    return bool(t) and ';' not in t and re.match(r'(SELECT|WITH)\b', t, re.I) is not None and not re.search(
        r'\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CALL|PUT|COPY)\b', t, re.I)


TRANSFORMS = {'lod_key_grain': lod_grain_sql}


def key_norm(key):
    """CUSTOMER_ID, "CUSTOMER_ID", t.customer_id -> CUSTOMER_ID (for comparing a proposed key with a query's key)."""
    return re.sub(r'["`\s]', '', str(key or '').split('.')[-1]).upper()


def lod_grain_for(key):
    """lod_key_grain limited to queries whose FIXED key is exactly `key` (a fix Cortex proposed).
    Any other query is left alone, so the fix does exactly what was proposed and proven."""
    want = key_norm(key)

    def transform(sql):
        if not want or [key_norm(k) for k in partition_keys(sql or '')] != [want]:
            return None
        return lod_grain_sql(sql)
    transform.key = key
    return transform


def transform_for(fix, key=None):
    """The rewrite a fix id (and optional proposed key) stands for; None when unknown."""
    if fix == 'lod_key_grain' and key:
        return lod_grain_for(key)
    return TRANSFORMS.get(fix)
