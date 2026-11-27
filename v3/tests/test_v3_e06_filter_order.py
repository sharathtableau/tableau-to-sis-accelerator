"""E06a filter order of operations -- quantitative filters. Written BEFORE the fix (tests first).

Tableau:
  * included-values on a quantitative filter decides Nulls: in-range / non-null exclude them, in-range-or-null keeps
    them, null keeps only them, all is no filter. The parser ignored it: a 'non-null' filter (no bounds) was dropped
    and an 'in-range-or-null' range lost its Nulls.
  * A filter on an AGGREGATED measure (sum:/avg:/... or an aggregate calculation) is a measure filter: it applies
    after aggregation at the view's level of detail (HAVING), never as a row WHERE. A filter on a disaggregated
    measure (none:) is a row filter.
Proved on two corpus workbooks (World Indicators: non-null on none: and avg:; Superstore: an aggregate calculation
range). Expected values are computed with plain pandas from the raw table, not with the code under test."""
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import tableau_parser as TP  # noqa: E402


def filters(xml):
    return TP.applied_filters(ET.fromstring(f'<worksheet><table><view>{xml}</view></table></worksheet>'), {})


class Parse(unittest.TestCase):
    def test_included_values(self):
        q = "<filter class='quantitative' column='[ds].[{c}]' included-values='{i}'>{b}</filter>"
        b = '<min>1</min><max>5</max>'
        self.assertEqual(filters(q.format(c='none:X:qk', i='in-range', b=b))[0]['nulls'], 'exclude')
        self.assertEqual(filters(q.format(c='none:X:qk', i='in-range-or-null', b=b))[0]['nulls'], 'include')
        f = filters(q.format(c='avg:X:qk', i='non-null', b=''))[0]
        self.assertEqual((f['kind'], f['nulls'], f['min'], f['max'], f['measure'], f['agg']),
                         ('range', 'exclude', None, None, True, 'avg'))
        self.assertEqual(filters(q.format(c='none:X:qk', i='null', b=''))[0]['nulls'], 'only')
        self.assertEqual(filters(q.format(c='none:X:qk', i='all', b='')), [])
        self.assertFalse(filters(q.format(c='none:X:qk', i='in-range', b=b))[0]['measure'])


class Predicate(unittest.TestCase):
    def rows(self, f):
        import duckdb
        import engine
        pred = engine._value_predicate(dict(f, caption='X'))
        sql = 'SELECT X FROM (VALUES (0), (1), (3), (9), (NULL)) t(X)' + (f' WHERE {pred}' if pred else '')
        return sorted(duckdb.sql(sql).fetchall(), key=lambda r: (r[0] is None, r[0] or 0))

    def test_null_semantics(self):
        rng = {'kind': 'range', 'min': 1, 'max': 5}
        self.assertEqual(self.rows(dict(rng, nulls='exclude')), [(1,), (3,)])
        self.assertEqual(self.rows(dict(rng, nulls='include')), [(1,), (3,), (None,)])
        self.assertEqual(self.rows({'kind': 'range', 'min': None, 'max': None, 'nulls': 'exclude'}),
                         [(0,), (1,), (3,), (9,)])
        self.assertEqual(self.rows({'kind': 'range', 'min': None, 'max': None, 'nulls': 'only'}), [(None,)])
        self.assertEqual(self.rows(rng), [(1,), (3,)])          # older IR without 'nulls': unchanged


def corpus(workbook, sheet_name):
    import plan_census as PC
    r = PC.intake(ROOT / 'Workbooks' / workbook)
    ir = r['ir']
    sheet = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == sheet_name)
    return ir, {k: v for k, v in r['bindings'].items() if v}, sheet


def col(engine, caption):
    return engine.rdim(caption).strip('"')


