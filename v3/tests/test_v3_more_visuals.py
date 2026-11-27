from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_v3_field_identity import workbook,parse,UI
from v3_local import preview_context
import engine,ir_contract,dual_plan,crosstab_plan


def fixture(kind,renamed=False,part='mn'):
    root=workbook(rename=renamed)
    for i,ws in enumerate(root.findall('./worksheets/worksheet')):
        ds=f'federated.source_{i}';table=ws.find('table');panes=table.find('panes');panes.clear()
        if kind=='dual':
            for parent in [root.find(f'./datasources/datasource[@name="{ds}"]'),table.find('datasource-dependencies')]:
                ET.SubElement(parent,'column',name='[When]',caption='Calendar' if not renamed else 'Renamed calendar',role='dimension',datatype='date')
            a=f'[{ds}].[sum:Value:qk]';b=f'[{ds}].[avg:Value:qk]'
            table.find('cols').text=f'[{ds}].[{part}:When:ok]';table.find('rows').text=f'({a} + {b})'
            for j,(token,mark) in enumerate([(a,'Bar'),(b,'Line')]):
                pane=ET.SubElement(panes,'pane',id=str(j+1),attrib={'y-axis-name':token});ET.SubElement(pane,'mark',attrib={'class':mark})
        else:
            a=f'[{ds}].[sum:Value:qk]';b=f'[{ds}].[avg:Value:qk]'
            table.find('cols').text=f'[{ds}].[:Measure Names]';table.find('rows').text=f'[{ds}].[none:Place{i}:nk]'
            pane=ET.SubElement(panes,'pane');ET.SubElement(pane,'mark',attrib={'class':'Text'})
            ET.SubElement(ET.SubElement(pane,'encodings'),'text',column=f'[{ds}].[Multiple Values]')
            f=ET.SubElement(table,'filter',column=f'[{ds}].[:Measure Names]',attrib={'class':'categorical'})
            g=ET.SubElement(f,'groupfilter',function='union')
            import json
            for token in [b,a]:ET.SubElement(g,'groupfilter',function='member',level='[:Measure Names]',member=json.dumps(token))
            manual=ET.SubElement(table,'manual-sort',column=f'[{ds}].[:Measure Names]',direction='ASC');dictionary=ET.SubElement(manual,'dictionary')
            for token in [a,b]:ET.SubElement(dictionary,'bucket').text=json.dumps(token)
            ET.SubElement(ET.SubElement(table,'shelf-sorts'),'shelf-sort-v2',attrib={'dimension-to-sort':table.find('rows').text,'measure-to-sort-by':a,'direction':'DESC','shelf':'rows','is-on-innermost-dimension':'true'})
    return root


class MoreVisualTests(unittest.TestCase):
    def execute(self,root,folder,data,kind):
        ir=parse(root,folder);self.assertEqual(ir_contract.validate_ir(ir),[])
        bindings={}
        for i in range(2):
            f=Path(folder)/f'{i}.csv';f.write_text(f'Place{i},Value,When\n'+data);bindings[f'Unseen source {i}']=str(f)
        ui=UI();frames=[]
        with preview_context(ir,bindings),patch.object(engine,'st',ui):
            real=engine.q
            def query(sql):
                df=real(sql);frames.append(df.copy());return df
            for sheet in ir['dashboards'][0]['sheets']:
                self.assertEqual(sheet['worksheet_semantics']['projection']['status'],'MAPPED',sheet.get('support'))
                with patch.object(engine,'q',side_effect=query),engine.field_scope(sheet):
                    (dual_plan if kind=='dual' else crosstab_plan).render(sheet,'',engine)
        return frames[-1],ui
    def test_calendar_components_and_full_dates(self):
        for rename in [False,True]:
            for part in ['mn','tmn']:
                with self.subTest(rename=rename,part=part),tempfile.TemporaryDirectory() as folder:
                    df,ui=self.execute(fixture('dual',rename,part),folder,'A,3,2024-01-02\nA,5,2025-01-03\nB,7,2024-02-01\n','dual')
                    if part=='mn':self.assertEqual(list(df.V0),[8,7]);self.assertEqual(list(df.V1),[4,7])
                    else:self.assertEqual(list(df.V0),[3,7,5])
                    chart=ui.charts[0].to_dict();self.assertEqual(chart['resolve']['scale']['y'],'independent')
    def test_crosstab_numeric_columns_and_authored_order(self):
        for renamed in [False,True]:
            with self.subTest(renamed=renamed),tempfile.TemporaryDirectory() as folder:
                df,ui=self.execute(fixture('cross',renamed),folder,'A,3,2024-01-02\nA,5,2025-01-03\nB,7,2024-02-01\n','cross')
                cols=[c for c in df if c.startswith('O_')]
                self.assertEqual(list(df[cols[0]]),['A','B']);self.assertEqual(list(df[cols[1]]),[8,7]);self.assertEqual(list(df[cols[2]]),[4,7])
    def test_fixed_filter_uses_bound_column_case(self):
        import visual_plan_common
        root=fixture('dual');ws=root.find('.//worksheet');table=ws.find('table')
        f=ET.SubElement(table,'filter',column='[federated.source_0].[none:Place0:nk]',attrib={'class':'categorical'})
        g=ET.SubElement(f,'groupfilter',function='except')
        ET.SubElement(g,'groupfilter',function='level-members',level='[none:Place0:nk]')
        ET.SubElement(g,'groupfilter',function='member',level='[none:Place0:nk]',member='%null%')
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(root,folder);sheet=ir['dashboards'][0]['sheets'][0]
            with patch.object(engine,'table_columns',return_value={'Place0','Value','When'}),patch.object(engine,'tbl',return_value='DB.S.T'):
                sql=visual_plan_common.apply_filters(engine,sheet,'')
                self.assertIn('NOT ("Place0" IS NULL)',sql)
                sheet['applied_filters'][0]['values']=["a\\b"]
                with self.assertRaisesRegex(ValueError,'backslash'):
                    visual_plan_common.apply_filters(engine,sheet,'')
    def test_crosstab_ties_refuse(self):
        with tempfile.TemporaryDirectory() as folder,self.assertRaisesRegex(ValueError,'ties'):
            self.execute(fixture('cross'),folder,'A,7,2024-01-02\nB,7,2024-02-01\n','cross')
    def test_partial_header_order_refuses_before_sql(self):
        root=fixture('cross');dictionary=root.find('.//manual-sort/dictionary');dictionary.remove(dictionary[-1])
        group=root.find('.//filter/groupfilter[@function="union"]')
        import json
        ET.SubElement(group,'groupfilter',function='member',level='[:Measure Names]',
                      member=json.dumps('[federated.source_0].[min:Value:qk]'))
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(root,folder);sheet=ir['dashboards'][0]['sheets'][0]
            self.assertIn('Partial Measure Names',sheet['support']['reason'])
            with patch.object(engine,'q',side_effect=AssertionError('query must not run')),patch.object(engine,'st',UI()):engine.render_sheet(sheet,[])


if __name__=='__main__':unittest.main()
