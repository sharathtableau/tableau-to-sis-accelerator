"""V7: Tableau text tables with dimensions on Columns, as crosstabs, with Tableau's totals.

Tableau lays a text table out as row headers (the Rows dimensions, and Measure Names when it sits on Rows) by column
headers (the Columns dimensions, and Measure Names when it sits there). Totals come from the worksheet XML:
<rows total='true'> and <cols total='true'> add grand totals, <subtotals><column> a subtotal for each member of an
outer header. Tableau's automatic totals re-aggregate the measure over the coarser level of detail -- an average total
is the average of the underlying rows, not of the cells -- so every total level is the plan run at that level
(the run() callback: execute() with the dropped dimensions removed). Display only: the plan's own rows are unchanged."""
import re

TOTAL, GRAND = 'Total', 'Grand Total'


def parse_totals(xml):
    """{'grand_rows', 'grand_cols', 'subtotals': [captions]} from a worksheet's <table> XML, or None."""
    xml = xml or ''
    cols = re.search(r"<cols\b[^>]*\btotal=['\"]true['\"]", xml)
    rows = re.search(r"<rows\b[^>]*\btotal=['\"]true['\"]", xml)
    subs = []
    m = re.search(r'<subtotals>(.*?)</subtotals>', xml, re.S)
    if m:
        for tok in re.findall(r'<column>([^<]+)</column>', m.group(1)):
            name = re.search(r'\.\[(?:\w+:)?([^:\]]+)(?::\w+)*\]$', tok.strip())
            if name:
                subs.append(name.group(1))
    if not (cols or rows or subs):
        return None
    # a total on a shelf totals that shelf's headers: Rows -> a grand total row, Columns -> a grand total column
    # (to confirm against a Tableau capture that includes totals; the corpus captures hold the detail rows only)
    return {'grand_rows': bool(rows), 'grand_cols': bool(cols), 'subtotals': subs}


def applies(plan):
    return plan.get('mark') == 'text' and any(f['channel'] == 'cols' and f['kind'] == 'dim' and f.get('discrete')
                                              for f in plan.get('fields') or [])


def _dims(plan, shelf):
    return [f for f in plan['fields'] if f['channel'] == shelf and f['kind'] == 'dim' and f.get('discrete')
            and not f.get('aggregate_dimension')]


def _mn(plan, shelf):
    return any(f['channel'] == shelf and f['kind'] == 'measure_names' for f in plan['fields'])


def _cells(plan, df, engine, row_dims, col_dims, label_of):
    """[(row key, col key, text)] for one result frame; dims missing from the frame take label_of(dim)."""
    import v3_sheet_plan as P
    shown = P.formatted_frame(plan, df, engine)
    _d, measures = P.query_fields(plan)
    measures = [m for m in measures if m.get('channel') not in ('sort', 'refline')]
    mn_rows, mn_cols = _mn(plan, 'rows'), _mn(plan, 'cols')
    out = []
    for i in range(len(shown)):
        r = tuple(str(shown[P._title(d)].iloc[i]) if P._title(d) in shown.columns else label_of(d) for d in row_dims)
        c = tuple(str(shown[P._title(d)].iloc[i]) if P._title(d) in shown.columns else label_of(d) for d in col_dims)
        for m in measures:
            t = P._title(m, True)
            if t not in shown.columns:
                continue
            v = shown[t].iloc[i]
            text = '' if v is None or (isinstance(v, float) and v != v) else str(v)
            rk = r + ((m['caption'],) if mn_rows else ())
            ck = c + ((m['caption'],) if (mn_cols or (len(measures) > 1 and not mn_rows)) else ())
            out.append((rk, ck, text))
    return out


def crosstab(plan, df, engine, run=None):
    """The crosstab as a DataFrame: row header columns, then one column per column-header combination."""
    import pandas as pd
    import v3_sheet_plan as P
    row_dims, col_dims = _dims(plan, 'rows'), _dims(plan, 'cols')
    totals = plan.get('totals') or {}
    cells = _cells(plan, df, engine, row_dims, col_dims, lambda d: '')

    # member order of every header: Tableau's sort for the dimension, else its natural (calendar) order
    order = {}
    sort_for = P._sort_lookup(plan, engine, {}, df)
    shown = P.formatted_frame(plan, df, engine)
    for d in row_dims + col_dims:
        title = P._title(d)
        if title not in shown.columns:
            continue
        given = sort_for(d)
        if isinstance(given, list):
            seq = [str(v) for v in given]
        else:
            raw = df[P.col_name(d)] if P.col_name(d) in df.columns else shown[title]
            pairs = sorted(zip(raw.tolist(), shown[title].astype(str).tolist()),
                           key=lambda p: (p[0] is None, str(type(p[0])), p[0] if p[0] is not None else 0))
            seq = list(dict.fromkeys(lab for _r, lab in pairs))
        order[title] = {v: i for i, v in enumerate(seq)}

    # totals: the plan at each coarser level (outer headers kept, inner ones totalled)
    def levels(dims, subs, grand):
        out = []
        for i, d in enumerate(dims[:-1]):
            if d['caption'] in subs:
                out.append(i + 1)                    # keep dims[:i+1]
        if grand and dims:
            out.append(0)
        return out
    subs = set(totals.get('subtotals') or [])
    r_levels = [len(row_dims)] + levels(row_dims, subs, totals.get('grand_rows'))
    c_levels = [len(col_dims)] + levels(col_dims, subs, totals.get('grand_cols'))
    if run is not None:
        for rl in r_levels:
            for cl in c_levels:
                if rl == len(row_dims) and cl == len(col_dims):
                    continue
                dropped = row_dims[rl:] + col_dims[cl:]
                variant = dict(plan, fields=[f for f in plan['fields'] if not any(f is d for d in dropped)])
                variant.pop('totals', None)
                try:
                    sub = run(variant)
                except Exception:
                    continue

                def label(d, rl=rl, cl=cl):
                    dims, keep = (row_dims, rl) if d in row_dims else (col_dims, cl)
                    first = dims.index(d) == keep
                    return (GRAND if keep == 0 else TOTAL) if first else ''
                cells += _cells(variant, sub, engine, row_dims, col_dims, label)

    def key_rank(key, dims):
        out = []
        for d, v in zip(dims, key):
            ranks = order.get(P._title(d), {})
            out.append((2, 0) if v == GRAND else (1, 0) if v == TOTAL else (0, ranks.get(v, len(ranks))) if v
                       else (3, 0))
        for extra in key[len(dims):]:              # a Measure Names member: the plan's measure order
            names = [m['caption'] for m in P.query_fields(plan)[1]]
            out.append((0, names.index(extra) if extra in names else len(names)))
        return tuple(out)
    rkeys = sorted({r for r, _c, _t in cells}, key=lambda k: key_rank(k, row_dims))
    ckeys = sorted({c for _r, c, _t in cells}, key=lambda k: key_rank(k, col_dims))
    grid = {(r, c): t for r, c, t in cells}
    head = [P._title(d) for d in row_dims] + (['Measure Names'] if _mn(plan, 'rows') else [])
    names = [' · '.join(x for x in c if x) or 'Value' for c in ckeys]
    rows = [list(r) + [grid.get((r, c), '') for c in ckeys] for r in rkeys]
    return pd.DataFrame(rows, columns=head + names)
