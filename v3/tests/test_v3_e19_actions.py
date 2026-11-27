"""E19: dashboard actions beyond the original one-field contract. Written with the general contract.

Corpus 22 Nov: 200 actions, 69 READY (one linked field on a bar / calendar chart), 131 blocked. Rule (generic):
  * filter (tsc:tsl-filter) and highlight (tsc:brush) actions; select / hover / menu activation;
  * the source is one sheet, or every sheet of the dashboard but the excluded ones (one record per source);
  * targets: the target dashboard's sheets but the excluded ones, or one worksheet (Tableau navigates to it);
    a target on another dashboard opens it;
  * fields: explicit links (several, url-encoded, prefixed calendar grains, cross-datasource target fields),
    'all fields' (every row-level dimension the source shows), field captions ('MONTH(Order Date)' included);
  * filter targets get a code-built predicate (rows AND-ed over fields, OR-ed over picks); highlight targets keep
    every row and dim the marks not picked; an action that passes nothing (no linked field on its source, or a
    sheet that filters itself) is NO_OP with the reason;
  * the source sheet gets a small selection control listing its own values (queried by code; Streamlit in
    Snowflake cannot select layered charts).
The original 69 keep their exact behaviour. Proof: Superstore 2024.3 (Customers, Overview, Shipping) and World
Indicators (Tourism, Business)."""
import glob
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import dashboard_actions as DA  # noqa: E402

SS = ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx'
WI = ROOT / 'Workbooks' / 'World_Indicators_Tableau2024_3.twbx'


def actions(path):
    import tableau_parser as TP
    ir = TP.build_ir(str(path), scoped=True)
    return ir, [(d, a) for d in ir['dashboards'] for a in d.get('actions') or []]


class Coverage(unittest.TestCase):
    def test_corpus(self):
        import collections
        st, mode = collections.Counter(), collections.Counter()
        for f in sorted(glob.glob(str(ROOT / 'Workbooks' / '**' / '*.twb*'), recursive=True)):
            if f.endswith('.twbr'):
                continue
            try:
                _ir, acts = actions(f)
            except Exception:
                continue
            for _d, a in acts:
                st[a['status']] += 1
                if a['status'] == 'READY':
                    mode[a.get('mode', 'chart')] += 1
                else:
                    self.assertTrue(a.get('reason'), a)
        if not st:
            self.skipTest('corpus')
        self.assertEqual(mode['chart'], 69)                          # the original contract is unchanged
        self.assertGreaterEqual(st['READY'], 185)
        self.assertGreaterEqual(st['READY'] + st['NO_OP'], 225)        # NO_OP: Tableau's action changes no view
        self.assertLessEqual(st['BLOCKED'], 4)


