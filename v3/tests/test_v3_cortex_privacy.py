"""AI00.1c: minimum task metadata; no live service calls."""
import json
import tempfile
import unittest
from copy import deepcopy
import xml.etree.ElementTree as ET
import cortex_assist as CA
from test_v3_cortex_assist import fixture, parse, Session
from test_v3_cortex_contract import independent_fixture, IndependentSession

MARKER = 'UNRELATED_PRIVATE_METADATA'

def private_fixture(independent=False, renamed=False):
    root = independent_fixture(renamed) if independent else fixture(reverse=renamed, calculated=True)
    for ds in root.findall('./datasources/datasource'):
        ET.SubElement(ds, 'column', name='[' + MARKER + ']', caption=MARKER,
                      datatype='real', role='measure')
    return root

class PrivacyTests(unittest.TestCase):
    def test_only_referenced_fields_reach_calculation_prompt(self):
        for independent in (False, True):
            for renamed in (False, True):
                with self.subTest(independent=independent, renamed=renamed), tempfile.TemporaryDirectory() as folder:
                    ir = parse(private_fixture(independent, renamed), folder)
                    before = deepcopy(ir)
                    self.assertIn(MARKER, json.dumps(ir['fields']))
                    session = IndependentSession() if independent else Session()
                    result = CA.assist(ir, session)
                    self.assertTrue(all(r['status']=='SUBSTITUTED' for r in result['ai_assistance']), result['ai_assistance'])
                    self.assertEqual(ir, before)
                    for _, params in session.calls:
                        self.assertNotIn(MARKER, params[1])
                        evidence = json.loads(params[1].split('\n',1)[1])['source_evidence']
                        if 'formula' in evidence:
                            self.assertEqual(set(evidence), {'formula','fields','grammar'})
                            self.assertEqual(len(evidence['fields']),1)
                        else:
                            self.assertEqual(set(evidence), {'source_mark','aggregation','dimension_count','measure_count','display_limit','display_order'})
                            self.assertNotIn('source_formula', json.dumps(evidence))
                    for sheet in result['dashboards'][0]['sheets']:
                        CA.validate(sheet, result['fields'])

    def test_visual_projection_cannot_forward_extra_contract_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(fixture(), folder)
            plan = CA.candidate(ir['dashboards'][0]['sheets'][0], ir['fields'])
            expected = CA.visual_evidence(plan)
            plan['future_private_property'] = MARKER
            plan['filter_plan']['private_property'] = MARKER
            self.assertEqual(CA.visual_evidence(plan), expected)
            self.assertNotIn(MARKER, json.dumps(expected))

if __name__ == '__main__':
    unittest.main()
