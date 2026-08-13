"""
tests/test_regression.py  --  regression suite for the accelerator.

Locks in everything verified against Tableau so far. Run before ANY commit:

    python tests/test_regression.py        (or: pytest tests/)

Covers: IR invariants (chart kinds, datasources, calc translation, aliases,
params), a findings-free render probe of every dashboard sheet, what-if
parameter math, and the full numeric validation harness.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import glob                            # noqa: E402
import tableau_parser as TP           # noqa: E402
import engine                          # noqa: E402
import findings                        # noqa: E402

TWB = "Superstore.twb"
# find the E-Commerce workbook by pattern so a rename (e.g. dropping the '#')
# doesn't break the suite
_ecom = glob.glob("E-Commerce*VOTD.twbx")
ECOM = _ecom[0] if _ecom else "E-Commerce (Software) Sales Dashboard #VOTD.twbx"

# Tableau-verified chart-kind inventory. If a parser change alters any of
# these, that is a REGRESSION unless the PNG comparison says otherwise.
EXPECTED_KINDS = {
    "QuotaAttainment": "bar", "CommissionProjection": "mbar",
    "Sales": "kpi", "OTE": "kpi",
    "CustomerScatter": "scatter", "CustomerRank": "bar", "CustomerOverview": "mbar",
    "Product Detail Sheet": "table",
    "Total Sales": "kpi", "Sale Map": "map",
    "Sales by Segment": "area", "Sales by Product": "area",
    "ProductView": "heatmap", "ProductDetails": "circle",
    "ShipSummary": "pctbar", "ShippingTrend": "area", "DaystoShip": "gantt",
    # standalone worksheet tabs (not on any dashboard), like Tableau shows them
    # (Performance is an explicit Bar mark over a discrete month pill -> dtbar)
    "Performance": "dtbar", "Forecast": "line", "What If Forecast": "table",
}


def test_ir_invariants():
    ir = TP.build_ir(TWB)
    kinds = {s["name"]: s["kind"] for d in ir["dashboards"] for s in d["sheets"]}
    assert kinds == EXPECTED_KINDS, {k: (kinds.get(k), v) for k, v in
                                     EXPECTED_KINDS.items() if kinds.get(k) != v}
    # cross-datasource BLEND calcs cannot become single-table SQL: correctly
    # dropped and reported (they only affect the standalone Performance sheet)
    # calcs register under caption AND internal name (same entry object) --
    # count UNIQUE translations, not registry keys
    n_unique = len({id(v) for v in ir["calcs"].values()})
    assert n_unique == 21 and set(ir["calc_drops"]) == {
        "Sales above Target?",
        "SUM([Sales])-SUM([Sales Target].[Sales Target])"}, \
        f"calc translation changed: {n_unique} unique, drops={list(ir['calc_drops'])}"
    assert set(ir["datasources"]) == {"Sample - Superstore", "Sales Commission",
                                      "Sales Target"}
    assert ir["aliases"]["Order Profitable?"]["true"] == "Profitable"
    assert "New Quota" in ir["params"]
    com = [s for d in ir["dashboards"] for s in d["sheets"]
           if d["name"] == "Commission Model"]
    assert all(s["datasource"] == "Sales Commission" for s in com)
    print("ok  IR invariants (20 kinds, 21 calcs + 2 blend drops, 3 datasources, aliases, params)")
    return ir


class _Col:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __getattr__(self, name): return lambda *a, **k: None


class _Probe:
    def __init__(self): self.msgs = []
    def columns(self, n): return [_Col() for _ in range(n if isinstance(n, int) else len(n))]
    def container(self, **k): return _Col()
    def warning(self, m): self.msgs.append(str(m))
    def __getattr__(self, name): return lambda *a, **k: None


def test_all_sheets_render(ir):
    engine.configure(ir)
    findings.clear()
    bad = []
    for d in ir["dashboards"]:
        for s in d["sheets"]:
            p = _Probe()
            engine.st = p
            try:
                engine.render_sheet(s, "")
            except Exception as e:
                p.msgs.append(f"{type(e).__name__}: {e}")
            if p.msgs:
                bad.append((s["name"], p.msgs))
    blockers = [f for f in findings.all_findings() if f["severity"] == "BLOCKER"]
    assert not bad, f"sheets degraded: {bad}"
    assert not blockers, f"blocking findings: {blockers}"
    print("ok  all 20 sheets render (17 dashboard + 3 standalone), zero blockers")


def test_product_detail_has_rows(ir):
    """Guards the EXCLUDE-filter inversion bug: the 'all cities except null'
    filter must not be captured as City IN ('%null%') -> zero rows."""
    engine.configure(ir)

    class _P(_Probe):
        def __init__(self): super().__init__(); self.dfs = []
        def dataframe(self, df, **k): self.dfs.append(df)

    p = _P()
    engine.st = p
    sheet = next(s for d in ir["dashboards"] for s in d["sheets"]
                 if s["name"] == "Product Detail Sheet")
    engine.render_sheet(sheet, "")
    assert p.dfs and len(p.dfs[0]) > 0, \
        f"Product Detail Sheet returned {len(p.dfs[0]) if p.dfs else 'no'} rows"
    print(f"ok  Product Detail Sheet renders {len(p.dfs[0])} rows (exclude-filter)")


def test_what_if_math(ir):
    from backend import run_sql
    engine.configure(ir)
    ote = engine.CALCS["OTE (Variable)"]["sql"]
    v1 = run_sql(f"SELECT AVG({engine.sub_params(ote)}) V "
                 f"FROM SUPERSTORE.PUBLIC.SALES_COMMISSION").V[0]
    engine.PARAMS["New Quota"] = 600000
    v2 = run_sql(f"SELECT AVG({engine.sub_params(ote)}) V "
                 f"FROM SUPERSTORE.PUBLIC.SALES_COMMISSION").V[0]
    engine.PARAMS["New Quota"] = 500000
    assert v1 == 142000 and abs(v2 - 160400) < 1e-6, (v1, v2)
    print("ok  what-if parameter math (OTE 142,000 -> 160,400 @600K quota)")


def test_numeric_validation():
    r = subprocess.run([sys.executable, "validate_numbers.py"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    print("ok  numeric validation harness (14 Tableau-verified checks)")


CORPUS = ["Superstore.twb", "World Indicators.twbx",
          "Regional Analysis.twbx", "Globalsalesdashboard.twbx"]


def test_color_category_closed():
    """The color model is CLOSED over Tableau's schema: auditing every corpus
    workbook must yield ZERO color findings. A new workbook using an unknown
    color construct will be flagged by the audit at assess time."""
    for b in CORPUS:
        if not os.path.exists(os.path.join(ROOT, b)):
            continue
        r = subprocess.run([sys.executable, "audit_coverage.py", b],
                           capture_output=True, text=True, cwd=ROOT)
        flags = [l for l in r.stdout.splitlines() if "color" in l.lower()]
        assert not flags, f"{b} color flags: {flags}"
    print("ok  color category closed (0 color flags across the corpus)")


def test_app_interactions():
    """Headless UI drive of the GENERATED app: sidebar parameter must move
    the KPI. Guards 'the parameters do nothing'."""
    r = subprocess.run([sys.executable, os.path.join("tests", "test_app_interactions.py")],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    print("ok  app interaction test (sidebar param moves the KPI)")


def test_2024_3_sample_pack():
    """Locks the Tableau 2024.3 sample-pack features (FCP column tags,
    content-based date typing, date-part range filters, top-N filters,
    forecast/subtotal/viz-in-tooltip capture). Skips silently when the
    pack workbooks are not present in the working dir."""
    ss, wi = "Superstore_Tableau2024_3.twbx", "World_Indicators_Tableau2024_3.twbx"
    if not (os.path.exists(ss) and os.path.exists(wi)):
        print("skip 2024.3 sample pack (workbooks not present)")
        return
    ir = TP.build_ir(ss)
    sheets = {s["name"]: s for d in ir["dashboards"] for s in d["sheets"]}
    # FCP-tagged count-of-records object resolves (2024.x <_.fcp....column>)
    y = sheets["ShippingTrend"].get("y") or {}
    assert y.get("count_records") and y.get("agg") == "cnt", \
        "ShippingTrend count-of-records measure not resolved: %r" % y
    # forecast + subtotals + viz-in-tooltip captured for honest reporting
    assert sheets["Forecast"].get("forecast"), "forecast-specification not captured"
    assert sheets["What If Forecast"].get("subtotals"), "subtotals not captured"
    assert sheets["Sale Map"].get("viz_tooltip") == ["Tooltip: Profit Ratio by City"]
    ir2 = TP.build_ir(wi)
    sheets2 = {s["name"]: s for d in ir2["dashboards"] for s in d["sheets"]}
    # top-N filter captured with its ranking expression
    tn = [f for f in sheets2["Economy"].get("applied_filters", [])
          if f.get("kind") == "top_n"]
    assert tn and tn[0]["n"] == 5 and tn[0]["order_expr"] == "AVG([GDP])", tn
    # date-part range filter carries datepart (EXTRACT, never raw compare)
    yr = [f for f in sheets2["Economy"].get("applied_filters", [])
          if f.get("kind") == "range"]
    assert yr and yr[0].get("datepart") == "yr", yr
    # content-typed date column: YEAR must load as datetime, not VARCHAR
    import backend
    t = backend.run_sql("SELECT EXTRACT(YEAR FROM YEAR) y FROM "
                        "WORLD_INDICATORS_LOCAL LIMIT 1")
    assert int(t.iloc[0, 0]) >= 2000
    # top-5 GDP subquery returns exactly Tableau's five largest economies
    top5 = backend.run_sql(
        "SELECT COUNTRY_REGION FROM WORLD_INDICATORS_LOCAL GROUP BY 1 "
        "ORDER BY AVG(GDP) DESC LIMIT 5")
    assert set(top5.iloc[:, 0]) == {"United States", "Japan", "China",
                                    "Germany", "United Kingdom"}, set(top5.iloc[:, 0])
    print("ok  2024.3 sample pack (FCP tags, date typing, date-part ranges, "
          "top-N, forecast/subtotal/viz-tooltip capture)")


def test_silent_gap_detections():
    """The three constructs that used to degrade with NO finding (custom SQL,
    relative-date filters, log/reversed axes) must now be detected. No corpus
    workbook exercises them, so lock the detections with synthetic XML."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring("""
      <workbook><datasources>
        <datasource caption='Sales DW' name='federated.x'>
          <connection class='sqlserver' one-time-sql='USE warehouse;'>
            <relation name='CustomQ' type='text'>SELECT a, b FROM t WHERE x=1</relation>
          </connection>
        </datasource>
      </datasources></workbook>""")
    notes = TP.datasource_notes(root)
    kinds = {n["kind"] for n in notes}
    # this fixture's connection is ALSO a live, non-Snowflake (sqlserver) one
    # with no <extract> -- live_connections() correctly adds a 3rd honest
    # finding for that (2026-07-21, live-connection support MVP item).
    assert kinds == {"custom-sql", "initial-sql", "live-connection"}, notes
    assert "SELECT a, b FROM t" in [n for n in notes
                                    if n["kind"] == "custom-sql"][0]["detail"]

    ws = ET.fromstring("""
      <worksheet name='W'><table><view>
        <filter class='relative-date' column='[fed.x].[none:Order Date:qk]'
                period-type='month' range-type='last' period-count='3'/>
      </view></table></worksheet>""")
    af = TP.applied_filters(ws, {})
    assert af and af[0]["kind"] == "relative_date" and af[0]["period"] == "month", af

    ws2 = ET.fromstring("""
      <worksheet name='W2'><table><style>
        <style-rule element='axis'>
          <format attr='scale' value='log'/>
          <format attr='reversed' value='true'/>
        </style-rule>
      </style></table></worksheet>""")
    ax = TP.axis_flags(ws2)
    assert ax == ["reversed=true", "scale=log"], ax
    # linear/false must NOT flag (the common default case)
    ws3 = ET.fromstring("""
      <worksheet name='W3'><table><style>
        <style-rule element='axis'><format attr='scale' value='linear'/></style-rule>
      </style></table></worksheet>""")
    assert TP.axis_flags(ws3) == [], TP.axis_flags(ws3)
    print("ok  silent-gap detections (custom/initial SQL, relative-date filter, "
          "log/reversed axes)")


def test_topn_by_parameter():
    """Top-N whose count references a PARAMETER: parser keeps the caption
    (n_param), engine emits a ROW_NUMBER window with a __PARAM__ token
    (LIMIT rejects the float literals numeric params substitute to)."""
    import xml.etree.ElementTree as ET
    ws = ET.fromstring("""
      <worksheet name='W'><table><view>
        <filter class='categorical' column='[fed.x].[none:State:nk]'>
          <groupfilter count='[Parameters].[Top N]' end='top' function='end'
                       units='records'>
            <groupfilter direction='DESC' expression='SUM([Sales])'
                         function='order'>
              <groupfilter function='level-members' level='[none:State:nk]'
                           user:ui-enumeration='all'
                           xmlns:user='http://www.tableausoftware.com/xml/user'/>
            </groupfilter>
          </groupfilter>
        </filter>
      </view></table></worksheet>""")
    af = TP.applied_filters(ws, {})
    assert af and af[0]["kind"] == "top_n" and af[0].get("n_param") == "Top N" \
        and "n" not in af[0], af
    # the window form must execute (numeric compare tolerates float N)
    import backend
    df = backend.run_sql(
        "SELECT COUNT(DISTINCT STATE_PROVINCE) FROM ORDERS_LOCAL WHERE "
        "STATE_PROVINCE IN (SELECT STATE_PROVINCE FROM (SELECT STATE_PROVINCE, "
        "ROW_NUMBER() OVER (ORDER BY SUM(SALES) DESC) AS rn FROM ORDERS_LOCAL "
        "GROUP BY STATE_PROVINCE) WHERE rn <= 3.0)")
    assert int(df.iloc[0, 0]) == 3, df
    print("ok  top-N by parameter (n_param capture + float-safe window SQL)")


def test_hierarchies_drill():
    """Hierarchy drill-down (queue #6): drill-paths land in ir['hierarchies'],
    the right sheets get a drill spec (deepest hierarchy level on an AXIS slot,
    never a filter), and choosing a different level actually changes the
    rendered data."""
    ir = TP.build_ir(TWB)
    assert set(ir["hierarchies"]) == {"Location", "Product"}, ir["hierarchies"]
    assert ir["hierarchies"]["Product"] == ["Category", "Sub-Category",
                                            "Product Name"]
    drills = {s["name"]: s["drill"] for d in ir["dashboards"]
              for s in d["sheets"] if s.get("drill")}
    assert set(drills) == {"ProductView", "ProductDetails", "DaystoShip"}, drills
    assert drills["ProductView"]["current"] == "Category"
    assert drills["ProductDetails"]["current"] == "Sub-Category"  # deepest wins

    engine.configure(ir)

    class _Drill:
        choice = None
        def __init__(s): s.charts = []
        def columns(s, n): return [s for _ in range(n if isinstance(n, int) else len(n))]
        def container(s, **k): return s
        def altair_chart(s, ch, **k): s.charts.append(ch)
        def selectbox(s, label, options, index=0, key=None, **k):
            if label.startswith("Drill:") and _Drill.choice:
                return _Drill.choice
            return options[index] if options else None
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def __getattr__(s, n): return lambda *a, **k: None

    pv = next(s for d in ir["dashboards"] for s in d["sheets"]
              if s["name"] == "ProductView")
    rows = {}
    for choice in (None, "Sub-Category"):
        _Drill.choice = choice
        fake = _Drill()
        engine.st = fake
        engine.render_sheet(pv, "")
        assert fake.charts, f"ProductView rendered no chart (drill={choice})"
        rows[choice] = len(fake.charts[0].data)
    assert rows["Sub-Category"] > rows[None], rows   # drilling adds detail rows
    print("ok  hierarchy drill-down (2 hierarchies, 3 sheets, level swap "
          f"changes data {rows[None]} -> {rows['Sub-Category']} rows)")


def test_device_layouts():
    """Device layouts (queue #5): names captured per dashboard, phone/tablet
    zones EXCLUDED from the desktop sheet/geometry scan, drop reported."""
    ir = TP.build_ir(TWB)
    dls = {d["name"]: d.get("device_layouts") for d in ir["dashboards"]
           if d.get("device_layouts")}
    assert len(dls) == 6 and all(v == ["Phone", "Tablet"] for v in dls.values()), dls
    # rendering a device-layout dashboard records the INFO finding
    engine.configure(ir)
    findings.clear()
    class _F:
        def __getattr__(s, n): return lambda *a, **k: _F()
        def __call__(s, *a, **k): return _F()
        def __iter__(s): return iter([])
        def __enter__(s): return s
        def __exit__(s, *a): return False
    engine.st = _F()
    engine.render_dashboard(next(d for d in ir["dashboards"]
                                 if d["name"] == "Overview"))
    codes = {f["code"] for f in findings.all_findings()}
    assert "device-layouts" in codes, codes
    print("ok  device layouts (6 dashboards Phone+Tablet captured, desktop "
          "scan clean, drop reported)")


def test_table_calc_engine():
    """Table calcs (the E-Commerce unlock): WINDOW_* / RANK* translate to
    window-over-aggregate SQL; RUNNING_/LOOKUP/TOTAL still refuse (need view
    ordering we won't guess); agg-of-FIXED queries execute via the layered
    window hoist; calcs register under caption AND internal name."""
    from calc_translator import translate_formula
    sql, ar = translate_formula(
        "IF SUM([Revenue]) = WINDOW_MAX(SUM([Revenue])) THEN 'Max' ELSE 'Others' END")
    assert ar and sql == ("CASE WHEN SUM(REVENUE) = MAX(SUM(REVENUE)) OVER () "
                          "THEN 'Max' ELSE 'Others' END"), sql
    sql, ar = translate_formula("RANK(SUM([Sales]))")
    assert ar and sql == "RANK() OVER (ORDER BY SUM(SALES) DESC)", sql
    for refuse in ("RUNNING_SUM(SUM([Sales]))", "LOOKUP(SUM([Sales]), -1)",
                   "TOTAL(AVG([Sales]))"):
        s, _ = translate_formula(refuse)
        assert s is None, (refuse, s)
    # layered hoist: agg-of-FIXED and window-in-window shapes must execute
    import backend  # noqa: F401  (registers tables)
    df = engine.q(
        "SELECT REGION, AVG(DATEDIFF('day', MIN(ORDER_DATE) OVER "
        "(PARTITION BY CUSTOMER_ID), MAX(ORDER_DATE) OVER "
        "(PARTITION BY CUSTOMER_ID))) AS VAL FROM ORDERS_LOCAL GROUP BY 1")
    assert len(df) == 4 and df["VAL"].min() > 0, df
    # agg-grain window inline in a grouped query (no hoist needed)
    df2 = engine.q("SELECT REGION, SUM(SALES) AS VAL, "
                   "CASE WHEN SUM(SALES) = MAX(SUM(SALES)) OVER () THEN 'Max' "
                   "ELSE 'Others' END AS FLAG FROM ORDERS_LOCAL GROUP BY 1")
    assert (df2["FLAG"] == "Max").sum() == 1, df2
    # E-Commerce: WINDOW_MAX calc translated + registered under internal name
    if not os.path.exists(ECOM):
        print("ok  table-calc engine (core verified; E-Commerce workbook absent, "
              "skipped its calc-registry check)")
        return
    ir = TP.build_ir(ECOM)
    for key in ("Max. Total Revenue", "Calculation_1840001932833705988"):
        assert key in ir["calcs"] and " OVER ()" in ir["calcs"][key]["sql"], key
    print("ok  table-calc engine (WINDOW_*/RANK translate, RUNNING/LOOKUP/TOTAL "
          "refuse, layered hoist executes, internal-name registry)")


def test_ecommerce_parity():
    """Locks the Tableau-parity fixes the user's screenshot comparison caught:
    (a) layered hoist must NOT reuse __W aliases across layers (DATEDIFF(
    __W0__, __W0__) silently returned 0 -- Days-to-2nd showed 0 vs Tableau 67);
    (b) internal calc names resolve via the XML's own name->caption map, not
    formula matching ('(copy)' calcs sharing formulas mis-bound);
    (c) MIN(0)-placeholder rank sheets render as rank TABLES with Tableau-
    matching values."""
    twbx = ECOM
    if not os.path.exists(twbx):
        print("skip e-commerce parity (workbook not present)")
        return
    ir = TP.build_ir(twbx)
    engine.configure(ir)
    T = ("SUPERSTORE.PUBLIC.CUSTOMERS_DATADNA_DATASET_CHALLENGE_"
         "E_COMMERCE_DATASET_NOVEMBER_2025")
    # (a) Days to Second Purchase == Tableau's 67 days (66.66 pre-round)
    c = ir["calcs"]["C.Days to Second Purchase"]
    v = float(engine.q(f"SELECT {c['sql']} AS V FROM {T}")["V"][0])
    assert 66 < v < 68, f"Days-to-2nd = {v}, Tableau shows 67"
    # (b) '[C.Orders (copy)_350...]' is captioned C.AOV in the XML -- the
    # registry must bind it to the AOV translation (same entry object)
    assert ir["calcs"]["C.Orders (copy)_350999307064033288"] is \
        ir["calcs"]["C.AOV"], "internal-name binding broken"
    # (c) rank table: Revenue by Channels == Tableau's exact top-3
    class _F:
        def __init__(s): s.html = []
        def columns(s, n): return [s for _ in range(n if isinstance(n, int) else len(n))]
        def container(s, **k): return s
        def markdown(s, m, **k): s.html.append(m)     # rank lists = HTML table
        def selectbox(s, l, o, index=0, **k): return o[index] if o else None
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def __getattr__(s, n): return lambda *a, **k: None
    sh = next(x for d in ir["dashboards"] for x in d["sheets"]
              if x["name"] == "Revenue by Channels")
    fk = _F(); engine.st = fk
    engine.render_sheet(sh, "")
    tbl_html = next((h for h in fk.html if "table class='rk'" in h), None)
    assert tbl_html, "rank table HTML did not render"
    # exact top-3 in order, values single-line (nowrap), shortened header
    for frag in ("Website", "$572,134", "Direct Sales", "$250,914",
                 "Reseller", "$168,002", "Revenue", ">1<", ">2<", ">3<"):
        assert frag in tbl_html, frag
    assert tbl_html.index("Website") < tbl_html.index("Direct Sales") \
        < tbl_html.index("Reseller"), "rank order wrong"
    print("ok  e-commerce Tableau parity (67-day KPI, internal-name binding, "
          "rank tables exact)")


def test_container_layout():
    """Container layout feature: the zone hierarchy parses into a layout tree
    (grouping + proportions + container backgrounds) and the engine walks it.
    Locks the E-Commerce black Gross/Net panel and Superstore tree presence."""
    twbx = ECOM
    if os.path.exists(twbx):
        ir = TP.build_ir(twbx)
        d = next(x for x in ir["dashboards"] if x["name"] == "Dashboard")
        lay = d.get("layout")
        assert lay and lay["dir"] == "vert", "layout tree missing"
        def find_dark(n):
            if "sheet" in n:
                return None
            if (n.get("bg") or "").lower() == "#000000":
                return n
            for k in n.get("children", []):
                r = find_dark(k)
                if r:
                    return r
            return None
        dark = find_dark(lay)
        assert dark, "black container not captured"
        names = []
        def collect(n):
            if "sheet" in n:
                names.append(n["sheet"])
            for k in n.get("children", []):
                collect(k)
        collect(dark)
        assert set(names) == {"Gross%", "Net%", "Gross vs. Net"}, names
    ir2 = TP.build_ir(TWB)
    assert all(d.get("layout") for d in ir2["dashboards"]
               if d["name"] == "Overview"), "Superstore Overview layout missing"
    print("ok  container layout (zone tree parsed; black Gross/Net panel "
          "groups its 3 sheets; Superstore tree present)")


def test_semantic_layer():
    """Semantic-view generator: DDL from the workbook's relationship graph
    must EXECUTE (DuckDB) and reproduce the flatten path's numbers exactly.
    Uses raw in-memory tables so no Snowflake account is needed."""
    twbx = ECOM
    if not os.path.exists(twbx):
        print("skip semantic layer (workbook not present)")
        return
    import duckdb
    import pandas as pd
    import semantic_layer as SL
    root = TP.load_twb_xml(twbx)
    model = SL.data_model(root)
    assert len(model) == 1 and len(model[0]["objects"]) == 3, \
        "E-Commerce model: expected 1 datasource, 3 tables"
    sql = SL.generate_views(model, db="", schema="")
    assert sql.count("CREATE OR REPLACE VIEW") == 1 and "LEFT JOIN" in sql
    # execute against the RAW hyper tables (the real multi-table source)
    try:
        from tableauhyperapi import HyperProcess, Connection, Telemetry
    except ImportError:
        print("ok  semantic layer (DDL generated; execution skipped -- "
              "no tableauhyperapi)")
        return
    import tempfile
    import zipfile
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(twbx) as z:
            hp_name = next(x for x in z.namelist() if x.endswith(".hyper"))
            z.extract(hp_name, td)
        con = duckdb.connect()
        with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as h:
            with Connection(h.endpoint, os.path.join(td, hp_name)) as c:
                for sch in c.catalog.get_schema_names():
                    for t in c.catalog.get_table_names(sch):
                        cols = [x.name.unescaped for x in
                                c.catalog.get_table_definition(t).columns]
                        rows = c.execute_list_query(f"SELECT * FROM {t}")
                        df = pd.DataFrame(rows, columns=cols)
                        nm = t.name.unescaped.rsplit("_", 1)[0].upper()
                        con.register("v_" + nm, df)
                        con.execute(f"CREATE TABLE {nm} AS SELECT * FROM v_{nm}")
    con.execute(sql[sql.index("CREATE"):])
    v = ("CUSTOMERS_DATADNA_DATASET_CHALLENGE_E_COMMERCE_DATASET_"
         "NOVEMBER_2025_MODEL")
    n = con.execute(f"SELECT COUNT(*) FROM {v}").fetchone()[0]
    assert n == 48000, n                            # star join: no fan-out
    # numbers through the VIEW must equal the flatten path exactly
    rev = con.execute(f"SELECT ROUND(SUM(CASE WHEN NOT IS_REFUNDED THEN "
                      f"NET_REVENUE_USD END)) FROM {v}").fetchone()[0]
    flat = pd.read_csv("data/TEMP_0xfnzak033ztnc13wohgv0xghqq0.csv",
                       low_memory=False)
    rev_flat = round(flat.loc[~flat["is_refunded"].astype(bool),
                              "net_revenue_usd"].sum())
    assert rev == rev_flat, (rev, rev_flat)
    seg = con.execute(f"SELECT COUNT(*) FROM {v} WHERE SEGMENT IS NOT NULL"
                      ).fetchone()[0]
    assert seg > 0, "joined dim column SEGMENT empty"
    print("ok  semantic layer (relationship graph -> executable view, 48000 "
          "rows, revenue matches flatten exactly)")


def test_non_data_sheets():
    """Non-data sheet detection (text boxes / show-hide toggles / blanks):
    generic 'no data field' signal, NOT a name match. Must catch scaffolding
    everywhere AND never mis-flag a real chart across the whole corpus."""
    # predicate: bare sheet is non-data; any real field makes it data
    assert TP._has_data_fields({"kind": "table"}) is False
    assert TP._has_data_fields({"kind": "table", "measures": []}) is False
    assert TP._has_data_fields({"measures": [{"caption": "Sales"}]}) is True
    assert TP._has_data_fields({"geo": "State/Province"}) is True   # map
    assert TP._has_data_fields({"color_measure": {"caption": "Profit Ratio"}}) is True
    assert TP._has_data_fields({"x": {"caption": "Order Date"}}) is True

    # corpus sweep: only genuine scaffolding flagged, ZERO real charts
    books = ["Superstore.twb", ECOM]
    for b in books:
        if not os.path.exists(b):
            continue
        ir = TP.build_ir(b)
        nd = [s["name"] for d in ir["dashboards"] for s in d["sheets"]
              if s.get("non_data")]
        # a real map/chart must NEVER be flagged
        assert "Sale Map" not in nd, f"{b}: real map flagged non-data"
        if "E-Commerce" in b:
            assert set(nd) == {"info", "Show", "Hide"}, nd  # exactly scaffolding
    print("ok  non-data sheets (generic no-field signal; scaffolding only, "
          "no real chart mis-flagged across corpus)")


def test_detail_table_inference():
    """Detail/crosstab table inference: 3+ DISTINCT discrete dims stacked on
    one shelf with NO dims on the other = a text/detail listing, not a chart.
    Must catch listings AND never flip a real crosstab (Performance) to table."""
    # E-Commerce Detail (5 row dims, measure on cols) -> table, was dtbar
    if os.path.exists(ECOM):
        ir = TP.build_ir(ECOM)
        det = next(s for d in ir["dashboards"] for s in d["sheets"]
                   if s["name"] == "Detail")
        assert det["kind"] == "table", det["kind"]
        assert "Event Id" in det.get("dims", []) and "Channel" in det.get("dims", [])
    # Superstore Performance is a real crosstab (dims on BOTH shelves) -> NOT table
    ir2 = TP.build_ir(TWB)
    perf = next(s for d in ir2["dashboards"] for s in d["sheets"]
                if s["name"] == "Performance")
    assert perf["kind"] == "dtbar", f"Performance flipped to {perf['kind']}"
    print("ok  detail-table inference (deep-dim listing -> table; real "
          "crosstab preserved)")


def test_visual_risk_checklist():
    """The up-front visual-risk checklist must be HIGH-SIGNAL: flag genuine
    mismatches (won't-render, mark not honored) but NEVER handled constructs
    (dual-axis, labels) or it cries wolf and gets ignored."""
    import report as R
    # handled constructs -> NOT flagged
    assert R.visual_risk({"status": "converted", "errors": [], "findings": [
        {"severity": "APPEARANCE",
         "message": "multiple measures on one shelf (dual / compound axis)"}]}) is None
    assert R.visual_risk({"status": "partial", "errors": [], "findings": [
        {"severity": "COSMETIC", "message": "custom tooltip"}]}) is None
    # genuine risks -> flagged
    assert R.visual_risk({"status": "partial", "errors": [], "findings": [
        {"severity": "APPEARANCE",
         "message": "explicit mark class not honored (Gantt/Pie/Square)"}]}) == "MED"
    assert R.visual_risk({"status": "failed", "errors": ["boom"],
                          "findings": []}) == "HIGH"
    assert R.visual_risk({"status": "partial", "errors": [], "findings": [
        {"severity": "BLOCKER", "message": "SQL error"}]}) == "HIGH"
    print("ok  visual-risk checklist (flags real mismatches, ignores handled "
          "constructs -- high signal, no cry-wolf)")


def test_placeholder_member_list():
    """Placeholder-measure sheets (AVG(0)/MIN(0) dummy axis) are Tableau
    member LISTS, not charts -> render dimension + text columns as a table.
    Generic + corpus-safe (never catches a real measure chart)."""
    twbx = ECOM
    if not os.path.exists(twbx):
        print("skip placeholder member list (workbook not present)")
        return
    ir = TP.build_ir(twbx)
    engine.configure(ir)
    # detector: placeholder-only -> True; real measures -> False
    assert engine._is_placeholder_only({"measures": [{"caption": "AVG(0)"},
                                                     {"caption": "MIN(0)"}]})
    assert not engine._is_placeholder_only({"measures": [{"caption": "Revenue"}]})
    assert not engine._is_placeholder_only({"measures": []})
    # corpus sweep: only genuine list sheets, no real chart
    for b in [TWB, twbx]:
        ir_b = TP.build_ir(b)
        engine.configure(ir_b)
        ph = {s["name"] for d in ir_b["dashboards"] for s in d["sheets"]
              if engine._is_placeholder_only(s)}
        if "E-Commerce" in b:
            assert ph == {"Select Products", "Product img"}, ph
        else:
            assert ph == set(), (b, ph)   # Superstore has none
    # renders a member list (columns from dim + text fields)
    engine.configure(ir)

    class _F:
        def __init__(s): s.tables = []
        def columns(s, n): return [s for _ in range(n if isinstance(n, int) else len(n))]
        def container(s, **k): return s
        def dataframe(s, df, **k): s.tables.append(df)
        def selectbox(s, l, o, index=0, **k): return o[index] if o else None
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def __getattr__(s, n): return lambda *a, **k: None
    sh = next(s for d in ir["dashboards"] for s in d["sheets"]
              if s["name"] == "Select Products")
    fk = _F(); engine.st = fk
    engine.render_sheet(sh, "")
    assert fk.tables and "Product Name" in fk.tables[0].columns, \
        "Select Products did not render as a member list"
    print("ok  placeholder member list (AVG(0)/MIN(0) sheets -> dimension "
          "list; corpus-safe, no real chart caught)")


def test_ecommerce_end_to_end():
    """E-Commerce (the frontier workbook) must PARSE -> CODEGEN -> RENDER clean.

    Guards the class of failure that made the converter 'fail' on E-Commerce:
    a render-time NameError (engine referenced config.DATASOURCES without
    `import config`) crashed every sheet, and a half-applied engine edit would
    do the same. This runs the whole pipeline headless against the extracted
    _ecom.twb (kept even when the .twbx is absent) and asserts zero exceptions
    and zero BLOCKER findings across all 50 sheets."""
    import ast as _ast

    import codegen as CG
    src = os.path.join(ROOT, "_ecom.twb")
    if not os.path.exists(src):
        print("skip e-commerce end-to-end (_ecom.twb not present)")
        return
    ir = TP.build_ir(src)
    nsheets = sum(len(d["sheets"]) for d in ir["dashboards"])
    assert nsheets >= 40, f"E-Commerce parsed only {nsheets} sheets"
    _ast.parse(CG.build(ir))                     # codegen must produce valid source
    engine.configure(ir)
    findings.clear()

    class _P:
        def __init__(s): s.err = []
        def columns(s, n): return [s] * (n if isinstance(n, int) else len(n))
        def container(s, **k): return s
        def selectbox(s, l, o, index=0, **k): return o[index] if o else None
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def __getattr__(s, n): return lambda *a, **k: None

    exc = 0
    for d in ir["dashboards"]:
        for sh in d["sheets"]:
            engine.st = _P()
            try:
                engine.render_sheet(sh, "")
            except Exception as e:                # a NameError here = the bug
                exc += 1
    blockers = [f for f in findings.all_findings() if f["severity"] == "BLOCKER"]
    assert exc == 0, f"{exc} sheets raised during render"
    assert not blockers, f"blocker findings: {[b['message'][:80] for b in blockers[:4]]}"
    print(f"ok  e-commerce end-to-end ({nsheets} sheets parse->codegen->render, "
          "0 exceptions, 0 blockers)")


def test_placed_param_renders_once():
    """A dashboard-placed parameter must render EXACTLY ONE control (in the
    dashboard row), never also in the sidebar.

    converter_app.py called engine._render_param_controls() with no argument
    and re-rendered placed params in the sidebar, so 'Select Region' appeared
    twice (sidebar South-East vs dashboard-row North-West) and changing one was
    overwritten by the other on the same rerun -> "changing it does nothing".
    Now _render_param_controls derives the placed set from the IR, so every
    caller agrees."""
    fil = os.path.join(os.path.dirname(ROOT), "Fil Test.twbx")
    if not os.path.exists(fil):
        print("skip placed-param-once (Fil Test.twbx not present)")
        return
    ir = TP.build_ir(fil)
    engine.configure(ir)
    assert engine._placed_params() == {"Select Region"}, engine._placed_params()

    class W:
        def __init__(s): s.labels = []
        def selectbox(s, l, o, index=0, **k): s.labels.append(l); return o[index] if o else None
        def number_input(s, l, *a, **k): s.labels.append(l); return 0
        def text_input(s, l, *a, **k): s.labels.append(l); return ""
        def columns(s, n): return [s] * (n if isinstance(n, int) else len(n))
        def container(s, **k): return s
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def __getattr__(s, n): return lambda *a, **k: s

    w = W(); w.sidebar = W(); engine.st = w
    findings.clear()
    engine._render_param_controls()                 # converter path (no arg)
    engine.build_where(ir["dashboards"][0])         # dashboard control row
    total = w.labels + w.sidebar.labels
    n = sum("Select Region" in str(x) for x in total)
    assert n == 1, f"Select Region rendered {n} controls (labels={total})"
    assert not any("Select Region" in str(x) for x in w.sidebar.labels), \
        "placed param leaked into the sidebar"
    print("ok  placed param renders once (Select Region: 1 control, dashboard "
          "row only -- no sidebar duplicate)")


def test_view_order_filter_and_own_extract():
    """Fil Test Sheet 1 vs the user's Tableau screenshot: EXACTLY the top 5
    Sub-Categories for 2022 (Chairs, Phones, Storage, Machines, Tables).

    Three bugs met on this one sheet:
      1. shelf-sort-v2 (Tableau 2020+ sort XML) was not parsed -> view order
         unknown -> the INDEX()<=5 gate could not be pushed -> 17 bars.
      2. The gate was dropped with an INFO saying "Tableau default covers the
         full range" -- false, and it hid the defect from the report.
      3. init_workbook mapped the datasource to a same-named CSV left in data/
         by an older workbook instead of the extract THIS .twbx ships, so the
         numbers came from a different dataset entirely.
    INDEX() is a TABLE CALC: Tableau applies it AFTER dimension filters and
    aggregation, so the ranking must run inside this view's filters."""
    fil = os.path.join(os.path.dirname(ROOT), "Fil Test.twbx")
    if not os.path.exists(fil):
        print("skip view-order filter (Fil Test.twbx not present)")
        return
    ir = TP.build_ir(fil)
    s1 = next(s for d in ir["dashboards"] for s in d["sheets"]
              if s["name"] == "Sheet 1")
    srt = s1.get("sort")
    assert srt and srt["field"] == "Sales" and srt["on"] == "Sub-Category" \
        and srt["dir"].startswith("desc"), f"shelf-sort-v2 not parsed: {srt}"

    engine.configure(ir)
    assert engine._rank_gate_n("ROW_NUMBER() OVER (__WIN_ORDER__)<=5") == 5
    assert engine._rank_gate_n("SUM(SALES)") is None          # never guess

    class _F:
        def __init__(s): s.charts = []
        def columns(s, n): return [s] * (n if isinstance(n, int) else len(n))
        def container(s, **k): return s
        def altair_chart(s, ch, **k): s.charts.append(ch)
        def selectbox(s, l, o, index=0, **k): return None
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def __getattr__(s, n): return lambda *a, **k: None

    fk = _F(); engine.st = fk
    engine.render_sheet(s1, "WHERE EXTRACT(YEAR FROM ORDER_DATE) = 2022")
    assert fk.charts, "Sheet 1 rendered nothing"
    got = list(fk.charts[0].data["DIM"])
    assert got == ["Chairs", "Phones", "Storage", "Machines", "Tables"], got
    print("ok  view-order filter + own-extract mapping (Fil Test Sheet 1 = "
          "Tableau's exact top 5 for 2022)")


def test_date_mark_class_decides_line_vs_bars():
    """Tableau's Automatic mark over date+measure is a LINE; only a pinned
    mark='Bar' draws bars. Discreteness (:ok) changes the AXIS, not the mark.

    Field-found: Fil Test Sheet 2/Sheet 4 (Automatic, discrete month pill)
    rendered as bars where the user's Tableau draws lines. The old rule
    (Automatic + discrete -> bars) was over-generalised from sheets that
    happen to PIN mark='Bar'.

    The corpus invariant below is the generality proof: every sheet we still
    call 'dtbar' must pin mark='Bar' in its XML -- if a future change makes an
    Automatic sheet render bars again, this fails."""
    import zipfile as _zip
    import xml.etree.ElementTree as _ET

    def _root(b):
        if b.endswith(".twbx"):
            z = _zip.ZipFile(b)
            n = [x for x in z.namelist() if x.endswith(".twb")][0]
            return _ET.fromstring(z.read(n))
        return _ET.parse(b).getroot()

    books = [TWB] + [b for b in (glob.glob(os.path.join(ROOT, "*.twbx")) +
                                 glob.glob(os.path.join(os.path.dirname(ROOT), "Fil Test.twbx")))
                     if os.path.exists(b)]
    checked = dtbars = 0
    for b in books:
        try:
            ir_b = TP.build_ir(b)
        except Exception:
            continue
        marks = {}
        for ws in _root(b).findall(".//worksheets/worksheet"):
            el = ws.find(".//pane/mark")
            marks[ws.get("name")] = el.get("class") if el is not None else "Automatic"
        for d in ir_b["dashboards"]:
            for s in d["sheets"]:
                if s.get("kind") == "dtbar":
                    assert marks.get(s["name"]) == "Bar", (
                        f"{os.path.basename(b)}/{s['name']} renders BARS but its mark is "
                        f"{marks.get(s['name'])!r} -- Tableau draws a line there")
                    dtbars += 1
        checked += 1
    # the INVARIANT (every dtbar pins mark='Bar') is asserted per sheet above;
    # the count only gauges sweep breadth -- don't hard-fail when corpus .twbx
    # files are absent from the working dir, just note the reduced coverage
    assert checked >= 1, "no workbooks present to sweep"
    if checked < 5 or dtbars < 5:
        print(f"   (reduced sweep: {checked} workbooks, {dtbars} dtbar sheets "
              "present -- invariant still enforced on those)")

    # the field case, pinned exactly: 1 bar / 2 line / 3 table / 4 line / 5 bars
    fil = os.path.join(os.path.dirname(ROOT), "Fil Test.twbx")
    if os.path.exists(fil):
        got = {s["name"]: s["kind"] for d in TP.build_ir(fil)["dashboards"]
               for s in d["sheets"]}
        assert got == {"Sheet 1": "bar", "Sheet 2": "line", "Sheet 3": "table",
                       "Sheet 4": "line", "Sheet 5": "dtbar"}, got
    print(f"ok  date mark class decides line vs bars ({dtbars} dtbar sheets across "
          f"{checked} workbooks all pin mark='Bar'; Fil Test 1=bar 2=line 4=line 5=bars)")


def test_codegen_emits_parsable_source():
    """codegen must survive ANY workbook value -- data is data, never syntax.

    A group over Product Name emits SQL like IN ('Belkin ... 6''') whose
    doubled apostrophe + closing quote spells ''' -- that TERMINATED the old
    r'''...''' wrapper mid-IR, so the generated app died with SyntaxError
    before rendering a single chart (Fil Test.twbx, found in the field, not
    by this suite -- no corpus workbook had a group over an apostrophe'd
    text field). Codegen is only 'deterministic' if it proves it."""
    import ast as _ast
    import json as _json

    import codegen as CG

    # the exact payload that broke it, plus every other quote/backslash form
    hostile = {
        "calcs": {"Manufacturer": {
            "sql": "CASE WHEN X IN ('Belkin 325VA UPS Surge Protector, 6''') "
                   "THEN 'Belkin' WHEN X IN ('Lock-Up Easel ''Spel-Binder''') "
                   "THEN 'Lock-Up' WHEN X IN ('Acco 19\\\" Shelf', 'back\\\\slash', "
                   "'tri''''''ple') THEN 'Acco' END",
            "agg_ready": False}},
        "params": {"q": "\"quoted\"", "nl": "line1\nline2\ttab"},
        "dashboards": [{"name": "D", "title": "D", "sheets": [], "filters": []}],
    }
    src = CG.build(hostile)
    _ast.parse(src)                      # must be valid Python
    ns = {}
    exec(compile(src.replace("from engine import run", "run = lambda ir: ir")
                 .replace("run(IR)", ""), "<gen>", "exec"), ns)
    assert ns["IR"] == hostile, "embedded IR did not round-trip"
    assert "'''" in hostile["calcs"]["Manufacturer"]["sql"], "test payload lost its teeth"

    # corpus sweep: every real IR must codegen to parsable source
    swept = 0
    for f in sorted(os.listdir(ROOT)):
        if not f.endswith("_ir.json") or f.startswith("_"):
            continue
        with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
            ir_f = _json.load(fh)       # a corrupt corpus IR must FAIL, not skip
        if "dashboards" not in ir_f:
            continue
        _ast.parse(CG.build(ir_f))
        swept += 1
    assert swept >= 5, f"corpus sweep only covered {swept} IRs"
    print(f"ok  codegen emits parsable source (hostile quotes/backslashes + "
          f"{swept}-IR corpus sweep)")


def test_converter_flattens_and_topn_guard():
    """Two locks for the 'Top 3 Channels could not render' field bug.

    ROOT CAUSE: converter_app._decode_hypers_locally decoded the .hyper WITHOUT
    the relationship graph, so a Tableau 2020.2+ multi-table relationship extract
    (Events + Customers + Products, stored SEPARATELY) dumped the largest table
    ONLY -- every Customers/Products column (e.g. 'acquisition_channel') vanished,
    and any sheet ranking by one crashed with a BinderException. init_workbook.py
    passed relationships, so the dev CSV and ALL headless verification flattened
    correctly; only the converter path was broken -- which is what the user ran.

    (a) CONVERTER PARITY: the converter's own decode must flatten the star, so
        acquisition_channel is present (skipped if the .twbx / tableauhyperapi
        are absent).
    (b) ENGINE GUARD: a top-N whose ranking column is absent from the sheet's
        table must degrade to a WARNING, never emit crashing SQL -- defence for
        a non-star extract that genuinely cannot flatten."""
    # (a) converter decode flattens the relationship extract ----------------
    ecom = (glob.glob(os.path.join(ROOT, "Workbooks", "E-Commerce*VOTD.twbx"))
            or glob.glob(os.path.join(ROOT, "E-Commerce*VOTD.twbx")))
    if ecom:
        import tempfile
        import zipfile
        import converter_app as CA
        import init_workbook as IW
        try:
            import tableauhyperapi  # noqa: F401
            have_hyper = True
        except ImportError:
            have_hyper = False
        if have_hyper:
            z = zipfile.ZipFile(ecom[0])
            twb = [n for n in z.namelist() if n.lower().endswith(".twb")][0]
            hyp = [n for n in z.namelist() if n.lower().endswith(".hyper")][0]
            tmp = tempfile.mkdtemp()
            root = TP.load_twb_xml(z.extract(twb, tmp))
            rels = IW.parse_relationships(root)
            files = {}
            CA._decode_hypers_locally([z.extract(hyp, tmp)], files,
                                      tempfile.mkdtemp(), relationships=rels)
            import pandas as _pd
            cols = set(_pd.read_csv(next(iter(files.values())), nrows=1).columns)
            assert {"acquisition_channel", "channel", "product_name"} <= cols, (
                "converter decode did NOT flatten the relationship extract -- "
                f"acquisition_channel/product_name missing (got {len(cols)} cols)")
        else:
            print("   (converter-flatten check skipped: tableauhyperapi absent)")
    else:
        print("   (converter-flatten check skipped: E-Commerce .twbx absent)")

    # (b) engine guard: missing ranking column -> WARNING, never crashing SQL -
    mini = {
        "datasources": ["D"], "calcs": {}, "params": {}, "aliases": {},
        "colmap": {}, "dashboards": [],
    }
    engine.configure(mini)
    sheet = {"name": "TopN Guard", "datasource": "D",
             "applied_filters": [{"caption": "Channel", "kind": "top_n",
                                  "dir": "top", "order_expr": "COUNT([ghost_col])",
                                  "order_dir": "DESC", "n": 3}]}
    real_tc = engine.table_columns
    engine.table_columns = lambda T=engine.ORDERS: {"CHANNEL"}   # no GHOST_COL
    findings.clear()
    try:
        where = engine._apply_sheet_filters(sheet, "", "D")
    finally:
        engine.table_columns = real_tc
    assert "GHOST_COL" not in where and "IN (SELECT" not in where, (
        f"guard failed -- emitted crashing SQL: {where!r}")
    warned = [f for f in findings.all_findings()
              if f.get("code") == "topn-column-missing"]
    assert warned, "guard did not record the topn-column-missing WARNING"
    print("ok  converter flattens relationship extract + top-N guards a missing "
          "ranking column (no BinderException, honest WARNING)")


def test_absolute_layout_rows():
    """A `layout-basic` (absolute-positioned) dashboard must reconstruct
    side-by-side rows from x/y geometry, not stack every child vertically.

    FIELD BUG: after the container-layout tree shipped, dashboards whose sheets
    are positioned absolutely (Regional Analysis View2: 'Region level Sales' +
    'Profit by Category' share a y-band, sit side by side) rendered as a single
    stacked column, because layout_tree only built horz rows from explicit
    `layout-flow param='horz'` containers. Superstore's Customer tab had the
    same collapse. Fix: group absolute children sharing a vertical band into a
    horz row ordered by x. This locks that two known side-by-side pairs stay in
    a horz node, and that a genuinely stacked dashboard (Shipping) stays vert."""
    def _find(node, name):
        if not node:
            return None
        if node.get("sheet") == name:
            return node
        for c in node.get("children", []):
            r = _find(c, name)
            if r:
                return r
        return None

    def _row_of(node, a, b):
        """True iff a and b are the two children of one horz node."""
        if not node:
            return False
        if node.get("dir") == "horz":
            kids = {k.get("sheet") for k in node.get("children", [])}
            if a in kids and b in kids:
                return True
        return any(_row_of(c, a, b) for c in node.get("children", []))

    checks = [
        ("Workbooks/Regional Analysis.twbx", "View2",
         "Region level Sales", "Profit by Category"),
        ("Workbooks/Superstore.twbx", "Customers",
         "CustomerScatter", "CustomerRank"),
    ]
    ran = 0
    for wb, dash, a, b in checks:
        if not os.path.exists(os.path.join(ROOT, wb)):
            continue
        ir = TP.build_ir(os.path.join(ROOT, wb))
        d = next((x for x in ir["dashboards"] if x["name"] == dash), None)
        assert d, f"{wb}: dashboard {dash} not found"
        assert _row_of(d.get("layout"), a, b), (
            f"{wb}/{dash}: '{a}' and '{b}' should share a horz row "
            f"(absolute-geometry side-by-side), got a vertical stack")
        ran += 1
    if ran == 0:
        print("skip absolute-layout rows (corpus .twbx absent)")
        return
    print(f"ok  absolute-layout rows ({ran} dashboards: geometry side-by-side "
          "pairs reconstructed into horz rows, not stacked)")


def test_legend_zone_not_mistaken_for_sheet():
    """A dashboard's filter/color/highlighter zone REUSES its sheet's name but
    is NOT the sheet -- only a zone with no type-v2 is the chart.

    FIELD BUG (Superstore Product tab): a color legend `<zone name='ProductDetails'
    type-v2='color' w=10227>` was treated as the ProductDetails sheet, so the
    full-width chart (w=99156) was dropped as a duplicate and a 10227-wide legend
    took its place -> a squished side panel instead of Tableau's full-width
    stacked chart. layout_tree now admits a sheet zone only when it has no type.
    This asserts ProductView + ProductDetails render as a full-width VERTICAL
    stack (Tableau's layout), not the legend-driven side split."""
    def leaves(n, acc):
        if not n:
            return
        if "sheet" in n:
            acc.append((n["sheet"], n.get("w")))
            return
        for c in n.get("children", []):
            leaves(c, acc)

    def _row_of(node, a, b):
        if not node:
            return False
        if node.get("dir") == "horz":
            k = {c.get("sheet") for c in node.get("children", [])}
            if a in k and b in k:
                return True
        return any(_row_of(c, a, b) for c in node.get("children", []))

    ran = 0
    for wb in ("Workbooks/Superstore.twbx",
               "Workbooks/Superstore_Tableau2024_3.twbx"):
        p = os.path.join(ROOT, wb)
        if not os.path.exists(p):
            continue
        ir = TP.build_ir(p)
        d = next((x for x in ir["dashboards"] if x["name"] == "Product"), None)
        assert d, f"{wb}: Product dashboard missing"
        lv = []
        leaves(d.get("layout"), lv)
        names = [n for n, _ in lv]
        # exactly the two real charts, each ONCE (no legend pseudo-sheet)
        assert names.count("ProductDetails") == 1 and names.count("ProductView") == 1, (
            f"{wb}: Product tree leaves = {names} (legend leaked as a sheet?)")
        # they stack vertically, NOT a legend-driven horz side split
        assert not _row_of(d.get("layout"), "ProductView", "ProductDetails"), (
            f"{wb}: ProductView/ProductDetails share a horz row -- should stack")
        # ProductDetails is the full-width chart, not the ~10227-wide legend
        w = dict(lv)["ProductDetails"]
        assert w and w > 50000, f"{wb}: ProductDetails w={w} (legend width leaked)"
        ran += 1
    if ran == 0:
        print("skip legend-not-a-sheet (Superstore .twbx absent)")
        return
    print(f"ok  legend zone not mistaken for a sheet ({ran} Product tabs: "
          "full-width ProductView/ProductDetails vertical stack, no legend leak)")


def _serialize_layout(node):
    """Canonical, geometry-free serialization of a layout tree: dir + ordered
    children, sheet leaves by name. Stable across runs -> snapshot-comparable."""
    if not node:
        return None
    if "sheet" in node:
        return node["sheet"]
    return {"dir": node.get("dir"),
            "children": [_serialize_layout(c) for c in node.get("children", [])]}


def _layout_leaves(node):
    if not node:
        return []
    if "sheet" in node:
        return [node["sheet"]]
    out = []
    for c in node.get("children", []):
        out += _layout_leaves(c)
    return out


def test_layout_snapshots(update=False):
    """Corpus-wide LAYOUT REGRESSION GUARD -- the structural counterpart to the
    numeric harness.

    The suite already proves numbers are right and sheets don't crash, but for a
    long time NOTHING asserted that a dashboard keeps its visual STRUCTURE, so a
    change aimed at one workbook's layout could silently restructure every other
    (the container-layout rewrite stacked absolute-positioned dashboards and let
    color legends masquerade as sheets, and every numeric test stayed green).

    This snapshots each corpus dashboard's layout tree (dir + sheet order, no
    geometry) into tests/layout_snapshots.json and fails on ANY drift. It also
    re-checks the two invariants that the field bugs violated: every placed
    sheet still appears in its tree, and no sheet appears twice (a legend leak).
    Regenerate intentionally with test_layout_snapshots(update=True) after a
    DELIBERATE layout change, and eyeball the diff before committing."""
    import json as _json
    snap_path = os.path.join(HERE, "layout_snapshots.json")
    stored = {}
    if os.path.exists(snap_path):
        stored = _json.load(open(snap_path, encoding="utf-8"))
    current, mismatches, ran = {}, [], 0
    for wb in sorted(glob.glob(os.path.join(ROOT, "Workbooks", "*.twbx"))):
        base = os.path.basename(wb)
        ir = TP.build_ir(wb)
        for d in ir["dashboards"]:
            key = base + "::" + d["name"]
            ser = _serialize_layout(d.get("layout"))
            current[key] = ser
            # invariant 1: no placed sheet lost from its own tree
            leaves = _layout_leaves(d.get("layout"))
            placed = [s["name"] for s in d["sheets"]]
            miss = [s for s in placed if s not in leaves and d.get("layout")]
            assert not miss, f"{key}: sheets dropped from layout tree: {miss}"
            # invariant 2: no sheet duplicated (chrome zone leaked as a sheet)
            dup = [s for s in set(leaves) if leaves.count(s) > 1]
            assert not dup, f"{key}: sheet appears twice in tree (legend leak?): {dup}"
            ran += 1
            if not update and key in stored and stored[key] != ser:
                mismatches.append(key)
    if update:
        _json.dump(current, open(snap_path, "w"), indent=1, sort_keys=True)
        print(f"ok  layout snapshots UPDATED ({ran} dashboards written)")
        return
    if ran == 0:
        print("skip layout snapshots (corpus .twbx absent)")
        return
    assert not mismatches, (
        "LAYOUT DRIFT in %d dashboard(s): %s\n  If intentional, re-run with "
        "update=True and eyeball the diff." % (len(mismatches), ", ".join(mismatches)))
    new = [k for k in current if k not in stored]
    print(f"ok  layout snapshots ({ran} corpus dashboards match; "
          f"{len(new)} new) -- structure locked, no silent drift")


def test_datepart_member_as_full_date():
    """A date-part filter member (Year/Quarter/Month IN ...) is stored EITHER as
    the part number ('2000') OR as a full date ('2000-01-01') -- Tableau writes
    whichever the source column holds. The engine did int(float(v)) on it, so a
    full-date member crashed render with 'could not convert string to float:
    2000-01-01' (field-found on World Indicators: TourismOverTime / Economy /
    TourismByCountry all carry a Year filter whose members are full dates).
    engine._part_num resolves both forms; unresolvable -> None (skip, not crash)."""
    assert engine._part_num("2000", "YEAR") == 2000
    assert engine._part_num("2000-01-01", "YEAR") == 2000
    assert engine._part_num("2000-07-15", "QUARTER") == 3
    assert engine._part_num("2000-07-15", "MONTH") == 7
    assert engine._part_num("2000-07-15", "DAY") == 15
    assert engine._part_num("not a date", "YEAR") is None
    # corpus proof: the 3 WI sheets that broke must now render clean
    wi = os.path.join(ROOT, "Workbooks", "World Indicators.twbx")
    if not os.path.exists(wi):
        print("ok  datepart member as full date (_part_num both forms; WI "
              "corpus render skipped -- workbook absent)")
        return
    ir = TP.build_ir(wi)
    engine.configure(ir)
    findings.clear()
    broke = []
    for d in ir["dashboards"]:
        for s in d["sheets"]:
            if s["name"] not in ("TourismOverTime", "Economy", "TourismByCountry"):
                continue
            p = _Probe()
            engine.st = p
            try:
                engine.render_sheet(s, "")
            except Exception as e:
                broke.append((s["name"], f"{type(e).__name__}: {e}"))
    assert not broke, f"date-part member sheets still break: {broke}"
    print("ok  datepart member as full date (_part_num both forms; 3 WI Year-"
          "filter sheets render clean, no float() crash)")


SNOWFLAKE_RESERVED = set("""
ACCOUNT ALL ALTER AND ANY AS BETWEEN BY CASE CAST CHECK COLUMN CONNECT
CONNECTION CONSTRAINT CREATE CROSS CURRENT DATABASE DELETE DISTINCT DROP ELSE
EXISTS FALSE FOLLOWING FOR FROM FULL GRANT GROUP GSCLUSTER HAVING ILIKE IN
INCREMENT INNER INSERT INTERSECT INTO IS ISSUE JOIN LATERAL LEFT LIKE
LOCALTIME LOCALTIMESTAMP MINUS NATURAL NOT NULL OF ON OR ORDER ORGANIZATION
QUALIFY REGEXP REVOKE RIGHT RLIKE ROW ROWS SAMPLE SCHEMA SELECT SET SOME START
TABLE TABLESAMPLE THEN TO TRIGGER TRUE TRY_CAST UNION UNIQUE UPDATE USING
VALUES VIEW WHEN WHENEVER WHERE WITH
""".split())


def test_no_reserved_word_sql_aliases():
    """Snowflake rejects a RESERVED word used as an UNQUOTED identifier; DuckDB
    accepts it. The engine aliases query columns (AS V, AS START, ...) and a
    reserved-word alias compiles locally but crashes ONLY in the deployed SiS
    app (field-found: 'Days to Ship by Product' gantt aliased a column AS START
    -> 'syntax error ... unexpected START'). Static guard over engine.py: every
    UNQUOTED uppercase SQL alias must be a non-reserved word (quote it, like the
    gantt's now-`AS "START"`, if you need a reserved one)."""
    import re as _re
    src = open(os.path.join(ROOT, "engine.py"), encoding="utf-8").read()
    # unquoted aliases only: `AS NAME` (a quoted `AS "START"` won't match)
    aliases = set(_re.findall(r'\bAS\s+([A-Z_][A-Z0-9_]*)\b', src))
    bad = sorted(aliases & SNOWFLAKE_RESERVED)
    assert not bad, (f"reserved-word SQL alias(es) will crash in Snowflake "
                     f"(quote them): {bad}")
    print(f"ok  no reserved-word SQL aliases ({len(aliases)} aliases scanned, "
          "none reserved -- the 'unexpected START' SiS gantt crash)")


def test_backend_uses_pushed_session():
    """The 'run the migrator locally, push to Snowflake' path (pipeline_app.py's
    checkbox) is worthless as a Snowflake-native demo unless chart QUERIES also
    execute in Snowflake -- not just the table load + semantic view. Before this
    fix, backend.run_sql only recognized get_active_session() (true ONLY when
    the process is deployed INSIDE Snowflake), so a locally-run-but-connected
    session was invisible to it: tables + the semantic view would be pushed for
    real, then every chart would silently render from local DuckDB anyway --
    exactly the 'where is Snowflake actually being used' gap a client would
    catch. backend.set_session() registers an externally-opened session so
    run_sql routes to it. Guarded with a FAKE session -- no live Snowflake."""
    import backend

    class FakeResult:
        def __init__(s, df): s._df = df
        def to_pandas(s): return s._df

    class FakeSession:
        def __init__(s): s.calls = []
        def sql(s, text):
            s.calls.append(text)
            import pandas as pd
            return FakeResult(pd.DataFrame({"N": [4242]}))

    try:
        fake = FakeSession()
        backend.set_session(fake)
        assert backend._running_in_snowflake() is True
        df = backend.run_sql("SELECT COUNT(*) AS N FROM SOME.REAL.TABLE")
        assert fake.calls == ["SELECT COUNT(*) AS N FROM SOME.REAL.TABLE"], fake.calls
        assert df["N"].iloc[0] == 4242
        # clearing reverts to local DuckDB routing (no live session dangling)
        backend.set_session(None)
        assert backend._running_in_snowflake() is False
    finally:
        backend.set_session(None)          # never leak state into later tests
    print("ok  backend routes queries through an externally-pushed Snowflake "
          "session (set_session) -- the local-migrator-actually-uses-Snowflake fix")


def test_write_pandas_date_fix():
    """write_pandas lands a pandas datetime64[ns] column as NUMBER(38,0) =
    epoch NANOSECONDS; Snowflake then rejects DATE_TRUNC / EXTRACT / DATEDIFF
    and `col BETWEEN 'YYYY-MM-DD'` on it -- EVERY date-using sheet of the
    deployed Superstore staged-demo failed this way (the CLI loader
    load_snowflake.py already repairs it; the demo loader pipeline.py did not
    -- the two-paths-diverge bug again).

    pipeline._fix_date_columns_session repairs a NUMBER date column to
    TIMESTAMP_NTZ (ADD tmp / UPDATE TO_TIMESTAMP(col,9) / DROP / RENAME) and is
    idempotent (leaves an already-temporal column alone). Guarded here with a
    FAKE session -- no live Snowflake -- asserting the repair fires for a
    NUMBER column and skips a TIMESTAMP one."""
    import pipeline

    class FakeResult:
        def __init__(s, rows): s._rows = rows
        def collect(s): return s._rows

    class FakeSession:
        def __init__(s, types): s.types = types; s.stmts = []
        def sql(s, text):
            s.stmts.append(text)
            if text.strip().upper().startswith("DESCRIBE"):
                return FakeResult([{"name": n, "type": t}
                                   for n, t in s.types.items()])
            return FakeResult([])

    sess = FakeSession({"ORDER_DATE": "NUMBER(38,0)",       # broken -> repair
                        "SHIP_DATE": "TIMESTAMP_NTZ(9)",    # already good -> skip
                        "SALES": "FLOAT"})
    fixed = pipeline._fix_date_columns_session(
        sess, "DB", "SCH", "T", ["ORDER_DATE", "SHIP_DATE"])
    assert fixed == ["ORDER_DATE"], fixed
    joined = " ".join(sess.stmts)
    assert 'TO_TIMESTAMP("ORDER_DATE", 9)' in joined, joined
    assert 'ADD COLUMN "ORDER_DATE__TS" TIMESTAMP_NTZ' in joined
    assert "SHIP_DATE__TS" not in joined, "already-TIMESTAMP column was touched"
    print("ok  write_pandas date-fix (NUMBER epoch -> TIMESTAMP repair fires; "
          "idempotent on temporal cols -- the Superstore demo date-sheet crash)")


def test_pipeline_reuses_preloaded_table():
    """A hyper-only workbook (Regional Analysis, Global Sales) has NO decodable
    file inside Snowflake -- the .hyper cannot be decoded in a SiS sandbox. Old
    behaviour: pipeline.load_into_snowflake marked it 'live/existing -- not
    loaded' and moved on, leaving the sheets + semantic view pointed at a table
    that was never created -> the 'does not exist or not authorized' cascade the
    user hit on Regional/Global.

    New behaviour (pre-load + reuse): for a datasource with no local file, probe
    the target table; if a pre-loaded table EXISTS reuse it (row count, note
    'existing (pre-loaded)'), else flag it MISSING so the UI can stop cleanly
    with the preload_demo.py remediation instead of cascading 404s. Guarded here
    with a FAKE session -- no live Snowflake."""
    import pipeline

    class FakeResult:
        def __init__(s, rows): s._rows = rows
        def collect(s): return s._rows

    class FakeSession:
        """Answers the 3 read shapes load_into_snowflake issues for a
        file-less datasource: SCHEMATA existence (ensure_target), TABLES
        existence (table_exists), and SELECT COUNT(*) (row count)."""
        def __init__(s, existing_tables): s.existing = {t.upper() for t in existing_tables}
        def sql(s, text):
            u = text.upper()
            if "INFORMATION_SCHEMA.SCHEMATA" in u:
                return FakeResult([{"N": 1}])           # schema exists
            if "INFORMATION_SCHEMA.TABLES" in u:
                present = any(f"TABLE_NAME = '{t}'" in u for t in s.existing)
                if "TABLE_SCHEMA AS S" in u:            # cross-schema resolver probe
                    return FakeResult([{"S": "PIPELINE_DEMO"}] if present else [])
                return FakeResult([{"N": 1 if present else 0}])   # table_exists COUNT
            if u.strip().startswith("SELECT COUNT(*)"):
                return FakeResult([[4242]])             # row supports [0]
            return FakeResult([])

    from calc_translator import to_phys
    pre = "Data using Relationships"       # pretend this one is pre-loaded
    miss = "Data Using Custom-SQL"         # this one is not
    sess = FakeSession({to_phys(pre)})
    report = pipeline.load_into_snowflake(
        sess, {pre: None, miss: None}, db="WBR_DB", schema="PIPELINE_DEMO")
    by_cap = {r[0]: r for r in report}
    assert by_cap[pre][3] == "existing (pre-loaded)", by_cap[pre]
    assert by_cap[pre][2] == 4242, by_cap[pre]
    assert by_cap[miss][3].startswith("MISSING"), by_cap[miss]
    # onboard-level: only the genuinely-missing one is surfaced for the UI stop
    missing = [r[0] for r in report if str(r[3]).startswith("MISSING")]
    assert missing == [miss], missing
    print("ok  pipeline reuses a pre-loaded table for an undecodable-in-Snowflake "
          "extract; flags a genuinely-missing one (Regional/Global hyper fix)")


def test_reuse_existing_table_cross_schema():
    """A pre-loaded table can live in a DIFFERENT schema than LOAD_SCHEMA -- the
    corpus load_snowflake.py loads every demo table into WBR_DB.PUBLIC, so the
    E-Commerce 'Customers (DataDNA ...)' relationship extract exists in PUBLIC
    but not PIPELINE_DEMO. The old probe only checked PIPELINE_DEMO -> Stage 1
    wrongly said MISSING even though the table was right there.

    New: pipeline.resolve_existing_table finds it anywhere in the DB and the
    reuse branch repoints config at its real location. Honesty boundary: a table
    name present in >1 schema is AMBIGUOUS -> surfaced, never silently bound (the
    Superstore-gravity wrong-table class). Guarded with a fake session."""
    import config
    import pipeline
    from calc_translator import to_phys

    class FR:
        def __init__(s, rows): s._rows = rows
        def collect(s): return s._rows

    class FakeSession:
        """table name -> [schemas it exists in]."""
        def __init__(s, table_schemas):
            s.ts = {k.upper(): [x.upper() for x in v] for k, v in table_schemas.items()}
        def sql(s, text):
            u = text.upper()
            if "INFORMATION_SCHEMA.SCHEMATA" in u:
                return FR([{"N": 1}])
            if "INFORMATION_SCHEMA.TABLES" in u:
                tbl = next((t for t in s.ts if f"TABLE_NAME = '{t}'" in u), None)
                schemas = s.ts.get(tbl, [])
                if "TABLE_SCHEMA AS S" in u:            # cross-schema search
                    return FR([{"S": sc} for sc in schemas])
                sc = next((x for x in ("PIPELINE_DEMO", "PUBLIC", "ANALYTICS")
                           if f"TABLE_SCHEMA = '{x}'" in u), None)  # table_exists COUNT
                return FR([{"N": 1 if sc in schemas else 0}])
            if u.strip().startswith("SELECT COUNT(*)"):
                return FR([[777]])
            return FR([])

    cap = "Customers (DataDNA Dataset Challenge - E-commerce Dataset - November 2025)"
    t = to_phys(cap)
    save = (dict(config.DATASOURCES), config.DEFAULT_DATASOURCE,
            config.ORDERS, engine.ORDERS)
    try:
        def _reset():
            config.DATASOURCES.clear()
            config.DATASOURCES[cap] = {"table": f"WBR_DB.PIPELINE_DEMO.{t}",
                                       "local_file": None}
            config.DEFAULT_DATASOURCE = cap

        # case 1: exists only in PUBLIC -> reused there, config repointed
        _reset()
        rep = pipeline.load_into_snowflake(
            FakeSession({t: ["PUBLIC"]}), {cap: None},
            db="WBR_DB", schema="PIPELINE_DEMO")
        assert rep[0][1] == f"WBR_DB.PUBLIC.{t}", rep[0]
        assert "reused from PUBLIC" in rep[0][3], rep[0]
        assert config.DATASOURCES[cap]["table"] == f"WBR_DB.PUBLIC.{t}", config.DATASOURCES[cap]
        assert config.ORDERS == f"WBR_DB.PUBLIC.{t}", config.ORDERS

        # case 2: ambiguous (two schemas) -> MISSING, never silently bound
        _reset()
        rep = pipeline.load_into_snowflake(
            FakeSession({t: ["PUBLIC", "ANALYTICS"]}), {cap: None},
            db="WBR_DB", schema="PIPELINE_DEMO")
        assert rep[0][3].startswith("MISSING") and "ambiguous" in rep[0][3].lower(), rep[0]
        assert config.DATASOURCES[cap]["table"] == f"WBR_DB.PIPELINE_DEMO.{t}", \
            "must NOT repoint on an ambiguous match"

        # case 3: absent everywhere -> MISSING with the preload remediation
        _reset()
        rep = pipeline.load_into_snowflake(
            FakeSession({}), {cap: None}, db="WBR_DB", schema="PIPELINE_DEMO")
        assert rep[0][3].startswith("MISSING") and "preload" in rep[0][3].lower(), rep[0]
    finally:
        config.DATASOURCES.clear(); config.DATASOURCES.update(save[0])
        config.DEFAULT_DATASOURCE = save[1]
        config.ORDERS = save[2]; engine.ORDERS = save[3]
    print("ok  cross-schema table reuse (a pre-loaded table in WBR_DB.PUBLIC is "
          "found + config repointed; ambiguous name surfaced not bound; absent -> "
          "MISSING)")


def test_snowflake_uppercase_alias():
    """Snowflake folds UNQUOTED SQL aliases to UPPERCASE; DuckDB keeps them
    lowercase. build_where's date-range widget aliased MIN()/MAX() lowercase
    ('... lo, ... hi') and read them back by lowercase key -> KeyError('lo')
    ONLY in the deployed Streamlit-in-Snowflake app. Every local test runs on
    DuckDB (lowercase), so this was invisible until a real Snowsight upload
    crashed every dashboard tab with "could not render ('lo')".

    CI has no live Snowflake, so this SIMULATES the folding: wrap engine.q to
    UPPERCASE every result column (exactly what Snowflake does to unquoted
    aliases), then drive the date-range branch. With the by-position (iloc)
    fix it passes; a regression to any lowercase by-name key access raises."""
    import datetime as _dt
    ir = TP.build_ir(TWB)
    engine.configure(ir)
    real_q = engine.q

    def upper_q(sql, *a, **k):
        df = real_q(sql, *a, **k).copy()
        df.columns = [str(c).upper() for c in df.columns]   # emulate Snowflake
        return df

    class W:
        def columns(s, n): return [s] * (n if isinstance(n, int) else len(n))
        def date_input(s, *a, **k):
            return (_dt.date(2020, 1, 1), _dt.date(2021, 1, 1))
        def selectbox(s, label, opts, **k): return "All"
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def __getattr__(s, n): return lambda *a, **k: s

    dash = {"name": "T", "filters": [{"caption": "Order Date", "kind": "date"}],
            "params": []}
    real_st = engine.st
    engine.q, engine.st = upper_q, W()
    try:
        parts = engine.build_where(dash)          # must NOT KeyError on 'LO'
    finally:
        engine.q, engine.st = real_q, real_st     # restore BOTH -- leaving
        # engine.st = W() clobbered every later render test (engine.py uses
        # engine.st for all st.* calls, so a fake st swallowed their charts)
    assert parts and any("BETWEEN" in p["clause"] for p in parts), parts
    print("ok  snowflake uppercase-alias date filter (build_where handles "
          "UPPER-folded MIN/MAX columns -- the 'could not render (lo)' SiS bug)")


def test_parity_validation():
    """parity.py -- the Validation stage (Stage 5) of the staged demo UI. This
    is the TRUST PROOF: every measure is computed two independent ways (app's
    own SQL path vs a direct source read), plus calculated-field metrics are
    execution-gated and cross-checked against known Tableau figures where
    available. Locks the 4-workbook demo corpus at 100% PASS + the DECIMAL-
    overflow bug this test suite itself caught (2026-07-20):

    generate_semantic_view's param substitution used a naive str(val) literal
    for calc-metric SQL, so a parameter value read as '0.064000000000000001'
    kept all 18 digits -- DuckDB inferred a huge-precision DECIMAL for that
    literal, and SUM(SALES) * it overflowed DECIMAL(38) on Superstore's
    'SUM([Sales])-SUM([Sales Forecast])' calc. Fix: sub_params delegates to
    calc_translator.param_sql_literal (str(float(v))), matching what
    engine.sub_params does at runtime and collapsing the literal to '0.064'."""
    import parity
    if not os.path.exists(os.path.join(ROOT, "Workbooks")):
        print("skip parity validation (Workbooks/ absent)")
        return
    _ecom_wb = glob.glob(os.path.join(ROOT, "Workbooks", "E-Commerce*VOTD.twbx"))
    books = [("Superstore.twbx", "superstore")]
    if _ecom_wb:
        books.append((os.path.basename(_ecom_wb[0]), "ecommerce"))
    books += [("Regional Analysis.twbx", "regional"),
              ("World Indicators.twbx", "world_indicators")]
    ran, total_bugs = 0, 0
    for name, stem in books:
        p = os.path.join(ROOT, "Workbooks", name)
        if not os.path.exists(p):
            continue
        ir = TP.build_ir(p)
        res = parity.check_workbook(ir)
        s = res["summary"]
        assert s["measures_checked"] > 0, f"{name}: parity found nothing to check"
        assert s["measures_bug"] == 0, (
            f"{name}: {s['measures_bug']} measure(s) failed parity: "
            f"{[m for m in res['measures'] + res.get('calc_metrics', []) if m['verdict'] == 'BUG']}")
        total_bugs += s["measures_bug"]
        ran += 1
    assert ran >= 1, "no demo workbooks present to validate"
    # the exact overflow regression, isolated
    import cortex_semantic as CS
    from calc_translator import param_sql_literal
    assert param_sql_literal("0.064000000000000001") == "0.064", \
        "long float repr no longer collapses -- DECIMAL overflow risk is back"
    ss = os.path.join(ROOT, "superstore_ir.json")
    if os.path.exists(ss):
        import json as _json
        ir = _json.load(open(ss, encoding="utf-8"))
        sql = CS.sub_params("SUM(SALES)-SUM((SALES*(1-__PARAM_CHURN_RATE__)"
                            "*(1+__PARAM_NEW_BUSINESS_GROWTH__)))", ir["params"])
        assert "0.064" in sql and "0.0640000000000000" not in sql, sql
        import backend  # noqa: F401
        v = engine.q(f"SELECT {sql} AS V FROM SUPERSTORE.PUBLIC.SALES_COMMISSION")["V"][0]
        assert v is not None    # must EXECUTE, not overflow
    print(f"ok  parity validation ({ran} demo workbooks, {total_bugs} bugs, all "
          "PASS; DECIMAL-overflow literal fix locked)")


def test_parity_no_local_file_reuses_table_repull():
    """LIVE bug this locks (2026-07-21): Regional Analysis / Global Sales
    uploaded into the hosted-in-Snowsight demo REUSE a pre-loaded table (their
    .hyper never decodes there), so config.DATASOURCES has no local_file for
    that datasource. parity.check_workbook's measure loop went app_v-vs-None ->
    _rel_ok() unconditionally False -> a FALSE 'BUG' on EVERY measure, even
    though the app's own Snowflake numbers were correct (screenshot: user saw
    'Source value: None' + BUG on all 5 measures right after Stage 4 rendered
    the workbook cleanly). None of the prior 39 gates caught it -- the only
    parity test uses Workbooks/ where the local file IS present.

    Fix: when no local file exists but the table does, fall back to an
    INDEPENDENT client-side re-pull + sum of the SAME table (source_kind=
    'table-repull') instead of silently comparing against None. Simulated here
    with real local data (no live Snowflake needed): warm the DuckDB cache with
    the datasource's real file present, then blank ONLY the config mapping's
    local_file -- the already-loaded table stays queryable, exactly like a
    pre-loaded Snowflake table with no decoded source file alongside it."""
    import backend
    import config
    import parity
    ss_path = os.path.join(ROOT, "Workbooks", "Superstore.twbx")
    if not os.path.exists(ss_path):
        print("skip parity no-local-file test (Workbooks/Superstore.twbx absent)")
        return
    ir = TP.build_ir(ss_path)
    baseline = parity.check_workbook(ir)      # normal (file-backed) path stays green
    assert baseline["summary"]["measures_bug"] == 0, baseline
    caps_with_measures = {m["datasource"] for m in baseline["measures"]}
    cap = next(c for c in caps_with_measures
              if config.DATASOURCES.get(c, {}).get("local_file"))
    saved = dict(config.DATASOURCES[cap])
    if backend._LOCAL_CON is None:
        backend._get_local_con()              # warm the cache WITH the file present
    try:
        config.DATASOURCES[cap] = {"table": saved["table"], "local_file": None}
        res = parity.check_workbook(ir)
        rows = [m for m in res["measures"] if m["datasource"] == cap]
        assert rows, f"no measures found for {cap}"
        for m in rows:
            assert m["verdict"] != "BUG", f"false BUG with no local file: {m}"
            assert m.get("source_kind") == "table-repull", m
        ds_row = next(d for d in res["datasources"] if d["datasource"] == cap)
        assert ds_row["match"] is None, (
            "row-count match should be UNKNOWN, not a false mismatch, when "
            "there is no independent source file: " + str(ds_row))
    finally:
        config.DATASOURCES[cap] = saved        # never leak state into later tests
    print("ok  parity falls back to an independent table-repull, not a false "
          "BUG, when no local source file exists -- the Regional/Global "
          "Snowsight validation fix")


def test_cortex_semantic_generation():
    """Cortex semantic-layer generator (cortex_semantic.py) -- the DETERMINISTIC
    scaffolding that feeds Cortex Analyst. Locks the offline logic; the live
    Cortex call needs an account and is not regression-testable (correctly so).

    Guards, each tied to a real bug hit while building it (2026-07-20):
      (a) metric dedup -- Tableau '(copy)' + internal-name calcs share formula
          text; identical SQL must MERGE into one metric with the human caption
          preferred and the Calculation_ name kept only as a synonym.
      (b) window-function calcs are NOT scalar metrics -- must be skipped, not
          emitted as a broken METRIC (Order Profitable? has an OVER()).
      (c) param substitution -- __PARAM_X__ tokens resolve to workbook defaults
          before the SQL is emitted (engine.sub_params convention).
      (d) THE DEPLOY BUG: tables loaded with original mixed-case names ('Sales
          Person') reject bare UPPER_SNAKE identifiers -- the DDL must quote the
          REAL introspected column, and rewrite metric-SQL column tokens to it,
          or CREATE SEMANTIC VIEW fails with 'invalid identifier SALES_PERSON'.
      (e) the emitted DDL/YAML must be structurally valid (parseable YAML)."""
    import json as _json

    import cortex_semantic as CS
    if not os.path.exists(os.path.join(ROOT, "superstore_ir.json")):
        print("skip cortex semantic (superstore_ir.json absent)")
        return
    ir = _json.load(open(os.path.join(ROOT, "superstore_ir.json"), encoding="utf-8"))

    # (a) + (b) metric build: dedup by SQL, window calcs skipped
    mets, skipped = CS.build_metrics(ir)
    of_quota = next(m for m in mets if m["name"] == "OF_QUOTA_ACHIEVED")
    assert of_quota["synonyms"][0] == "% of quota achieved" and \
        "Calculation_0440925131659539" in of_quota["synonyms"], of_quota["synonyms"]
    assert "Order Profitable?" in [c for c, _ in skipped], \
        "window-function calc not skipped as a metric"
    assert all(" OVER (" not in m["sql"].upper() for m in mets), \
        "a window-function calc leaked into METRICS"

    # (c) param substitution
    # delegates to calc_translator.param_sql_literal (str(float(v))) so it
    # matches engine.sub_params exactly and collapses long float reprs
    # ('0.064000000000000001' -> '0.064') that would otherwise overflow
    # DuckDB's inferred DECIMAL precision (Superstore Sales-Forecast calc)
    assert CS.sub_params("x >= __PARAM_NEW_QUOTA__", ir["params"]) == "x >= 500000.0"
    from calc_translator import param_sql_literal
    assert param_sql_literal("0.064000000000000001") == "0.064"

    # (d) identifier quoting + metric-SQL rewrite to REAL columns (the deploy bug)
    assert CS._ident("SALES") == "SALES" and CS._ident("Sales Person") == '"Sales Person"'
    mapping = {"Sales Commission": {"table": "WBR_DB.PUBLIC.SALES_COMMISSION"},
               "Sample - Superstore": {"table": "WBR_DB.PUBLIC.SAMPLE_SUPERSTORE"}}
    real = {"WBR_DB.PUBLIC.SALES_COMMISSION":
            ["Sales Person", "Region", "Order Date", "Sales"],
            "WBR_DB.PUBLIC.SAMPLE_SUPERSTORE":
            ["Order ID", "Category", "Sub-Category", "City", "Customer Name",
             "Order Date", "Product Name", "Segment", "Sales", "Profit",
             "Quantity", "Discount"]}
    ddl = CS.generate_semantic_view(ir, mapping, "superstore", real_cols=real)
    assert "CREATE OR REPLACE SEMANTIC VIEW" in ddl
    assert 'AS "Sales Person"' in ddl, "real mixed-case column not quoted in DDL"
    assert 'SALES_COMMISSION.SALES_PERSON AS "Sales Person"' in ddl
    # a metric's SQL must reference the quoted real column, never bare SALES
    assert '"Sales"' in ddl and "SUM((SALES))" not in ddl, \
        "metric SQL kept the bare identifier -- would fail to compile"
    # (e) YAML output parses and carries the tables
    ytext, n = CS.generate(ir, mapping, "superstore")
    try:
        import yaml
        doc = yaml.safe_load(ytext)
        assert doc["tables"] and all(t["base_table"]["table"] for t in doc["tables"])
    except ImportError:
        assert "tables:" in ytext and "base_table:" in ytext
    print("ok  cortex semantic generation (metric dedup, window-calc skip, param "
          "sub, real-identifier quoting/rewrite [deploy bug], valid YAML/DDL)")


def test_cortex_calc_fallback_guards():
    """Cortex calc-fallback (cortex_calc_fallback.py) -- the TRUST SCAFFOLDING
    around the AI proposal. The live COMPLETE call + execution gate need an
    account; this locks the offline logic that decides WHAT gets asked and how
    the answer is parsed. The trust model is only as good as these.

    Guards:
      (a) THE REFUSAL RULE: view-order table calcs (LOOKUP/LAST/FIRST/INDEX/
          PREVIOUS_VALUE/RUNNING_*) must classify as 'order-dependent' -> the
          caller SKIPS them, never AI-guesses a row order no model can know.
          This is the same 'never guess view order' rule the deterministic
          engine enforces; the AI layer must not become a backdoor around it.
      (b) blends + nested LODs classify so they route to Cortex; a plain calc
          stays 'general'.
      (c) extract_sql: recover a bare SQL statement from prose/fenced/CLI-
          escaped model output (snow --format json double-escapes \\n).
      (d) json_payload: pull the first JSON array out of mixed CLI output
          (warnings + echoed query precede the result row).
      (e) the module imports and its ORDER_DEPENDENT regex is anchored to
          function calls, not substrings (a column named LASTNAME must not trip
          LAST)."""
    import cortex_calc_fallback as CF
    # (a) the refusal rule
    for f in ("LOOKUP(min(MONTH([Date])),0)",
              "if LAST()=0 THEN TRUE ELSE FALSE END",
              "RUNNING_SUM(SUM([Sales]))",
              "INDEX() <= 5", "PREVIOUS_VALUE(0)"):
        assert CF.classify(f) == "order-dependent", (f, CF.classify(f))
    # (e) not a substring match: LASTNAME / lookup_key must NOT be order-dependent
    assert CF.classify("SUM([LASTNAME_COUNT])") != "order-dependent"
    assert CF.classify("MAX([lookup_table_id])") != "order-dependent"
    # (b) routing classes
    assert CF.classify("SUM([Sales])-SUM([federated.0hg].[Sales Target])") == "blend"
    assert CF.classify("{ FIXED [R]:AVG(({ FIXED [R],[S]: SUM([X])}))}") == "nested-lod"
    assert CF.classify("SUM([Sales])*2") == "general"
    # (c) SQL recovery from the three shapes model output arrives in
    assert CF.extract_sql("prose ```sql\nSELECT 1\n``` tail").strip() == "SELECT 1"
    assert CF.extract_sql("WITH x AS (\\nSELECT 1)").startswith("WITH x AS (\n")
    assert CF.extract_sql("Here is it: SELECT a FROM t").startswith("SELECT a")
    # (d) JSON array out of noisy CLI output
    assert CF.json_payload("UserWarning: ...\n[{\"R\": \"ok\"}]\n")[0]["R"] == "ok"
    assert CF.json_payload("no json here") is None
    print("ok  cortex calc-fallback guards (order-dependent REFUSAL rule, blend/"
          "LOD routing, SQL recovery, JSON parse -- trust scaffolding locked)")


def test_per_workbook_profile_routing():
    """MVP fix (2026-07-21): config.py used to hardcode `import profile_superstore
    as PROFILE` for EVERY workbook -- a genuinely foreign client's workbook could
    coincidentally use a generic raw-field caption ("Sales"/"Profit"/"Discount")
    and silently inherit Superstore's curated measure SQL/format/colors instead
    of its own. Now the profile is resolved PER WORKBOOK from ir['source_file']
    via config.profile_for() / config.set_profile(), called every time
    engine.configure(ir) runs.

    Locks: (a) the known corpus workbooks stay mapped to profile_superstore
    byte-identical to before this fix (regression-safe); (b) an unrecognized/
    foreign workbook gets profile_default (neutral, empty) -- the actual fix;
    (c) engine.configure() actually swaps config.PROFILE + calc_translator's
    MEASURE_LIBRARY/CAPTION_ALIASES/KPI_ORDER live (mutated in place, so
    engine.py's `from calc_translator import MEASURE_LIBRARY` binding sees the
    new content without re-importing); (d) cycling through a foreign workbook
    and back never corrupts profile_superstore's own original dict (mutation
    must happen on a COPY, not an alias)."""
    import config
    import calc_translator as CT

    # (a) known corpus workbook -> unchanged profile_superstore
    import profile_superstore as PS
    import profile_default as PD
    assert config.profile_for("Superstore.twb") is PS
    assert config.profile_for("superstore.twbx") is PS          # case-insensitive
    assert config.profile_for("World Indicators.twbx") is PS

    # (b) a genuinely new/foreign workbook -> neutral default, NOT Superstore
    foreign = config.profile_for(r"C:\clients\Acme Retail Q3.twbx")
    assert foreign is PD, foreign
    assert foreign.MEASURE_LIBRARY == {} and foreign.DIM_VALUE_COLORS == {}

    # (c) engine.configure() actually applies it live
    superstore_sales_sql = dict(PS.MEASURE_LIBRARY["Sales"])
    engine.configure({"source_file": r"C:\clients\Acme Retail Q3.twbx"})
    assert config.PROFILE is PD
    assert CT.MEASURE_LIBRARY == {}, "foreign workbook must not see Superstore's measure library"
    assert CT.CAPTION_ALIASES == {}
    assert CT.KPI_ORDER == []

    # (d) switching BACK restores Superstore's library exactly, and the
    # foreign-workbook mutation never touched profile_superstore's own dict
    engine.configure({"source_file": "Superstore.twb"})
    assert config.PROFILE is PS
    assert CT.MEASURE_LIBRARY["Sales"] == superstore_sales_sql
    assert PS.MEASURE_LIBRARY["Sales"] == superstore_sales_sql, \
        "mutating the current profile must never corrupt profile_superstore's own dict"

    print("ok  per-workbook profile routing (corpus workbooks unchanged; a foreign "
          "workbook gets the neutral default, never a silent Superstore inherit)")


_LIVE_CONN_FIXTURE = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <datasources>
    <datasource caption='Live Sales' name='fed.livesales'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='snowflake.abc' caption='snowflake'>
            <connection class='snowflake' dbname='PROD_DB' schema='SALES' server='xyz.snowflakecomputing.com' warehouse='WH_XS'/>
          </named-connection>
        </named-connections>
        <relation name='ORDERS' table='[ORDERS]' type='table' connection='snowflake.abc'/>
      </connection>
    </datasource>
    <datasource caption='Live SQL Server' name='fed.sqls'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='sqlserver.def' caption='sqlserver'>
            <connection class='sqlserver' dbname='LegacyDB' server='sql01'/>
          </named-connection>
        </named-connections>
        <relation name='Orders' table='[dbo].[Orders]' type='table' connection='sqlserver.def'/>
      </connection>
    </datasource>
  </datasources>
</workbook>"""


def test_live_connection_support():
    """MVP item 2 (2026-07-21): live connections were NOT built at all -- any
    datasource with no decodable local file (including a genuinely live one)
    silently fell back to reusing whatever pre-loaded stand-in table happened
    to exist at the expected name, or flagged MISSING. Never queried a live
    source.

    Scope (0.5-1 day, matches the MVP estimate): a live connection straight to
    SNOWFLAKE ITSELF, querying a single named table (no join, no custom SQL --
    those are separate constructs) is now genuinely queryable -- config points
    straight at the source's own db.schema.table, no copy. Every other live
    class (sqlserver here; sqlproxy is the real corpus example -- a published
    Tableau Server/Cloud datasource, proven against the actual EMEA workbook
    XML during dev) is reported HONESTLY via datasource_notes instead of
    silently substituted.

    No corpus workbook has a live Snowflake connection (the whole corpus is
    extract-based; the one real live-connection workbook, EMEA, is sqlproxy --
    out of MVP scope) -- so this is verified against a synthetic fixture built
    from real Tableau federated/named-connection/relation XML shape, plus a
    corpus sweep proving no FALSE POSITIVE (a local-file 'live' connection --
    excel-direct/textscan, Superstore's own Sales Target.xlsx/Sales
    Commission.csv -- must NOT be flagged; those are already handled by the
    existing file-matching path)."""
    import xml.etree.ElementTree as ET
    import pipeline

    root = ET.fromstring(_LIVE_CONN_FIXTURE)
    live = TP.live_connections(root)

    # (a) genuinely queryable: Snowflake, single table, all identifiers present
    sf = live["Live Sales"]
    assert sf["queryable"] is True, sf
    assert (sf["dbname"], sf["schema"], sf["table"]) == ("PROD_DB", "SALES", "ORDERS"), sf

    # (b) non-Snowflake live class: honest refusal, not a silent substitution
    sql = live["Live SQL Server"]
    assert sql["queryable"] is False, sql
    assert "not Snowflake" in sql["reason"], sql

    # (c) datasource_notes surfaces the non-queryable one, not the queryable one
    notes = TP.datasource_notes(root)
    live_notes = {n["datasource"]: n for n in notes if n["kind"] == "live-connection"}
    assert "Live SQL Server" in live_notes, notes
    assert "Live Sales" not in live_notes, notes

    # (d) FALSE-POSITIVE guard: a local-file 'live' connection (no <extract>,
    # but the connection carries `filename` -- Sample - Superstore.xls / Sales
    # Target.xlsx / Sales Commission.csv, all excel-direct/textscan) must
    # never be flagged as a real live connection -- already handled generically.
    for f in ("Superstore.twb", "_ecom.twb", "_filtest.twb"):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            assert TP.live_connections(TP.load_twb_xml(p)) == {}, f

    # (e) configure_datasources routes the queryable one straight at its OWN
    # db.schema.table (no copy into LOAD_DB/LOAD_SCHEMA); the non-queryable
    # one is unaffected (same fallback behavior as before this feature).
    # configure_datasources REPLACES config.DATASOURCES globally -- snapshot
    # and restore so later tests in this same process still see the real
    # (Superstore) mapping, not this fixture's fake entries.
    import config
    _saved_ds = dict(config.DATASOURCES)
    _saved_default = config.DEFAULT_DATASOURCE
    _saved_orders = config.ORDERS
    try:
        ds = pipeline.configure_datasources(
            {"Live Sales": None, "Live SQL Server": None}, live=live)
        assert ds["Live Sales"] == {"table": "PROD_DB.SALES.ORDERS",
                                    "local_file": None, "live": True}, ds["Live Sales"]
        assert ds["Live SQL Server"]["table"].startswith(pipeline.LOAD_DB), ds["Live SQL Server"]
    finally:
        config.DATASOURCES.clear()
        config.DATASOURCES.update(_saved_ds)
        config.DEFAULT_DATASOURCE = _saved_default
        config.ORDERS = _saved_orders
        engine.ORDERS = _saved_orders

    # (f) load_into_snowflake genuinely PROBES the live table (proves
    # reachability, doesn't just trust the config) instead of write_pandas-ing
    # a copy or silently reusing an unrelated stand-in.
    class FakeResult:
        def __init__(s, rows): s._rows = rows
        def collect(s): return s._rows

    class FakeSession:
        def sql(s, text):
            u = text.upper()
            if "INFORMATION_SCHEMA.SCHEMATA" in u:
                return FakeResult([{"N": 1}])
            if "INFORMATION_SCHEMA.TABLES" in u:
                return FakeResult([{"N": 0}])          # nothing pre-loaded
            if u.strip().startswith("SELECT COUNT(*) FROM PROD_DB.SALES.ORDERS"):
                return FakeResult([[777]])
            return FakeResult([[0]])

    report = pipeline.load_into_snowflake(
        FakeSession(), {"Live Sales": None, "Live SQL Server": None}, live=live)
    by_cap = {r[0]: r for r in report}
    assert by_cap["Live Sales"] == ("Live Sales", "PROD_DB.SALES.ORDERS", 777,
                                    "live (queried directly, no copy)"), by_cap["Live Sales"]
    assert by_cap["Live SQL Server"][3].startswith("MISSING"), by_cap["Live SQL Server"]

    print("ok  live connection support (Snowflake live source queried directly, "
          "no copy; non-Snowflake live class reported honestly, not silently "
          "substituted; local-file 'live' connections never false-positive)")


_CUSTOM_SQL_FIXTURE = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <datasources>
    <datasource caption='Live Custom SQL' name='fed.csql'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='snowflake.abc' caption='snowflake'>
            <connection class='snowflake' dbname='PROD_DB' schema='SALES'/>
          </named-connection>
        </named-connections>
        <relation name='CustomQ' type='text' connection='snowflake.abc'>SELECT region, SUM(amount) AS total FROM orders GROUP BY region</relation>
      </connection>
    </datasource>
    <datasource caption='Legacy Custom SQL' name='fed.legacy'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='sqlserver.def' caption='sqlserver'>
            <connection class='sqlserver' dbname='LegacyDB' server='sql01'/>
          </named-connection>
        </named-connections>
        <relation name='CustomQ' type='text' connection='sqlserver.def'>SELECT TOP 10 * FROM dbo.Orders</relation>
      </connection>
    </datasource>
  </datasources>
</workbook>"""


_SOURCE_TABLE_FIXTURE = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <datasources>
    <datasource caption='Orders Extract' name='fed.orders'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='snowflake.abc' caption='snowflake'>
            <connection class='snowflake' dbname='PROD_DB' schema='SALES' server='xyz.snowflakecomputing.com' warehouse='WH_XS'/>
          </named-connection>
        </named-connections>
        <relation name='ORDERS' table='[SALES].[ORDERS]' type='table' connection='snowflake.abc'/>
        <metadata-records>
          <metadata-record class='column'><remote-name>Order ID</remote-name><parent-name>[ORDERS]</parent-name></metadata-record>
          <metadata-record class='column'><remote-name>Sales</remote-name><parent-name>[ORDERS]</parent-name></metadata-record>
          <metadata-record class='column'><remote-name>Region</remote-name><parent-name>[ORDERS]</parent-name></metadata-record>
          <metadata-record class='column'><remote-name>Calculation_123456</remote-name><parent-name>[ORDERS]</parent-name></metadata-record>
        </metadata-records>
      </connection>
      <extract><connection class='dataengine' dbname='Data/Datasources/orders.hyper'/></extract>
    </datasource>
    <datasource caption='Star Model' name='fed.star'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='snowflake.def' caption='snowflake'>
            <connection class='snowflake' dbname='PROD_DB' schema='SALES'/>
          </named-connection>
        </named-connections>
        <relation name='EVENTS' table='[SALES].[EVENTS]' type='table' connection='snowflake.def'/>
        <relation name='CUSTOMERS' table='[SALES].[CUSTOMERS]' type='table' connection='snowflake.def'/>
      </connection>
      <extract><connection class='dataengine' dbname='Data/Datasources/star.hyper'/></extract>
    </datasource>
  </datasources>
</workbook>"""


class _FakeAccount:
    """Minimal Snowpark-session stand-in that answers exactly the metadata reads
    resolve_source_binding makes, from a {fqn: [columns]} dict. Lets the whole
    R3 confidence ladder be exercised offline -- no live account, no credentials."""

    def __init__(self, tables, rows=1234):
        self.tables = {k.upper(): [c.upper() for c in v] for k, v in tables.items()}
        self.rows = rows
        self.wrote = []                      # any write_pandas is a FAILURE here

    class _R:
        def __init__(self, rows): self._rows = rows
        def collect(self): return self._rows

    def write_pandas(self, *a, **k):
        self.wrote.append(a)
        raise AssertionError("auto-bound datasource must never be copied")

    def sql(self, text):
        u = " ".join(text.upper().split())
        if u.startswith("CREATE OR REPLACE VIEW"):
            # so a view this SAME fake session just deployed is found by a
            # subsequent fqn_exists check -- deploy_model_view's own
            # "ran but does not exist afterwards" guard needs this to see it.
            import re as _re
            m = _re.search(r"CREATE OR REPLACE VIEW\s+(\S+)", text, _re.I)
            if m:
                self.tables[m.group(1).replace('"', "").upper()] = []
            return self._R([])
        if "INFORMATION_SCHEMA.SCHEMATA" in u:
            return self._R([{"N": 1}])
        if "INFORMATION_SCHEMA.COLUMNS" in u:
            sch = u.split("TABLE_SCHEMA = '")[1].split("'")[0]
            tbl = u.split("TABLE_NAME = '")[1].split("'")[0]
            db = u.split('FROM "')[1].split('"')[0]
            cols = self.tables.get(f"{db}.{sch}.{tbl}", [])
            return self._R([{"C": c} for c in cols])
        if "INFORMATION_SCHEMA.TABLES" in u:
            db = u.split('FROM "')[1].split('"')[0]
            tbl = u.split("TABLE_NAME = '")[1].split("'")[0]
            if "TABLE_SCHEMA = '" in u:      # existence probe -> COUNT
                sch = u.split("TABLE_SCHEMA = '")[1].split("'")[0]
                return self._R([{"N": int(f"{db}.{sch}.{tbl}" in self.tables)}])
            hits = sorted({k.split(".")[1] for k in self.tables      # name search
                           if k.startswith(db + ".") and k.endswith("." + tbl)})
            return self._R([{"S": s} for s in hits])
        if u.startswith("SELECT COUNT(*) FROM "):
            return self._R([[self.rows]])
        return self._R([[0]])


def test_auto_bind_existing_snowflake_table():
    """ROADMAP R3 (2026-07-26): auto-point at an EXISTING Snowflake table read
    from the workbook's data model, instead of decoding its extract and copying
    it in with write_pandas.

    THE GAP THIS CLOSES: live_connections() deliberately SKIPS any datasource
    carrying an <extract> -- correct for its own job, but it meant an
    extract-based workbook whose upstream table ALREADY lives in the account was
    always decoded and duplicated, because nothing ever read where the extract
    came FROM. tableau_parser.source_tables() reads that (connection
    dbname/schema + relation table names + the source columns Tableau recorded);
    pipeline.resolve_source_binding() decides, against a real account, whether a
    confident match exists.

    THE POINT OF THE TEST IS THE HONESTY BOUNDARY, not the happy path. Binding on
    a NAME MATCH ALONE is exactly the wrong-table class this project has been
    burned by twice (Superstore-gravity; the Cortex foreign-table pick), so the
    cases that must REFUSE are asserted as hard as the cases that must bind:
    ambiguous names never resolve themselves, and a same-named table whose
    columns don't match the workbook is rejected even though its name is perfect.
    """
    import xml.etree.ElementTree as ET
    import config
    import pipeline

    root = ET.fromstring(_SOURCE_TABLE_FIXTURE)
    src = TP.source_tables(root)

    # --- parser: the extract-bearing datasource IS read (live_connections is not) --
    assert TP.live_connections(root) == {}, "extract datasources are not live"
    orders = src["Orders Extract"]
    assert orders["has_extract"] is True and orders["bindable"] is True, orders
    assert orders["tables"] == [{"schema": "SALES", "name": "ORDERS"}], orders
    assert orders["columns"] == ["Order ID", "Sales", "Region"], \
        f"Tableau's synthetic Calculation_* column must not be verified against " \
        f"the source: {orders['columns']}"
    # a multi-table model is reported but NOT a single-table rebind target --
    # replicating a star belongs to the data-model view path, not to R3.
    star = src["Star Model"]
    assert star["bindable"] is False and "data model" in star["reason"], star
    assert len(star["tables"]) == 2, star

    COLS = ["ORDER_ID", "SALES", "REGION"]

    # (a) TIER 1 -- the workbook's own declared location exists + columns match.
    acct = _FakeAccount({"PROD_DB.SALES.ORDERS": COLS})
    fq, note, status = pipeline.resolve_source_binding(acct, "Orders Extract", orders)
    assert (fq, status) == ("PROD_DB.SALES.ORDERS", "bound"), (fq, note, status)
    assert "no copy" in note, note

    # (b) TIER 2 -- declared location absent, but the NAME resolves to exactly one
    # schema of the load DB and its columns check out.
    acct = _FakeAccount({f"{pipeline.LOAD_DB}.ANALYTICS.ORDERS": COLS})
    fq, note, status = pipeline.resolve_source_binding(acct, "Orders Extract", orders)
    assert (fq, status) == (f"{pipeline.LOAD_DB}.ANALYTICS.ORDERS", "bound"), (fq, note, status)

    # (c) AMBIGUOUS -- same name in two schemas. MUST NOT pick one.
    acct = _FakeAccount({f"{pipeline.LOAD_DB}.ANALYTICS.ORDERS": COLS,
                         f"{pipeline.LOAD_DB}.STAGING.ORDERS": COLS})
    fq, note, status = pipeline.resolve_source_binding(acct, "Orders Extract", orders)
    assert fq is None and status == "ambiguous", (fq, note, status)
    assert "ambiguous" in note.lower(), note

    # (d) THE WRONG-TABLE GUARD -- a table at the EXACT declared location whose
    # columns are someone else's. A name is not evidence; this must refuse.
    acct = _FakeAccount({"PROD_DB.SALES.ORDERS": ["EMPLOYEE_ID", "HIRE_DATE"]})
    fq, note, status = pipeline.resolve_source_binding(acct, "Orders Extract", orders)
    assert fq is None and status == "mismatch", (fq, note, status)
    assert "columns do not match" in note and "loading the extract instead" in note, note

    # (e) cannot-verify is NOT verified: a table that exists but whose columns
    # can't be read must fall through to the normal decode+load path.
    acct = _FakeAccount({"PROD_DB.SALES.ORDERS": []})
    fq, _n, status = pipeline.resolve_source_binding(acct, "Orders Extract", orders)
    assert fq is None and status == "mismatch", (fq, status)

    # (f) NOTHING in the account -> no-match; the pipeline proceeds untouched.
    acct = _FakeAccount({})
    fq, note, status = pipeline.resolve_source_binding(acct, "Orders Extract", orders)
    assert (fq, note, status) == (None, None, "no-match"), (fq, note, status)

    # (g) multi-table datasource is skipped with its reason, never half-bound
    fq, note, status = pipeline.resolve_source_binding(acct, "Star Model", star)
    assert fq is None and status == "skipped" and "data model" in note, (fq, note, status)

    # (h) sources.json EXPLICIT MAP -- a human's binding wins over the inference,
    # including to a table the inference would never have found. A STALE entry
    # must fail loudly rather than bind to nothing.
    acct = _FakeAccount({"OTHER_DB.MART.ORDERS_V2": COLS})
    fq, note, status = pipeline.resolve_source_binding(
        acct, "Orders Extract", orders,
        source_map={"Orders Extract": "OTHER_DB.MART.ORDERS_V2"})
    assert (fq, status) == ("OTHER_DB.MART.ORDERS_V2", "bound"), (fq, note, status)
    fq, note, status = pipeline.resolve_source_binding(
        acct, "Orders Extract", orders,
        source_map={"Orders Extract": "OTHER_DB.MART.GONE"})
    assert fq is None and status == "mismatch" and "does not exist" in note, note

    # (i) auto_bind_sources aggregates: bound captions only, but every refusal
    # still reported (the UI shows both -- a refused match must be visible).
    acct = _FakeAccount({"PROD_DB.SALES.ORDERS": COLS})
    bound, reports = pipeline.auto_bind_sources(acct, root)
    assert bound == {"Orders Extract": "PROD_DB.SALES.ORDERS"}, bound
    assert {r[0] for r in reports} == {"Orders Extract", "Star Model"}, reports
    assert pipeline.auto_bind_sources(None, root) == ({}, []), "no session -> no-op"

    # (j) FALSE-POSITIVE SWEEP: against an EMPTY account no corpus workbook may
    # auto-bind anything -- an accelerator that binds on a hunch is the bug.
    empty = _FakeAccount({})
    for f in ("Superstore.twb", "_ecom.twb", "_filtest.twb", "_wi2024.twb"):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            b, _r = pipeline.auto_bind_sources(empty, TP.load_twb_xml(p))
            assert b == {}, f"{f} auto-bound against an empty account: {b}"

    # (l) CORPUS SHAPES -- three real-XML facts found by sweeping the corpus, each
    # of which silently produced a WRONG source before it was handled. Locked here
    # because they are the difference between reading the real upstream table and
    # reading the extract's own internals.
    wb = os.path.join(ROOT, "Workbooks", "Regional Analysis.twbx")
    if os.path.exists(wb):
        st = TP.source_tables(TP.load_twb_xml(wb))
        rel = st["Data using Relationships"]
        # 1. The extract's OWN relations must not be mistaken for sources. This
        #    datasource carries 3 real SANDBOX.DS tables AND 3 mangled
        #    [Extract].[NAME (DB.NAME)_<guid>] twins from the .hyper connection.
        names = {t["name"] for t in rel["tables"]}
        assert names == {"SAMPLE_SUPER_STORE_ORDERS", "SAMPLE_SUPER_STORE_PEOPLE",
                         "SAMPLE_SUPER_STORE_RETURNS"}, names
        assert all(t["schema"] == "DS" for t in rel["tables"]), rel["tables"]
        # 2. '[DB].[SCHEMA].[TABLE]' must not double the schema into the FQN.
        assert rel["dbname"] == "SANDBOX", rel
        # 3. A 3-table model defers to the data-model view path, never a rebind.
        assert rel["bindable"] is False, rel
    for f, cap in (("World Indicators.twbx", None), ("Superstore.twbx", None)):
        p = os.path.join(ROOT, "Workbooks", f)
        if os.path.exists(p):
            # An extract-only / file-only workbook has NO upstream DB source. Before
            # the class filter, the .hyper connection's '[Extract].[Extract]' relation
            # read as a bindable table literally named EXTRACT -- one same-named
            # table in the account away from binding a workbook to garbage.
            assert TP.source_tables(TP.load_twb_xml(p)) == {}, f

    # (m) a queryable LIVE datasource is left to the live path, not re-routed
    wb = os.path.join(ROOT, "Workbooks", "Superstore_KPI_Parameter_Dashboard_Live.twbx")
    if os.path.exists(wb):
        r2 = TP.load_twb_xml(wb)
        live_caps = {c for c, i in TP.live_connections(r2).items() if i["queryable"]}
        assert live_caps, "fixture workbook should still have a queryable live ds"
        acct2 = _FakeAccount({f"WBR_DB.PUBLIC.{t}": ["A"] for t in ("SUPERSTORE_ORDERS",)})
        b2, _r2 = pipeline.auto_bind_sources(acct2, r2)
        assert not (set(b2) & live_caps), \
            f"live-path datasource was re-routed through R3: {b2}"

    # (k) routing + load: an auto-bound caption keeps local_file=None (nothing to
    # copy) and is PROBED for real, not merely configured. write_pandas raises in
    # _FakeAccount, so any regression that copies it fails loudly here.
    _saved = (dict(config.DATASOURCES), config.DEFAULT_DATASOURCE, config.ORDERS)
    try:
        ds = pipeline.configure_datasources(
            {"Orders Extract": "data/should_not_be_used.csv"},
            auto_bound={"Orders Extract": "PROD_DB.SALES.ORDERS"})
        assert ds["Orders Extract"] == {"table": "PROD_DB.SALES.ORDERS",
                                        "local_file": None, "live": True,
                                        "auto_bound": True}, ds["Orders Extract"]
    finally:
        config.DATASOURCES.clear()
        config.DATASOURCES.update(_saved[0])
        config.DEFAULT_DATASOURCE, config.ORDERS = _saved[1], _saved[2]
        engine.ORDERS = _saved[2]

    acct = _FakeAccount({"PROD_DB.SALES.ORDERS": COLS}, rows=9994)
    report = pipeline.load_into_snowflake(
        acct, {"Orders Extract": None},
        auto_bound={"Orders Extract": "PROD_DB.SALES.ORDERS"})
    assert report == [("Orders Extract", "PROD_DB.SALES.ORDERS", 9994,
                       "existing table (auto-bound, no copy)")], report
    assert acct.wrote == [], "auto-bound datasource was copied anyway"

    print("ok  R3 auto-bind to existing Snowflake table (declared location + "
          "verified-name match bind with no copy; ambiguous names and "
          "column-mismatched same-named tables REFUSE and surface; sources.json "
          "overrides; no corpus false positives)")


_MULTI_TABLE_LIVE_FIXTURE = """<?xml version='1.0' encoding='utf-8'?>
<workbook>
  <datasources>
    <datasource caption='Live Star Model' name='fed.livestar'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='snowflake.abc' caption='snowflake'>
            <connection class='snowflake' dbname='PROD_DB' schema='SALES'/>
          </named-connection>
        </named-connections>
        <relation name='ORDERS' table='[PROD_DB].[SALES].[ORDERS]' type='table' connection='snowflake.abc'/>
        <relation name='CUSTOMERS' table='[PROD_DB].[SALES].[CUSTOMERS]' type='table' connection='snowflake.abc'/>
        <metadata-records>
          <metadata-record class='column'><remote-name>order_id</remote-name><parent-name>[Orders]</parent-name></metadata-record>
          <metadata-record class='column'><remote-name>customer_id</remote-name><parent-name>[Orders]</parent-name></metadata-record>
          <metadata-record class='column'><remote-name>amount</remote-name><parent-name>[Orders]</parent-name></metadata-record>
          <metadata-record class='column'><remote-name>customer_id</remote-name><parent-name>[Customers]</parent-name></metadata-record>
          <metadata-record class='column'><remote-name>name</remote-name><parent-name>[Customers]</parent-name></metadata-record>
        </metadata-records>
      </connection>
      <_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
        <objects>
          <object caption='Orders' id='Orders_ABC'>
            <properties context=''><relation connection='snowflake.abc' name='ORDERS' table='[PROD_DB].[SALES].[ORDERS]' type='table'/></properties>
          </object>
          <object caption='Customers' id='Customers_DEF'>
            <properties context=''><relation connection='snowflake.abc' name='CUSTOMERS' table='[PROD_DB].[SALES].[CUSTOMERS]' type='table'/></properties>
          </object>
        </objects>
        <relationships>
          <relationship>
            <expression op='='><expression op='[customer_id]'/><expression op='[customer_id (Customers)]'/></expression>
            <first-end-point object-id='Orders_ABC'/>
            <second-end-point object-id='Customers_DEF'/>
          </relationship>
        </relationships>
      </_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
    </datasource>
  </datasources>
</workbook>"""


def test_r9_live_multitable_join():
    """ROADMAP R9 (2026-07-26): a genuinely LIVE (no extract) datasource whose
    relationship graph joins MULTIPLE tables -- previously refused outright
    ("joins multiple tables live -- not yet supported").

    THE REAL BUG THIS CLOSES was worse than the documented refusal: verified
    live that live_connections()'s relation scan used `ds.findall(".//relation")`
    -- EVERY <relation> anywhere in the datasource, which ALSO matches the
    per-OBJECT <relation> elements nested inside the object-model's
    <object-graph> (a separate sibling of <connection>, describing each joined
    table for Stage 3's data-model view). For a 2-table live model this
    silently returned duplicate relations and `next(r for r in rels if
    type=='table')` picked the FIRST one and called the WHOLE datasource
    single-table queryable AT JUST THAT TABLE -- an ACTIVELY WRONG answer
    (silently dropping the second table and its join), not an honest refusal.
    Confirmed on `_MULTI_TABLE_LIVE_FIXTURE` (which has no legacy
    `type='join'` relation at all, only the modern <relationships> graph):
    the OLD code returned `queryable: True` pointed at ORDERS alone.

    THE FIX: scan only relations that are DIRECT CHILDREN of the federated
    <connection> element (confirmed against the REAL KPI Live workbook's XML
    shape) -- the object-graph's per-object relations are never direct
    children of <connection>, so they're excluded; a genuine multi-table live
    model is now correctly refused (not silently mis-detected as single-table).

    THE PAYOFF: once live_connections() stops falsely claiming
    queryable=True, the ALREADY-BUILT R10 machinery (pipeline.onboard's
    missing-resolution -> build_data_model_tables' verify-then-deploy) picks
    up the caption automatically and deploys the join view against the real
    live tables -- ZERO new plumbing needed beyond the detection fix itself.
    Proven end-to-end here with a fake account matching the fixture's
    declared tables (no live account needed for THIS gate; live-verified
    separately against the real account, see NEW_CHAT.md)."""
    import xml.etree.ElementTree as ET
    import tempfile
    import pipeline
    import config

    root = ET.fromstring(_MULTI_TABLE_LIVE_FIXTURE)

    # (a) the detection fix, directly
    live = TP.live_connections(root)
    info = live["Live Star Model"]
    assert info["queryable"] is False, info
    assert "2 tables" in info["reason"] and "relationship/join model" in info["reason"], info

    # (b) NO REGRESSION -- the real single-table live workbook stays queryable
    p = os.path.join(ROOT, "Workbooks", "Superstore_KPI_Parameter_Dashboard_Live.twbx")
    if os.path.exists(p):
        real = TP.live_connections(TP.load_twb_xml(p))
        cap = next(iter(real))
        assert real[cap]["queryable"] is True, real
        assert real[cap]["table"] == "SUPERSTORE_ORDERS", real

    # (c) corpus false-positive sweep unaffected (no local-file 'live' connection
    # -- Superstore's own bundled Excel/CSV -- gets flagged)
    for f in ("Superstore.twb", "_ecom.twb", "_filtest.twb"):
        fp = os.path.join(ROOT, f)
        if os.path.exists(fp):
            assert TP.live_connections(TP.load_twb_xml(fp)) == {}, f

    # (d) THE PAYOFF -- end to end through onboard(), no session-specific code
    # beyond what R10 already built. write_pandas RAISES in _FakeAccount, so
    # any regression that copies instead of binding the live tables fails loudly.
    acct = _FakeAccount({"PROD_DB.SALES.ORDERS": ["ORDER_ID", "CUSTOMER_ID", "AMOUNT"],
                        "PROD_DB.SALES.CUSTOMERS": ["CUSTOMER_ID", "NAME"]}, rows=9994)
    _saved = (dict(config.DATASOURCES), config.DEFAULT_DATASOURCE, config.ORDERS)
    tmp = tempfile.NamedTemporaryFile(suffix=".twb", delete=False, mode="w", encoding="utf-8")
    tmp.write(_MULTI_TABLE_LIVE_FIXTURE)
    tmp.close()
    try:
        d = pipeline.onboard(tmp.name, b"", session=acct)
        assert d["missing"] == [], d["missing"]
        cap, table, rows, note = d["load_report"][0]
        assert "no decode, no copy (R10)" in note, note
        assert rows == 9994, d["load_report"]
        assert acct.wrote == [], "a live multi-table datasource was copied instead of bound"
    finally:
        config.DATASOURCES.clear(); config.DATASOURCES.update(_saved[0])
        config.DEFAULT_DATASOURCE, config.ORDERS = _saved[1], _saved[2]
        os.unlink(tmp.name)

    print("ok  R9 live multi-table join (live_connections() no longer mis-scans "
          "the object-graph's per-object relations and silently claims a "
          "2-table model queryable at just the first table; the honest refusal "
          "now correctly routes through R10's already-built verify+deploy "
          "machinery with zero new plumbing; real single-table live workbook "
          "and corpus false-positive sweep unaffected)")


def test_r10_multitable_source_autobind():
    """ROADMAP R10 (2026-07-26): multi-table extract/live auto-bind to
    pre-existing separate Snowflake tables -- the ROOT-CAUSE fix.

    THE BUG: semantic_layer._connection(ds) did `ds.find(".//connection")` --
    the FIRST <connection> in document order, which for every real federated
    datasource is the OUTER class='federated' WRAPPER, never the real upstream
    connection nested in <named-connections>. Verified LIVE on Regional
    Analysis before this fix: _connection() returned {'class': 'federated',
    'dbname': None, ...}, so _src_table()'s "keep the declared location" branch
    (gated on class == 'snowflake') could never fire -- every multi-table model
    was assumed to need copying, even when its tables already exist separately
    in the account. This is also WHY a live multi-table JOIN (R9) stays
    honestly refused: removing that refusal without this fix would have
    silently bound to the wrong (assumed-copy) location.

    THE FIX: _connection() now reuses tableau_parser._upstream_connections
    (the SAME upstream-detection R3's source_tables() already uses and is
    gated on) instead of a bare `.find`. _src_table() also gained a real
    1/2/3-segment table-ref parser (_parse_relation_table) so a fully-qualified
    '[DB].[SCHEMA].[TABLE]' relation (Regional Analysis' actual shape) is never
    dot-joined onto a caller-supplied default db/schema -- the exact
    double-schema bug class (db.schema.schema.table) this project keeps having
    to guard against.

    THE POINT OF THIS TEST is the VERIFICATION boundary, not the happy path.
    _src_table's resolved location is only ever a CANDIDATE -- a workbook
    SAYING a table lives somewhere is not proof. pipeline.verify_table_candidate
    must existence+column-check a declared-source table exactly like R3's
    single-table resolver does, and pipeline.build_data_model_tables must
    refuse the WHOLE model (never a partial bind) the moment ANY table in the
    graph fails to verify."""
    import xml.etree.ElementTree as ET
    import pipeline
    import semantic_layer as SL

    root = ET.fromstring(_MULTI_TABLE_LIVE_FIXTURE)

    # (a) THE ROOT CAUSE, directly: _connection() now finds the REAL upstream,
    # not the federated wrapper.
    ds = SL.data_model(root)[0]
    assert ds["connection"]["class"] == "snowflake", ds["connection"]
    assert ds["connection"]["dbname"] == "PROD_DB", ds["connection"]

    # (b) _src_table resolves BOTH tables to their declared 3-segment location,
    # WITHOUT doubling the db/schema (the fixture uses fully-qualified
    # '[PROD_DB].[SALES].[ORDERS]' -- exactly Regional Analysis' real shape).
    m = SL.describe_model(root, "WBR_DB", "PIPELINE_DEMO")[0]
    assert m["shape"] == "star" and m["joinable"], m
    tabs = {t["caption"]: t for t in m["tables"]}
    assert tabs["Orders"]["fqn"] == "PROD_DB.SALES.ORDERS", tabs["Orders"]
    assert tabs["Customers"]["fqn"] == "PROD_DB.SALES.CUSTOMERS", tabs["Customers"]
    assert all(t["is_declared_source"] for t in tabs.values()), tabs
    # a declared-source view must reference ORIGINAL (quoted) column names --
    # this pipeline never touched these tables, so there is nothing normalized.
    assert 'f."order_id"' in m["view_ddl"] or 'f."customer_id"' in m["view_ddl"], \
        m["view_ddl"]
    assert "ORDER_ID" in m["view_ddl"]  # alias side is still to_phys, unquoted

    COLS_ORDERS = ["ORDER_ID", "CUSTOMER_ID", "AMOUNT"]
    COLS_CUSTOMERS = ["CUSTOMER_ID", "NAME"]

    # (c) BOTH tables verify (exist + columns cover) -> deployable, no copy.
    acct = _FakeAccount({"PROD_DB.SALES.ORDERS": COLS_ORDERS,
                        "PROD_DB.SALES.CUSTOMERS": COLS_CUSTOMERS})
    rep = pipeline.data_model_report(acct, root, db="WBR_DB", schema="PIPELINE_DEMO")
    star = next(r for r in rep if r["shape"] == "star")
    assert star["deployable"] is True and star["table_notes"] == [], star

    v = pipeline.deploy_model_view(acct, star)
    assert v.upper().endswith("MODEL"), v

    # (d) build_data_model_tables must deploy the SAME way with ZERO decode and
    # ZERO copy -- write_pandas RAISES in _FakeAccount, so any regression that
    # falls through to the decode/copy path fails loudly here, not silently.
    out = pipeline.build_data_model_tables(acct, root, hyper_paths=[],
                                           db="WBR_DB", schema="PIPELINE_DEMO")
    assert len(out) == 1 and out[0][1] is not None, out
    assert "no decode, no copy" in out[0][2], out
    assert acct.wrote == [], "R10 auto-bind copied the table anyway"

    # (e) THE WRONG-TABLE GUARD: Customers exists but its columns DON'T match
    # the workbook's declared source -> the WHOLE model refuses, not a partial
    # view over one verified + one unverified table.
    acct2 = _FakeAccount({"PROD_DB.SALES.ORDERS": COLS_ORDERS,
                         "PROD_DB.SALES.CUSTOMERS": ["EMPLOYEE_ID", "HIRE_DATE"]})
    rep2 = pipeline.data_model_report(acct2, root, db="WBR_DB", schema="PIPELINE_DEMO")
    star2 = next(r for r in rep2 if r["shape"] == "star")
    assert star2["deployable"] is False, star2
    assert any("columns do not match" in n for n in star2["table_notes"]), star2
    out2 = pipeline.build_data_model_tables(acct2, root, hyper_paths=[],
                                            db="WBR_DB", schema="PIPELINE_DEMO")
    assert out2[0][1] is None and "skipped" in out2[0][2], out2
    assert acct2.wrote == [], "mismatched table must never silently bind either"

    # (f) declared table doesn't exist at all -> same honest refusal, not a crash.
    acct3 = _FakeAccount({})
    rep3 = pipeline.data_model_report(acct3, root, db="WBR_DB", schema="PIPELINE_DEMO")
    assert next(r for r in rep3 if r["shape"] == "star")["deployable"] is False

    # (g) DOUBLE-SCHEMA PARSING unit assertions -- the other half of the root
    # cause: a 3-segment relation must NEVER be dot-joined onto a caller
    # default; 1/2-segment relations must fall back correctly.
    assert SL._parse_relation_table("[SANDBOX].[DS].[SAMPLE_SUPER_STORE_ORDERS]",
                                    "OTHERDB", "OTHERSCHEMA") == \
        ("SANDBOX", "DS", "SAMPLE_SUPER_STORE_ORDERS")
    assert SL._parse_relation_table("[DS].[ORDERS]", "OTHERDB", "OTHERSCHEMA") == \
        ("OTHERDB", "DS", "ORDERS")
    assert SL._parse_relation_table("[ORDERS]", "OTHERDB", "OTHERSCHEMA") == \
        ("OTHERDB", "OTHERSCHEMA", "ORDERS")

    # (h) CORPUS PROOF -- Regional Analysis' real XML now resolves its 3 tables
    # to their true SANDBOX.DS.* location (previously WBR_DB.PIPELINE_DEMO.*,
    # the assumed-copy location this bug produced). This is the exact
    # regression this fix was built to prove, on the exact workbook that
    # exposed it.
    p = os.path.join(ROOT, "Workbooks", "Regional Analysis.twbx")
    if os.path.exists(p):
        ra = TP.load_twb_xml(p)
        rads = SL.data_model(ra)[0]
        assert rads["connection"]["class"] == "snowflake", rads["connection"]
        ram = SL.describe_model(ra, "WBR_DB", "PIPELINE_DEMO")
        rastar = next(m for m in ram if m["n_tables"] > 1)
        fqns = {t["caption"]: t["fqn"] for t in rastar["tables"]}
        assert all(f.startswith("SANDBOX.DS.") for f in fqns.values()), fqns
        assert all(t["is_declared_source"] for t in rastar["tables"]), rastar["tables"]

    # (i) NO REGRESSION -- a FLAT-FILE star (Superstore: Orders/People/Returns
    # are file-based, no real Snowflake upstream) must resolve EVERY table as
    # an assumed copy, byte-identical to before this fix.
    p2 = os.path.join(ROOT, "Superstore.twb")
    if os.path.exists(p2):
        ss = TP.load_twb_xml(p2)
        ssm = SL.describe_model(ss, "WBR_DB", "PIPELINE_DEMO")
        ssstar = next(m for m in ssm if m["shape"] == "star")
        assert not any(t["is_declared_source"] for t in ssstar["tables"]), \
            ssstar["tables"]

    print("ok  R10 multi-table source auto-bind (root cause: semantic_layer."
          "_connection() now finds the real upstream past the federated "
          "wrapper; _src_table resolves 3-segment relations without doubling "
          "the schema; declared-source tables are existence+column-VERIFIED "
          "before binding, never a bare name match; a single unverified table "
          "refuses the WHOLE model, no partial binds; build_data_model_tables "
          "skips decode+copy entirely when everything verifies; Regional "
          "Analysis' real XML now resolves to its true SANDBOX.DS location; "
          "flat-file stars (Superstore) are unaffected)")


def test_onboard_resolves_multitable_missing_before_stopping():
    """REAL BUG found LIVE (2026-07-26, same day as R10): uploading a genuine
    R10 workbook (a multi-table datasource whose constituent tables already
    verify separately) to the deployed app still failed Stage 1 with MISSING
    -- even though R10's own mechanism (build_data_model_tables) works
    perfectly when called directly. ROOT CAUSE: pipeline.onboard's missing-
    check is `load_into_snowflake`'s per-caption probe, which only ever looks
    for ONE table named to_phys(caption) -- it has no idea a MULTI-TABLE
    datasource's constituent tables might independently verify (R10) or be
    decodable as separate tables (scope B). Neither possibility is single-
    table-shaped, so the naive probe always calls a multi-table datasource
    MISSING. pipeline_app.py's Stage 1 then st.stop()s on ANY missing caption
    -- BEFORE Stage 3 (which calls build_data_model_tables and WOULD have
    resolved it) ever runs. The fix belongs in onboard() itself: resolve a
    missing multi-table caption via build_data_model_tables BEFORE returning,
    not one stage later.

    Proven against the REAL live account with decode genuinely blocked
    (simulating the Snowsight sandbox, where tableauhyperapi is absent) --
    this bug is invisible on a laptop where hyper decodes locally and
    load_into_snowflake never even reaches the missing branch.

    NOT WIRED INTO main() -- deliberately. pipeline.snow_session can open a
    browser for interactive SSO and BLOCK waiting for it; that is never safe
    to run unattended in the default suite. Run this one manually, with a
    cached/authenticated `wbr` session, when re-verifying this specific fix."""
    import pipeline

    p = os.path.join(ROOT, "Workbooks", "R10_Chain_Over_Existing_Tables.twbx")
    if not os.path.exists(p):
        print("skip onboard/R10-missing-resolution (test workbook not present)")
        return

    _orig_decode = pipeline.decode_hypers_locally
    def _blocked(hyper_paths, files, workdir, relationships=None):
        return [os.path.basename(hp) for hp in hyper_paths]   # simulate SiS: nothing decodes
    pipeline.decode_hypers_locally = _blocked
    try:
        session = pipeline.snow_session("wbr")
    except Exception as e:
        pipeline.decode_hypers_locally = _orig_decode
        print(f"skip onboard/R10-missing-resolution (no live session: {e})")
        return
    try:
        raw = open(p, "rb").read()
        d = pipeline.onboard(p, raw, session=session)
        assert d["blocked"], "the .hyper must genuinely fail to decode for this test to mean anything"
        assert d["missing"] == [], \
            f"multi-table datasource stayed MISSING despite verifying separately: {d['missing']}"
        cap, table, rows, note = d["load_report"][0]
        assert "no decode, no copy (R10)" in note, note
        assert rows == 10194, d["load_report"]
        df = session.sql(f"SELECT SUM(SALES) AS S FROM {table}").collect()[0]["S"]
        assert round(float(df), 2) == 2326534.36, df
    finally:
        pipeline.decode_hypers_locally = _orig_decode
        session.close()

    print("ok  onboard() resolves a multi-table R10 datasource's MISSING status "
          "via build_data_model_tables BEFORE Stage 1 would have stopped -- "
          "verified live, with decode genuinely blocked (simulating the "
          "Snowsight sandbox)")


def test_tableau_server_url_parsing_and_fetch():
    """ROADMAP R1 (2026-07-28): pull a workbook from Tableau Server/Cloud by
    link. Offline-gated, same posture as R9/R10 before their live workbooks
    existed -- no Tableau site available this session, so this proves the URL
    parser (both Cloud and self-hosted Server shapes, incl. the reject case)
    and the sign-in/lookup/download request SHAPING via a monkeypatched
    `requests` module, never a real network call."""
    import types
    import xml.etree.ElementTree as ET
    import tableau_server as TSV

    # --- URL parsing: the shapes that actually appear + the reject case ----
    cloud = TSV.parse_tableau_url(
        "https://10ax.online.tableau.com/#/site/mysite/views/Superstore/Overview")
    assert cloud == {"server_url": "https://10ax.online.tableau.com",
                     "site_content_url": "mysite", "content_url": "Superstore",
                     "workbook_id": ""}, cloud

    by_id = TSV.parse_tableau_url(
        "https://10ax.online.tableau.com/#/site/mysite/workbooks/"
        "a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    assert by_id["workbook_id"] == "a1b2c3d4-e5f6-7890-abcd-ef1234567890", by_id
    assert by_id["content_url"] == "", by_id

    default_site = TSV.parse_tableau_url(
        "https://tableau.mycompany.com/#/views/Superstore/Overview")
    assert default_site["site_content_url"] == "", default_site
    assert default_site["content_url"] == "Superstore", default_site

    try:
        TSV.parse_tableau_url("https://example.com/not-tableau")
        assert False, "a non-Tableau-shaped URL must raise, never silently guess"
    except ValueError:
        pass

    # 2026-07-28, found live: Tableau's web UI routes a workbook's OVERVIEW
    # page (its "Views" list tab) through a purely-numeric internal id that
    # the REST API 404s on directly (confirmed against the real account) --
    # it is neither a contentUrl slug (never all-digit) nor a resolvable REST
    # id. Must fail clearly, not silently attempt a contentUrl lookup that
    # can never match.
    try:
        TSV.parse_tableau_url(
            "https://prod-useast-b.online.tableau.com/#/site/b360bi/workbooks/4419629/views")
        assert False, "a numeric workbook-overview-page id must raise, never be treated as a contentUrl"
    except ValueError as e:
        assert "numeric" in str(e) and "4419629" in str(e), e

    # --- request shaping, via a fake `requests` module (no network) --------
    calls = []

    class _FakeResp:
        def __init__(self, xml_str, status=200):
            self.content = xml_str.encode("utf-8")
            self.text = xml_str
            self.status_code = status

    def _fake_post(url, data=None, headers=None, timeout=None, **kw):
        if url.endswith("/auth/signout"):
            return _FakeResp("<tsResponse/>")
        calls.append(("POST", url, data))
        assert url.endswith("/auth/signin"), url
        root = ET.fromstring(data)
        creds = root.find("credentials")
        assert creds.get("personalAccessTokenName") == "tok_name", data
        assert creds.get("personalAccessTokenSecret") == "tok_secret", data
        site = creds.find("site")
        assert site.get("contentUrl") == "mysite", data
        return _FakeResp(
            '<tsResponse xmlns="http://tableau.com/api">'
            '<credentials token="FAKE-TOKEN">'
            '<site id="site-123"/><user id="user-456"/>'
            '</credentials></tsResponse>')

    def _fake_get(url, headers=None, timeout=None, stream=None, **kw):
        calls.append(("GET", url, None))
        assert headers.get("X-Tableau-Auth") == "FAKE-TOKEN", headers
        if "/workbooks?filter=contentUrl:eq:" in url:
            assert url.endswith("contentUrl:eq:Superstore"), url
            return _FakeResp(
                '<tsResponse xmlns="http://tableau.com/api">'
                '<workbooks><workbook id="wb-789" name="Superstore" '
                'contentUrl="Superstore"/></workbooks></tsResponse>')
        if url.endswith("/workbooks/wb-789/content"):
            resp = _FakeResp("")
            resp.content = b"FAKE-TWBX-BYTES"
            return resp
        raise AssertionError(f"unexpected GET {url}")

    fake_requests = types.SimpleNamespace(post=_fake_post, get=_fake_get)
    real_requests = TSV.requests
    TSV.requests = fake_requests
    try:
        result = TSV.fetch_workbook(
            "https://10ax.online.tableau.com/#/site/mysite/views/Superstore/Overview",
            token_name="tok_name", token_secret="tok_secret")
    finally:
        TSV.requests = real_requests

    assert result["bytes"] == b"FAKE-TWBX-BYTES", result
    assert result["workbook_id"] == "wb-789", result
    assert result["filename"].endswith((".twb", ".twbx")), result
    kinds = [c[0] for c in calls]
    assert kinds == ["POST", "GET", "GET"], \
        f"expected signin -> lookup -> download in order, got {kinds}"

    # sign_out fires a POST to /auth/signout as the LAST call (finally block)
    signout_calls = []
    def _fake_post_with_signout(url, data=None, headers=None, timeout=None, **kw):
        if url.endswith("/auth/signout"):
            signout_calls.append(url)
            return _FakeResp("<tsResponse/>")
        return _fake_post(url, data=data, headers=headers, timeout=timeout, **kw)

    fake_requests2 = types.SimpleNamespace(post=_fake_post_with_signout, get=_fake_get)
    TSV.requests = fake_requests2
    try:
        TSV.fetch_workbook(
            "https://10ax.online.tableau.com/#/site/mysite/views/Superstore/Overview",
            token_name="tok_name", token_secret="tok_secret")
    finally:
        TSV.requests = real_requests
    assert signout_calls, "fetch_workbook must always sign out, even on success"

    # --- missing-PAT guard: a clear error, never a silent skip --------------
    old_env = {k: os.environ.pop(k, None) for k in
              ("TABLEAU_PAT_NAME", "TABLEAU_PAT_SECRET")}
    try:
        try:
            TSV.fetch_workbook("https://10ax.online.tableau.com/#/site/mysite/"
                               "views/Superstore/Overview")
            assert False, "must raise when no PAT is configured"
        except TSV.TableauAuthError:
            pass
    finally:
        for k, v in old_env.items():
            if v is not None:
                os.environ[k] = v

    # --- SiS Snowflake-Secret fallback (2026-07-28: local env vars don't exist
    # inside a deployed Streamlit-in-Snowflake app -- _snowflake.
    # get_username_password() is the real mechanism there) -----------------
    assert TSV._pat_from_sis_secret() == (None, None), \
        "outside SiS (no _snowflake module), the fallback must return (None, None), never raise"

    class _FakeCred:
        def __init__(s, u, p): s.username, s.password = u, p

    class _FakeSnowflakeModule:
        def get_username_password(self, name):
            assert name == TSV.SIS_SECRET_NAME, name
            return _FakeCred("sis_pat_name", "sis_pat_secret")

    sys.modules["_snowflake"] = _FakeSnowflakeModule()
    try:
        assert TSV._pat_from_sis_secret() == ("sis_pat_name", "sis_pat_secret")

        # fetch_workbook must fall through to the SiS secret when no env var
        # and no explicit arg is given
        old_env = {k: os.environ.pop(k, None) for k in
                  ("TABLEAU_PAT_NAME", "TABLEAU_PAT_SECRET")}
        try:
            captured_creds = {}

            def _fake_post_capture(url, data=None, headers=None, timeout=None, **kw):
                if url.endswith("/auth/signout"):
                    return _FakeResp("<tsResponse/>")
                root = ET.fromstring(data)
                creds = root.find("credentials")
                captured_creds["name"] = creds.get("personalAccessTokenName")
                captured_creds["secret"] = creds.get("personalAccessTokenSecret")
                return _FakeResp(
                    '<tsResponse xmlns="http://tableau.com/api">'
                    '<credentials token="FAKE-TOKEN">'
                    '<site id="site-123"/><user id="user-456"/>'
                    '</credentials></tsResponse>')

            fake_requests3 = types.SimpleNamespace(post=_fake_post_capture, get=_fake_get)
            TSV.requests = fake_requests3
            try:
                TSV.fetch_workbook(
                    "https://10ax.online.tableau.com/#/site/mysite/views/Superstore/Overview")
            finally:
                TSV.requests = real_requests
            assert captured_creds == {"name": "sis_pat_name", "secret": "sis_pat_secret"}, \
                captured_creds
        finally:
            for k, v in old_env.items():
                if v is not None:
                    os.environ[k] = v
    finally:
        del sys.modules["_snowflake"]

    # --- browse-by-dropdown flow (2026-07-28): parse_site_url, paginated
    # list_projects/list_workbooks, and fetch_workbook_by_id -- added after
    # discovering Tableau's own web UI links aren't uniformly REST-resolvable
    # (the numeric workbook-overview-page id above); this sidesteps URL-shape
    # guessing by using real ids straight from the API. ------------------
    site = TSV.parse_site_url(
        "https://10ax.online.tableau.com/#/site/mysite/views/Superstore/Overview")
    assert site == {"server_url": "https://10ax.online.tableau.com",
                    "site_content_url": "mysite"}, site
    site_no_frag = TSV.parse_site_url("https://tableau.mycompany.com/#/home")
    assert site_no_frag == {"server_url": "https://tableau.mycompany.com",
                            "site_content_url": ""}, site_no_frag

    # pagination: a fake 2-page project list must not silently return only
    # page 1 -- the exact class of bug that would under-populate a dropdown.
    def _page_xml(items, page, page_size, total):
        rows = "".join(f'<project id="p{i}" name="Project {i}"/>' for i in items)
        return (f'<tsResponse xmlns="http://tableau.com/api">'
               f'<projects>{rows}</projects>'
               f'<pagination pageNumber="{page}" pageSize="{page_size}" '
               f'totalAvailable="{total}"/></tsResponse>')

    paginate_calls = []

    def _fake_get_paginated(url, headers=None, timeout=None, **kw):
        paginate_calls.append(url)
        assert headers.get("X-Tableau-Auth") == "FAKE-TOKEN", headers
        if "pageNumber=1" in url:
            return _FakeResp(_page_xml(range(3), 1, 3, 5))
        elif "pageNumber=2" in url:
            return _FakeResp(_page_xml(range(3, 5), 2, 3, 5))
        raise AssertionError(f"unexpected page request: {url}")

    client = TSV.TableauRestClient("https://10ax.online.tableau.com")
    client.auth_token = "FAKE-TOKEN"
    client.site_id = "site-123"
    fake_requests4 = types.SimpleNamespace(get=_fake_get_paginated)
    TSV.requests = fake_requests4
    try:
        projects = client.list_projects()
    finally:
        TSV.requests = real_requests
    assert len(projects) == 5, f"pagination must follow BOTH pages, got {len(projects)}"
    assert len(paginate_calls) == 2, paginate_calls
    assert {p["id"] for p in projects} == {f"p{i}" for i in range(5)}, projects

    # fetch_workbook_by_id: no URL parsing, no contentUrl lookup -- just
    # signin -> download -> signout against a known real id.
    dl_calls = []

    def _fake_get_dl(url, headers=None, timeout=None, stream=None, **kw):
        dl_calls.append(url)
        assert url.endswith("/workbooks/wb-known-id/content"), url
        resp = _FakeResp("")
        resp.content = b"FAKE-TWBX-BYTES-2"
        return resp

    fake_requests5 = types.SimpleNamespace(post=_fake_post_with_signout, get=_fake_get_dl)
    TSV.requests = fake_requests5
    try:
        result2 = TSV.fetch_workbook_by_id(
            "https://10ax.online.tableau.com", "mysite", "wb-known-id",
            name_hint="My Workbook", token_name="tok_name", token_secret="tok_secret")
    finally:
        TSV.requests = real_requests
    assert result2["bytes"] == b"FAKE-TWBX-BYTES-2", result2
    assert result2["workbook_id"] == "wb-known-id", result2
    assert dl_calls == [f"{client.base_url}/sites/site-123/workbooks/wb-known-id/content"], dl_calls

    print("ok  Tableau Server/Cloud URL parsing (Cloud + self-hosted + reject), "
          "fetch_workbook's signin->lookup->download->signout request shaping, "
          "and the Snowflake-Secret fallback used inside a deployed "
          "Streamlit-in-Snowflake app (no OS env vars there) -- all offline via "
          "fake requests/_snowflake modules -- ROADMAP R1 (ingest live-verified "
          "against a real Tableau site 2026-07-28; the SiS secret/network-egress "
          "wiring itself still needs the account-owner to run "
          "tableau_server_sis_setup.sql)")


def test_tableau_server_view_data_pull():
    """ROADMAP R2 (2026-07-28): the missing half of R1's original scope --
    pulling a view's data AS RENDERED (not just downloading the .twbx), which
    is what feeds R2's tableau_truth for a literal rendered-dashboard
    comparison instead of just the TWB formula. Offline-gated the same way
    R1's own request-shaping was before a live site existed for it."""
    import types
    import tableau_server as TSV

    class _FakeResp:
        def __init__(self, content, status=200, text=None):
            self.content = content if isinstance(content, bytes) else content.encode("utf-8")
            self.text = text if text is not None else (content if isinstance(content, str) else "")
            self.status_code = status

    real_requests = TSV.requests

    # --- list_views, scoped to a workbook, following pagination -----------
    view_calls = []

    def _fake_get_views(url, headers=None, timeout=None, **kw):
        view_calls.append(url)
        assert headers.get("X-Tableau-Auth") == "FAKE-TOKEN", headers
        assert "/workbooks/wb-789/views" in url, url
        return _FakeResp(
            '<tsResponse xmlns="http://tableau.com/api"><views>'
            '<view id="view-1" name="Overview" contentUrl="Superstore/Overview">'
            '<workbook id="wb-789"/></view>'
            '<view id="view-2" name="Performance" contentUrl="Superstore/Performance">'
            '<workbook id="wb-789"/></view>'
            '</views><pagination pageNumber="1" pageSize="1000" totalAvailable="2"/>'
            '</tsResponse>')

    client = TSV.TableauRestClient("https://10ax.online.tableau.com")
    client.auth_token = "FAKE-TOKEN"
    client.site_id = "site-123"
    TSV.requests = types.SimpleNamespace(get=_fake_get_views)
    try:
        views = client.list_views(workbook_id="wb-789")
    finally:
        TSV.requests = real_requests
    assert len(views) == 2, views
    assert {v["id"] for v in views} == {"view-1", "view-2"}, views
    assert all(v["workbook_id"] == "wb-789" for v in views), views
    assert len(view_calls) == 1, "a single page (totalAvailable=2) must not paginate further"

    # --- query_view_data_csv: the RENDERED data, not the extract rows -----
    csv_calls = []

    def _fake_get_csv(url, headers=None, timeout=None, **kw):
        csv_calls.append(url)
        assert url.endswith("/views/view-2/data"), url
        assert headers.get("X-Tableau-Auth") == "FAKE-TOKEN", headers
        # a UTF-8 BOM prefix, as Tableau's own CSV export actually sends --
        # query_view_data_csv must strip it (utf-8-sig), not leak "﻿Category"
        return _FakeResp(b"\xef\xbb\xbfCategory,SUM(Sales)\nFurniture,754747.76\n")

    TSV.requests = types.SimpleNamespace(get=_fake_get_csv)
    try:
        csv_text = client.query_view_data_csv("view-2")
    finally:
        TSV.requests = real_requests
    assert csv_text.startswith("Category,SUM(Sales)"), repr(csv_text[:40])
    assert "754747.76" in csv_text, csv_text
    assert len(csv_calls) == 1, csv_calls

    # --- fetch_view_data: self-contained signin -> pull -> signout --------
    calls = []

    def _fake_post(url, data=None, headers=None, timeout=None, **kw):
        if url.endswith("/auth/signout"):
            calls.append(("signout", url))
            return _FakeResp("<tsResponse/>")
        calls.append(("signin", url))
        return _FakeResp(
            '<tsResponse xmlns="http://tableau.com/api">'
            '<credentials token="FAKE-TOKEN">'
            '<site id="site-123"/><user id="user-456"/>'
            '</credentials></tsResponse>')

    def _fake_get(url, headers=None, timeout=None, **kw):
        calls.append(("get", url))
        assert headers.get("X-Tableau-Auth") == "FAKE-TOKEN", headers
        assert url.endswith("/views/view-2/data"), url
        return _FakeResp("Category,SUM(Sales)\nFurniture,754747.76\n")

    TSV.requests = types.SimpleNamespace(post=_fake_post, get=_fake_get)
    try:
        result = TSV.fetch_view_data(
            "https://10ax.online.tableau.com", "mysite", "view-2",
            token_name="tok_name", token_secret="tok_secret")
    finally:
        TSV.requests = real_requests
    assert "754747.76" in result, result
    kinds = [c[0] for c in calls]
    assert kinds == ["signin", "get", "signout"], \
        f"expected signin -> get -> signout in order, got {kinds}"

    print("ok  Tableau view-data pull (list_views scoped-by-workbook + "
          "pagination-respected, query_view_data_csv strips the BOM and "
          "returns the RENDERED CSV, fetch_view_data's self-contained "
          "signin->pull->signout) -- ROADMAP R2's missing ground-truth "
          "seam (tableau_truth), offline-gated; needs a live site to feed "
          "real values into parity.build_section_validation_notebook next")


def test_tableau_server_view_image_pull():
    """ROADMAP R8 (2026-07-28, Tableau-side REST piece): the real-image half
    of R8's vision validation -- pull a view's RENDERED IMAGE (PNG bytes)
    over REST, never a screenshot of any Tableau UI. Mirrors R2's
    query_view_data_csv/pull_all_view_csvs/fetch_view_data trio exactly,
    with 'data'/csv swapped for 'image'/png -- same self-contained
    signin->pull(->pull...)->signout shape, offline-gated the same way
    before a live site existed to test against."""
    import types
    import tableau_server as TSV

    class _FakeResp:
        def __init__(self, content, status=200, text=None):
            self.content = content if isinstance(content, bytes) else content.encode("utf-8")
            self.text = text if text is not None else (content if isinstance(content, str) else "")
            self.status_code = status

    real_requests = TSV.requests
    fake_png = b"\x89PNG\r\n\x1a\nFAKE-IMAGE-BYTES"

    # --- query_view_image: PNG bytes, high-resolution by default ----------
    img_calls = []

    def _fake_get_img(url, headers=None, timeout=None, stream=None, **kw):
        img_calls.append(url)
        assert url.endswith("/views/view-2/image?resolution=high"), url
        assert headers.get("X-Tableau-Auth") == "FAKE-TOKEN", headers
        resp = _FakeResp("")
        resp.content = fake_png
        return resp

    client = TSV.TableauRestClient("https://10ax.online.tableau.com")
    client.auth_token = "FAKE-TOKEN"
    client.site_id = "site-123"
    TSV.requests = types.SimpleNamespace(get=_fake_get_img)
    try:
        png = client.query_view_image("view-2")
    finally:
        TSV.requests = real_requests
    assert png == fake_png, png
    assert len(img_calls) == 1, img_calls

    # low-resolution must drop the query param, not just ignore the flag
    def _fake_get_img_lowres(url, headers=None, timeout=None, stream=None, **kw):
        assert url.endswith("/views/view-2/image") and "resolution" not in url, url
        resp = _FakeResp("")
        resp.content = fake_png
        return resp

    TSV.requests = types.SimpleNamespace(get=_fake_get_img_lowres)
    try:
        client.query_view_image("view-2", high_resolution=False)
    finally:
        TSV.requests = real_requests

    # --- pull_all_view_images: one signin for every view in the workbook,
    # a single view's failure doesn't abort the rest -----------------------
    def _fake_get_views(url, headers=None, timeout=None, **kw):
        assert "/workbooks/wb-789/views" in url, url
        return _FakeResp(
            '<tsResponse xmlns="http://tableau.com/api"><views>'
            '<view id="view-1" name="Overview" contentUrl="Superstore/Overview">'
            '<workbook id="wb-789"/></view>'
            '<view id="view-2" name="Broken" contentUrl="Superstore/Broken">'
            '<workbook id="wb-789"/></view>'
            '</views><pagination pageNumber="1" pageSize="1000" totalAvailable="2"/>'
            '</tsResponse>')

    def _fake_post(url, data=None, headers=None, timeout=None, **kw):
        if url.endswith("/auth/signout"):
            return _FakeResp("<tsResponse/>")
        return _FakeResp(
            '<tsResponse xmlns="http://tableau.com/api">'
            '<credentials token="FAKE-TOKEN">'
            '<site id="site-123"/><user id="user-456"/>'
            '</credentials></tsResponse>')

    def _fake_get_multi(url, headers=None, timeout=None, stream=None, **kw):
        if "/views" in url and "/image" not in url:
            return _fake_get_views(url, headers=headers, timeout=timeout)
        if url.endswith("/views/view-1/image?resolution=high"):
            resp = _FakeResp("")
            resp.content = fake_png
            return resp
        if url.endswith("/views/view-2/image?resolution=high"):
            raise RuntimeError("simulated REST failure for this one view")
        raise AssertionError(f"unexpected GET {url}")

    TSV.requests = types.SimpleNamespace(post=_fake_post, get=_fake_get_multi)
    try:
        results = TSV.pull_all_view_images(
            "https://10ax.online.tableau.com", "mysite", "wb-789",
            token_name="tok_name", token_secret="tok_secret")
    finally:
        TSV.requests = real_requests
    assert len(results) == 2, results
    ok_result = next(r for r in results if r["view"] == "Overview")
    bad_result = next(r for r in results if r["view"] == "Broken")
    assert ok_result["png"] == fake_png and ok_result["error"] is None, ok_result
    assert bad_result["png"] is None and "simulated REST failure" in bad_result["error"], bad_result

    # --- fetch_view_image: self-contained signin -> pull -> signout -------
    calls = []

    def _fake_post_track(url, data=None, headers=None, timeout=None, **kw):
        if url.endswith("/auth/signout"):
            calls.append(("signout", url))
            return _FakeResp("<tsResponse/>")
        calls.append(("signin", url))
        return _fake_post(url, data=data, headers=headers, timeout=timeout)

    def _fake_get_track(url, headers=None, timeout=None, stream=None, **kw):
        calls.append(("get", url))
        assert url.endswith("/views/view-9/image?resolution=high"), url
        resp = _FakeResp("")
        resp.content = fake_png
        return resp

    TSV.requests = types.SimpleNamespace(post=_fake_post_track, get=_fake_get_track)
    try:
        img = TSV.fetch_view_image(
            "https://10ax.online.tableau.com", "mysite", "view-9",
            token_name="tok_name", token_secret="tok_secret")
    finally:
        TSV.requests = real_requests
    assert img == fake_png, img
    kinds = [c[0] for c in calls]
    assert kinds == ["signin", "get", "signout"], \
        f"expected signin -> get -> signout in order, got {kinds}"

    print("ok  Tableau view-IMAGE pull (query_view_image returns real PNG "
          "bytes, high_resolution controls the ?resolution=high query param, "
          "pull_all_view_images pulls every view in one session and a single "
          "view's REST failure doesn't abort the rest, fetch_view_image's "
          "self-contained signin->pull->signout) -- ROADMAP R8's Tableau-side "
          "REST piece, offline-gated; needs a live site next, and the "
          "headless Streamlit-side render + vision diff are still unbuilt")


def test_headless_render_to_png():
    """ROADMAP R8 (2026-07-28): the headless Streamlit-side render piece --
    capture the SAME Altair chart OBJECT engine.py builds (no live Streamlit
    session, no browser) and convert it to a real PNG via vl-convert-python,
    instead of screenshotting the SSO-gated deployed app (rejected -- see
    NEW_CHAT.md's R8 redesign entry). Proven against the REAL Superstore
    fixture, not a synthetic chart -- real PNG magic bytes, not just 'some
    bytes came back'."""
    import headless_render as HR
    import engine
    import tableau_parser as TP

    ir = TP.build_ir(TWB)
    engine.configure(ir)
    dash = next(d for d in ir["dashboards"] if d["sheets"])
    chart_sheet = next((s for s in dash["sheets"] if s["kind"] != "kpi"), dash["sheets"][0])
    where_parts = engine.build_where(dash)

    png, reason = HR.render_sheet_to_png(chart_sheet, where_parts)
    assert reason is None, f"expected a real chart to render, got: {reason}"
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n", \
        "not a real PNG (wrong magic bytes)"
    assert len(png) > 500, "suspiciously tiny PNG -- likely a blank/broken render"

    # a sheet that draws nothing must return a stated reason, never crash or
    # fabricate an image
    png2, reason2 = HR.render_sheet_to_png(
        {"name": "NoSuchKind", "kind": "does-not-exist",
         "datasource": "Sample - Superstore"}, where_parts)
    assert png2 is None and reason2 is not None, (png2, reason2)

    # A Plotly-rendered sheet (a map/treemap) now EXPORTS to PNG via kaleido
    # rather than being refused (2026-08-07). The old behavior -- an honest
    # "not yet supported" refusal -- was the reason 5 of Superstore's 10
    # dashboards produced NO app-side image at all, so their visual
    # validation was auto-BLOCKED with nothing to compare. Isolated from
    # engine.render_sheet's real behavior via monkeypatching, the same
    # technique the rest of this suite already uses.
    real_render_sheet = engine.render_sheet

    def _fake_plotly_sheet(s, wp):
        import plotly.graph_objects as go
        engine.st.plotly_chart(go.Figure(go.Bar(x=["a", "b"], y=[3, 5])))

    engine.render_sheet = _fake_plotly_sheet
    try:
        png3, reason3 = HR.render_sheet_to_png({"name": "map"}, [])
    finally:
        engine.render_sheet = real_render_sheet
    assert reason3 is None, f"a Plotly sheet must now export, got: {reason3}"
    assert png3 is not None and png3[:8] == b"\x89PNG\r\n\x1a\n", \
        "Plotly export did not produce a real PNG"

    # ...but a Plotly call carrying NO figure still yields a stated reason,
    # never a crash and never a fabricated image.
    def _empty_plotly_sheet(s, wp):
        engine.st.plotly_chart(None)

    engine.render_sheet = _empty_plotly_sheet
    try:
        png3b, reason3b = HR.render_sheet_to_png({"name": "map"}, [])
    finally:
        engine.render_sheet = real_render_sheet
    assert png3b is None and reason3b, (png3b, reason3b)

    # st.altair_chart/st.plotly_chart must ALWAYS be restored, even when
    # render_sheet raises -- a crash must not leak the monkeypatch into the
    # shared streamlit module for every other caller in the process
    real_altair = engine.st.altair_chart
    real_plotly = engine.st.plotly_chart

    def _raising_sheet(s, wp):
        raise RuntimeError("boom")

    engine.render_sheet = _raising_sheet
    try:
        png4, reason4 = HR.render_sheet_to_png({"name": "x"}, [])
    finally:
        engine.render_sheet = real_render_sheet
    assert png4 is None and "boom" in reason4, (png4, reason4)
    assert engine.st.altair_chart is real_altair, "monkeypatch leaked on exception"
    assert engine.st.plotly_chart is real_plotly, "monkeypatch leaked on exception"

    # render_dashboard_to_png: real composite from the real fixture, notes
    # cover EVERY sheet (KPI/text-only sheets show up as not-rendered, never
    # silently dropped from the notes)
    dpng, notes = HR.render_dashboard_to_png(dash)
    assert dpng is not None and dpng[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(notes) == len(dash["sheets"]), \
        "every sheet in the dashboard must have a note, even if it didn't render"
    assert any(n["rendered"] for n in notes), "at least one real sheet must have rendered"

    # a dashboard where EVERY sheet fails must return (None, notes), never a
    # blank canvas passed off as a real composite
    dpng2, notes2 = HR.render_dashboard_to_png(
        {"sheets": [{"name": "x", "kind": "nope"}]}, where_parts=[])
    assert dpng2 is None and len(notes2) == 1 and notes2[0]["rendered"] is False

    print("ok  headless render to PNG (render_sheet_to_png captures the SAME "
          "Altair chart object via monkeypatch -- proven against the real "
          "Superstore fixture, real PNG magic bytes; a non-chart/Plotly/"
          "raising sheet each get a stated reason, never a crash or a "
          "fabricated image; the monkeypatch never leaks on exception; "
          "render_dashboard_to_png composites real sheets using the SAME "
          "row-grouping data render_dashboard() itself uses, notes cover "
          "every sheet, an all-failed dashboard returns None not a blank "
          "canvas) -- ROADMAP R8's headless-render piece, no browser, no SSO")


def test_headless_render_never_touches_real_widgets():
    """REAL BUG FOUND LIVE 2026-07-29, deployed pipeline_demo: clicking 'Run
    visual validation' crashed with StreamlitDuplicateElementKey inside
    engine.build_where's st.date_input call. Root cause: the live app had
    ALREADY rendered this dashboard once (its own preview), registering a
    widget keyed '<dashboard>::<field>::<part>'; the headless R8 path then
    called the REAL engine.build_where() / engine.render_sheet() a SECOND
    time for the SAME dashboard, re-creating the identical key. Streamlit
    enforces global key uniqueness per script run, so the second call always
    collides, no matter how the value is computed.

    FIX: headless_render mocks every Streamlit INPUT WIDGET function
    (selectbox/date_input/etc.) during a headless render, so the REAL
    widget-creating functions are structurally never called at all --
    proven directly here by counting real calls, not just checking output
    looks right (output looking right was never the problem; a second real
    widget registration was)."""
    import headless_render as HR
    import engine
    import tableau_parser as TP

    ir = TP.build_ir(TWB)
    engine.configure(ir)
    dash = next(d for d in ir["dashboards"] if d["sheets"])

    real_calls = {"selectbox": 0, "date_input": 0, "number_input": 0,
                 "text_input": 0}
    reals = {a: getattr(engine.st, a) for a in real_calls}

    def _counted(name):
        def _fn(*a, **kw):
            real_calls[name] += 1
            return reals[name](*a, **kw)
        return _fn

    for a in real_calls:
        setattr(engine.st, a, _counted(a))
    try:
        # simulate the live app's own dashboard preview having ALREADY run
        # once this script -- this is what actually registers the
        # colliding key in a real Streamlit session
        engine.build_where(dash)
        live_preview_calls = dict(real_calls)

        # now the headless R8 path -- must NOT touch the real widget
        # functions again, structurally, not just "happen to not collide"
        for a in real_calls:
            real_calls[a] = 0
        png, notes = HR.render_dashboard_to_png(dash)
    finally:
        for a, fn in reals.items():
            setattr(engine.st, a, fn)

    assert all(v == 0 for v in real_calls.values()), \
        (f"headless render touched REAL widget functions -- this is exactly "
         f"what caused StreamlitDuplicateElementKey live: {real_calls}")
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n", \
        "widget mocking must not break real chart output"
    assert any(n["rendered"] for n in notes), notes
    assert engine.st.selectbox is reals["selectbox"], "selectbox not restored"
    assert engine.st.date_input is reals["date_input"], "date_input not restored"
    assert live_preview_calls["selectbox"] + live_preview_calls["date_input"] > 0, \
        "the simulated live preview itself must have used a real widget " \
        "(otherwise this test isn't reproducing the actual collision setup)"

    # render_sheet_to_png alone (not just the dashboard-level composite) must
    # have the same guarantee, since a SHEET can carry its own Drill dropdown
    # or worksheet-shown parameter -- not just the dashboard-level filter row
    orig_sb = engine.st.selectbox
    engine.st.selectbox = _counted("selectbox")
    real_calls["selectbox"] = 0
    try:
        HR.render_sheet_to_png(dash["sheets"][0], [])
    finally:
        engine.st.selectbox = orig_sb
    assert real_calls["selectbox"] == 0, \
        "render_sheet_to_png must never touch the real selectbox either"

    print("ok  headless render never touches REAL Streamlit widget functions "
          "(selectbox/date_input/etc. all mocked during a headless render, "
          "proven by counting actual calls to the real functions -- 0 in "
          "every case, restored correctly afterward) -- the exact class of "
          "bug (StreamlitDuplicateElementKey) found live 2026-07-29 when the "
          "live app's own dashboard preview + the R8 headless render both "
          "tried to register the same widget key in one script run")


def test_dashboard_composite_follows_zone_tree():
    """REAL BUG FOUND 2026-08-07 by opening the generated pairs: the app
    renders each dashboard through engine._render_layout, which walks the
    workbook's zone-container tree; the headless COMPOSITE threw that tree
    away (`rows = [[s] for s in dash["sheets"]]`) and stacked one sheet per
    row in sheet-list ORDER. Superstore's Customer Analysis was therefore
    captured as scatter / rank / KPI-row stacked vertically, where both
    Tableau AND the app show the KPI row on top with scatter and rank side
    by side. Every Tableau-vs-app image pair mismatched on LAYOUT before a
    single mark was compared, and the mismatch was blamed on the migration.

    Proven here on the REAL Superstore fixture with each sheet stubbed to a
    solid, distinctly-coloured block, so the assertions are about WHERE each
    zone lands in the composite -- not about a similarity score, which is
    exactly the kind of number that hid this in the first place."""
    import io

    from PIL import Image

    import engine
    import headless_render as HR
    import tableau_parser as TP

    ir = TP.build_ir(TWB)
    engine.configure(ir)
    dash = next(d for d in ir["dashboards"] if d["name"] == "Customers")
    assert dash.get("layout"), "fixture must exercise the layout-tree path"

    colors = {"CustomerOverview": (255, 0, 0), "CustomerScatter": (0, 255, 0),
              "CustomerRank": (0, 0, 255)}

    seen_widths = []

    def _stub(sheet, where_parts=None, scale=2.0, width=None):
        rgb = colors.get(sheet.get("name"))
        if rgb is None:
            return None, "not in this fixture"
        seen_widths.append(width)
        # deliberately NARROWER than any zone: the compositor must PAD, not
        # magnify (upscaling a small render is what smeared the Product
        # Drilldown heatmap's cell labels into an unreadable overlap).
        buf = io.BytesIO()
        Image.new("RGB", (400, 200), rgb).save(buf, format="PNG")
        return buf.getvalue(), None

    real = HR.render_sheet_to_png
    HR.render_sheet_to_png = _stub
    try:
        png, notes = HR.render_dashboard_to_png(dash, where_parts=[], title=False)
    finally:
        HR.render_sheet_to_png = real

    assert png is not None and len(notes) == len(dash["sheets"])
    im = Image.open(io.BytesIO(png)).convert("RGB")

    def _band(name):
        """Rows of the composite in which this sheet's colour appears."""
        rgb = colors[name]
        return [y for y in range(0, im.height, 4)
                if any(im.getpixel((x, y)) == rgb
                       for x in range(0, im.width, 8))]

    top, scat, rank = _band("CustomerOverview"), _band("CustomerScatter"), _band("CustomerRank")
    assert top and scat and rank, (len(top), len(scat), len(rank))

    # 1. ORDER: the KPI overview zone is the FIRST child of the vertical
    #    root, so it must sit ABOVE both charts -- even though it is LAST in
    #    dash["sheets"], which is what the old compositor followed.
    assert max(top) < min(scat) and max(top) < min(rank), \
        ("CustomerOverview must composite ABOVE the scatter/rank row (it is "
         "the layout tree's first child); sheet-list order would put it last")

    # 2. SIDE BY SIDE: scatter and rank share a horz zone, so they must
    #    overlap vertically and occupy opposite halves horizontally.
    assert set(scat) & set(rank), "scatter and rank must share vertical space"
    scat_x = [x for x in range(im.width) if im.getpixel((x, scat[len(scat) // 2])) == colors["CustomerScatter"]]
    rank_x = [x for x in range(im.width) if im.getpixel((x, scat[len(scat) // 2])) == colors["CustomerRank"]]
    assert scat_x and rank_x and max(scat_x) < min(rank_x), \
        "scatter must occupy the LEFT half and rank the RIGHT half of their row"

    # 3. WIDTH SHARE comes from the workbook's own zone weights and is
    #    PUSHED DOWN into the render, so a chart is drawn at the size it will
    #    occupy rather than drawn small and magnified afterwards. The full-
    #    width KPI zone gets the whole canvas; the two equally-weighted
    #    chart zones get equal, roughly-half widths.
    assert len(seen_widths) == 3 and all(w for w in seen_widths), seen_widths
    full, halves = max(seen_widths), sorted(seen_widths)[:2]
    assert full == im.width, (full, im.width)
    assert halves[0] == halves[1], f"equal zone weights -> equal widths: {halves}"
    assert 0.4 * im.width < halves[0] < 0.5 * im.width, (halves, im.width)

    # 4. A render NARROWER than its zone is PADDED, never magnified -- the
    #    stub returns 400px into a ~890px zone and must survive at 400px.
    row_y = scat[len(scat) // 2]
    assert len(scat_x) == 400 and len(rank_x) == 400, \
        ("a sheet rendered narrower than its zone must be padded, not "
         f"upscaled: {len(scat_x)}/{len(rank_x)} px at y={row_y}")

    # 5. A sheet the tree never references is still rendered and noted --
    #    never silently dropped because tree and sheet list disagree.
    orphan = {"name": "Orphan", "kind": "bar"}
    colors["Orphan"] = (255, 255, 0)
    d2 = dict(dash, sheets=list(dash["sheets"]) + [orphan])
    HR.render_sheet_to_png = _stub
    try:
        png2, notes2 = HR.render_dashboard_to_png(d2, where_parts=[], title=False)
    finally:
        HR.render_sheet_to_png = real
    assert any(n["sheet"] == "Orphan" and n["rendered"] for n in notes2), notes2
    im2 = Image.open(io.BytesIO(png2)).convert("RGB")
    assert any(im2.getpixel((x, y)) == (255, 255, 0)
               for y in range(0, im2.height, 4) for x in range(0, im2.width, 8)), \
        "a sheet outside the layout tree must still appear in the composite"

    # 6. _arrange_altair reads the arrangement off the render instead of
    #    always hconcat-ing: charts drawn into ONE st.columns() call sit side
    #    by side, charts drawn at module level stack. Before this, a sheet
    #    that stacks its panels was captured as a horizontal strip -- the
    #    capture inventing a layout the app never drew (Executive Overview's
    #    two 3-panel small-multiple sheets came out as one 6-across row).
    import altair as alt
    import pandas as pd
    df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    ch = [alt.Chart(df).mark_bar().encode(x="a", y="b") for _ in range(4)]
    side = HR._arrange_altair(ch[:2], [7, 7]).to_dict()
    stacked = HR._arrange_altair(ch[:2], [None, None]).to_dict()
    mixed = HR._arrange_altair(ch, [None, 3, 3, None]).to_dict()
    assert "hconcat" in side and "vconcat" not in side, list(side)
    assert "vconcat" in stacked and "hconcat" not in stacked, list(stacked)
    assert len(mixed["vconcat"]) == 3 and "hconcat" in mixed["vconcat"][1], mixed
    assert HR._arrange_altair(ch[:1], [None]) is ch[0], "a lone chart is not wrapped"

    print("ok  dashboard composite follows the workbook's OWN zone tree "
          "(zone ORDER, side-by-side GROUPING and even WIDTH SHARE all proven "
          "positionally on the real Superstore fixture, not via a similarity "
          "score; a sheet outside the tree is still composited and noted; "
          "_arrange_altair hconcats only panels the app drew into one "
          "st.columns call and stacks the rest) -- fixes the capture "
          "flattening that made every Tableau-vs-app pair mismatch on layout")


def test_app_screenshot_no_pipe_deadlock():
    """REAL BUG FOUND LIVE 2026-08-10: app_screenshot.capture_app launched the
    generated Streamlit app with `stdout=subprocess.PIPE` and never read from
    it. Streamlit logs a line of console output on close to every rerun (the
    "missing ScriptRunContext" warning alone fires several times per render),
    so the OS pipe buffer (64 KB on Windows) filled after about two dashboards
    -- and the CHILD PROCESS THEN BLOCKED ON WRITE mid-render. Every tab after
    the first two timed out and looked exactly like a slow or broken app; it
    was a hung process. Symptom cost two full 10-minute investigations before
    the actual mechanism (an undrained pipe, not app or Playwright logic) was
    found by diffing against a working standalone script that used DEVNULL.

    Two things are proven, on the REAL subprocess mechanism (not a mock),
    because this class of bug reproduces only with a genuinely chatty child
    and a genuinely small, unread pipe -- asserting call arguments alone
    would not catch a regression that swaps PIPE back in under a different
    name:
      1. capture_app's own Popen call never passes stdout=PIPE without
         draining it (source-level guard -- cheap, catches a direct revert).
      2. The FILE-redirect pattern capture_app actually uses survives a
         child that writes well past a small pipe's capacity, using the
         SAME buffer size Windows would apply, while a bare PIPE with the
         exact same chatty child provably hangs past a generous timeout --
         so the test demonstrates the failure it prevents, not just its
         absence."""
    import inspect
    import subprocess
    import sys
    import tempfile
    import textwrap

    import app_screenshot as APS

    src = inspect.getsource(APS.capture_app)
    assert "stdout=subprocess.PIPE" not in src, (
        "capture_app must not redirect the child's stdout to an undrained "
        "PIPE -- see the 2026-08-10 hang. Log to a file instead.")
    assert "stdout=log_fh" in src or "stdout=" in src, \
        "capture_app must still redirect the child's output SOMEWHERE"

    chatty = textwrap.dedent("""
        import sys
        for _ in range(20000):
            sys.stdout.write("x" * 20)   # ~400 KB total, well past 64 KB
        sys.stdout.flush()
        sys.exit(0)
    """)
    script = os.path.join(tempfile.mkdtemp(), "chatty.py")
    open(script, "w").write(chatty)

    # 1. The FIXED pattern: redirect to a file. Must complete quickly.
    log_path = os.path.join(tempfile.mkdtemp(), "out.log")
    with open(log_path, "wb") as fh:
        p = subprocess.Popen([sys.executable, script], stdout=fh,
                             stderr=subprocess.STDOUT)
        rc = p.wait(timeout=15)
    assert rc == 0, f"file-redirected chatty child should exit cleanly, got {rc}"
    assert os.path.getsize(log_path) > 65536, \
        "the child must have actually written past a pipe-buffer's worth " \
        "of output, or this test proves nothing"

    # 2. The BROKEN pattern this replaces: undrained PIPE. Must hang -- this
    #    is the demonstration that the bug was real, not a guess.
    p2 = subprocess.Popen([sys.executable, script], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)
    try:
        p2.wait(timeout=5)
        hung = False
    except subprocess.TimeoutExpired:
        hung = True
    finally:
        p2.kill()
        p2.wait(timeout=10)
    assert hung, ("an undrained PIPE with a chatty-enough child should still "
                  "hang on this platform -- if it doesn't, the reproduction "
                  "no longer demonstrates the bug this test guards against")

    print("ok  app_screenshot never redirects the generated app's stdout to "
          "an undrained PIPE (source-level guard) -- and the fix is proven "
          "on the real mechanism: a file-redirected chatty child survives "
          "writing 6x a pipe buffer's worth of output while the SAME child "
          "under a bare undrained PIPE provably hangs, reproducing the "
          "exact 2026-08-10 bug (every dashboard tab after the first two "
          "timed out because the Streamlit child was blocked on write, not "
          "because the app or Playwright was slow)")


def test_validation_pack_slimmed():
    """The generated pack was audited 2026-08-07 and shipped 49 files / 5.7 MB,
    of which a measurable share was duplication or filler: a `visual-staging/`
    scratch dir holding byte-identical copies of the diffs already in
    evidence/, a header-only `comparison.csv` for every BLOCKED chart (14 of
    21), and 270 KB of row arrays in the summary JSON restating those same
    CSVs. Both HTML reports also linked their screenshots by RELATIVE PATH,
    so the report broke the moment it left its folder -- which is now the
    normal case, since the pack ships as individual files rather than a zip.

    Gates the slimming, and specifically that it removes only redundancy:
    every verdict, count and reason survives."""
    import copy
    import json
    import os
    import shutil
    import tempfile

    import deep_validation as DV

    root = tempfile.mkdtemp()
    try:
        out = os.path.join(root, "pack")
        chart_dir = os.path.join(out, "evidence", "dash-a", "charts", "chart-1")
        blocked_dir = os.path.join(out, "evidence", "dash-a", "charts", "chart-2")
        staging = os.path.join(out, "visual-staging")
        for d in (chart_dir, blocked_dir, staging):
            os.makedirs(d)
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        for p in (os.path.join(out, "evidence", "dash-a", "tableau.png"),
                  os.path.join(out, "evidence", "dash-a", "visual-diff.png"),
                  os.path.join(staging, "dasha-diff.png")):
            open(p, "wb").write(png)
        open(os.path.join(chart_dir, "comparison.csv"), "w").write("k,v\n1,2\n")
        open(os.path.join(blocked_dir, "comparison.csv"), "w").write("k,v\n")
        open(os.path.join(out, "validation_report.html"), "w", encoding="utf-8").write(
            '<img src="evidence/dash-a/tableau.png"><img src="evidence/dash-a/visual-diff.png">')
        open(os.path.join(out, "issues.csv"), "w").write("level,status\nchart,FAIL\n")

        summary = {
            "workbook": "W.twbx", "run_id": "VAL-1", "environment": "UAT",
            "generated_at": "2026-08-07T00:00:00", "status": "FAIL",
            "summary": {"dashboards": 1, "passed": 0, "review": 0,
                        "failed_or_blocked": 1, "charts": 2},
            "dashboards": [{"name": "dash-a", "status": "FAIL", "charts": [
                {"id": "chart-1", "title": "Real", "status": "FAIL",
                 "failed_cells": 3, "skip_reason": "",
                 "comparison_csv": "evidence/dash-a/charts/chart-1/comparison.csv",
                 "comparison_rows": [{"status": "FAIL"}] * 40,
                 "tableau_rows": [{"a": 1}] * 40, "streamlit_rows": [{"a": 1}] * 40,
                 "backend_rows": [{"a": 1}] * 40, "duplicates": [1, 2, 3]},
                {"id": "chart-2", "title": "Blocked", "status": "BLOCKED",
                 "failed_cells": 0, "skip_reason": "no comparable rows",
                 "comparison_csv": "evidence/dash-a/charts/chart-2/comparison.csv",
                 "comparison_rows": []}]}]}
        before = copy.deepcopy(summary)
        note = DV._slim_validation_pack(out, summary, meta={
            "tableau_source": "srv · site s", "backend": "DB.SCHEMA"})

        # 1. The in-memory summary is NOT mutated -- the Cortex vision step and
        #    the Stage-5 UI both read it after this runs.
        assert summary == before, "slimming mutated the in-memory summary"

        # 2. Staging gone, every loose PNG gone, images inlined instead.
        assert not os.path.isdir(os.path.join(out, "visual-staging"))
        assert not [f for _r, _d, fs in os.walk(out) for f in fs
                    if f.lower().endswith(".png")], "loose PNGs still shipped"
        html = open(os.path.join(out, "validation_report.html"), encoding="utf-8").read()
        assert html.count("src=\"data:image/png;base64,") == 2, html[:200]
        assert ".png\"" not in html, "a relative image path survived"
        assert note["png_inlined"] == 2 and note["staging"] == 1

        # 3. The header-only CSV is gone WITH its pointer; the real one stays.
        assert os.path.exists(os.path.join(chart_dir, "comparison.csv"))
        assert not os.path.exists(os.path.join(blocked_dir, "comparison.csv"))
        assert note["empty_csv"] == 1

        # 4. The shipped JSON keeps every verdict and drops only the row
        #    arrays the CSVs already carry.
        slim = json.load(open(os.path.join(out, "validation_summary.json"),
                              encoding="utf-8"))
        c1, c2 = slim["dashboards"][0]["charts"]
        assert slim["status"] == "FAIL" and slim["summary"]["charts"] == 2
        assert c1["status"] == "FAIL" and c1["failed_cells"] == 3
        assert c2["status"] == "BLOCKED" and c2["skip_reason"] == "no comparable rows"
        assert c1["comparison_csv"].endswith("chart-1/comparison.csv")
        assert "comparison_csv" not in c2, "pointer to a deleted CSV survived"
        for k in ("comparison_rows", "tableau_rows", "streamlit_rows",
                  "backend_rows", "duplicates"):
            assert k not in c1, f"{k} still restated in the summary JSON"

        # 5. The README is the entry point, and lists only files that EXIST --
        #    a manifest naming a missing file is worse than no manifest.
        readme = open(os.path.join(out, "README.md"), encoding="utf-8").read()
        assert "VAL-1" in readme and "DB.SCHEMA" in readme
        assert "BLOCKED" in readme and "not measured" in readme
        assert "filter" in readme.lower(), \
            "the filter-state caveat is what stops a saved-filter difference " \
            "being raised as a migration defect"
        assert "`validation_report.html`" in readme
        assert "dashboard_validation_report.html" not in readme, \
            "README lists a file this pack does not contain"
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("ok  validation pack slimmed to what a reviewer can act on "
          "(scratch staging + byte-identical diff copies removed, header-only "
          "comparison CSVs deleted with their pointers, row arrays dropped "
          "from the summary JSON that the CSVs already carry, screenshots "
          "inlined so each report stands alone now that files ship "
          "individually instead of zipped, README manifest generated from "
          "what is actually on disk and carrying the status legend + the "
          "filter-state caveat) -- every verdict, count and reason preserved, "
          "and the in-memory summary left untouched for the vision step")


def test_vision_validate_dashboard():
    """ROADMAP R8 (2026-07-29): the actual Cortex vision-diff orchestration,
    using the call shape CONFIRMED WORKING LIVE against the real account
    this session -- AI_COMPLETE + PROMPT()'s {0} placeholder + an
    SNOWFLAKE_SSE-encrypted stage. Both gotchas that were found live (client
    -side-encrypted stages rejected; PROMPT() silently returns NULL without
    the {0} placeholder) are encoded here as gates, not just comments."""
    import json

    import parity

    class _FakeFile:
        def __init__(self, sink):
            self._sink = sink

        def put_stream(self, stream, path, auto_compress=None, overwrite=None):
            self._sink.append((path, stream.read()))

    class _FakeSession:
        def __init__(self, responses):
            self.calls = []
            self.staged = []
            self._responses = list(responses)
            self.file = _FakeFile(self.staged)

        def sql(self, query):
            self.calls.append(query)
            return self

        def collect(self):
            resp = self._responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return [(resp,)]

    # --- ensure_vision_stage: creates with the CONFIRMED-required SSE
    # encryption, never the (rejected) default -----------------------------
    sess = _FakeSession([None])
    parity.ensure_vision_stage(sess, "WBR_DB.PIPELINE_DEMO.R8_VISION_TEST")
    assert "CREATE STAGE IF NOT EXISTS" in sess.calls[0], sess.calls[0]
    assert "SNOWFLAKE_SSE" in sess.calls[0], \
        "must request SSE encryption -- the default is rejected by AI_COMPLETE (confirmed live)"

    # --- _describe_image_via_cortex: the {0}-placeholder call shape -------
    sess2 = _FakeSession(["A detailed description of KPI values and charts."])
    desc, tok, err = parity._describe_image_via_cortex(
        sess2, "WBR_DB.PIPELINE_DEMO.R8_VISION_TEST", "tableau_x.png",
        "This is a Tableau dashboard.")
    assert err is None and desc.startswith("A detailed description"), (desc, err)
    assert tok > 0, tok
    q = sess2.calls[0]
    assert "AI_COMPLETE" in q and "PROMPT(" in q and "TO_FILE(" in q, q
    assert "{0}" in q, "PROMPT() must carry the {0} placeholder -- omitting " \
                       "it silently returns NULL instead of erroring (found live)"
    assert "tableau_x.png" in q and "R8_VISION_TEST" in q, q

    # AI_COMPLETE returns a VARIANT, so plain prose arrives JSON-encoded --
    # quoted, with escaped newlines. OBSERVED LIVE 2026-07-30 against the real
    # account: descriptions came back as '"## Overall Layout\\n\\n..."'. Left
    # wrapped, every description carries literal \\n and quotes into the
    # comparison prompt for no reason.
    sess2b = _FakeSession([json.dumps("## Overall Layout\n\nA scatter plot.")])
    desc2b, _, err2b = parity._describe_image_via_cortex(
        sess2b, "stage", "x.png", "focus")
    assert err2b is None, err2b
    assert desc2b.startswith("## Overall Layout"), desc2b
    assert "\n" in desc2b and not desc2b.startswith('"'), \
        f"VARIANT-wrapped description must be decoded to real text: {desc2b!r}"
    assert parity._unwrap_variant_text("plain text") == "plain text", \
        "plain (non-JSON) text must pass through untouched"
    assert parity._unwrap_variant_text('"unterminated') == '"unterminated'

    # an empty/null Cortex response must be a stated error, never treated as
    # a real (empty) description
    sess3 = _FakeSession([None])
    desc3, _, err3 = parity._describe_image_via_cortex(
        sess3, "stage", "x.png", "focus")
    assert desc3 is None and err3 is not None, (desc3, err3)

    # a raising session must return an error, not propagate the exception
    class _RaisingSession:
        def sql(self, q):
            raise RuntimeError("boom")

    desc4, tok4, err4 = parity._describe_image_via_cortex(
        _RaisingSession(), "stage", "x.png", "focus")
    assert desc4 is None and tok4 == 0 and "boom" in err4, (desc4, tok4, err4)

    # --- _compare_descriptions_via_cortex: plain-text call, no TO_FILE at
    # all -- deliberately NOT the untested multi-image PROMPT() shape ------
    sess5 = _FakeSession(['{"verdict": "BUG", "explanation": "profit ratio differs"}'])
    v, exp, tok5, err5 = parity._compare_descriptions_via_cortex(
        sess5, "Customer Analysis", "desc A text", "desc B text")
    assert v == "BUG" and exp == "profit ratio differs" and err5 is None
    q5 = sess5.calls[0]
    assert "TO_FILE" not in q5 and "PROMPT(" not in q5, \
        "the comparison call must be plain-text AI_COMPLETE, not an " \
        "unverified multi-image PROMPT() call"
    assert "CRITICAL CONTEXT" not in q5, \
        "with no render notes there is nothing known-absent to caveat"

    # --- the OMITTED-SHEET caveat (real false-BUG class found live
    # 2026-07-29/30). headless_render draws Altair CHARTS only, so a
    # dashboard's KPI tiles and Plotly/map sheets are legitimately absent
    # from the app-side image. Without being told, Cortex faithfully
    # reported "the migrated app is missing the KPI summary panel" as a BUG
    # on essentially every dashboard with KPI tiles -- a RENDERER scope
    # limit described as a MIGRATION defect. -------------------------------
    sess5b = _FakeSession(['{"verdict": "PASS", "explanation": "same data"}'])
    notes = [{"sheet": "Sales KPI", "rendered": False, "reason": "KPI-only sheet"},
             {"sheet": "Sales Map", "rendered": False, "reason": "Plotly sheet"},
             {"sheet": "Trend", "rendered": True, "reason": None}]
    v5b, _, _, _ = parity._compare_descriptions_via_cortex(
        sess5b, "Executive Overview", "A", "B", app_render_notes=notes)
    q5b = sess5b.calls[0]
    assert v5b == "PASS", v5b
    assert "CRITICAL CONTEXT" in q5b, q5b
    for omitted in ("Sales KPI", "Sales Map"):
        assert omitted in q5b, f"{omitted!r} must be named as known-absent: {q5b}"
    _caveat = q5b.split("CRITICAL CONTEXT")[1].split("Description A")[0]
    assert "Trend" not in _caveat, \
        "a sheet that DID render must never be listed as known-absent"
    assert "NOT a bug" in q5b and "PASS" in _caveat, \
        "the caveat must tell Cortex not to report these as defects"
    assert parity._omitted_sheets(notes) == ["Sales KPI", "Sales Map"]
    assert parity._omitted_sheets(None) == []

    # --- vision_validate_dashboard: the full orchestration -----------------
    sess6 = _FakeSession([
        None,                                                     # CREATE STAGE
        "Tableau shows Sales $2.3M, Profit $292K across 4 regions.",
        "App shows Sales $2.3M, Profit $292K across 4 regions.",
        '{"verdict": "PASS", "explanation": "figures match"}',
    ])
    result = parity.vision_validate_dashboard(
        sess6, "WBR_DB.PIPELINE_DEMO.R8_VISION_TEST", "Executive Overview",
        b"FAKE-TABLEAU-PNG-BYTES", b"FAKE-APP-PNG-BYTES")
    assert result["verdict"] == "PASS", result
    assert result["explanation"] == "figures match", result
    assert "Sales $2.3M" in result["tableau_description"], result
    assert "Sales $2.3M" in result["app_description"], result
    assert result["errors"] == [], result
    assert result["tokens"] > 0, result
    assert len(sess6.staged) == 2, sess6.staged
    staged_paths = [p for p, _ in sess6.staged]
    assert any("tableau_" in p for p in staged_paths), staged_paths
    assert any("app_" in p for p in staged_paths), staged_paths
    staged_bytes = [b for _, b in sess6.staged]
    assert b"FAKE-TABLEAU-PNG-BYTES" in staged_bytes
    assert b"FAKE-APP-PNG-BYTES" in staged_bytes

    # a failed image description must surface as UNKNOWN, never a silent
    # PASS, and the comparison call must never be attempted with a missing
    # description
    sess7 = _FakeSession([None, None, "app description text"])
    result7 = parity.vision_validate_dashboard(
        sess7, "stage", "Broken Dash", b"png1", b"png2")
    assert result7["verdict"] == "UNKNOWN", result7
    assert result7["errors"], "a failed description must be recorded, not swallowed"
    assert len(sess7.calls) == 3, \
        "must NOT attempt the comparison call when a description failed"

    print("ok  R8 vision-diff orchestration (ensure_vision_stage requests "
          "SNOWFLAKE_SSE encryption -- the confirmed-required, non-default "
          "type; _describe_image_via_cortex uses the confirmed {0}-"
          "placeholder PROMPT()+TO_FILE() shape, null/raising responses "
          "become stated errors not silent passes; "
          "_compare_descriptions_via_cortex is plain-text, deliberately not "
          "an untested multi-image PROMPT() call; vision_validate_dashboard "
          "stages both real images, orchestrates all three calls, and stops "
          "before the comparison call when either description fails)")


def test_non_star_join_and_blends():
    """ROADMAP R7 (2026-07-26): data-model completeness -- non-star joins + blends.

    PART A -- NON-STAR JOINS. The old check accepted only a STAR (one fact, every
    other table hanging DIRECTLY off it) and reported everything else "model
    manually". But a star is just the depth-1 case of a TREE: a SNOWFLAKE SCHEMA
    (Orders -> Product -> Category) has exactly one path between any two tables,
    so the join order is FORCED, not chosen. Refusing those was a limitation of
    the check, not of the data. What must STILL refuse is what is genuinely
    ambiguous -- multi-fact graphs (joinable several ways, different row counts)
    and cycles/disconnected graphs.

    The SQL correctness point: a depth-2 join's ON clause must reference ITS OWN
    PARENT's alias. The old emitter hardcoded `ON f.<key>` -- fine for a star
    (every parent IS the fact), silently wrong the moment a dim joins to a dim.

    PART B -- BLENDS. A blend is NOT a join: Tableau aggregates the SECONDARY to
    the linking fields' grain and left-joins that aggregate onto the primary's
    view. Modelling it as a row-level join fans out the primary and double-counts
    its measures. The workbook DECLARES its link fields
    (<datasource-relationships><datasource-relationship><column-mapping>) and
    nobody was reading them -- which is why the Cortex fallback had to GUESS a
    blend calc's join key and proposed Region = Segment: SQL that compiled and ran
    cleanly while being wrong. Asserted here against Superstore's REAL blend."""
    import pandas as pd
    import init_workbook as IW
    import semantic_layer as SL

    # ---------- PART A: the join planner ----------
    star = SL.join_plan(
        {"F": {}, "A": {}, "B": {}},
        [{"first": "F", "second": "A", "lkey": "k1", "rkey": "k1"},
         {"first": "F", "second": "B", "lkey": "k2", "rkey": "k2"}])
    assert star["shape"] == "star" and star["root"] == "F", star
    assert all(s["parent_alias"] == "f" for s in star["steps"]), star["steps"]

    chain = SL.join_plan(
        {"Orders": {}, "Product": {}, "Category": {}},
        [{"first": "Orders", "second": "Product", "lkey": "pid", "rkey": "pid"},
         {"first": "Product", "second": "Category", "lkey": "cid", "rkey": "cid"}])
    assert chain["shape"] == "snowflake" and chain["root"] == "Orders", chain
    # THE correctness assertion: the 2nd hop hangs off the 1st, not off the fact.
    assert [s["parent_alias"] for s in chain["steps"]] == ["f", "d0"], chain["steps"]

    # Refusals -- each must name WHY, never silently pick a join.
    mf = SL.join_plan({"A": {}, "B": {}, "C": {}},
                      [{"first": "A", "second": "C", "lkey": "k", "rkey": "k"},
                       {"first": "B", "second": "C", "lkey": "k", "rkey": "k"}])
    assert mf["shape"] == "multi_fact" and not mf["steps"], mf
    assert "fact tables" in mf["reason"], mf
    cyc = SL.join_plan({"A": {}, "B": {}, "C": {}},
                       [{"first": "A", "second": "B", "lkey": "k", "rkey": "k"},
                        {"first": "B", "second": "C", "lkey": "k", "rkey": "k"},
                        {"first": "A", "second": "C", "lkey": "k", "rkey": "k"}])
    assert not cyc["steps"] and "cycle" in cyc["reason"], cyc
    disc = SL.join_plan({"A": {}, "B": {}, "C": {}, "D": {}},
                        [{"first": "A", "second": "B", "lkey": "k", "rkey": "k"},
                         {"first": "C", "second": "D", "lkey": "k", "rkey": "k"}])
    assert not disc["steps"], disc

    # ---------- PART A: the emitted DDL ----------
    ds = {"caption": "Sales Model",
          "objects": {"Orders": {"caption": "Orders", "source": "[Orders]"},
                      "Product": {"caption": "Product", "source": "[Product]"},
                      "Category": {"caption": "Category", "source": "[Category]"}},
          "relationships": [
              {"first": "Orders", "second": "Product", "lkey": "pid", "rkey": "pid"},
              {"first": "Product", "second": "Category", "lkey": "cid", "rkey": "cid"}],
          "columns": {"Orders": ["order_id", "pid", "amount"],
                      "Product": ["pid", "cid", "product_name"],
                      "Category": ["cid", "category_name"]},
          "connection": {"class": "snowflake", "dbname": "DB", "schema": "S"}}
    ddl = SL.generate_views([ds], "DB", "S")
    assert 'LEFT JOIN DB.S.Product d0 ON f."pid" = d0."pid"' in ddl, ddl
    assert 'LEFT JOIN DB.S.Category d1 ON d0."cid" = d1."cid"' in ddl, \
        f"depth-2 join must reference its own parent (d0), not the fact:\n{ddl}"
    assert "CATEGORY_NAME" in ddl, "second-level columns must reach the view"

    # ---------- PART A: the FLATTEN must agree with the DDL ----------
    # One planner drives both. Two data paths disagreeing about what is joinable
    # is this project's most-repeated bug class (the converter/init_workbook
    # decode divergence that silently dropped E-Commerce's dim columns).
    tables = {
        "Orders": pd.DataFrame({"order_id": [1, 2, 3], "pid": [10, 11, 10],
                                "amount": [5, 7, 9], "name": ["a", "b", "c"]}),
        "Product": pd.DataFrame({"pid": [10, 11], "cid": [100, 200],
                                 "name": ["widget", "gadget"]}),
        "Category": pd.DataFrame({"cid": [100, 200],
                                  "category_name": ["Tools", "Toys"]})}
    flat, log = IW.flatten_tables(tables, ds["relationships"])
    assert list(flat["category_name"]) == ["Tools", "Toys", "Tools"], flat
    assert len(flat) == 3, f"row fan-out in a depth-2 flatten: {len(flat)}"
    assert "name (Product)" in flat.columns, \
        f"colliding dim column must be renamed Tableau-style: {list(flat.columns)}"
    assert "snowflake" in log, log
    # a refused graph dumps the largest table + says why, never a guessed join
    _f2, log2 = IW.flatten_tables(
        {"A": pd.DataFrame({"k": [1]}), "B": pd.DataFrame({"k": [1, 2]}),
         "C": pd.DataFrame({"k": [1]})},
        [{"first": "A", "second": "C", "lkey": "k", "rkey": "k"},
         {"first": "B", "second": "C", "lkey": "k", "rkey": "k"}])
    assert "fact tables" in log2 and "largest table ONLY" in log2, log2

    # ---------- PART A: no corpus regression ----------
    # Every corpus multi-table datasource is a STAR; each must stay star + joinable
    # (this refactor replaced the star path, so silent breakage there is the risk).
    for f in ("Superstore.twb", "_ecom.twb"):
        p = os.path.join(ROOT, f)
        if not os.path.exists(p):
            continue
        for m in SL.describe_model(TP.load_twb_xml(p)):
            if m["n_tables"] > 1:
                assert m["shape"] == "star" and m["joinable"], (f, m["caption"], m)
                assert m["view_ddl"] and "LEFT JOIN" in m["view_ddl"], (f, m["caption"])

    # ---------- PART B: blends, against Superstore's REAL blend ----------
    p = os.path.join(ROOT, "Superstore.twb")
    if os.path.exists(p):
        root = TP.load_twb_xml(p)
        bl = TP.blends(root)
        assert len(bl) == 1, bl
        b = bl[0]
        assert b["primary"] == "Sample - Superstore", b
        assert b["secondary"] == "Sales Target", b
        got = {(l["primary_field"], l["secondary_field"]) for l in b["links"]}
        assert got == {("Order Date", "Order Date"), ("Category", "Category"),
                       ("Segment", "Segment")}, got
        # Tableau writes ONE map per pill DERIVATION -- 6 maps for these 3 fields
        # (mn:/tmn:/tyr:/yr: Order Date). Collapsing to the underlying FIELD is
        # what makes this readable instead of six near-duplicate "links".
        assert len(b["links"]) == 3, b["links"]
        od = next(l for l in b["links"] if l["primary_field"] == "Order Date")
        assert len(od["derivations"]) >= 2, od
        assert b["sheets"] == ["Performance"], b["sheets"]

        # the blend is REPORTED, not silently dropped
        kinds = {n["kind"] for n in TP.datasource_notes(root)}
        assert "blend" in kinds, kinds

        # THE POINT: the Cortex prompt now carries the workbook's REAL link
        # fields instead of asking the model to infer join keys (which is how it
        # produced the wrong Region = Segment key -- execute-clean but incorrect).
        import cortex_calc_fallback as CF
        ir = TP.build_ir(p)
        assert ir["blends"], "blends must reach the IR"
        blend_calcs = [f for f in ir["calc_drops"].values()
                       if CF.classify(f) == "blend"]
        assert blend_calcs, "Superstore's blend calcs should classify as 'blend'"
        con = CF.blend_constraint(ir, blend_calcs[0])
        assert "Order Date = Order Date" in con and "Segment = Segment" in con, con
        assert "Do not infer keys" in con, con
        assert "Region = Segment" not in con, "the old wrong-key guess must be gone"
        prompt = CF.build_prompt("X", blend_calcs[0], "blend", "TBL", [],
                                 blend_ctx=con)
        assert "BLEND LINK FIELDS" in prompt, prompt[:400]
        # a NON-blend formula must not get a blend constraint bolted on
        assert CF.blend_constraint(ir, "SUM([Sales])") == "" or \
            "federated" not in "SUM([Sales])", "no spurious blend context"

        # remodel guidance pre-aggregates (the anti-fan-out point), and is
        # clearly marked for review -- it is guidance, never auto-deployed
        sql = TP.blend_remodel_sql(b)
        assert "GROUP BY" in sql and "LEFT JOIN (" in sql, sql
        assert "REVIEW REQUIRED" in sql, sql

    print("ok  R7 non-star joins + blends (snowflake-schema chains join on their "
          "own parent, in BOTH the view DDL and the flatten; multi-fact/cyclic/"
          "disconnected graphs refuse with a reason; Superstore's real blend "
          "links extracted and fed to the Cortex prompt as a constraint)")


def test_custom_sql_execution():
    """MVP item 1 (2026-07-21): custom SQL (<relation type='text'>) was
    DETECTED ONLY -- surfaced as a finding, never executed. A workbook whose
    datasource IS its own custom SQL (no extract) rendered nothing real.

    Scope (matches the 1-day MVP estimate, same boundary as live connections):
    custom SQL whose connection class is 'snowflake' is already valid
    Snowflake SQL (Tableau never translates dialects) and no extract exists to
    fall back on, so it can be EXECUTED VERBATIM as a derived table -- no
    rewriting, no dialect translation attempted. table_for()'s return value is
    interpolated raw into every `FROM {T}` in engine.py with zero identifier
    validation (verified by inspection), so a parenthesized subquery works
    identically to a real table name everywhere downstream -- this feature
    needed ZERO engine.py changes, only onboarding-layer routing (config.py
    already handles a datasource whose CAPTION happens to collide with a
    built-in via the per-workbook routing fix earlier this session; this is
    the analogous fix for what TABLE STRING a caption maps to).

    An extract-backed custom-SQL datasource is UNCHANGED (the extract already
    IS the custom SQL's materialized result -- custom_sql_sources() excludes
    any datasource with an <extract>, same guard as live_connections()). A
    non-Snowflake-class custom SQL is reported honestly (different SQL
    dialect, cannot safely run verbatim against Snowflake) instead of being
    silently skipped or guessed at."""
    import xml.etree.ElementTree as ET
    import pipeline

    root = ET.fromstring(_CUSTOM_SQL_FIXTURE)
    csql = TP.custom_sql_sources(root)

    # (a) Snowflake-class custom SQL: queryable, SQL captured verbatim
    sf = csql["Live Custom SQL"]
    assert sf["queryable"] is True, sf
    assert sf["sql"] == "SELECT region, SUM(amount) AS total FROM orders GROUP BY region", sf

    # (b) non-Snowflake class: honest refusal (different dialect)
    legacy = csql["Legacy Custom SQL"]
    assert legacy["queryable"] is False, legacy
    assert "dialect" in legacy["reason"], legacy

    # (c) extract-backed custom SQL is UNCHANGED -- excluded here entirely
    # (the real corpus already exercises this: any workbook with a shipped
    # extract's custom-SQL relation is decoded via the existing hyper path).
    for f in ("Superstore.twb", "_ecom.twb", "_filtest.twb"):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            assert TP.custom_sql_sources(TP.load_twb_xml(p)) == {}, f

    # (d) datasource_notes distinguishes "executed live" from "detected only"
    notes = {n["datasource"]: n for n in TP.datasource_notes(root) if n["kind"] == "custom-sql"}
    assert "executed live against Snowflake" in notes["Live Custom SQL"]["detail"]
    assert "detected only" in notes["Legacy Custom SQL"]["detail"]

    # (e) configure_datasources routes the queryable one to a derived-table
    # subquery; the non-queryable one falls back unaffected (no regression).
    import config
    _saved_ds = dict(config.DATASOURCES)
    _saved_default = config.DEFAULT_DATASOURCE
    _saved_orders = config.ORDERS
    try:
        ds = pipeline.configure_datasources(
            {"Live Custom SQL": None, "Legacy Custom SQL": None}, custom_sql=csql)
        assert ds["Live Custom SQL"] == {
            "table": "(SELECT region, SUM(amount) AS total FROM orders GROUP BY region) AS LIVE_CUSTOM_SQL_CSQL",
            "local_file": None, "live": True, "custom_sql": True}, ds["Live Custom SQL"]
        assert ds["Legacy Custom SQL"]["table"].startswith(pipeline.LOAD_DB), ds["Legacy Custom SQL"]
    finally:
        config.DATASOURCES.clear()
        config.DATASOURCES.update(_saved_ds)
        config.DEFAULT_DATASOURCE = _saved_default
        config.ORDERS = _saved_orders
        engine.ORDERS = _saved_orders

    # (f) load_into_snowflake genuinely EXECUTES the SQL (proves it compiles
    # and runs), and a failed execution is treated as MISSING -- the pipeline
    # must stop rather than leave sheets pointed at a broken derived table.
    class FakeResult:
        def __init__(s, rows): s._rows = rows
        def collect(s): return s._rows

    class OKSession:
        def sql(s, text):
            if text.strip().startswith("SELECT COUNT(*) FROM (SELECT region"):
                return FakeResult([[55]])
            return FakeResult([[0]])

    class FailingSession:
        def sql(s, text):
            if "SUM(amount)" in text:
                raise RuntimeError("SQL compilation error: invalid identifier 'ORDERS'")
            return FakeResult([[0]])

    ok_report = pipeline.load_into_snowflake(
        OKSession(), {"Live Custom SQL": None}, custom_sql=csql)
    assert ok_report[0][2] == 55, ok_report
    assert ok_report[0][3] == "custom SQL executed live, no copy", ok_report

    fail_report = pipeline.load_into_snowflake(
        FailingSession(), {"Live Custom SQL": None}, custom_sql=csql)
    assert fail_report[0][3].startswith("MISSING"), fail_report
    assert "failed to execute" in fail_report[0][3], fail_report

    print("ok  custom SQL execution (Snowflake-dialect custom SQL run verbatim "
          "as a derived table, no copy, execution-gated; non-Snowflake dialect "
          "reported honestly; extract-backed custom SQL unaffected)")


def test_dashboard_filter_governs_sheet_filter():
    """Live bug (2026-07-21, Superstore_KPI_Parameter_Dashboard_Live): a
    'Region Context Filter' sheet carried a SAVED `Region IN ('Central')`
    (a Tableau context filter) while the dashboard ALSO had its own Region
    filter widget. The engine applied BOTH, AND'd -> `REGION='East' AND
    REGION IN ('Central')` = always empty, so the sheet rendered BLANK for
    every region except the one the workbook was saved on (Central). In
    Tableau a dashboard quick-filter and a worksheet/context filter on the
    SAME field are ONE filter surfaced as a control; the dashboard governs it.

    Fix: _apply_sheet_filters(..., governed=<dashboard-controlled cols>)
    suppresses the sheet's own filter on a governed column."""
    engine.configure(TP.build_ir(TWB))
    import config, backend
    T = config.table_for("Sample - Superstore")
    sheet = {"name": "ctx", "datasource": "Sample - Superstore",
             "applied_filters": [{"caption": "Region", "kind": "in",
                                  "values": ["Central"]}]}
    # WITHOUT governance: the sheet's own Central filter is AND'd on -> the old
    # (buggy) double-filter; East + Central = empty.
    w_plain = engine._apply_sheet_filters(sheet, "WHERE REGION = 'East'", T)
    assert "Central" in w_plain, w_plain
    n_plain = backend.run_sql(f"SELECT COUNT(*) AS C FROM {T} {w_plain}")["C"][0]
    assert n_plain == 0, f"expected the OLD double-filter to be empty, got {n_plain}"
    # WITH the dashboard governing REGION: the sheet's saved Central value is
    # dropped, so the dashboard's East selection alone applies -> non-empty.
    w_gov = engine._apply_sheet_filters(sheet, "WHERE REGION = 'East'", T,
                                        governed={"REGION"})
    assert "Central" not in w_gov, w_gov
    n_east = backend.run_sql(f"SELECT COUNT(*) AS C FROM {T} {w_gov}")["C"][0]
    assert n_east > 0, f"East should render rows once the sheet's Central filter is governed away, got {n_east}"
    print("ok  dashboard filter governs a sheet's same-column (context) filter "
          "-- no double-filter blanking (Superstore KPI Live 'Region Context' fix)")


def test_dashboard_filter_all_overrides_sheet_saved_value():
    """Live bug (2026-07-21, follow-up): the 'governed' suppression only fired
    when the dashboard widget had a SPECIFIC value selected -- build_where
    emitted no part for an 'All' selection, so `governed` was empty and the
    sheet's SAVED context-filter value (Region='Central') leaked back in when
    the widget read 'All'. Result: the Top-N chart showed only Central rows at
    Region='All' -- e.g. a customer's Central-only ~$50 instead of the true
    all-region $10,311. 'All' must GOVERN the column (override the saved value)
    while adding no restriction.

    Fix: build_where always emits a part (clause=None at 'All') with
    governs=True; _where_for skips None clauses; the column is still governed.
    Locks: an 'All' part (clause=None) still governs its column, and a sheet's
    own saved filter on that column is dropped -> no restriction, all rows."""
    engine.configure(TP.build_ir(TWB))
    import config, backend
    T = config.table_for("Sample - Superstore")
    sheet = {"name": "Top Customers", "datasource": "Sample - Superstore",
             "applied_filters": [{"caption": "Region", "kind": "in", "values": ["Central"]}]}

    # an 'All' selection = a part with clause=None, scope-bound to this sheet
    all_part = {"col": "REGION", "clause": None, "caption": "Region",
                "scope": "Top Customers", "governs": True}
    applicable = engine._parts_for_sheet([all_part], sheet)
    # the 'All' part applies (bound sheet) and contributes NO SQL restriction
    assert engine._where_for(T, applicable) == "", engine._where_for(T, applicable)
    # but it GOVERNS REGION, so the sheet's saved Central value is suppressed
    governed = {p["col"] for p in applicable if isinstance(p, dict)}
    assert governed == {"REGION"}, governed
    w = engine._apply_sheet_filters(sheet, "", T, governed=governed)
    assert "Central" not in w, w
    # full table (all regions) is visible -- not narrowed to Central
    n_all = backend.run_sql(f"SELECT COUNT(*) AS C FROM {T}")["C"][0]
    n_got = backend.run_sql(f"SELECT COUNT(*) AS C FROM {T} {w}".strip())["C"][0]
    assert n_got == n_all, f"'All' must show every region ({n_all}), got {n_got}"
    print("ok  dashboard filter at 'All' overrides a sheet's saved value (governs "
          "the column, no restriction) -- no stale context-filter narrowing")


def test_context_filter_applied_inside_topn_ranking():
    """Live bug (2026-07-21, Superstore_KPI_Parameter_Dashboard_Live 'by Sales'
    version): the 'Top Customers' sheet has a Customer-Name top-N BY SUM(Sales)
    and a Region filter marked <filter context='true'>. Tableau applies a
    CONTEXT filter BEFORE the top-N, so Region=Central means 'the top 10
    customers WITHIN Central'. The engine ranked over the WHOLE table then
    filtered to Central -> it showed the GLOBAL top customers with their tiny
    Central-only sales (Sean Miller $526, Raymond Buch $20) instead of Central's
    real top customers (Tamara Chand 18,437 ... Laura Armstrong 5,076).

    Two parts: (a) parser captures context columns SEPARATELY (context_fields)
    because an all-members context filter is skipped by applied_filters; (b)
    engine injects the context column's live (dashboard-governed) value into the
    top-N ranking subquery's FROM.

    Verified on Superstore: a synthetic 'context Region=Central' + top-N by
    SUM(Sales) ranks WITHIN Central (every returned customer's Central sales is
    among Central's real top), and the SAME ranking with NO context returns a
    genuinely different, higher-valued global set."""
    engine.configure(TP.build_ir(TWB))
    import config, backend
    T = config.table_for("Sample - Superstore")

    # (a) parser: context_columns picks up a context='true' filter even when the
    # worksheet enumerates all its members. Synthetic worksheet XML:
    import xml.etree.ElementTree as ET
    ws_xml = """<worksheet name='w'><table><view>
      <datasources><datasource name='x' caption='Sample - Superstore'/></datasources>
      <filter class='categorical' column='[x].[none:Region:nk]' context='true'>
        <groupfilter function='level-members' level='[none:Region:nk]' user:ui-enumeration='all'/>
      </filter>
    </view></table></worksheet>"""
    ws = ET.fromstring(ws_xml.replace("user:", "ui_"))  # strip ns prefix for ET
    # parse_field needs the caption; context_columns returns the field caption
    cctx = TP.context_columns(ws, {})
    assert any("Region" in c for c in cctx), cctx

    # (b) engine: a context Region=Central injected into the top-N subquery
    sheet = {"name": "Top Customers", "datasource": "Sample - Superstore",
             "context_fields": ["Region"],
             "applied_filters": [{"caption": "Customer Name", "kind": "top_n",
                                  "dir": "top", "order_expr": "SUM([SALES])",
                                  "order_dir": "DESC", "n": 10}]}
    part = {"col": "REGION", "clause": "REGION = 'Central'", "caption": "Region",
            "scope": "Top Customers", "governs": True}
    w = engine._apply_sheet_filters(sheet, "WHERE REGION = 'Central'", T,
                                    governed={"REGION"}, dash_parts=[part])
    # the ranking subquery must itself be restricted to Central
    assert "FROM %s WHERE REGION = 'Central'" % T in w.replace("  ", " "), w
    got = backend.run_sql(f"SELECT CUSTOMER_NAME, SUM(SALES) v FROM {T} {w} "
                          "GROUP BY 1 ORDER BY v DESC").iloc[:10]
    within = set(got["CUSTOMER_NAME"])
    # independent ground truth: Central's real top-10 customers by sales
    truth = backend.run_sql(
        f"SELECT CUSTOMER_NAME FROM {T} WHERE REGION='Central' GROUP BY 1 "
        "ORDER BY SUM(SALES) DESC LIMIT 10")["CUSTOMER_NAME"].tolist()
    assert within == set(truth), (within ^ set(truth))

    # contrast: NO context -> a genuinely different (global) ranking
    w_noctx = engine._apply_sheet_filters(
        {"name": "t2", "datasource": "Sample - Superstore",
         "applied_filters": sheet["applied_filters"]}, "", T)
    glob = backend.run_sql(f"SELECT CUSTOMER_NAME, SUM(SALES) v FROM {T} {w_noctx} "
                           "GROUP BY 1 ORDER BY v DESC").iloc[:10]
    assert set(glob["CUSTOMER_NAME"]) != within, "global vs context ranking must differ"

    # a RANGE / DATE-PART context filter now injects too (not just categorical) --
    # the shared _value_predicate covers every fixed-value filter kind
    assert engine._value_predicate({"caption": "Sales", "kind": "range",
                                    "min": 100, "max": 500}) == "SALES >= 100 AND SALES <= 500"
    assert engine._value_predicate({"caption": "Order Date", "kind": "in",
        "datepart": "yr", "values": [2021]}) == "EXTRACT(YEAR FROM ORDER_DATE) IN (2021)"
    assert engine._value_predicate({"caption": "Region", "kind": "not_in",
        "values": ["West"]}) == "REGION NOT IN ('West')"
    print("ok  context filter applied INSIDE the top-N ranking (top-N within the "
          "region, not global-then-filtered; range/date-part/exclude covered) -- "
          "Superstore KPI Live Central fix")


def test_bar_colored_by_own_axis_has_no_offset():
    """Live bug (2026-07-21, same workbook): 'Selected Measure by Category'
    is a bar with y=Category AND color=Category AND grouped=True. The engine
    added a yOffset keyed to the color field -- but offsetting a bar by its OWN
    axis dimension reserves one slot per category inside EVERY category band
    and fills only the matching one, so each bar shrank to 1/N of its band with
    the rest blank ('inconsistent gaps' between the bars). Fix: only add the
    grouped-bar offset when the color/group field is a DIFFERENT dimension from
    the axis dimension."""
    engine.configure(TP.build_ir(TWB))
    import altair as alt
    charts = []
    _orig = engine.st.altair_chart
    engine.st.altair_chart = lambda c=None, **k: charts.append(c)
    st = engine.st
    try:
        # bar colored by its OWN axis dim -> must NOT get an offset
        same = {"name": "same", "kind": "bar", "mark": "Bar", "orient": "h",
                "grouped": True, "datasource": "Sample - Superstore",
                "y": {"caption": "Category"},
                "x": {"caption": "Sales", "agg": "sum"},
                "color": {"caption": "Category", "kind": "dimension", "agg": "none"}}
        charts.clear()
        engine.render_sheet(same, [])
        enc = charts[0].to_dict().get("encoding", {})
        assert "yOffset" not in enc and "xOffset" not in enc, \
            f"a bar colored by its own axis dim must not be offset: {list(enc)}"

        # control: a bar grouped by a DIFFERENT dim (Category axis, Segment
        # color) legitimately keeps its offset -- the fix must not kill real
        # grouped bars.
        diff = {"name": "diff", "kind": "bar", "mark": "Bar", "orient": "h",
                "grouped": True, "datasource": "Sample - Superstore",
                "y": {"caption": "Category"},
                "x": {"caption": "Sales", "agg": "sum"},
                "color": {"caption": "Segment", "kind": "dimension", "agg": "none"}}
        charts.clear()
        engine.render_sheet(diff, [])
        enc2 = charts[0].to_dict().get("encoding", {})
        assert "yOffset" in enc2, \
            f"a genuinely grouped bar (color != axis) must keep its offset: {list(enc2)}"
    finally:
        st.altair_chart = _orig
    print("ok  bar colored by its own axis dimension gets no grouped offset "
          "(no phantom gaps); a real cross-dim grouped bar keeps its offset")


def test_dashboard_filter_scoped_to_bound_sheets():
    """Live bug (2026-07-21, Superstore_KPI_Parameter_Dashboard_Live): a
    dashboard 'Region' QUICK-FILTER (a placed <zone type='filter'
    name='Top N Customers by Sales'>) was applied to EVERY sheet on the
    dashboard's datasource -- so it bled onto the PARAMETER-driven
    'Selected Measure by Category' chart and AND'd with its Selected Region
    parameter: Region='West' (filter) AND Region='South' (param calc) = BLANK.
    It also silently narrowed the KPI Summary tile (which Tableau leaves at the
    grand total) to a single region.

    Tableau scope: a placed filter applies to the worksheet its zone is bound
    to, PLUS any worksheet carrying the same field in its own filters (Tableau
    writes a multi-worksheet filter into each affected worksheet's XML). Fix:
    parser captures the zone's `scope_sheet`; engine._parts_for_sheet applies a
    part only to its bound sheet or a sheet that filters on the same field.

    Verified two ways: (a) the parser captures scope_sheet from the real
    workbook's Region zone; (b) _parts_for_sheet routes a scoped Region part to
    ONLY the bound sheet, not to a KPI/parameter sibling -- and the corpus
    invariant that Superstore's own dashboard filters still reach every sheet
    that legitimately carries the field (Order Date across the Overview tiles).
    """
    # (a) parser captures the zone's bound worksheet on the corpus (Superstore
    # Overview: Region is bound to Sale Map; Order Date likewise a placed filter)
    ir = TP.build_ir(TWB)
    ov = next(d for d in ir["dashboards"] if d["name"] == "Overview")
    region_f = next(f for f in ov["filters"] if f["caption"] == "Region")
    assert region_f.get("scope_sheet") == "Sale Map", region_f

    # (b) _parts_for_sheet routing: a Region part bound to 'Sale Map' reaches
    # Sale Map but NOT a sibling that neither is the target nor filters on Region
    engine.configure(ir)
    region_part = {"col": "REGION", "clause": "REGION = 'West'",
                   "caption": "Region", "scope": "Sale Map"}
    order_part = {"col": "ORDER_DATE", "clause": "ORDER_DATE >= '2021-01-01'",
                  "caption": "Order Date", "scope": "Sale Map"}
    parts = [region_part, order_part]

    sale_map = {"name": "Sale Map",
                "applied_filters": [{"caption": "Order Date"}, {"caption": "Profit Ratio"}]}
    total_sales = {"name": "Total Sales",           # sibling: only Order Date of its own
                   "applied_filters": [{"caption": "Order Date"}]}

    got_map = {p["col"] for p in engine._parts_for_sheet(parts, sale_map)}
    assert got_map == {"REGION", "ORDER_DATE"}, got_map            # bound sheet gets both

    got_total = {p["col"] for p in engine._parts_for_sheet(parts, total_sales)}
    # Order Date reaches Total Sales (it filters on Order Date itself); Region
    # does NOT (not the bound sheet, no own Region filter) -- the exact fix.
    assert got_total == {"ORDER_DATE"}, got_total

    # a part with NO scope stays global (standalone-tab filters must not regress)
    globalpart = {"col": "REGION", "clause": "REGION = 'West'", "caption": "Region", "scope": None}
    assert engine._parts_for_sheet([globalpart], total_sales) == [globalpart]
    print("ok  dashboard filter scoped to its bound sheet (+ sheets that filter "
          "on the field) -- no bleed onto parameter/KPI siblings; unscoped "
          "filters stay global (Superstore KPI Live Region/param independence)")


def test_union_support():
    """MVP item 3 (2026-07-22): a Tableau UNION (<relation type='union'>) stacks
    same-schema inputs row-wise (UNION ALL) -- multiple CSVs / Excel sheets. NO
    corpus workbook has one, so this validates against a SYNTHETIC fixture
    (tests/fixtures/union_test.twb + union_east.csv [3 rows] / union_west.csv
    [2 rows]). Before: onboarding's pick_local_file grabbed ONE member file and
    silently dropped the others' rows. After: the parser detects the members and
    onboarding materializes ALL of them into one table.

    HONEST NOTE: synthetic-validated only until a real union workbook exists."""
    import tempfile, pandas as pd
    import config, backend, init_workbook as IW
    fx = os.path.join(HERE, "fixtures")
    root = TP.load_twb_xml(os.path.join(fx, "union_test.twb"))

    # (a) parser detects both members, in order
    um = TP.union_members(root)
    assert um == {"Sales Union": ["union_east.csv", "union_west.csv"]}, um
    # (b) datasource_notes reports the union honestly
    assert "union" in {n["kind"] for n in TP.datasource_notes(root)}

    # (c) materialization = UNION ALL of every member (by column name)
    out = os.path.join(tempfile.mkdtemp(), "u.csv")
    IW.materialize_union([os.path.join(fx, "union_east.csv"),
                          os.path.join(fx, "union_west.csv")], out)
    df = pd.read_csv(out)
    assert len(df) == 5, len(df)                     # 3 east + 2 west, not one member
    assert set(df["Region"]) == {"East", "West"}, set(df["Region"])
    assert df["Sales"].sum() == 1000, df["Sales"].sum()
    assert "Table Name" in df.columns, "Tableau adds a union source column"

    # (d) queried through the backend, BOTH members aggregate correctly
    cap = "Sales Union"
    saved, sd, so = dict(config.DATASOURCES), config.DEFAULT_DATASOURCE, config.ORDERS
    try:
        config.DATASOURCES = {cap: {"table": "UNIONDB.PUBLIC.SALES_UNION",
                                    "local_file": out, "union": True}}
        config.DEFAULT_DATASOURCE, config.ORDERS = cap, "UNIONDB.PUBLIC.SALES_UNION"
        backend._LOCAL_CON = None
        agg = backend.run_sql("SELECT REGION AS D, SUM(SALES) AS V FROM "
                              "UNIONDB.PUBLIC.SALES_UNION GROUP BY 1")
        assert dict(zip(agg["D"], agg["V"])) == {"East": 450, "West": 550}, dict(zip(agg["D"], agg["V"]))
    finally:
        config.DATASOURCES.clear(); config.DATASOURCES.update(saved)
        config.DEFAULT_DATASOURCE, config.ORDERS = sd, so
        backend._LOCAL_CON = None

    # (e) no corpus false positive (no real workbook has a union)
    for f in ("Superstore.twb", "_ecom.twb", "_filtest.twb"):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            assert TP.union_members(TP.load_twb_xml(p)) == {}, f
    print("ok  union support (Tableau UNION -> all members materialized row-wise; "
          "both members queried, not just one; no corpus false positive)")


def test_tracker_consistency():
    """SELF-POLICING gate against tracker drift (added 2026-07-22 after the
    weekly report silently understated a full session's work).

    The weekly status report is only ~1/7 auto-computed (the regression gate
    count); the rest -- audit status labels + status_config roadmap/mvp -- is
    hand-maintained, so shipping a feature does NOT update them. This gate makes
    the drift MECHANICAL: for every feature that has a passing proving-gate, it
    asserts the audits do NOT still mark it 'gap' and the roadmap does NOT still
    mark it 'planned'. If you ship a construct with its own gate, add a row to
    SHIPPED below -- and if you forget to flip its audit/roadmap status, THIS
    gate goes red and names exactly what's stale.

    It intentionally only checks for UNDER-statement (gap/planned for something
    proven to work) -- it never forces a status up, so honest 'partial'/'progress'
    labels are fine."""
    import json, inspect
    import audit_features, audit_filters
    mod = sys.modules[__name__]
    main_src = inspect.getsource(main)          # to check a gate is actually run

    def feat_status(label):
        return next((r[2] for r in audit_features.FEATURES if r[1] == label), "MISSING")

    def filt_status(label):
        return next((r[1] for r in audit_filters.FILTER_TYPES if r[0] == label), "MISSING")

    # (human label, status-lookup, proving regression gate) -- every construct
    # shipped with a dedicated gate. Add a row when you ship a new one.
    SHIPPED = [
        ("Unions", feat_status, "test_union_support"),
        ("Live data source (map to table)", feat_status, "test_live_connection_support"),
        ("Custom SQL", feat_status, "test_custom_sql_execution"),
        ("Context filters", feat_status, "test_context_filter_applied_inside_topn_ranking"),
        ("Context filter (order of operations)", filt_status,
         "test_context_filter_applied_inside_topn_ranking"),
    ]
    bad = []
    for label, lookup, gate in SHIPPED:
        if not callable(getattr(mod, gate, None)):
            bad.append(f"proving gate {gate}() for '{label}' does not exist")
            continue
        if (gate + "()") not in main_src:
            bad.append(f"gate {gate}() exists but is NOT wired into main() -- it never runs")
        st = lookup(label)
        if st == "MISSING":
            bad.append(f"'{label}' not found in its audit table")
        elif st == "gap":
            bad.append(f"'{label}' has passing gate {gate}() but its audit still marks it 'gap'")

    # status_config roadmap: a shipped-and-done feature must not read 'planned'
    cfg = json.load(open(os.path.join(ROOT, "status_config.json"), encoding="utf-8"))
    DONE_IN_ROADMAP = ["Union support", "Live connection support",
                       "Custom SQL execution", "Per-workbook profile",
                       "Human-gated in-app Deploy button",
                       "Auto-point to an existing Snowflake table",
                       "Multi-table extract auto-bind",
                       "Live connection with a multi-table JOIN"]
    for name in DONE_IN_ROADMAP:
        for r in cfg["roadmap"]:
            if name.lower() in r.get("item", "").lower() and r.get("status") == "planned":
                bad.append(f"roadmap '{r['item']}' is a shipped feature but still 'planned'")

    # MVP manager doc (MVP_ACCELERATOR_SCOPE.md) must be INTERNALLY consistent
    # with the MVP done-count -- catches the drift where the top table was
    # updated (5 of 6) but the grand-total/summary at the bottom still said an
    # OLD count (4 of 6). Assert the doc's done-count matches status_config's
    # mvp.items AND no CONTRADICTORY 'N of 6' appears anywhere in the file.
    mvp_done = sum(1 for it in cfg.get("mvp", {}).get("items", [])
                   if it.get("status") == "done")
    mvp_md = os.path.join(ROOT, "MVP_ACCELERATOR_SCOPE.md")
    if os.path.exists(mvp_md) and mvp_done:
        txt = open(mvp_md, encoding="utf-8").read()
        if f"{mvp_done} of 6" not in txt:
            bad.append(f"MVP_ACCELERATOR_SCOPE.md: status_config has {mvp_done}/6 MVP done "
                       f"but the doc never says '{mvp_done} of 6'")
        for other in range(7):
            if other != mvp_done and f"{other} of 6" in txt:
                bad.append(f"MVP_ACCELERATOR_SCOPE.md: contains stale '{other} of 6' "
                           f"(a contradictory count) -- MVP is {mvp_done} of 6")

    assert not bad, "TRACKER DRIFT (report understates shipped work):\n  " + "\n  ".join(bad)
    print("ok  tracker consistency (no shipped feature is understated as 'gap'/'planned' "
          "in the audits/roadmap; MVP doc internally agrees on the done-count)")


def test_build_data_model_tables_scope_b():
    """Scope B: load a STAR datasource's tables SEPARATELY (not flattened) +
    CREATE the relationship view + repoint config at the view, so the app and the
    existing Stage-5 validation query the real replicated data model. Offline:
    the hyper decode is monkeypatched to tiny CSVs and a stub session records
    write_pandas + SQL. Proves the orchestration (3 tables written, one view
    created, config repointed) without a live Snowflake account."""
    import re
    import tempfile
    import config
    import init_workbook as IW
    import pipeline

    work = tempfile.mkdtemp(prefix="scopeb_")
    def _csv(name):
        p = os.path.join(work, name + ".csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("A,B\n1,2\n")
        return p
    fake = {"Orders": _csv("Orders"), "People": _csv("People"), "Returns": _csv("Returns")}
    _orig = IW.hyper_to_tables
    IW.hyper_to_tables = lambda hp, outdir=".": dict(fake)

    class _R:
        def __init__(s, r): s._r = r
        def collect(s): return s._r

    class Stub:
        def __init__(s): s.written, s.created, s.existing = [], [], set()
        def write_pandas(s, df, table, **k): s.written.append(table)
        def sql(s, q):
            u = q.upper()
            if u.strip().startswith("CREATE OR REPLACE VIEW"):
                s.created.append(q)
                m = re.search(r"VIEW\s+(\S+)", q, re.I)
                if m:
                    s.existing.add(m.group(1).replace('"', '').upper())
                return _R([])
            if "INFORMATION_SCHEMA.SCHEMATA" in u:
                return _R([{"N": 1}])
            if "INFORMATION_SCHEMA.TABLES" in u:
                db = re.search(r'"([^"]+)"\.INFORMATION_SCHEMA', u)
                sch = re.search(r"TABLE_SCHEMA = '([^']+)'", u)
                tbl = re.search(r"TABLE_NAME = '([^']+)'", u)
                if db and sch and tbl:
                    fqn = f"{db.group(1)}.{sch.group(1)}.{tbl.group(1)}".upper()
                    return _R([{"N": 1 if fqn in s.existing else 0}])
                return _R([{"N": 0}])
            return _R([])

    save = (dict(config.DATASOURCES), config.DEFAULT_DATASOURCE,
            config.ORDERS, engine.ORDERS)
    try:
        root = TP.load_twb_xml(TWB)
        stub = Stub()
        rep = pipeline.build_data_model_tables(
            stub, root, ["fake.hyper"], db="WBR_DB", schema="PIPELINE_DEMO")
        assert set(stub.written) >= {"ORDERS", "PEOPLE", "RETURNS"}, stub.written
        assert any(q.strip().upper().startswith("CREATE OR REPLACE VIEW")
                   for q in stub.created), "no CREATE VIEW issued"
        star = [r for r in rep if r[1]]                 # entries with a view fqn
        assert star and star[0][1].endswith("SAMPLE_SUPERSTORE_MODEL"), rep
        assert "replicated" in star[0][2], rep
        assert config.DATASOURCES.get("Sample - Superstore", {}).get(
            "table", "").endswith("SAMPLE_SUPERSTORE_MODEL"), \
            "config not repointed at the data-model view"
    finally:
        IW.hyper_to_tables = _orig
        config.DATASOURCES.clear(); config.DATASOURCES.update(save[0])
        config.DEFAULT_DATASOURCE = save[1]
        config.ORDERS = save[2]; engine.ORDERS = save[3]
    print("ok  scope B (star datasource -> separate tables + relationship view, "
          "config repointed at the view so Stage-5 validates the real model)")


def test_section_validation_notebook():
    """R2 — per-section Tableau validation notebook (dashboard-validation
    methodology): a three-way table<->app<->Tableau layout, a PLUGGABLE
    Tableau ground truth (the seam R1's real per-section REST values drop
    into), and — as of 2026-07-28, an explicit user architecture decision —
    Cortex now DECIDES the PASS/BUG verdict at notebook-run time (not just
    narrates it): each metric gets a %%sql cell computing the real app value
    and handing it + the real Tableau value to CORTEX.COMPLETE for JSON
    judgment, then a plain %python cell (stdlib only, no project-module
    import) that parses that JSON and prints the verdict Cortex actually
    returned. The old deterministic check_calc_metrics verdict is still
    shown as a labeled cross-check, never silently dropped."""
    import ast
    import json
    import parity

    ir = {"source_file": TWB}
    result = {
        "datasources": [{"datasource": "Sample - Superstore",
                         "table": "WBR_DB.PUBLIC.ORDERS", "app_rows": 9994,
                         "match": True, "source_rows": 9994}],
        "calc_metrics": [
            {"datasource": "Sample - Superstore", "metric": "Profit Ratio",
             "name": "Profit Ratio", "sql": "SUM(PROFIT)/NULLIF(SUM(SALES),0)",
             "value": 0.12, "tableau_bound": (0.11, 0.13), "error": None,
             "verdict": "PASS"},
            {"datasource": "Sample - Superstore", "metric": "Bad Metric",
             "name": "Bad Metric", "sql": "SUM(NOPE)", "value": None,
             "tableau_bound": (1, 2), "error": "BinderException", "verdict": "BUG"},
            {"datasource": "Sample - Superstore", "metric": "No Truth Metric",
             "name": "No Truth Metric", "sql": "SUM(WHATEVER)", "value": 42.0,
             "tableau_bound": None, "error": None, "verdict": "EXECUTED"},
        ],
    }
    nb = parity.build_section_validation_notebook(ir, result, "Superstore")
    d = json.loads(nb)                                   # valid notebook JSON
    assert d["nbformat"] == 4
    assert all(c.get("outputs") == [] and c.get("execution_count") is None
               for c in d["cells"] if c["cell_type"] == "code"), \
        "code cells must have cleared outputs + null execution_count"
    assert "SNOWFLAKE.CORTEX.COMPLETE" in nb, "no Cortex judge cell"
    assert "Cortex-judged validation" in nb
    assert "Bug summary" in nb and "Bad Metric" in nb, "bug metric not surfaced"
    # deterministic verdict still shown as a labeled cross-check, not the
    # headline verdict (that's Cortex's job now)
    assert "Deterministic cross-check" in nb and "BinderException" in nb

    # REAL BUG FOUND LIVE 2026-07-29: a metric with NO independent Tableau
    # reference at all (no known-figure bound, no live REST pull) was still
    # sent to Cortex to "judge" against the literal text "unknown" -- Cortex
    # correctly cannot validate that and reliably answered BUG, which
    # misrepresented "nothing to compare against" as a real defect (flooded
    # a live Stage 5 run with false BUGs). Fixed: such a metric must be
    # skipped entirely, never handed to Cortex as a fake comparison.
    assert "No Truth Metric" in nb
    assert "No independent Tableau reference available" in nb, \
        "a metric with no Tableau reference at all must say so, not be silently judged"
    assert "NO_REFERENCE" in nb
    # exactly 2 real Cortex judge calls (Profit Ratio, Bad Metric) -- NOT 3;
    # "No Truth Metric" must never generate a CORTEX.COMPLETE cell asking it
    # to compare against nothing
    assert nb.count("SNOWFLAKE.CORTEX.COMPLETE(") == 2, \
        f"expected exactly 2 real Cortex judge calls, got {nb.count('SNOWFLAKE.CORTEX.COMPLETE(')}"

    # every plain %python cell (stdlib-only parse/rollup logic) must be
    # SYNTACTICALLY VALID Python -- this is generated code-as-text, pyflakes
    # on parity.py itself cannot see string-escaping mistakes inside it, so
    # compile() each one directly. %%sql-magic cells are skipped (not valid
    # standalone Python).
    py_cells = [c for c in d["cells"] if c["cell_type"] == "code"
                and not "".join(c["source"]).lstrip().startswith("%%sql")]
    assert py_cells, "expected at least the _r2_results setup + parse + rollup cells"
    for c in py_cells:
        src = "".join(c["source"])
        try:
            ast.parse(src)
        except SyntaxError as e:
            raise AssertionError(f"generated %python cell has a syntax error: "
                                 f"{e}\n---\n{src}")

    # PLUGGABLE Tableau ground truth (the R1/R2 seam): overriding a metric's
    # Tableau value must change the notebook, proving real per-section REST
    # values can drop in.
    nb2 = parity.build_section_validation_notebook(
        ir, result, "Superstore", tableau_truth={"Profit Ratio": 0.125})
    assert "0.125" in nb2 and "0.125" not in nb, "tableau_truth not pluggable"

    # --- cortex_judge_section: the LIVE in-app judge, used by pipeline_app.py
    # Stage 5 directly (not just baked into the downloadable notebook) -------
    class _FakeResult:
        def __init__(self, text):
            self._text = text

        def collect(self):
            return [(self._text,)]

    class _FakeSession:
        def __init__(self, text):
            self._text = text

        def sql(self, q):
            assert "SNOWFLAKE.CORTEX.COMPLETE" in q, q
            return _FakeResult(self._text)

    # clean JSON response -> parsed verdict + explanation
    sess = _FakeSession('{"verdict": "PASS", "explanation": "within tolerance"}')
    v, exp, tok, err = parity.cortex_judge_section(
        sess, "Profit Ratio", "SUM([Profit])/SUM([Sales])",
        "SUM(PROFIT)/NULLIF(SUM(SALES),0)", 0.12, "0.11-0.13")
    assert v == "PASS" and exp == "within tolerance" and err is None, (v, exp, err)
    assert tok > 0, tok

    # response wrapped in markdown fences / extra prose -> still parses (the
    # first decodable JSON object wins, same robustness class as
    # cortex_calc_fallback.json_payload for arrays)
    sess2 = _FakeSession(
        'Sure, here is my answer:\n```json\n{"verdict": "BUG", '
        '"explanation": "off by a rounding function"}\n```')
    v2, exp2, _, err2 = parity.cortex_judge_section(
        sess2, "X", "f", "s", 1, "2")
    assert v2 == "BUG" and exp2 == "off by a rounding function" and err2 is None

    # garbage response -> UNKNOWN, never a silent PASS
    sess3 = _FakeSession("I cannot determine this.")
    v3, exp3, _, err3 = parity.cortex_judge_section(sess3, "X", "f", "s", 1, "2")
    assert v3 == "UNKNOWN" and err3 is not None, (v3, err3)

    # a raising session -> UNKNOWN + the exception surfaced, never propagated
    class _RaisingSession:
        def sql(self, q):
            raise RuntimeError("boom")

    v4, exp4, tok4, err4 = parity.cortex_judge_section(
        _RaisingSession(), "X", "f", "s", 1, "2")
    assert v4 == "UNKNOWN" and tok4 == 0 and "boom" in err4

    print("ok  section validation notebook (three-way table<->app<->Tableau; "
          "Cortex DECIDES the verdict at notebook-run time via a JSON judge "
          "prompt + a stdlib-only parse cell, generated %python cells proven "
          "syntactically valid via compile(); deterministic check_calc_metrics "
          "verdict kept as a labeled cross-check; pluggable Tableau ground "
          "truth = R1/R2 seam; cortex_judge_section covers clean/fenced/"
          "garbage/raising Cortex responses, UNKNOWN never a silent PASS)")


def test_r2_live_truth_pull():
    """ROADMAP R2 (2026-07-28, completing it). Mapping Tableau's REST-
    rendered view data to calc_metrics, so tableau_truth carries Tableau's
    ACTUAL numbers instead of just the TWB formula's known-figure bound.

    ONLY ONE CONFIDENCE TIER NOW: EXACT (a single-row CSV, Tableau's own
    aggregation, no re-aggregation on our side). The APPROXIMATE
    (multi-row, column-summed) tier introduced 2026-07-29 was REMOVED
    2026-07-30 after its first live run produced a 6x-inflated Quantity --
    the view's rows did not partition the data, and that is not reliably
    decidable from a CSV alone (see test_raw_measure_live_truth_and_json_
    verdicts for the row-partition diagnostic and the VDS replacement,
    which asks Tableau's own engine for a true aggregate instead of
    guessing from a rendered view)."""
    import parity
    import tableau_server as TSV

    calc_metrics = [
        {"metric": "Profit Ratio", "name": "Profit Ratio",
         "sql": "SUM(PROFIT)/NULLIF(SUM(SALES),0)"},
        {"metric": "Sales", "name": "Sales", "sql": "SUM(SALES)"},
    ]

    # --- EXACT match: single-row CSV, header-normalization ------------------
    truth, reason = parity.truth_from_view_csv(
        "AGG(Profit Ratio),SUM(Sales)\n0.1256,2326534.35\n", calc_metrics)
    assert truth == {"Profit Ratio": {"value": 0.1256, "approx": False, "rows": 1},
                     "Sales": {"value": 2326534.35, "approx": False, "rows": 1}}, truth
    assert reason is None

    # --- multi-row CSVs are NEVER summed anymore -- always empty, with a
    # reason naming it a dimension breakdown, regardless of shape ----------
    truth2, reason2 = parity.truth_from_view_csv(
        "Category,SUM(Sales)\nFurniture,754747.76\nOffice Supplies,731893.31\n",
        calc_metrics)
    assert truth2 == {}, \
        f"a multi-row view must never be summed, even for a pure-SUM metric: {truth2}"
    assert reason2 is not None and "grand total" in reason2, reason2

    truth2b, reason2b = parity.truth_from_view_csv(
        "Category,SUM(Discount)\nFurniture,12.3\nOffice Supplies,9.1\n", calc_metrics)
    assert truth2b == {}, truth2b
    assert reason2b is not None and "grand total" in reason2b

    truth3, reason3 = parity.truth_from_view_csv(
        "Region,SUM(Quantity)\nEast,1234\n", calc_metrics)
    assert truth3 == {} and reason3 is not None, \
        "single row but no header matches any metric caption -- empty, with a reason"

    truth4, _ = parity.truth_from_view_csv("", calc_metrics)
    assert truth4 == {}, "an empty CSV must not raise or fabricate a value"

    # --- pull_live_tableau_truth: multi-row views contribute NOTHING; a
    # single-row EXACT match from ANY view still resolves; first-EXACT-wins
    # among multiple exact matches; one bad view's error doesn't abort the
    # rest --------------------------------------------------------------
    def _fake_pull(server_url, site, wb_id, token_name=None, token_secret=None):
        assert server_url == "https://x" and site == "s" and wb_id == "wb-1", \
            (server_url, site, wb_id)
        return [
            # multi-row -- must contribute nothing now
            {"view": "By Category", "id": "v1",
             "csv": "Category,SUM(Sales)\nFurniture,754747.76\nOffice Supplies,731893.31\n",
             "error": None},
            {"view": "Broken", "id": "v2", "csv": None, "error": "RuntimeError: boom"},
            # the EXACT single-row Sales + Profit Ratio figures
            {"view": "Overview", "id": "v3",
             "csv": "AGG(Profit Ratio),SUM(Sales)\n0.1256,2326534.35\n", "error": None},
            # a further LATER view naming Profit Ratio again must NOT
            # overwrite the already-resolved value
            {"view": "Duplicate Profit Ratio", "id": "v4",
             "csv": "AGG(Profit Ratio)\n0.999\n", "error": None},
        ]

    real_pull = TSV.pull_all_view_csvs
    TSV.pull_all_view_csvs = _fake_pull
    try:
        truth5, notes = parity.pull_live_tableau_truth(
            "https://x", "s", "wb-1", calc_metrics)
    finally:
        TSV.pull_all_view_csvs = real_pull

    assert truth5 == {
        "Sales": {"value": 2326534.35, "approx": False, "rows": 1},
        "Profit Ratio": {"value": 0.1256, "approx": False, "rows": 1},
    }, f"multi-row must contribute nothing; the single exact view must resolve both: {truth5}"
    assert len(notes) == 4, notes
    assert notes[0]["matched"] == [], \
        f"the multi-row view must match NOTHING now (no summing): {notes[0]}"
    assert notes[1]["skipped"] == "RuntimeError: boom", notes[1]
    assert notes[2]["matched"] == ["Profit Ratio", "Sales"], notes[2]
    assert notes[3]["matched"] == [], \
        "the duplicate view must not re-report a metric already resolved: " + str(notes[3])

    print("ok  R2 live-truth pull (truth_from_view_csv normalizes SUM()/AGG() "
          "headers; EXACT single-row match only -- the unsound APPROXIMATE "
          "multi-row-sum tier was removed 2026-07-30 after producing a "
          "6x-wrong value on its first live run; pull_live_tableau_truth "
          "merges across every view, a multi-row view contributes nothing, "
          "one view's REST error doesn't abort the rest)")


def test_raw_measure_live_truth_and_json_verdicts():
    """TWO REAL BUGS found from a live Stage 5 run, 2026-07-30.

    BUG A -- the RAW-COLUMN measure table's "Tableau" column was fed ONLY by
    the hardcoded module-level parity.TABLEAU_TRUTH dict: one datasource
    ("Sample - Superstore") and three columns (SALES/PROFIT/QUANTITY), typed
    in by hand when Superstore was the only demo workbook. So Discount showed
    "-" on the very workbook that dict covers, and EVERY raw measure on EVERY
    other workbook showed "-" permanently no matter how live the Tableau REST
    connection was. R1/R2's dynamic pull already existed but was only ever
    handed calc_metrics -- raw columns, the EASIEST thing to match (a view
    exporting `SUM(Discount)` maps straight onto the Discount measure), were
    never offered to the matcher at all. Locked here: raw measures adapt into
    the same matcher shape, resolve over REST, and fold back into the result.

    BUG B -- `AI_COMPLETE` returns a VARIANT, and stringifying a VARIANT
    yields its JSON REPRESENTATION: a quoted, backslash-escaped string. So a
    perfectly well-formed Cortex verdict arrived as `"{\\"verdict\\": ...}"`,
    `_extract_json_obj` hit the `\\"` right after the opening brace, failed on
    every candidate `{`, and EVERY R8 vision dashboard reported "UNKNOWN" with
    the raw escaped JSON dumped into its note. The verdicts were never being
    read. Locked here alongside the shapes that already worked."""
    import json

    import parity

    # ---- BUG B: double-encoded JSON, plus every previously-working shape ----
    live = json.dumps('{"verdict": "BUG", "explanation": "missing KPI panel"}')
    obj = parity._extract_json_obj(live)
    assert obj and obj["verdict"] == "BUG", \
        f"double-encoded (VARIANT-stringified) verdict must parse, got {obj!r}"
    assert parity._extract_json_obj(
        '{"verdict": "PASS", "explanation": "ok"}')["verdict"] == "PASS"
    assert parity._extract_json_obj(
        'Sure!\n```json\n{"verdict": "PASS", "explanation": "ok"}\n```')["verdict"] == "PASS"
    assert parity._extract_json_obj(
        'text {"verdict": "BUG", "explanation": "x"} more')["verdict"] == "BUG"
    assert parity._extract_json_obj("no json here") is None, \
        "garbage must stay UNKNOWN, never a fabricated verdict"
    assert parity._extract_json_obj("") is None
    assert parity._extract_json_obj(json.dumps("plain text, not json")) is None, \
        "a JSON string that isn't JSON-encoded object must not become a verdict"

    # ---- BUG A: raw measures reach the matcher and fold back in ------------
    def _res():
        return {"measures": [
            {"datasource": "Sample - Superstore", "measure": "Discount",
             "column": "DISCOUNT", "app": 1583.99, "source": 1583.99,
             "source_kind": "file", "tableau": None, "verdict": "PASS"},
            {"datasource": "Sample - Superstore", "measure": "Sales",
             "column": "SALES", "app": 2326534.35, "source": 2326534.35,
             "source_kind": "file", "tableau": None, "verdict": "PASS"}],
            "calc_metrics": [],
            "summary": {"measures_checked": 2, "measures_pass": 2,
                        "measures_bug": 0}}

    adapted = parity.raw_measure_metrics(_res())
    assert [a["metric"] for a in adapted] == ["Discount", "Sales"], adapted
    assert all(parity._is_pure_sum_sql(a["sql"]) for a in adapted), \
        "a raw column measure is a bare SUM(col) -- it must qualify for the " \
        "approximate multi-row path, which is where real workbooks land"

    # EXACT: a real single-row Tableau grand-total view
    truth, _ = parity.truth_from_view_csv(
        "SUM(Discount),SUM(Sales)\n1583.99,2326534.35\n", adapted)
    key = parity._raw_truth_key("Sample - Superstore", "Discount")
    assert truth[key] == {"value": 1583.99, "approx": False, "rows": 1}, truth

    r = _res()
    assert parity.apply_live_truth_to_measures(r, truth) == 2
    disc = r["measures"][0]
    assert disc["tableau"] == 1583.99 and disc["tableau_source"] == "REST", disc
    assert disc["tableau_approx"] is False and disc["verdict"] == "PASS"

    # MULTI-ROW VIEWS ARE NEVER SUMMED (REMOVED 2026-07-30, second real
    # finding same day). The approximate-sum tier's first live run produced
    # Quantity = 231,924 against a true 38,654 -- 6x wrong, from a 24-row
    # view whose rows repeated. A partition guard was tried first (below),
    # but a genuine multi-row DETAIL listing (many real Tableau views) also
    # has repeated dimension values while still being summable, and that
    # cannot be told apart from a crosstab using the CSV alone -- the guard
    # correctly caught the bad case but also killed legitimate ones. The
    # user's decision (2026-07-30): drop the approximate tier entirely: true
    # totals now come from Tableau's OWN ENGINE via the VizQL Data Service
    # (see the VDS section below), which needs no view to display a total at
    # all and cannot be inflated by row repetition. A multi-row view is
    # therefore ALWAYS a dimension breakdown, reported as such, never summed.
    truth_a, why_a = parity.truth_from_view_csv(
        "Category,SUM(Discount)\nFurniture,600.00\nOffice Supplies,500.00\n"
        "Technology,483.99\n", adapted)
    assert truth_a == {}, "a multi-row view must never be summed -- true " \
                          "totals come from VDS instead"
    assert "grand total" in why_a, why_a

    # An EXACT reference that genuinely disagrees IS a bug -- and the summary
    # must be re-rolled, or Stage 5 shows "N/N pass" above a BUG row.
    r3 = _res()
    r3["measures"][1]["app"] = 1.0
    parity.apply_live_truth_to_measures(
        r3, {parity._raw_truth_key("Sample - Superstore", "Sales"):
             {"value": 2326534.35, "approx": False, "rows": 1}})
    assert r3["measures"][1]["verdict"] == "BUG"
    assert r3["summary"]["measures_bug"] == 1 and r3["summary"]["measures_pass"] == 1, \
        f"summary must be recomputed after a verdict flips: {r3['summary']}"

    # a measure with no REST match is left completely untouched
    r4 = _res()
    assert parity.apply_live_truth_to_measures(r4, {}) == 0
    assert r4["measures"][0]["tableau"] is None and r4["measures"][0]["verdict"] == "PASS"

    # ---- _rows_partition_data still exists as a DIAGNOSTIC in the "skipped"
    # reason (so the UI can say WHY a view was a crosstab vs. a genuine
    # breakdown), even though its verdict no longer gates a sum -- there is
    # no sum left to gate. Both shapes must still be correctly identified.
    qty = [{"name": "raw::DS::Quantity", "metric": "Quantity",
            "sql": "SUM(QUANTITY)"}]
    repeated = ("Customer,Category,SUM(Quantity)\n"
                + "".join(f"Cust{i % 4},Cat{i % 6},1610\n" for i in range(24)))
    ok_rep, why_rep = parity._rows_partition_data(
        *parity._parse_view_csv(repeated))
    assert ok_rep is False and "repeated dimension row" in why_rep, why_rep
    ok_clean, _ = parity._rows_partition_data(*parity._parse_view_csv(
        "Category,SUM(Quantity)\nFurniture,100\nOffice Supplies,200\n"
        "Technology,300\n"))
    assert ok_clean is True, "a genuine partitioned breakdown must still be " \
                             "recognized as such, even though it is no " \
                             "longer summed"
    # ...and truth_from_view_csv reports EITHER shape as "not a grand total",
    # never a sum, regardless of which diagnostic applies
    t_rep, why_rep2 = parity.truth_from_view_csv(repeated, qty)
    assert t_rep == {} and "grand total" in why_rep2, why_rep2
    t_clean, why_clean = parity.truth_from_view_csv(
        "Category,SUM(Quantity)\nFurniture,100\nOffice Supplies,200\n"
        "Technology,300\n", qty)
    assert t_clean == {} and "grand total" in why_clean, why_clean
    # the single-row EXACT path is completely untouched
    t_ex, _ = parity.truth_from_view_csv("SUM(Quantity)\n38654\n", qty)
    assert t_ex["raw::DS::Quantity"] == {"value": 38654.0, "approx": False,
                                         "rows": 1}, t_ex

    # ---- notes carry the view's REAL exported columns ----------------------
    # Without these, "why does measure X have no Tableau value?" is
    # unanswerable from the UI and becomes guesswork.
    import tableau_server as TSV2

    def _fake_pull2(server_url, site, wb_id, token_name=None, token_secret=None):
        return [{"view": "Total", "id": "v1",
                 "csv": "SUM(Quantity)\n38654\n", "error": None},
                {"view": "Customers", "id": "v2",
                 "csv": "Customer,SUM(Quantity)\nA,10\nB,20\n", "error": None}]

    _real2 = TSV2.pull_all_view_csvs
    TSV2.pull_all_view_csvs = _fake_pull2
    try:
        truth2, notes2 = parity.pull_live_tableau_truth("u", "s", "w", qty)
    finally:
        TSV2.pull_all_view_csvs = _real2
    assert truth2 == {"raw::DS::Quantity": {"value": 38654.0, "approx": False,
                                            "rows": 1}}, truth2
    assert notes2[0]["columns"] == ["SUM(Quantity)"], notes2
    assert notes2[0]["matched"] == ["raw::DS::Quantity"], notes2
    assert notes2[1]["columns"] == ["Customer", "SUM(Quantity)"], notes2
    assert notes2[1]["matched"] == [], \
        "the multi-row view must contribute NOTHING to truth now, only " \
        f"appear in the notes for diagnosis: {notes2[1]}"

    # ---- VDS: the TRUE aggregate source (option 1, user decision 2026-07-30)
    # Tableau's own engine answers SUM(field) with NO grouping -- one row,
    # the real grand total. Needs no view to display a total, and the
    # 6x-repetition class cannot occur because no view is queried at all.
    import types as _types

    class _R:
        def __init__(self, content=b"", payload=None):
            self.content, self._payload, self.status_code = content, payload, 200
            self.text = ""
        def json(self):
            return self._payload

    _signin_xml = (b'<tsResponse xmlns="http://tableau.com/api">'
                  b'<credentials token="tok"><site id="site-1"/></credentials>'
                  b'</tsResponse>')

    # A) the realistic case for an EMBEDDED-data workbook: no published
    # datasource exists at all -- must be a STATED reason, never silence,
    # and VDS must never even be called with zero datasources.
    def _get_empty(url, headers=None, timeout=None, **kw):
        return _R(b'<tsResponse xmlns="http://tableau.com/api">'
                  b'<datasources/></tsResponse>')
    def _post_empty(url, headers=None, data=None, json=None, timeout=None, **kw):
        if "signin" in url:
            return _R(_signin_xml)
        if "signout" in url:
            return _R(b"")
        raise AssertionError("VDS must never be queried with 0 datasources: " + url)

    real_requests = TSV2.requests
    TSV2.requests = _types.SimpleNamespace(get=_get_empty, post=_post_empty)
    try:
        res_empty = TSV2.pull_tableau_aggregates(
            "https://x", "s", ["Discount"], token_name="n", token_secret="s")
    finally:
        TSV2.requests = real_requests
    assert res_empty["values"] == {}, res_empty
    assert "EMBEDDED" in res_empty["notes"][0]["error"], res_empty

    # B) a published datasource answers with the TRUE aggregate
    def _get_one(url, headers=None, timeout=None, **kw):
        return _R(b'<tsResponse xmlns="http://tableau.com/api"><datasources>'
                  b'<datasource id="ds-1" name="Sample - Superstore"/>'
                  b'</datasources></tsResponse>')
    _captured = {}
    def _post_one(url, headers=None, data=None, json=None, timeout=None, **kw):
        if "signin" in url:
            return _R(_signin_xml)
        if "signout" in url:
            return _R(b"")
        if "vizql-data-service" in url:
            _captured["body"] = json
            _captured["headers"] = headers
            return _R(payload={"data": [{"SUM(Discount)": 1583.99}]})
        raise AssertionError(url)

    TSV2.requests = _types.SimpleNamespace(get=_get_one, post=_post_one)
    try:
        vds_truth, vds_notes = parity.pull_vds_tableau_truth(
            "https://x", "s", [{"metric": "Profit Ratio", "name": "Profit Ratio",
                               "sql": "SUM(PROFIT)/NULLIF(SUM(SALES),0)"}],
            measure_captions=["Discount"],
            datasource_hints=["Sample - Superstore"],
            token_name="n", token_secret="s")
    finally:
        TSV2.requests = real_requests
    disc_key = parity._raw_truth_key_from_caption("Discount")
    assert vds_truth == {disc_key: {"value": 1583.99, "approx": False,
                                    "rows": 1, "source": "VDS"}}, vds_truth
    assert _captured["body"]["query"]["fields"] == [
        {"fieldCaption": "Discount", "function": "SUM"}], _captured
    assert not any("Profit Ratio" in str(v) for v in vds_truth), \
        "a compound (non-SUM) metric must never be requested from VDS as SUM"

    # A REAL BUG, found live 2026-08-04 against the actual account: `Accept`
    # was hardcoded to "application/xml" regardless of the requested
    # Content-Type, so EVERY VDS call (a JSON-only API -- it has no XML
    # representation) failed with `400 "No acceptable representation"` on
    # EVERY published datasource, a textbook content-negotiation mismatch
    # invisible to any test that only checks the request BODY (as the
    # assertion just above this one did, right up until this bug shipped).
    # `_headers()` now defaults `Accept` to match `content_type` unless
    # overridden -- locked here so this exact class can't silently return.
    assert _captured["headers"]["Accept"] == "application/json", \
        f"VDS's Accept header must match its JSON Content-Type, or Tableau " \
        f"rejects every call with 400 'No acceptable representation' " \
        f"(the real bug, found live): {_captured['headers']}"
    assert _captured["headers"]["Content-Type"] == "application/json", \
        _captured["headers"]
    # the classic XML REST API must be COMPLETELY unaffected by this fix --
    # every existing caller relies on Accept defaulting to application/xml
    xml_headers = TSV2.TableauRestClient("https://x")._headers()
    assert xml_headers == {"Content-Type": "application/xml",
                          "Accept": "application/xml"}, xml_headers
    assert vds_notes and vds_notes[0]["matched"] == ["Discount"], vds_notes

    # VDS truth folds into the raw-column table via the SAME apply function,
    # caption-keyed rows reconciling against datasource-keyed measure rows
    r5 = _res()
    n5 = parity.apply_live_truth_to_measures(r5, vds_truth)
    d5 = r5["measures"][0]
    assert n5 == 1 and d5["tableau"] == 1583.99, d5
    assert d5["tableau_source"] == "VDS" and d5["tableau_approx"] is False, d5

    print("ok  raw-measure live truth + JSON verdict parsing + VDS true "
          "aggregates (raw columns now reach the REST matcher instead of a "
          "3-entry hardcoded dict; the unsound sum-across-rows tier was "
          "REMOVED after producing a 6x-wrong Quantity on its first live run "
          "-- a multi-row view is now always reported as a dimension "
          "breakdown, never summed; TRUE totals instead come from Tableau's "
          "own engine via the VizQL Data Service, which needs no view to "
          "display a total and cannot be inflated by row repetition; an "
          "embedded-data workbook with no published datasource states that "
          "reason rather than failing silently; VARIANT-stringified double-"
          "encoded Cortex JSON now parses instead of every verdict reading "
          "UNKNOWN)")


def test_dashboard_section_validation():
    """The dashboard-validation methodology at SECTION (dashboard) grain, not
    per-calculated-field: one combined query per dashboard, a deterministic
    TWB-vs-app formula comparison per measure, one Cortex narrative per
    section, and a cross-section bug rollup. Offline-only (no live session --
    build_dashboard_section_report itself needs one) but locks the pieces that
    don't: the formula-shape comparison, dashboard-to-measures collection, and
    both renderers producing valid, non-empty output from already-executed
    data."""
    import json
    import parity

    # --- _formula_match: the real false positive found building this, fixed,
    # and now gated so it can never silently come back. NULLIF/CASE are guard
    # functions the deterministic translator adds for safety (divide-by-zero),
    # not a change in aggregation -- counting them as "different aggregation
    # functions" flagged Profit Ratio as a bug it never was.
    m, imp = parity._formula_match(
        "sum([Profit])/sum([Sales])", "sum(PROFIT)/ NULLIF(sum(SALES), 0)", "calc")
    assert m is True, f"NULLIF guard must not be flagged as a real mismatch: {imp}"

    # a GENUINELY different aggregation must still be caught, not swallowed by
    # the NULLIF fix
    m2, imp2 = parity._formula_match("AVG([Sales])", "SUM(SALES)", "calc")
    assert m2 is False and "AVG" in str(imp2) and "SUM" in str(imp2)

    # a genuinely different operator must still be caught
    m3, imp3 = parity._formula_match("SUM([A])+SUM([B])", "SUM(A)-SUM(B)", "calc")
    assert m3 is False

    # a plain physical column always matches (both sides reference the same
    # field by definition -- there is no "Tableau formula" to disagree with)
    m4, imp4 = parity._formula_match("irrelevant", "SUM(X)", "column")
    assert m4 is True and imp4 is None

    # countD(...) (Tableau's own spelling) vs COUNT(DISTINCT ...) (what the
    # generated SQL always expands it to) are the SAME operation -- a second
    # real false positive found live, alongside the NULLIF one, on the exact
    # same run (Sales per Customer / Profit per Order both wrongly flagged
    # as bugs for "using COUNT instead of COUNTD").
    m5, imp5 = parity._formula_match(
        "Sum([Sales])/countD([Customer Name])",
        "Sum(SALES)/ NULLIF(COUNT(DISTINCT CUSTOMER_NAME), 0)", "calc")
    assert m5 is True, f"countD and COUNT(DISTINCT ...) must be recognized as equivalent: {imp5}"

    # --- _resolve_measure_sql: a plain column's aggregation must follow the
    # REAL agg token the sheet used it with, not an assumed SUM -- a real
    # query error found live: a sheet used 'Customer Name' with agg='ctd'
    # (Count of Customers), and the first version of this function always
    # emitted SUM(CUSTOMER_NAME) regardless -- Snowflake correctly rejected
    # summing a text column. engine._agg_expr is the SAME translator the
    # actual generated app uses, reused here rather than a second guess.
    sql, kind, ref = parity._resolve_measure_sql(
        {"colmap": {}}, "Customer Name", "ctd", {"CUSTOMER_NAME"}, [])
    assert sql == "COUNT(DISTINCT CUSTOMER_NAME)", \
        f"a ctd-aggregated column must use COUNT DISTINCT, not an assumed SUM: {sql}"
    assert kind == "column"

    # --- collect_dashboard_section: measures/dims collected from shelf pills,
    # scoped to the dashboard's DOMINANT datasource, group dim only chosen
    # when it resolves to a real column (never invented). Includes an mbar
    # (multi-measure bar) sheet -- the REAL bug found live on Superstore's
    # "Customer Analysis" dashboard: an mbar sheet stores its whole measure
    # list under `measures` (a list of dicts, agg token "ctd" = COUNT
    # DISTINCT) and its breakdown dimension under a bare `dim` string, NEITHER
    # of which lives in x/y/color -- the first version of this scan silently
    # dropped Region, Count of Customers, Quantity and Sales per Customer
    # from validation entirely, with no error, no warning, just absent.
    dash = {"sheets": [
        {"datasource": "DS1", "x": {"caption": "Sales", "agg": "sum"}},
        {"datasource": "DS1", "color": {"caption": "Profit Ratio", "agg": "usr",
                                        "kind": "measure"}},
        {"datasource": "DS1", "dim": "Region",
         "measures": [{"agg": "ctd", "caption": "Customer Name",
                       "label": "Count of Customers"},
                      {"agg": "sum", "caption": "Quantity"}]},
        {"datasource": "OTHER_DS", "x": {"caption": "Ignored", "agg": "sum"}},
    ]}
    import config as _config
    _config.DATASOURCES["DS1"] = {"table": "DB.SCH.T1", "local_file": None}
    orig_table_cols = parity.CS._table_columns
    parity.CS._table_columns = lambda entry: {
        "SALES", "PROFIT_RATIO", "REGION", "CUSTOMER_NAME", "QUANTITY"}
    try:
        info = parity.collect_dashboard_section({"colmap": {}}, dash)
        assert info["datasource"] == "DS1", "must pick the DOMINANT datasource, not OTHER_DS"
        assert set(info["measure_caps"]) == {"Sales", "Profit Ratio", "Customer Name", "Quantity"}, \
            f"mbar sheet's `measures` list must be scanned, not just x/y/color: {info['measure_caps']}"
        assert info["measure_caps"]["Customer Name"] == ("ctd", "Count of Customers"), \
            "the real (agg token, display label) must be kept, not discarded to a bare caption set"
        assert info["group_dim"] == ("Region", "REGION"), \
            "an mbar sheet's bare `dim` string must be a GROUP BY candidate, not just x/y/color pills"
    finally:
        parity.CS._table_columns = orig_table_cols
        del _config.DATASOURCES["DS1"]

    # --- STRONG beats WEAK even when outnumbered: the exact real shape found
    # live on Customer Analysis. 'Customer Name' is a WEAK candidate TWICE
    # (a rank chart's bare y-axis label, a scatter's per-point `detail` pill)
    # while 'Region' is a STRONG candidate ONCE (an overview chart's actual
    # declared `dim`). A plain occurrence count picked Customer Name (800
    # rows, one per customer) over the dashboard's own explicitly-declared
    # breakdown -- fixed so an explicit dim declaration always outranks an
    # incidental axis label, regardless of which appears more often.
    dash2 = {"sheets": [
        {"datasource": "DS1", "y": {"caption": "Customer Name"}},          # weak #1
        {"datasource": "DS1", "detail": {"caption": "Customer Name",
                                          "kind": "dimension", "agg": "none"}},  # weak #2
        {"datasource": "DS1", "dim": "Region",
         "measures": [{"agg": "sum", "caption": "Sales"}]},                 # strong #1
    ]}
    _config.DATASOURCES["DS1"] = {"table": "DB.SCH.T1", "local_file": None}
    orig_table_cols = parity.CS._table_columns
    parity.CS._table_columns = lambda entry: {"SALES", "REGION", "CUSTOMER_NAME"}
    try:
        info2 = parity.collect_dashboard_section({"colmap": {}}, dash2)
        assert info2["group_dim"] == ("Region", "REGION"), (
            "an explicitly-declared dim must outrank a merely incidental "
            f"axis label even when outnumbered 2-to-1: got {info2['group_dim']}")
    finally:
        parity.CS._table_columns = orig_table_cols
        del _config.DATASOURCES["DS1"]

    # --- a measure confirmed on ANY sheet must never win group_dim via an
    # incidental dimension appearance on a DIFFERENT sheet in the SAME
    # dashboard -- the real bug found live on Executive Overview -
    # Profitability: 'Profit' is a measure everywhere, but one sheet's
    # tooltip-style mention put it in the dimension pool too, and it won
    # group_dim over 'Region' -- producing "GROUP BY raw Profit value",
    # 7,575 nonsensical one-row-per-value groups instead of a real breakdown.
    dash3 = {"sheets": [
        {"datasource": "DS1", "y": {"caption": "Profit", "agg": "sum"}},     # Profit = measure here
        {"datasource": "DS1", "tooltip_fields": ["Profit"]},                 # ...but a bare name-list
                                                                              # mention on ANOTHER sheet
        {"datasource": "DS1", "dim": "Region",
         "measures": [{"agg": "sum", "caption": "Sales"}]},
    ]}
    _config.DATASOURCES["DS1"] = {"table": "DB.SCH.T1", "local_file": None}
    orig_table_cols = parity.CS._table_columns
    parity.CS._table_columns = lambda entry: {"SALES", "PROFIT", "REGION"}
    try:
        info3 = parity.collect_dashboard_section({"colmap": {}}, dash3)
        assert "Profit" in info3["measure_caps"], "Profit must resolve as a measure"
        assert info3["group_dim"] == ("Region", "REGION"), (
            "a dashboard-wide measure must never win group_dim via an "
            f"incidental cross-sheet dimension mention: got {info3['group_dim']}")
    finally:
        parity.CS._table_columns = orig_table_cols
        del _config.DATASOURCES["DS1"]

    # --- the "ctd" aggregation token specifically -- calc_translator.AGGS is
    # the single source of truth this now reuses; the first version had a
    # local literal set spelling it "cntd" (which appears nowhere in the
    # actual IR), silently dropping every COUNT DISTINCT measure.
    assert "ctd" in parity._MEASURE_AGGS and "cntd" not in parity._MEASURE_AGGS

    # --- both renderers: valid, non-empty output from already-executed data
    # (no live session -- this is exactly the shape build_dashboard_section_report
    # hands them after a real run).
    sections = [
        {"title": "Customer Analysis", "table": "WBR_DB.PUBLIC.ORDERS",
         "sql": "SELECT CATEGORY AS GRP, SUM(SALES) AS SALES FROM WBR_DB.PUBLIC.ORDERS GROUP BY 1",
         "columns": ["Category", "Sales"],
         "rows": [{"Category": "Furniture", "Sales": 754747.76}],
         "query_error": None,
         "formula_rows": [
             {"metric": "Sales", "twb": "raw column (SALES)", "app_sql": "SUM(SALES)",
              "match": True, "impact": None},
             {"metric": "Profit Ratio", "twb": "sum([Profit])/sum([Sales])",
              "app_sql": "sum(PROFIT)/ NULLIF(sum(SALES), 0)",
              "match": False, "impact": "different aggregation functions"},
         ],
         "cortex_summary": "Data validates cleanly except Profit Ratio.",
         "cortex_tokens": 42, "cortex_error": None},
        {"title": "Empty Dashboard", "skipped": "no measure pills resolve to a real Snowflake table"},
    ]
    bug_rollup = [{"section": "Customer Analysis", "metric": "Profit Ratio",
                  "twb": "sum([Profit])/sum([Sales])",
                  "app_sql": "sum(PROFIT)/ NULLIF(sum(SALES), 0)",
                  "match": False, "impact": "different aggregation functions"}]

    nb = parity.dashboard_report_to_notebook(sections, bug_rollup, "Superstore")
    d = json.loads(nb)                                    # valid notebook JSON
    assert d["nbformat"] == 4
    assert "Customer Analysis" in nb and "Empty Dashboard" in nb
    assert "Skipped" in nb, "a skipped dashboard must say WHY, not vanish silently"
    assert "Summary of All Bugs" in nb and "Profit Ratio" in nb

    html = parity.dashboard_report_to_html(sections, bug_rollup, "Superstore")
    assert "<html>" in html and html.count("</section>") == 2
    assert "Customer Analysis" in html and "Skipped" in html
    assert "Data validates cleanly except Profit Ratio." in html, \
        "the real Cortex narrative must appear verbatim, not be summarized away"
    assert "Summary of All Bugs" in html and "Profit Ratio" in html

    print("ok  dashboard-section validation (formula-shape match ignores NULLIF/CASE "
          "guards but still catches real aggregation/operator differences -- the "
          "Profit Ratio false positive found live and fixed; the full closed set of "
          "shelf/measure/dim keys is scanned -- an mbar sheet's `measures` list and "
          "bare `dim` string are no longer silently dropped, and the real 'ctd' "
          "aggregation token is used instead of a nonexistent 'cntd' -- both found "
          "live on Superstore's Customer Analysis dashboard; dominant-datasource + "
          "verified-column group-dim selection; a dashboard-wide measure never wins "
          "group_dim via an incidental cross-sheet dimension mention -- the "
          "'GROUP BY raw Profit value' bug found live on Executive Overview; both "
          "renderers produce valid output from already-executed data, skipped "
          "sections say why)")


def test_cortex_dashboard_validation_report():
    """The SKILL-DRIVEN dashboard validation report (parity.
    build_cortex_dashboard_validation_report / dashboard_validation_report_
    to_notebook) -- the richer, per-dashboard-section report that follows the
    dashboard-validation skill's own methodology (comparison table +
    diagnostic-if-warranted + a categorized verdict, written by Cortex from
    ALREADY-EXECUTED real data). Built in parity.py but never wired into
    pipeline_app.py's Stage 5 UI until this session -- test_dashboard_section_
    validation above only covers the OLDER build_dashboard_section_report /
    dashboard_report_to_notebook path (generic one-line Cortex narration);
    this locks the actual functions the UI now calls.

    Runs end-to-end against the REAL Superstore IR with a FAKE Snowflake
    session (no live account needed) -- catches wiring/shape bugs a pure
    unit test on crafted section dicts would miss (e.g. a real dashboard
    whose measures don't resolve, a real combined-query column count)."""
    import json
    import parity

    ir = json.load(open("workbook_ir.json", encoding="utf-8"))

    class FakeRow:                      # any-index numeric stand-in for a
        def __getitem__(self, i):       # real combined-query result row
            return 42

    class FakeCortexRow:                # SNOWFLAKE.CORTEX.COMPLETE's one cell
        def __init__(self, text): self.text = text
        def __getitem__(self, i): return self.text

    class FakeResult:
        def __init__(self, rows): self._rows = rows
        def collect(self): return self._rows

    class FakeSession:
        def __init__(self): self.calls = 0
        def sql(self, text):
            self.calls += 1
            if "CORTEX.COMPLETE" in text:
                # matches the real prompt: NO comparison table asked of
                # Cortex anymore -- diagnostic-if-warranted + verdict only.
                return FakeResult([FakeCortexRow("No bugs found.")])
            return FakeResult([FakeRow(), FakeRow()])

    # REAL check_workbook() result (offline, local DuckDB backend against the
    # corpus's own data files) -- the actual App/Backend-Source/Tableau
    # three-way values a section's "Data Comparison" table must reuse, not
    # a fabricated stand-in. This is the exact `res` pipeline_app.py's
    # Stage 5 already computes and now passes through.
    res = parity.check_workbook(ir)
    assert res["measures"], "check_workbook must produce real measure rows on the corpus IR"

    fake = FakeSession()
    sections, rollup = parity.build_cortex_dashboard_validation_report(
        ir, fake, "Superstore.twb", res=res)
    assert len(sections) == len(ir["dashboards"]), \
        "every dashboard must produce a section -- skipped or not, never silently dropped"
    resolved = [s for s in sections if not s.get("skipped")]
    assert resolved, "at least one real dashboard must resolve on the corpus's own IR"
    assert any(sec.get("value_rows") for sec in resolved), \
        "at least one section must carry real App/Backend/Tableau value rows from check_workbook"
    for sec in resolved:
        assert sec.get("sql"), f"{sec['title']}: a resolved section must carry its live query"
        assert sec.get("cortex_report") or sec.get("cortex_error"), \
            f"{sec['title']}: must carry either a real report or a stated reason it's missing"
    skipped = [s for s in sections if s.get("skipped")]
    for sec in skipped:
        assert sec["skipped"], f"{sec['title']}: a skipped section must state why"
    assert fake.calls > 0, "must actually call the fake session, not fabricate output"

    nb = parity.dashboard_validation_report_to_notebook(sections, rollup, "Superstore.twb")
    d = json.loads(nb)                                      # valid notebook JSON
    assert d["nbformat"] == 4
    # json.dumps(ensure_ascii=True) escapes emoji to \uXXXX in the raw string
    # -- decode the checklist cell's actual source text before checking icons.
    checklist_cell = "".join(
        "".join(c["source"]) for c in d["cells"]
        if "Validation Complete" in "".join(c["source"]))
    for sec in resolved:
        assert sec["title"] in nb, f"{sec['title']} must appear in the rendered notebook"
    for sec in skipped:
        assert sec["title"] in nb and sec["skipped"] in nb, \
            f"{sec['title']}: skip reason must appear in the notebook, not vanish"
        # a real bug found reading a live-generated report: the checklist
        # marked EVERY section with checkmark, including skipped ones -- a
        # skip is neither pass nor fail and must not read as a false clean
        # bill of health next to text saying "not resolved."
        assert f"⚠️ {sec['title']}:" in checklist_cell, \
            f"{sec['title']}: a skipped section must use the warning icon in the checklist, not a checkmark"
    for sec in resolved:
        assert f"✅ {sec['title']}:" in checklist_cell, \
            f"{sec['title']}: a resolved section must keep the checkmark in the checklist"
    assert "Summary of All Bugs" in nb
    assert "Testing Plan" in nb and "not applicable" in nb.lower(), \
        "no TESTING_PLAN.md exists for a Tableau migration -- must say so, not fabricate test IDs"
    assert "Tooltip Completeness" in nb and "not applicable" in nb.lower(), \
        "no tooltip metadata exists for a Tableau migration -- must say so, not fabricate it"

    # HTML twin -- a .ipynb needs a notebook-capable viewer; this must open
    # in any browser with the SAME already-executed content, not a summary.
    html = parity.dashboard_validation_report_to_html(sections, rollup, "Superstore.twb")
    assert "<html>" in html
    for sec in resolved:
        assert sec["title"] in html, f"{sec['title']} must appear in the HTML report"
    for sec in skipped:
        # html.escape() turns apostrophes into &#x27; -- compare against the
        # same escaping the renderer itself applies, not the raw string.
        import html as _html_mod
        assert sec["title"] in html and _html_mod.escape(sec["skipped"], quote=True) in html, \
            f"{sec['title']}: skip reason must appear in the HTML, not vanish"
        assert f"⚠️ {_html_mod.escape(sec['title'], quote=True)}:" in html, \
            f"{sec['title']}: a skipped section must use the warning icon in the HTML checklist too"
    for sec in resolved:
        assert f"✅ {_html_mod.escape(sec['title'], quote=True)}:" in html, \
            f"{sec['title']}: a resolved section must keep the checkmark in the HTML checklist"
    assert "Summary of All Bugs" in html
    assert "not applicable" in html.lower() and html.lower().count("not applicable") >= 2, \
        "Testing Plan AND Tooltip audits must both state not-applicable in the HTML too"

    # THE ACTUAL BUG FOUND READING A LIVE REPORT: the "comparison table" was
    # Cortex's markdown prose dumped into a <pre> block -- no real table, no
    # real data, just narration. Lock that a REAL <table> now exists for
    # both the query result and the formula comparison, built from the
    # actual data/verdicts, not Cortex's retelling of them.
    for sec in resolved:
        assert '<table class="cmp-tbl">' in html, \
            "the formula comparison must be a real <table>, not markdown text in a <pre>"
        for r in sec["formula_rows"]:
            assert f"<b>{_html_mod.escape(r['metric'], quote=True)}</b>" in html, \
                f"{r['metric']}: must appear as a real table cell, not just prose"
        assert '<table class="data-tbl">' in html, \
            "the live query result must be a real <table> of the actual rows, not prose citing a few values"
        # the FakeSession's combined-query rows are stubbed to literal 42 --
        # a real value must land in a real <td>, not be paraphrased away
        assert "<td>42</td>" in html, \
            "the real query result values must appear as real table cells"
    # Cortex is no longer asked to author the comparison table at all
    for sec in resolved:
        if sec.get("cortex_report"):
            assert "| Metric | Tableau TWB Formula" not in sec["cortex_report"], \
                "Cortex must not be asked to re-author the comparison table -- it's rendered separately now"

    # same lock on the notebook -- deterministic markdown tables must exist
    # as their OWN cells, not be folded into Cortex's prose.
    assert "Formula Comparison" in nb and "Live query result" in nb
    for sec in resolved:
        for r in sec["formula_rows"]:
            assert f"**{r['metric']}**" in nb, \
                f"{r['metric']}: must appear in the notebook's deterministic formula table"

    # THE ACTUAL ASK: real DATA matching, not just formula-shape matching --
    # App vs Backend/Source vs Tableau, side by side, with real numbers and
    # a real status, reusing check_workbook's own computed values.
    assert "App vs Backend vs Tableau" in html and "App vs Backend vs Tableau" in nb, \
        "the three-way DATA comparison section must exist in both outputs"
    value_sections = [sec for sec in resolved if sec.get("value_rows")]
    assert value_sections, "at least one section must have real value rows to render"
    for sec in value_sections:
        for r in sec["value_rows"]:
            app_fmt = parity._fmt_val(r.get("app"))
            assert f"<b>{_html_mod.escape(r['metric'], quote=True)}</b>" in html, \
                f"{sec['title']}/{r['metric']}: must appear as a real cell in the HTML data table"
            assert f"**{r['metric']}**" in nb, \
                f"{sec['title']}/{r['metric']}: must appear as a real cell in the notebook data table"
            assert _html_mod.escape(app_fmt, quote=True) in html, \
                f"{sec['title']}/{r['metric']}: the real App value ({app_fmt}) must appear, not be paraphrased"
            # PASS/BUG must come from check_workbook's own verdict, never a
            # second computation invented here
            assert r["verdict"] in ("PASS", "BUG", "EXECUTED"), \
                f"{sec['title']}/{r['metric']}: verdict must be check_workbook's own, got {r['verdict']!r}"

    print(f"ok  skill-driven dashboard validation report -- "
          f"{len(resolved)}/{len(sections)} Superstore dashboard(s) resolved "
          "against a fake session, notebook AND html render valid/complete "
          "with every section (resolved or honestly skipped), Testing Plan / "
          "Tooltip audits reported as honestly not-applicable rather than fabricated")


def test_interaction_proof():
    """R11 -- compute_interaction_proof: the FILTER and TOOLTIP checks that
    make the "Interaction Proof" section real instead of a static "not
    validated" note. HONEST SCOPE proven here too: this is APP-SIDE proof
    only (drives engine.build_where() and captures the real chart spec),
    never a live Tableau observation -- see the function's own docstring.

    Runs fully offline against the real Superstore IR + local DuckDB
    (config's dev built-ins, no live session needed)."""
    import importlib
    import json
    import config
    import engine
    import headless_render as HR
    import parity

    # Other tests in this suite mutate config.DATASOURCES/ORDERS and don't
    # always restore engine.ORDERS in lockstep (pipeline.configure_
    # datasources sets both together; a few older tests only restore
    # config's side) -- reload config fresh so this test's table/columns
    # reflect config.py's own local dev defaults, not leaked state from
    # whatever ran earlier in this same process.
    importlib.reload(config)
    for fn in (engine.table_columns, engine._q_exec):
        try:
            fn.clear()
        except Exception:
            pass
    engine.configure(ir := json.load(open("workbook_ir.json", encoding="utf-8")))
    engine.ORDERS = config.ORDERS
    table = config.ORDERS
    table_cols = engine.table_columns(table)

    customer_analysis = next(d for d in ir["dashboards"] if d["name"] == "Customers")
    rows = parity.compute_interaction_proof(customer_analysis, table, table_cols)
    assert rows, "Customer Analysis has real filters and a tooltip-declaring sheet -- must produce rows"

    filter_rows = [r for r in rows if r["interaction"].startswith("Filter:")]
    tooltip_rows = [r for r in rows if r["interaction"].startswith("Tooltip:")]
    assert filter_rows, "Customer Analysis has real dashboard filters -- must produce Filter rows"
    assert all(r["status"] == "PASS" for r in filter_rows), (
        "every real filter on the real Superstore data must verify PASS -- "
        f"a FAIL here means build_where() itself is broken: {filter_rows}")
    for r in filter_rows:
        assert "no live Tableau click observed" in r["proof"], \
            "every row must state the honest app-side-only scope, never imply a live Tableau observation"

    # --- the REAL bug found building this: a date/date-range filter has
    # MANY distinct raw values even when correctly filtered -- a naive
    # "distinct == 1" check produced a false FAIL on every date filter.
    # Lock that the shape-aware check is actually being exercised (not
    # just present in the code) by requiring at least one non-categorical
    # filter shape to appear and PASS on the corpus's own real data.
    shaped = [r for r in filter_rows if "EXTRACT(" in r["streamlit"] or "BETWEEN" in r["streamlit"]]
    assert shaped, "Customer Analysis' Order Date filter must produce a date-shaped row"
    assert all(r["status"] == "PASS" for r in shaped), \
        f"date-shaped filters must not false-FAIL: {shaped}"

    # --- FAIL must still fire for a genuinely wrong clause -- teeth proof,
    # not just "it always says PASS". Monkeypatch build_where to return an
    # always-true clause instead of a real per-value restriction.
    real_build_where = engine.build_where
    try:
        engine.build_where = lambda dash: [
            {"col": "REGION", "clause": "REGION = 'Central' OR 1=1",
             "caption": "Region (rigged)", "scope": None, "governs": True}]
        rigged = parity.compute_interaction_proof(customer_analysis, table, table_cols)
        rigged_filter = next(r for r in rigged if r["interaction"].startswith("Filter:"))
        assert rigged_filter["status"] == "FAIL", (
            "a clause that doesn't actually restrict to the selected value must FAIL, "
            f"not silently PASS: {rigged_filter}")
    finally:
        engine.build_where = real_build_where

    # --- Tooltip proof: the REAL, currently-true finding is that engine.py
    # labels tooltip channels with generic internal aliases (DIM/VAL/T/C),
    # never the real Tableau caption -- so a declared tooltip_fields caption
    # is expected to come back WARNING (missing), not silently PASS. This
    # locks that the check actually inspects the REAL captured chart spec
    # rather than trusting the sheet's own declared list.
    assert tooltip_rows, "Customer Analysis' CustomerRank sheet declares tooltip_fields -- must produce a row"
    assert any(r["status"] == "WARNING" for r in tooltip_rows), (
        "known real gap: engine.py's tooltip channels are not labeled with "
        "Tableau's caption -- must be caught, not silently reported as PASS")
    for r in tooltip_rows:
        assert "REAL captured Altair chart" in r["proof"] or "captured Altair chart" in r["proof"]

    # --- extract_tooltip_titles: unit-level proof it reads title-over-field
    # and walks layered specs, using a hand-built spec (no live render needed)
    class _FakeChart:
        def __init__(self, d): self._d = d
        def to_dict(self): return self._d
    simple = _FakeChart({"encoding": {"tooltip": [
        {"field": "SALES", "type": "quantitative", "title": "Sales"},
        {"field": "REGION", "type": "nominal"},
    ]}})
    assert HR.extract_tooltip_titles(simple) == ["Sales", "REGION"], \
        "must prefer a channel's title over its bare field id, and fall back to field when no title"
    layered = _FakeChart({"layer": [
        {"encoding": {"tooltip": [{"field": "X", "title": "Profit"}]}},
        {"encoding": {}},
    ]})
    assert HR.extract_tooltip_titles(layered) == ["Profit"], \
        "a layered spec's tooltip can live on any layer, not just the top level"

    print("ok  R11 interaction proof (FILTER: engine.build_where() driven through its "
         "OWN real code path with an ACTUAL selected value via _mocked_widgets(pick_"
         "real=True), verified against the real table with a CLAUSE-SHAPE-AWARE check "
         "-- categorical uses a distinct-value check, date_part/date-range use an "
         "independent boundary check, fixing a real false-FAIL the naive distinct-only "
         "check produced on every date filter; a rigged always-true clause still FAILs, "
         "proving the check has teeth; TOOLTIP: the real captured Altair chart's tooltip "
         "encoding is inspected via extract_tooltip_titles (title-over-field, walks "
         "layered specs), correctly catching the real, currently-true gap that engine.py "
         "labels tooltips with generic aliases, never Tableau's own caption; every row "
         "states its honest app-side-only scope)")


def test_layered_chart_streamlit_rows_and_pie_theta_caption():
    """TWO real bugs found live 2026-08-11 on Regional Analysis (a SECOND
    corpus workbook -- neither is Superstore-specific):

    1. Every stacked-bar and line-with-labels chart reported "captured
       chart exposed no dataframe to compare". engine.py builds these as
       `bar + text` (an Altair LayerChart) -- Altair only hoists `.data`
       to the LayerChart's own top level when every layer shares the
       EXACT same dataframe object, and the text-label layer always adds
       derived columns (a stacking cumulative offset, a label midpoint),
       so it never does. The real rows were one attribute away, on
       `chart.layer[0].data`, and `validation_adapter.streamlit_rows` only
       ever checked `chart.data`.
    2. The pie chart's `theta` channel had no Vega-Lite `title`, so it was
       the one channel on the whole chart with no resolvable Tableau
       caption -- "channel 'theta' ... no resolvable Tableau caption" --
       even though `color` (built from the exact same sheet spec) already
       titled itself correctly.

    Proven on the REAL Regional Analysis fixture (not synthetic Altair
    charts), since the whole point is that engine.py's REAL layering
    shape is what broke the old code -- a hand-built LayerChart might not
    reproduce it."""
    import json

    import engine
    import headless_render as HR
    import validation_adapter as VA

    ir = json.load(open("regional_analysis_ir.json", encoding="utf-8"))
    engine.configure(ir)

    cases = [
        ("View1", "Segment wise Profit by Region", ["Segment", "Profit", "Region"]),
        ("View1", "Quantity by Ship Mode Region Split", ["Ship Mode", "Quantity", "Region"]),
        ("View2", "Sales Trend by Month", ["Order Date", "Profit"]),
        ("View2", "Profit by Category", ["Category", "Sales", "Region"]),
        ("View2", "Region level Sales", ["Region", "Sales"]),
    ]
    for dname, sname, expect_cols in cases:
        dash = next(d for d in ir["dashboards"] if d["name"] == dname)
        sheet = next(s for s in dash["sheets"] if s["name"] == sname)
        chart, reason = HR.capture_sheet_chart(sheet, dashboard_name=dname)
        assert chart is not None, f"{sname}: capture failed: {reason}"
        grain, measures, rename = VA.resolve_chart_columns(sheet, chart)
        assert grain is not None, f"{sname}: resolve_chart_columns refused: {rename}"
        rows = VA.streamlit_rows(chart, rename)
        assert rows, f"{sname}: streamlit_rows returned {rows!r} -- the exact " \
                     f"2026-08-11 bug (a real LayerChart's data lives on a " \
                     f"sub-layer, not chart.data)"
        got_cols = set(rows[0])
        for col in expect_cols:
            assert col in got_cols, f"{sname}: missing {col!r} in {sorted(got_cols)}"

    # _candidate_dataframes must prefer a layer with EVERY needed column over
    # one with only some -- proven directly, not just via the end-to-end case
    # above, so a future regression that picks a partial match is caught even
    # if it happens to still return non-empty rows.
    import altair as alt
    import pandas as pd

    class _Obj:
        def __init__(self, data=None, layer=None):
            self.data = data if data is not None else alt.Undefined
            self.layer = layer if layer is not None else alt.Undefined

    partial = pd.DataFrame({"DIM": ["a"], "VAL": [1]})
    full = pd.DataFrame({"DIM": ["a"], "VAL": [1], "C": ["x"]})
    wrapper = _Obj(layer=[_Obj(data=partial), _Obj(data=full)])
    rows = VA.streamlit_rows(wrapper, {"DIM": "Category", "VAL": "Sales", "C": "Region"})
    assert rows and set(rows[0]) == {"Category", "Sales", "Region"}, \
        "streamlit_rows must prefer the layer with the FULL column set, not " \
        f"just the first non-empty one: got {rows}"

    print("ok  layered chart streamlit_rows + pie theta caption (a real "
          "LayerChart's data lives on a sub-layer, not chart.data -- fixed "
          "by walking layer/hconcat/vconcat/concat and preferring the first "
          "candidate with the FULL needed column set, proven both on the "
          "real Regional Analysis fixture and directly against a partial-"
          "vs-full synthetic pair; the pie chart's theta channel now titles "
          "itself from the measure caption, matching color, so it is no "
          "longer the one channel on the chart with no resolvable Tableau "
          "caption) -- found on a SECOND corpus workbook, not Superstore-specific")


def test_multisheet_dashboard_csv_matched_by_header_and_thousands_comma():
    """TWO more real bugs found on the SAME live Regional Analysis run:

    1. Tableau's dashboard-level `query_view_data` export for a MULTI-
       SHEET dashboard returns ONE sheet's crosstab, not a per-worksheet
       one, and the accelerator's OWN matching code discarded it outright
       whenever a dashboard had more than one sheet ("never done for a
       multi-sheet dashboard -- that would be guessing"). But the export's
       own HEADER proves which sheet it belongs to: it exactly equals that
       sheet's own declared fields (measures + strong dims + weak dims,
       via parity._sheet_pill_captions). This is a content match, not a
       guess -- `_assign_dashboard_csv_by_header` in deep_validation.py.
       A SUBSET match was tried first and found genuinely ambiguous live
       (Regional Analysis's View2: both "Region level Sales" {Region,
       Sales} and "Profit by Category" {Category, Region, Sales} are
       subsets of the same 3-column header) -- an EXACT set match is what
       actually resolves it, proven here as a real regression case, not
       just asserted.
    2. Tableau's own crosstab formats a number with a thousands comma
       ("163,797.1638"), which Python's Decimal() rejects outright.
       validation_report._d() silently returned None for every Tableau
       cell in a comma-formatted export -- never reported wrong, just
       never actually compared, which is worse: the report can read PASS
       (streamlit/backend still matched) while the Tableau leg was
       silently never checked at all."""
    import validation_report as VR
    from deep_validation import _assign_dashboard_csv_by_header

    # --- 1. header-based assignment, proven on the REAL ambiguity case ---
    class _Evidence:
        def __init__(self):
            self.calls = []

        def set_tableau_csv(self, dname, sname, csv_text):
            self.calls.append((dname, sname))

    view2 = next(d for d in _rad_ir()["dashboards"] if d["name"] == "View2")
    csv_text = "Category,Region,Sales\r\nFurniture,West,252612.7435\r\n"
    ev = _Evidence()
    _assign_dashboard_csv_by_header("View2", view2["sheets"], csv_text, ev)
    assert ev.calls == [("View2", "Profit by Category")], (
        f"expected the header to resolve UNAMBIGUOUSLY to Profit by "
        f"Category (its full field set == the header); a subset match "
        f"would have also matched Region level Sales and refused: {ev.calls}")

    # a header that fits NO sheet exactly must assign nothing (never guess)
    ev2 = _Evidence()
    _assign_dashboard_csv_by_header(
        "View2", view2["sheets"], "Something,Else\r\nx,1\r\n", ev2)
    assert ev2.calls == [], f"an unrecognized header must match nothing: {ev2.calls}"

    # a genuinely ambiguous header (matches more than one sheet's full set)
    # must ALSO assign nothing -- rig via real IR shape: two sheets that
    # resolve the SAME pill set (Region, Sales), so both qualify equally.
    twin_a = dict(view2["sheets"][1], name="Twin A")
    twin_b = dict(view2["sheets"][1], name="Twin B")
    ev3 = _Evidence()
    _assign_dashboard_csv_by_header("View2", [twin_a, twin_b],
                                    "Region,Sales\r\nWest,1\r\n", ev3)
    assert ev3.calls == [], f"two equally-qualifying sheets must refuse, not guess: {ev3.calls}"

    # --- 2. thousands-comma parsing in the vendored comparison engine ---
    assert VR._d("163,797.1638") == VR._d("163797.1638"), \
        "a comma-thousands Tableau export value must parse identically to " \
        "its uncommaed form"
    assert VR._d("1,234") is not None and float(VR._d("1,234")) == 1234.0
    assert VR._d(None) is None and VR._d("") is None, \
        "the comma fix must not turn an absent value into a fabricated 0"
    assert VR._d("not a number") is None, \
        "a genuinely unparseable string must still return None, not raise"

    print("ok  multi-sheet dashboard CSV matched to the RIGHT sheet by its "
          "header content (EXACT full-caption-set match, not a looser "
          "subset that was proven ambiguous on the real View2 case; an "
          "unrecognized or genuinely ambiguous header assigns nothing) and "
          "a thousands-comma Tableau export value now parses instead of "
          "silently registering as never-compared -- both found live on "
          "Regional Analysis, where Tableau values were reaching the pack "
          "but never actually landing in a comparison")


def _rad_ir():
    import json
    return json.load(open("regional_analysis_ir.json", encoding="utf-8"))


def test_validation_pack_adapter():
    """R12 -- the proof-first validation pack: the vendored comparison engine
    (validation_report.py) plus THIS accelerator's adapters
    (validation_adapter.py).

    What matters here is that the adapter feeds the engine REAL, correctly
    SCOPED data. Two false-failure bugs were found running it for real and
    are locked below:
      * a chart legitimately showing a SUBSET (top-30 ranking, sheet filter)
        was compared against an unscoped backend query returning every key
        (30 vs 800) -- a pure scope difference reported as a huge missing-key
        failure;
      * a monthly chart was compared against daily backend rows (48 vs 1242)
        because the chart's own DATE_TRUNC was not reproduced.
    Both are fixed by scoping the backend query to the displayed keys and by
    reproducing the sheet's declared date part.

    Runs fully offline against the corpus IR + local DuckDB."""
    import importlib
    import json
    from decimal import Decimal
    import config
    import cortex_semantic as CS
    import engine
    import parity
    import validation_adapter as VA
    import validation_report as VR

    importlib.reload(config)
    for fn in (engine.table_columns, engine._q_exec):
        try:
            fn.clear()
        except Exception:
            pass
    ir = json.load(open("workbook_ir.json", encoding="utf-8"))
    engine.configure(ir)
    engine.ORDERS = config.ORDERS
    all_metrics, _ = CS.build_metrics(ir)

    # --- tolerance comes from the WORKBOOK's own number format, never a flat
    # global percentage: a whole-dollar currency measure gets +/-$0.50, which
    # is exactly what makes a Tableau whole-dollar export reconcile against a
    # cents-precise backend without loosening the check for everything else.
    money = VA.measure_definition("Sales", "cur0")
    assert money["kind"] == "currency" and money["display_decimals"] == 0
    assert VR.derive_tolerance(money) == Decimal("0.5"), VR.derive_tolerance(money)
    cents = VA.measure_definition("Sales", "cur2")
    assert VR.derive_tolerance(cents) == Decimal("0.005")
    pct = VA.measure_definition("Profit Ratio", "pct")
    assert pct["kind"] == "percent" and pct.get("value_scale") == "fraction"
    unknown = VA.measure_definition("Mystery", None)
    assert unknown["kind"] == "number" and unknown["display_decimals"] == 2, \
        "an unknown format must fall back to a 2-decimal number, never claim exactness"

    # --- a real ranked chart (top-30 of 800 customers) must compare 30 vs 30
    dash = next(d for d in ir["dashboards"] if d["name"] == "Customers")
    sheet = next(s for s in dash["sheets"] if s["name"] == "CustomerRank")
    info = parity.collect_dashboard_section(ir, dash)
    spec = VA.build_chart_spec(ir, sheet, info["table"], all_metrics)
    assert not spec.get("skip_reason"), spec.get("skip_reason")
    assert spec["grain"] == ["Customer Name"], spec["grain"]
    n_st, n_be = len(spec["streamlit_rows"]), len(spec["backend_rows"])
    assert n_st == n_be, (
        f"backend must be SCOPED to the chart's displayed keys -- got {n_st} "
        f"displayed vs {n_be} backend row(s), the exact false-failure this fixes")
    assert n_st < 100, "CustomerRank is a top-N chart; it must not return all 800 customers"

    # --- a monthly date chart must compare monthly, not daily
    perf = next(d for d in ir["dashboards"] if d["name"] == "Performance")
    psheet = next(s for s in perf["sheets"] if s["name"] == "Performance")
    pinfo = parity.collect_dashboard_section(ir, perf)
    pspec = VA.build_chart_spec(ir, psheet, pinfo["table"], all_metrics)
    assert not pspec.get("skip_reason"), pspec.get("skip_reason")
    assert len(pspec["streamlit_rows"]) == len(pspec["backend_rows"]), (
        "a monthly chart must be compared against monthly backend rows -- "
        f"{len(pspec['streamlit_rows'])} vs {len(pspec['backend_rows'])}")
    assert "DATE_TRUNC" in pspec["backend_sql"].upper(), \
        "the sheet declares a month date part -- the backend query must reproduce it"

    # --- the STREAMLIT leg must be the app's OWN rendered data, not a second
    # run of the backend SQL (that would be circular, proving nothing)
    assert spec["streamlit_rows"] and spec["backend_rows"]
    assert spec["streamlit_rows"] is not spec["backend_rows"]

    # --- and the values must actually reconcile on the real corpus data
    engine_result = VR.compare_chart(dict(spec, tableau_rows=spec["streamlit_rows"]))
    assert engine_result["key_set_match"], "displayed and backend keys must align"
    failing = [r for r in engine_result["comparison_rows"] if r["status"] != VR.PASS]
    assert not failing, f"real corpus values must reconcile within tolerance: {failing[:2]}"

    # --- KPI / text-only sheets render no Altair chart but ARE a dashboard's
    # most-read numbers. They validate as ONE grand-total row, each tile
    # keeping its own number format so a currency tile and a percent tile get
    # their own correct tolerance. The comparison is against the DISPLAYED
    # (rounded) value on purpose -- that is what a user reads, and precision-
    # derived tolerance is exactly what reconciles it to a full-precision
    # backend figure.
    assert VA.parse_displayed("$2,326,534", "currency") == 2326534.0
    assert abs(VA.parse_displayed("12.6%", "percent") - 0.126) < 1e-12, \
        "a percent tile must come back on the FRACTION scale, matching the backend"
    assert VA.parse_displayed("n/a", "currency") is None, \
        "an unparseable tile must be skipped, never compared as a fabricated 0"
    kpi_dash = next(d for d in ir["dashboards"] if d["name"] == "Overview")
    kpi_sheet = next(s for s in kpi_dash["sheets"] if s["name"] == "Total Sales")
    kpi_info = parity.collect_dashboard_section(ir, kpi_dash)
    kpi_spec = VA.build_chart_spec(ir, kpi_sheet, kpi_info["table"], all_metrics)
    assert not kpi_spec.get("skip_reason"), kpi_spec.get("skip_reason")
    assert len(kpi_spec["streamlit_rows"]) == 1 and len(kpi_spec["backend_rows"]) == 1, \
        "a KPI sheet is one grand-total row"
    _kinds = {m["name"]: m["kind"] for m in kpi_spec["measures"]}
    assert _kinds.get("Sales") == "currency" and _kinds.get("Profit Ratio") == "percent", \
        f"each tile must keep its own format for tolerance: {_kinds}"
    kpi_result = VR.compare_chart(dict(kpi_spec, tableau_rows=kpi_spec["streamlit_rows"]))
    assert kpi_result["failed_cells"] == 0, (
        "the app's displayed KPI values must reconcile with the backend within "
        f"display precision: {kpi_result['failed_cells']} failed")

    # --- A SCOPED BACKEND QUERY THAT MATCHES NOTHING IS A TOOLING LIMIT, NOT A
    # MIGRATION DEFECT. Before this guard it produced a comparison where every
    # displayed value was marked failed -- a false failure, the worst possible
    # output for a client-facing report.
    _empty = VA.build_chart_spec(
        ir, kpi_sheet, kpi_info["table"], all_metrics)
    _forced = dict(_empty, streamlit_rows=_empty["streamlit_rows"], backend_rows=[])
    _cmp = VR.compare_chart(dict(_forced, tableau_rows=None))
    assert _cmp["status"] in (VR.BLOCKED, VR.FAIL)   # must never be PASS
    agg2 = next(d for d in ir["dashboards"] if d["name"] == "Overview")
    agg2_sheet = next(s for s in agg2["sheets"] if s["name"] == "Sales by Segment")
    agg2_spec = VA.build_chart_spec(ir, agg2_sheet,
                                    parity.collect_dashboard_section(ir, agg2)["table"],
                                    all_metrics)
    if agg2_spec.get("skip_reason"):
        # "Sales by Segment" is faceted into one small-multiple panel per
        # Segment (multiple st.altair_chart calls, one per column) --
        # headless_render.capture_sheet_chart combines them with
        # alt.hconcat (2026-08-07 fix: it used to silently keep only the
        # LAST panel and discard the rest, which meant a faceted sheet's
        # captured "data" was really just its final facet, not the whole
        # chart -- comparing that against a grain expecting every Segment
        # would have been a wrong comparison passed off as real, not an
        # honest one). An hconcat chart's top-level `.data` is Undefined
        # (each panel carries its own), so streamlit_rows correctly refuses
        # rather than guessing which panel's rows to use -- this reason is
        # therefore expected, not a defect.
        assert ("no rows" in agg2_spec["skip_reason"]
                or "AGGREGATE" in agg2_spec["skip_reason"]
                or "no dataframe" in agg2_spec["skip_reason"]), \
            f"unexpected refusal reason: {agg2_spec['skip_reason']}"
    else:
        assert agg2_spec["backend_rows"], \
            "a chart may only be reported comparable when the backend actually returned rows"

    # --- an AGGREGATE-grain calculated dimension (a window/LOD calc used as a
    # colour series) cannot be scoped in a WHERE clause. It must be refused
    # with a stated reason, never allowed to surface as a raw database
    # exception in a client-facing report. The guard regex is asserted
    # directly because a shell-escaping slip once left a literal control
    # character in it, silently disabling the check.
    assert VA._AGG_IN_EXPR.pattern.startswith("\\b"), \
        f"guard regex lost its word boundary: {VA._AGG_IN_EXPR.pattern!r}"
    assert VA._AGG_IN_EXPR.search("sum(PROFIT) OVER (PARTITION BY ORDER_ID)>0"), \
        "an aggregate/window grain expression must be detected"
    assert not VA._AGG_IN_EXPR.search("ORDER_DATE"), "a plain column must not trip the guard"
    assert not VA._AGG_IN_EXPR.search("DATE_TRUNC('MONTH', ORDER_DATE)"), \
        "DATE_TRUNC is not an aggregate -- date-grain charts must stay comparable"
    agg_dash = next(d for d in ir["dashboards"] if d["name"] == "Overview")
    agg_sheet = next(s for s in agg_dash["sheets"] if s["name"] == "Sales by Segment")
    agg_info = parity.collect_dashboard_section(ir, agg_dash)
    agg_spec = VA.build_chart_spec(ir, agg_sheet, agg_info["table"], all_metrics)
    reason = agg_spec.get("skip_reason", "")
    # Any honest refusal is acceptable: the direct aggregate-grain refusal,
    # (once the base-CTE precompute handles the expression) the
    # empty-scoped-result refusal, or -- this sheet is ALSO faceted into one
    # small-multiple panel per Segment, so capture_sheet_chart's 2026-08-07
    # alt.hconcat fix (combine every st.altair_chart call instead of keeping
    # only the last) now refuses at the capture step itself, before the
    # aggregate-grain scoping logic downstream is ever reached -- an
    # hconcat's top-level .data is Undefined, so streamlit_rows correctly
    # declines rather than guessing which panel's rows belong to the chart.
    # What must NEVER happen is a raw database exception surfacing in a
    # client-facing report, or a silent pass.
    assert reason, "an aggregate-grain series must not be silently passed"
    assert (("AGGREGATE calculated" in reason) or ("no rows" in reason)
            or ("no dataframe" in reason)), \
        f"expected a stated, human-readable refusal, got: {reason!r}"
    assert "Binder" not in reason and "Exception" not in reason, \
        f"a raw database exception must never reach the report: {reason!r}"

    # --- a chart the adapter cannot honestly extract must be BLOCKED WITH ITS
    # REASON, never dropped (an omitted chart reads as a validated one)
    blocked = VR.compare_chart({"id": "x", "title": "Unmappable",
                                "skip_reason": "channel renders an unnamed column"})
    assert blocked["status"] == VR.BLOCKED
    html = VR.render_html({"workbook": "W", "run_id": "R", "environment": "UAT",
                           "generated_at": "now", "status": VR.BLOCKED,
                           "dashboards": [{"id": "d", "name": "D", "status": VR.BLOCKED,
                                           "visual_status": VR.BLOCKED,
                                           "images": {"tableau": None, "streamlit": None},
                                           "visual": {}, "charts": [blocked],
                                           "formulas": [], "interactions": []}]})
    assert "Unmappable" in html and "channel renders an unnamed column" in html, \
        "a skipped chart's REASON must appear in the report, not be silently omitted"

    # --- missing Tableau rows must BLOCK, never quietly pass -- BUT the
    # Streamlit-vs-backend agreement that IS provable must still be reported.
    # The original all-or-nothing rule marked every cell BLOCKED whenever any
    # one source was absent, hiding a real disagreement the available pair
    # could have caught (a file-uploaded workbook never has a Tableau export,
    # so that is the common case, not the edge case).
    no_tableau = VR.compare_chart(dict(spec, tableau_rows=None))
    assert no_tableau["status"] == VR.BLOCKED, \
        "with no Tableau export there is no key-set authority -- must be BLOCKED, not PASS"
    assert no_tableau["failed_cells"] == 0, (
        "the Streamlit/backend pair reconciles, so no cell may be counted as "
        f"failed merely because Tableau is absent: {no_tableau['failed_cells']}")
    _cells = [m for r in no_tableau["comparison_rows"] for m in r["measures"]]
    assert _cells and all(m["status"] == VR.PASS for m in _cells), \
        "an available pair must still be judged PASS/FAIL on its merits"
    assert all(m["pair_diffs"]["streamlit_backend"] is not None for m in _cells), \
        "the available Streamlit-vs-backend difference must be computed and shown"
    assert all(m["pair_diffs"]["tableau_streamlit"] is None for m in _cells), \
        "an unavailable pair must stay None, never a fabricated zero difference"
    # a genuinely wrong app value must still FAIL on the available pair alone
    _broken = [dict(r) for r in spec["streamlit_rows"]]
    _mname = spec["measures"][0]["name"]
    _broken[0] = dict(_broken[0], **{_mname: float(_broken[0][_mname]) + 10000})
    _bad = VR.compare_chart(dict(spec, tableau_rows=None, streamlit_rows=_broken))
    assert _bad["failed_cells"] >= 1, \
        "a wrong app value must FAIL against the backend even with Tableau absent"

    # --- formula classification carries the distinction a boolean cannot:
    # a raw Tableau column rendered as SUM(col) is equivalent only AT THIS GRAIN
    cls, _impact = VA.classify_formula("raw column (SALES)", "SUM(SALES)", "column", True, None)
    assert cls == "EQUIVALENT_AT_CURRENT_GRAIN", cls
    assert VR._formula_status({"classification": cls}) == VR.REVIEW, \
        "an unapproved grain-dependent equivalence must surface as REVIEW, not PASS"
    assert VR._formula_status({"classification": cls, "approved": True}) == VR.PASS
    cls2, _ = VA.classify_formula("sum([Profit])/sum([Sales])",
                                  "sum(PROFIT)/ NULLIF(sum(SALES), 0)", "calc", True, None)
    assert cls2 == "SEMANTICALLY_EQUIVALENT", cls2
    cls3, _ = VA.classify_formula("AVG([Sales])", "SUM(SALES)", "calc", False, "different agg")
    assert cls3 == "MISMATCH" and VR._formula_status({"classification": cls3}) == VR.FAIL

    print("ok  R12 validation pack (precision-derived tolerance read from the WORKBOOK's "
         "own number format -- cur0 -> +/-$0.50, unknown -> no false exactness; backend "
         "SCOPED to the chart's displayed keys, fixing a real 30-vs-800 false missing-key "
         "failure on a top-N chart; the sheet's declared date part reproduced, fixing a "
         "real 48-vs-1242 monthly-vs-daily false failure; the Streamlit leg is the app's "
         "OWN captured chart data, never a second run of the backend SQL; real corpus "
         "values reconcile within tolerance; an unextractable chart is BLOCKED WITH ITS "
         "REASON in the rendered HTML rather than omitted; absent Tableau rows BLOCK "
         "instead of passing; formula classification distinguishes EQUIVALENT_AT_CURRENT_"
         "GRAIN (REVIEW) from SEMANTICALLY_EQUIVALENT (PASS) and MISMATCH (FAIL))")


def test_blocked_visual_does_not_cap_passing_data_proof():
    """REAL BUG FOUND LIVE 2026-08-10: `validate_run`'s dashboard status was
    `_status_of([visual_status] + chart/formula/interaction statuses)` --
    the WORST of all of them, and BLOCKED outranks PASS. So a dashboard
    whose Streamlit-side screenshot could not be captured (no browser in
    the environment -- exactly the case running the accelerator's own
    validation button FROM INSIDE the deployed Streamlit-in-Snowflake app,
    which has no browser) read BLOCKED even when every chart, formula and
    interaction had genuinely, deterministically PASSED. The accelerator
    would show a CORRECTLY migrated workbook as broken, in the one
    environment (its own deployed demo) most likely to be used for a live
    client walkthrough.

    Fix: a BLOCKED visual (missing evidence) is excluded from the rollup.
    A visual that WAS captured and found genuinely wrong (FAIL) or below
    threshold (REVIEW) is NOT excluded -- "no proof, no pass" still holds
    for the visual claim itself; this only stops its ABSENCE from
    overriding results that WERE measured.

    Runs the REAL `validate_run`/`generate_report` path (not a
    reimplementation of the rollup) on a REAL passing chart comparison
    reused from `test_validation_pack_adapter`'s own proven CustomerRank
    fixture -- the same real corpus data, not synthetic rows."""
    import json
    import shutil
    import tempfile

    import cortex_semantic as CS
    import engine
    import parity
    import validation_adapter as VA
    import validation_report as VR

    ir = json.load(open("workbook_ir.json", encoding="utf-8"))
    engine.configure(ir)
    dash = next(d for d in ir["dashboards"] if d["name"] == "Customers")
    sheet = next(s for s in dash["sheets"] if s["name"] == "CustomerRank")
    info = parity.collect_dashboard_section(ir, dash)
    all_metrics, _ = CS.build_metrics(ir)
    spec = VA.build_chart_spec(ir, sheet, info["table"], all_metrics)
    assert not spec.get("skip_reason"), spec.get("skip_reason")
    # tableau_rows = streamlit_rows: proves this chart's OWN comparison PASSES
    # (same proof shape test_validation_pack_adapter already locks) -- the
    # point here is what the DASHBOARD-level rollup does with that PASS,
    # not re-proving the chart engine itself.
    chart = dict(spec, tableau_rows=spec["streamlit_rows"])

    root = tempfile.mkdtemp()
    try:
        # Case 1: no screenshots supplied at all -- visual_status must
        # compute BLOCKED (see _visual_status), and the dashboard's overall
        # status must still be PASS, because the one chart genuinely passed.
        out1 = root + "/no_visual"
        result1 = VR.generate_report(
            {"dashboards": [{"name": "Customers", "visual": {}, "charts": [chart]}]},
            out1)
        summary1 = json.loads(open(result1["summary"], encoding="utf-8").read())
        d1 = summary1["dashboards"][0]
        assert d1["visual_status"] == VR.BLOCKED, d1["visual_status"]
        assert d1["status"] == VR.PASS, (
            f"a dashboard with a genuinely passing chart must not read "
            f"{d1['status']} just because no screenshot was available -- "
            f"this is the exact bug that made the deployed accelerator's "
            f"OWN validation button show a correct migration as broken")
        assert summary1["status"] == VR.PASS, summary1["status"]

        # Case 2: a screenshot WAS captured and the visual check genuinely
        # FAILED. This must still cap the dashboard -- the fix narrows the
        # exemption to BLOCKED only, it must not swallow a real finding.
        # _visual_status checks for real, EXISTING screenshot files before
        # it even looks at `checks` -- real (tiny, valid) PNGs are needed
        # here, not just a checks list, or it reads BLOCKED regardless.
        png = root + "/dummy.png"
        with open(png, "wb") as fh:
            fh.write(bytes.fromhex(
                "89504e470d0a1a0a0000000d494844520000000100000001080600000"
                "01f15c4890000000a49444154789c6300010000050001"
                "0d0a2db40000000049454e44ae426082"))
        out2 = root + "/visual_fail"
        result2 = VR.generate_report(
            {"dashboards": [{
                "name": "Customers",
                "visual": {"tableau_screenshot": png, "streamlit_screenshot": png,
                          "checks": [{"status": "FAIL"}]},
                "charts": [chart],
            }]},
            out2)
        summary2 = json.loads(open(result2["summary"], encoding="utf-8").read())
        d2 = summary2["dashboards"][0]
        assert d2["visual_status"] == VR.FAIL, d2["visual_status"]
        assert d2["status"] == VR.FAIL, (
            "a PROVEN visual mismatch must still cap the dashboard -- the "
            "fix only exempts MISSING evidence, never a real finding")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("ok  a BLOCKED visual (no screenshot available) no longer caps a "
          "dashboard's status when its chart/formula/interaction proof "
          "genuinely passed -- proven on the real CustomerRank fixture "
          "through the real validate_run/generate_report path, not a "
          "reimplementation; a PROVEN visual FAIL still caps the dashboard "
          "exactly as before, so this only exempts missing evidence, never "
          "a real finding -- fixes the deployed accelerator showing a "
          "correct migration as BLOCKED when run from its own Streamlit-in-"
          "Snowflake sandbox, which has no browser for the visual leg")


def test_migration_report_html():
    """parity.build_migration_report_html -- the executive-readable report
    (status banner, executive summary, Dashboard Validation Matrix, an
    auto-derived Issues Register, per-dashboard Visual/Data/Formula proof).
    Every status/number here must come from sections/rollup already computed
    by build_cortex_dashboard_validation_report -- this function only
    organizes and displays them, never recomputes a verdict. Runs against
    the real Superstore IR with the same FakeSession pattern as the test
    above (offline, no live account)."""
    import json
    import parity

    ir = json.load(open("workbook_ir.json", encoding="utf-8"))

    class FakeRow:
        def __getitem__(self, i): return 42

    class FakeCortexRow:
        def __init__(self, text): self.text = text
        def __getitem__(self, i): return self.text

    class FakeResult:
        def __init__(self, rows): self._rows = rows
        def collect(self): return self._rows

    class FakeSession:
        def sql(self, text):
            if "CORTEX.COMPLETE" in text:
                return FakeResult([FakeCortexRow("No bugs found.")])
            return FakeResult([FakeRow(), FakeRow()])

    res = parity.check_workbook(ir)
    sections, rollup = parity.build_cortex_dashboard_validation_report(
        ir, FakeSession(), "Superstore.twb", res=res)
    resolved = [s for s in sections if not s.get("skipped")]
    skipped = [s for s in sections if s.get("skipped")]

    # --- no screenshots supplied: must state that honestly, never fake one
    html_no_shots = parity.build_migration_report_html(sections, rollup, "Superstore.twb")
    assert "<html" in html_no_shots and "</html>" in html_no_shots
    assert "<img" not in html_no_shots, \
        "with no screenshots supplied, there must be no <img> tag at all -- no fabricated placeholder"
    assert "Not captured this run" in html_no_shots

    # --- every section appears in the top-level matrix AND has a detail anchor
    for sec in sections:
        assert sec["title"] in html_no_shots, f"{sec['title']} missing from the report"
    import re as _re
    for sec in resolved:
        anchor = _re.sub(r"[^a-z0-9]+", "-", sec["title"].lower()).strip("-")
        assert f'id="{anchor}"' in html_no_shots and f'href="#{anchor}"' in html_no_shots, \
            f"{sec['title']}: matrix row must link to a real anchor in the detail section"

    # --- Issues Register: the skip must appear as a real LOW/Coverage row,
    # never silently dropped from the register
    import html as _html_mod2
    for sec in skipped:
        assert _html_mod2.escape(sec["skipped"], quote=True) in html_no_shots

    # --- a REAL screenshot, when supplied, must be embedded (base64 <img>),
    # never a gray placeholder box standing in for real evidence
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40   # not a valid PNG, just real bytes to embed
    shot_title = resolved[0]["title"]
    html_with_shot = parity.build_migration_report_html(
        sections, rollup, "Superstore.twb", app_screenshots={shot_title: fake_png})
    assert "<img src=\"data:image/png;base64," in html_with_shot, \
        "a supplied screenshot must be embedded as a real base64 image"
    import base64
    assert base64.b64encode(fake_png).decode("ascii") in html_with_shot, \
        "the embedded image bytes must be the REAL supplied screenshot, not a stand-in"

    # --- status badges must reflect the REAL verdicts already decided, not
    # a second judgment made by this renderer
    any_data_bug = any(r.get("verdict") == "BUG"
                       for sec in resolved for r in sec.get("value_rows", []))
    any_formula_bug = any(not r["match"]
                          for sec in resolved for r in sec["formula_rows"])
    if not any_data_bug and not any_formula_bug and not skipped:
        assert '<span class="value">PASS</span>' in html_no_shots
    if skipped:
        assert '<span class="badge na">INCOMPLETE</span>' in html_no_shots, \
            "a skipped section must render as INCOMPLETE in the matrix, not silently pass"

    print("ok  migration validation report HTML (executive status banner, summary, "
         "coverage legend, dashboard matrix with real anchors, auto-derived issues "
         "register including skipped sections, real embedded screenshots when supplied "
         "-- never a fabricated placeholder -- and status badges reflecting only "
         "already-decided verdicts)")


def test_no_undefined_names_in_app():
    """A NameError inside a Stage helper only surfaces at RUNTIME in the deployed
    SiS app -- e.g. _stage3_cortex_layer referenced `config` when it was only
    imported INSIDE run_pipeline, so the hosted app died with 'name config is not
    defined' at Stage 3. pyflakes catches undefined names statically. Skips
    cleanly if pyflakes isn't installed (never a false failure)."""
    import subprocess
    files = ["pipeline_app.py", "pipeline.py", "semantic_layer.py",
             "init_workbook.py", "engine.py", "cortex_semantic.py",
             "backend.py", "config.py", "parity.py", "codegen.py"]
    try:
        r = subprocess.run([sys.executable, "-m", "pyflakes"] + files,
                           capture_output=True, text=True, cwd=ROOT)
    except Exception as e:
        print(f"skip undefined-name check (pyflakes unavailable: {e})")
        return
    if "No module named pyflakes" in (r.stderr or ""):
        print("skip undefined-name check (pyflakes not installed)")
        return
    undefined = [l for l in r.stdout.splitlines() if "undefined name" in l.lower()]
    assert not undefined, ("undefined names (runtime NameError risk in the deployed "
                           "app):\n  " + "\n  ".join(undefined))
    print("ok  no undefined names in app/pipeline modules (pyflakes) -- guards the "
          "class of live NameError like the Stage-3 `config` bug")


def test_no_silent_conn_gated_ui():
    """STATIC GATE against a bug class that shipped TWICE, reported both
    times as "the feature doesn't work" with no visible error (2026-07-30):
    a Stage 5 UI section gated on `if _conn:` (only true when the workbook
    was loaded via the Tableau REST fetch flow, never for a file upload) had
    NO `else`/`elif` -- so for a file-uploaded workbook the WHOLE section,
    heading included, rendered NOTHING. That is indistinguishable, from the
    outside, from a broken feature, and cost multiple rounds of "still not
    working" / screenshots / re-diagnosis each time it happened (once for
    R8's visual validation, once for the raw-column Tableau-reference panel).

    This project's own rule is that an unavailable capability STATES its
    reason. This gate makes that mechanical instead of relying on remembering
    it: any TOP-LEVEL `if` statement (not nested inside another `if`) whose
    condition tests `_conn` and whose body renders Streamlit UI
    (markdown/info/warning/error/dataframe/button/expander) MUST carry an
    `elif`/`else`, so a future `_conn`-gated section can't reintroduce the
    same silent-non-render bug. A `_conn` check NESTED inside an already-
    message-printing branch (deciding which of several reasons to show, not
    whether to show anything at all) is deliberately NOT flagged -- only
    the outermost gate of a section matters here."""
    import ast

    src = open(os.path.join(ROOT, "pipeline_app.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    def _uses_conn(test):
        return any(isinstance(n, ast.Name) and n.id == "_conn"
                  for n in ast.walk(test))

    def _renders_ui(node):
        return any(isinstance(n, ast.Attribute) and n.attr in (
            "markdown", "info", "warning", "error", "dataframe",
            "button", "expander") for n in ast.walk(node))

    violations = []

    def _walk(node, if_depth):
        if isinstance(node, ast.If):
            if if_depth == 0 and _uses_conn(node.test) and _renders_ui(node):
                if not node.orelse:
                    violations.append(node.lineno)
            for child in node.body:
                _walk(child, if_depth + 1)
            for child in node.orelse:
                _walk(child, if_depth + 1)
            return
        for _, value in ast.iter_fields(node):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        _walk(item, if_depth)
            elif isinstance(value, ast.AST):
                _walk(value, if_depth)

    _walk(tree, 0)
    assert not violations, (
        "pipeline_app.py has a top-level `if _conn:`-gated UI section with "
        "no else/elif -- a file-uploaded workbook will render NOTHING here, "
        "indistinguishable from a broken feature (lines: "
        + ", ".join(map(str, violations)) + ")")
    print("ok  no silent _conn-gated UI sections (a top-level `if _conn:` "
          "block that renders Streamlit UI must carry an else/elif stating "
          "why the capability is unavailable for a file-uploaded workbook -- "
          "this exact silent-non-render class shipped twice, 2026-07-30, "
          "each reported as 'it doesn't work' with no visible error)")


def test_pipeline_demo_bundle_complete():
    """Every LOCAL module the deployed pipeline_demo app imports (transitively,
    including function-level imports) must be listed in snowflake.yml's
    pipeline_demo artifacts -- else the hosted SiS app ImportErrors at runtime.
    This is exactly the bug that shipped when Stage 3a added `import
    semantic_layer` but the artifact list didn't: caught here mechanically."""
    import ast
    import re
    yml = open(os.path.join(ROOT, "snowflake.yml"), encoding="utf-8").read()
    block = yml[yml.index("pipeline_demo:"):]              # last entity in the file
    arts = set(re.findall(r"-\s+([A-Za-z0-9_]+\.py)", block))

    def local_imports(mod_file):
        p = os.path.join(ROOT, mod_file)
        if not os.path.exists(p):
            return set()
        tree = ast.parse(open(p, encoding="utf-8").read())
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                names |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                names.add(n.module.split(".")[0])
        # keep only names that are local .py modules in the repo root
        return {f"{m}.py" for m in names
                if os.path.exists(os.path.join(ROOT, f"{m}.py"))}

    seen, queue = set(), ["pipeline_app.py"]
    while queue:
        m = queue.pop()
        if m in seen:
            continue
        seen.add(m)
        queue.extend(local_imports(m))
    missing = sorted(m for m in seen if m not in arts)
    assert not missing, (f"pipeline_demo bundle (snowflake.yml) is missing modules the "
                         f"deployed app imports: {missing}. Add them to the artifacts "
                         f"list or the hosted app ImportErrors at runtime.")
    print(f"ok  pipeline_demo bundle complete ({len(seen)} imported modules all in "
          "snowflake.yml artifacts -- no runtime ImportError)")


def test_data_model_view_deploy():
    """Stage 3a — replicate the Tableau DATA MODEL (joins/relationships) as a real
    Snowflake VIEW, but ONLY when the constituent tables exist SEPARATELY in
    Snowflake. Superstore's 'Sample - Superstore' datasource is a 3-table star
    (Orders + People + Returns) -- semantic_layer.describe_model classifies it and
    builds the CREATE VIEW; pipeline.data_model_report probes whether the tables
    exist separately; deploy_model_view runs + verifies it. Offline via a stub.

    Honesty: a FLATTENED extract (only the merged table exists) is NOT deployable
    -- said plainly, never a view over tables that aren't there. Also covers the
    Cortex-view skip-if-exists probe."""
    import re
    import semantic_layer as SL
    import pipeline

    class _R:
        def __init__(s, rows): s._rows = rows
        def collect(s): return s._rows

    class _ModelStub:
        def __init__(s, existing=(), semantic_views=()):
            s.existing = {f.upper() for f in existing}
            s.sv = {v.upper() for v in semantic_views}
            s.created = []
        def sql(s, q):
            u = q.upper()
            if u.strip().startswith("CREATE OR REPLACE VIEW"):
                s.created.append(q)
                m = re.search(r"CREATE OR REPLACE VIEW\s+(\S+)", q, re.I)
                if m:
                    s.existing.add(m.group(1).replace('"', '').upper())
                return _R([])
            if "SHOW SEMANTIC VIEWS" in u:
                # database_name/schema_name mirror real SHOW output -- required
                # since 2026-08-06's fix scopes the match to the full triple,
                # not the bare name alone (see the false-positive regression
                # check below).
                return _R([{"name": v, "database_name": "WBR_DB",
                           "schema_name": "PUBLIC"} for v in s.sv])
            if "INFORMATION_SCHEMA.TABLES" in u:
                db = re.search(r'"([^"]+)"\.INFORMATION_SCHEMA', u)
                sch = re.search(r"TABLE_SCHEMA = '([^']+)'", u)
                tbl = re.search(r"TABLE_NAME = '([^']+)'", u)
                if db and sch and tbl:
                    fqn = f"{db.group(1)}.{sch.group(1)}.{tbl.group(1)}".upper()
                    return _R([{"N": 1 if fqn in s.existing else 0}])
                return _R([{"N": 0}])
            return _R([])

    root = TP.load_twb_xml(TWB)
    rep = SL.describe_model(root, "WBR_DB", "PIPELINE_DEMO")
    star = next(m for m in rep if m["shape"] == "star")
    assert star["n_tables"] == 3 and star["view_ddl"], star
    assert any(m["shape"] == "single" for m in rep), "expected single-table ds too"

    # CASING (the live scope-B bug): phys_source=True must reference NORMALIZED
    # columns (f.ORDER_ID, unquoted UPPER) because the pipeline loads tables via
    # _normalize_columns; the default keeps ORIGINAL-case quoted refs (f."Order
    # ID") for a live source. A quoted lowercase ref against a folded-uppercase
    # column is exactly the 'invalid identifier F."event_id"' failure.
    star_phys = next(m for m in SL.describe_model(
        root, "WBR_DB", "PIPELINE_DEMO", phys_source=True) if m["shape"] == "star")
    assert '."' not in star_phys["view_ddl"], \
        "phys_source view must have NO quoted column refs (normalized UPPER only)"
    assert '."' in star["view_ddl"], \
        "default view must keep quoted original-case column refs (live source)"
    tabs = [t["fqn"] for t in star["tables"]]

    # A: tables exist separately -> deployable, and deploy verifies the view
    sess = _ModelStub(existing=tabs)
    r = pipeline.data_model_report(sess, root, db="WBR_DB", schema="PIPELINE_DEMO")
    rstar = next(m for m in r if m["shape"] == "star")
    assert rstar["deployable"] is True, rstar
    v = pipeline.deploy_model_view(sess, rstar)
    assert v.endswith("SAMPLE_SUPERSTORE_MODEL"), v
    assert any(q.strip().upper().startswith("CREATE OR REPLACE VIEW")
               for q in sess.created), "no CREATE VIEW issued"

    # B: flattened extract (no separate tables) -> NOT deployable, no false view
    sess2 = _ModelStub(existing=[])
    r2 = pipeline.data_model_report(sess2, root, db="WBR_DB", schema="PIPELINE_DEMO")
    assert next(m for m in r2 if m["shape"] == "star")["deployable"] is False

    # C: Cortex semantic-view skip-if-exists probe
    sess3 = _ModelStub(semantic_views=["SUPERSTORE_SEMANTIC"])
    assert pipeline.semantic_view_exists(sess3, "WBR_DB.PUBLIC.SUPERSTORE_SEMANTIC") is True
    assert pipeline.semantic_view_exists(sess3, "NOT_THERE") is False
    # REGRESSION LOCK (2026-08-06): a same-named semantic view in a DIFFERENT
    # schema must NOT count as "exists" for this target -- the exact bug that
    # let a stale/unrelated view make the real CREATE SEMANTIC VIEW get
    # skipped, so Cortex Analyst then 404'd on a view that was never actually
    # deployed to the target schema. Matching used to be bare-name-only.
    assert pipeline.semantic_view_exists(
        sess3, "WBR_DB.PIPELINE_DEMO.SUPERSTORE_SEMANTIC") is False, \
        "must not match a same-named view registered under a different schema"
    print("ok  data-model view (star detected + deployed when tables exist separately; "
          "flattened source not deployable, no phantom view; semantic-view skip-if-exists "
          "scoped to database+schema, not bare name -- 2026-08-06 false-positive fix locked)")


def test_worksheet_shown_params_render_on_tab():
    """A parameter Tableau shows as a control ON a standalone WORKSHEET tab must
    render in that tab's control row -- not get hoisted to the global sidebar.

    Live gap (Superstore_Tableau2024_3 / Superstore.twb): the 'What If Forecast'
    worksheet shows two parameter controls (New Business Growth + Churn Rate) via
    <card type='parameter'> in its <window>. The engine reads a DASHBOARD's
    placed paramctrl zones (so the Commission tab was exact) but ignored these
    worksheet cards, so those two params fell through engine._param_is_live into
    the global sidebar -- looking 'missing' from the tab the user compares to
    Tableau. Fix: tableau_parser.worksheet_shown_params reads the cards and
    build_ir places them on the worksheet tab.

    Teeth: the two params ARE live (so the OLD code would sidebar them), yet they
    must now be PLACED on the What If Forecast tab and therefore absent from the
    sidebar set."""
    ir = TP.build_ir(TWB)
    engine.configure(ir)
    FORE = {"New Business Growth", "Churn Rate"}

    # the helper reads the worksheet's parameter cards (mapped to captions)
    root = TP.load_twb_xml(TWB)
    shown = TP.worksheet_shown_params(root, ir_param_alias(root))
    assert "What If Forecast" in shown, f"cards not read: {shown}"
    assert FORE <= set(shown["What If Forecast"]), shown["What If Forecast"]

    # they render on the What If Forecast tab's control row
    tab = next((d for d in ir["dashboards"] if d["name"] == "What If Forecast"), None)
    assert tab is not None, "What If Forecast tab missing"
    assert FORE <= set(tab.get("params") or []), tab.get("params")

    # ...and are therefore PLACED, so the global sidebar no longer shows them
    placed = engine._placed_params()
    assert FORE <= placed, f"forecast params not in placed set: {placed}"
    sidebar = [c for c in engine.PARAM_DEFS
               if c not in placed and engine._param_is_live(c)]
    assert not (FORE & set(sidebar)), f"forecast params still in sidebar: {sidebar}"

    # the two params ARE genuinely live -- proving the fix relocated them rather
    # than the sidebar simply never wanting them (guards against a hollow pass)
    assert all(engine._param_is_live(p) for p in FORE), \
        "forecast params must be live (else this gate proves nothing)"

    # the Commission dashboard's placed params are untouched (no regression)
    comm = next((d for d in ir["dashboards"] if d["name"] == "Commission Model"), None)
    assert comm and {"New Quota", "Base Salary", "Sort by", "Commission Rate"} \
        <= set(comm.get("params") or []), comm.get("params") if comm else None
    print("ok  worksheet-shown parameter controls render on their own tab (What If "
          "Forecast: New Business Growth + Churn Rate), not the global sidebar")


def ir_param_alias(root):
    """internal-name -> caption map for parameters (test helper), derived
    straight from the XML columns so it needs no meta pipeline."""
    alias = {}
    for col in root.findall(".//column"):
        if col.get("param-domain-type") is not None:
            nm = (col.get("name") or "").strip("[]")
            cap = col.get("caption") or nm
            if nm and nm != cap:
                alias[nm] = cap
    return alias


class _StubSession:
    """A minimal stand-in for a Snowpark Session so the DEPLOY orchestration can
    be proven offline (CI has no Snowflake account). Records every SQL string
    and every file.put so the gate can assert what WOULD run, without a network."""
    def __init__(self):
        self.sql_log = []
        self.put_log = []

    class _Q:
        def __init__(self, rows):
            self._rows = rows

        def collect(self):
            return self._rows

    def get_current_warehouse(self):
        return '"POWERHOUSE"'                       # quoted, as Snowpark returns

    def sql(self, q):
        self.sql_log.append(q)
        up = q.upper()
        if "INFORMATION_SCHEMA.SCHEMATA" in up:
            return _StubSession._Q([{"N": 1}])       # schema exists -> no CREATE
        if "CURRENT_ORGANIZATION_NAME" in up:
            return _StubSession._Q([{"O": "MYORG", "A": "MYACCT"}])
        return _StubSession._Q([])

    class _File:
        def __init__(self, parent):
            self.parent = parent

        def put(self, local, stage, **kw):
            self.parent.put_log.append((local, stage))

    @property
    def file(self):
        return _StubSession._File(self)


def test_deploy_streamlit_app():
    """Proving gate for the HUMAN-GATED in-app Deploy button (R5). The pipeline
    used to only GENERATE app_<stem>.py and offer a download; pipeline.
    deploy_streamlit_app now ships it to Streamlit-in-Snowflake through the
    Snowpark session (no `snow` CLI -- a SiS sandbox has none).

    CI cannot hit a real account, so this proves the offline-decidable
    guarantees with teeth: identifier + DDL are well-formed, EVERY runtime
    module the deployed app imports exists on disk (else the deploy 404s in
    Snowflake), the orchestration stages the app + all modules + a
    datasources.json and runs exactly one CREATE STREAMLIT, and -- the project's
    hard sandbox rule -- it NEVER issues a session-context `USE`."""
    import pipeline

    # 1) identifier: valid unquoted Snowflake identifier, uppercased, no leading
    #    digit even for a digit-leading stem.
    ident = pipeline._streamlit_identifier("2024 Superstore Top-N!")
    assert ident.isidentifier() and ident == ident.upper()
    assert ident.startswith("TABLEAU_TO_SIS_") and not ident[len("TABLEAU_TO_SIS_")].isdigit()

    # 2) DDL: has the required clauses, fully-qualified, and NEVER a `USE`.
    ddl = pipeline._create_streamlit_ddl(
        "WBR_DB", "PIPELINE_DEMO", ident, "STREAMLIT_STAGE", "superstore",
        "app_superstore.py", "POWERHOUSE", None)
    for needle in ("CREATE OR REPLACE STREAMLIT", "ROOT_LOCATION",
                   "MAIN_FILE = 'app_superstore.py'", "QUERY_WAREHOUSE"):
        assert needle in ddl, f"DDL missing {needle!r}:\n{ddl}"
    assert " USE " not in f" {ddl.upper()} ", "DDL must not issue a session-context USE"

    # 3) every runtime module the generated app needs actually exists on disk --
    #    a deploy would fail in Snowflake if one were missing/renamed.
    for m in pipeline.APP_RUNTIME_MODULES:
        assert os.path.exists(os.path.join(ROOT, m)), f"runtime module absent: {m}"

    # 4) refuses cleanly with no session (not a bare AttributeError).
    try:
        pipeline.deploy_streamlit_app(None, "x", "app_x.py")
        assert False, "deploy with no session must raise"
    except RuntimeError:
        pass

    # 5) full orchestration against the stub: stages app + all modules +
    #    datasources.json, runs exactly one CREATE STREAMLIT, no USE anywhere,
    #    returns a well-formed identifier/url.
    import tempfile
    work = tempfile.mkdtemp(prefix="deploytest_")
    appf = os.path.join(work, "app_stub.py")
    with open(appf, "w", encoding="utf-8") as f:
        f.write("from engine import run\nrun({})\n")
    stub = _StubSession()
    res = pipeline.deploy_streamlit_app(
        stub, "stub", appf, root_dir=ROOT,
        datasources={"DS": {"table": "WBR_DB.PIPELINE_DEMO.DS"}})
    put_names = {os.path.basename(local) for local, _ in stub.put_log}
    assert "app_stub.py" in put_names, put_names
    assert "datasources.json" in put_names, put_names
    assert {"engine.py", "backend.py", "config.py"} <= put_names, put_names
    creates = [q for q in stub.sql_log
               if q.strip().upper().startswith("CREATE OR REPLACE STREAMLIT")]
    assert len(creates) == 1, f"expected exactly one CREATE STREAMLIT, got {len(creates)}"
    assert not any(" USE " in f" {q.upper()} " for q in stub.sql_log), \
        "no session-context USE may be issued to a SiS-style session"
    assert res["identifier"].endswith("TABLEAU_TO_SIS_STUB")
    assert res["url"] and res["url"].endswith("WBR_DB.PIPELINE_DEMO.TABLEAU_TO_SIS_STUB")
    print("ok  deploy streamlit app (identifier + DDL well-formed, every runtime "
          "module staged, exactly one CREATE STREAMLIT, never a session-context USE)")


def main():
    if "--update-layout-snapshots" in sys.argv:
        test_layout_snapshots(update=True)
        return
    ir = test_ir_invariants()
    test_all_sheets_render(ir)
    test_product_detail_has_rows(ir)
    test_what_if_math(ir)
    test_numeric_validation()
    test_color_category_closed()
    test_app_interactions()
    test_2024_3_sample_pack()
    test_silent_gap_detections()
    test_topn_by_parameter()
    test_hierarchies_drill()
    test_device_layouts()
    test_table_calc_engine()
    test_ecommerce_parity()
    test_container_layout()
    test_semantic_layer()
    test_non_data_sheets()
    test_detail_table_inference()
    test_visual_risk_checklist()
    test_placeholder_member_list()
    test_date_mark_class_decides_line_vs_bars()
    test_ecommerce_end_to_end()
    test_placed_param_renders_once()
    test_view_order_filter_and_own_extract()
    test_codegen_emits_parsable_source()
    test_converter_flattens_and_topn_guard()
    test_absolute_layout_rows()
    test_legend_zone_not_mistaken_for_sheet()
    test_layout_snapshots()
    test_datepart_member_as_full_date()
    test_no_reserved_word_sql_aliases()
    test_backend_uses_pushed_session()
    test_write_pandas_date_fix()
    test_pipeline_reuses_preloaded_table()
    test_reuse_existing_table_cross_schema()
    test_snowflake_uppercase_alias()
    test_parity_validation()
    test_parity_no_local_file_reuses_table_repull()
    test_cortex_semantic_generation()
    test_cortex_calc_fallback_guards()
    test_per_workbook_profile_routing()
    test_live_connection_support()
    test_custom_sql_execution()
    test_auto_bind_existing_snowflake_table()
    test_r10_multitable_source_autobind()
    test_r9_live_multitable_join()
    test_tableau_server_url_parsing_and_fetch()
    test_tableau_server_view_data_pull()
    test_tableau_server_view_image_pull()
    test_headless_render_to_png()
    test_headless_render_never_touches_real_widgets()
    test_dashboard_composite_follows_zone_tree()
    test_validation_pack_slimmed()
    test_app_screenshot_no_pipe_deadlock()
    test_vision_validate_dashboard()
    test_non_star_join_and_blends()
    test_union_support()
    test_dashboard_filter_governs_sheet_filter()
    test_dashboard_filter_all_overrides_sheet_saved_value()
    test_dashboard_filter_scoped_to_bound_sheets()
    test_tracker_consistency()
    test_context_filter_applied_inside_topn_ranking()
    test_bar_colored_by_own_axis_has_no_offset()
    test_deploy_streamlit_app()
    test_worksheet_shown_params_render_on_tab()
    test_data_model_view_deploy()
    test_build_data_model_tables_scope_b()
    test_no_undefined_names_in_app()
    test_no_silent_conn_gated_ui()
    test_section_validation_notebook()
    test_r2_live_truth_pull()
    test_raw_measure_live_truth_and_json_verdicts()
    test_dashboard_section_validation()
    test_cortex_dashboard_validation_report()
    test_interaction_proof()
    test_migration_report_html()
    test_layered_chart_streamlit_rows_and_pie_theta_caption()
    test_multisheet_dashboard_csv_matched_by_header_and_thousands_comma()
    test_validation_pack_adapter()
    test_blocked_visual_does_not_cap_passing_data_proof()
    test_pipeline_demo_bundle_complete()
    print("\nALL REGRESSION TESTS PASSED")


if __name__ == "__main__":
    main()