@unittest.skipUnless(SS.exists(), 'corpus workbook')
class Superstore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import plan_census as PC
        cls.r = PC.intake(SS)
        cls.ir = cls.r['ir']
        cls.dash = {d['name']: d for d in cls.ir['dashboards']}

    def find(self, dash, kind, source):
        return next(a for a in self.dash[dash]['actions']
                    if a.get('mode') == 'control' and a['kind'] == kind and a['source'] == source)

    def test_shapes(self):
        f = self.find('Customers', 'filter', 'CustomerOverview')
        self.assertEqual(([e['caption'] for e in f['fields']], f['exclude'], f['target']),
                         (['Region'], ['CustomerOverview'], 'Customers'))
        hover = [a for a in self.dash['Customers']['actions'] if a.get('activation') == 'on-hover']
        self.assertEqual(sorted(a['source'] for a in hover), ['CustomerRank', 'CustomerScatter'])
        self.assertTrue(all(a['kind'] == 'highlight' for a in hover))

    def run_sheet_count(self, dash, sheet_name, state):
        import engine
        import streamlit as st
        from v3_local import preview_context
        sheet = next(s for s in self.dash[dash]['sheets'] if s['name'] == sheet_name)
        with preview_context(self.ir, {k: v for k, v in self.r['bindings'].items() if v}):
            for action, rows in state:
                st.session_state[DA.state_key(engine.IR, action)] = rows
            try:
                with engine.field_scope(sheet):
                    clauses = DA.predicates(sheet, engine)
                    T = engine.tbl(sheet)
                    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
                    got = int(engine.q(f'SELECT COUNT(*) AS N FROM {T}{where}').iloc[0, 0])
                    return clauses, got, T, engine
            finally:
                for action, _rows in state:
                    st.session_state.pop(DA.state_key(engine.IR, action), None)

    def test_filter_reaches_targets_not_the_excluded_source(self):
        f = self.find('Customers', 'filter', 'CustomerOverview')
        clauses, got, T, engine = self.run_sheet_count('Customers', 'CustomerScatter', [(f, [['East']])])
        self.assertEqual(len(clauses), 1)
        self.assertIn("= 'East'", clauses[0])
        import engine as E
        from v3_local import preview_context
        with preview_context(self.ir, {k: v for k, v in self.r['bindings'].items() if v}):
            want = int(E.q(f"SELECT COUNT(*) AS N FROM {T} WHERE REGION = 'East'").iloc[0, 0])
        self.assertEqual(got, want)
        clauses, _got, _T, _e = self.run_sheet_count('Customers', 'CustomerOverview', [(f, [['East']])])
        self.assertEqual(clauses, [])                                   # source / excluded sheet unfiltered

    def test_highlight_never_filters(self):
        h = self.find('Customers', 'highlight', 'CustomerScatter')
        clauses, _n, _T, _e = self.run_sheet_count('Customers', 'CustomerRank', [(h, [['Adam Bellavance']])])
        self.assertEqual(clauses, [])

    def test_highlight_dims_the_other_marks(self):
        import altair as alt
        import engine
        import pandas as pd
        import streamlit as st
        h = self.find('Customers', 'highlight', 'CustomerScatter')
        sheet = next(s for s in self.dash['Customers']['sheets'] if s['name'] == 'CustomerRank')
        df = pd.DataFrame({'D|none|Customer Name': ['Adam Bellavance', 'Bart Watters'], 'M|sum|Sales': [1.0, 2.0]})
        chart = alt.Chart(df).mark_bar().encode(x='M|sum|Sales:Q', y='D|none|Customer Name:N')
        old = engine.IR
        engine.IR = self.ir
        st.session_state[DA.state_key(self.ir, h)] = [['Adam Bellavance']]
        try:
            out = DA.emphasize(chart, sheet, engine)
        finally:
            st.session_state.pop(DA.state_key(self.ir, h), None)
            engine.IR = old
        self.assertEqual(list(out.data['__HL']), [True, False])
        self.assertIn('opacity', out.to_dict()['encoding'])

    def test_source_options_are_the_sheets_own_values(self):
        import engine
        from v3_local import preview_context
        f = self.find('Shipping', 'filter', 'ShippingTrend')
        sheet = next(s for s in self.dash['Shipping']['sheets'] if s['name'] == 'ShippingTrend')
        with preview_context(self.ir, {k: v for k, v in self.r['bindings'].items() if v}):
            with engine.field_scope(sheet):
                opts = DA.source_options(sheet, f, engine)
        self.assertTrue(opts and all(len(r) == 1 for r in opts))
        self.assertIn(['Shipped On Time'], opts)


