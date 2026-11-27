"""AI03.2 typed dependency, parameter, numeric and refusal evidence."""
import json
import unittest
from copy import deepcopy
from pathlib import Path
import sys
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import duckdb
import include_lod_plan as L
from typed_include_expression import parameter_declarations
from test_v3_include_lod import catalog
from test_v3_cortex_contract import RawSession
import cortex_assist as CA


def nested_catalog(renamed=False):
    fields=catalog('{INCLUDE YEAR([Shifted]), MONTH([Shifted]), DAY([Shifted]): [Revenue]}',renamed)
    for name,datatype,formula in [('Offset','integer','[Parameters].[DaysKey]'),('Shifted','date',"DATEADD('day',[Offset],[When])"),('Positive','real','IF [Amount] > 0 THEN [Amount] END'),('Units','integer',None),('ValidUnits','integer','IF [Units] >= 0 THEN [Units] ELSE 0 END'),('Revenue','real','SUM([Positive]*[ValidUnits])')]:
        fields['ds::'+name]={'id':'ds::'+name,'datasource_id':'ds','internal':name,'physical':name,'caption':('Alias ' if renamed else '')+name,'datatype':datatype,'is_calculation':formula is not None,'formula':formula}
    return fields


PARAMETERS={'DaysKey':{'datatype':'integer','domain':'any'}}


