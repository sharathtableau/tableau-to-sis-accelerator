"""Authored canvas geometry and compact chart chrome remain structural."""
import sys, unittest, tempfile
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
import xml.etree.ElementTree as ET
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import engine
from test_v3_field_identity import parse, UI, workbook

class LayoutFidelityTests(unittest.TestCase):
 def test_fixed_canvas_survives_parser(self):
  root=workbook();d=root.find('.//dashboard');ET.SubElement(d,'size',maxheight='720',minheight='720',maxwidth='1440',minwidth='1440',**{'sizing-mode':'fixed'})
  with tempfile.TemporaryDirectory() as folder:ir=parse(root,folder)
  self.assertEqual(ir['dashboards'][0]['canvas'],{'maxheight':720,'minheight':720,'maxwidth':1440,'minwidth':1440,'sizing_mode':'fixed'})
 def test_layout_scale_uses_authored_fixed_height_without_mutating_ir(self):
  source={'dir':'vert','w':100000,'children':[{'sheet':'Renamed','h':10000,'w':40000}]}
  out=engine._layout_with_canvas(source,{'minheight':900,'maxheight':900})
  self.assertAlmostEqual(out['children'][0]['_zone_px']*10000,90)
  self.assertNotIn('_zone_px',source['children'][0])
  dynamic=engine._layout_with_canvas(source,{'minheight':600,'maxheight':900})
  self.assertEqual(dynamic['_zone_px'],engine._ZONE_PX)
 def chart(self,extra):
  ui=UI();s={'name':'Renamed calendar','x':{'caption':'Date','datepart':'tdy'},'y':{'caption':'Amount'},'color':{'caption':'Grouping'},**extra}
  with ExitStack() as stack:
   for name,value in {'st':ui,'tbl':lambda s:'T','px':lambda cap:cap,'resolve_measure':lambda *a:{'sql':'SUM(V)','fmt':'int'},'flag_dim':lambda *a:('G','group'),'q':lambda sql:pd.DataFrame({'T':['2025-01-01','2025-01-02'],'VAL':[10,20],'FLAG':['A','B']}),'value_labels':lambda *a:None,'cat_colors':lambda *a:(None,None)}.items():stack.enter_context(patch.object(engine,name,value))
   engine._timeseries_one(s,'',False,bar=True)
  return ui.charts[0].to_dict()
 def test_calendar_honors_zone_height_hidden_axes_and_absent_legend(self):
  chart=self.chart({'_compact':True,'_hpx':78,'_legends':[],'hide_axis':['rows']})
  self.assertEqual(chart['height'],78);self.assertIsNone(chart['encoding']['y']['axis']);self.assertIsNone(chart['encoding']['color']['legend'])
 def test_standalone_calendar_retains_default_chrome(self):
  chart=self.chart({});self.assertEqual(chart['height'],200)
  self.assertNotIn('legend',chart['encoding']['color']);self.assertIsNotNone(chart['encoding']['y'].get('axis'))
 def test_kpi_fixed_zone_rebuilds_balanced_visible_lines(self):
  html=[];s={'name':'Renamed KPI','_hpx':83,'measures':[{'caption':'Amount','fmt':'int'}],'label_runs':[{'text':'Metric title'},{'text':'\n\n\n'},{'field':'Amount','fontsize':'14','bold':'true'},{'text':'\n\n'},{'text':'vs. prior period','fontsize':'8'},{'text':'\n\n'}]}
  with patch.object(engine,'tbl',lambda s:'T'),patch.object(engine,'resolve_measure',lambda *a:{'sql':'1','fmt':'int'}),patch.object(engine,'q',lambda sql:pd.DataFrame({'M0':[1234]})),patch.object(engine.st,'markdown',lambda body,**kw:html.append(body)):
   engine.r_kpi(s,'')
  body=''.join(html);self.assertIn('min-height:83px',body);self.assertNotIn('<br>',body)
  self.assertEqual(body.count('<span'),body.count('</span>'));self.assertIn('1,234',body);self.assertIn('vs. prior period',body)

if __name__=='__main__':unittest.main()
