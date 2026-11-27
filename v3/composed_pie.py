"""Source-owned duplicate placeholder axes for composed donut cards."""
import re,xml.etree.ElementTree as ET

def build(spec,model):
 occ=model['occurrences'];rows=[o for o in occ if o['channel']=='rows'];wedges=[o for o in occ if o['channel']=='wedge-size']
 if spec.get('kind')!='pie' or len(model['panes'])!=3 or len(rows)!=2 or rows[0]['token']!=rows[1]['token'] or len(wedges)!=1:return None
 from visual_plan_common import reasons
 from semantic_layer import _objects,join_plan
 from init_workbook import parse_relationships
 errors=reasons(spec,model);roles={};source=ET.fromstring(model['datasource_xml']);ws=ET.fromstring(model['source_xml'])
 try:
  objects=_objects(source);graph=join_plan(objects,parse_relationships(source,strict=True))
  if len(graph['steps'])!=len(objects)-1 or len(objects)<2 or source.findall('.//filter'):raise ValueError('Unplanned data model')
 except Exception:errors.append('Composed donut needs one complete bound fact tree')
 else:errors=[r for r in errors if r not in {'Joins, relationships, custom SQL and source filters require a data-model plan','Multiple physical source identities require a data-model plan'}]
 if any(not re.fullmatch(r'\s*MIN\s*\(\s*0\s*\)\s*',o.get('calculation_source',''),re.I) for o in rows):errors.append('Donut center axes must be authored MIN(0)')
 shelf=ws.findtext('./table/rows','').replace(rows[0]['token'],'F')
 if re.sub(r'[\s()]','',shelf)!='F+F':errors.append('Donut needs an explicit duplicate-axis overlay')
 if [p['marks'] for p in model['panes']]!=[['Pie'],['Pie'],['Pie']]:errors.append('Donut has unplanned marks')
 panes=[p for p in model['panes'] if p['axes']=={'y-axis-name':rows[0]['token']}]
 if len(panes)!=2 or not any(p['attributes'].get('y-index')=='1' for p in panes):errors.append('Donut pane ownership is incomplete')
 colors=[o for o in occ if o['channel']=='color'];lod=[o for o in occ if o['channel']=='lod'];texts=[o for o in occ if o['channel']=='text'];wedge=wedges[0]
 if len(colors)!=1 or len(lod)!=1 or colors[0]['token']!=lod[0]['token'] or colors[0]['level']!='row' or colors[0]['field_definition'].get('is_calculation') is not False:errors.append('Donut needs one physical segment grain')
 if wedge['aggregation'] not in ('sum','avg','min','max','cnt','ctd') or wedge['level']!='aggregate':errors.append('Donut wedge requires a scalar aggregate')
 center=[o for o in texts if o.get('field_id')==wedge.get('field_id') and o['aggregation']==wedge['aggregation']]
 unused=[o for o in texts if o not in center]
 if len(center)!=1 or len(unused)>1:errors.append('Donut center must use the same wedge total')
 for o in unused:
  formula=o.get('calculation_source','')
  if o['aggregation']!='min' or o['field_definition'].get('datatype')!='string' or re.search(r'\b(WINDOW_\w+|LOOKUP|INDEX|RANK\w*|RUNNING_\w+)\s*\(',formula,re.I):errors.append('Unplanned donut center text')
 order=[]
 sorts=ws.findall('.//manual-sort')
 if len(sorts)!=1 or len(colors)!=1 or sorts[0].get('column')!=colors[0]['token']:errors.append('Donut needs exact authored segment ordering')
 else:
  import json
  try:order=[json.loads(b.text) for b in sorts[0].findall('./dictionary/bucket')]
  except Exception:errors.append('Invalid donut category sort')
  if not order or len(order)>100 or len(order)!=len(set(order)) or not all(isinstance(v,str) for v in order):errors.append('Donut category order is not a bounded list')
 allowed={wedge['caption']}
 if not spec.get('label_runs') or any(r.get('field') and r['field'] not in allowed for r in spec['label_runs']):errors.append('Donut center label needs another plan')
 for o in occ:
  if o in rows:role='zero-axis'
  elif o['channel']=='y-axis-name' and o['token']==rows[0]['token']:role='zero-axis-selector'
  elif o in colors or o in lod:role='segment'
  elif o is wedge:role='wedge-value'
  elif o in center:role='center-total'
  elif o in unused:role='unreferenced-center-text'
  else:errors.append('Unconsumed donut occurrence');continue
  roles[o['id']]=role
  if o.get('unresolved') or o.get('table_calculation_xml') or o['level']=='table' or o.get('datasource_id')!=spec.get('datasource_id'):errors.append('Unplanned donut calculation/source')
 return {'status':'REQUIRES_PLAN' if errors else 'MAPPED','renderer':'composed-donut','bindings':roles,'reasons':sorted(set(errors)),'segment':colors[0]['id'] if len(colors)==1 else None,'measure':wedge['id'],'order':order,'appearance_adaptation':'Original donut renderer retains segment totals and center total; exact shape and tooltip styling require visual acceptance'}
