"""C14: the Tableau function catalogue. Every Tableau function either translates to Snowflake SQL with Tableau's own
result, or refuses with a named reason -- never passed through as an unknown call that fails at query time (or, worse,
runs with a different meaning, e.g. INT() rounding instead of truncating). Values are Tableau's documented results;
the SQL is Snowflake's dialect and runs locally through the same DuckDB compatibility layer the preview uses
(backend.duckdb_compat / backend.local_sql). Written before the code."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import calc_translator as CT  # noqa: E402

CASES = [
    # number
    ('ABS([N])', 2), ('CEILING([X])', 4), ('FLOOR([X])', 3), ('ROUND([X], 1)', 3.7), ('DIV(7, 2)', 3),
    ('DIV(-7, 2)', -3), ('SQUARE(3)', 9), ('SQRT(9)', 3), ('POWER(2, 3)', 8), ('LOG(100)', 2), ('LOG(8, 2)', 3),
    ('LOG2(8)', 3), ('LN(1)', 0), ('EXP(0)', 1), ('SIGN([N])', -1), ('ZN([NUL])', 0), ('ROUND(COT(PI()/4), 6)', 1),
    ('ROUND(DEGREES(PI()), 6)', 180), ('2^3', 8), ('[N]^2', 4), ('ROUND(EXP(1)^-(1), 9)', 0.367879441),
    ('2^3^2', 512), ('-2^2', 4), ('3 - 2^2', -1), ('MIN(1, 2)', 1), ('MAX(1, 2)', 2), ('MAX([NUL], 2)', None),
    # string
    ('ASCII("A")', 65), ('CHAR(65)', 'A'), ('CONTAINS([S], "wor")', True), ('STARTSWITH([S], "he")', True),
    ('ENDSWITH([S], "ld")', True), ('ENDSWITH([S], "xx")', False), ('FIND([S], "o")', 5), ('FIND([S], "z")', 0),
    ('FIND([S], "o", 6)', 8), ('FINDNTH([S], "o", 2)', 8), ('FINDNTH([S], "o", 3)', 0), ('LEFT([S], 3)', 'hel'),
    ('RIGHT([S], 3)', 'rld'), ('MID([S], 2, 3)', 'ell'), ('MID([S], 7)', 'world'), ('LEN([S])', 11),
    ('LOWER([S2])', 'abc'), ('UPPER([S2])', 'ABC'), ('PROPER([S])', 'Hello World'),
    ('REPLACE([S], "o", "0")', 'hell0 w0rld'), ('SPACE(2)', '  '), ('SPLIT([CSV], ",", 2)', 'b'),
    ('SPLIT([CSV], ",", -1)', 'c'), ('TRIM(" x ")', 'x'), ('LTRIM(" x")', 'x'), ('RTRIM("x ")', 'x'),
    ('REGEXP_MATCH([S], "w.r")', True), ('REGEXP_MATCH([S], "^w")', False), ('REGEXP_EXTRACT([S], "(w[a-z]+)")', 'world'),
    ('REGEXP_EXTRACT_NTH([S], "([a-z]+) ([a-z]+)", 2)', 'world'), ('REGEXP_REPLACE([S], "o", "0")', 'hell0 w0rld'),
    # date
    ('DATEPART("year", [D])', 2024), ('DATEPART("quarter", [D])', 1), ('DATEPART("month", [D])', 3),
    ('DATEPART("day", [D])', 15), ('DATEPART("weekday", [D])', 6), ('DATEPART("dayofyear", [D])', 75),
    ('DATEPART("week", [D])', 11), ('DATEPART("week", #2024-01-06#)', 1), ('DATEPART("week", #2024-01-07#)', 2),
    ('DATEPART("hour", [TS])', 13), ('DATEPART("minute", [TS])', 45), ('WEEK(#2024-01-07#)', 2),
    ('DATEDIFF("day", [D], #2024-03-20#)', 5), ('DATEDIFF("week", #2024-03-09#, #2024-03-10#)', 1),
    ('DATEDIFF("week", #2024-03-10#, #2024-03-16#)', 0), ('DATEDIFF("month", #2024-01-31#, #2024-02-01#)', 1), ('DATEADD("month", 1, #2024-01-31#)', '2024-02-29'),
    ('DATEPARSE("yyyy-MM-dd", [DS])', '2024-02-01'), ('DATEPARSE("dd/MM/yyyy", "03/02/2024")', '2024-02-03'),
    ('ISDATE("2024-02-01")', True), ('ISDATE("nope")', False), ('MAKEDATE(2024, 2, 3)', '2024-02-03'),
    ('MAKEDATETIME(#2024-02-03#, #10:30:00#)', '2024-02-03 10:30:00'), ('MAKETIME(10, 30, 0)', '10:30:00'),
    ('ISOWEEK(#2024-01-01#)', 1), ('ISOWEEK(#2023-01-01#)', 52), ('ISOYEAR(#2023-01-01#)', 2022),
    ('ISOWEEKDAY([D])', 5), ('ISOQUARTER(#2024-05-01#)', 2),
    # type conversion
    ('INT(3.7)', 3), ('INT(-3.7)', -3), ('FLOAT("2.5")', 2.5), ('STR(5)', '5'), ('DATETIME("2024-02-01 10:00:00")', '2024-02-01 10:00:00'),
    # logical
    ('IIF([N] < 0, "neg", "pos")', 'neg'), ('IIF([NUL] > 0, "a", "b", "u")', 'u'), ('ISNULL([NUL])', True),
    ('IFNULL([NUL], 1)', 1),
    # aggregate
    ('STDEVP([X])', 0), ('VARP([X])', 0), ('ROUND(PERCENTILE([X], 0.5), 6)', 3.7), ('COVARP([X], [N])', 0),
    ('STDEV([X])', None), ('VAR([X])', None),
]

REFUSED = [('SCRIPT_REAL("return 1", SUM([X]))', 'external'), ('RAWSQL_REAL("%1", [X])', 'pass-through'),
           ('MODEL_PERCENTILE(SUM([X]), SUM([N]))', 'predictive'), ('MAKEPOINT([X], [N])', 'spatial'),
           ('HEXBINX([X], [N])', 'spatial'), ('USERNAME()', 'user'), ('ISMEMBEROF("g")', 'user'),
           ('COLLECT([X])', 'spatial'), ('SOMEFUTUREFN([X])', 'not translated')]


def duck():
    import duckdb
    import backend
    c = duckdb.connect()
    backend.duckdb_compat(c)
    c.execute("CREATE TABLE T AS SELECT * FROM (VALUES ('hello world', 'Abc', 3.7, -2, DATE '2024-03-15', "
              "TIMESTAMP '2024-03-15 13:45:10', NULL::DOUBLE, 'a,b,c', '2024-02-01')) "
              "v(S, S2, X, N, D, TS, NUL, CSV, DS)")
    return c


def value(c, formula):
    import backend
    sql, _agg = CT.translate_formula(formula)
    if sql is None:
        raise AssertionError(f'{formula}: refused ({CT.refusal(formula)})')
    got = c.execute('SELECT ' + backend.local_sql(sql.replace('__TBL__', 'T')) + ' FROM T').fetchone()[0]
    if hasattr(got, 'isoformat'):
        got = str(got)[:19]
        got = got[:10] if got.endswith('00:00:00') and len(got) == 19 else got
    return got, sql


class Catalogue(unittest.TestCase):
    def test_every_function_has_tableaus_result(self):
        c = duck()
        bad = []
        for formula, want in CASES:
            try:
                got, sql = value(c, formula)
            except Exception as exc:
                bad.append(f'{formula}: {type(exc).__name__}: {str(exc)[:120]}')
                continue
            same = (got == want) or (want is None and got is None) or (
                isinstance(want, (int, float)) and not isinstance(want, bool) and got is not None
                and not isinstance(got, (str, bool)) and abs(float(got) - want) < 1e-9)
            if not same:
                bad.append(f'{formula}: got {got!r}, Tableau {want!r} | {sql}')
        self.assertEqual(bad, [], '\n'.join(bad))

    def test_snowflake_dialect_not_duckdb(self):
        """The SQL is Snowflake's: names DuckDB lacks are supplied locally by the compatibility layer only."""
        for formula, name in (('PROPER([S])', 'INITCAP('), ('REGEXP_EXTRACT([S], "(a)")', 'REGEXP_SUBSTR('),
                              ('MAKEDATE(2024, 1, 2)', 'DATE_FROM_PARTS('), ('ISOWEEK([D])', 'WEEKISO('),
                              ('DATEPARSE("yyyy-MM-dd", [DS])', 'TRY_TO_TIMESTAMP(')):
            self.assertIn(name, CT.translate_formula(formula)[0], formula)

    def test_aggregates_are_aggregates(self):
        for f in ('STDEV([X])', 'VARP([X])', 'PERCENTILE([X], 0.9)', 'COVAR([X], [N])', 'CORR([X], [N])'):
            self.assertTrue(CT.translate_formula(f)[1], f)
        self.assertFalse(CT.translate_formula('MIN([X], [N])')[1])      # two arguments: row-level

    def test_refusals_name_the_reason(self):
        for formula, reason in REFUSED:
            self.assertEqual(CT.translate_formula(formula), (None, False), formula)
            self.assertIn(reason, CT.refusal(formula).lower(), formula)

    def test_date_literals_inside_formulas(self):
        self.assertIn("DATE '2024-03-20'", CT.translate_formula('[D] < #2024-03-20#')[0])
        self.assertIn("TIMESTAMP '2024-03-20 10:00:00'", CT.translate_formula('[TS] < #2024-03-20 10:00:00#')[0])
        self.assertIn("'#2024-03-20#'", CT.translate_formula('"#2024-03-20#"')[0])     # inside a string: text


class Reason(unittest.TestCase):
    def test_the_sheet_error_names_the_reason(self):
        import engine
        old = dict(engine.IR)
        engine.IR.clear()
        engine.IR.update({'field_scopes': {'d': {'calcs': {}, 'colmap': {}}},
                          'fields': {'d::c': {'datasource_id': 'd', 'is_calculation': True,
                                              'refusal': 'SCRIPT_REAL: external service (TabPy / R) calculation'}}})
        try:
            with self.assertRaises(ValueError) as why:
                with engine.field_scope({'datasource_id': 'd', 'field_bindings': {'Score': 'd::c'}}):
                    pass
            self.assertIn('TabPy', str(why.exception))
        finally:
            engine.IR.clear()
            engine.IR.update(old)

if __name__ == '__main__':
    unittest.main()
