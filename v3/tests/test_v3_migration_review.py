import unittest,json
from copy import deepcopy
from unittest.mock import patch
import cortex_assist as C
import migration_review as M
class ReviewTests(unittest.TestCase):
 def fixture(self):
  return {'source_file':'private-workbook','rows':[{'secret':'sensitive'}], 'field_scopes':{},'dashboards':[{'name':'private-name','actions':[{'status':'READY'}],'sheets':[{'support':{'status':'UNSUPPORTED'},'datasource_id':'private-id','worksheet_semantics':{'datasource_xml':'<datasource><relationship/></datasource>'}}]}]}
 def test_minimized_evidence(self):
  ir=self.fixture();e,choices=M.evidence(ir);text=json.dumps(e)
  self.assertNotIn('private',text);self.assertNotIn('sensitive',text);self.assertEqual(e['counts']['relationship_sources'],1);self.assertIn('repair_visual_contracts',choices)
 def test_review_never_promotes_execution(self):
  ir=self.fixture();before=deepcopy(ir)
  with patch.object(M,'request',return_value={'choice':'repair_visual_contracts','reason':'Check marks'}):r=M.review(ir,object(),'model',C.ProposalContext(1))
  self.assertEqual(ir,before);self.assertEqual(r['status'],'REVIEWED');self.assertEqual(r['acceptance'],'NOT_VERIFIED');self.assertFalse(r['execution_changed'])
 def test_no_connection_and_budget(self):
  ctx=C.ProposalContext(0);ir=self.fixture()
  self.assertEqual(M.review(ir,None,'model',ctx)['status'],'UNAVAILABLE');self.assertEqual(ctx.attempts,0)
  r=M.review(ir,object(),'model',ctx);self.assertEqual(r['error_code'],'CALL_LIMIT');self.assertEqual(ctx.attempts,0)
 def test_refusal(self):
  with patch.object(M,'request',return_value={'choice':'refuse','reason':'Insufficient evidence'}):r=M.review(self.fixture(),object(),'model',C.ProposalContext(1))
  self.assertEqual(r['status'],'REFUSED')
if __name__=='__main__':unittest.main()
