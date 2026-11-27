"""R47b: Superstore 2024.3 'Sample - Superstore' (Orders / People / Returns, an Excel relationship model) showed
'Needs explicit binding -- BINDING_CHECK_FAILED' on every run (live report, 26 Nov).
Cause: Tableau 2024+ writes a metadata record's owner as <_.fcp.ObjectModelEncapsulateLegacy.true...object-id>
(a feature-flag prefix). The loaded-tables model read only <object-id>, so no field had an owning table, the
relationship keys were 'not in the loaded data', and the model was never found or loaded per table. The code shown
was the generic one because the only recorded reason was 'not a Snowflake connection'.
Rule: the owner is read from either spelling (one helper for every reader); a file / extract source with no
Snowflake connection shows DATA_NOT_IN_SNOWFLAKE (load it, or bind a table), not a failed check.
Proof: Superstore 2024.3 (prefixed tags) through owners -> shape -> plan -> the Snowflake check on DuckDB; the
R1 Superstore upload (plain tags) as the second workbook gives the same owners."""
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
SS24 = ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx'
R1 = ROOT / 'Workbooks' / 'R1_Test_Upload_-_Superstore.twbx'
CAP = 'Sample - Superstore'


class Helper(unittest.TestCase):
    def test_both_spellings(self):
        import init_workbook as IW
        plain = ET.fromstring('<metadata-record class="column"><object-id>[Orders_1]</object-id></metadata-record>')
        flagged = ET.fromstring('<metadata-record class="column"><_.fcp.ObjectModelEncapsulateLegacy.true...object-id>'
                                '[Orders_1]</_.fcp.ObjectModelEncapsulateLegacy.true...object-id></metadata-record>')
        self.assertEqual(IW.metadata_object_id(plain), 'Orders_1')
        self.assertEqual(IW.metadata_object_id(flagged), 'Orders_1')
        self.assertEqual(IW.metadata_object_id(ET.fromstring('<metadata-record/>')), '')

    def test_file_source_reason(self):
        import v3_console as C
        result = {'source_reports': [{'caption': 'S', 'reason': 'Automatic binding requires exactly one authored '
                                                                 'Snowflake connection'}]}
        self.assertTrue(C._binding_reason(result, 'S').startswith(' — DATA_NOT_IN_SNOWFLAKE'))


def model_for(path):
    import plan_census as PC
    import tableau_parser as TP
    import v3_data_entities as ENT
    import v3_data_load as DL
    r = PC.intake(path)
    root = TP.load_twb_xml(str(path))
    raw = path.read_bytes()
    did, ds = DL._ds_by_caption(root, r['ir'], CAP)
    return r, root, raw, did, ds, ENT.owners(ds, r['ir'], did)


@unittest.skipUnless(SS24.exists(), 'corpus workbook')
class Superstore2024(unittest.TestCase):
    def test_owners_found_and_model_checks_at_fact_grain(self):
        import duckdb
        import v3_data_entities as ENT
        import v3_data_load as DL
        from test_v3_dm8_relationship_semantics import DuckSession
        r, root, raw, did, ds, own = model_for(SS24)
        self.assertGreater(len(own), 20)
        self.assertEqual({o['owner'].split('_')[0] for o in own.values()}, {'Orders', 'People', 'Returns'})
        loc = DL.locate(raw, root, r['ir'], CAP)
        frames = ENT.frames_for(raw, loc)
        shaped, _notes, own2, ds2 = ENT.shape(root, r['ir'], loc, frames, False)
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS WBR_DB")
        con.execute('CREATE SCHEMA WBR_DB.PIPELINE_DEMO')
        tables = {}
        for oid, e in shaped.items():
            con.register('f', e['frame'])
            con.execute(f"CREATE TABLE WBR_DB.PIPELINE_DEMO.{e['target']} AS SELECT * FROM f")
            con.unregister('f')
            tables[oid] = f"WBR_DB.PIPELINE_DEMO.{e['target']}"
        plan = ENT.plan_for(ds2, own2, tables, {oid: set(e['frame'].columns) for oid, e in shaped.items()})
        self.assertEqual(len(plan['keys']), 2)
        checks = ENT.check(DuckSession(con), plan)
        self.assertEqual(checks['fact_rows'], checks['output_rows'])
        self.assertEqual(checks['fact_rows'], len(frames[plan['root']]))


@unittest.skipUnless(R1.exists() and SS24.exists(), 'corpus workbooks')
class SecondWorkbook(unittest.TestCase):
    def test_plain_tags_give_the_same_owners(self):
        own_old = model_for(R1)[-1]
        own_new = model_for(SS24)[-1]
        self.assertGreater(len(own_old), 20)
        pick = lambda own: {k: (o['owner'].split('_')[0], o['remote']) for k, o in own.items()}
        common = set(pick(own_old)) & set(pick(own_new))
        self.assertGreater(len(common), 15)
        self.assertEqual({k: pick(own_old)[k] for k in common}, {k: pick(own_new)[k] for k in common})


if __name__ == '__main__':
    unittest.main()
