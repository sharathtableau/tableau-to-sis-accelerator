"""
report_sql.py  --  report-ir/1 visual -> one Snowflake SQL statement.

Shared (not Power BI specific). Responsibilities:

* Join the tables a visual needs along the model's many-to-one relationships
  (base = the table its measures aggregate; dimensions LEFT JOINed, so
  unmatched keys become a blank member, as in Power BI).
* Resolve column tokens {{c:T::C}} to physical columns, calculated-column SQL
  or derived date expressions (virtual date tables), recursively.
* Evaluate each measure leaf in its own filter context: a leaf with
  CALCULATE overrides drops the filters on those columns and does not group
  by them; the leaf groups are joined back on the remaining dimensions.
* Drop rows whose measures are all BLANK (Power BI does not show them).
* Quote every identifier and render every filter value as a typed literal.
  No user text is ever concatenated into SQL unquoted.

Every statement uses plain ANSI constructs that run on both Snowflake and the
local DuckDB test harness (IS NOT DISTINCT FROM, CASE, COUNT(DISTINCT ...)).
"""

import datetime as _dt
import re

_TOKEN_C = re.compile(r"\{\{c:(.+?)::(.+?)\}\}")
_TOKEN_A = re.compile(r"\{\{a:(\d+)\}\}")


class CompileError(Exception):
    pass


def qident(name):
    if name is None or str(name) == "":
        raise CompileError("empty identifier")
    return '"' + str(name).replace('"', '""') + '"'


def fqn(binding):
    return ".".join(qident(binding[k]) for k in ("database", "schema", "object"))


