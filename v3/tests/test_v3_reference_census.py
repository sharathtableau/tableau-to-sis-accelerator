"""tools/reference_census.py: Tableau's own numbers (REST view data) against the app, for every workbook of a Tableau
Cloud project -- corpus-wide 'equal to Tableau' counts without a browser. Tableau calls are injected so the tests never
reach Tableau; the comparison is v3_reference.compare_view (one sheet per view or 'not comparable', never guessed).
Written BEFORE the tool (tests first)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import reference_census as RC  # noqa: E402

SITE = {'projects': [{'id': 'p1', 'name': 'Tableau to Streamlit'}, {'id': 'p2', 'name': 'Other'}],
        'workbooks': [{'id': 'w1', 'name': 'Sales', 'project_id': 'p1'}, {'id': 'w2', 'name': 'X', 'project_id': 'p2'},
                      {'id': 'w3', 'name': 'Broken', 'project_name': 'Tableau to Streamlit'}]}


class Selection(unittest.TestCase):
    def test_workbooks_of_the_project_only(self):
        self.assertEqual([w['id'] for w in RC.project_workbooks(SITE, 'Tableau to Streamlit')], ['w1', 'w3'])
        self.assertEqual(RC.project_workbooks(SITE, 'Nope'), [])


class Run(unittest.TestCase):
    def test_statuses_per_view_and_failures_as_codes(self):
        def fetch(wid, name):
            if wid == 'w3':
                raise RuntimeError('secret token text 401')
            return {'filename': name + '.twbx', 'bytes': b'x', 'published_views': ['V1', 'V2']}

        def compare(fetched, wid):
            return [{'View': 'V1', 'Sheet': 'S1', 'Status': 'Match', 'Matched values': 4, 'Mismatched values': 0,
                     'Rows missing in app': 0, 'Note': ''},
                    {'View': 'V2', 'Sheet': '—', 'Status': 'Not comparable', 'Matched values': 0,
                     'Mismatched values': 0, 'Rows missing in app': 0, 'Note': 'no sheet produces exactly these'}]
        rows = RC.run(RC.project_workbooks(SITE, 'Tableau to Streamlit'), fetch, compare, log=lambda m: None)
        self.assertEqual([(r['workbook'], r['View'], r['Status']) for r in rows],
                         [('Sales', 'V1', 'Match'), ('Sales', 'V2', 'Not comparable'),
                          ('Broken', '(workbook)', 'WORKBOOK_FAILED:RuntimeError')])
        self.assertNotIn('secret', repr(rows))

    def test_report(self):
        rows = [{'workbook': 'A', 'View': 'v1', 'Status': 'Match', 'Matched values': 3, 'Mismatched values': 0},
                {'workbook': 'A', 'View': 'v2', 'Status': 'Difference found', 'Matched values': 1, 'Mismatched values': 2},
                {'workbook': 'B', 'View': 'v3', 'Status': 'Not comparable', 'Matched values': 0, 'Mismatched values': 0}]
        md = RC.report(rows, 'Tableau to Streamlit')
        self.assertIn('Views: 3 · Match 1 · Difference found 1 · Not comparable 1', md)
        self.assertIn('| A | 2 | 1 | 1 | 0 |', md)
        self.assertIn('Difference found', md.split('## Differences')[1])


class Source(unittest.TestCase):
    """A workbook with no Snowflake table compares on its OWN bundled data (the extract Tableau Cloud queries), on
    local DuckDB with the Snowflake session set aside for that workbook only (always restored)."""

    def test_choose_source(self):
        self.assertEqual(RC.choose_source({'A': 'DB.S.T', 'B': '/tmp/x.csv'}), ('snowflake', {'A': 'DB.S.T'}))
        self.assertEqual(RC.choose_source({'A': '/tmp/x.csv', 'B': None}), ('bundled', {'A': '/tmp/x.csv'}))
        self.assertEqual(RC.choose_source({'A': None}), (None, {}))

    def test_session_is_set_aside_and_restored(self):
        import backend
        sentinel = object()
        seen = []
        backend.set_session(sentinel)
        try:
            with RC.local_only():
                seen.append(backend._active_session())
            self.assertIsNone(seen[0])
            self.assertIs(backend._active_session(), sentinel)
            with self.assertRaises(RuntimeError):
                with RC.local_only():
                    raise RuntimeError('boom')
            self.assertIs(backend._active_session(), sentinel)
        finally:
            backend.set_session(None)


    def test_snowpark_global_session_is_hidden_too(self):
        """A Session made with Session.builder becomes Snowpark's global active session; backend falls back to it."""
        import backend
        from unittest.mock import patch
        sentinel = object()
        with patch('snowflake.snowpark.context.get_active_session', return_value=sentinel):
            self.assertIs(backend._active_session(), sentinel)
            with RC.local_only():
                self.assertIsNone(backend._active_session())
            self.assertIs(backend._active_session(), sentinel)