class WorldIndicators(unittest.TestCase):
    """Non-null filters: 'none:' is a row filter (other measures average only the remaining rows); 'avg:' is a
    measure filter (other measures still average every row of the mark)."""

    def run_sheet(self, name):
        import engine
        import v3_sheet_plan as P
        from v3_local import preview_context
        ir, b, s = corpus('World_Indicators_Tableau2024_3.twbx', name)
        with preview_context(ir, b):
            with engine.field_scope(s):
                raw = engine.q(f'SELECT * FROM {engine.tbl(s)}')
                cols = {c: col(engine, c) for c in ('Country/Region', 'Region', 'Ease of Business',
                                                     'Hours to do Tax', 'Business Tax Rate')}
            df = P.execute(P.from_sheet(s, ir), s, engine)
        return df, raw, cols

    def test_row_level_non_null(self):
        df, raw, c = self.run_sheet('Biz Tax rate')
        kept = raw[raw[c['Business Tax Rate']].notna()]
        want = kept.groupby([c['Country/Region'], c['Region']])[c['Ease of Business']].mean()
        self.assertEqual(len(df), len(want))
        got = df.set_index(['D|none|Country/Region', 'D|none|Region'])['M|avg|Ease of Business']
        for k, v in want.items():
            self.assertTrue((v != v and got[k] != got[k]) or abs(got[k] - v) < 1e-9, k)

    def test_measure_non_null_is_after_aggregation(self):
        df, raw, c = self.run_sheet('Hours to do Taxes')
        g = raw.groupby([c['Country/Region'], c['Region']])
        avg = g[c['Hours to do Tax']].mean()
        keep = avg[avg.notna()].index
        self.assertEqual(len(df), len(keep))
        self.assertFalse(df['M|avg|Hours to do Tax'].isna().any())
        ease = g[c['Ease of Business']].mean()          # every row of the mark, not only non-null Hours rows
        got = df.set_index(['D|none|Country/Region', 'D|none|Region'])['M|avg|Ease of Business']
        for k in keep:
            self.assertTrue((ease[k] != ease[k] and got[k] != got[k]) or abs(got[k] - ease[k]) < 1e-9, k)


class SuperstoreAggregateCalc(unittest.TestCase):
    """Profit Ratio = SUM(Profit)/SUM(Sales), filtered by range with Nulls kept: HAVING at State/Country level."""

    def test_aggregate_calc_range_is_having(self):
        import engine
        import findings
        import v3_sheet_plan as P
        from v3_local import preview_context
        ir, b, s = corpus('Superstore_Tableau2024_3.twbx', 'Sale Map')
        lo, hi = 0.1, 0.25                              # tighter than the saved range so the filter bites
        s = dict(s, applied_filters=[dict(f, min=lo, max=hi) if f['caption'] == 'Profit Ratio' else f
                                     for f in s['applied_filters']])
        with preview_context(ir, b):
            with engine.field_scope(s):
                raw = engine.q(f'SELECT * FROM {engine.tbl(s)}')
                st, ctry = col(engine, 'State/Province'), col(engine, 'Country/Region')
                od, pr, sa = col(engine, 'Order Date'), col(engine, 'Profit'), col(engine, 'Sales')
            df = P.execute(P.from_sheet(s, ir), s, engine)
        import pandas as pd
        d = pd.to_datetime(raw[od])
        raw = raw[(d >= '2021-01-03') & (d <= '2024-12-30')]
        g = raw.groupby([st, ctry])
        ratio = g[pr].sum() / g[sa].sum()
        want = ratio[(ratio >= lo) & (ratio <= hi)]
        self.assertEqual(len(df), len(want))
        self.assertTrue(df['M|usr|Profit Ratio'].between(lo, hi).all())
        self.assertNotIn('filter-not-pushed', [f['code'] for f in findings.for_sheet('Sale Map')
                                                if 'Profit Ratio' in f['message']])


    def test_saved_bounds_keep_the_boundary_state(self):
        """Tableau saved the slider's bounds as the data's own min/max (Ohio is the minimum); a different float
        summation order must not drop it. Tableau shows 58 of 59 states (New Brunswick, -0.33, is outside)."""
        import engine
        import v3_sheet_plan as P
        from v3_local import preview_context
        ir, b, s = corpus('Superstore_Tableau2024_3.twbx', 'Sale Map')
        with preview_context(ir, b):
            df = P.execute(P.from_sheet(s, ir), s, engine)
        self.assertEqual(len(df), 58)
        self.assertIn('Ohio', set(df['D|none|State/Province']))
        self.assertNotIn('New Brunswick', set(df['D|none|State/Province']))


