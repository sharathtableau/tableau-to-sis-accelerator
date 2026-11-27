"""Original UI reuse keeps source execution and scoped intake authoritative."""
from pathlib import Path
import sys,unittest,io,zipfile
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import engine

class PresentationTests(unittest.TestCase):
 def test_negative_gate_literal_arrow_is_preserved(self):
  # Release 27 (F1): the authored literal space is kept -- Tableau Cloud prints '▲ 3.5%' (see test_v3_sheet_capture)
  self.assertEqual(engine.fmt_tableau(.009,{'tableau_format':'*▼ 0.0%'}),'▼ 0.9%')
  self.assertEqual(engine.fmt_tableau(.035,{'tableau_format':'*▲ 0.0%'}),'▲ 3.5%')
 def test_signed_percentage_without_glyph_keeps_sign(self):
  self.assertEqual(engine.fmt_tableau(-.009,{'tableau_format':'0.0%'}),'-0.9%')
 def test_null_prior_period_does_not_show_nan(self):
  self.assertEqual(engine.fmt_tableau(float('nan'),{'tableau_format':'0.0%'}),'—')
 def test_native_card_reuses_metric_and_negative_badge(self):
  sheet={'name':'Renamed metric','_presentation':'native','measures':[{'caption':'Current','fmt':'num0'},{'caption':'Negative %','tableau_format':'*▼ 0.0%'}],'label_runs':[{'text':'Title'},{'field':'Current'}]}
  with patch.object(engine,'tbl',return_value='T'),patch.object(engine,'resolve_measure',return_value={'sql':'1','fmt':'num0'}),patch.object(engine,'q',return_value=pd.DataFrame({'M0':[10694],'M1':[.009]})),patch.object(engine.st,'metric') as metric,patch.object(engine.st,'markdown') as html:
   engine.r_kpi(sheet,'')
  self.assertEqual(metric.call_args.args[:2],('Current','10,694'));self.assertEqual(metric.call_args.kwargs['delta'],'-0.9%');html.assert_not_called()
 def test_native_presentation_copies_sheets_and_does_not_mutate_source(self):
  source={'name':'Unseen','sheets':[{'name':'Any sheet'}]}
  with patch.object(engine,'_render_dashboard_impl') as renderer:
   engine.render_dashboard(source,presentation='native')
  self.assertEqual(source,{'name':'Unseen','sheets':[{'name':'Any sheet'}]})
  self.assertEqual(renderer.call_args.args[0]['sheets'][0]['_presentation'],'native')
 def test_complex_kpi_composition_retains_authored_label(self):
  sheet={'name':'Renamed compound','_presentation':'native','measures':[{'caption':'One'},{'caption':'Two'}],'label_runs':[{'text':'Context: '},{'field':'One'},{'text':' / '},{'field':'Two'}]}
  with patch.object(engine,'tbl',return_value='T'),patch.object(engine,'resolve_measure',return_value={'sql':'1','fmt':'num0'}),patch.object(engine,'q',return_value=pd.DataFrame({'M0':[10],'M1':[20]})),patch.object(engine.st,'metric') as metric,patch.object(engine.st,'markdown') as html:
   engine.r_kpi(sheet,'')
  metric.assert_not_called();self.assertIn('Context:',html.call_args.args[0]);self.assertIn('20',html.call_args.args[0])
 def test_cloud_dashboard_view_returns_to_same_intake_without_another_fetch(self):
  from streamlit.testing.v1 import AppTest
  from test_v3_local import XML,CSV
  archive=io.BytesIO()
  with zipfile.ZipFile(archive,'w') as z:z.writestr('book.twb',XML);z.writestr('readings.csv',CSV)
  with patch('tableau_server.fetch_workbook',return_value={'filename':'remote.twbx','bytes':archive.getvalue(),'workbook_id':'id'}) as fetch:
   at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'v3_app.py'),default_timeout=30).run()
   at.radio[0].set_value('Tableau Cloud').run();at.radio[1].set_value('Direct workbook or view link').run();at.text_input[0].set_value('https://example.test/book').run()
   next(b for b in at.button if b.label=='Pull and assess workbook').click().run()
   next(b for b in at.button if b.label=='Open dashboard').click().run()
   self.assertFalse(at.exception);self.assertFalse(at.error);self.assertFalse(any('Assessment:' in s.value for s in at.subheader));self.assertTrue((at.get('vega_lite_chart') or at.get('arrow_vega_lite_chart')))
   next(b for b in at.button if b.label=='Back to migration workspace').click().run()
   self.assertFalse(at.exception);self.assertTrue(any('Assessment:' in s.value for s in at.subheader));self.assertEqual(fetch.call_count,1)
   self.assertEqual(at.radio[0].value,'Tableau Cloud');self.assertEqual(at.radio[1].value,'Direct workbook or view link');self.assertEqual(at.text_input[0].value,'https://example.test/book')
 def test_independent_quantity_delta_is_negative_in_default_period(self):
  df=pd.read_csv('reports/demo_ecommerce_20261002/packaged/TEMP_0xfnzak033ztnc13wohgv0xghqq0.csv',low_memory=False)
  dates=pd.to_datetime(df.event_date);age=(dates.max()-dates).dt.days;valid=~df.is_refunded.astype(str).str.lower().isin(['true','1'])
  current=df.loc[valid & age.between(0,20),'quantity'].sum();prior=df.loc[valid & age.between(21,41),'quantity'].sum()
  self.assertEqual(current,10694);self.assertLess((current-prior)/prior,0);self.assertEqual(f'{(current-prior)/prior:+.1%}','-0.9%')