class PlanPath(unittest.TestCase):
    """Views are compared on the plan path (v3_exact_loop.prove: the same comparison that verifies sheets against
    Tableau captures) -- the renderer capture returned nothing for planned sheets, so no view was ever comparable.
    Proof: Superstore_Tableau2024_3 'Overview' rebuilt as a REST view (plain captions, as Query View Data returns
    them) from Tableau's own capture of its Sale Map sheet."""

    def test_overview_matches_on_the_plan_path(self):
        import io, csv, json, re
        import engine
        import plan_census as PC
        import tableau_reference_bundles as B
        from v3_local import preview_context
        cap = json.loads(B.BUNDLES['Superstore_Tableau2024_3'])['captures']
        entry = next(e for e in (cap.values() if isinstance(cap, dict) else cap) if e.get('dashboard') == 'Overview')
        sheet = entry['sheets']['Sale Map']
        header = [re.sub(r'^(AGG|SUM|AVG|MIN|MAX)\((.*)\)$', r'\2', c) for c in sheet['columns']]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(sheet['rows'])
        r = PC.intake(ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx')
        with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
            rec = RC.compare_plan(r['ir'], {'view': 'Overview', 'csv': buf.getvalue()}, engine)
        self.assertEqual((rec['Status'], rec['Sheet']), ('Match', 'Sale Map'), rec)
        self.assertGreater(rec['Matched values'], 0)

    def test_rest_measure_spellings_map(self):
        """REST Query View Data names measures in plain words: SUM(x) / AGG(x) -> 'x', CNTD(x) -> 'Distinct count of
        x' (seen in the 11 Nov run). The Product, Customers and Shipping views rebuilt that way from Tableau's own
        captures must match (they showed 'Tableau columns the plan does not produce: Profit, Sales ...')."""
        import io, csv, json, re
        import engine
        import plan_census as PC
        import tableau_reference_bundles as B
        from v3_local import preview_context

        def rest(c):
            m = re.match(r'^(SUM|AGG)\((.*)\)$', c)
            if m:
                return m.group(2)
            m = re.match(r'^CNTD\((.*)\)$', c)
            return f'Distinct count of {m.group(1)}' if m else c

        cap = json.loads(B.BUNDLES['Superstore_Tableau2024_3'])['captures']
        by = {e['dashboard']: e for e in (cap.values() if isinstance(cap, dict) else cap)}
        r = PC.intake(ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx')
        got = {}
        with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
            for view, sheet in (('Product', 'ProductDetails'), ('Customers', 'CustomerOverview'),
                                ('Shipping', 'DaystoShip')):
                sh = by[view]['sheets'][sheet]
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow([rest(c) for c in sh['columns']])
                w.writerows(sh['rows'])
                rec = RC.compare_plan(r['ir'], {'view': view, 'csv': buf.getvalue()}, engine)
                got[view] = (rec['Status'], rec['Sheet'], rec['Mismatched values'], rec['Note'][:160])
        for view, sheet in (('Product', 'ProductDetails'), ('Customers', 'CustomerOverview'), ('Shipping', 'DaystoShip')):
            self.assertEqual(got[view][:3], ('Match', sheet, 0), got[view])

    def test_rest_date_part_spellings_map(self):
        """REST names a date part 'Month of Order Date' (the capture says MONTH(Order Date)); the 11 Nov full run
        showed Performance as 'Tableau columns the plan does not produce: Month of Order Date, Year of Order Date'."""
        import io, csv, json, re
        import engine
        import plan_census as PC
        import tableau_reference_bundles as B
        from v3_local import preview_context

        def rest(c):
            m = re.match(r'^(YEAR|QUARTER|MONTH|WEEK|DAY|WEEKDAY)\((.*)\)$', c)
            if m:
                return f'{m.group(1).title()} of {m.group(2)}'
            m = re.match(r'^(SUM|AGG)\((.*)\)$', c)
            return m.group(2) if m else c

        cap = json.loads(B.BUNDLES['Superstore_Tableau2024_3'])['captures']
        sh = next(e for e in cap.values() if e['dashboard'] == 'Performance')['sheets']['Performance']
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([rest(c) for c in sh['columns']])
        w.writerows(sh['rows'])
        self.assertIn('Month of Order Date', buf.getvalue().splitlines()[0])
        r = PC.intake(ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx')
        with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
            rec = RC.compare_plan(r['ir'], {'view': 'Performance', 'csv': buf.getvalue()}, engine)
        self.assertEqual((rec['Status'], rec['Sheet'], rec['Mismatched values']), ('Match', 'Performance', 0), rec)

    @unittest.skipUnless((ROOT / 'Workbooks' / 'cloud_20261010' / 'Fil_Test.twbx').exists(), 'cloud corpus workbook')
    def test_rest_date_part_second_workbook(self):
        """Second workbook: Fil Test 'Sheet 2' (MONTH(Order Date), SUM(Profit)) named as REST names it."""
        import engine
        import plan_census as PC
        import v3_sheet_plan as P
        from v3_local import preview_context
        months = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
                  'November', 'December']
        r = PC.intake(ROOT / 'Workbooks' / 'cloud_20261010' / 'Fil_Test.twbx')
        ir = r['ir']
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'Sheet 2')
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            df = P.execute(P.from_sheet(s, ir), s, engine)
            text = 'Month of Order Date,Profit\n' + ''.join(f'{months[int(a) - 1]},"{b:,.0f}"\n' for a, b in df.values)
            rec = RC.compare_plan(ir, {'view': 'Dashboard 1', 'csv': text}, engine)
        self.assertEqual((rec['Status'], rec['Sheet'], rec['Matched values']), ('Match', 'Sheet 2', 12), rec)


    def test_unavailable_reference_is_reported(self):
        rec = RC.compare_plan({'dashboards': []}, {'view': 'V', 'error': 'x 400081 y'}, None)
        self.assertEqual(rec['Status'], 'Tableau reference unavailable')


class NoCortex(unittest.TestCase):
    def test_no_cortex_and_read_only(self):
        src = (ROOT / 'tools' / 'reference_census.py').read_text(encoding='utf-8')
        for bad in ('prepare_payload(', 'assist(', 'AI_COMPLETE', 'INSERT ', 'CREATE '):
            self.assertNotIn(bad, src)


if __name__ == '__main__':
    unittest.main()