class TypedIncludeTests(unittest.TestCase):
    def test_nested_conditions_dates_and_runtime_parameter(self):
        for renamed in (False,True):
            fields=nested_catalog(renamed);plan=L.build(fields['ds::Metric'],fields,[],'avg',PARAMETERS)
            self.assertEqual(plan['parameters'],PARAMETERS)
            self.assertIn('ds::Offset',plan['dependency_ids']);self.assertIn('ds::Revenue',plan['dependency_ids'])
            db=duckdb.connect();db.execute('CREATE TABLE facts("When" DATE, Amount DOUBLE, Units BIGINT)')
            # Day groups: 8, 10, NULL. Negative/zero price and NULL quantity never contribute.
            db.execute("INSERT INTO facts VALUES ('2024-02-28',2,3),('2024-02-28',1,2),('2024-02-29',5,2),('2024-02-29',-2,10),('2024-02-29',0,3),('2024-02-29',9,NULL),('2024-03-01',NULL,2)")
            for days in (-366,0,1,366):
                sql=L.compile_unfiltered_sql(plan,fields,'facts',PARAMETERS,{'DaysKey':days},'duckdb')
                self.assertEqual(db.execute(sql).fetchone()[0],9)
            db.close()

    def test_parameter_changes_calendar_grain(self):
        f=nested_catalog();f['ds::Metric']['formula']='{INCLUDE MONTH([Shifted]): SUM([Amount])}'
        p=L.build(f['ds::Metric'],f,[],'avg',PARAMETERS);db=duckdb.connect()
        db.execute('CREATE TABLE facts("When" DATE, Amount DOUBLE)');db.execute("INSERT INTO facts VALUES ('2024-01-30',2),('2024-01-31',6)")
        for value,expected in [(0,8),(1,4),(2,8)]:
            self.assertEqual(db.execute(L.compile_unfiltered_sql(p,f,'facts',PARAMETERS,{'DaysKey':value},'duckdb')).fetchone()[0],expected)
        db.close()

    def test_plan_does_not_alias_parameter_declarations(self):
        f=nested_catalog();params=deepcopy(PARAMETERS)
        p=L.build(f['ds::Metric'],f,[],'avg',params)
        params['DaysKey']['datatype']='real'
        self.assertEqual(p['parameters'],PARAMETERS)
        with self.assertRaises(ValueError):L.validate(p,f,params)

    def test_stale_transitive_fields_and_parameters_refuse(self):
        f=nested_catalog();p=L.build(f['ds::Metric'],f,[],'avg',PARAMETERS)
        for name in ('Positive','ValidUnits','Revenue','Shifted','Offset'):
            changed=deepcopy(f);changed['ds::'+name]['formula']='0'
            with self.assertRaises(ValueError):L.validate(p,changed,PARAMETERS)
        for params in (None,{}, {'DaysKey':{'datatype':'real','domain':'any'}},{'DaysKey':{'datatype':'integer','domain':'list'}}):
            with self.assertRaises(ValueError):L.validate(p,f,params)
        for value in (True,1.0,'1',None,'1);DROP TABLE facts',1000001):
            with self.assertRaises(ValueError):L.compile_unfiltered_sql(p,f,'facts',PARAMETERS,{'DaysKey':value})

    def test_cycles_aggregates_types_and_unknown_syntax_refuse(self):
        cases=[('Positive','[Revenue]'),('Revenue','SUM(SUM([Amount]))'),('Positive','[ValidUnits]+[When]'),('Offset','[Offset]'),('Shifted',"DATEADD('month',1,[When])"),('Shifted',"DATEADD('day',1.5,[When])"),('Positive','IF [Amount] THEN 1 ELSE 0 END'),('Positive','IF [Amount]>0 THEN [When] END'),('Positive','IF [Amount]>0 THEN 1 ELSEIF [Amount]<0 THEN 2 END'),('Positive','[other].[Amount]'),('Revenue','SUM([Amount])+[Units]')]
        for name,formula in cases:
            f=nested_catalog();f['ds::'+name]['formula']=formula
            with self.subTest(name=name,formula=formula),self.assertRaises(ValueError):L.build(f['ds::Metric'],f,[],'avg',PARAMETERS)

    def test_expansion_budget_refuses_exponential_dependencies(self):
        f=nested_catalog()
        for i in range(16):
            field=deepcopy(f['ds::Positive']);field.update(id='ds::N'+str(i),internal='N'+str(i),formula=('[N'+str(i+1)+']+[N'+str(i+1)+']') if i<15 else '[Amount]');f[field['id']]=field
        f['ds::Positive']['formula']='[N0]'
        with self.assertRaisesRegex(ValueError,'budget'):L.build(f['ds::Metric'],f,[],'avg',PARAMETERS)

    def test_source_parameter_identity_and_minimized_ai_evidence(self):
        sheet={'worksheet_semantics':{'source_xml':'<worksheet><datasource-dependencies datasource="Parameters"><column name="[DaysKey]" caption="PRIVATE_CAPTION" value="4018" datatype="integer" param-domain-type="any"/></datasource-dependencies></worksheet>'}}
        self.assertEqual(parameter_declarations(sheet),PARAMETERS)
        f=nested_catalog();p=L.build(f['ds::Metric'],f,[],'avg',PARAMETERS)
        session=RawSession(response={'choice':'use_validated_include_plan','reason':'proposal','expression_json':json.dumps(p['expression'])})
        self.assertEqual(L.propose(p,f,session,CA.DEFAULT_MODEL,CA.ProposalContext(1),PARAMETERS),p)
        prompt=session.calls[0][1][1]
        for forbidden in ('UNRELATED_PRIVATE_FIELD','physical','PRIVATE_CAPTION','4018'):
            self.assertNotIn(forbidden,prompt)
        self.assertIn('IF [Amount]',prompt)
        wrapped=deepcopy(p['expression']);wrapped[3]=[wrapped[3]]
        session=RawSession(response={'choice':'use_validated_include_plan','reason':'proposal','expression_json':json.dumps(wrapped)})
        with self.assertRaisesRegex(ValueError,'independent'):L.propose(p,f,session,CA.DEFAULT_MODEL,CA.ProposalContext(1),PARAMETERS)
        wrong=deepcopy(p['expression']);wrong[3]=['number',42]
        session=RawSession(response={'choice':'use_validated_include_plan','reason':'proposal','expression_json':json.dumps(wrong)})
        with self.assertRaisesRegex(ValueError,'independent'):L.propose(p,f,session,CA.DEFAULT_MODEL,CA.ProposalContext(1),PARAMETERS)


if __name__=='__main__':unittest.main()

