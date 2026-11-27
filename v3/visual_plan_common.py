"""Shared refusal checks for bounded V3 visual query plans."""
import xml.etree.ElementTree as ET
import re


def reasons(spec, model):
    errors=[]
    ws=ET.fromstring(model['source_xml'])
    ds=ET.fromstring(model['datasource_xml']) if model.get('datasource_xml') else None
    if any(spec.get(k) for k in ('blend','drill','table_calcs','forecast','subtotals','axis_flags','viz_tooltip')):
        errors.append('Unplanned blend, drill, table calculation, total or axis semantics')
    if ds is not None:
        physical_names={}
        for column in ds.findall('./column'):
            if column.find('calculation') is not None or column.get('param-domain-type') or column.get('datatype')=='table':continue
            internal=column.get('name','').strip('[]')
            normalized=re.sub(r'[^0-9a-zA-Z]+','_',internal).strip('_').upper() or 'COL'
            if normalized in physical_names and physical_names[normalized]!=internal:
                errors.append('Distinct physical fields collide after SQL identifier normalization')
            physical_names[normalized]=internal
        if any(n.tag in {'relationship','filter','initial-sql'} or (n.tag=='relation' and n.get('type') not in {'table'}) for n in ds.iter()):
            errors.append('Joins, relationships, custom SQL and source filters require a data-model plan')
        # Extract metadata repeats the upstream relation; compare identities, not node counts.
        rels=ds.findall('.//relation')
        connected=[r for r in rels if r.get('connection')]
        identities={(r.get('connection'),r.get('table')) for r in (connected or rels)}
        if len(identities)>1:
            errors.append('Multiple physical source identities require a data-model plan')
        if any(c.findall('./aliases/alias') and not c.get('param-domain-type') for c in ds.findall('.//column')):
            errors.append('Datasource member aliases require a display plan')
    if ds is not None and any('fiscal' in n.tag.lower() or any('fiscal' in k.lower() for k in n.attrib) for n in ds.iter()):
        errors.append('Fiscal date semantics require a calendar plan')
    for n in ws.iter():
        if n.tag=='aggregation' and n.attrib!={'value':'true'}:
            errors.append('Disaggregated views require a row identity plan')
        if 'fiscal' in n.tag.lower() or any('fiscal' in k.lower() for k in n.attrib):
            errors.append('Fiscal date semantics require a calendar plan')
        if n.tag in {'table-calc','grand-total','subtotals'}:
            errors.append('Totals and table calculations require a separate plan')
    for f in spec.get('applied_filters',[]):
        if f.get('kind') not in {'in','not_in'} or f.get('context') or f.get('datepart'):
            errors.append('Only ordinary categorical row filters are planned')
        if any(isinstance(v,str) and ('\\' in v or any(ord(c)<32 for c in v)) for v in f.get('values',[])):
            errors.append('Filter backslash/control literal dialect requires a typed plan')
        entry=model.get('filter_fields',{}).get(f.get('field_id'),{})
        if entry.get('is_calculation'):
            calc=entry.get('calculation',{})
            # Resolved SQL is checked again at runtime; boolean row gates only.
            if entry.get('datatype')!='boolean':errors.append('Calculated filter needs a boolean row-gate plan')
    for f in ws.findall('.//filter'):
        if f.get('class')!='categorical':errors.append('Unplanned noncategorical filter')
        for g in f.iter('groupfilter'):
            if g.get('function') not in {'union','member','except','level-members'}:
                errors.append('Unplanned categorical filter operation')
        if f.findall('.//groupfilter[@function="except"]'):
            gs=f.findall('./groupfilter')
            if not (len(gs)==1 and len(gs[0])==2 and gs[0][0].get('function')=='level-members' and gs[0][1].get('function')=='member' and gs[0][1].get('member')=='%null%'):
                errors.append('Only explicit null exclusion is planned')
    return errors


def finite(frame, columns):
    import numpy as np
    import pandas as pd
    for name in columns:
        if not pd.api.types.is_numeric_dtype(frame[name]) or not np.isfinite(frame[name].astype(float)).all():
            raise ValueError('Visual measure contains null, nonnumeric or nonfinite values: '+name)


def physical(engine, spec, occurrence):
    """Resolve a declared physical field against the bound schema, then quote it."""
    wanted=occurrence['field_definition']['physical']
    columns=engine.table_columns(engine.tbl(spec))
    matches=[c for c in columns if engine.to_phys(c)==engine.to_phys(wanted)]
    if len(matches)!=1:raise ValueError('Physical field is absent or normalizes ambiguously: '+wanted)
    return '"'+matches[0].replace('"','""')+'"'


def apply_filters(engine, spec, where, governed=None):
    """Never silently drop a fixed filter when source column casing differs."""
    model=spec['worksheet_semantics'];predicates=[];governed=governed or set()
    fields=model.get('filter_fields',{})
    for item in spec.get('applied_filters',[]):
        field=fields.get(item.get('field_id'))
        if not field:raise ValueError('Filter lacks a scoped field definition')
        if engine.to_phys(field['physical']) in governed and item.get('scope')!='datasource':continue
        values=item.get('values',[])
        if item.get('datepart') and not field.get('is_calculation'):
            # date-part members are part numbers, never raw dates (DSF): the engine's single filter predicate
            pred=engine._value_predicate(item,expr=physical(engine,spec,{'field_definition':field}))
            if not pred:raise ValueError('Unplanned date-part filter')
            predicates.append(pred);continue
        if field.get('is_calculation'):
            calc=engine.CALCS.get(item['caption'])
            if not calc or calc.get('agg_ready') or calc.get('window') or field.get('datatype')!='boolean' or item['kind']!='in':
                raise ValueError('Unplanned calculated filter evaluation level')
            selected={str(v).lower() for v in values}
            if selected=={'true','false'}:continue
            if selected not in ({'true'},{'false'}):raise ValueError('Invalid boolean gate selection')
            predicates.append(('NOT ' if selected=={'false'} else '')+'('+calc['sql']+')');continue
        if item.get('kind') not in ('in','not_in'):
            # this adapter compares member values only: a range / top N / relative date would become FALSE (an
            # empty chart) -- refuse with the reason instead
            raise ValueError(f"Unplanned {item.get('kind')} filter on this chart type")
        column=physical(engine,spec,{'field_definition':field})
        tests=[]
        for value in values:
            if value=='%null%' or value is None:tests.append(column+' IS NULL');continue
            dtype=field.get('datatype')
            if dtype=='string':
                if '\\' in str(value) or any(ord(c)<32 for c in str(value)):
                    raise ValueError('Unplanned filter backslash/control literal')
                literal="'"+str(value).replace("'","''")+"'"
            elif dtype in {'integer','real'}:
                import math
                number=float(value)
                if not math.isfinite(number):raise ValueError('Nonfinite numeric filter')
                literal=str(number)
            elif dtype=='boolean' and str(value).lower() in {'true','false'}:literal=str(value).upper()
            else:raise ValueError('Unplanned filter literal type')
            tests.append(column+' = '+literal)
        predicate='('+' OR '.join(tests)+')' if tests else 'FALSE'
        if item['kind']=='not_in':predicate='NOT '+predicate
        predicates.append(predicate)
    return where+(' AND ' if where.strip() else ' WHERE ')+' AND '.join(predicates) if predicates else where
