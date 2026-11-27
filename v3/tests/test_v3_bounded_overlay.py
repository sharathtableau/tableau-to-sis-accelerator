import unittest
from copy import deepcopy
import tableau_parser,bounded_overlay,dashboard_actions,worksheet_semantics

class OverlayTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.ir=tableau_parser.build_ir('Workbooks/ECommerce_Live_Relationship_Events_Customers_Products.twbx',scoped=True)
  cls.sheets={s['name']:s for d in cls.ir['dashboards'] for s in d['sheets']}
 def test_eight_families_are_source_mapped_and_actions_ready(self):
  names=['Country','Age Band','Channel','Segment','Payment Method','Prod. Qty','Prod. Revenue','CLV']
  for name in names:
   s=self.sheets[name];p=s['worksheet_semantics']['projection']
   self.assertEqual(p['status'],'MAPPED',name);self.assertEqual(p['renderer'],'bounded-overlay')
   self.assertEqual(set(p['bindings']),{o['id'] for o in s['worksheet_semantics']['occurrences']})
   self.assertFalse(worksheet_semantics.validate_sheet(s),name)
   self.assertEqual(len([a for a in s['dashboard_actions'] if a['source']==name and a['status']=='READY']),1)
 def test_renamed_worksheet_uses_same_structural_plan(self):
  for name in ['Country','Prod. Qty']:
   s=deepcopy(self.sheets[name]);s['name']='Unseen worksheet';self.assertEqual(bounded_overlay.build(s,s['worksheet_semantics']),s['worksheet_semantics']['projection'])
 def test_duplicate_axis_ownership_is_required(self):
  for name in ['Country','Prod. Qty']:
   s=deepcopy(self.sheets[name]);m=s['worksheet_semantics']
   for p in m['panes']:p['attributes'].pop('x-index',None);p['attributes'].pop('y-index',None)
   self.assertEqual(bounded_overlay.build(s,m)['status'],'REQUIRES_PLAN')
 def test_unconsumed_tooltip_is_blocked(self):
  s=deepcopy(self.sheets['Prod. Qty']);m=s['worksheet_semantics'];o=deepcopy(m['occurrences'][0]);o['id']='extra';o['channel']='detail';m['occurrences'].append(o)
  self.assertEqual(bounded_overlay.build(s,m)['status'],'REQUIRES_PLAN')
 def test_generated_filter_requires_matching_declared_action(self):
  s=deepcopy(self.sheets['Prod. Qty']);m=s['worksheet_semantics'];m['declared_filter_actions']={}
  self.assertEqual(bounded_overlay.build(s,m)['status'],'REQUIRES_PLAN')
 def test_parameter_label_cannot_introduce_physical_group(self):
  s=deepcopy(self.sheets['Country']);m=s['worksheet_semantics'];o=next(o for o in m['occurrences'] if o['channel']=='text' and o['level']=='row')
  defs=bounded_overlay.definitions(m,s['datasource_id']);defs[o['field_definition']['physical']]="CASE [Parameters].[Parameter 2] WHEN 1 THEN [country] END"
  self.assertIsNone(bounded_overlay.parameter_scalar(o,m,defs))

if __name__=='__main__':unittest.main()
