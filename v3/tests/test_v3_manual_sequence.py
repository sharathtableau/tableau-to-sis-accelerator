import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as E
from test_v3_header_remainder import partial
from test_v3_more_visuals import parse,engine,ir_contract,preview_context
from test_v3_tables import TableUI
import crosstab_plan

class AuthoredManualSequenceTests(unittest.TestCase):
 def root(self,renamed=False):
  root=partial(renamed,True)
  for table in root.findall('./worksheets/worksheet/table'):
   d=table.find('manual-sort/dictionary');d.remove(d[-1])
   g=table.find('filter/groupfilter');g.set('{http://www.tableausoftware.com/xml/user}op','manual')
   members={json.loads(x.get('member')):x for x in g};prefix=[json.loads(x.text) for x in d]
   order=prefix+[t for t in members if t not in prefix];g[:]=[members[t] for t in order]
  return root
 def test_manual_sequence_in_renamed_and_independent_shapes(self):
  for renamed in [False,True]:
   with tempfile.TemporaryDirectory() as folder:
    ir=parse(self.root(renamed),folder);self.assertEqual(ir_contract.validate_ir(ir),[])
    s=ir['dashboards'][0]['sheets'][0];plan=crosstab_plan.build(s,s['worksheet_semantics'])
    self.assertEqual(plan['status'],'READY')
    lookup={o['id']:o for o in s['worksheet_semantics']['occurrences']}
    self.assertEqual([lookup[o]['aggregation'] for o in plan['measures']],['avg','sum','min'])
 def test_conflicting_prefix_and_unmarked_union_refuse(self):
  for change in ['prefix','unmarked','duplicate']:
   root=self.root();g=root.find('.//filter/groupfilter')
   if change=='prefix':g[:]=list(reversed(list(g)))
   elif change=='unmarked':g.attrib.pop('{http://www.tableausoftware.com/xml/user}op')
   else:g.append(E.fromstring(E.tostring(g[-1])))
   with tempfile.TemporaryDirectory() as f:
    s=parse(root,f)['dashboards'][0]['sheets'][0]
    self.assertIn(s.get('support',{}).get('status'),['UNSUPPORTED','INVALID'])
 def test_explicit_numeric_innermost_peers(self):
  root=self.root()
  for c in root.findall('.//column'):
   if c.get('name','').startswith('[Place'):c.set('datatype','integer')
  for n in root.findall('.//shelf-sort-v2'):n.set('is-on-innermost-dimension','true')
  with tempfile.TemporaryDirectory() as folder:
   ir=parse(root,folder);bindings={}
   for i in range(2):
    p=Path(folder)/f'{i}.csv';p.write_text(f'Place{i},Value,Extra\n1,7,3\n3,7,9\n2,8,4\n',encoding='utf-8');bindings[f'Unseen source {i}']=str(p)
   with preview_context(ir,bindings):
    for s in ir['dashboards'][0]['sheets']:
     ui=TableUI()
     with patch.object(engine,'st',ui):engine.render_sheet(s,'')
     self.assertEqual(ui.errors,[]);self.assertEqual(len(ui.tables),1)
     self.assertEqual(ui.tables[0].iloc[:,0].tolist(),[2,3,1])

if __name__=='__main__':unittest.main()