@unittest.skipUnless(WI.exists(), 'corpus workbook')
class WorldIndicators(unittest.TestCase):
    def test_tourism_region_filter_and_business_all_fields(self):
        import engine
        import plan_census as PC
        import streamlit as st
        from v3_local import preview_context
        r = PC.intake(WI)
        dash = {d['name']: d for d in r['ir']['dashboards']}
        f = next(a for a in dash['Tourism']['actions'] if a.get('mode') == 'control' and a['source'] == 'Tourism By Country/Region')
        self.assertEqual([e['caption'] for e in f['fields']], ['Region'])
        target = next(s for s in dash['Tourism']['sheets'] if s['name'] == 'Tourism Over Time')
        with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
            st.session_state[DA.state_key(engine.IR, f)] = [['Europe']]
            try:
                with engine.field_scope(target):
                    clauses = DA.predicates(target, engine)
                    T = engine.tbl(target)
                    got = int(engine.q(f"SELECT COUNT(*) FROM {T} WHERE {' AND '.join(clauses)}").iloc[0, 0])
                    want = int(engine.q(f"SELECT COUNT(*) FROM {T} WHERE REGION = 'Europe'").iloc[0, 0])
            finally:
                st.session_state.pop(DA.state_key(engine.IR, f), None)
        self.assertEqual(got, want)
        b = [a for a in dash['Business']['actions'] if a.get('mode') == 'control']
        self.assertEqual(len(b), 4)
        self.assertTrue(all(len(a['fields']) == 3 for a in b))          # every row-level dimension of the source


class Parse(unittest.TestCase):
    def test_multi_field_predicate_and_literals(self):
        class E:
            IR = {'fields': {'d::A': {'caption': 'A', 'datasource_id': 'd'},
                             'd::D': {'caption': 'D', 'datasource_id': 'd', 'datatype': 'date'}}}

            @staticmethod
            def rdim(c):
                return c.upper()

            class st:
                session_state = {}
        action = {'id': 'x', 'mode': 'control', 'status': 'READY', 'kind': 'filter', 'source': 'S', 'target': 'Dash',
                  'exclude': [], 'fields': [
                      {'field_id': 'd::A', 'caption': 'A', 'datepart': None, 'source_caption': 'A'},
                      {'field_id': 'd::D', 'caption': 'D', 'datepart': ['trunc', 'MONTH'], 'source_caption': 'D'}]}
        sheet = {'name': 'T', 'datasource_id': 'd', 'action_dashboard': 'Dash', 'dashboard_actions': [action]}
        E.st.session_state[DA.state_key(E.IR, action)] = [["O'Neil", '2024-03-01'], [None, '2024-04-01']]
        got = DA.general_predicates(sheet, E)
        self.assertEqual(got, ["((A = 'O''Neil' AND CAST(DATE_TRUNC('month', D) AS DATE) = DATE '2024-03-01') OR "
                               "(A IS NULL AND CAST(DATE_TRUNC('month', D) AS DATE) = DATE '2024-04-01'))"])
        E.st.session_state[DA.state_key(E.IR, action)] = [['x', "2024-03-01'; DROP"]]
        with self.assertRaises(ValueError):
            DA.general_predicates(sheet, E)

    def test_worksheet_target_and_deselect_pattern_are_no_ops(self):
        import xml.etree.ElementTree as ET
        node = ET.fromstring(
            "<action caption='Deselect'><activation auto-clear='true' type='on-select'/>"
            "<source dashboard='D' type='sheet' worksheet='K'/>"
            "<link expression='tsl:K?%5Bd%5D.%5BC%5D~s0=&lt;[d].[C]~na&gt;'/>"
            "<command command='tsc:tsl-filter'><param name='target' value='K'/></command></action>")
        dash = {'name': 'D', 'sheets': [{'name': 'K', 'datasource_id': 'd'}]}
        out = DA._general(node, dash, {'D': dash}, {'d::C': {'caption': 'C', 'datasource_id': 'd'}}, 'base')
        self.assertEqual(out[0]['status'], 'NO_OP')
        self.assertIn('deselect', out[0]['reason'])


class Wiring(unittest.TestCase):
    def test_render_and_plan_use_it(self):
        self.assertIn('source_control', (ROOT / 'engine.py').read_text(encoding='utf-8'))
        self.assertIn('emphasize', (ROOT / 'v3_sheet_plan.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