def literal(v):
    """Python value -> SQL literal. Raises on anything unexpected."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(int(v))
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise CompileError("non-finite number in filter")
        return repr(v)
    if isinstance(v, (_dt.datetime,)):
        return f"CAST('{v.strftime('%Y-%m-%d %H:%M:%S')}' AS TIMESTAMP)"
    if isinstance(v, _dt.date):
        return f"CAST('{v.isoformat()}' AS DATE)"
    if hasattr(v, "item"):                      # numpy scalar
        return literal(v.item())
    return "'" + str(v).replace("'", "''") + "'"


_DATE_LIT = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$")


def _typed(v, data_type):
    if isinstance(v, str) and data_type in ("datetime", "date") and _DATE_LIT.match(v):
        d = v.replace("T", " ")
        return f"CAST('{d}' AS TIMESTAMP)"
    return literal(v)


class Compiler:
    def __init__(self, ir):
        self.model = ir["model"]
        self.tables = self.model["tables"]
        self.rels = [r for r in self.model["relationships"]
                     if r["active"] and r["support"] in ("auto", "partial")]

    # ------------------------------------------------------------ joins --- #
    def _edges(self, frm):
        return [r for r in self.rels if r["from_table"] == frm
                and self.tables.get(r["to_table"], {}).get("kind") == "source"]

    def _path(self, base, target):
        """Relationship path base -> target following many-to-one edges."""
        if base == target:
            return []
        seen, queue = {base}, [(base, [])]
        while queue:
            cur, path = queue.pop(0)
            for r in self._edges(cur):
                nxt = r["to_table"]
                if nxt in seen:
                    continue
                if nxt == target:
                    return path + [r]
                seen.add(nxt)
                queue.append((nxt, path + [r]))
        return None

    def _plan(self, base, tables):
        """alias map + JOIN clauses for `tables` reachable from `base`."""
        b = self.tables.get(base)
        if not b or b["kind"] != "source" or not b.get("binding"):
            raise CompileError(f"table {base} has no Snowflake binding")
        aliases = {base: "t0"}
        joins = []
        for t in sorted(tables):
            if t in aliases:
                continue
            tt = self.tables.get(t)
            if not tt:
                raise CompileError(f"unknown table {t}")
            if tt["kind"] == "virtual_date":
                if base not in tt["anchors"]:
                    raise CompileError(f"date table {t} is not related to {base}")
                continue
            if tt["kind"] != "source" or not tt.get("binding"):
                raise CompileError(f"table {t} is not available in Snowflake")
            path = self._path(base, t)
            if path is None:
                raise CompileError(f"no relationship path from {base} to {t}")
            for r in path:
                if r["to_table"] in aliases:
                    continue
                a = f"t{len(aliases)}"
                aliases[r["to_table"]] = a
                fcol = self._phys(r["from_table"], r["from_column"], aliases)
                tcol = self._phys(r["to_table"], r["to_column"], aliases)
                joins.append(f"LEFT JOIN {fqn(self.tables[r['to_table']]['binding'])} {a} "
                             f"ON {fcol} = {tcol}")
        return aliases, joins

    def _phys(self, table, column, aliases):
        c = self.tables[table]["columns"][column]
        if not c.get("physical"):
            raise CompileError(f"{table}[{column}] is not a physical column")
        return f"{aliases[table]}.{qident(c['physical'])}"

    # ---------------------------------------------------------- columns --- #
    def column_sql(self, table, column, base, aliases, depth=0):
        if depth > 20:
            raise CompileError("calculated column nesting too deep")
        t = self.tables.get(table)
        if not t or column not in t["columns"]:
            raise CompileError(f"unknown column {table}[{column}]")
        c = t["columns"][column]
        if c["support"] == "manual":
            raise CompileError(f"{table}[{column}] is not converted")
        if t["kind"] == "virtual_date":
            if not c.get("dax"):
                anchor = t["anchors"].get(base)
                if anchor is None:
                    raise CompileError(f"date table {table} not related to {base}")
                return f"CAST({self.column_sql(base, anchor, base, aliases, depth + 1)} AS DATE)"
            return self._resolve(c["sql"], base, aliases, depth)
        if c.get("dax"):
            if not c.get("sql"):
                raise CompileError(f"{table}[{column}] is not converted")
            return "(" + self._resolve(c["sql"], base, aliases, depth) + ")"
        if table not in aliases:
            raise CompileError(f"{table} is not joined")
        return self._phys(table, column, aliases)

    def _resolve(self, sql, base, aliases, depth=0):
        return _TOKEN_C.sub(
            lambda m: self.column_sql(m.group(1), m.group(2), base, aliases, depth + 1), sql)

    def _tokens_tables(self, sql, acc):
        for m in _TOKEN_C.finditer(sql or ""):
            t, c = m.group(1), m.group(2)
            acc.add(t)
            tt = self.tables.get(t, {})
            col = tt.get("columns", {}).get(c, {})
            if col.get("sql"):
                self._tokens_tables(col["sql"], acc)
        return acc

    def field_sql(self, field, base, aliases):
        expr = self.column_sql(field["table"], field["name"], base, aliases)
        lv = field.get("level")
        if lv:
            return {"year": "YEAR({})", "quarter": "QUARTER({})", "month": "MONTH({})",
                    "day": "CAST({} AS DATE)"}[lv].format(expr)
        return expr

    def field_tables(self, field):
        acc = {field["table"]}
        col = self.tables[field["table"]]["columns"][field["name"]]
        if col.get("sql"):
            self._tokens_tables(col["sql"], acc)
        return acc

    # --------------------------------------------------------- measures --- #
    def translation(self, field):
        import dax_rules
        if field["kind"] == "measure":
            m = self.model["measures"][field["name"]]
            if m["status"] != "ready":
                raise CompileError(f"measure {field['name']} is held (not verified)")
            return m["translation"]
        if field["kind"] == "aggregation":
            return dax_rules.implicit_measure(field["table"], field["name"], field.get("agg"))
        raise CompileError(f"{field.get('name')} is not a measure")

    def choose_base(self, dims, measures):
        leaf_tables = set()
        for m in measures:
            for leaf in self.translation(m)["leaves"]:
                leaf_tables.update(leaf["tables"])
        if len(leaf_tables) > 1:
            raise CompileError("measures aggregate more than one table "
                               f"({', '.join(sorted(leaf_tables))}); not supported yet")
        if leaf_tables:
            return leaf_tables.pop()
        needed = set()
        for d in dims:
            needed |= self.field_tables(d)
        src = [t for t in needed if self.tables[t]["kind"] == "source"]
        for cand in sorted(src) + sorted(t for t, v in self.tables.items()
                                         if v["kind"] == "source"):
            ok = all(self.tables[t]["kind"] == "virtual_date" and cand in self.tables[t]["anchors"]
                     or self._path(cand, t) is not None for t in needed)
            if ok:
                return cand
        raise CompileError("no single table connects these fields")

    # ----------------------------------------------------------- filters --- #
    def filter_sql(self, f, base, aliases):
        fld = f["field"]
        expr = self.field_sql(fld, base, aliases)
        dt = self.tables[fld["table"]]["columns"][fld["name"]].get("data_type")
        op, vals = f["op"], list(f.get("values") or [])
        if op in ("in", "not_in"):
            nulls = any(v is None for v in vals)
            vals = [v for v in vals if v is not None]
            parts = []
            if vals:
                parts.append(f"{expr} IN ({', '.join(_typed(v, dt) for v in vals)})")
            if nulls:
                parts.append(f"{expr} IS NULL")
            if not parts:
                return "1=0" if op == "in" else "1=1"
            cond = "(" + " OR ".join(parts) + ")"
            return cond if op == "in" else f"(NOT {cond} OR {expr} IS NULL)" if not nulls \
                else f"(NOT {cond})"
        if op == "between":
            lo, hi = vals
            if dt in ("datetime", "date"):
                return (f"(CAST({expr} AS DATE) BETWEEN {literal(lo)} AND {literal(hi)})")
            return f"({expr} BETWEEN {literal(lo)} AND {literal(hi)})"
        if op in ("=", ">", ">=", "<", "<="):
            return f"({expr} {op} {_typed(vals[0], dt)})"
        raise CompileError(f"filter operator {op}")

    # ------------------------------------------------------------- query --- #
    def visual_query(self, dims, measures, filters, order=None, limit=None,
                     drop_blank_rows=True):
        """Compile one visual query.

        dims      list of field dicts (columns / date levels)
        measures  list of field dicts (measures / implicit aggregations)
        filters   list of filter dicts (report, page, visual and slicer)
        order     list of (output name, "asc"|"desc"), names D<i>, S<i>, M<i>
        Returns (sql, meta) where meta lists the output columns."""
        base = self.choose_base(dims, measures)
        needed = set()
        for d in dims:
            needed |= self.field_tables(d)
        for f in filters:
            needed |= self.field_tables(f["field"])
        trs = [self.translation(m) for m in measures]
        for tr in trs:
            for leaf in tr["leaves"]:
                needed |= self._tokens_tables(leaf["sql"], set())
        aliases, joins = self._plan(base, needed)
        frm = f"FROM {fqn(self.tables[base]['binding'])} t0" + \
              ("".join("\n  " + j for j in joins) if joins else "")

        dim_cols = []            # (name, sql, field-key)
        for i, d in enumerate(dims):
            dim_cols.append((f"D{i}", self.field_sql(d, base, aliases), (d["table"], d["name"])))
            sb = self.tables[d["table"]]["columns"][d["name"]].get("sort_by")
            if sb and not d.get("level"):
                sf = {"table": d["table"], "name": sb, "kind": "column"}
                dim_cols.append((f"S{i}", self.field_sql(sf, base, aliases), (d["table"], d["name"])))

        filt = [(f, self.filter_sql(f, base, aliases)) for f in filters]

        # leaf groups by override set
        groups = {}
        leaf_ref = []           # per measure: list of (group key, leaf col name)
        for mi, tr in enumerate(trs):
            refs = []
            for li, leaf in enumerate(tr["leaves"]):
                key = tuple(sorted(tuple(o) for o in leaf["overrides"]))
                g = groups.setdefault(key, [])
                col = f"L{len(g)}"
                g.append((col, self._resolve(leaf["sql"], base, aliases)))
                refs.append((key, col))
            leaf_ref.append(refs)
        groups.setdefault((), [])

        ctes, gname = [], {}
        for gi, (key, leaves) in enumerate(sorted(groups.items(), key=lambda kv: len(kv[0]))):
            name = f"q{gi}"
            gname[key] = name
            over = set(key)
            gdims = [(n, s) for n, s, k in dim_cols if k not in over]
            sel = [f"{s} AS {qident(n)}" for n, s in gdims] + \
                  [f"{s} AS {qident(c)}" for c, s in leaves]
            if not sel:
                sel = ["COUNT(*) AS \"_N\""]
            where = [c for f, c in filt
                     if (f["field"]["table"], f["field"]["name"]) not in over]
            sql = "SELECT " + ", ".join(sel) + "\n  " + frm
            if where:
                sql += "\n  WHERE " + " AND ".join(where)
            if gdims:
                sql += "\n  GROUP BY " + ", ".join(str(i + 1) for i in range(len(gdims)))
            ctes.append((name, sql, [n for n, _ in gdims]))

        main = gname[()]
        out_sel = [f"{main}.{qident(n)}" for n, _, _ in dim_cols]
        joins2 = []
        for name, _, gd in ctes:
            if name == main:
                continue
            on = [f"{main}.{qident(n)} IS NOT DISTINCT FROM {name}.{qident(n)}" for n in gd]
            joins2.append(f"LEFT JOIN {name} ON " + (" AND ".join(on) if on else "1=1"))
        for mi, tr in enumerate(trs):
            refs = leaf_ref[mi]
            expr = _TOKEN_A.sub(lambda m: f"{gname[refs[int(m.group(1))][0]]}."
                                          f"{qident(refs[int(m.group(1))][1])}", tr["expr"])
            out_sel.append(f"{expr} AS {qident(f'M{mi}')}")
        if not out_sel:
            raise CompileError("visual has no fields")
        sql = "WITH " + ",\n".join(f"{n} AS (\n  {s}\n)" for n, s, _ in ctes) + \
              "\nSELECT " + ", ".join(out_sel) + f"\nFROM {main}" + \
              "".join("\n" + j for j in joins2)
        outer = []
        if drop_blank_rows and dims and measures:
            outer.append("NOT (" + " AND ".join(f"{qident(f'M{i}')} IS NULL"
                                                for i in range(len(measures))) + ")")
        if outer or order or limit:
            sql = f"SELECT * FROM (\n{sql}\n) r"
            if outer:
                sql += "\nWHERE " + " AND ".join(outer)
            if order:
                sql += "\nORDER BY " + ", ".join(
                    f"{qident(n)} {'DESC' if d == 'desc' else 'ASC'} NULLS LAST" for n, d in order)
            if limit:
                sql += f"\nLIMIT {int(limit)}"
        meta = {"dims": [n for n, _, _ in dim_cols], "measures": [f"M{i}" for i in range(len(measures))],
                "base": base}
        return sql, meta

    def distinct_values(self, field, filters, limit=1000):
        base = self.choose_base([field], [])
        needed = self.field_tables(field)
        for f in filters:
            needed |= self.field_tables(f["field"])
        aliases, joins = self._plan(base, needed)
        expr = self.field_sql(field, base, aliases)
        sb = self.tables[field["table"]]["columns"][field["name"]].get("sort_by")
        sel = [f"{expr} AS \"V\""]
        order = '"V"'
        if sb:
            sel.append(f"{self.field_sql({'table': field['table'], 'name': sb}, base, aliases)} AS \"S\"")
            order = '"S", "V"'
        sql = f"SELECT DISTINCT {', '.join(sel)} FROM {fqn(self.tables[base]['binding'])} t0"
        sql += "".join(" " + j for j in joins)
        where = [self.filter_sql(f, base, aliases) for f in filters]
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql + f" ORDER BY {order} LIMIT {int(limit)}"

    def range_values(self, field, filters):
        base = self.choose_base([field], [])
        needed = self.field_tables(field)
        for f in filters:
            needed |= self.field_tables(f["field"])
        aliases, joins = self._plan(base, needed)
        expr = self.field_sql(field, base, aliases)
        sql = (f"SELECT MIN({expr}) AS \"LO\", MAX({expr}) AS \"HI\" "
               f"FROM {fqn(self.tables[base]['binding'])} t0" + "".join(" " + j for j in joins))
        where = [self.filter_sql(f, base, aliases) for f in filters]
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql
