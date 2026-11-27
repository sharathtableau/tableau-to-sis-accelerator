"""
tests/test_powerbi.py -- Power BI (PBIP: PBIR + TMDL) -> Streamlit converter.

Fixture: tests/fixtures/powerbi/Superstore_PBIP, a PBIP generated for this
project over WBR_DB.PUBLIC.SAMPLE_SUPERSTORE. It passes Microsoft's published
PBIR JSON schemas but has NOT been opened in Power BI Desktop, so these tests
prove the converter against our reading of the format, not against Desktop.
Numbers are checked against data/Sample - Superstore.csv (the same rows that
were loaded into Snowflake) through a local DuckDB catalog that uses the same
fully-qualified names as Snowflake.

Run: pytest tests/test_powerbi.py
"""
import json
import os
import shutil
import sys
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import codegen  # noqa: E402
import conversion_report  # noqa: E402
import dax_rules as D  # noqa: E402
import pbi_parser  # noqa: E402
import pbi_pbir  # noqa: E402
import pbi_tmdl  # noqa: E402
import report_data  # noqa: E402
import report_ir  # noqa: E402
from report_sql import Compiler, CompileError, literal  # noqa: E402

PBIP = os.path.join(HERE, "fixtures", "powerbi", "Superstore_PBIP")
MODEL_DEF = os.path.join(PBIP, "Superstore.SemanticModel", "definition")
CSV = os.path.join(ROOT, "data", "Sample - Superstore.csv")
FQ = "WBR_DB.PUBLIC.SAMPLE_SUPERSTORE"

# README answer key (source data, all rows, no filters)
EXPECTED = {"Total Sales": 2326534.35, "Total Profit": 292296.81,
            "Profit Ratio": 0.1256, "Total Quantity": 38654, "Order Count": 5111,
            "Customer Count": 804, "Furniture Sales": 754747.76,
            "Avg Days to Ship": 3.96}
REGION_SALES = {"Central": 503170.67, "East": 691828.17, "South": 391721.90,
                "West": 739813.61}


@pytest.fixture(scope="module")
def ir():
    return pbi_parser.parse_pbip(os.path.join(PBIP, "Superstore.pbip"))


@pytest.fixture(scope="module")
def C(ir):
    report_data.set_session(None)
    report_data.configure_local({FQ: CSV})
    return Compiler(ir)


def M(name):
    return {"kind": "measure", "table": "Orders", "name": name, "label": name}


def col(t, c):
    return {"kind": "column", "table": t, "name": c, "label": c}


def run(C, dims, ms, filters=(), order=None):
    sql, meta = C.visual_query(dims, ms, list(filters), order=order)
    return report_data.run_sql(sql)


# --------------------------------------------------------------------------- #
# TMDL
# --------------------------------------------------------------------------- #

def test_tmdl_reads_sample_model():
    m = pbi_tmdl.read_tmdl_folder(MODEL_DEF)
    tabs = {t["name"]: t for t in m["tables"]}
    assert set(tabs) == {"Orders", "Date"}
    assert m["table_order"] == ["Orders", "Date"]
    assert len(tabs["Orders"]["measures"]) == 12
    assert len(tabs["Orders"]["columns"]) == 23
    calc = {c["name"]: c["expression"] for c in tabs["Date"]["columns"] if c["expression"]}
    assert calc["Month"] == 'FORMAT(\'Date\'[Date], "MMM")'
    month = next(c for c in tabs["Date"]["columns"] if c["name"] == "Month")
    assert month["sort_by"] == "Month Number"
    rel = m["relationships"][0]
    assert (rel["from_table"], rel["from_column"], rel["to_table"], rel["to_column"]) == \
        ("Orders", "Order Date", "Date", "Date")
    assert m["roles"][0]["table_permissions"] == {"Orders": 'Orders[Region] = "Central"'}
    params = {e["name"]: e["value"] for e in m["expressions"] if e["is_parameter"]}
    assert params["SnowflakeDatabase"] == "WBR_DB" and params["SnowflakeSchema"] == "PUBLIC"
    src = tabs["Orders"]["partitions"][0]["source"]
    assert src.startswith("let") and "SAMPLE_SUPERSTORE" in src


def test_tmdl_multiline_measure_quotes_and_flags():
    src = (
        "table 'Sales Fact'\n"
        "\tmeasure 'Margin %' =\n"
        "\t\t\tVAR p = [Profit Amt]\n"
        "\t\t\tRETURN DIVIDE(p, [Revenue])\n"
        "\t\tformatString: 0.0%\n"
        "\t\tisHidden\n"
        "\tmeasure 'It''s' = 1\n"
        "\tcolumn Amount\n"
        "\t\tdataType: double\n"
        "\t\tsourceColumn: AMOUNT\n"
        "\tcalculationGroup\n"
        "\t\tprecedence: 1\n"
        "relationship r1\n"
        "\tfromColumn: 'Sales Fact'.'Cust Key'\n"
        "\ttoColumn: Customer.Key\n"
        "\tcrossFilteringBehavior: bothDirections\n"
        "\tisActive: false\n")
    m = pbi_tmdl.parse_tmdl_text(src)
    t = m["tables"][0]
    assert t["name"] == "Sales Fact"
    ms = {x["name"]: x for x in t["measures"]}
    assert ms["Margin %"]["expression"] == "VAR p = [Profit Amt]\nRETURN DIVIDE(p, [Revenue])"
    assert ms["Margin %"]["format_string"] == "0.0%" and ms["Margin %"]["is_hidden"]
    assert "It's" in ms
    assert t["unparsed"] and t["unparsed"][0]["kind"] == "calculationGroup"
    r = m["relationships"][0]
    assert r["from_table"] == "Sales Fact" and r["from_column"] == "Cust Key"
    assert r["cross_filter"] == "bothDirections" and r["active"] is False


# --------------------------------------------------------------------------- #
# PBIR
# --------------------------------------------------------------------------- #

def test_pbir_reads_pages_in_order_and_visuals():
    rep = pbi_pbir.read_report_folder(os.path.join(PBIP, "Superstore.Report"))
    assert [p["name"] for p in rep["pages"]] == [
        "Executive Overview", "Customer Analysis", "On-Time Shipment Trends",
        "Order Details", "Sales Performance vs Last Year"]
    vis = [v for p in rep["pages"] for v in p["visuals"]]
    assert len(vis) == 28
    bar = next(v for v in vis if v["title"] == "Customers by Sales")
    assert bar["visual_type"] == "clusteredBarChart"
    assert bar["roles"]["Category"][0] == {**bar["roles"]["Category"][0],
                                           "kind": "column", "table": "Orders",
                                           "name": "Customer Name"}
    assert bar["sort"][0]["direction"] == "desc"
    assert bar["position"]["width"] == 610


def test_pbir_literals_and_field_shapes():
    lit = pbi_pbir.parse_literal
    assert lit("'O''Brien'") == "O'Brien" and lit("12L") == 12 and lit("2.5D") == 2.5
    assert lit("true") is True and lit("null") is None
    assert lit("datetime'2024-01-01T00:00:00'") == {"datetime": "2024-01-01T00:00:00"}
    agg = pbi_pbir.parse_field({"Aggregation": {"Expression": {"Column": {
        "Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "Sales"}}, "Function": 0}})
    assert agg == {"kind": "aggregation", "table": "Orders", "name": "Sales",
                   "agg": "sum", "inner_kind": "column"}
    lvl = pbi_pbir.parse_field({"HierarchyLevel": {"Expression": {"Hierarchy": {
        "Expression": {"PropertyVariationSource": {"Expression": {"SourceRef": {
            "Entity": "Orders"}}, "Name": "Variation", "Property": "Order Date"}},
        "Hierarchy": "Date Hierarchy"}}, "Level": "Year"}})
    assert lvl["kind"] == "date_level" and lvl["name"] == "Order Date" and lvl["level"] == "Year"


def _col_expr(src, prop):
    return {"Column": {"Expression": {"SourceRef": {"Source": src}}, "Property": prop}}


def test_pbir_filters_in_not_in_and_unknown():
    f_in = {"name": "f1", "type": "Categorical",
            "field": {"Column": {"Expression": {"SourceRef": {"Entity": "Orders"}},
                                 "Property": "Region"}},
            "filter": {"Version": 2, "From": [{"Name": "o", "Entity": "Orders", "Type": 0}],
                       "Where": [{"Condition": {"In": {
                           "Expressions": [_col_expr("o", "Region")],
                           "Values": [[{"Literal": {"Value": "'East'"}}],
                                      [{"Literal": {"Value": "'West'"}}]]}}}]}}
    p = pbi_pbir.parse_filter(f_in, "page")
    assert p["conditions"] == [{"field": {"kind": "column", "table": "Orders", "name": "Region"},
                                "op": "in", "values": ["East", "West"]}]
    f_not = json.loads(json.dumps(f_in))
    f_not["filter"]["Where"][0]["Condition"] = {"Not": {"Expression": f_in["filter"]["Where"][0]["Condition"]}}
    assert pbi_pbir.parse_filter(f_not, "page")["conditions"][0]["op"] == "not_in"
    f_topn = json.loads(json.dumps(f_in))
    f_topn["type"] = "TopN"
    f_topn["filter"]["Where"][0]["Condition"] = {"TopN": {}}
    assert pbi_pbir.parse_filter(f_topn, "visual")["conditions"] is None


def test_legacy_pbir_and_pbix_are_refused(tmp_path):
    legacy = tmp_path / "X.Report"
    legacy.mkdir()
    (legacy / "report.json").write_text("{}")
    with pytest.raises(pbi_pbir.PbirFormatError, match="PBIR-legacy"):
        pbi_pbir.read_report_folder(str(legacy))
    pbix = tmp_path / "r.pbix"
    pbix.write_bytes(b"PK")
    with pytest.raises(pbi_parser.PbipInputError, match="Save as"):
        pbi_parser.locate(str(pbix))


def test_zip_input_and_path_traversal(tmp_path):
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as z:
        for d, _, files in os.walk(PBIP):
            for f in files:
                p = os.path.join(d, f)
                z.write(p, os.path.relpath(p, os.path.dirname(PBIP)))
    ir = pbi_parser.parse_pbip(str(good))
    assert ir["source"]["name"] == "Superstore"
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(pbi_parser.PbipInputError, match="unsafe"):
        pbi_parser.parse_pbip(str(bad))


# --------------------------------------------------------------------------- #
# DAX rules
# --------------------------------------------------------------------------- #

def _env(measures, cols=("Sales", "Profit", "Category", "Region", "Customer ID", "Order Date",
                         "Ship Date", "Discount")):
    return D.MeasureEnv(lambda t, c: t == "T" and c in cols, measures.get,
                        lambda t: t == "T")


def test_dax_supported_patterns():
    ms = {"S": "SUM(T[Sales])", "P": "SUM('T'[Profit])", "R": "DIVIDE([P], [S])",
          "N": "DISTINCTCOUNT(T[Customer ID])", "C": "COUNTROWS(T)",
          "F": 'CALCULATE([S], T[Category] = "Furniture")',
          "I": 'CALCULATE([S], T[Region] IN {"East", "West"})',
          "X": "AVERAGEX(T, DATEDIFF(T[Order Date], T[Ship Date], DAY))",
          "V": "VAR a = [S] RETURN a * 2", "Q": "[S] / [P]", "D": "[S] - [P]"}
    env = _env(ms)
    t = {n: D.translate_measure(n, env) for n in ms}
    assert t["S"]["support"] == "auto" and t["S"]["leaves"][0]["sql"] == "SUM({{c:T::Sales}})"
    assert t["R"]["support"] == "auto" and len(t["R"]["leaves"]) == 2
    assert "IS NULL OR" in t["R"]["expr"]
    assert t["N"]["leaves"][0]["sql"].startswith("NULLIF(COUNT(DISTINCT")
    assert t["C"]["leaves"][0]["sql"] == "NULLIF(COUNT(1), 0)"
    assert t["F"]["support"] == "partial"
    assert t["F"]["leaves"][0]["overrides"] == [["T", "Category"]]
    assert "IN ('East', 'West')" in t["I"]["leaves"][0]["sql"]
    assert t["X"]["support"] == "partial" and "DATEDIFF('day'" in t["X"]["leaves"][0]["sql"]
    assert t["V"]["support"] == "auto"
    assert t["Q"]["support"] == "partial" and "NULLIF" in t["Q"]["expr"]
    assert "COALESCE" in t["D"]["expr"]


@pytest.mark.parametrize("dax,reason", [
    ("CALCULATE([S], SAMEPERIODLASTYEAR('Date'[Date]))", "not a constant column filter"),
    ("CALCULATE([S], ALL(T))", "not a constant column filter"),
    ("SUMX(FILTER(T, T[Sales] > 0), T[Sales])", "plain table"),
    ("RANKX(ALL(T[Region]), [S])", "not in the MVP DAX subset"),
    ("VAR a = [S] RETURN CALCULATE(a, T[Region] = \"East\")", "VAR used together"),
    ("[Nope]", "not a measure"),
    ("SUM(T[Missing])", "unknown column"),
])
def test_dax_unsupported_is_held_with_reason(dax, reason):
    env = _env({"S": "SUM(T[Sales])", "Z": dax})
    with pytest.raises(D.Unsupported, match=reason):
        D.translate_measure("Z", env)


def test_dax_blank_semantics_execute(tmp_path):
    """Run the emitted SQL on a tiny table where BLANK and NULL differ."""
    import duckdb
    con = duckdb.connect()
    con.execute('CREATE TABLE t ("SALES" DOUBLE, "PROFIT" DOUBLE, "CUST" VARCHAR, "CAT" VARCHAR)')
    con.execute("INSERT INTO t VALUES (10, NULL, 'a', 'x'), (5, NULL, NULL, 'x'), (0, NULL, 'a', 'y')")
    env = D.MeasureEnv(lambda tb, c: c in ("Sales", "Profit", "Cust", "Cat"),
                       {"S": "SUM(T[Sales])", "P": "SUM(T[Profit])",
                        "SP": "[S] + [P]", "PP": "[P] + [P]",
                        "DC": "DISTINCTCOUNT(T[Cust])", "DCNB": "DISTINCTCOUNTNOBLANK(T[Cust])",
                        "Z": "DIVIDE([S], [P])", "Z2": "DIVIDE([S], [P], -1)",
                        "CNT": "COUNTROWS(T)"}.get, lambda tb: True)
    phys = {"Sales": "SALES", "Profit": "PROFIT", "Cust": "CUST", "Cat": "CAT"}

    def val(name, where=""):
        tr = D.translate_measure(name, env)
        leaves = [l["sql"] for l in tr["leaves"]]
        import re
        leaves = [re.sub(r"\{\{c:T::(\w+)\}\}", lambda m: phys[m.group(1)], l) for l in leaves]
        expr = re.sub(r"\{\{a:(\d+)\}\}", lambda m: f"({leaves[int(m.group(1))]})", tr["expr"])
        return con.execute(f"SELECT {expr} FROM t {where}").fetchone()[0]

    assert val("S") == 15
    assert val("P") is None                      # SUM over blanks -> BLANK
    assert val("SP") == 15                       # BLANK + 15 = 15 in DAX
    assert val("PP") is None                     # BLANK + BLANK = BLANK
    assert val("DC") == 2                        # 'a' and BLANK
    assert val("DCNB") == 1
    assert val("Z") is None and val("Z2") == -1  # DIVIDE by BLANK
    assert val("CNT", "WHERE CAT = 'none'") is None   # COUNTROWS of nothing -> BLANK


def test_calculated_columns():
    ok = lambda t, c: t == "T" and c in ("Profit", "Order Date", "Ship Date", "Qty")  # noqa
    r = D.translate_column('IF(T[Profit] > 0, "Profitable", "Unprofitable")', "T", ok)
    assert r["sql"].startswith("(CASE WHEN") and r["support"] == "partial"
    assert "DATEDIFF('day'" in D.translate_column(
        "DATEDIFF(T[Order Date], T[Ship Date], DAY)", "T", ok)["sql"]
    assert "COALESCE" in D.translate_column("T[Profit] = 0", "T", ok)["sql"]
    with pytest.raises(D.Unsupported, match="another table"):
        D.translate_column("Customer[Name]", "T", ok)
    with pytest.raises(D.Unsupported):
        D.translate_column("LOOKUPVALUE(T[Qty], T[Profit], 1)", "T", ok)


# --------------------------------------------------------------------------- #
# parser classification (README answer key)
# --------------------------------------------------------------------------- #

def test_ir_is_valid_and_binds_snowflake(ir):
    assert report_ir.validate(ir) == []
    assert ir["model"]["tables"]["Orders"]["binding"] == {
        "database": "WBR_DB", "schema": "PUBLIC", "object": "SAMPLE_SUPERSTORE", "kind": "table"}
    assert ir["model"]["tables"]["Date"]["kind"] == "virtual_date"
    assert ir["model"]["tables"]["Date"]["anchors"] == {"Orders": "Order Date"}


def test_measure_classification_matches_answer_key(ir):
    ms = ir["model"]["measures"]
    for n in ("Total Sales", "Total Profit", "Profit Ratio", "Total Quantity",
              "Order Count", "Customer Count", "Avg Discount", "Sales per Customer"):
        assert (ms[n]["status"], ms[n]["support"]) == ("ready", "auto"), n
    assert ms["Furniture Sales"]["support"] == "partial"
    # deviation from the README key (it said Manual): AVERAGEX over the base
    # table with a supported row expression is translated, marked partial
    assert ms["Avg Days to Ship"]["support"] == "partial"
    for n in ("Sales LY", "Sales YoY %"):
        assert ms[n]["status"] == "held" and ms[n]["support"] == "manual"


def test_nothing_is_silently_dropped(ir):
    items = {(f["area"], f["item"]) for f in ir["features"]}
    nvis = sum(len(p["visuals"]) for p in ir["pages"])
    assert nvis == 28
    assert sum(1 for f in ir["features"] if f["area"] == "visual") == 28
    assert sum(1 for f in ir["features"] if f["area"] == "measure") == 12
    assert ("security", "role Central Region") in items
    assert any(a == "relationship" for a, _ in items)
    combo = next(v for p in ir["pages"] for v in p["visuals"] if v["kind"] == "combo")
    assert combo["held_measures"] == ["Sales LY", "Sales YoY %"]
    assert combo["support"] == "partial"


def test_bindings_can_be_retargeted():
    ir2 = pbi_parser.parse_pbip(PBIP, bindings={"database": "PROD_DB", "schema": "BI"})
    assert ir2["model"]["tables"]["Orders"]["binding"]["database"] == "PROD_DB"
    ir3 = pbi_parser.parse_pbip(PBIP, bindings={"tables": {"Orders": "A.B.C"}})
    assert ir3["model"]["tables"]["Orders"]["binding"]["object"] == "C"


def test_power_query_steps_are_reported():
    src = ('let\n Source = Snowflake.Databases("h", "wh"),\n'
           ' Db = Source{[Name = "DB", Kind = "Database"]}[Data],\n'
           ' Sch = Db{[Name = "S", Kind = "Schema"]}[Data],\n'
           ' T = Sch{[Name = "V1", Kind = "View"]}[Data],\n'
           ' Kept = Table.SelectRows(T, each [X] > 0)\nin\n Kept')
    b, steps, problems = pbi_parser.bind_partition(src, {})
    assert b == {"database": "DB", "schema": "S", "object": "V1", "kind": "view"}
    assert ("Kept", "Table.SelectRows") in steps
    b2, _, p2 = pbi_parser.bind_partition('let S = Sql.Database("x","y") in S', {})
    assert b2 is None and "not Snowflake" in p2[0]


# --------------------------------------------------------------------------- #
# numbers (local DuckDB over the same rows loaded into Snowflake)
# --------------------------------------------------------------------------- #

def test_kpi_numbers_match_expected(C):
    names = list(EXPECTED)
    df = run(C, [], [M(n) for n in names])
    for i, n in enumerate(names):
        got = float(df.iloc[0][f"M{i}"])
        assert got == pytest.approx(EXPECTED[n], rel=1e-3), (n, got)


def test_sales_by_region(C):
    df = run(C, [col("Orders", "Region")], [M("Total Sales")], order=[("D0", "asc")])
    got = dict(zip(df["D0"], df["M0"].astype(float)))
    for k, v in REGION_SALES.items():
        assert got[k] == pytest.approx(v, abs=0.01)


def test_calculate_replaces_slicer_filter_on_same_column(C):
    """Power BI: Furniture Sales ignores a Category slicer, Total Sales does not."""
    flt = [{"field": col("Orders", "Category"), "op": "in", "values": ["Office Supplies"]}]
    df = run(C, [], [M("Total Sales"), M("Furniture Sales")], flt)
    assert float(df.iloc[0]["M1"]) == pytest.approx(754747.76, abs=0.01)
    assert float(df.iloc[0]["M0"]) < 754747.76
    by_reg = run(C, [col("Orders", "Region")], [M("Furniture Sales")], flt)
    assert float(by_reg["M0"].sum()) == pytest.approx(754747.76, abs=0.01)


def test_date_table_columns_and_sort_by(C):
    yr = col("Date", "Year")
    df = run(C, [col("Date", "Month")], [M("Total Sales")],
             [{"field": yr, "op": "in", "values": [2024]}], order=[("S0", "asc")])
    assert df["D0"].tolist()[:3] == ["Jan", "Feb", "Mar"] and len(df) == 12
    tot = run(C, [], [M("Total Sales")], [{"field": yr, "op": "in", "values": [2024]}])
    assert float(df["M0"].sum()) == pytest.approx(float(tot.iloc[0]["M0"]), abs=0.01)


def test_blank_rows_dropped_and_slicer_between(C):
    flt = [{"field": col("Orders", "Order Date"), "op": "between",
            "values": [__import__("datetime").date(2024, 1, 1),
                       __import__("datetime").date(2024, 1, 31)]}]
    df = run(C, [col("Orders", "Region")], [M("Total Sales")], flt)
    assert len(df) >= 1 and df["M0"].notna().all()


def test_not_in_keeps_blanks_and_values_are_quoted(C):
    nasty = "O'Brien\"; DROP TABLE x; --"
    df = run(C, [], [M("Order Count")],
             [{"field": col("Orders", "Customer Name"), "op": "not_in", "values": [nasty]}])
    assert int(df.iloc[0]["M0"]) == 5111
    assert literal(nasty) == "'O''Brien\"; DROP TABLE x; --'"


def test_held_measure_cannot_be_queried(C):
    with pytest.raises(CompileError, match="held"):
        C.visual_query([], [M("Sales LY")], [])


def test_slicer_queries(C):
    vals = report_data.run_sql(C.distinct_values(col("Orders", "Region"), []))["V"].tolist()
    assert vals == ["Central", "East", "South", "West"]
    months = report_data.run_sql(C.distinct_values(col("Date", "Month"), []))["V"].tolist()
    assert months[:2] == ["Jan", "Feb"]


# --------------------------------------------------------------------------- #
# generation, report, app smoke test
# --------------------------------------------------------------------------- #

def test_codegen_report_runtime_roundtrip_and_tableau_unchanged(ir):
    src = codegen.build(ir, runtime="report_runtime")
    assert "from report_runtime import run" in src and "IR = json.loads(" in src
    tab = codegen.build({"dashboards": []})
    assert tab == codegen.build({"dashboards": []}, runtime="engine")
    assert tab.startswith("# GENERATED by codegen.py") and "from engine import run" in tab
    with pytest.raises(ValueError):
        codegen.build({}, runtime="os")


def test_conversion_report_lists_everything(ir):
    md = conversion_report.to_markdown(ir)
    for f in ir["features"]:
        assert f["item"].replace("|", "\\|") in md
    assert "Sales LY" in md and "CREATE ROW ACCESS POLICY" in md
    assert "owner's rights" in md
    js = json.loads(conversion_report.to_json(ir))
    assert js["held_measures"] == ["Sales LY", "Sales YoY %"]


def test_cli_writes_outputs(tmp_path):
    import pbi_convert
    rc = pbi_convert.main([os.path.join(PBIP, "Superstore.pbip"), "-o", str(tmp_path)])
    assert rc == 0
    assert {p.name for p in tmp_path.iterdir()} == {
        "report_ir.json", "app_superstore.py", "conversion_report.md", "conversion_report.json"}
    for m in pbi_convert.PBI_RUNTIME_MODULES:
        assert os.path.exists(os.path.join(ROOT, m)), m


def test_generated_app_renders_every_page(tmp_path, ir):
    from streamlit.testing.v1 import AppTest
    app = tmp_path / "app_test_pbi.py"
    app.write_text(
        "import sys\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "import report_data\n"
        f"report_data.configure_local({{{FQ!r}: {CSV!r}}})\n" +
        codegen.build(ir, runtime="report_runtime"))
    at = AppTest.from_file(str(app), default_timeout=120).run()
    assert not at.exception, [e.message for e in at.exception]
    kpis = {m.label: m.value for m in at.metric}
    assert kpis["Total Sales"] == "$2,326,534" and kpis["Profit Ratio"] == "12.6%"
    assert kpis["Order Count"] == "5,111"
    for page in at.sidebar.radio[0].options:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, (page, [e.message for e in at.exception])
    at.sidebar.radio[0].set_value("Sales Performance vs Last Year").run()
    assert any("Pending verification" in c.value for c in at.caption)
    # a slicer selection changes the KPI
    at.sidebar.radio[0].set_value("Executive Overview").run()
    at.multiselect[0].set_value(["East"]).run()
    kpis = {m.label: m.value for m in at.metric}
    assert kpis["Total Sales"] == "$691,828"