class ResetAndExportTests(unittest.TestCase):
 def test_action_widget_uses_saved_state_without_competing_default(self):
  import altair as alt,dashboard_actions as actions,contextlib
  from types import SimpleNamespace
  from test_v3_dashboard_actions import AllFieldsActionTests
  fixture=AllFieldsActionTests();root,dash,fields=fixture.fixture('datetime','[ds].[tdy:key:qk]');action=fixture.action(root,dash,fields)
  ir={'source_file':'unseen.twb'};key=actions.state_key(ir,action);control=key+'_chart_values';state={key:[],control:[]};controls=[]
  host=SimpleNamespace(session_state=state,altair_chart=lambda *a,**k:None,popover=lambda label:contextlib.nullcontext(),multiselect=lambda *a,**kw:controls.append(kw))
  chart=alt.Chart(pd.DataFrame({'T':pd.to_datetime(['2025-01-01']),'V':[1]})).mark_bar().encode(x='T:T',y='V:Q')
  actions.chart(chart,dash['sheets'][0],'T',SimpleNamespace(IR=ir,st=host))
  self.assertIsNone(controls[0]['default']);self.assertEqual(controls[0]['key'],control)
  state[control]=['2025-01-01'];controls[0]['on_change']();self.assertEqual(state[key],['2025-01-01'])
 def test_standalone_scoped_entry_uses_the_same_native_presentation(self):
  import contextlib
  from contextlib import ExitStack
  ir={'field_scopes':{},'dashboards':[{'name':'Unseen','title':'Unseen','sheets':[]}]}
  with ExitStack() as stack:
   for name in ['configure','_render_param_controls','_render_findings']:stack.enter_context(patch.object(engine,name))
   for name in ['set_page_config','title']:stack.enter_context(patch.object(engine.st,name))
   stack.enter_context(patch.object(engine.st,'tabs',return_value=[contextlib.nullcontext()]))
   render=stack.enter_context(patch.object(engine,'render_dashboard'))
   engine.run(ir)
  render.assert_called_once_with(ir['dashboards'][0],presentation='native')

if __name__=='__main__':unittest.main()