class RendererWhere(unittest.TestCase):
    """E06b: the dashboard renderers' own WHERE (engine._apply_sheet_filters, used by every chart kind) applies
    measure filters too -- the marks whose aggregate passes, after the dimension filters -- not a warning."""

    def where_rows(self, workbook, name, select):
        import engine
        import findings
        from v3_local import preview_context
        ir, b, s = corpus(workbook, name)
        with preview_context(ir, b):
            with engine.field_scope(s):
                T = engine.tbl(s)
                w = engine._apply_sheet_filters(s, '', T)
                n = engine.q(f'SELECT COUNT(*) AS N FROM (SELECT {select} FROM {T} {w} GROUP BY 1) x')['N'].iloc[0]
        codes = [f['code'] for f in findings.for_sheet(name)]
        return int(n), codes

    def test_sale_map(self):
        n, codes = self.where_rows('Superstore_Tableau2024_3.twbx', 'Sale Map', 'STATE_PROVINCE')
        self.assertEqual(n, 58)
        self.assertNotIn('measure-filter-not-applied', codes)

    def test_hours_to_do_taxes(self):
        import engine
        n, codes = self.where_rows('World_Indicators_Tableau2024_3.twbx', 'Hours to do Taxes',
                                   "COUNTRY_REGION || '|' || REGION")
        self.assertEqual(n, 185)
        self.assertNotIn('measure-filter-not-applied', codes)


class RowLevelCalcDimensionFilter(unittest.TestCase):
    """E06c: a categorical filter (IN / NOT IN) on a ROW-LEVEL calculated dimension (a group, a cluster, a CASE)
    is a dimension filter: Tableau applies it to the rows. Only true/false filters on calculations were pushed; the
    rest were dropped as 'aggregate-level' with every member shown."""

    def test_world_indicators_cluster_exclusion(self):
        import engine
        import findings
        import v3_sheet_plan as P
        from v3_local import preview_context
        ir, b, s = corpus('World_Indicators_Tableau2024_3.twbx', 'Ease of Biz Correlation Sheet')
        self.assertEqual([(f['kind'], f['values']) for f in s['applied_filters']], [('not_in', ['Not Clustered'])])
        with preview_context(ir, b):
            got = P.execute(P.from_sheet(s, ir), s, engine)
            every = P.execute(P.from_sheet(s, ir), dict(s, applied_filters=[]), engine)
        c = next(c for c in got.columns if 'clusters' in c)
        want = every[every[c] != 'Not Clustered']
        self.assertLess(len(want), len(every))
        self.assertEqual(len(got), len(want))
        self.assertNotIn('Not Clustered', set(got[c]))
        self.assertNotIn('filter-not-pushed', [f['code'] for f in findings.for_sheet(s['name'])])

    def test_superstore_ship_status(self):
        import engine
        import pandas as pd
        import v3_sheet_plan as P
        from v3_local import preview_context
        ir, b, s = corpus('Superstore_Tableau2024_3.twbx', 'Sale Map')
        fid = next(k for k, e in ir['fields'].items() if e.get('caption') == 'Ship Status')
        with preview_context(ir, b):
            with engine.field_scope(s):
                raw = engine.q(f'SELECT * FROM {engine.tbl(s)}')
                st, ctry = col(engine, 'State/Province'), col(engine, 'Country/Region')
                od, sd, sm = col(engine, 'Order Date'), col(engine, 'Ship Date'), col(engine, 'Ship Mode')
                pr, sa = col(engine, 'Profit'), col(engine, 'Sales')
            runs = {}
            for kind, values in (('in', ['Shipped Late']), ('not_in', ['Shipped Early'])):
                f = {'caption': 'Ship Status', 'kind': kind, 'values': values, 'context': False, 'field_id': fid,
                     'datasource_id': s['datasource_id']}
                runs[kind] = P.execute(P.from_sheet(s, ir), dict(s, applied_filters=[f]), engine)
        days = (pd.to_datetime(raw[sd]) - pd.to_datetime(raw[od])).dt.days
        sched = raw[sm].map({'Same Day': 0, 'First Class': 1, 'Second Class': 3, 'Standard Class': 6})
        status = pd.Series('Shipped Early', index=raw.index).mask(days == sched, 'Shipped On Time').mask(
            days > sched, 'Shipped Late')
        for kind, keep in (('in', status == 'Shipped Late'), ('not_in', status != 'Shipped Early')):
            g = raw[keep].groupby([st, ctry])
            ratio = g[pr].sum() / g[sa].sum()
            df = runs[kind].set_index(['D|none|State/Province', 'D|none|Country/Region'])['M|usr|Profit Ratio']
            self.assertEqual(len(df), len(ratio), kind)
            for k, v in ratio.items():
                self.assertAlmostEqual(df[k], v, places=9, msg=(kind, k))


if __name__ == '__main__':
    unittest.main()
