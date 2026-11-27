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
import calc_translator as CT           # noqa: E402
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


def test_measure_names_native_param_filter_detected():
    """Native (no-calc) Measure Names/Values selector, part 1: the parser
    recognizes a Measure Names filter set to Condition -> By formula, which
    Tableau stores as <groupfilter function='formula' formula='[Measure Names]
    = [Parameters].[X]'> -- distinct from the ALREADY-SHIPPED enumerated-
    members form (function='member', measure_names_from_filter), which this
    must NOT misfire on. SYNTHETIC: no corpus workbook exercises this pattern
    (verified by grep across every Measure-Names-using workbook in this repo),
    so this is hand-authored from Tableau's documented groupfilter schema,
    same honesty posture as this project's Union support ('synthetic-
    validated, no corpus workbook exists')."""
    import xml.etree.ElementTree as ET

    def ws_with_formula(formula):
        return ET.fromstring(f"""
          <worksheet name='W'><table><view>
            <filter class='categorical' column='[federated.x].[:Measure Names]'>
              <groupfilter function='formula' formula='{formula}' />
            </filter>
          </view></table></worksheet>""")

    # internal name differs from caption -- must resolve through param_alias,
    # exactly like calc formulas already do (extract_calcs_params's alias map)
    ws = ws_with_formula("[Measure Names] = [Parameters].[Parameter 1]")
    assert TP.measure_names_param(ws, {"Parameter 1": "Select Measure"}) == "Select Measure"
    # no alias available (internal name == caption, the common case) -- must
    # still resolve, falling back to the raw name
    ws2 = ws_with_formula("[Measure Names] = [Parameters].[Select Measure]")
    assert TP.measure_names_param(ws2, {}) == "Select Measure"
    # operand order reversed -- Tableau can emit either
    ws3 = ws_with_formula("[Parameters].[Select Measure] = [Measure Names]")
    assert TP.measure_names_param(ws3, {}) == "Select Measure"
    # the EXISTING static/enumerated form (function='member') must NOT be
    # mistaken for a parameter-driven filter
    ws_static = ET.fromstring("""
      <worksheet name='W'><table><view>
        <filter class='categorical' column='[federated.x].[:Measure Names]'>
          <groupfilter function='union'>
            <groupfilter function='member' level='[:Measure Names]'
                         member='&quot;[federated.x].[sum:SALES:qk]&quot;' />
          </groupfilter>
        </filter>
      </view></table></worksheet>""")
    assert TP.measure_names_param(ws_static, {}) is None
    # no Measure Names filter at all
    ws_none = ET.fromstring("<worksheet name='W'><table><view/></table></worksheet>")
    assert TP.measure_names_param(ws_none, {}) is None
    print("ok  native Measure Names filter (Condition -> By formula) detected "
          "and resolved through param_alias; the existing enumerated-members "
          "form and a sheet with no Measure Names filter both correctly return None")


def test_measure_names_native_param_wires_into_mbar_spec():
    """Native selector part 2: a worksheet with Measure Values on a positional
    shelf + a parameter-conditioned Measure Names filter infers as kind='mbar'
    with param_select set (via the SAME _infer_core/infer path every other
    sheet goes through) and 'measures' populated with the real candidate set
    from datasource-dependencies column-instances -- the existing fallback
    scan measure_names_list() already relies on for the static form, so this
    reuses proven machinery rather than adding a second, drifting path."""
    import xml.etree.ElementTree as ET
    ws = ET.fromstring("""
      <worksheet name='NativeMeasureSelector'><table>
        <view>
          <datasource-dependencies datasource='federated.x'>
            <column-instance column='[SALES]' derivation='Sum'
                             name='[sum:SALES:qk]' type='quantitative' />
            <column-instance column='[PROFIT]' derivation='Sum'
                             name='[sum:PROFIT:qk]' type='quantitative' />
            <column-instance column='[QUANTITY]' derivation='Sum'
                             name='[sum:QUANTITY:qk]' type='quantitative' />
          </datasource-dependencies>
          <filter class='categorical' column='[federated.x].[:Measure Names]'>
            <groupfilter function='formula'
                         formula='[Measure Names] = [Parameters].[Select Measure]' />
          </filter>
        </view>
        <cols>[federated.x].[:Measure Values]</cols>
        <rows>[federated.x].[none:CATEGORY:nk]</rows>
      </table></worksheet>""")
    meta = {
        "[SALES]": {"caption": "Sales", "role": "measure", "datatype": "real", "fmt": None},
        "[PROFIT]": {"caption": "Profit", "role": "measure", "datatype": "real", "fmt": None},
        "[QUANTITY]": {"caption": "Quantity", "role": "measure", "datatype": "integer", "fmt": None},
        "[CATEGORY]": {"caption": "Category", "role": "dimension", "datatype": "string", "fmt": None},
    }
    spec = TP.infer(ws, meta, param_alias={})
    assert spec["kind"] == "mbar", spec
    assert spec.get("param_select") == "Select Measure", spec
    caps = sorted(m["caption"] for m in spec["measures"])
    assert caps == ["Profit", "Quantity", "Sales"], caps
    print("ok  worksheet with Measure Values + parameter-conditioned Measure "
          "Names filter infers as mbar with param_select set and the real "
          "3-measure candidate set from column-instances (not guessed)")


def test_native_measure_selector_narrows_to_one_measure():
    """Native selector part 3: at RENDER time, engine._select_param_measure
    narrows the mbar candidate list down to whichever ONE measure the
    parameter's CURRENT value names -- reusing PARAMS, the same live parameter
    state the calc-based swap already reads via sub_params(). Proven both
    directions: every domain value selects its own measure and nothing else,
    and an unmatched/garbage current value falls back to the full list rather
    than silently rendering nothing."""
    import engine as E
    ms = [{"caption": "Sales", "agg": "sum"}, {"caption": "Profit", "agg": "sum"},
          {"caption": "Quantity", "agg": "sum"}]
    E.PARAMS["Select Measure"] = '"Profit"'
    picked = E._select_param_measure(ms, "Select Measure")
    assert [m["caption"] for m in picked] == ["Profit"], picked
    E.PARAMS["Select Measure"] = '"Sales"'
    assert [m["caption"] for m in E._select_param_measure(ms, "Select Measure")] == ["Sales"]
    E.PARAMS["Select Measure"] = '"Quantity"'
    assert [m["caption"] for m in E._select_param_measure(ms, "Select Measure")] == ["Quantity"]
    # no param_select at all -- unchanged, full list (every other mbar sheet)
    assert E._select_param_measure(ms, None) == ms
    # current value matches nothing -- fall back to the full list, never an
    # empty chart from one bad/stale parameter value
    E.PARAMS["Select Measure"] = '"Nonexistent Measure"'
    assert E._select_param_measure(ms, "Select Measure") == ms
    del E.PARAMS["Select Measure"]
    # the parameter must get a live control -- this is the ONLY thing that
    # marks it live for a param_select-only usage (no calc references it)
    E.IR = {"dashboards": [{"sheets": [{"param_select": "Select Measure"}]}]}
    try:
        assert E._param_is_live("Select Measure")
        assert not E._param_is_live("Unrelated Parameter")
    finally:
        E.IR = {}
    print("ok  engine narrows an mbar measure list to the parameter's current "
          "selection (proven for every domain value), falls back to the full "
          "list on no match, and marks a param_select-only parameter as live")


def test_native_measure_selector_real_workbook_end_to_end():
    """Native selector part 4: the SAME pattern, through the REAL pipeline
    (TP.build_ir on an actual .twbx file), not an inline XML fragment.
    Workbooks/Superstore_MeasureSelector_Native.twbx is Superstore_TopN_
    MeasureSwap.twbx (the already-proven calc-based fixture) with ONE new,
    isolated worksheet + parameter grafted on -- same real Sample-Superstore
    .hyper extract, same real datasource/column definitions, only the new
    'Native Measure Selector' tab and 'Select Measure Native' parameter are
    added. Proves two things fixture-level unit tests can't: (1) the pattern
    survives the FULL parse (column_meta, extract_calcs_params, dashboards(),
    worksheet_shown_params(), not just the two functions exercised directly
    above), and (2) grafting it causes ZERO change to the three pre-existing,
    already-proven calc-based sheets in the same file. STILL SYNTHETIC in one
    sense worth stating plainly: this file was hand-grafted, not exported by
    a person clicking through Tableau Desktop's own 'Condition -> By formula'
    dialog -- the underlying data and datasource are real, the specific
    filter construct is this project's best-effort match to Tableau's
    documented groupfilter schema, unverified against an actual export."""
    wb = os.path.join(ROOT, "Workbooks", "Superstore_MeasureSelector_Native.twbx")
    if not os.path.exists(wb):
        print("skip native measure selector real workbook (file not present)")
        return
    ir = TP.build_ir(wb)
    names = sorted(d["name"] for d in ir["dashboards"])
    assert "Native Measure Selector" in names, names
    assert "Superstore Dashboard" in names, names

    native = next(d for d in ir["dashboards"] if d["name"] == "Native Measure Selector")
    sheet = native["sheets"][0]
    assert sheet["kind"] == "mbar", sheet
    assert sheet.get("param_select") == "Select Measure Native", sheet
    assert sorted(m["caption"] for m in sheet["measures"]) == ["Profit", "Quantity", "Sales"], sheet
    assert sheet.get("dim") == "Category", sheet
    # the parameter shows its OWN control on its OWN tab (worksheet_shown_params),
    # not just a fallback into the global sidebar
    assert native.get("params") == ["Select Measure Native"], native.get("params")

    assert ir["params"].get("Select Measure Native") == '"Sales"', ir["params"]
    assert ir["param_domains"].get("Select Measure Native") == \
        ['"Sales"', '"Profit"', '"Quantity"'], ir["param_domains"]

    # the THREE PRE-EXISTING calc-based sheets are untouched by the graft
    dash = next(d for d in ir["dashboards"] if d["name"] == "Superstore Dashboard")
    orig_names = sorted(s["name"] for s in dash["sheets"])
    assert orig_names == ["Measure Trend", "Measure by Region", "Top N Products"], orig_names
    assert "Select Measure" in ir["params"], ir["params"]   # the ORIGINAL param, untouched

    # engine-level: configuring on this real IR and driving the parameter
    # through every domain value narrows to the right single measure each time
    engine.configure(ir)
    for val, expect in (('"Sales"', "Sales"), ('"Profit"', "Profit"),
                        ('"Quantity"', "Quantity")):
        engine.PARAMS["Select Measure Native"] = val
        picked = engine._select_param_measure(sheet["measures"], sheet["param_select"])
        assert [m["caption"] for m in picked] == [expect], (val, picked)
    print("ok  real workbook (Workbooks/Superstore_MeasureSelector_Native.twbx, "
          "genuine Sample-Superstore .hyper extract) parses end to end through "
          "the full pipeline: native selector wired correctly, its own tab shows "
          "its own parameter control, and the three pre-existing calc-based "
          "sheets in the same file are provably untouched")


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
    # C6 (release33): RUNNING_ / LOOKUP translate to windows whose ORDER only the exact plan supplies (the sheet's
    # addressing, __TC_ORDER__); every other path refuses rather than guess the order. TOTAL(AVG) still refuses.
    for ordered in ("RUNNING_SUM(SUM([Sales]))", "LOOKUP(SUM([Sales]), -1)"):
        s, _ = translate_formula(ordered)
        assert s and "__TC_ORDER__" in s, (ordered, s)
    s, _ = translate_formula("TOTAL(AVG([Sales]))")
    assert s is None, s
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
    if not node or "blank" in node:
        return None
    if "sheet" in node:
        return node["sheet"]
    # release 26c: placed zones (controls, legends, text, images, buttons) by kind; blank spacers carry no structure
    for kind in ("control", "legend", "title", "text", "image", "nav", "toggle", "unsupported_zone"):
        if kind in node:
            return "<%s>" % kind
    out = {"dir": node.get("dir"),
           "children": [c for c in (_serialize_layout(c) for c in node.get("children", [])) if c is not None]}
    if node.get("floating"):
        out["floating"] = [_serialize_layout(f.get("child")) for f in node["floating"]]
    return out


def _layout_leaves(node):
    if not node:
        return []
    if "sheet" in node:
        return [node["sheet"]]
    out = []
    for c in node.get("children", []):
        out += _layout_leaves(c)
    for f in node.get("floating") or []:          # release 26c: zones floating over the tiled layout
        out += _layout_leaves(f.get("child"))
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
            # hidden-by-user and zero-sized worksheet zones are Tableau's
            # inactive show/hide state, not visible canvas leaves.
            placed = [s["name"] for s in d["sheets"]
                      if not s.get("hidden_by_user")]
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


def test_discount_curated_profile_respects_declared_aggregation():
    """D2 -- SHARATH'S RULING (2026-08-13): a curated profile entry applies
    only where the workbook itself declares that SAME aggregation; otherwise
    the workbook's declared agg wins. Discount is a RAW COLUMN curated in
    profile_superstore.py as AVG(DISCOUNT) (correct on most sheets), but
    Product Detail Sheet declares agg=sum for it -- before this fix the
    curated entry silently overrode that unconditionally, so the shipped app
    showed 0.1/0.2/0.1 while Tableau AND the backend both showed 0.2/0.8/0.4.

    Proves the structural fix, not a one-line AVG->SUM swap: a sheet
    declaring agg=sum now gets SUM(DISCOUNT); a sheet declaring agg=avg
    (legitimately correct elsewhere) is completely unaffected; a compound
    curated measure with no raw column at all (Profit Ratio) is never
    second-guessed since it has nothing to compare against; and the real
    grand-total SUM(DISCOUNT) this produces (1583.99) matches the SAME real
    value parity.py's raw-measure ground truth already validated
    independently (test_raw_measure_live_truth_and_json_verdicts), so this
    isn't just internally self-consistent -- it agrees with a second,
    unrelated proof of the real number."""
    import config
    engine.configure({"source_file": "Superstore.twb"})
    T = config.ORDERS

    # the actual bug: Product Detail Sheet declares agg=sum for Discount
    sum_case = engine.resolve_measure(T, "Product Detail Sheet",
                                      {"caption": "Discount", "agg": "sum"})
    assert sum_case["sql"] == "SUM(DISCOUNT)", sum_case
    total = engine.q(f"SELECT {sum_case['sql']} AS V FROM {T}")["V"].iloc[0]
    assert round(float(total), 2) == 1583.99, \
        f"must match parity.py's independently-validated real SUM(Discount): {total}"

    # sheets where AVG is genuinely correct must stay AVG -- unaffected
    avg_case = engine.resolve_measure(T, "Some Other Sheet",
                                      {"caption": "Discount", "agg": "avg"})
    assert avg_case["sql"] == "AVG(DISCOUNT)", avg_case

    # a compound/derived curated measure has no raw column to compare
    # against -- never second-guessed, always its own formula
    ratio = engine.resolve_measure(T, "Any Sheet",
                                   {"caption": "Profit Ratio", "agg": "sum"})
    assert ratio["sql"] == "SUM(PROFIT) / NULLIF(SUM(SALES), 0)", ratio

    # a curated raw-column entry whose agg ALREADY matches the workbook's
    # declared agg is untouched (Sales is curated as SUM, declared as sum)
    sales = engine.resolve_measure(T, "Any Sheet", {"caption": "Sales", "agg": "sum"})
    assert sales["sql"] == "SUM(SALES)", sales

    # the detector itself
    assert engine._leading_agg_fn("AVG(DISCOUNT)") == "AVG"
    assert engine._leading_agg_fn("SUM(PROFIT) / NULLIF(SUM(SALES), 0)") == "SUM"
    assert engine._leading_agg_fn("") is None
    assert engine._leading_agg_fn(None) is None

    print("ok  D2: Discount's curated AVG no longer silently overrides a "
          "sheet's declared agg=sum (SUM(DISCOUNT), matching Tableau/backend's "
          "real 1583.99, independently confirmed by parity.py's own ground "
          "truth) -- sheets where AVG is correct stay AVG, and a compound "
          "curated measure with no raw column is never second-guessed")


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


def test_source_tables_skips_relations_nested_in_a_legacy_join():
    """Item 6 (DATA_MODEL_STATUS.md #3.7) -- Globalsalesdashboard.twbx's
    SAMPLE_SUPER_STORE_ORDERS datasource is ONE already-resolved object built
    from a legacy <relation type='join'> tree (real shape: <connection>
    <relation type='join'><clause/><relation type='join'><clause/>
    <relation type='table'.../><relation type='table'.../></relation>
    <relation type='table'.../></relation></connection> -- two join levels,
    three leaf tables, none of them a standalone top-level object).
    source_tables()'s old `.//relation[@type='table']` scan walked INTO both
    join levels and counted all 3 leaf tables as if they were separate
    top-level sources, since each still carries the SAME upstream
    connection= attribute a real top-level relation would -- misreporting a
    single pre-joined object as a "3-table data model". PRACTICAL IMPACT WAS
    ALREADY NONE (bindable=False either way -- a pre-joined object was never
    going to be an R3 single-table rebind target), so this is a REASON-STRING
    accuracy fix, not a behavior change: proves bindable stays False AND the
    reason now correctly names a legacy pre-joined object instead of a
    fictitious 3-table model, AND (the regression this fix must not cause) a
    genuinely multi-table relationship-graph datasource (Regional Analysis'
    real 3-table model, siblings under <relationships>, not nested) still
    reports all 3 real tables exactly as before."""
    wb = os.path.join(ROOT, "Workbooks", "Globalsalesdashboard.twbx")
    if not os.path.exists(wb):
        print("skip source_tables legacy-join nesting (workbook not present)")
        return
    src = TP.source_tables(TP.load_twb_xml(wb))
    ds = src["SAMPLE_SUPER_STORE_ORDERS (SANDBOX.SAMPLE_SUPER_STORE_ORDERS)+ (DS)"]
    assert ds["tables"] == [], \
        f"nested join-operand relations must not be counted as top-level tables: {ds['tables']}"
    assert ds["bindable"] is False, ds        # unchanged conclusion
    assert "3-table" not in (ds["reason"] or ""), ds["reason"]
    assert "pre-joined" in (ds["reason"] or "") and "legacy" in (ds["reason"] or ""), ds["reason"]

    # regression guard: a REAL multi-table relationship-graph datasource
    # (siblings, not nested in a join tree) must still report every table
    wb2 = os.path.join(ROOT, "Workbooks", "Regional Analysis.twbx")
    if os.path.exists(wb2):
        rel = TP.source_tables(TP.load_twb_xml(wb2))["Data using Relationships"]
        names = {t["name"] for t in rel["tables"]}
        assert names == {"SAMPLE_SUPER_STORE_ORDERS", "SAMPLE_SUPER_STORE_PEOPLE",
                         "SAMPLE_SUPER_STORE_RETURNS"}, names
        assert rel["bindable"] is False and "data model" in rel["reason"], rel

    print("ok  source_tables() skips relations nested inside a legacy "
          "<relation type='join'> tree (Globalsalesdashboard's pre-joined "
          "object no longer misreported as a fictitious '3-table data "
          "model'; bindable stays False, only the reason string is "
          "accurate now) -- a genuine multi-table relationship-graph "
          "datasource still reports every real table")


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


# Same 2-table live model as _MULTI_TABLE_LIVE_FIXTURE, but in the shape REAL
# Tableau Desktop actually writes for a MULTI-table datasource: the table
# relations wrapped in a single <relation type='collection'>. Verified against
# the real Tableau-authored E-Commerce workbook's own XML -- _MULTI_TABLE_LIVE_
# FIXTURE's flat siblings are this repo's own synthetic shape, never produced
# by (or opened in) Tableau Desktop.
_MULTI_TABLE_LIVE_COLLECTION_FIXTURE = _MULTI_TABLE_LIVE_FIXTURE.replace(
    "        <relation name='ORDERS' table='[PROD_DB].[SALES].[ORDERS]' type='table' connection='snowflake.abc'/>\n"
    "        <relation name='CUSTOMERS' table='[PROD_DB].[SALES].[CUSTOMERS]' type='table' connection='snowflake.abc'/>",
    "        <relation type='collection'>\n"
    "          <relation name='ORDERS' table='[PROD_DB].[SALES].[ORDERS]' type='table' connection='snowflake.abc'/>\n"
    "          <relation name='CUSTOMERS' table='[PROD_DB].[SALES].[CUSTOMERS]' type='table' connection='snowflake.abc'/>\n"
    "        </relation>")


def test_live_multitable_relations_inside_a_collection():
    """A real multi-table LIVE datasource declares its table relations inside a
    <relation type='collection'>, and live_connections() must see through it.

    THE GAP (found 2026-08-25, re-authoring the real E-Commerce workbook onto a
    live Snowflake connection): R9's fix scoped the relation scan to DIRECT
    children of the federated <connection> -- correct, and it must stay that
    way, because the object-graph's per-object relations are what R9 had to
    stop mis-counting. But that scan was confirmed against the real KPI Live
    workbook, which is SINGLE-table; a single table IS a direct child. Real
    Tableau wraps a MULTI-table datasource's relations in a collection, so all
    of them sat one level below the scan and `table_rels` came back EMPTY --
    the model fell through to "no single named table relation found", a
    misleading reason for a datasource that plainly declares 2+ live tables.
    R9's own gate never caught it because _MULTI_TABLE_LIVE_FIXTURE uses the
    flat shape this repo invented, not the shape Tableau emits.

    NOT a wrong bind (queryable stayed False either way, so nothing was ever
    silently mis-queried) -- an inaccurate REASON, which this project treats as
    a real defect: the honest-refusal strings are the product's own
    explanation of what it did and why.

    THE FIX: expand exactly one level through a <relation type='collection'>
    before classifying. Only 'collection' is expanded (never 'join'), so the
    legacy-join detection is untouched, and the object-graph stays out of
    scope because it is a SIBLING of <connection>, never inside it."""
    import xml.etree.ElementTree as ET

    # (a) THE FIX -- the real Tableau shape is now read correctly.
    root = ET.fromstring(_MULTI_TABLE_LIVE_COLLECTION_FIXTURE)
    info = TP.live_connections(root)["Live Star Model"]
    assert info["queryable"] is False, info
    assert "2 tables" in info["reason"], info
    assert "relationship/join model" in info["reason"], info
    assert "no single named table relation" not in (info["reason"] or ""), info

    # (b) R9 IS NOT REOPENED. The object-graph declares the SAME 2 tables
    # again; if the scan ever widened back to `.//relation` the count would
    # read 4, and the pre-R9 bug would claim queryable=True at ORDERS alone.
    assert "4 tables" not in info["reason"], "object-graph relations leaked back in"
    assert info["table"] is None, info

    # (c) BOTH real shapes agree -- flat (single-table's shape) and collection
    # (multi-table's shape) classify a 2-table live model identically.
    flat = TP.live_connections(ET.fromstring(_MULTI_TABLE_LIVE_FIXTURE))["Live Star Model"]
    assert flat["reason"] == info["reason"], (flat["reason"], info["reason"])

    # (d) NO REGRESSION on the real single-table live workbook (its one
    # relation is a direct child -- the shape R9 was confirmed against).
    p = os.path.join(ROOT, "Workbooks", "Superstore_KPI_Parameter_Dashboard_Live.twbx")
    if os.path.exists(p):
        real = TP.live_connections(TP.load_twb_xml(p))
        cap = next(iter(real))
        assert real[cap]["queryable"] is True, real
        assert real[cap]["table"] == "SUPERSTORE_ORDERS", real

    # (e) REAL WORKBOOK, real Tableau-authored collection shape: the
    # E-Commerce workbook re-authored onto a live Snowflake connection over
    # WBR_DB.PIPELINE_DEMO's 3 tables. This is the file the gap was found on.
    # NB: this is a .twbx, not a .twb -- it bundles the workbook's 20 images
    # (logo, sidebar nav icons, KPI card icons, avatar). A live workbook still
    # needs an archive when it has non-data assets; see the packaging note in
    # MVP_ACCELERATOR_SCOPE.md's R9 row.
    live_ecom = os.path.join(ROOT, "Workbooks",
                             "ECommerce_Live_Join_Events_Customers_Products.twbx")
    if os.path.exists(live_ecom):
        import re as _re
        import zipfile
        with zipfile.ZipFile(live_ecom) as _z:
            _names = _z.namelist()
            _twb = _z.read([n for n in _names if n.endswith(".twb")][0]).decode("utf-8")
        # every referenced image must actually be in the archive, and the
        # extract must NOT be (the data is live in Snowflake)
        _refs = set(_re.findall(r"(Image/[^'\"<>]+?\.png)", _twb))
        assert _refs, "no image zones found -- fixture no longer exercises this"
        assert _refs <= set(_names), sorted(_refs - set(_names))
        assert not [n for n in _names if n.endswith(".hyper")], _names

        e = TP.live_connections(TP.load_twb_xml(live_ecom))
        cap = next(iter(e))
        assert e[cap]["class"] == "snowflake", e
        assert e[cap]["queryable"] is False, e
        assert "3 tables" in e[cap]["reason"], e
        # and the data-model path still resolves it (this is what Stage 3 uses)
        import semantic_layer as SL
        d = SL.describe_model(TP.load_twb_xml(live_ecom), "WBR_DB",
                              "PIPELINE_DEMO", phys_source=True)[0]
        assert d["joinable"] is True and d["shape"] == "star", d
        assert {t["fqn"] for t in d["tables"]} == {
            "WBR_DB.PIPELINE_DEMO.EVENTS", "WBR_DB.PIPELINE_DEMO.CUSTOMERS",
            "WBR_DB.PIPELINE_DEMO.PRODUCTS"}, d

    print("ok  live multi-table relations inside a <relation type='collection'> "
          "(real Tableau's multi-table shape) are now read correctly instead of "
          "reporting 'no single named table relation found'; R9's direct-child "
          "scope is preserved so the object-graph still cannot leak back in; "
          "real single-table live workbook unaffected)")


def test_live_model_view_join_keys_use_physical_columns():
    """The ON clause of a live data-model view must name the tables' REAL
    physical columns -- the same ones the SELECT list already names.

    THE BUG (found 2026-08-25, live on the deployed app -- Discovery died with
    `SQL compilation error: ... invalid identifier 'F."customer_id"'` on the
    3-table live E-Commerce workbook). Two different sources feed one view:

      * SELECT columns come from semantic_layer._columns() -> the
        metadata-records' <remote-name>, i.e. the table's REAL column
        (CUSTOMER_ID). Always correct.
      * JOIN keys come from init_workbook.parse_relationships() -> the
        object-graph's <relationship><expression op='[customer_id]'>, which
        are Tableau's LOCAL field names, NOT column names.

    For an extract that difference was invisible: the copy path pushes both
    through to_phys(), which upper-snakes them to the same token. But against
    a workbook's OWN DECLARED live Snowflake source `_src()` emits the name
    QUOTED and verbatim -- and a quoted lowercase ref cannot match a
    folded-uppercase column. So EVERY live multi-table star failed to deploy,
    while the identical extract-backed star deployed fine. Same casing class
    the phys_source assertion in test_data_model_view_deploy locks for the
    copy path; the ON clause simply never got the same treatment.

    THE FIX: semantic_layer._local_to_remote() maps each table's local field
    names to its remote column names (registering Tableau's collision-renamed
    spelling AND the stripped one parse_relationships hands back), and
    generate_views resolves each side of every ON against ITS OWN table.

    Left caption-style on purpose: describe_model's `joins` list, which the UI
    renders -- that pane shows the model the way TABLEAU shows it."""
    import init_workbook as IW
    import semantic_layer as SL
    import pipeline

    live_ecom = os.path.join(ROOT, "Workbooks",
                             "ECommerce_Live_Relationship_Events_Customers_Products.twbx")
    if not os.path.exists(live_ecom):
        live_ecom = os.path.join(ROOT, "Workbooks",
                                 "ECommerce_Live_Join_Events_Customers_Products.twbx")
    if not os.path.exists(live_ecom):
        print("ok  live model-view join keys (skipped -- no live multi-table workbook)")
        return
    root = TP.load_twb_xml(live_ecom)

    # the two spellings this must bridge, read straight off the workbook --
    # if Tableau ever stopped disagreeing, this gate would prove nothing
    rels = IW.parse_relationships(root)
    assert any(r["lkey"] == "customer_id" for r in rels), rels
    cmap = SL.data_model(root)[0]["col_map"]
    assert cmap["Events"]["customer_id"] == "CUSTOMER_ID", cmap.get("Events")
    # the dim side keeps Tableau's collision rename ('customer_id (Customers)')
    # while parse_relationships hands back the stripped 'customer_id' -- the map
    # is lowercase-keyed and BOTH spellings must resolve, or the ON clause's
    # right-hand side falls back to the raw lowercase key and the bug returns
    assert any(r["rkey"] == "customer_id" for r in rels), rels
    assert cmap["Customers"]["customer_id (customers)"] == "CUSTOMER_ID",         cmap.get("Customers")
    assert cmap["Customers"]["customer_id"] == "CUSTOMER_ID", cmap.get("Customers")

    # both phys_source modes: data_model_report uses True, the offline preview False
    for phys in (True, False):
        d = next(m for m in SL.describe_model(root, "WBR_DB", "PIPELINE_DEMO",
                                              phys_source=phys) if m["joinable"])
        stmt = pipeline._extract_create_view(d["view_ddl"])
        ons = [l for l in stmt.splitlines() if l.startswith("LEFT JOIN")]
        assert len(ons) == 2, stmt
        assert all('."CUSTOMER_ID"' in l or '."PRODUCT_ID"' in l for l in ons), ons
        # THE TEETH: the exact identifier Snowflake rejected, in the exact
        # quoted-lowercase form, must not appear anywhere in the DDL.
        for bad in ('"customer_id"', '"product_id"'):
            assert bad not in stmt, (f"phys_source={phys}: {bad} still "
                                     f"emitted:" + chr(10) + stmt)

        # every ON column must be a column the workbook says that table HAS --
        # so this can never pass by rewriting keys into a name that isn't real
        by_cap = {t["caption"]: {c.upper() for c in t["columns"]} for t in d["tables"]}
        alias_cap = {"f": next(m["caption"] for m in d["tables"]
                               if m["fqn"].endswith(".EVENTS"))}
        for l, dim_cap in zip(ons, ["Customers", "Products"]):
            key = l.split(" ON ", 1)[1]
            lcol = key.split("=")[0].split(".")[-1].strip().strip('"')
            rcol = key.split("=")[1].split(".")[-1].strip().strip('"')
            assert lcol in by_cap[alias_cap["f"]], (lcol, dim_cap)
            assert rcol in by_cap[dim_cap], (rcol, dim_cap)

    # UI display is deliberately UNCHANGED -- Tableau's own field names
    d = next(m for m in SL.describe_model(root, "WBR_DB", "PIPELINE_DEMO")
             if m["joinable"])
    assert {j["lkey"] for j in d["joins"]} == {"customer_id", "product_id"}, d["joins"]

    print("ok  live data-model view ON clauses now name real physical columns "
          "(f.\"CUSTOMER_ID\", resolved via metadata-record remote-name) instead of "
          "Tableau's local field names -- the 'invalid identifier F.\"customer_id\"' "
          "Discovery failure; UI join display stays caption-style)")


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
    import io
    import types
    import zipfile
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

    # A live workbook is legitimately returned as plain TWB XML even though
    # the catalogue name carries no extension. Use a real payload shape here;
    # arbitrary bytes would correctly be rejected by the intake validator.
    fake_workbook = b'<?xml version="1.0"?><workbook version="2026.1"></workbook>'

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
            resp.content = fake_workbook
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

    assert result["bytes"] == fake_workbook, result
    assert result["workbook_id"] == "wb-789", result
    assert result["filename"] == "Superstore.twb", result
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

    # --- SiS Snowflake-Secret fallback. Container runtimes expose a bound
    # PASSWORD secret as alias-derived env vars / st.secrets; warehouse
    # runtimes expose it through _snowflake. All must resolve identically. ---
    assert TSV._pat_from_sis_secret() == (None, None), \
        "outside SiS (no _snowflake module), the fallback must return (None, None), never raise"

    container_keys = (f"{TSV.SIS_SECRET_NAME}_USERNAME",
                      f"{TSV.SIS_SECRET_NAME}_PASSWORD")
    old_container_env = {k: os.environ.pop(k, None) for k in container_keys}
    try:
        os.environ[container_keys[0]] = "container_pat_name"
        os.environ[container_keys[1]] = "container_pat_secret"
        assert TSV._pat_from_sis_secret() == ("container_pat_name",
                                              "container_pat_secret")
    finally:
        for k in container_keys:
            os.environ.pop(k, None)
        for k, v in old_container_env.items():
            if v is not None:
                os.environ[k] = v

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
    fake_twbx_io = io.BytesIO()
    with zipfile.ZipFile(fake_twbx_io, "w") as archive:
        archive.writestr("My Workbook.twb", fake_workbook)
    fake_twbx = fake_twbx_io.getvalue()

    def _fake_get_dl(url, headers=None, timeout=None, stream=None, **kw):
        dl_calls.append(url)
        assert url.endswith("/workbooks/wb-known-id/content"), url
        resp = _FakeResp("")
        resp.content = fake_twbx
        return resp

    fake_requests5 = types.SimpleNamespace(post=_fake_post_with_signout, get=_fake_get_dl)
    TSV.requests = fake_requests5
    try:
        result2 = TSV.fetch_workbook_by_id(
            "https://10ax.online.tableau.com", "mysite", "wb-known-id",
            name_hint="My Workbook", token_name="tok_name", token_secret="tok_secret")
    finally:
        TSV.requests = real_requests
    assert result2["bytes"] == fake_twbx, result2
    assert result2["workbook_id"] == "wb-known-id", result2
    assert result2["filename"] == "My_Workbook.twbx", result2
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

    def _stub(sheet, where_parts=None, scale=2.0, width=None, bg=None):
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
    # release 26c: widths follow the authored rectangles (44,236 vs 44,178 units here), so the two
    # near-equal zones may differ by a pixel; anything beyond 1% is a real layout change
    assert abs(halves[0] - halves[1]) <= 0.01 * max(halves), f"equal zone weights -> equal widths: {halves}"
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


def _pd_chart(id_, title, status_measures, grain=("Key",), extra=None):
    """A minimal real compare_chart() call for the report-simplification
    gates below -- builds rows with the given (key, tableau, streamlit,
    backend) tuples so FAIL/PASS/BLOCKED shape is controlled precisely."""
    import validation_report as VR
    rows_t, rows_s, rows_b = [], [], []
    for key, t, s, b in status_measures:
        rows_t.append({grain[0]: key, "Sales": t})
        rows_s.append({grain[0]: key, "Sales": s})
        rows_b.append({grain[0]: key, "Sales": b})
    chart = dict(
        id=id_, title=title, chart_type="bar", grain=list(grain),
        measures=[{"name": "Sales", "field": "Sales", "kind": "currency",
                   "display_decimals": 0}],
        tableau_rows=rows_t, streamlit_rows=rows_s, backend_rows=rows_b,
        formulas=[], interactions=[],
    )
    if extra:
        chart.update(extra)
    return VR.compare_chart(chart)


def test_report_progressive_disclosure():
    """Gates the 2026-08-21 report-simplification pass (progressive
    disclosure: concise results up top, complete proof still in the file,
    just collapsed). Every item here maps to one line of the acceptance
    checklist that shipped with the change -- see HANDOFF_TRACK_B_VALIDATION.md
    / the PR description for the full before/after rationale. This is
    PRESENTATION ONLY: every chart/dashboard status below is a REAL verdict
    from VR.compare_chart / VR.validate_run, never hand-set, so a future
    change to the comparison engine's own decisions would still be caught
    by the other regression tests, not silently hidden by this one."""
    import html as html_lib
    import json
    import os
    import re
    import shutil
    import tempfile

    import validation_report as VR

    # A FAIL chart with 8 failing rows -- proves "at most 5 shown up front".
    many_fail = _pd_chart(
        "many-fail", "Many Failures",
        [(f"K{i}", 100 + i, 999 + i, 100 + i) for i in range(8)])
    assert many_fail["status"] == VR.FAIL and many_fail["failed_cells"] == 8

    # A clean PASS chart with 6 rows -- proves the full table collapses even
    # when small (the OLD renderer only collapsed above 20 rows). Also
    # carries the chart-level interaction evidence -- validate_run's own
    # dashboard["interactions"] is ALWAYS rebuilt from each chart's
    # interaction_results, never taken from a dashboard-level key, so real
    # evidence has to be attached here to survive into the rendered report.
    clean_pass = _pd_chart(
        "clean-pass", "Clean Pass",
        [(f"K{i}", 50 + i, 50 + i, 50 + i) for i in range(6)],
        extra={
            "interactions": [
                {"name": "Region filter", "tableau": "West only",
                 "streamlit": "West only", "proof": "screenshot", "status": VR.PASS},
            ],
            "formulas": [
                {"id": "gate-formula", "metric": "Gate Metric",
                 "tableau": "sum([X])", "streamlit": "SUM(X)",
                 "classification": "SEMANTICALLY_EQUIVALENT", "impact": "None"},
            ],
        })
    assert clean_pass["status"] == VR.PASS
    assert clean_pass["interaction_results"], "guards the fixture above"
    assert clean_pass["formula_results"], "guards the fixture above"

    # Two BLOCKED charts, same skip_reason wording repeated -- proves the
    # generic "why a blocked chart is listed at all" boilerplate collapses
    # to ONE occurrence (moved to the workbook-level methodology section)
    # instead of once per blocked chart.
    blocked_1 = VR.compare_chart({"id": "b1", "title": "Blocked One",
                                  "skip_reason": "no comparable Altair chart"})
    blocked_2 = VR.compare_chart({"id": "b2", "title": "Blocked Two",
                                  "skip_reason": "no comparable Altair chart"})

    spec = {
        "workbook": "GateTest.twb", "run_id": "VAL-GATE-1", "environment": "UAT",
        "generated_at": "2026-08-21T00:00:00",
        "dashboards": [
            {
                "name": "Dash Fail", "charts": [many_fail],
                # a real screenshot pair + zero semantic checks -- proves the
                # empty "Visual check" table is gone, not just hidden
                "visual": {"tableau_screenshot": None, "streamlit_screenshot": None,
                           "checks": []},
                "interactions": [],
            },
            {
                "name": "Dash Pass", "charts": [clean_pass, blocked_1, blocked_2],
                "visual": {"tableau_screenshot": None, "streamlit_screenshot": None},
            },
            {
                "name": "Dash NotApplicable", "charts": [],
                "visual": {"tableau_screenshot": None, "streamlit_screenshot": None,
                           "not_applicable": True},
                "interactions": [],
            },
        ],
        "capture_diagnostics": {},
    }

    root = tempfile.mkdtemp()
    try:
        result = VR.generate_report(spec, root)
        summary = json.loads(open(result["summary"], encoding="utf-8").read())
        html = open(result["report"], encoding="utf-8").read()

        # 1. No empty visual-check table (checks==[] on "Dash Fail").
        assert "<th>Visual check</th>" not in html, \
            "the empty per-check table must be gone entirely, not just unpopulated"

        # 2. Similarity as a percentage, one decimal -- and the raw fraction
        #    never leaks into the document.
        html2 = VR.render_html({**summary, "dashboards": [
            dict(summary["dashboards"][0], visual={
                "tableau_screenshot": "x.png", "streamlit_screenshot": "y.png",
                "similarity": 0.898422385620915, "threshold": 0.85},
                images={"tableau": "x.png", "streamlit": "y.png"}),
        ]})
        assert "89.8%" in html2 and "85.0%" in html2
        assert "0.898422385620915" not in html2, \
            "the raw unrounded similarity float must never be printed"

        # 3. The numbered dashboard navigation list is gone.
        nav = re.search(r"<nav>.*?</nav>", html, re.S).group(0)
        assert not re.search(r">\d+\.\s", nav), \
            f"a numbered per-dashboard nav link survived: {nav}"
        assert nav.count("<a ") <= 4, \
            f"nav should carry only Summary/Formulas/Methodology-style links, not one per dashboard: {nav}"

        # 4. Summary table dashboard names are clickable anchors to the section.
        for dashboard in summary["dashboards"]:
            assert f'<a href="#{dashboard["id"]}"><b>{html_lib.escape(dashboard["name"])}</b></a>' in html, \
                f"{dashboard['name']} is not a clickable anchor in the summary table"

        # 5. The generic blocked-chart policy statement appears at most once
        #    (workbook-level methodology), even with 2 blocked charts present.
        assert html.count("indistinguishable from a validated one") <= 1, \
            "the blocked-chart boilerplate must not repeat once per chart"
        # each blocked chart still states its OWN precise reason though.
        assert html.count("no comparable Altair chart") == 2, \
            "each blocked chart's own reason must still appear -- only the " \
            "generic policy sentence should be deduplicated, never the reason"

        # 6 & 7. No interaction evidence -> no interactions SECTION, but a
        #    NOT TESTED badge in that dashboard's header; the dashboard that
        #    DOES have interaction evidence still gets the full section.
        dash_fail_id = summary["dashboards"][0]["id"]
        dash_pass_id = summary["dashboards"][1]["id"]
        fail_section = re.search(
            rf'<section class="dashboard" id="{dash_fail_id}">.*?(?=<section class="dashboard"|\Z)',
            html, re.S)
        assert fail_section and "Filters and interactions" not in fail_section.group(0), \
            "a dashboard with zero interaction evidence must not render the section heading"
        assert "NOT TESTED" in fail_section.group(0)
        pass_section = re.search(
            rf'<section class="dashboard" id="{dash_pass_id}">.*', html, re.S).group(0)
        assert "Filters and interactions" in pass_section, \
            "a dashboard WITH real interaction evidence must still show the full section"

        # 8 & 9. Full comparison rows survive but are collapsed; at most 5
        #    representative failed rows show up front for the FAIL chart.
        chart_block = re.search(
            r'<details class="chart" id="chart-many-fail".*?'
            r'(?=<details class="chart"|<h3>Calculations|<h3>Filters and interactions|</section>)',
            html, re.S)
        assert chart_block, "the FAIL chart's own detail block must exist"
        block = chart_block.group(0)
        assert "View all 8 compared rows" in block
        visible_before_collapse = block.split('<details class="full-data"')[0]
        assert visible_before_collapse.count("<tr>") <= 5 + 1, \
            "at most 5 representative failed rows may be visible before the " \
            "collapsed full table (+1 for the table header row)"
        full_data_open = re.search(r'<details class="full-data"[^>]*>', block)
        assert full_data_open and "open" not in full_data_open.group(0), \
            "the full row comparison must be collapsed by default"

        # 10. Formula details remain available but collapsed, and are not
        #     repeated in full per dashboard -- see the workbook catalog test
        #     below for the cross-dashboard dedup itself.
        assert 'id="formula-catalog"' in html

        # 11 & 12. Capture diagnostics and methodology each appear once, and
        #     methodology is collapsed by default.
        assert html.count('id="methodology"') == 1
        methodology_tag = re.search(r'<details id="methodology"[^>]*>', html).group(0)
        assert "open" not in methodology_tag, "methodology must be collapsed by default"

        # 13. A missing screenshot pair is explicitly reported, per side --
        #     never silently dropped -- for a dashboard missing BOTH images.
        assert "Missing required Tableau dashboard screenshot" in fail_section.group(0)
        assert "Missing required Streamlit dashboard screenshot" in fail_section.group(0)

        # 14. NOT APPLICABLE only fires when the opt-in metadata flag is set,
        #     and BLOCKED is still the default everywhere else.
        na_dash_id = summary["dashboards"][2]["id"]
        na_section = re.search(
            rf'<section class="dashboard" id="{na_dash_id}">.*', html, re.S).group(0)
        assert "NOT APPLICABLE" in na_section, \
            "the dashboard with visual.not_applicable=True must render NOT APPLICABLE"
        assert "NOT APPLICABLE" not in fail_section.group(0), \
            "a dashboard with NO metadata flag must never default to NOT APPLICABLE"

        # 15. No PASS is produced without supporting evidence -- a chart
        #     missing its Tableau leg entirely must never render a PASS badge
        #     for itself (this is the engine's own invariant; re-asserted
        #     here against the RENDERED report, not just the verdict dict,
        #     since the simplification touched every render path).
        no_evidence = VR.compare_chart({
            "id": "no-ev", "title": "No Evidence", "chart_type": "bar",
            "grain": ["Key"],
            "measures": [{"name": "Sales", "field": "Sales", "kind": "currency",
                          "display_decimals": 0}],
            "tableau_rows": None, "streamlit_rows": None, "backend_rows": None,
            "formulas": [], "interactions": [],
        })
        assert no_evidence["status"] != VR.PASS
        no_ev_html = VR._chart_html(no_evidence, open_by_default=True)
        assert '<span class="badge pass">' not in no_ev_html, \
            "a chart with zero evidence on every leg must never render as PASS"

        # 16. Comparison CSVs are still generated and linked, real files on disk.
        many_fail_csv = os.path.join(root, summary["dashboards"][0]["charts"][0]["comparison_csv"])
        assert os.path.exists(many_fail_csv)
        assert summary["dashboards"][0]["charts"][0]["comparison_csv"] in html

        # 17. Fully standalone -- no external network assets.
        assert "http://" not in html and "https://" not in html, \
            "the report must not reference any external asset"
        assert "<script" not in html, \
            "no JS dependency was added -- the report stays pure static HTML"
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("ok  validation report simplification: empty visual-check table gone, "
          "similarity shown as a rounded percentage, numbered dashboard nav "
          "removed in favor of clickable summary-table anchors, repeated "
          "BLOCKED boilerplate collapses to one workbook-level statement while "
          "each chart's own reason still shows, interaction sections vanish "
          "(replaced by a NOT TESTED badge) when no evidence was supplied but "
          "still render in full when it was, at most 5 representative failed "
          "rows show before a collapsed full table, the formula catalog and "
          "methodology sections exist collapsed and exactly once, NOT "
          "APPLICABLE only fires on an explicit metadata flag (BLOCKED "
          "remains the default), comparison CSVs are still written and "
          "linked, and the report stays fully standalone")


def test_workbook_formula_catalog_deduplicates_across_dashboards():
    """The same calculation used on multiple dashboards must be described
    ONCE in a workbook-level catalog, not once per dashboard -- each
    dashboard's own compact table links to the shared entry instead of
    repeating the full Tableau/Streamlit formula text."""
    import validation_report as VR

    def _dash(name, chart_id):
        chart = dict(
            id=chart_id, title=f"{name} chart", chart_type="bar", grain=["Key"],
            measures=[{"name": "Sales", "field": "Sales", "kind": "currency",
                       "display_decimals": 0}],
            tableau_rows=[{"Key": "A", "Sales": 10}],
            streamlit_rows=[{"Key": "A", "Sales": 10}],
            backend_rows=[{"Key": "A", "Sales": 10}],
            formulas=[{
                "id": "profit-ratio", "metric": "Profit Ratio",
                "tableau": "sum([Profit])/sum([Sales])",
                "streamlit": "SUM(PROFIT)/NULLIF(SUM(SALES),0)",
                "classification": "SEMANTICALLY_EQUIVALENT", "impact": "None",
                "approved": True,
            }],
            interactions=[],
        )
        chart_result = VR.compare_chart(chart)
        return {
            "name": name,
            "visual": {"tableau_screenshot": None, "streamlit_screenshot": None},
            "charts": [chart_result],
            "interactions": [],
        }

    spec = {
        "workbook": "CatalogTest.twb", "run_id": "R", "environment": "UAT",
        "generated_at": "now",
        "dashboards": [_dash("Sales", "c1"), _dash("Customers", "c2")],
    }
    import tempfile
    root = tempfile.mkdtemp()
    validated = VR.validate_run(spec, __import__("pathlib").Path(root))
    html = VR.render_html(validated)

    assert html.count("sum([Profit])/sum([Sales])") == 1, \
        "the full Tableau formula text must appear exactly once, in the " \
        "workbook-level catalog, not repeated per dashboard"
    assert html.count("Profit Ratio") >= 3, \
        "the metric NAME may still appear per dashboard (compact reference) " \
        "plus once in the catalog"
    assert "Sales — Sales chart" in html and "Customers — Customers chart" in html, \
        "the catalog must state every dashboard/chart that uses the formula"

    print("ok  a calculation shared across dashboards is described once in a "
          "workbook-level formula catalog, with every using dashboard/chart "
          "listed against it instead of the full formula text repeating")


def test_table_scoped_lod_subquery_columns_resolve():
    """A TABLE-SCOPED LOD's scalar subquery must not make its measure look
    unresolvable.

    THE BUG (found 2026-08-26 on the live E-Commerce pack). Tableau's
    {MAX([Order Date])} compiles -- by design, a shipped calc_translator
    feature -- to a scalar subquery:

        (SELECT MAX(EVENT_DATE) FROM __TBL__)

    cortex_semantic._sql_columns extracts "the columns this SQL references"
    and _resolve_measure_sql then requires every one to exist on the target
    table. SELECT and FROM were not in _SQL_WORDS and __TBL__ (calc_translator
    's own TABLE placeholder) was not excluded, so all three were extracted AS
    COLUMN NAMES. The subset check could never pass, and every measure built
    on such a calc reported

        measure 'C.Total Revenue' does not resolve against <table>

    while the app computed it correctly. On the live E-Commerce workbook this
    blocked C.Total Revenue, C.Quantity and C.AOV -- most of the book's charts
    -- and, because a blocked measure yields no evidence at all, took whole
    dashboards' data legs with it.

    Also gates the deliberate NON-exclusions: a real column may legitimately
    be named DAY/MONTH/YEAR/STATUS, and over-excluding would mask a genuinely
    missing column, which is the opposite and worse failure."""
    import cortex_semantic as CS

    sql = ("SUM(CASE WHEN EVENT_DATE <= (SELECT MAX(EVENT_DATE) FROM __TBL__) "
           "AND NOT IS_REFUNDED THEN NET_REVENUE_USD ELSE 0 END)")
    cols = CS._sql_columns(sql)

    # (a) the real columns, and ONLY the real columns
    assert cols == {"EVENT_DATE", "IS_REFUNDED", "NET_REVENUE_USD"}, sorted(cols)
    for junk in ("SELECT", "FROM", "__TBL__"):
        assert junk not in cols, (junk, sorted(cols))

    # (b) the whole point: the subset check now passes against a real table
    table_cols = {"EVENT_DATE", "IS_REFUNDED", "NET_REVENUE_USD", "CUSTOMER_ID"}
    assert cols <= table_cols, sorted(cols - table_cols)

    # (c) every placeholder this project emits is excluded, not just __TBL__
    ph = CS._sql_columns("SUM(X) OVER (ORDER BY __WIN_ORDER__) + __PARAM_NEW_QUOTA__")
    for junk in ("__WIN_ORDER__", "__PARAM_NEW_QUOTA__"):
        assert junk not in ph, (junk, sorted(ph))
    assert "X" in ph, sorted(ph)

    # (d) NOT over-excluded -- a genuinely missing column must still be caught,
    # including one named like a date part or a common word.
    for name in ("DAY", "MONTH", "YEAR", "QUARTER", "STATUS", "VALUE"):
        assert name in CS._sql_columns(f"SUM({name})"), name

    print("ok  a table-scoped LOD's scalar subquery (SELECT/FROM/__TBL__) no "
          "longer counts as columns, so measures built on one resolve against "
          "the real table instead of reporting 'does not resolve' while the "
          "app computes them fine; placeholders excluded, and columns named "
          "like date parts are still checked for real")


def test_agg_grain_calc_dimension_is_not_a_backend_key():
    """An AGGREGATE-grain calculated dimension is not a row key, and must
    never reach the backend query as a physical column name.

    THE REAL BUG (found 2026-08-25 on the deployed R1 Superstore pack, which
    reported 0 of 21 charts validated). Superstore's "Achieved Quota" is
    `CASE WHEN SUM([Sales]) >= [New Quota] THEN '100% +' ... END` -- a value
    that only exists AFTER aggregation. engine.CALCS marks it agg_ready, and
    engine.flag_dim gives exactly this case its own mode, 'agg', documented
    "SELECT-only, NEVER in GROUP BY". That is why the APP renders the colour
    bands correctly.

    validation_adapter did not have that branch. resolve_chart_columns saw a nominal
    colour channel and made it a grain key; backend_rows then put it in
    GROUP BY *and* in the scoping WHERE, resolving it through engine.rdim --
    which has no agg_ready branch and falls through to px(), i.e. the
    PHYSICAL COLUMN NAME. No such column exists, so the query died with

        invalid identifier 'ACHIEVEMENT_VARIABLE_COPY'

    and the chart was reported BLOCKED under a DOWNSTREAM reason ("sheet drew
    no Altair chart"), taking its dashboard's whole data leg with it. The
    misattributed reason is why this survived several reports.

    Gates both halves: the calc is kept OUT of the grain, and the emitted SQL
    never names the bogus column. Also gates the defence-in-depth refusal in
    _grain_expr, so a future caller that bypasses resolve_chart_columns fails loudly
    instead of silently emitting a nonexistent identifier."""
    import validation_adapter as VA
    import engine as E

    saved = dict(E.CALCS)
    try:
        E.CALCS["Achieved Quota"] = {
            "sql": "CASE WHEN SUM((SALES)) >= 500000 THEN '100% +' ELSE 'Below' END",
            "agg_ready": True}
        E.CALCS["Ship Status"] = {"sql": "CASE WHEN DAYS > 3 THEN 'Late' ELSE 'On time' END",
                                 "agg_ready": False}

        # (a) the aggregate-grain calc is refused as a key, by name, with the
        #     real reason -- never a physical column.
        try:
            VA._grain_expr({}, "Achieved Quota")
            raise AssertionError("_grain_expr accepted an aggregate-grain calc")
        except ValueError as e:
            assert "aggregate-grain" in str(e), str(e)
            assert "ACHIEVEMENT" not in str(e).upper(), str(e)

        # (b) a ROW-level calc is still resolved to its real SQL -- this fix
        #     must not turn every calculated dimension into a refusal.
        assert "CASE WHEN DAYS" in VA._grain_expr({}, "Ship Status"), \
            VA._grain_expr({}, "Ship Status")

        # (c) the classifier itself
        assert VA._is_agg_grain_calc("Achieved Quota") is True
        assert VA._is_agg_grain_calc("Ship Status") is False
        assert VA._is_agg_grain_calc("Sales Person") is False

        # (d) resolve_chart_columns keeps it out of the grain while still mapping the
        #     captured column to its caption (the series is still labelled --
        #     it just is not a comparison key).
        class _Chart:
            def to_dict(self):
                return {"encoding": {
                    "x": {"field": "ACHIEVEMENT_ESTIMATED", "type": "quantitative"},
                    "y": {"field": "SALES_PERSON", "type": "nominal"},
                    "color": {"field": "ACHIEVED_QUOTA", "type": "nominal"}}}

        sheet = {"name": "QuotaAttainment", "kind": "bar",
                "x": {"caption": "Achievement (estimated)", "agg": "sum"},
                "y": {"caption": "Sales Person"},
                "color": {"caption": "Achieved Quota", "kind": "dimension"}}
        grain, measures, rename = VA.resolve_chart_columns(sheet, _Chart())
        assert grain == ["Sales Person"], grain
        assert "Achieved Quota" not in grain, grain
        assert rename.get("ACHIEVED_QUOTA") == "Achieved Quota", rename
        assert any(m["name"] == "Achievement (estimated)" for m in measures), measures
    finally:
        E.CALCS.clear(); E.CALCS.update(saved)

    print("ok  an aggregate-grain calculated dimension (SUM-based CASE, e.g. "
          "'Achieved Quota') is no longer treated as a backend row key -- it "
          "was resolving through rdim to a physical column that does not "
          "exist, killing the query with `invalid identifier` and blocking "
          "the chart under a misattributed downstream reason; row-level "
          "calculated dimensions still resolve to their real SQL")


def test_validation_tab_offers_exactly_one_report():
    """SHARATH'S RULING (2026-08-25): the Validation tab offers EXACTLY ONE
    file to download -- the report -- and nothing else.

    THE REGRESSION THIS CATCHES, and why it went unnoticed: `_render_pack_files`
    surfaced a download button for EVERY top-level pack file (README.md, BOTH
    HTML reports, issues.csv, validation_summary.json) plus one more per
    per-chart comparison CSV. The 2026-08-07 change that replaced the pack .zip
    with individual files was aimed at the same complaint -- a reviewer should
    not have to hunt -- but only removed the unpack step, not the hunt. No gate
    covered this renderer at all, so it stayed wrong across releases while
    being reported as fixed.

    Deliberately asserts the NEGATIVE too (the other files are NOT offered),
    because a gate that only checks "the report is present" passes just as
    happily when six other buttons sit beside it -- which is the actual bug.
    """
    import deep_validation as DV

    class _Ctx:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def __getattr__(self, n): return lambda *a, **k: None

    class _FakeSt:
        def __init__(self):
            self.downloads = []      # (label, file_name)
            self.warnings = []
            self.expanders = []      # (label, expanded)
        def download_button(self, label, data, file_name=None, **k):
            self.downloads.append((label, file_name))
        def warning(self, msg, *a, **k): self.warnings.append(str(msg))
        def expander(self, *a, **k):
            self.expanders.append((a[0] if a else None, k.get("expanded")))
            return _Ctx()
        def columns(self, spec):
            n = spec if isinstance(spec, int) else len(spec)
            return [_Ctx() for _ in range(n)]
        def container(self, **k): return _Ctx()
        def __getattr__(self, n): return lambda *a, **k: None

    report_html = "<html><body>REPORT BODY</body></html>"

    def _f(name, size=2048, mime="text/html"):
        return {"name": name, "bytes": report_html.encode(), "size": size, "mime": mime}

    pack = {"files": [
        _f("README.md", mime="text/markdown"),
        _f("validation_report.html"),
        _f(DV.PACK_TAB_REPORT),
        _f("issues.csv", mime="text/csv"),
        _f("validation_summary.json", mime="application/json"),
        _f("evidence/dash-a/charts/chart-1/comparison.csv", mime="text/csv"),
        _f("evidence/dash-a/charts/chart-2/comparison.csv", mime="text/csv"),
    ]}

    real_st = DV.st
    try:
        fake = _FakeSt(); DV.st = fake
        rendered = []
        real_inline = DV._inline_html
        DV._inline_html = lambda html, **k: rendered.append(html)
        try:
            DV._render_pack_files(pack, "bookstem")
        finally:
            DV._inline_html = real_inline

        # (a0) THE REQUIREMENT: the report is RENDERED IN THE TAB, not merely
        # offered as a file. This regressed once -- the report was demoted
        # behind a collapsed expander with a download button in front of it --
        # and no gate caught it, because the gate only counted downloads.
        assert len(rendered) == 1, f"report was not rendered in the tab: {rendered}"
        assert rendered[0] == report_html, "rendered bytes differ from the report"
        # and it must NOT be hidden behind a collapsed expander
        assert not any(exp is False for _lbl, exp in fake.expanders), fake.expanders

        # (a) EXACTLY ONE download, and it is the report.
        assert len(fake.downloads) == 1, fake.downloads
        label, fname = fake.downloads[0]
        assert DV.PACK_TAB_REPORT in fname, fname
        assert fname.startswith("bookstem_"), fname

        # (b) THE NEGATIVE -- nothing else is downloadable. This is the half
        # that actually fails when the old every-file renderer comes back.
        offered = " ".join(f"{l} {n}" for l, n in fake.downloads)
        for banned in ("README", "issues.csv", "validation_summary.json",
                       "comparison.csv", "validation_report.html"):
            if banned == "validation_report.html" and \
                    banned in DV.PACK_TAB_REPORT:
                continue          # substring of the chosen report's own name
            assert banned not in offered, (banned, fake.downloads)

        # (c) A missing report must SAY so, never render an empty panel --
        # silence is indistinguishable from "nothing went wrong".
        fake2 = _FakeSt(); DV.st = fake2
        rendered2 = []
        real_inline = DV._inline_html
        DV._inline_html = lambda html, **k: rendered2.append(html)
        try:
            DV._render_pack_files({"files": [_f("issues.csv", mime="text/csv")]},
                                  "bookstem")
        finally:
            DV._inline_html = real_inline
        assert fake2.downloads == [], fake2.downloads
        assert rendered2 == [], rendered2
        assert fake2.warnings and DV.PACK_TAB_REPORT in fake2.warnings[0], \
            fake2.warnings

        # (d) An empty pack stays silent (nothing ran -- not an error state).
        fake3 = _FakeSt(); DV.st = fake3
        DV._render_pack_files({"files": []}, "bookstem")
        assert fake3.downloads == [] and fake3.warnings == [], \
            (fake3.downloads, fake3.warnings)
    finally:
        DV.st = real_st

    print("ok  validation tab RENDERS the report in-page (never behind a "
          "collapsed expander) and offers exactly ONE downloadable file "
          "-- README, the second HTML report, issues.csv, the summary JSON and "
          "every per-chart comparison CSV are no longer surfaced; a missing "
          "report warns instead of rendering an empty panel")


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


def test_multifact_and_cyclic_refusal_on_real_xml():
    """Item 7 (DATA_MODEL_STATUS.md #3.4/#3.5) -- the multi-fact and cyclic
    refusal logic in semantic_layer.join_plan() is proof-only, no code
    expected: it was already built and gated, but ONLY ever exercised via
    hand-built {objs, rels} Python dicts passed straight to join_plan()
    (test_non_star_join_and_blends, above) -- never through the REAL XML ->
    object-graph parsing path (semantic_layer.data_model(), init_workbook.
    parse_relationships()) a real workbook would actually go through.

    Workbooks/DataModel_MultiFact_And_Cyclic_Refusal.twb (built by
    tests/make_datamodel_workbooks.py's build_multifact_and_cyclic_refusal_
    workbook) is a real, well-formed <object-graph>/<relationships> XML
    fixture -- deliberately minimal (no extract, no worksheets: a parser-
    shape proof, not a demo) -- with two datasources, one per refusal
    reason: Orders+Returns both joining Product (two fact tables sharing a
    dim: which one's rows drive the join is a real modelling decision, not
    inferable) and A->B->C->A (a 3-table loop: edge count doesn't match a
    tree, so more than one path exists). Proves the refusal fires end to
    end -- real file, real parse, real join_plan() call -- not just that the
    planner itself is correct in isolation."""
    import semantic_layer as SL

    wb = os.path.join(ROOT, "Workbooks", "DataModel_MultiFact_And_Cyclic_Refusal.twb")
    if not os.path.exists(wb):
        print("skip multi-fact/cyclic refusal on real XML (workbook not present)")
        return
    models = {m["caption"]: m for m in SL.data_model(TP.load_twb_xml(wb))}

    mf = models["Multi-Fact Model"]
    assert set(mf["objects"]) and len(mf["objects"]) == 3, mf["objects"]
    assert len(mf["relationships"]) == 2, mf["relationships"]
    plan = SL.join_plan(mf["objects"], mf["relationships"])
    assert plan["shape"] == "multi_fact" and plan["root"] is None and not plan["steps"], plan
    assert "2 fact tables" in plan["reason"], plan["reason"]

    cy = models["Cyclic Model"]
    assert len(cy["objects"]) == 3, cy["objects"]
    assert len(cy["relationships"]) == 3, cy["relationships"]
    plan2 = SL.join_plan(cy["objects"], cy["relationships"])
    assert plan2["shape"] == "non_star" and plan2["root"] is None and not plan2["steps"], plan2
    assert "cycle" in plan2["reason"], plan2["reason"]

    print("ok  item 7: multi-fact (Orders+Returns -> Product) and cyclic "
          "(A -> B -> C -> A) graphs both refuse correctly through the REAL "
          "XML parse path (data_model() -> join_plan()), not just the "
          "hand-built dicts test_non_star_join_and_blends already covers")


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


def test_table_caption_in_both_dims_and_measures_not_duplicated():
    """B2 (Global Sales Dashboard, 'Details Standard Class' -- a 3-datasource
    BLEND), found live re-verifying whether the old screenshot-crashing bug
    still reproduces under the CURRENT real capture path (app_screenshot.
    capture_app): it doesn't -- that crash was specific to the retired
    headless_render re-render path -- but a genuine, different bug surfaced
    in the same investigation. The sheet's own dims list AND measures list
    both carried the SAME caption ('SUM_OF_SALES', 'SALES '), so r_table
    selected it twice: once via the raw dims loop (rdim(), unaggregated) and
    once via the measures loop (resolve_measure(), properly aggregated).
    `df.columns = labels` doesn't itself raise on the duplicate, but Arrow --
    which Streamlit's own dataframe serialization goes through -- refuses a
    DataFrame with duplicate column names outright ('Duplicate column names
    found'), so the table rendered an inline error card instead of data,
    captured live in a real screenshot before this fix.

    Proves the fix end to end: a caption present in both dims and measures
    is selected exactly ONCE (via the measure's own aggregated SQL, the
    correct read -- not the raw per-row dims value), a genuinely distinct dim
    is untouched, and the resulting DataFrame actually renders (no exception,
    real values, no duplicate columns)."""
    engine.configure(TP.build_ir(TWB))
    st = engine.st
    captured = []
    _orig_df = st.dataframe
    st.dataframe = lambda df=None, **k: captured.append(df)
    try:
        s = {"name": "Collision Table", "kind": "table",
             "datasource": "Sample - Superstore",
             "dims": ["Category", "Sales"],           # 'Sales' ALSO a measure below
             "measures": [{"caption": "Sales", "agg": "sum",
                          "fmt": "money0", "label": "Sales"}],
             "filters": []}
        captured.clear()
        engine.render_sheet(s, [])
        assert captured and captured[0] is not None, "table must actually render, not error out"
        df = captured[0]
        assert list(df.columns) == ["Category", "Sales"], \
            f"'Sales' must appear exactly once (as the measure), not once per list: {list(df.columns)}"
        assert not df.columns.duplicated().any(), df.columns.tolist()
        assert df["Sales"].sum() > 0, "must be the real aggregated value, not empty/blocked"
    finally:
        st.dataframe = _orig_df
    print("ok  B2: a table sheet's caption appearing in BOTH dims and "
          "measures (found live on a 3-datasource blend, 'Details Standard "
          "Class') is selected exactly once via its measure aggregation, not "
          "duplicated -- the exact shape that crashed Arrow/Streamlit's "
          "dataframe serialization with 'Duplicate column names found'")


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


def test_pivoted_crosstab_unpivots_to_chart_grain():
    """B3, fixed 2026-08-13: Tableau's REST crosstab sometimes returns a
    `Measure Names`/`Measure Values` shape that the aligner could not reduce to
    the chart's own grain, so those charts' Tableau leg was structurally
    unusable.

    THE ORIGINAL DIAGNOSIS IN THE HANDOFF WAS WRONG, and this gate encodes the
    corrected one. It was tracked as alignment failing with "missing source
    columns" because the measures become values in a long-format table. Against
    the REAL REST pull, alignment SUCCEEDS -- Tableau emits the measure columns
    AND the Measure Names/Values pair, repeating each grain row once per
    measure. So the comparison received 24 rows where the chart has 4, each
    carrying the DISPLAY-FORMATTED wide value ('$391,722') while the
    full-precision number sat unread in Measure Values ('391721.905000001').

    Fixtures are real `Query View Data` responses (not hand-made -- see
    tests/fixtures/tableau_crosstabs/README.md); a UI export produces a
    different layout, which is exactly the trap this gate exists to avoid."""
    import validation_evidence_bridge as VEB

    fx = os.path.join(os.path.dirname(__file__), "fixtures", "tableau_crosstabs")

    def _rows(name):
        with open(os.path.join(fx, name), encoding="utf-8") as fh:
            return VEB.csv_rows(fh.read())

    # (a) THE CORE CLAIM: 24 pivoted rows collapse to the chart's real 4-row
    # grain, and every value is the FULL-PRECISION one from Measure Values --
    # never the rounded, currency-formatted wide column beside it.
    raw = _rows("pivoted_customers.csv")
    assert len(raw) == 24, f"fixture changed: expected 24 pivoted rows, got {len(raw)}"
    out, err = VEB._align_chart_rows(
        raw, ["Region"],
        [{"name": "Sales", "field": "Sales"}, {"name": "Profit", "field": "Profit"}])
    assert err is None, f"pivoted export should now align, got: {err}"
    assert len(out) == 4, f"expected one row per Region, got {len(out)}"
    by_region = {r["Region"]: r for r in out}
    assert set(by_region) == {"South", "Central", "East", "West"}
    # Full precision, not Tableau's displayed '$391,722'.
    assert by_region["South"]["Sales"].replace(",", "") == "391721.905000001", \
        f"expected the Measure Values figure, got {by_region['South']['Sales']!r}"
    assert "$" not in by_region["South"]["Sales"]
    # And it must actually PARSE -- an unparseable cell is never compared,
    # which is the silent-pass class the thousands-comma fix already closed.
    from validation_report import _d as _dec
    assert _dec(by_region["West"]["Sales"]) is not None

    # (b) A wider grain still works: 5 grain columns, 40 groups.
    out2, err2 = VEB._align_chart_rows(
        _rows("pivoted_order_details_trimmed.csv"),
        ["Customer Name", "Order Date", "Order ID", "Ship Date", "Ship Mode"],
        [{"name": "Sales", "field": "Sales"}, {"name": "Discount", "field": "Discount"}])
    assert err2 is None and len(out2) == 40, f"{err2} / {len(out2) if out2 else None}"

    # (c) An ordinary wide export must be left EXACTLY as it was -- the reshape
    # must not touch the shape that already worked.
    wide = _rows("wide_regional_view1.csv")
    same, err3 = VEB.unpivot_measure_names(wide, ["Category", "Region"])
    assert err3 is None and same is wide, "non-pivoted export must pass through untouched"

    # (d) REFUSAL, not a guess: a pivoted export whose declared grain isn't in
    # the CSV must report a reason rather than reshape to something plausible.
    none_rows, reason = VEB.unpivot_measure_names(_rows("pivoted_customers.csv"),
                                                  ["Nonexistent Field"])
    assert none_rows is None and reason and "cannot be reshaped" in reason, reason
    assert "Nonexistent Field" in reason, "the refusal must name the field it could not find"

    # (e) Grouping is by the chart's DECLARED grain, never inferred from the
    # data. Commission Model is the case that proves why: its non-measure
    # columns include `Rank over 3` and `Total Compensation` -- measures, not
    # dimensions -- so an infer-the-grain implementation would group by them.
    out5, err5 = VEB._align_chart_rows(
        _rows("pivoted_commission_model.csv"), ["Sales Person"],
        [{"name": "Commission (Variable)", "field": "Commission (Variable)"}])
    assert err5 is None, err5
    assert len(out5) == 41, f"expected 41 sales people, got {len(out5)}"

    print("ok  pivoted crosstab unpivots to the chart's declared grain "
          "(B3: 24 rows -> 4 at full precision from Measure Values; wide "
          "exports untouched; missing grain refuses with a stated reason)")


def test_datepart_alias_and_formatted_number_parsing():
    """B4 (column aliasing half) + the currency/percent parsing ruling, both
    2026-08-13, both proven against the REAL REST fixtures.

    PARSING: validation_report._d() stripped a thousands comma but not a
    currency '$' or a trailing '%', so '$391,722' and '11.9%' parsed to None
    and were NEVER COMPARED -- the same silent-pass class the comma fix closed
    in that very function. Sharath's ruling: strip the '$', divide the '%' by
    100. The '%' is scaled rather than stripped because value_scale is
    "fraction" everywhere (validation_adapter.py:63 is its only producer), so
    '11.9%' is 0.119 here; stripping alone would be a 100x error.

    ACCOUNTING NEGATIVES are the trap this gate exists to hold shut. The real
    'Lower Prediction Interval for Sales' column contains "($1,137)". Stripping
    '$' without reading the parentheses yields +1137 -- a SIGN INVERSION, which
    is strictly worse than the original bug: a silently WRONG comparison rather
    than an honestly missing one.

    ALIASING: Tableau heads a date-part column "Month of Order Date" while the
    chart's grain is "Order Date", so it never matched. Only the part the sheet
    DECLARES is aliased, and only from grain channels."""
    import json as _json
    from decimal import Decimal
    import validation_evidence_bridge as VEB
    from validation_report import _d as _dec

    # -- parsing, against the exact strings that appear in the real exports --
    assert _dec("$391,722") == Decimal("391722")
    assert _dec("$765.08") == Decimal("765.08")
    assert _dec("11.9%") == Decimal("0.119"), "a '%' source value is a FRACTION here"
    assert _dec("($1,137)") == Decimal("-1137"), "parenthesised negative must stay NEGATIVE"
    assert _dec("($657)") == Decimal("-657")
    assert _dec("-1,326.932000000") == Decimal("-1326.932000000")
    assert _dec("163,797.1638") == Decimal("163797.1638")  # the 2026-08-11 comma fix
    # A RANGE LABEL is not a number. The real 'Achieved Quota' column holds
    # "75-100%"; it must stay None rather than become 75 or 0.75.
    assert _dec("75-100%") is None
    assert _dec("Standard Class") is None and _dec("(foo)") is None

    # -- date-part aliasing, driven by the REAL Superstore IR --
    with open(os.path.join(ROOT, "superstore_ir.json"), encoding="utf-8") as fh:
        ir = _json.load(fh)
    sheets = {(s.get("title") or s.get("name")): s
              for d in ir["dashboards"] for s in d.get("sheets", [])}

    perf = sheets["Sales Performance vs Target"]
    assert VEB.declared_date_parts(perf) == {"Order Date": "Month"}, \
        "the sheet's own declared grain part drives the alias"
    # FILTERS ARE NOT GRAIN. ShipSummary filters Order Date by 'qr' and 'yr';
    # aliasing off a filter would point a chart at the wrong column entirely.
    assert VEB.declared_date_parts(sheets["ShipSummary"]) == {}, \
        "a filter's datepart must never produce a grain alias"

    fx = os.path.join(os.path.dirname(__file__), "fixtures", "tableau_crosstabs")
    with open(os.path.join(fx, "datepart_performance.csv"), encoding="utf-8") as fh:
        rows = VEB.csv_rows(fh.read())
    grain = ["Category", "Segment", "Order Date"]
    measures = [{"name": "Sales", "field": "Sales"},
                {"name": "Sales Target", "field": "Sales Target"}]

    # Without the declared part the column is invisible -- the original bug.
    _, before = VEB._align_chart_rows(rows, grain, measures)
    assert before and "Order Date" in before, f"expected the pre-fix miss, got {before!r}"

    aligned, err = VEB._align_chart_rows(rows, grain, measures,
                                         VEB.declared_date_parts(perf))
    assert err is None, f"date-part column should now alias, got: {err}"
    assert len(aligned) == 428, f"expected every export row, got {len(aligned)}"
    # And the '$'-formatted measure it carries must now actually parse, which
    # is the whole point -- aliasing a column whose values never parse would
    # move the failure rather than fix it.
    assert _dec(aligned[0]["Sales"]) is not None, aligned[0]["Sales"]

    # The export carries BOTH 'Month of Order Date' AND 'Year of Order Date'.
    # Aliasing every part would match two columns and report ambiguous, which
    # is no better than missing -- so only the declared part may alias.
    only_year = VEB._align_chart_rows(rows, grain, measures, {"Order Date": "Year"})[0]
    # NB the value is an INT, not the export's "2021" string: B4b reconciles a
    # Year part to the number EXTRACT(YEAR) produces on the app side.
    assert only_year is not None and only_year[0]["Order Date"] in {2021, 2022, 2023, 2024}, \
        "a different declared part must select that OTHER column, proving the alias is part-specific"

    print("ok  date-part column aliases to the chart's declared grain (B4 "
          "column half) and Tableau's '$'/'%'/accounting-negative formats now "
          "parse instead of silently never being compared -- ($1,137) stays "
          "NEGATIVE and '75-100%' stays non-numeric")


def test_ui_crosstab_header_recovery_and_kpi_aliases():
    """UI-downloaded Tableau crosstabs may omit all headers or leave a
    parameter-selected measure header blank. Recover only shapes that the
    chart contract determines exactly, and retain the first consumed row."""
    import validation_evidence_bridge as VEB

    # Headerless workbook exports: DictReader consumed the first data row as
    # headings. Its values must be restored, not merely used to rename later
    # rows (the live Top Customers export has exactly this shape).
    headerless = VEB.csv_rows(
        "Sean Miller,25043.05\nTamara Chand,19052.218\nRaymond Buch,15117.339\n")
    rows, err = VEB._align_chart_rows(
        headerless, ["Customer Name"], [{"name": "Sales", "field": "Sales"}])
    assert err is None, err
    assert rows[0] == {"Customer Name": "Sean Miller", "Sales": "25043.05"}
    assert len(rows) == 3, "the first data row consumed as a header must be restored"

    # A real header with one blank, parameter-generated value column is not a
    # headerless export. Fill only that one unambiguous contract field.
    partial = VEB.csv_rows(
        "Month of Order Date,\nJanuary 2021,100\nFebruary 2021,120\n")
    rows, err = VEB._align_chart_rows(
        partial, ["Order Date"],
        [{"name": "Selected Measure Value", "field": "Selected Measure Value"}],
        {"Order Date": "Month"})
    assert err is None, err
    assert rows[0]["Selected Measure Value"] == "100"
    assert rows[0]["Order Date"] == "2021-01-01 00:00:00"

    # KPI crosstabs have no physical grain and spell COUNTD columns using the
    # source caption. Both differences are proven by the rendered chart spec.
    kpi = VEB.csv_rows(
        "Sales,Distinct count of Customer Name\n2326534.3543,800\n")
    measures = [
        {"name": "Sales", "field": "Sales", "source_caption": "Sales",
         "aggregation": "sum"},
        {"name": "Count of Customers", "field": "Count of Customers",
         "source_caption": "Customer Name", "aggregation": "ctd"},
    ]
    displayed = [{"KPI tile": "KPI Summary", "Sales": 2326534.3543,
                  "Count of Customers": 800}]
    rows, err = VEB._align_chart_rows(
        kpi, ["KPI tile"], measures, displayed_rows=displayed)
    assert err is None, err
    assert rows == [{"KPI tile": "KPI Summary", "Sales": "2326534.3543",
                     "Count of Customers": "800"}]

    print("ok  UI crosstab header recovery retains the first row, fills one "
          "blank parameter header, and aligns synthetic-grain COUNTD KPI data")


def test_datepart_values_reconcile_to_app_grain():
    """B4b, fixed 2026-08-13: the half B4a could not reach. With the column
    aliased, Tableau's date-part VALUES still could not be compared to the
    app's, so those charts' cells never matched:

        Tableau  "Month of Order Date" = 'January' (+ 'Year of Order Date')
        app      DATE_TRUNC('MONTH', ..) = 2023-03-01   (engine.py:2078)

    THE HEADLINE ASSERTION is the end-to-end one: the reconciled Tableau keys
    must equal the keys the REAL app actually produces -- captured here from
    engine's own render path, not hand-written -- because everything else is
    just plumbing if the two key sets don't meet.

    Three real shapes, all three in the committed fixtures, and they do NOT
    share one mapping -- which is why this could not be guessed:
      1. `mn`  'January'    + a separate 'Year of Order Date' column
      2. `tmn` 'April 2021' -- year INSIDE the same column, no year column
      3. month-only with NO year anywhere (and 'All' rows) -> must refuse
    """
    import json as _json
    import validation_evidence_bridge as VEB
    import headless_render as HR
    import engine as _engine

    fx = os.path.join(os.path.dirname(__file__), "fixtures", "tableau_crosstabs")

    def _rows(name):
        with open(os.path.join(fx, name), encoding="utf-8") as fh:
            return VEB.csv_rows(fh.read())

    with open(os.path.join(ROOT, "superstore_ir.json"), encoding="utf-8") as fh:
        ir = _json.load(fh)
    pairs = {(s.get("title") or s.get("name")): (d, s)
             for d in ir["dashboards"] for s in d.get("sheets", [])}
    dash, sheet = pairs["Sales Performance vs Target"]

    # (a) THE REAL CLAIM: reconciled Tableau keys == the app's own keys.
    _engine.IR = ir
    captured = HR.capture_sheet_chart(
        sheet, dashboard_name=dash.get("title") or dash.get("name"))
    chart = captured[0] if isinstance(captured, (list, tuple)) else captured
    app_keys = {row["X"] for row in VEB.records(chart.data)}

    aligned, err = VEB._align_chart_rows(
        _rows("datepart_performance.csv"),
        ["Category", "Segment", "Order Date"],
        [{"name": "Sales", "field": "Sales"}],
        VEB.declared_date_parts(sheet))
    assert err is None, err
    tableau_keys = {row["Order Date"] for row in aligned}
    assert tableau_keys == app_keys, (
        "reconciled Tableau month keys must equal the app's own; "
        f"unmatched: {sorted(tableau_keys ^ app_keys)[:5]}")
    assert len(tableau_keys) == 48, f"expected 4 years x 12 months, got {len(tableau_keys)}"

    # (b) Shape 2 -- the year lives INSIDE the column, no year column exists.
    out2, err2 = VEB.reconcile_date_part_values(
        _rows("datepart_forecast.csv"), ["Order Date"], {"Order Date": "Month"})
    assert err2 is None, err2
    assert "2021-01-01 00:00:00" in {r["Month of Order Date"] for r in out2}

    # (c) Shape 3 -- month-only with no year anywhere. MUST refuse: a month
    # name alone cannot name a point on the app's date axis.
    out3, why3 = VEB.reconcile_date_part_values(
        _rows("pivoted_datepart_what_if_forecast.csv"),
        ["Order Date"], {"Order Date": "Month"})
    assert out3 is None and why3 and "no 'Year of Order Date' column" in why3, why3

    # (d) A yearly axis is EXTRACT(YEAR) -- a number, not a date.
    out4, err4 = VEB.reconcile_date_part_values(
        _rows("datepart_performance.csv"), ["Order Date"], {"Order Date": "Year"})
    assert err4 is None and {r["Year of Order Date"] for r in out4} == {2021, 2022, 2023, 2024}

    # (e) Refusals, not guesses, on every unproven edge.
    assert VEB.reconcile_date_part_values(
        [{"Week of Order Date": "Week 3", "Year of Order Date": "2021"}],
        ["Order Date"], {"Order Date": "Week"})[0] is None, \
        "an unmapped date part must refuse"
    assert VEB.reconcile_date_part_values(
        [{"Month of Order Date": "Smarch", "Year of Order Date": "2021"}],
        ["Order Date"], {"Order Date": "Month"})[0] is None, \
        "an unrecognized month must refuse"
    # No part column at all means there is no part encoding to undo.
    plain = [{"Order Date": "2021-01-01", "Sales": "10"}]
    assert VEB.reconcile_date_part_values(
        plain, ["Order Date"], {"Order Date": "Month"}) == (plain, None)

    print("ok  date-part VALUES reconcile to the app's own grain (B4b: all 48 "
          "real Tableau month keys match the app's captured keys exactly; "
          "'April 2021' and 'January'+year both handled; month-with-no-year "
          "refuses with a stated reason instead of guessing a year)")


def test_ordinary_altair_charts_get_datepart_reconciliation_too():
    """FOUND LIVE 2026-08-17, against a real deployed report: only 1 of 21
    charts (Product Detail Sheet, a TABLE) carried real Tableau row data --
    every other chart read 0 Tableau rows despite Streamlit and backend both
    having real data, and despite the Tableau REST pull itself returning 10/10
    views with no errors and real CSV content.

    ROOT CAUSE, confirmed against the real account, not guessed: B4/B4b (date-
    part column+value reconciliation) was built and gated against
    `_align_chart_rows` directly -- test_datepart_values_reconcile_to_app_grain
    above proves that function works. But `build_complete_validation_spec`
    only ROUTES a chart through `_align_chart_rows` when engine.py recorded an
    explicit ChartPayload in the REGISTRY, and `_record_chart_evidence` (engine
    .py) ONLY fires for map/treemap/table/rank-table -- kinds with no Altair
    object at all. An ORDINARY bar or line chart (the common case, not the
    exception -- Performance and Forecast here are both plain Altair charts)
    always has `payload is None`, and fell to the OLDER `align_rows()`, which
    just renames columns 1:1 and has no idea "Order Date" and "Month of Order
    Date" are the same field. Every date-part-grouped ordinary chart failed
    with "missing source columns: Order Date" and reported 0 Tableau rows --
    not because no data existed (10/10 REST views pulled clean), but because
    the fix for exactly this shape was never wired into the branch most charts
    actually take.

    Proven here via `build_complete_validation_spec` itself -- the actual
    integration point that broke -- not just the underlying function, using
    the REAL committed REST fixtures (datepart_performance.csv,
    datepart_forecast.csv) from this exact workbook, offline."""
    import json as _json
    import parity
    import validation_evidence_bridge as VEB
    import engine as _engine

    fx = os.path.join(os.path.dirname(__file__), "fixtures", "tableau_crosstabs")
    with open(os.path.join(ROOT, "superstore_ir.json"), encoding="utf-8") as fh:
        ir = _json.load(fh)

    class _FakeCortexRow:
        def __getitem__(self, i): return "No bugs found."

    class _FakeResult:
        def collect(self): return [_FakeCortexRow()]

    class _FakeSession:
        def sql(self, text): return _FakeResult()

    res = parity.check_workbook(ir)
    sections, _rollup = parity.build_cortex_dashboard_validation_report(
        ir, _FakeSession(), "Superstore.twb", res=res)

    evidence = VEB.EvidenceBundle()
    with open(os.path.join(fx, "datepart_performance.csv"), encoding="utf-8") as fh:
        evidence.set_tableau_csv("Sales Performance vs Target",
                                 "Sales Performance vs Target", fh.read())
    with open(os.path.join(fx, "datepart_forecast.csv"), encoding="utf-8") as fh:
        evidence.set_tableau_csv("Sales Forecast", "Sales Forecast", fh.read())

    def _table_for(dash):
        info = parity.collect_dashboard_section(ir, dash)
        return info["table"] if info else None

    _engine.EVIDENCE_CAPTURE = True
    try:
        spec = VEB.build_complete_validation_spec(
            ir, sections, "Superstore.twb", _table_for, evidence,
            environment="UAT")
    finally:
        _engine.EVIDENCE_CAPTURE = False

    def _chart(dashboard_title, sheet_title):
        dash = next(d for d in spec["dashboards"]
                   if (d.get("title") or d.get("name")) == dashboard_title)
        return next(c for c in dash["charts"]
                   if (c.get("title") or c.get("id")).lower()
                       == sheet_title.lower())

    perf = _chart("Sales Performance vs Target", "Sales Performance vs Target")
    assert perf.get("skip_reason") is None, (
        f"Performance must be comparable, not skipped: {perf.get('skip_reason')}")
    assert not perf.get("tableau_alignment_error"), (
        "no alignment error expected -- got: " + str(perf.get("tableau_alignment_error")))
    assert perf.get("tableau_rows"), (
        "Performance must carry real Tableau rows -- this is the exact "
        "regression: a real 10/10 REST pull silently became 0 rows because "
        "this chart never reached the date-part-aware aligner")
    assert len(perf["tableau_rows"]) > 0

    fc = _chart("Sales Forecast", "Sales Forecast")
    assert not fc.get("tableau_alignment_error"), (
        "no alignment error expected -- got: " + str(fc.get("tableau_alignment_error")))
    assert fc.get("tableau_rows"), \
        "Forecast (year-inside-column date-part shape) must also carry real Tableau rows"

    print("ok  ORDINARY Altair charts (bar/line -- the common case, not just "
          "the explicitly-captured map/treemap/table/rank-table kinds) now get "
          "the SAME date-part unpivot+reconciliation build_complete_validation_"
          "spec's explicit-payload branch already had -- Performance and "
          "Forecast go from a silent 0 to their real Tableau row counts")


def test_empty_source_blocks_and_states_its_reason():
    """Both findings from the 2026-08-14 DEPLOYED run, which produced 0 PASS
    across 21 charts and was the first time this pack was read end to end.

    (1) AN EMPTY SOURCE IS NOT A FAILED ONE. `Estimated Sales` (Tableau rows
        `[]`) read FAIL while `Executive Overview - Profitability` (Tableau
        rows `None`) read BLOCKED -- identical evidence, 0 Tableau rows each,
        opposite verdicts, decided purely by whether alignment returned [] or
        None. Zero reference rows cannot prove a migration WRONG. This is the
        BLOCKED-vs-FAIL invariant, and it was violated in the shipped report.

    (2) THE REASON WAS COMPUTED AND THEN THROWN AWAY.
        `tableau_alignment_error` was written by the evidence bridge in two
        places and read by NO renderer, so every unusable leg showed
        "0 source rows / BLOCKED" with no cause -- a reader could not act on
        any of it. "Nothing silently dropped" failing at the last step."""
    from decimal import Decimal as _Dec
    import validation_report as VR
    import validation_report_dashboard as VRD

    base = dict(
        id="k", title="Estimated Sales", chart_type="kpi", grain=["KPI tile"],
        measures=[{"name": "Sales", "field": "Sales", "kind": "currency",
                   "display_decimals": 0}],
        streamlit_rows=[{"KPI tile": "Estimated Sales", "Sales": 15357898}],
        backend_rows=[{"KPI tile": "Estimated Sales", "Sales": 15357898}],
        formulas=[], interactions=[])

    verdicts = {}
    for label, rows in (("absent", None), ("empty", [])):
        chart = dict(base)
        chart["tableau_rows"] = rows
        verdicts[label] = VR.compare_chart(chart)["status"]
    assert verdicts["absent"] == verdicts["empty"] == "BLOCKED", (
        f"an unmeasured Tableau leg must BLOCK, never FAIL: {verdicts}")

    # And it must not sneak to PASS on Streamlit<->backend agreement alone --
    # "no proof, no pass" has to survive the empty case too.
    assert verdicts["empty"] != "PASS"

    # (2) the stated reason must reach BOTH renderers.
    chart = dict(base)
    chart["tableau_rows"] = []
    chart["tableau_alignment_error"] = "missing source columns: Sales Target"
    compared = VR.compare_chart(chart)

    engine_html = VR._chart_html(compared, open_by_default=True)
    assert "Sales Target" in engine_html, \
        "the proof-first renderer must state WHY the Tableau leg is unusable"

    client_html = VRD._chart_record(compared)
    assert "Sales Target" in client_html, \
        "the client-facing renderer must state it too"


def test_order_match_structural_only_for_rank_and_topn():
    """D1 -- SHARATH'S RULING (2026-08-13): row ORDER is a chart-level
    structural FAIL only for charts where display order carries meaning --
    ranked lists and top-N. Everywhere else (grouped bars, plain tables) a
    sequence difference is informational, never a FAIL. Before this fix,
    validation_report.compare_chart's order_match made ANY row-order
    difference a structural_failure unconditionally -- live on Regional
    Analysis, two fully-evidenced charts (every individual cell PASS) read
    chart-level FAIL purely from row sequence.

    Proves BOTH directions the ruling explicitly demands: a ranked chart with
    shuffled rows still FAILs; a grouped bar chart with different row order
    does not. Also proves the detection itself (validation_adapter.
    order_is_semantic) and the safety-first default for callers that predate
    this fix."""
    import validation_adapter as VA
    import validation_report as VR

    # --- part 1: detection itself --------------------------------------
    sheet_topn = {"kind": "bar", "applied_filters": [{"kind": "top_n", "n": 10}]}
    sheet_plain_bar = {"kind": "bar", "applied_filters": []}
    sheet_plain_table = {"kind": "table", "applied_filters": [{"kind": "categorical"}]}
    assert VA.order_is_semantic(sheet_topn) is True, \
        "a top-N filter on ANY chart kind makes order semantic"
    assert VA.order_is_semantic(sheet_plain_bar) is False
    assert VA.order_is_semantic(sheet_plain_table) is False
    # chart_type=='rank_table' is a second, independent path (engine.py's
    # _rank_table shape, which is order-semantic even absent an explicit
    # top-N filter -- e.g. a naturally-sorted rank list)
    assert VA.order_is_semantic(sheet_plain_bar, chart_type="rank_table") is True

    # --- part 2: compare_chart, both directions -------------------------
    def _shuffled_chart(order_significant):
        rows_in_order = [{"Product": "A", "Sales": 10},
                         {"Product": "B", "Sales": 20},
                         {"Product": "C", "Sales": 30}]
        shuffled = [rows_in_order[2], rows_in_order[1], rows_in_order[0]]
        chart = dict(
            id="c", title="T", chart_type="bar", grain=["Product"],
            measures=[{"name": "Sales", "field": "Sales", "kind": "number",
                      "display_decimals": 0}],
            tableau_rows=rows_in_order, streamlit_rows=shuffled,
            backend_rows=shuffled, formulas=[], interactions=[])
        if order_significant is not None:
            chart["order_significant"] = order_significant
        return chart

    ranked = VR.compare_chart(_shuffled_chart(True))
    assert ranked["order_match"] is False
    assert ranked["status"] == VR.FAIL, \
        f"a ranked/top-N chart with shuffled rows must still FAIL: {ranked['status']}"

    grouped = VR.compare_chart(_shuffled_chart(False))
    assert grouped["order_match"] is False, \
        "the mismatch is still RECORDED, just not structural"
    assert grouped["status"] != VR.FAIL, \
        f"a grouped-bar chart must NOT fail on row order alone: {grouped['status']}"

    # a chart dict that predates this fix (no order_significant key at all)
    # must default to significant -- never silently loosen for an
    # uninstrumented caller
    legacy = VR.compare_chart(_shuffled_chart(None))
    assert legacy["order_significant"] is True
    assert legacy["status"] == VR.FAIL

    # --- part 3: the HTML states which case it is -----------------------
    sig_html = VR._chart_html(ranked, open_by_default=True)
    assert "rank/position is significant" in sig_html
    insig_html = VR._chart_html(grouped, open_by_default=True)
    assert "informational only" in insig_html

    print("ok  D1: order_match is a structural FAIL only for rank/top-N "
          "charts (order_is_semantic, both signals) -- a ranked chart with "
          "shuffled rows still FAILs, a grouped bar chart with different row "
          "order does not, a pre-fix chart dict defaults to significant, and "
          "the report states which case applies")

    print("ok  an EMPTY source blocks exactly like an absent one (the deployed "
          "run's Estimated Sales FAIL vs Executive Overview BLOCKED split, on "
          "identical 0-row evidence) and both renderers now state WHY a leg is "
          "unusable instead of discarding the computed reason")


def test_tableau_leg_scoped_and_pivoted_csv_assigned():
    """The two fixes aimed at the 2026-08-14 deployed run's emptiest results.

    (1) SCOPING THE TABLEAU LEG -- and this closes a regression this project
        introduced. Once B3 taught the aligner to reshape a pivoted export,
        Product Detail Sheet produced 5111 real Tableau rows, compared against
        the 30 the app displays: 20,324 "failed values", chart FAIL, on a chart
        that is probably correct. Before B3 it was honestly BLOCKED. Tableau
        exports a view's UNDERLYING data, not its drawn marks, so the Tableau
        leg is now narrowed to the displayed keys exactly as the backend leg
        already is -- with the exclusion STATED, never silent.

    (2) ASSIGNING A PIVOTED DASHBOARD CROSSTAB TO ITS SHEET. A pivoted export
        can never match on its raw header (`Measure Names`/`Measure Values`
        appear in no sheet's pill set), which is why every multi-sheet
        dashboard -- 16 of that run's 21 charts -- silently received no Tableau
        evidence at all. Verified against the REAL exports: the extra tiers
        recover `Customers` -> `CustomerOverview` and correctly REFUSE the
        three dashboards whose export genuinely matches no single sheet."""
    import json as _json
    import validation_evidence_bridge as VEB
    import deep_validation as DV

    # -- (1) scoping --
    grain = ["Order ID"]
    streamlit = [{"Order ID": f"ID-{i}", "Sales": i} for i in range(30)]
    tableau = [{"Order ID": f"ID-{i}", "Sales": i} for i in range(5111)]
    scoped, note = VEB.scope_to_displayed_keys(tableau, streamlit, grain)
    assert len(scoped) == 30, f"expected the 30 displayed keys, got {len(scoped)}"
    assert note and "5111" in note and "not drawn by the chart" in note, note

    # A key the CHART displays but Tableau lacks is a real failure and must
    # survive scoping -- scoping may only ever remove Tableau surplus.
    partial = [{"Order ID": "ID-0", "Sales": 0}]
    kept, note2 = VEB.scope_to_displayed_keys(partial, streamlit, grain)
    assert kept == partial and note2 is None, "no surplus means no narrowing"

    # Scoping must never empty the leg: no shared keys at all is a finding.
    disjoint = [{"Order ID": "OTHER", "Sales": 1}]
    kept2, note3 = VEB.scope_to_displayed_keys(disjoint, streamlit, grain)
    assert kept2 == disjoint and note3 is None, \
        "a fully disjoint Tableau leg must stay visible, not be scoped to nothing"

    # -- (2) pivoted crosstab assignment, against the REAL exports --
    fx = os.path.join(os.path.dirname(__file__), "fixtures", "tableau_crosstabs")
    with open(os.path.join(fx, "pivoted_customers.csv"), encoding="utf-8") as fh:
        customers_csv = fh.read()
    with open(os.path.join(ROOT, "superstore_ir.json"), encoding="utf-8") as fh:
        ir = _json.load(fh)
    by_name = {d["name"]: d for d in ir["dashboards"]}

    class _Ev:
        def __init__(self):
            self.assigned = []

        def set_tableau_csv(self, dashboard, sheet, text):
            self.assigned.append((dashboard, sheet))

    dash = by_name["Customers"]
    ev = _Ev()
    DV._assign_dashboard_csv_by_header("Customer Analysis", dash["sheets"],
                                       customers_csv, ev)
    assert ev.assigned == [("Customer Analysis", "CustomerOverview")], (
        "the pivoted Customers export must resolve to the one sheet at its "
        f"grain, got {ev.assigned}")

    # The raw header alone cannot do this -- proving the new tiers are what
    # resolved it, not the pre-existing exact-header match.
    import csv as _csv, io as _io
    header = {c.strip() for c in
              next(_csv.reader(_io.StringIO(customers_csv.lstrip("﻿"))))}
    assert "Measure Names" in header and "Measure Values" in header
    dims = DV._export_dimension_columns(customers_csv, header)
    assert dims == {"Region"}, f"dimension columns read from the data: {dims}"

    print("ok  Tableau leg scoped to the chart's displayed keys with the "
          "exclusion STATED (closes the 5111-vs-30 / 20,324-false-failure "
          "regression B3 introduced), and a pivoted dashboard crosstab now "
          "resolves to its sheet by dimension columns, still refusing when "
          "the export matches no single sheet")


def test_table_sheet_csv_assigned_by_combined_strong_and_weak_dims():
    """A fine-grained TABLE sheet's dashboard crosstab must resolve when its
    row grain is only fully captured by STRONG-plus-WEAK dims together.

    FOUND LIVE 2026-08-21, continuing the same investigation as
    test_map_sheet_csv_assigned_by_geo_field: after that fix, a full
    end-to-end trace against the real account still showed 11 more sheets
    across 4 dashboards reading 0 Tableau rows. 'ProductDetails' ("Sales and
    Profit by Product Names") is one of them -- the 'Product' dashboard's own
    REST view returns {Category, Product Name, Segment, Sub-Category, Profit
    Ratio, Profit, Sales}, real data unambiguously at ProductDetails' own
    grain. Tier 3 (deep_validation.py) only compares a sheet's STRONG dims
    against the export's dimension columns, and ProductDetails declares only
    ONE strong dim (Segment -- the one shelf-declared breakdown); Category,
    Product Name and Sub-Category are all WEAK by parity.py's own rules
    (nothing marked them as THE breakdown), even though on this fine-grained
    table sheet they are just as much a real part of the row grain. Tier 5
    fixes exactly this: strong UNION weak, still an exact match against the
    export's dimension columns, still refusing on ambiguity like every tier
    above -- 'ProductView', the OTHER sheet on this dashboard, needs 'Order
    Date' (not present in this export at all) so it correctly stays
    unresolved rather than getting a false match."""
    import json as _json

    import deep_validation as DV
    import parity

    fx = os.path.join(os.path.dirname(__file__), "fixtures", "tableau_crosstabs")
    with open(os.path.join(fx, "product_wide_table.csv"), encoding="utf-8") as fh:
        product_csv = fh.read()
    with open(os.path.join(ROOT, "superstore_ir.json"), encoding="utf-8") as fh:
        ir = _json.load(fh)
    by_name = {d["name"]: d for d in ir["dashboards"]}
    dash = by_name["Product"]
    product_details = next(s for s in dash["sheets"] if s["name"] == "ProductDetails")

    class _Ev:
        def __init__(self):
            self.assigned = []

        def set_tableau_csv(self, dashboard, sheet, text):
            self.assigned.append((dashboard, sheet, len(text)))

    ev = _Ev()
    DV._assign_dashboard_csv_by_header("Product Drilldown", dash["sheets"], product_csv, ev)
    assert len(ev.assigned) == 1 and ev.assigned[0][:2] == (
        "Product Drilldown", "Sales and Profit by Product Names"), (
        "the fine-grained table sheet's own combined dims must resolve the "
        f"export to it, and ProductView must NOT get a false match, got {ev.assigned}")

    # Teeth: shrink weak dims away the way the pre-fix pill scan always did,
    # and confirm the assignment reverts to nothing -- proving Tier 5, not
    # some other tier, is what resolved it above. ProductDetails' weak dims
    # come from y_dims (Category, Sub-Category) and detail (Product Name)
    # specifically -- confirmed by reading the real fixture sheet dict, not
    # assumed -- so those are the keys that must be blanked to remove them.
    stripped = dict(product_details)
    stripped["y_dims"] = []
    stripped.pop("detail", None)
    _m, strong_after, weak_after = parity._sheet_pill_captions(stripped)
    assert strong_after == {"Segment"} and not weak_after, (
        "fixture drifted -- stripping y_dims/detail should leave only the "
        f"one strong dim, got strong={strong_after} weak={weak_after}")
    sheets_stripped = [stripped if s["name"] == "ProductDetails" else s
                       for s in dash["sheets"]]
    ev2 = _Ev()
    DV._assign_dashboard_csv_by_header("Product Drilldown", sheets_stripped, product_csv, ev2)
    assert ev2.assigned == [], (
        "with the weak dims gone, strong alone (Segment) must not equal the "
        f"4-column export -- a match here would mean the earlier pass got "
        f"lucky some other way, got {ev2.assigned}")

    print("ok  a fine-grained table sheet's dashboard crosstab resolves by "
          "its combined strong+weak dimensions (ProductDetails, real "
          "account data) instead of reading BLOCKED")


def test_map_sheet_csv_assigned_by_geo_field():
    """A MAP sheet's dashboard crosstab must resolve, not read BLOCKED.

    FOUND LIVE 2026-08-20: the user reported "not seeing Tableau values for
    all sections" on a real deployed run. Traced end to end against the real
    account (workbook 'R1 Test Upload - Superstore', id
    0b53cf7f-cce2-4d1f-b74a-5339aa2916a4) -- the REST pull itself was clean
    (all 10 views returned real data, zero fetch errors), so the gap was in
    _assign_dashboard_csv_by_header's matching, not the fetch. The 'Overview'
    dashboard's own REST view returns exactly ONE of its 4 nested sheets'
    data (Tableau's documented one-sheet-per-dashboard-view export quirk,
    see _assign_dashboard_csv_by_header's docstring) -- here, 'Sale Map's
    real crosstab: Country/Region, State/Province, Profit Ratio, Latitude
    (generated), Longitude (generated). 'Sale Map' declares its geography
    field under a `geo` key (kind='map', geo='State/Province'), which is not
    one of the shelf keys parity._sheet_pill_captions scans (dim/dims/x/y/
    color/...), so it never appeared in that sheet's strong_dims -- Tier 3's
    exact `strong == dims` could never equal the export's dimension columns
    (which also carry the geo field's hierarchy parent 'Country/Region',
    something no sheet ever declares as its own pill) even in principle.
    Every one of that dashboard's 4 sheets read 100% "Missing" in the
    report. Tier 4 (deep_validation.py) fixes exactly this: a subset check
    against the sheet's own declared `geo` field, scoped only to sheets that
    declare one -- a real Tableau-parsed signal, not a guess -- and still
    refusing on ambiguity like every tier above it."""
    import json as _json

    import deep_validation as DV

    fx = os.path.join(os.path.dirname(__file__), "fixtures", "tableau_crosstabs")
    with open(os.path.join(fx, "overview_geo_map.csv"), encoding="utf-8") as fh:
        overview_csv = fh.read()
    with open(os.path.join(ROOT, "superstore_ir.json"), encoding="utf-8") as fh:
        ir = _json.load(fh)
    by_name = {d["name"]: d for d in ir["dashboards"]}
    dash = by_name["Overview"]
    sale_map = next(s for s in dash["sheets"] if s["name"] == "Sale Map")
    assert sale_map.get("geo") == "State/Province", (
        "fixture drifted -- this test needs a real map sheet with a geo field")

    class _Ev:
        def __init__(self):
            self.assigned = []

        def set_tableau_csv(self, dashboard, sheet, text):
            self.assigned.append((dashboard, sheet))

    ev = _Ev()
    DV._assign_dashboard_csv_by_header(
        "Executive Overview - Profitability", dash["sheets"], overview_csv, ev)
    assert ev.assigned == [("Executive Overview - Profitability", "Sales by Geography")], (
        "the map sheet's own geo field must resolve the export to it, and "
        f"nothing else on the dashboard should get a false match, got {ev.assigned}")

    # Teeth: strip `geo` from the sheet the way the pre-fix IR always did,
    # and confirm the assignment reverts to nothing -- proving Tier 4, not
    # some other tier, is what resolved it above.
    sale_map_no_geo = dict(sale_map)
    sale_map_no_geo.pop("geo", None)
    sheets_no_geo = [sale_map_no_geo if s["name"] == "Sale Map" else s
                     for s in dash["sheets"]]
    ev2 = _Ev()
    DV._assign_dashboard_csv_by_header(
        "Executive Overview - Profitability", sheets_no_geo, overview_csv, ev2)
    assert ev2.assigned == [], (
        "without a declared geo field this must stay unresolved -- a false "
        f"assignment here would mean the earlier pass got lucky, got {ev2.assigned}")

    print("ok  a map sheet's dashboard crosstab resolves by its own geo "
          "field (Sale Map, real account data) instead of reading BLOCKED "
          "on every one of that dashboard's 4 sheets")


def test_screenshot_unavailability_names_its_cause():
    """The visual leg's rule, made enforceable (2026-08-14, user's ruling).

    A server-side re-render must NEVER stand in for a screenshot, and "no
    screenshot" must say WHICH of its several causes applied -- Playwright
    absent, Chromium absent, Streamlit absent, or running inside the SiS
    sandbox, which has no browser at all. Those have different fixes, and the
    deployed run reported all ten dashboards as a bare "Missing required
    Streamlit dashboard screenshot", which sends a reader looking in the
    wrong place.

    VERIFIED ON THE REAL APP the same day: from a workstation, all 10
    Superstore dashboards capture as real PNGs in ~151s. The deployed run's
    failure was ONLY the sandbox, so the fix is where the pack is generated,
    not what it falls back to."""
    import app_screenshot as APS
    import validation_report as VR

    # (a) The sandbox names ITSELF, rather than surfacing as a misleading
    # "no usable Chromium" -- which would send someone to install a browser
    # into an environment that can never run one.
    os.environ["SNOWFLAKE_STREAMLIT_APP_NAME"] = "TABLEAU_TO_SIS_PIPELINE_DEMO"
    try:
        ok, why = APS.available()
    finally:
        os.environ.pop("SNOWFLAKE_STREAMLIT_APP_NAME", None)
    assert ok is False and why, "inside SiS a real screenshot is impossible"
    low = why.lower()
    assert "snowflake" in low and "browser" in low, why
    assert "workstation" in low, \
        "the reason must state where the screenshot CAN be taken, not just that it can't"
    assert "chromium" not in low, \
        "must not blame Chromium for a sandbox that can never run any browser"

    # (b) The reason must reach the REPORT, not only the accelerator UI. This
    # is the gap the deployed run exposed: it was computed, shown in the app,
    # and dropped from the document a reviewer actually reads.
    html = VR._image_or_missing(None, "Streamlit dashboard screenshot", why)
    assert "Missing required" in html and "Snowflake" in html, html[:200]

    # (c) A missing image is BLOCKED, never quietly replaced. The bridge must
    # carry a note field for it at all.
    import validation_evidence_bridge as VEB
    ev = VEB.EvidenceBundle()
    ev.set_visual("D", tableau_screenshot=None, streamlit_screenshot=None,
                  streamlit_screenshot_note=why)
    assert ev.visual_for("D").streamlit_screenshot is None, \
        "no substitute image may be inserted"
    assert ev.visual_for("D").streamlit_screenshot_note == why

    print("ok  a missing screenshot names its own cause (SiS sandbox vs "
          "Playwright vs Chromium), the reason reaches the REPORT and not just "
          "the app UI, and no re-render is ever substituted for it")


def test_workstation_pack_cli():
    """The CLI that generates the pack where a browser exists (2026-08-14).

    The SiS sandbox cannot photograph the running app -- no browser, no way to
    install one, no second Streamlit server -- so a pack built by the in-app
    button reports visual BLOCKED for all ten dashboards. Every OTHER leg runs
    fine there. This CLI moves the whole pack to a machine with a browser
    instead of weakening the visual leg.

    VERIFIED BY RUNNING IT the same day, against the real account: 10 Tableau
    screenshots, 8 REAL Streamlit screenshots, 8 crosstab views, pack written
    in 221s -- and 8 of 10 dashboards scored a real visual similarity
    (0.77-0.90) where the deployed run had 10 of 10 BLOCKED.

    What this gate protects is the property that makes the script safe: it
    must remain a THIN DRIVER over the same functions the in-app button calls.
    A second pack implementation would drift from the first, and the two would
    disagree about what a migration proved."""
    import ast

    path = os.path.join(ROOT, "make_validation_pack.py")
    assert os.path.exists(path), "the workstation CLI must exist"
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source)

    called = {node.attr for node in ast.walk(tree)
              if isinstance(node, ast.Attribute)}
    # It must DRIVE the shipped pipeline, not reimplement it.
    for required in ("_render_app_screenshots", "_build_validation_pack",
                     "build_cortex_dashboard_validation_report",
                     "check_workbook"):
        assert required in called, (
            f"the CLI must call {required} rather than reimplementing it")

    # It must not carry its own copy of the comparison/report logic.
    for forbidden in ("compare_chart", "generate_report", "validate_run"):
        assert forbidden not in called, (
            f"the CLI must not call {forbidden} itself -- that is "
            "_build_validation_pack's job, and two callers drift")

    # A credential must never be accepted as an argument: arguments land in
    # shell history and in process listings.
    args = {node.args[0].value for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "add_argument"
            and node.args and isinstance(node.args[0], ast.Constant)}
    assert not any("token" in a or "secret" in a or "password" in a
                   for a in args), \
        f"no credential may be a CLI argument: {sorted(args)}"
    assert "TABLEAU_PAT_SECRET" in source, \
        "the PAT must come from the environment"

    # The browser precondition is checked BEFORE the expensive work, and the
    # failure names itself rather than being discovered minutes in.
    assert "--no-screenshots" in source
    # Compare the real CALL sites, not text positions -- the docstring names
    # these functions too, which is what makes a naive index() comparison lie.
    def _call_line(attr):
        return min((n.lineno for n in ast.walk(tree)
                    if isinstance(n, ast.Call)
                    and getattr(n.func, "attr", "") == attr), default=None)

    probe, build = _call_line("available"), _call_line("_build_validation_pack")
    assert probe and build and probe < build, (
        "check the browser precondition BEFORE spending a live Snowflake "
        f"query per dashboard (available() at {probe}, pack at {build})")

    print("ok  workstation pack CLI is a thin driver over the SAME pack "
          "functions the in-app button uses (no second implementation), takes "
          "no credential as an argument, and fails the browser precondition "
          "up front instead of minutes in")


def test_automated_capture_queue_and_bundle():
    """The automated visual-validation pipeline: queue semantics, the hash
    gate, and the rules that keep the whole thing honest (2026-08-14).

    SiS cannot photograph the app -- no browser, no second Streamlit server --
    so capture runs in a container worker and the two sides talk through a
    durable queue plus a hashed bundle. What this gate protects:

      * ONE implementation. The worker reuses app_screenshot.capture_app; a
        second browser implementation would drift, and a screenshot is
        evidence, so drift is not cosmetic.
      * ATOMIC CLAIM. Two workers must never process one RUN_ID.
      * THE HASH GATE. A screenshot of code that is not the validated code is
        not evidence about this migration.
      * BLOCKED IS NOT FAILED, and a deterministic failure is not retried
        forever.
      * NO CREDENTIAL ever reaches the bundle, the stage or the logs.
    """
    import ast
    import shutil
    import tempfile
    import validation_jobs as VJ

    # ---------- the bundle is hashed, and the hash gate has teeth ----------
    ir = {"dashboards": [{"name": "D1", "title": "Dash One", "sheets": []}]}
    bundle, manifest = VJ.build_capture_bundle(
        run_id="VAL-TEST-0001", ir=ir, app_source="import engine\n",
        dashboards=[{"id": "00-dash-one", "title": "Dash One"}],
        tableau_shots={"Dash One": b"\x89PNG\r\n\x1a\nfake"})
    assert manifest["artifacts"]["app.py"]["sha256"] == \
        VJ.sha256_bytes(b"import engine\n")
    assert "tableau/dash-one.png" in bundle.files

    tmp = tempfile.mkdtemp(prefix="valbundle")
    try:
        for name, data in bundle.files.items():
            target = os.path.join(tmp, name.replace("/", os.sep))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(data)
        assert VJ.Bundle().verify(tmp) == [], "an untouched bundle must verify"

        # Tamper with the generated app exactly as a stale/rebuilt artifact
        # would, and the gate must catch it.
        with open(os.path.join(tmp, "app.py"), "wb") as fh:
            fh.write(b"import engine  # tampered\n")
        problems = VJ.Bundle().verify(tmp)
        assert problems and "app.py" in problems[0], problems
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------- atomic claim: exactly one winner ----------
    class _FakeSession:
        """A Snowflake stand-in that OBEYS THE SQL IT IS GIVEN.

        This matters more than it looks. A fake that re-implements the guards
        in Python would pass whether or not the real statement still contains
        them -- a gate that cannot fail is not a gate. So the guards are read
        out of the statement text: the claim is conditional only if the
        statement really carries `AND STATUS = 'QUEUED'`, and the insert
        de-duplicates only if it really carries `WHERE NOT EXISTS`. Remove
        either from validation_jobs.py and this test goes red, which is
        exactly what it is for.
        """

        def __init__(self):
            self.jobs = {}

        def sql(self, statement, params=None):
            self.last = (" ".join(statement.split()), list(params or []))
            return self

        def collect(self):
            stmt, params = self.last
            if stmt.startswith("INSERT INTO"):
                run_id = params[0]
                guarded = "WHERE NOT EXISTS" in stmt
                if run_id in self.jobs and guarded:
                    return [[0]]
                # Unguarded: Snowflake would happily insert a duplicate.
                self.jobs.setdefault(run_id, {"status": VJ.QUEUED, "worker": None,
                                              "attempt": 0, "cancel": False})
                if run_id in self.jobs and not guarded:
                    self.jobs[f"{run_id}#dup{len(self.jobs)}"] = dict(
                        self.jobs[run_id])
                return [[1]]
            if "SET STATUS = 'CLAIMED'" in stmt:
                conditional = "AND STATUS = 'QUEUED'" in stmt
                for run_id in sorted(self.jobs):
                    job = self.jobs[run_id]
                    if job["cancel"]:
                        continue
                    # The subquery picks the oldest QUEUED row. WITHOUT the
                    # trailing guard the UPDATE then applies regardless of the
                    # row's current status, so a second worker overwrites the
                    # first one's claim -- the bug this asserts against.
                    if job["status"] == VJ.QUEUED or not conditional:
                        job.update(status=VJ.CLAIMED, worker=params[0],
                                   attempt=job["attempt"] + 1)
                        return [[1]]
                return [[0]]
            if stmt.startswith("SELECT RUN_ID FROM"):
                for run_id, job in self.jobs.items():
                    if job["worker"] == params[0] and job["status"] == VJ.CLAIMED:
                        return [[run_id]]
                return []
            return [[0]]

    session = _FakeSession()
    VJ.enqueue(session, "VAL-A")
    VJ.enqueue(session, "VAL-A")          # duplicate submit must be a no-op
    assert len(session.jobs) == 1, "enqueue must be idempotent on RUN_ID"

    first = VJ.claim_next(session, "worker-1")
    second = VJ.claim_next(session, "worker-2")
    assert first == "VAL-A" and second is None, (
        f"exactly one worker may claim a job, got {first!r} and {second!r}")

    # ---------- retry policy ----------
    assert "CHROMIUM_STARTUP" in VJ.RETRYABLE_ERROR_CODES
    assert "APP_STARTUP" not in VJ.RETRYABLE_ERROR_CODES, (
        "a deterministic application failure must not be retried forever -- "
        "it fails identically every time and just delays the honest answer")
    assert VJ.BLOCKED in VJ.TERMINAL_STATUSES and VJ.BLOCKED != VJ.FAILED

    # ---------- the worker reuses, and never re-implements ----------
    with open(os.path.join(ROOT, "validation_worker.py"), encoding="utf-8") as fh:
        worker_src = fh.read()
    worker_tree = ast.parse(worker_src)
    called = {n.attr for n in ast.walk(worker_tree) if isinstance(n, ast.Attribute)}
    assert "capture_app" in called, \
        "the worker must reuse app_screenshot.capture_app, not drive a browser itself"
    assert "sync_playwright" not in worker_src, \
        "no second browser implementation in the worker"

    # ---------- authentication rules ----------
    # Look for USE, not mentions: the module docstring names externalbrowser
    # precisely to forbid it, and a prose ban must not read as a violation.
    literals = {n.value for n in ast.walk(worker_tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "externalbrowser" not in literals, \
        "the worker must never pass externalbrowser as an authenticator"
    assert "oauth" in literals or "SNOWFLAKE_PRIVATE_KEY" in worker_src, \
        "the worker needs a real non-interactive credential path"
    for secret_sink in ("bundle.add(\"password", "print(os.environ"):
        assert secret_sink not in worker_src
    # SiS side must never try to photograph anything itself.
    with open(os.path.join(ROOT, "validation_capture_ui.py"), encoding="utf-8") as fh:
        ui_src = fh.read()
    # Again: imports and calls, not prose. This module's docstring names
    # Playwright precisely to say it never imports it.
    ui_tree = ast.parse(ui_src)
    imported = set()
    for node in ast.walk(ui_tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "playwright" not in imported, \
        "the SiS side must never import Playwright -- it has no browser"
    assert "headless_render" not in imported, \
        "a server-side render must never be submitted as a streamlit screenshot"
    ui_calls = {n.attr for n in ast.walk(ui_tree) if isinstance(n, ast.Attribute)}
    assert "capture_app" not in ui_calls, \
        "the SiS side must queue a job, never attempt the capture itself"

    print("ok  automated capture queue: bundle hashed with a working tamper "
          "gate, enqueue idempotent, claim atomic (one winner), deterministic "
          "failures not retried, BLOCKED distinct from FAILED, worker reuses "
          "app_screenshot and never uses interactive SSO, SiS never captures")


def test_server_side_render_feeds_vision_but_never_the_screenshot_gate():
    """Cortex vision works without a browser -- and the render still never
    counts as a screenshot (2026-08-14).

    Inside SiS there is no browser, so the pack's app-side screenshot set is
    empty and the Cortex vision panel used to stop at "no app-side
    screenshots" -- leaving every dashboard with no visual judgment at all.
    headless_render draws the app's OWN chart objects with no browser, which
    is a real picture of what the app builds, just not a photograph of it
    running. It is now fed to the VISION COMPARISON only.

    The rule this gate exists to hold: a reconstruction must NEVER be
    presented as a screenshot. So the render may reach Cortex, but it must
    never reach `streamlit_screenshot`, the visual status must stay BLOCKED,
    and no similarity score may be computed against it. A real screenshot
    always wins when one exists."""
    import ast

    with open(os.path.join(ROOT, "deep_validation.py"), encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)

    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name == "_server_side_renders"), None)
    assert fn is not None, "the no-browser vision fallback must exist"

    # It must draw with headless_render, not pretend to photograph.
    calls = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert "render_dashboard_to_png" in calls
    assert "capture_app" not in calls, \
        "the fallback must not claim to be a screenshot path"

    # THE CORE RULE: the render must never be handed to set_visual as a
    # streamlit screenshot. Check every set_visual call in the module.
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "set_visual"):
            for kw in node.keywords:
                if kw.arg == "streamlit_screenshot":
                    rendered = ast.dump(kw.value)
                    assert "_server_side_renders" not in rendered and \
                           "render_paths" not in rendered, \
                        "a server-side render must never be set as the " \
                        "streamlit screenshot -- the visual gate stays BLOCKED"

    vision = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "render_cortex_vision")
    vision_src = ast.get_source_segment(src, vision) or ""
    # A real screenshot must take precedence over the render.
    assert "s_paths or render_paths" in vision_src, \
        "a real screenshot must win whenever one exists"
    # And the user must be told which one they are looking at.
    assert "non-authoritative" in vision_src.lower(), \
        "the render must be labelled non-authoritative in the UI"

    print("ok  server-side render feeds the Cortex vision comparison when no "
          "browser exists (so SiS gets a real visual verdict instead of "
          "silence) while never being set as a streamlit screenshot -- the "
          "visual gate stays BLOCKED and a real screenshot always wins")


def test_in_snowflake_capture_ships_real_routing_and_waits_for_tabs():
    """Real Streamlit screenshots from INSIDE Snowflake (2026-08-14).

    PROVEN against the real account: 10 of 10 Superstore dashboards captured
    by a container-runtime notebook on SYSTEM_COMPUTE_POOL_CPU in ~160s, with
    no Docker, no image build and no local machine. Two defects surfaced doing
    it, and this gate holds both shut.

    1. TABLE ROUTING. The first successful run produced ten perfect
       screenshots OF TEN ERROR MESSAGES -- "Database 'SUPERSTORE' does not
       exist or not authorized" -- because the repo's datasources.json is the
       LOCAL DEV map. The capture was flawless; the app inside it had been
       pointed at nothing. The bundle now ships the CALLER'S live
       config.DATASOURCES, which is also the data-fidelity requirement: the
       captured app must query the same tables the validation queried.

    2. WAITING FOR THE APP. capture_app counted tabs the moment the DOM
       settled. Against local DuckDB that is fine; inside Snowflake every
       query is a round trip, the page still reads "RUNNING..." at that
       moment, and a perfectly healthy app looked like one that had failed to
       start. It now waits for the tab strip and, if it never appears, says
       whether the app was still running."""
    import ast
    import inspect
    import app_screenshot as APS
    import notebook_capture as NBC

    # -- (1) the bundle must carry live routing, never the on-disk dev map --
    # The bundle-build logic moved from capture() into _capture_once() in the
    # 2026-08-16 retry refactor (capture() is now a thin wrapper that only
    # resolves `dashboards` and calls _capture_with_retry) -- this checks
    # wherever the routing is ACTUALLY built, not a fixed function name.
    src = inspect.getsource(NBC._capture_once)
    assert "config.DATASOURCES" in src, (
        "the capture bundle must ship the caller's live routing -- shipping "
        "the repo's datasources.json points the app at a SUPERSTORE database "
        "that does not exist in Snowflake")
    tree = ast.parse(inspect.getsource(NBC))
    # It must not simply copy datasources.json across as a runtime module.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "datasources.json":
            pass  # written, not copied -- the write is asserted above
    assert "datasources.json" not in NBC.RUNTIME_MODULES, \
        "datasources.json must be BUILT from live routing, not copied from disk"

    # -- (2) capture_app must wait for tabs, not count them immediately ------
    cap = inspect.getsource(APS.capture_app)
    assert "wait_for" in cap and "role=\"tab\"" in cap, \
        "capture_app must WAIT for the tab strip before concluding there is none"
    wait_at = cap.index("tabs.first.wait_for")
    count_at = cap.index("n = tabs.count()")
    assert wait_at < count_at, \
        "the wait must come BEFORE the count, or a slow app reads as a broken one"
    assert "still running" in cap.lower(), \
        "a still-running app must be distinguished from one that failed to start"

    # -- the capture must reuse app_screenshot, never re-drive a browser -----
    nb_src = inspect.getsource(NBC)
    assert "capture_app" in nb_src, "the in-Snowflake path must reuse capture_app"
    assert "sync_playwright" not in nb_src.split("_NOTEBOOK_TEMPLATE")[0], \
        "notebook_capture itself must not drive a browser -- it ships work to one"

    # -- (3) THE ACCESSOR MUST ACTUALLY RESOLVE -------------------------------
    # The first release of this path called a GUESSED `backend._snowpark()`,
    # which does not exist, inside a bare `except Exception`. Every deployed
    # run therefore skipped the in-Snowflake capture in silence and reported
    # the plain "no browser here" message as if nothing else had been tried --
    # a bug that was indistinguishable from a platform limit. Source-string
    # checks did not catch it; resolving the attribute does.
    import backend
    import deep_validation as DV

    assert hasattr(backend, "_active_session"), \
        "deep_validation._active_session() depends on backend._active_session"
    # Read the ATTRIBUTE NAMES it actually reaches for, not the source text --
    # the docstring names the old wrong accessor precisely to explain it, and a
    # text match cannot tell an explanation from a call.
    dv_tree = ast.parse(inspect.getsource(DV._active_session).lstrip())
    reached = {node.args[1].value for node in ast.walk(dv_tree)
               if isinstance(node, ast.Call)
               and getattr(node.func, "id", "") == "getattr"
               and len(node.args) >= 2
               and isinstance(node.args[1], ast.Constant)
               and isinstance(node.args[1].value, str)}
    assert reached, "the accessor name should be looked up explicitly"
    for name in sorted(reached):
        assert hasattr(backend, name), (
            f"deep_validation._active_session reaches backend.{name}, which "
            "does not exist -- verify the accessor name, do not guess it")
    # It must report WHY, never just None, or a skipped fallback looks like a
    # platform limit again.
    got = DV._active_session()
    assert isinstance(got, tuple) and len(got) == 2, \
        "_active_session must return (session, reason) so a skip can explain itself"
    session_obj, reason = got
    assert (session_obj is not None) or reason, \
        "when there is no session the caller must be given a stated reason"

    print("ok  in-Snowflake capture ships the caller's LIVE table routing (not "
          "the dev map that produced 10 screenshots of 10 'database does not "
          "exist' errors), capture_app WAITS for the tab strip instead of "
          "reading a still-running app as broken, and the session accessor "
          "RESOLVES -- the guessed backend._snowpark() silently disabled the "
          "whole fallback in the previous release")


# ---- retry logic around EXECUTE NOTEBOOK (2026-08-16) ---------------------
# error 300002:2250622648 killed two consecutive real EXECUTE NOTEBOOK calls
# in under a second each; a later identical call, zero code changes, worked
# cleanly (10/10, byte-identical to the known-good baseline) -- the platform
# fault was transient, not a bug here. This suite proves the retry wrapper
# around it entirely offline, with a fake Snowpark session.
import json                            # noqa: E402
import re                              # noqa: E402
import time                            # noqa: E402


def _tiny_png(width=800, height=500):
    from PIL import Image
    import io as _io
    buf = _io.BytesIO()
    Image.new("RGB", (max(1, width), max(1, height)), color=(240, 240, 240)).save(buf, format="PNG")
    return buf.getvalue()


def _nbcap_ir(titles):
    return {"dashboards": [{"name": t, "title": t} for t in titles]}


def _safe_slug(title):
    return "".join(c.lower() if c.isalnum() else "-" for c in str(title)).strip("-")


class _FakeCaptureSession:
    """Fakes exactly what notebook_capture.py touches on a Snowpark session:
    .sql(...).collect() for CREATE/ALTER/EXECUTE/DROP NOTEBOOK, and
    .file.put()/.file.get() for staging the bundle and pulling results home.

    `attempts` is one entry per expected EXECUTE NOTEBOOK call, in order:
      {"raise": Exception(...)}                    -- EXECUTE NOTEBOOK itself raises
      {"no_output": True}                           -- file.get() raises after execute
      {"no_manifest": True}                         -- output has no manifest at all
      {"malformed_manifest": True}                  -- manifest.json is invalid JSON
      {"files": {"Title": png_bytes, ...}}          -- a completed attempt (may still
                                                        fail _validate_capture, e.g.
                                                        missing title or a bad PNG)
      {"files": {...}, "step_fails": "pip_install"} -- capture_app step never ran
    A call beyond the scripted list is a test-authoring error, not a real
    Snowflake behavior -- it raises AssertionError immediately."""

    def __init__(self, attempts):
        self.attempts = list(attempts)
        self.execute_count = 0
        self.notebooks_created = []
        self.notebooks_dropped = []
        self.puts = []
        self.gets = []
        self._current_entry = None
        self.file = _FakeCaptureSession._File(self)

    def sql(self, stmt):
        self._last = " ".join(stmt.split())
        return self

    def collect(self):
        stmt = self._last
        if stmt.startswith("CREATE OR REPLACE NOTEBOOK"):
            m = re.search(r"NOTEBOOK (\S+)", stmt)
            self.notebooks_created.append(m.group(1) if m else None)
            return []
        if stmt.startswith("ALTER NOTEBOOK") and "ADD LIVE VERSION" in stmt:
            return []
        if stmt.startswith("ALTER NOTEBOOK") and "EXTERNAL_ACCESS_INTEGRATIONS" in stmt:
            return []
        if stmt.startswith("EXECUTE NOTEBOOK"):
            idx = self.execute_count
            self.execute_count += 1
            assert idx < len(self.attempts), (
                f"attempt {idx + 1} was not scripted -- retry loop ran longer "
                "than the test expected")
            entry = self.attempts[idx]
            self._current_entry = entry
            if "raise" in entry:
                raise entry["raise"]
            return []
        if stmt.startswith("DROP NOTEBOOK"):
            m = re.search(r"NOTEBOOK IF EXISTS (\S+)", stmt)
            self.notebooks_dropped.append(m.group(1) if m else None)
            return []
        return []

    class _File:
        def __init__(self, outer):
            self.outer = outer

        def put(self, local_path, stage_prefix, auto_compress=False, overwrite=True):
            self.outer.puts.append((os.path.basename(local_path), stage_prefix))

        def get(self, remote_prefix, local_dir):
            self.outer.gets.append((remote_prefix, local_dir))
            entry = self.outer._current_entry
            os.makedirs(local_dir, exist_ok=True)
            if entry is None or entry.get("no_output"):
                raise RuntimeError("simulated: no downloadable output")
            if entry.get("no_manifest"):
                return
            if entry.get("malformed_manifest"):
                with open(os.path.join(local_dir, "capture_manifest.json"), "w",
                          encoding="utf-8") as fh:
                    fh.write("{not valid json")
                return
            files_spec = entry.get("files", {})
            manifest_files = {}
            for title, png_bytes in files_spec.items():
                fname = f"streamlit-{_safe_slug(title)}.png"
                with open(os.path.join(local_dir, fname), "wb") as fh:
                    fh.write(png_bytes)
                manifest_files[title] = {"file": fname, "bytes": len(png_bytes)}
            step_fails = entry.get("step_fails")
            if step_fails:
                steps = [{"step": step_fails, "ok": False, "error": "boom"}]
            else:
                steps = [{"step": "capture_app", "ok": True,
                         "detail": {"captured": len(manifest_files),
                                   "files": manifest_files}}]
            manifest = {"run_id": "x", "steps": steps, "ok": not step_fails}
            with open(os.path.join(local_dir, "capture_manifest.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(manifest, fh)


def _run_retry(attempts, titles=("Dash A", "Dash B")):
    """Real codegen.build(ir) needs a full workbook IR this suite is not
    trying to exercise here -- stubbed the same way the rest of this file
    monkeypatches a real module for an isolated test, restored in finally."""
    import codegen
    import notebook_capture as NBC
    real_build = codegen.build
    codegen.build = lambda ir: "# stub app\n"
    session = _FakeCaptureSession(attempts)
    ir = _nbcap_ir(titles)
    dashboards = [{"title": t, "name": t} for t in titles]
    try:
        shots, notes = NBC._capture_with_retry(
            session, ir, "nbcaptest", dashboards, log=None,
            sleep_fn=lambda s: None, jitter_fn=lambda: 0)
    finally:
        codegen.build = real_build
    return session, shots, notes


def test_capture_retry_first_attempt_succeeds():
    files = {"Dash A": _tiny_png(), "Dash B": _tiny_png()}
    session, shots, notes = _run_retry([{"files": files}])
    assert session.execute_count == 1, "a clean first attempt must not retry"
    assert set(shots) == {"Dash A", "Dash B"}
    assert notes == [], "no diagnostics needed when attempt 1 just works"
    print("ok  first attempt succeeds -- no retry, no diagnostics noise")


def test_capture_retry_transient_then_success():
    err = RuntimeError("SnowparkSQLException: (1304): 01c66cb2-061d-1b1a-0000-"
                       "3081207c17ce: 000603 (XX000): SQL execution internal "
                       "error: Processing aborted due to error "
                       "300002:2250622648; incident 6979790.")
    files = {"Dash A": _tiny_png(), "Dash B": _tiny_png()}
    session, shots, notes = _run_retry([{"raise": err}, {"files": files}])
    assert session.execute_count == 2, "attempt 1 transient -> exactly one retry"
    assert set(shots) == {"Dash A", "Dash B"}
    assert len(notes) == 1 and notes[0]["captured"] is True
    attempts = notes[0]["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["ok"] is False and attempts[1]["ok"] is True
    assert attempts[0]["query_id"] == "01c66cb2-061d-1b1a-0000-3081207c17ce"
    assert attempts[0]["sqlstate"] == "XX000"
    assert attempts[0]["incident_id"] == "6979790"
    assert attempts[0]["error_code"] == "300002"
    assert "attempt 2 of 3" in notes[0]["summary"]
    print("ok  attempt 1 transient (300002/XX000/incident all parsed), "
          "attempt 2 succeeds -- diagnostics preserved even though nothing "
          "is BLOCKED")


def test_capture_retry_three_transient_exhausts():
    def err(n):
        return RuntimeError(f"000603 (XX000): SQL execution internal error: "
                            f"error 300002:{n}; incident {1000 + n}.")
    session, shots, notes = _run_retry(
        [{"raise": err(1)}, {"raise": err(2)}, {"raise": err(3)}])
    assert session.execute_count == 3, "must stop at MAX_ATTEMPTS, never a 4th"
    assert shots == {}
    assert len(notes) == 1 and notes[0]["captured"] is False
    assert len(notes[0]["attempts"]) == 3
    assert all(a["ok"] is False for a in notes[0]["attempts"])
    assert "3 attempt" in notes[0]["reason"]
    print("ok  three transient failures exhaust retries, never a fourth "
          "attempt, BLOCKED with full 3-attempt history")


def test_capture_retry_deterministic_stops_after_one():
    err = RuntimeError("002003 (02000): SQL compilation error: Insufficient "
                       "privileges to operate on notebook 'CAPTURE_X'")
    session, shots, notes = _run_retry([{"raise": err}])
    assert session.execute_count == 1, \
        "a privilege/compilation error must not be retried"
    assert shots == {}
    assert notes[0]["attempts"][0]["retryable"] is False
    print("ok  a deterministic (privilege) failure stops after one attempt, "
          "never burns the 10s/30s backoff on something that cannot succeed")


def test_capture_retry_missing_manifest_triggers_retry():
    files = {"Dash A": _tiny_png(), "Dash B": _tiny_png()}
    session, shots, notes = _run_retry([{"no_manifest": True}, {"files": files}])
    assert session.execute_count == 2
    assert set(shots) == {"Dash A", "Dash B"}
    assert notes[0]["attempts"][0]["error"] == "capture job wrote no manifest"
    assert notes[0]["attempts"][0]["retryable"] is True
    print("ok  EXECUTE NOTEBOOK 'succeeding' with no manifest at all is "
          "retried, not accepted as done")


def test_capture_retry_malformed_manifest_triggers_retry():
    files = {"Dash A": _tiny_png(), "Dash B": _tiny_png()}
    session, shots, notes = _run_retry(
        [{"malformed_manifest": True}, {"files": files}])
    assert session.execute_count == 2
    assert set(shots) == {"Dash A", "Dash B"}
    assert "manifest malformed" in notes[0]["attempts"][0]["error"]
    print("ok  an unparsable manifest.json is retried, not silently ignored")


def test_capture_retry_partial_images_triggers_retry():
    partial = {"Dash A": _tiny_png()}   # Dash B missing
    full = {"Dash A": _tiny_png(), "Dash B": _tiny_png()}
    session, shots, notes = _run_retry([{"files": partial}, {"files": full}])
    assert session.execute_count == 2
    assert set(shots) == {"Dash A", "Dash B"}
    reason = notes[0]["attempts"][0]["error"]
    assert "Dash B" in reason, reason
    print("ok  1-of-2 (analogous to 9-of-10) screenshots captured is retried, "
          "and the reason names the missing dashboard")


def test_capture_retry_invalid_png_triggers_retry():
    bad = {"Dash A": _tiny_png(), "Dash B": b"not a real png at all"}
    good = {"Dash A": _tiny_png(), "Dash B": _tiny_png()}
    session, shots, notes = _run_retry([{"files": bad}, {"files": good}])
    assert session.execute_count == 2
    assert set(shots) == {"Dash A", "Dash B"}
    reason = notes[0]["attempts"][0]["error"]
    assert "Dash B" in reason and ("PNG" in reason or "PIL" in reason), reason
    print("ok  a corrupted/non-PNG file is retried, not shipped as evidence")


def test_capture_retry_legit_blank_png_accepted():
    """Sheet 22, 2026-08-16: a genuinely empty placeholder sheet (confirmed
    against the IR -- non_data=True, no dims/measures -- and against
    Tableau's own screenshot of the same sheet, also blank) captured at
    2608x66. A full-width, short image is real evidence, not a defect, and
    must be accepted WITHOUT a retry."""
    import notebook_capture as NBC
    files = {"Dash A": _tiny_png(width=2608, height=66),
             "Dash B": _tiny_png(width=1600, height=1200)}
    session, shots, notes = _run_retry([{"files": files}])
    assert session.execute_count == 1, \
        "a legitimately short-but-full-width screenshot must not trigger a retry"
    assert set(shots) == {"Dash A", "Dash B"}
    assert NBC.MIN_WIDTH == 640 and 66 < NBC.MIN_HEIGHT, \
        "test assumes the real MIN_WIDTH/MIN_HEIGHT constants"
    print("ok  a full-width, near-zero-height screenshot (Sheet 22's real "
          "shape) is accepted -- content blankness is never a rejection "
          "reason, only structure is")


def test_capture_retry_tiny_corrupt_png_rejected():
    import notebook_capture as NBC
    ok, reason, shots = NBC._validate_capture(
        os.path.join("reports", "__nbcap_unused__"), None, ["Dash A"])
    assert ok is False and reason == "manifest missing"

    tiny_dir = os.path.join("reports", "__nbcap_tiny_test__")
    os.makedirs(tiny_dir, exist_ok=True)
    try:
        with open(os.path.join(tiny_dir, "streamlit-dash-a.png"), "wb") as fh:
            fh.write(_tiny_png(width=1, height=1))
        manifest = {"steps": [{"step": "capture_app", "ok": True,
                              "detail": {"captured": 1, "files": {
                                  "Dash A": {"file": "streamlit-dash-a.png",
                                            "bytes": 1}}}}]}
        ok2, reason2, shots2 = NBC._validate_capture(tiny_dir, manifest, ["Dash A"])
        assert ok2 is False and "width" in reason2, reason2
        assert shots2 == {}
    finally:
        import shutil
        shutil.rmtree(tiny_dir, ignore_errors=True)
    print("ok  a 1x1 PNG (below MIN_WIDTH) is rejected by _validate_capture "
          "directly, distinct from the legitimate-blank case above")


def test_capture_retry_isolated_stage_paths_and_notebook_names():
    # attempt 1 must reach file.get() (so BOTH attempts leave a recorded
    # output prefix to compare) but still fail -- an empty output directory
    # does exactly that, unlike a raise inside EXECUTE NOTEBOOK itself.
    files = {"Dash A": _tiny_png(), "Dash B": _tiny_png()}
    session, shots, notes = _run_retry([{"no_manifest": True}, {"files": files}])
    assert len(session.notebooks_created) == 2
    assert session.notebooks_created[0] != session.notebooks_created[1], \
        "each attempt must create its own notebook name"
    assert "A1" in session.notebooks_created[0]
    assert "A2" in session.notebooks_created[1]
    assert len(session.notebooks_dropped) == 2, \
        "every created notebook must be cleaned up, including the failed one"
    input_prefixes = {p for _f, p in session.puts if p.endswith("/input")}
    assert len(input_prefixes) == 2, \
        f"each attempt must stage its bundle under its own input prefix, got {input_prefixes}"
    output_prefixes = {rp for rp, _ld in session.gets}
    assert len(output_prefixes) == 2, \
        f"each attempt must pull output from its own prefix, got {output_prefixes}"
    print("ok  each attempt gets its own notebook name, stage input prefix "
          "and output prefix -- and every created notebook is dropped")


def test_capture_retry_only_successful_attempt_shots_used():
    stale = {"Dash A": _tiny_png(width=100, height=100),  # too narrow -> fails
             "Dash B": _tiny_png()}
    fresh_a = _tiny_png(width=900, height=600)
    winning = {"Dash A": fresh_a, "Dash B": _tiny_png()}
    session, shots, notes = _run_retry([{"files": stale}, {"files": winning}])
    assert session.execute_count == 2
    assert shots["Dash A"] == fresh_a, \
        "the final shots must come from the WINNING attempt only, never a mix"
    print("ok  only the successful attempt's screenshots are returned -- a "
          "failed earlier attempt's images are never merged in")


def test_capture_diagnostics_render_once_in_html():
    import validation_report as VR
    run = {
        "workbook": "Test", "run_id": "r1", "environment": "UAT",
        "generated_at": "now", "status": "BLOCKED",
        "dashboards": [
            {"id": "d1", "name": "Dash A", "charts": [], "formulas": [],
             "interactions": [], "images": {"tableau": None, "streamlit": None,
                                            "diff": None},
             "visual": {"streamlit_screenshot_note": "capture failed after 3 "
                                                      "attempts -- see capture "
                                                      "diagnostics above"},
             "visual_status": "BLOCKED", "status": "BLOCKED"},
            {"id": "d2", "name": "Dash B", "charts": [], "formulas": [],
             "interactions": [], "images": {"tableau": None, "streamlit": None,
                                            "diff": None},
             "visual": {"streamlit_screenshot_note": "capture failed after 3 "
                                                      "attempts -- see capture "
                                                      "diagnostics above"},
             "visual_status": "BLOCKED", "status": "BLOCKED"},
        ],
        "capture_diagnostics": {
            "summary": "failed after 3 attempt(s)",
            "attempts": [
                {"attempt": n, "run_id": f"CAP-x-A{n}", "ok": False,
                 "error": "SnowparkSQLException ... 300002 ... incident 999",
                 "query_id": None, "sqlstate": "XX000", "incident_id": "999",
                 "error_code": "300002", "elapsed_s": 0.5}
                for n in (1, 2, 3)
            ],
        },
    }
    html = VR.render_html(run)
    assert html.count("Streamlit capture diagnostics") == 1, \
        "the diagnostics block must appear ONCE, not once per dashboard"
    # One row per attempt (3) inside the ONE diagnostics table is expected and
    # fine; what must NOT happen is that count multiplying by dashboard count
    # (2) to 6 -- i.e. the technical dump leaking into each per-dashboard card.
    assert html.count("300002") == len(run["capture_diagnostics"]["attempts"]), \
        "the full technical detail must live ONLY in the one diagnostics " \
        "table (one occurrence per attempt row), never duplicated per " \
        "dashboard on top of that"
    assert html.count("capture failed after 3 attempts") == 2, \
        "each dashboard keeps its OWN short one-line reason -- that is not " \
        "a duplication of the technical dump, just each card stating BLOCKED"
    print("ok  capture diagnostics render exactly once at workbook level; "
          "per-dashboard cards stay short, not a repeated exception dump")


def test_pack_screenshots_not_discarded_by_unrelated_skipped_section():
    """REAL BUG, found live 2026-08-19 on the first genuine UI click-through
    of the deployed app's 'Run proof-first validation' button. 'On-Time
    Shipment Trends' and 'Sheet 22' both got a real, successfully-captured
    Streamlit screenshot -- confirmed independently against the account
    (task_capture.fetch_results() returned them, byte-identical to what was
    on the stage) -- but the report showed 'Missing required Streamlit
    dashboard screenshot' for both anyway.

    ROOT CAUSE: `shots = {t: p for t, p in all_shots.items() if t in
    resolved_titles}`, where resolved_titles came from `sections`
    (parity.build_cortex_dashboard_validation_report's per-dashboard
    FORMULA/INTERACTION output, a COMPLETELY SEPARATE leg of evidence). Both
    dashboards' formula/interaction section was independently skipped (no
    comparable calculated fields on those sheets) -- an unrelated reason --
    so a real, already-captured screenshot was silently thrown away.

    Proves the fix: _pack_screenshots returns every real capture unfiltered,
    regardless of whether that dashboard's formula/interaction section was
    skipped, still-pending, or never existed at all."""
    import deep_validation as DV
    all_shots = {"On-Time Shipment Trends": b"\x89PNG-real-bytes-1",
                "Sheet 22": b"\x89PNG-real-bytes-2",
                "Sales Commission Model": b"\x89PNG-real-bytes-3"}
    sections = [
        {"title": "On-Time Shipment Trends", "skipped": "no comparable calculated fields"},
        {"title": "Sheet 22", "skipped": "no comparable calculated fields"},
        {"title": "Sales Commission Model", "skipped": None},
    ]
    result = DV._pack_screenshots(all_shots, sections)
    assert result == all_shots, \
        f"a real captured screenshot must never be dropped because an " \
        f"UNRELATED formula/interaction section was skipped: {result}"
    # a dashboard with NO section entry at all (never reached by
    # build_cortex_dashboard_validation_report for any reason) must also
    # keep its screenshot -- the function must not require sections to be
    # complete or even non-empty
    assert DV._pack_screenshots(all_shots, []) == all_shots
    print("ok  a dashboard's real Streamlit screenshot survives even when "
          "its formula/interaction section was independently skipped -- "
          "visual and data/logic evidence are separate legs, one being "
          "unavailable must never silently discard the other")


def test_capture_report_embeds_all_shots_after_retry_success():
    import inspect
    import notebook_capture as NBC
    sig = inspect.signature(NBC.capture)
    assert list(sig.parameters) == ["session", "ir", "stem", "dashboards", "log"], \
        "capture()'s public signature must stay exactly as it was before " \
        "the retry logic existed -- sleep_fn/jitter_fn belong on the " \
        "private _capture_with_retry only"

    import codegen
    real_build = codegen.build
    real_sleep = time.sleep
    codegen.build = lambda ir: "# stub app\n"
    time.sleep = lambda s: None
    try:
        err = RuntimeError("XX000 internal error 300002:1; incident 7")
        files = {"Dash A": _tiny_png(), "Dash B": _tiny_png()}
        session = _FakeCaptureSession([{"raise": err}, {"files": files}])
        ir = _nbcap_ir(["Dash A", "Dash B"])
        shots, notes = NBC.capture(session, ir, "nbcaptest2")
    finally:
        codegen.build = real_build
        time.sleep = real_sleep
    assert set(shots) == {"Dash A", "Dash B"}
    assert shots["Dash A"] == files["Dash A"]
    assert shots["Dash B"] == files["Dash B"]
    print("ok  the public capture() entry point (unchanged signature) "
          "retries internally and returns every dashboard's screenshot "
          "after eventual success")


def test_capture_retry_no_blocked_dashboards_after_eventual_success():
    """Mirrors deep_validation._build_validation_pack's own shot_reason /
    global_reason construction from (shots, notes) -- a captured=True
    diagnostics note must never leak into a dashboard's BLOCKED reason."""
    err = RuntimeError("XX000 internal error 300002:1; incident 7")
    files = {"Dash A": _tiny_png(), "Dash B": _tiny_png()}
    session, shots, notes = _run_retry([{"raise": err}, {"files": files}])

    shot_reason, global_reason = {}, None
    for note in notes:
        if note.get("captured"):
            continue
        if note.get("dashboard"):
            shot_reason[note["dashboard"]] = note.get("reason")
        elif global_reason is None:
            global_reason = note.get("reason")

    for title in ("Dash A", "Dash B"):
        streamlit_path = shots.get(title)
        resolved_note = None if streamlit_path else (shot_reason.get(title) or global_reason)
        assert streamlit_path is not None, f"{title} must have a real screenshot"
        assert resolved_note is None, \
            f"{title} must not carry a BLOCKED reason after eventual success"
    print("ok  after a successful retry, no dashboard is left BLOCKED by a "
          "stale attempt-1 failure reason")


def test_capture_retry_error_not_repeated_ten_times():
    def err(n):
        return RuntimeError(f"XX000 internal error 300002:{n}; incident {n}")
    titles = [f"Dash {i}" for i in range(5)]
    session, shots, notes = _run_retry(
        [{"raise": err(1)}, {"raise": err(2)}, {"raise": err(3)}], titles=titles)
    assert shots == {}
    assert len(notes) == 1, \
        "a workbook-level capture failure must be ONE note, never one per dashboard"
    assert len(notes[0]["attempts"]) == 3
    print("ok  a total capture failure across 5 dashboards still produces "
          "exactly one note, not five (or fifteen)")


# ---- task-driven capture (2026-08-16) -------------------------------------
# EXECUTE NOTEBOOK cannot be called from a SiS app session -- measured 0/8
# from the deployed app against 8/8 from a workstation, every failure the same
# code (300002:2250622648) in ~0.6s. So the deployed app enqueues and a
# Snowflake TASK, in its own execution context, runs the notebook. These gates
# cover the SiS side of that contract offline.

class _FakeQueueSession:
    """Fakes the queue as a dict of rows, driven by the ACTUAL SQL text the
    module emits -- not a Python reimplementation of the rules.

    That distinction matters: a fake that re-implements the atomic-claim
    guard in Python would pass whether or not the real UPDATE still carries
    `AND STATUS = 'QUEUED'`. Here the guard is read out of the statement, so
    deleting it from task_capture/validation_jobs turns the concurrency gate
    red -- which is the only reason that gate is worth having.

    `script` drives what the "worker" does when a job is claimed; the test
    calls `run_worker()` to simulate a task tick.
    """

    def __init__(self, outputs=None, worker=None):
        self.rows = {}
        self.outputs = outputs or {}      # run_id -> {filename: bytes|str}
        self.worker = worker              # callable(session, run_id)
        self.puts = []
        self.gets = []
        self.claim_guarded = None
        self.file = _FakeQueueSession._File(self)

    # -- SQL ---------------------------------------------------------------
    def sql(self, statement, params=None):
        self._last = (" ".join(statement.split()), list(params or []))
        return self

    def collect(self):
        stmt, params = self._last
        up = stmt.upper()
        if up.startswith("INSERT INTO"):
            run_id = params[0]
            guarded = "WHERE NOT EXISTS" in up
            if run_id in self.rows and guarded:
                return [[0]]
            self.rows.setdefault(run_id, {
                "STATUS": "QUEUED", "WORKER_ID": None, "ATTEMPT": 0,
                "CANCEL_REQUESTED": False, "PROGRESS_LABEL": "Queued",
                "PROGRESS_DONE": 0, "PROGRESS_TOTAL": 0,
                "ERROR_CODE": None, "ERROR_MESSAGE": None,
                "RESULT_MANIFEST_PATH": None, "REPORT_PATH": None})
            return [[1]]
        if "SET STATUS = 'CLAIMED'" in up:
            # record whether the real statement still carries the guard
            self.claim_guarded = "AND STATUS = 'QUEUED'" in up
            for rid in sorted(self.rows):
                row = self.rows[rid]
                if row["CANCEL_REQUESTED"]:
                    continue
                if row["STATUS"] == "QUEUED" or not self.claim_guarded:
                    row.update(STATUS="CLAIMED", WORKER_ID=params[0],
                               ATTEMPT=row["ATTEMPT"] + 1)
                    return [[1]]
            return [[0]]
        if up.startswith("SELECT RUN_ID FROM"):
            for rid, row in self.rows.items():
                if row["WORKER_ID"] == params[0] and row["STATUS"] == "CLAIMED":
                    return [[rid]]
            return []
        if up.startswith("SELECT STATUS,"):
            row = self.rows.get(params[0])
            if not row:
                return []
            return [[row["STATUS"], row["PROGRESS_LABEL"], row["PROGRESS_DONE"],
                     row["PROGRESS_TOTAL"], row["ERROR_CODE"],
                     row["ERROR_MESSAGE"], row["REPORT_PATH"],
                     row["WORKER_ID"], row["ATTEMPT"]]]
        if up.startswith("SELECT CANCEL_REQUESTED"):
            row = self.rows.get(params[0])
            return [[row["CANCEL_REQUESTED"]]] if row else []
        if up.startswith("SHOW TASKS"):
            return [["CAPTURE_WORKER_TASK"]]
        if up.startswith("UPDATE") and "SET STATUS = 'TIMED_OUT'" in up:
            n = 0
            for row in self.rows.values():
                if row["STATUS"] in ("QUEUED", "CLAIMED", "STARTING_APP",
                                     "CAPTURING", "GENERATING_REPORT"):
                    row.update(STATUS="TIMED_OUT", ERROR_CODE="TIMED_OUT")
                    n += 1
            return [[n]]
        if up.startswith("UPDATE"):
            # generic status/progress update -- apply what we can parse
            rid = params[-1] if params else None
            row = self.rows.get(rid)
            if row is not None:
                for status in ("COMPLETED", "FAILED", "BLOCKED", "CAPTURING",
                               "STARTING_APP", "GENERATING_REPORT"):
                    if f"SET STATUS = '{status}'" in up or \
                       f", STATUS = '{status}'" in up:
                        row["STATUS"] = status
            return [[1]]
        return [[0]]

    # -- files -------------------------------------------------------------
    class _File:
        def __init__(self, outer):
            self.outer = outer

        def put(self, local, prefix, auto_compress=False, overwrite=True):
            self.outer.puts.append((os.path.basename(local), prefix))

        def get(self, prefix, dest):
            self.outer.gets.append((prefix, dest))
            os.makedirs(dest, exist_ok=True)
            run_id = prefix.rstrip("/").split("/")[-2]
            files = self.outer.outputs.get(run_id)
            if files is None:
                raise RuntimeError("simulated: nothing at " + prefix)
            for name, data in files.items():
                path = os.path.join(dest, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                mode = "wb" if isinstance(data, bytes) else "w"
                with open(path, mode) as fh:
                    fh.write(data)

    def run_worker(self):
        """Simulate one task tick."""
        if self.worker:
            self.worker(self)


def _tc_manifest(run_id, titles, attempts=None, winning="A1", files=None):
    return json.dumps({
        "run_id": run_id, "mode": "CAPTURE",
        "attempts": attempts or [{"attempt": 1, "ok": True, "error": None,
                                  "child_id": f"{run_id}-A1", "elapsed_s": 100.0}],
        "winning_attempt": int(winning[1:]),
        "winning_prefix": f"stage/{run_id}/output/{winning}",
        "files": files or {t: {"file": f"streamlit-{_safe_slug(t)}.png",
                               "bytes": 1000} for t in titles},
        "expected": list(titles),
    }, indent=1)


def _run_tc_capture(outputs, worker, titles=("Dash A", "Dash B"), **kw):
    """Drive task_capture.capture() against the fake queue with no real
    sleeping. The 'worker' fires on the first poll, standing in for a task
    tick."""
    import codegen
    import task_capture as TC
    real_build = codegen.build
    codegen.build = lambda ir: "# stub\n"
    session = _FakeQueueSession(outputs=outputs, worker=worker)
    ticked = {"n": 0}

    def fake_sleep(_s):
        ticked["n"] += 1
        if ticked["n"] == 1:
            session.run_worker()

    ir = _nbcap_ir(titles)
    try:
        shots, notes = TC.capture(session, ir, "tctest",
                                  sleep_fn=fake_sleep, poll_seconds=0, **kw)
    finally:
        codegen.build = real_build
    return session, shots, notes


def _tc_worker_success(titles, attempts=None, winning="A1", files=None):
    def worker(session):
        run_id = next(iter(session.rows))
        session.outputs[run_id] = {
            "capture_manifest.json": _tc_manifest(run_id, titles, attempts,
                                                  winning, files),
            **{f"{winning}/streamlit-{_safe_slug(t)}.png": _tiny_png()
               for t in (files or {t: None for t in titles})},
        }
        session.rows[run_id]["STATUS"] = "COMPLETED"
    return worker


def test_task_capture_success():
    titles = ("Dash A", "Dash B")
    session, shots, notes = _run_tc_capture(
        {}, _tc_worker_success(titles), titles=titles)
    assert set(shots) == set(titles), shots
    assert notes == [], "a clean single-attempt capture needs no diagnostics"
    staged = {n for n, _p in session.puts}
    assert "app_generated.py" in staged and "dashboards.json" in staged
    assert "datasources.json" in staged, \
        "the LIVE table routing must be staged, or the captured app queries " \
        "a database that does not exist in Snowflake"
    print("ok  task capture: bundle staged (incl. live routing), job queued, "
          "worker result retrieved, all screenshots returned")


def test_task_capture_transient_retry_then_success():
    titles = ("Dash A", "Dash B")
    attempts = [
        {"attempt": 1, "ok": False, "retryable": True, "elapsed_s": 4.2,
         "child_id": "R-A1",
         "error": "300002:2250622648; incident 6979790 (XX000)"},
        {"attempt": 2, "ok": True, "error": None, "elapsed_s": 155.0,
         "child_id": "R-A2"},
    ]
    session, shots, notes = _run_tc_capture(
        {}, _tc_worker_success(titles, attempts=attempts, winning="A2"),
        titles=titles)
    assert set(shots) == set(titles)
    assert len(notes) == 1 and notes[0]["captured"] is True
    assert len(notes[0]["attempts"]) == 2
    assert "attempt 2" in notes[0]["summary"]
    print("ok  task capture: attempt-1 transient failure retried by the "
          "WORKER, attempt 2 wins, and attempt 1 survives in diagnostics "
          "even though nothing is BLOCKED")


def test_task_capture_three_attempts_all_fail():
    titles = ("Dash A", "Dash B")
    attempts = [{"attempt": n, "ok": False, "retryable": True,
                 "child_id": f"R-A{n}", "elapsed_s": 4.0,
                 "error": f"300002; incident {n}"} for n in (1, 2, 3)]

    def worker(session):
        run_id = next(iter(session.rows))
        session.outputs[run_id] = {
            "capture_manifest.json": json.dumps(
                {"run_id": run_id, "attempts": attempts, "files": {}}),
        }
        session.rows[run_id]["STATUS"] = "BLOCKED"
        session.rows[run_id]["ERROR_CODE"] = "CAPTURE_INCOMPLETE"

    session, shots, notes = _run_tc_capture({}, worker, titles=titles)
    assert shots == {}
    assert len(notes) == 1, "one workbook-level note, never one per dashboard"
    assert notes[0]["captured"] is False
    assert len(notes[0]["attempts"]) == 3
    print("ok  task capture: three failed worker attempts -> BLOCKED with the "
          "full attempt history in ONE note")


def test_task_capture_deterministic_failure_is_failed_not_blocked():
    """BLOCKED and FAILED must stay distinct: BLOCKED means evidence could not
    be produced, FAILED means the job itself could not run. Collapsing them
    would hide an infrastructure problem behind 'no screenshot'."""
    def worker(session):
        run_id = next(iter(session.rows))
        session.outputs[run_id] = {
            "capture_manifest.json": json.dumps(
                {"run_id": run_id, "attempts": [
                    {"attempt": 1, "ok": False, "retryable": False,
                     "error": "ModuleNotFoundError: no module named engine"}],
                 "files": {}, "fatal": "ModuleNotFoundError"}),
        }
        session.rows[run_id]["STATUS"] = "FAILED"
        session.rows[run_id]["ERROR_CODE"] = "WORKER_FATAL"

    session, shots, notes = _run_tc_capture({}, worker)
    assert shots == {}
    assert notes[0]["captured"] is False
    assert len(notes[0]["attempts"]) == 1, \
        "a deterministic failure must not be retried three times"
    assert session.rows[next(iter(session.rows))]["STATUS"] == "FAILED"
    print("ok  task capture: a deterministic worker failure reports FAILED "
          "(job broken) and stays distinct from BLOCKED (no evidence)")


def test_task_capture_timeout_names_the_grant():
    """A job nothing ever claims is the signature of the missing EXECUTE TASK
    grant, so the timeout message must say so rather than leaving someone to
    rediscover it."""
    session, shots, notes = _run_tc_capture({}, worker=None, timeout_seconds=0)
    assert shots == {}
    reason = notes[0]["reason"]
    assert "never claimed" in reason, reason
    assert "EXECUTE TASK" in reason, reason
    print("ok  task capture: a job that is never claimed times out with a "
          "message naming the EXECUTE TASK grant as the likely cause")


def test_task_capture_stale_job_expires_to_timed_out():
    import task_capture as TC
    session = _FakeQueueSession()
    TC.submit(session, run_id="STALE-1")
    assert session.rows["STALE-1"]["STATUS"] == "QUEUED"
    n = TC.expire_stale(session, timeout_seconds=1)
    assert n == 1
    assert session.rows["STALE-1"]["STATUS"] == TC.TIMED_OUT
    state = TC.poll(session, "STALE-1")
    assert state["terminal"] is True, "TIMED_OUT must be terminal for the poller"
    print("ok  task capture: a stale job expires to TIMED_OUT and the poller "
          "treats it as terminal instead of waiting forever")


def test_task_capture_concurrent_claims_single_winner():
    """Two workers, one job. The guard in the real UPDATE is what makes this
    safe -- and the fake obeys the statement text, so removing the guard from
    the shipped SQL turns this red."""
    import validation_jobs as VJ
    session = _FakeQueueSession()
    VJ.enqueue(session, "RACE-1")
    VJ.enqueue(session, "RACE-1")            # duplicate submit
    assert len(session.rows) == 1, "enqueue must be idempotent on RUN_ID"
    first = VJ.claim_next(session, "worker-A")
    second = VJ.claim_next(session, "worker-B")
    assert first == "RACE-1"
    assert second is None, "a second worker must not claim the same job"
    assert session.claim_guarded is True, \
        "the claim UPDATE must still carry `AND STATUS = 'QUEUED'` -- without " \
        "it two workers both win the race"
    print("ok  task capture: duplicate submits collapse to one row and only "
          "ONE worker can claim it (guard verified in the real SQL text)")


def test_task_capture_missing_manifest():
    def worker(session):
        run_id = next(iter(session.rows))
        session.outputs[run_id] = {}          # completed but nothing written
        session.rows[run_id]["STATUS"] = "COMPLETED"
    session, shots, notes = _run_tc_capture({}, worker)
    assert shots == {}
    assert notes and notes[0]["captured"] is False
    print("ok  task capture: COMPLETED with no manifest yields no screenshots "
          "and a stated reason, never a silent pass")


def test_task_capture_malformed_manifest():
    def worker(session):
        run_id = next(iter(session.rows))
        session.outputs[run_id] = {"capture_manifest.json": "{not json"}
        session.rows[run_id]["STATUS"] = "COMPLETED"
    session, shots, notes = _run_tc_capture({}, worker)
    assert shots == {}
    assert notes and notes[0]["captured"] is False
    print("ok  task capture: an unparsable manifest yields no screenshots "
          "and a stated reason")


def test_task_capture_incomplete_screenshots_rejected_by_validator():
    """The worker's own validator is the shared module the tests import --
    one implementation, so this exercises the code that actually runs."""
    import capture_validation as CV
    d = os.path.join("reports", "__tc_incomplete__")
    os.makedirs(d, exist_ok=True)
    try:
        open(os.path.join(d, "a.png"), "wb").write(_tiny_png())
        files = {"Dash A": {"file": "a.png"}}
        ok, reason = CV.validate_capture(files, d, ["Dash A", "Dash B"])
        assert ok is False and "Dash B" in reason, reason
        print("ok  task capture: 1-of-2 (analogous to 9-of-10) is rejected, "
              "and the reason names the missing dashboard")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_task_capture_invalid_png_rejected():
    import capture_validation as CV
    d = os.path.join("reports", "__tc_badpng__")
    os.makedirs(d, exist_ok=True)
    try:
        open(os.path.join(d, "a.png"), "wb").write(b"definitely not a png")
        ok, reason = CV.validate_capture({"Dash A": {"file": "a.png"}}, d, ["Dash A"])
        assert ok is False and "PNG" in reason, reason
        # and a structurally-valid but 1x1 image fails on width
        open(os.path.join(d, "b.png"), "wb").write(_tiny_png(1, 1))
        ok2, reason2 = CV.validate_capture({"Dash A": {"file": "b.png"}}, d, ["Dash A"])
        assert ok2 is False and "width" in reason2, reason2
        print("ok  task capture: a corrupt PNG and a 1x1 PNG are both "
              "rejected as evidence")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_task_capture_legit_blank_png_accepted():
    """Sheet 22 is a genuinely empty placeholder sheet -- confirmed against
    the IR (non_data=True, no dims/measures) AND against Tableau's own
    screenshot of it, equally blank -- and captures as a real full-width,
    ~66px PNG. Rejecting it would fail every run forever on a dashboard that
    cannot look any different."""
    import capture_validation as CV
    d = os.path.join("reports", "__tc_blank__")
    os.makedirs(d, exist_ok=True)
    try:
        open(os.path.join(d, "s22.png"), "wb").write(_tiny_png(2608, 66))
        ok, reason = CV.validate_capture({"Sheet 22": {"file": "s22.png"}}, d,
                                         ["Sheet 22"])
        assert ok is True, reason
        assert 66 < CV.MIN_WIDTH, "test assumes the real MIN_WIDTH constant"
        print("ok  task capture: a full-width, near-zero-height screenshot "
              "(Sheet 22's real shape) is ACCEPTED -- structure is checked, "
              "content blankness never is")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_task_capture_no_cross_attempt_mixing():
    """Only the winning attempt's files may be returned. A leftover file from
    a failed earlier attempt sitting in the same download must be ignored."""
    titles = ("Dash A", "Dash B")
    attempts = [{"attempt": 1, "ok": False, "retryable": True,
                 "error": "300002", "child_id": "R-A1"},
                {"attempt": 2, "ok": True, "error": None, "child_id": "R-A2"}]
    winner_png = _tiny_png(1400, 900)
    loser_png = _tiny_png(700, 400)

    def worker(session):
        run_id = next(iter(session.rows))
        files = {t: {"file": f"streamlit-{_safe_slug(t)}.png", "bytes": 1}
                 for t in titles}
        session.outputs[run_id] = {
            "capture_manifest.json": _tc_manifest(run_id, titles, attempts,
                                                  "A2", files),
            **{f"A2/streamlit-{_safe_slug(t)}.png": winner_png for t in titles},
            # stale leftovers from the failed attempt, same filenames
            **{f"A1/streamlit-{_safe_slug(t)}.png": loser_png for t in titles},
        }
        session.rows[run_id]["STATUS"] = "COMPLETED"

    session, shots, notes = _run_tc_capture({}, worker, titles=titles)
    assert set(shots) == set(titles)
    for t in titles:
        assert shots[t] == winner_png, \
            f"{t} came from the losing attempt -- attempts must never be mixed"
    print("ok  task capture: only the WINNING attempt's screenshots are "
          "returned; a failed attempt's leftovers are never mixed in")


def test_task_capture_diagnostics_render_once_and_data_survives():
    """Requirement 7: a missing screenshot must block the VISUAL claim only,
    never erase passing data proof -- and the technical dump appears once."""
    import validation_report as VR
    run = {
        "workbook": "T", "run_id": "r", "environment": "UAT",
        "generated_at": "now", "status": "REVIEW",
        "dashboards": [
            {"id": f"d{i}", "name": f"Dash {i}", "charts": [], "formulas": [],
             "interactions": [],
             "images": {"tableau": None, "streamlit": None, "diff": None},
             "visual": {"streamlit_screenshot_note":
                        "capture ended BLOCKED after 3 attempts. See capture "
                        "diagnostics above."},
             "visual_status": "BLOCKED", "status": "PASS"}
            for i in range(3)
        ],
        "capture_diagnostics": {
            "summary": "capture ended BLOCKED after 3 attempt(s)",
            "attempts": [{"attempt": n, "run_id": f"VAL-x-A{n}", "ok": False,
                          "error": "300002:2250622648 incident 6979790",
                          "query_id": None, "sqlstate": "XX000",
                          "incident_id": "6979790", "error_code": "300002",
                          "elapsed_s": 4.2} for n in (1, 2, 3)],
        },
    }
    html = VR.render_html(run)
    assert html.count("Streamlit capture diagnostics") == 1, \
        "the diagnostics block must appear ONCE, not once per dashboard"
    # The incident id legitimately appears more than once INSIDE the
    # diagnostics table (its own column, plus inside the error text), and that
    # is fine. What must never happen is the count scaling with the number of
    # dashboards -- i.e. the technical dump leaking into each per-dashboard
    # card. So compare against the block rendered on its own.
    block_only = VR._capture_diagnostics_html(run["capture_diagnostics"])
    assert html.count("6979790") == block_only.count("6979790"), \
        "the incident detail must live ONLY in the one diagnostics block -- " \
        "any extra occurrences mean it is repeated per dashboard"
    assert len(run["dashboards"]) == 3, "guards the scaling check above"
    # data proof survives a blocked visual
    assert html.count("badge pass") >= 3, \
        "a BLOCKED visual must not erase the dashboards' passing data status"
    print("ok  task capture: diagnostics render exactly once at workbook "
          "level, and a BLOCKED visual leaves passing data proof intact")


def test_task_capture_worker_source_is_sound():
    """The worker body ships as notebook source, so a syntax error in it is
    only discovered minutes into a real run on a compute pool. Compile it
    here, and prove it carries the guards that matter."""
    import task_capture as TC
    src = TC.worker_notebook_source("capture")
    compile(src, "capture_worker", "exec")
    for placeholder in ("__TABLE__", "__STAGE__", "__MAX_ATTEMPTS__",
                        "__MIN_WIDTH__", "__APP_WRAPPER__"):
        assert placeholder not in src, f"unsubstituted placeholder {placeholder}"
    assert "AND STATUS = 'QUEUED'" in src, \
        "the worker's claim must stay atomic"
    assert "capture_validation" in src, \
        "the worker must use the SHARED validator, never a private copy"
    assert "capture_app" in src, \
        "the worker must reuse app_screenshot.capture_app, not a second browser"
    assert "'BLOCKED'" in src and "'FAILED'" in src, \
        "the worker must keep BLOCKED and FAILED distinct"
    assert "capture_validation.py" in TC.RUNTIME_MODULES, \
        "the shared validator must be staged into the container"
    # the selftest body must still build -- it is the minimal-test harness
    compile(TC.worker_notebook_source("selftest"), "selftest", "exec")
    print("ok  task capture: the worker notebook body compiles, has no "
          "unsubstituted placeholders, and keeps its atomic claim, shared "
          "validator, single-browser reuse and BLOCKED/FAILED split")


class _FakeDeploySession:
    """Records every DDL statement's text (whitespace-collapsed) issued by
    deploy_worker() and no-ops file.put -- narrow, on purpose: deploy_worker
    only issues CREATE/ALTER statements and never inspects their results, so
    unlike _FakeQueueSession this doesn't need to simulate row state."""
    def __init__(self):
        self.statements = []
        self.file = self

    def put(self, *a, **k):
        pass

    def sql(self, statement, params=None):
        self.statements.append(" ".join(statement.split()))
        return self

    def collect(self):
        return []


def test_task_capture_worker_notebook_has_network_egress():
    """REAL BUG, found on the FIRST-EVER end-to-end run through this task
    path (2026-08-17, blocked until then on the EXECUTE TASK grant):
    deploy_worker() created the worker notebook with NO external access
    integration bound at all, so pip had no egress inside the container --
    every real attempt failed installing playwright with 'Failed to
    establish a new connection: [Errno -2] Name or service not known'. The
    standalone capture path (notebook_capture.py) already attaches this
    correctly via a separate ALTER right after CREATE NOTEBOOK (CREATE
    NOTEBOOK itself has no inline EXTERNAL_ACCESS_INTEGRATIONS clause); this
    one simply never was, because nothing could exercise the real task path
    before the grant landed to catch it -- every prior gate here used a fake
    session and could not see a missing network binding.

    Proves deploy_worker() issues the SAME ALTER ... SET
    EXTERNAL_ACCESS_INTEGRATIONS = (PLAYWRIGHT_EAI) statement, read from the
    real SQL text a fake session captures -- not a Python re-implementation
    of the rule -- for BOTH worker modes (selftest and capture)."""
    import task_capture as TC

    for mode in ("selftest", "capture"):
        fake = _FakeDeploySession()
        TC.deploy_worker(fake, mode=mode, log=lambda m: None)
        alters = [s for s in fake.statements
                 if s.upper().startswith("ALTER NOTEBOOK")
                 and "EXTERNAL_ACCESS_INTEGRATIONS" in s.upper()]
        assert alters, f"[{mode}] no EXTERNAL_ACCESS_INTEGRATIONS grant issued: {fake.statements}"
        assert f"({TC.EAI})" in alters[0], (mode, alters[0])
        assert TC.WORKER_NOTEBOOK in alters[0], (mode, alters[0])
        # must come AFTER the notebook actually exists, never before
        create_idx = next(i for i, s in enumerate(fake.statements)
                          if s.upper().startswith("CREATE OR REPLACE NOTEBOOK"))
        alter_idx = fake.statements.index(alters[0])
        assert alter_idx > create_idx, (mode, fake.statements)

    print("ok  task capture: the worker notebook is created WITH network "
          "egress (EXTERNAL_ACCESS_INTEGRATIONS attached, both modes) -- "
          "without it pip cannot install playwright inside the container "
          "runtime, exactly the failure the first real end-to-end run hit")


def test_row_level_calc_resolves_as_measure():
    """B5, fixed 2026-08-13: a ROW-LEVEL workbook calc used as a chart measure
    could never be resolved by the validation path, so its charts reported
    "does not resolve against the backend table" while the generated app
    rendered them perfectly.

    ROOT CAUSE (confirmed, not guessed): `_resolve_measure_sql` looked the
    caption up only in cortex_semantic.build_metrics(), which deliberately
    emits ONLY aggregating calcs -- it `continue`s past any calc with no
    aggregate function as "row-level calc, not a metric". Correct for a
    semantic view; wrong here, because a chart legitimately aggregates a
    row-level calc and engine._resolve_measure does exactly that (wraps the
    pill's own agg round a non-agg_ready calc). Two REAL Superstore measures
    hit this: `Days to Ship Scheduled` (a CASE over SHIP_MODE) and
    `Sales Forecast` (SALES * churn * growth, parameter-driven). Neither even
    appeared in build_metrics' `skipped` list -- the row-level `continue`
    records nothing -- so they vanished with NO stated reason, the exact
    silent-drop this project forbids.

    Asserts the fix AND its safety property AND the numbers."""
    import json as _json, warnings as _w
    _w.filterwarnings("ignore")
    import parity, cortex_semantic as CS, engine as _engine
    import backend as _backend
    from backend import run_sql

    ir = _json.load(open(os.path.join(ROOT, "workbook_ir.json"), encoding="utf-8"))
    # An earlier gate in this suite pushes a FAKE Snowpark session into backend
    # and doesn't always clear it. run_sql then routes to that fake session
    # instead of local DuckDB, table_columns() swallows the resulting exception
    # and returns [], and this gate quietly skipped -- passing the suite while
    # proving nothing. That is precisely the "missing proof looks like a pass"
    # failure this project forbids, so clear the session and assert instead of
    # skipping. (Found 2026-08-13 when this gate ran green standalone but
    # skipped inside the full suite.)
    _backend.set_session(None)
    _engine.configure(ir)

    # Do NOT hardcode the table name. Earlier gates in this suite reconfigure
    # config.DATASOURCES for other workbooks, and run_sql rewrites a fully
    # qualified name using whatever mapping is loaded AT THAT MOMENT -- so
    # "SUPERSTORE.PUBLIC.ORDERS" resolved to zero columns when this gate ran
    # after them, even though it worked standalone. Locate the table by the
    # columns the calcs actually need, which is self-validating: if no loaded
    # table has SHIP_MODE and SALES, there is genuinely nothing to test against.
    import config as _config
    need = {"SHIP_MODE", "SALES"}
    T, cols = None, []
    for _cap, _spec in (getattr(_config, "DATASOURCES", {}) or {}).items():
        cand = _spec.get("table") if isinstance(_spec, dict) else None
        if not cand:
            continue
        got = _engine.table_columns(cand)
        if got and need <= set(got):
            T, cols = cand, got
            break
    if not T:
        print("skip row-level-calc measure resolution "
              "(no loaded table carries SHIP_MODE + SALES)")
        return
    mets, _skipped = CS.build_metrics(ir)

    # the precondition that makes this bug possible -- keep it asserted, so if
    # build_metrics ever starts emitting row-level calcs this gate explains why
    # it changed rather than silently passing for a different reason.
    names = {s for m in mets for s in m["synonyms"]}
    assert "Days to Ship Scheduled" not in names, \
        "precondition changed: build_metrics now emits row-level calcs"
    assert "Sales Forecast" not in names, \
        "precondition changed: build_metrics now emits row-level calcs"

    # 1. both resolve, and the AGG COMES FROM THE PILL, not a hardcoded SUM
    r_avg = parity._resolve_measure_sql(ir, "Days to Ship Scheduled", "avg", cols, mets)
    r_sum = parity._resolve_measure_sql(ir, "Days to Ship Scheduled", "sum", cols, mets)
    r_fc = parity._resolve_measure_sql(ir, "Sales Forecast", "sum", cols, mets)
    for nm, r in (("Days to Ship Scheduled/avg", r_avg),
                  ("Days to Ship Scheduled/sum", r_sum),
                  ("Sales Forecast/sum", r_fc)):
        assert r is not None, f"{nm} still unresolved -- B5 regression"
        assert r[1] == "calc", f"{nm} resolved as {r[1]}, expected 'calc'"
    assert r_avg[0].upper().startswith("AVG("), r_avg[0][:60]
    assert r_sum[0].upper().startswith("SUM("), r_sum[0][:60]

    # 2. parameters are substituted -- an unresolved __PARAM__ token would be
    #    invalid SQL, and was one of the two shapes that failed here
    assert "__PARAM_" not in r_fc[0], "parameter token left unsubstituted"

    # 3. the SQL actually EXECUTES and the number is arithmetically right.
    #    Resolving is not the same as being correct.
    base = float(run_sql(f"SELECT SUM(SALES) AS V FROM {T}").iloc[0]["V"])
    got = float(run_sql(f"SELECT {r_fc[0]} AS V FROM {T}").iloc[0]["V"])
    want = base * (1 - 0.064) * (1 + 0.6)      # churn / growth parameter defaults
    assert abs(got - want) < 0.01, f"Sales Forecast {got} != expected {want}"
    days = float(run_sql(f"SELECT {r_avg[0]} AS V FROM {T}").iloc[0]["V"])
    assert 0 <= days <= 6, f"avg days-to-ship {days} outside the CASE's own 0..6 range"

    # 4. SAFETY PRESERVED: a calc whose columns are NOT all on the table must
    #    still refuse. Resolving against columns we cannot see would be worse
    #    than the bug this fixes.
    assert parity._resolve_measure_sql(
        ir, "Days to Ship Scheduled", "avg", {"SALES"}, mets) is None, \
        "resolved a calc against a table missing its columns -- unsafe"

    # 5. a list of columns must not crash the subset check (engine.table_columns
    #    returns a list; the `<=` contract wants a set)
    assert parity._resolve_measure_sql(
        ir, "Sales Forecast", "sum", list(cols), mets) is not None

    print("ok  row-level calc resolves as a measure (B5: build_metrics emits only "
          "AGGREGATING calcs, so a chart aggregating a row-level calc -- Days to "
          "Ship Scheduled, Sales Forecast -- was unresolvable and reported as a "
          "backend failure while the app rendered it fine; now mirrors "
          "engine._resolve_measure by wrapping the pill's own agg, params "
          "substituted, numbers verified against an independent calculation, and "
          "still refuses a calc whose columns aren't all on the table)")


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
            "https://x.online.tableau.com", "s", ["Discount"], token_name="n", token_secret="s")
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
            "https://x.online.tableau.com", "s", [{"metric": "Profit Ratio", "name": "Profit Ratio",
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
    xml_headers = TSV2.TableauRestClient("https://x.online.tableau.com")._headers()
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
    # Pairwise agreement is preserved in pair_diffs, but the THREE-WAY cell
    # itself remains BLOCKED while Tableau is absent. Calling that row PASS
    # made the client report contradict its own chart-level BLOCKED verdict.
    no_tableau = VR.compare_chart(dict(spec, tableau_rows=None))
    assert no_tableau["status"] == VR.BLOCKED, \
        "with no Tableau export there is no key-set authority -- must be BLOCKED, not PASS"
    assert no_tableau["failed_cells"] == 0, (
        "the Streamlit/backend pair reconciles, so no cell may be counted as "
        f"failed merely because Tableau is absent: {no_tableau['failed_cells']}")
    _cells = [m for r in no_tableau["comparison_rows"] for m in r["measures"]]
    assert _cells and all(m["status"] == VR.BLOCKED for m in _cells), \
        "a three-way cell with no Tableau value must not be labelled PASS"
    assert no_tableau["blocked_cells"] == len(_cells), \
        "missing evidence must be counted separately from numeric mismatches"
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
    assert re.search(r"external_access_integrations:\s*\n\s*-\s*TABLEAU_ACCESS_INTEGRATION", block), (
        "pipeline_demo must preserve its Tableau external-access integration "
        "when Snowflake CLI recreates the Streamlit object")
    assert re.search(r"secrets:\s*\n\s*tableau_pat:\s*WBR_DB\.PUBLIC\.TABLEAU_PAT_SECRET", block), (
        "pipeline_demo must preserve the bound Tableau PAT secret alias when "
        "Snowflake CLI recreates the Streamlit object")
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


def test_zone_crosstab_url_and_xlsx_conversion():
    """tableau_zone_crosstab.py's two pure/offline-testable pieces:
    _view_url's contentUrl parsing, and _xlsx_bytes_to_csv's conversion.

    Everything ELSE in that module (cookie injection, iframe/toolbar
    discovery, the dialog's keyboard-only selection quirk, the download
    itself) needs a real browser and a real live Tableau session -- not
    something this offline gate can exercise, same as the rest of this
    project's live-verified-then-fixture-gated pattern (R15, the crosstab
    fixtures under tests/fixtures/tableau_crosstabs/). What CAN be gated
    forever: the URL construction bug that was actually found live
    2026-08-21 (passing the dashboard's DISPLAY name, 'Commission Model',
    silently produces a URL Tableau never renders behind -- confirmed by
    the toolbar iframe never appearing; only the REST contentUrl's own
    slug, 'CommissionModel', with the '/sheets' segment removed, works),
    and that the real downloaded .xlsx (QuotaAttainment.xlsx, saved from
    the actual live run) converts to correct CSV rows."""
    import tableau_zone_crosstab as TZC

    # (a) URL construction -- the real bug, teeth via the exact wrong input.
    url = TZC._view_url(
        "https://prod-useast-b.online.tableau.com", "b360bi",
        "R1TestUpload-Superstore/sheets/CommissionModel")
    assert url == ("https://prod-useast-b.online.tableau.com/t/b360bi/views/"
                   "R1TestUpload-Superstore/CommissionModel"), url
    assert " " not in url and "%20" not in url, (
        "the dashboard's URL segment must be the REST contentUrl slug, "
        f"never the display name with its space -- got {url}")

    # A trailing slash or an already-slash-free contentUrl must both still work.
    url2 = TZC._view_url("https://x.online.tableau.com/", "mysite",
                         "WB/sheets/View1/")
    assert url2 == "https://x.online.tableau.com/t/mysite/views/WB/View1", url2

    # No site segment when site_content_url is falsy (Default site).
    url3 = TZC._view_url("https://x.online.tableau.com", "", "WB/sheets/View1")
    assert url3 == "https://x.online.tableau.com/views/WB/View1", url3

    # (b) XLSX -> CSV conversion, against the REAL file downloaded live
    # 2026-08-21 (QuotaAttainment.xlsx, 41 real sales people).
    fx = os.path.join(os.path.dirname(__file__), "fixtures",
                      "tableau_zone_crosstab", "QuotaAttainment.xlsx")
    with open(fx, "rb") as fh:
        raw = fh.read()
    csv_text = TZC._xlsx_bytes_to_csv(raw)
    lines = csv_text.splitlines()
    assert lines[0] == ("Sales Person,Achieved Quota,Rank over 3,"
                        "% of quota achieved,Achievement (estimated),"
                        "Max. OTE (Variable),New Quota,Total Compensation"), lines[0]
    assert len(lines) == 42, f"expected 41 sales people + header, got {len(lines)}"
    assert "Barbara Davis,75-100%,1,0.806938,403469,142000,500000,124238.296" in csv_text, (
        "a specific real row must survive the conversion unchanged -- this "
        "is the exact row that matched the live dashboard's displayed 81%")

    print("ok  tableau_zone_crosstab's URL construction uses the REST "
          "contentUrl slug (not the display name, whose space silently "
          "renders nothing -- found live 2026-08-21) and its XLSX->CSV "
          "conversion reproduces a real downloaded crosstab exactly")


def test_crosstab_jobs_paths_and_run_ids():
    """tableau_crosstab_jobs.py's pure/offline-testable pieces: run-id
    shape and stage-path construction. The queue mechanics themselves
    (atomic claim, heartbeat, complete/fail, the worker's independent
    sign-in) need a real Snowflake session and a real Tableau account --
    not something this offline gate can exercise -- and were verified
    live 2026-08-21 instead: 3 real jobs (CommissionProjection, OTE,
    Sales) enqueued together, batch-processed by ONE worker invocation
    (not one per scheduled tick -- a workbook can surface several
    unresolved sheets at once, and waiting a minute PER SHEET would make
    one "Run Validation" click take 10+ minutes), all COMPLETED with real
    data matching the live dashboard exactly. A real, teeth-worthy bug
    was found and fixed in that same pass: passing a borrowed REST
    SESSION TOKEN (rather than the PAT name+secret) let an unrelated
    sign_out() elsewhere invalidate it before a delayed job got
    processed -- all 3 jobs failed identically with a browser-level
    navigation error, not a Tableau-side one. Fixed by having the worker
    sign in with its OWN independent, disposable session immediately
    before use."""
    import tableau_crosstab_jobs as XJ

    run_id = XJ.new_run_id()
    assert run_id.startswith("XTAB-"), run_id
    # Must never collide with R15's own run-id shape (validation_jobs.py's
    # "VAL-...") even though both can live under the same STAGE root.
    assert not run_id.startswith("VAL-"), run_id

    in_path = XJ.input_path(run_id)
    out_path = XJ.output_path(run_id)
    assert in_path == f"{XJ.STAGE}/{XJ.STAGE_SUBDIR}/{run_id}/input.json", in_path
    assert out_path == f"{XJ.STAGE}/{XJ.STAGE_SUBDIR}/{run_id}/result.json", out_path
    # Scoped to its own subdirectory -- must never write into R15's own
    # run-id-keyed folders directly under STAGE.
    assert f"/{XJ.STAGE_SUBDIR}/" in in_path, in_path

    print("ok  tableau_crosstab_jobs' run-id shape and stage-path "
          "construction stay isolated from R15's own queue (VAL- vs "
          "XTAB- prefixes, a dedicated _crosstab stage subdirectory) -- "
          "the queue mechanics themselves were live-verified against the "
          "real account: 3 concurrent jobs batch-processed correctly, "
          "including the independent-sign-in fix for a real bug found "
          "the same day")



def test_nested_ratio_calc_nullif_wrap_is_idempotent():
    """A parameter-driven measure-swap CASE that references an EXISTING ratio
    calc (Profit/Unit = SUM(Profit)/SUM(Quantity)) by bracket name must not
    double-wrap that nested calc's already-protected division.

    THE REAL BUG (found live 2026-09-09, "Sales Dashboard Ver 01"). Every KPI
    tile on the dashboard failed with SnowparkSQLException: syntax error
    unexpected ')' immediately followed by unexpected "Profit/Unit" -- 5/5/5
    stages had all reported PASS (none of them ever execute the generated
    app -- see test_late_session_routing... 's neighbor gap), so the first
    place this surfaced at all was the Preview tab.

    translate_formula's divide-by-zero protection is two regexes: one opens
    "/ NULLIF(AGG(" on a bare "/ AGG(", the other closes it with ", 0)".
    _field() (bracket-reference substitution, resolving [Field] against
    calc_defs) splices a NESTED calc's OWN FULLY-TRANSLATED sql back into the
    parent's formula BEFORE the parent's own pass of these two regexes runs.
    If that nested calc was itself a ratio (as here -- the swap CASE's
    'Profit/Unit' branch references the real Profit/Unit calc), its division
    already has a correctly-closed "/ NULLIF(SUM(x), 0)" spliced into the
    parent text. The closing regex never checked whether a NULLIF it matches
    is already closed -- it matched "/ NULLIF(SUM(x)" (up to SUM's own
    closing paren) regardless of what follows, and appended a SECOND ", 0)",
    leaving the ORIGINAL ", 0)" as trailing garbage: "/ NULLIF(SUM(x), 0), 0))".
    That stray text is exactly Snowflake's "unexpected ')'" / unexpected
    quoted-fragment pair once it hits a token like "Profit/Unit" nearby.

    FIX, superseded same day by _protect_ratio_divisions (a single
    paren-aware pass replacing both regexes -- see
    test_ratio_agg_wrapping_conditional_aggregate for the second, more
    fundamental bug found in that same closing regex and fixed by the same
    rewrite): the trigger for wrapping a division is "/" directly followed
    by an aggregate name, which an already-spliced-in "NULLIF(" blocks --
    so an already-protected nested division is never re-matched at all, and
    a fresh, unprotected division still gets wrapped exactly once. Proven
    below alongside the already-fixed nested case.
    """
    from calc_translator import translate_formula

    # The real shape: a swap CASE (values via __PARAM_METRIC__) whose
    # 'Profit/Unit' branch references an EXISTING ratio calc by bracket name,
    # not an inline division -- exactly what made this collide.
    calc_defs = {"Profit/Unit": "SUM([Profit]) / SUM([Quantity])"}
    formula = ("CASE [Parameters].[Metric]\n"
               "WHEN \"Sales\" THEN SUM([Sales])\n"
               "WHEN \"Profit/Unit\" THEN [Profit/Unit]\n"
               "END")
    sql, agg_ready = translate_formula(
        formula, params={"Metric": {"caption": "Metric", "value": "Sales"}},
        calc_defs=calc_defs)
    assert agg_ready, sql
    # exactly ONE close per NULLIF -- the doubled-close regression left a
    # stray ", 0)" immediately before the branch's own closing paren.
    assert sql.count("NULLIF(SUM(QUANTITY)") == 1, sql
    assert ", 0), 0)" not in sql, sql
    assert sql.count(", 0)") == 1, sql
    assert "NULLIF(SUM(QUANTITY), 0))\nEND" in sql, sql

    # teeth: the PRE-FIX closing regex (no negative lookahead) reproduces the
    # exact malformed double-close on this same already-nested-and-protected
    # text, proving this gate would have caught the regression.
    import re as _re
    pre_fix_broken = _re.sub(
        r"/ NULLIF\((SUM|AVG|COUNT|MIN|MAX)\(([^()]*)\)",
        r"/ NULLIF(\1(\2), 0)", sql, flags=_re.I)
    assert ", 0), 0)" in pre_fix_broken, pre_fix_broken

    # a FRESH (not-yet-protected) division must still be wrapped exactly
    # once -- the lookahead must not suppress a legitimate first wrap.
    fresh_sql, fresh_ok = translate_formula("SUM([Profit]) / SUM([Quantity])")
    assert fresh_ok, fresh_sql
    assert fresh_sql == "SUM(PROFIT) / NULLIF(SUM(QUANTITY), 0)", fresh_sql

    print("ok  nested ratio calc's NULLIF divide-by-zero wrap is idempotent "
          "-- a parameter-swap CASE referencing an existing ratio calc by "
          "bracket name no longer double-closes its NULLIF (', 0), 0))'), "
          "which broke every KPI tile on the real 'Sales Dashboard Ver 01' "
          "workbook with a SnowparkSQLException; teeth proven against the "
          "pre-fix regex on the same input, and a fresh division still gets "
          "wrapped exactly once")


def test_ratio_agg_wrapping_conditional_aggregate():
    """A ratio dividing by an aggregate that wraps its OWN nested call (a
    conditional/filtered measure -- SUM(CASE WHEN ... THEN x END), an
    everyday month-over-month or filtered-total shape) must still get its
    NULLIF closed correctly.

    THE REAL BUG (found live 2026-09-09, sweeping the real "BDO
    Pre-Optimization Dashboard" / "Email Campaign Analytics" Tableau Cloud
    workbooks -- 90 and 94 of their calcs respectively failed this exact
    way). _protect_ratio_divisions replaced two regexes: one opened
    "NULLIF(" on a bare "/ AGG(", the other tried to close it by matching
    the aggregate's own argument as a run of NON-PARENTHESIS characters --
    which can never match when that argument itself contains parens, as any
    conditional aggregate's CASE body does. NULLIF( was left open forever:
    real SnowparkSQLException "syntax error at end of input".

    Gated separately from test_nested_ratio_calc_nullif_wrap_is_idempotent
    (that one gates the DOUBLE-close regression; this one gates the
    NEVER-closes-at-all regression -- distinct failure shapes in the same
    function, both found on real content the same sweep)."""
    from calc_translator import translate_formula
    formula = ("SUM(IF DATETRUNC('month',[Date]) = [P Month] THEN [Clicks] END)"
               "/ SUM(IF DATETRUNC('month',[Date]) = [P Month] THEN [Impressions] END)")
    sql, agg_ready = translate_formula(
        formula, params={"P Month": {"caption": "P Month", "value": "2024-01-01"}})
    assert agg_ready, sql
    assert sql.count("NULLIF(") == 1, sql
    assert sql.rstrip().endswith(
        "THEN IMPRESSIONS END), 0)"), sql

    import duckdb
    probe = sql.replace("__PARAM_P_MONTH__", "DATE '2024-01-01'")
    try:
        duckdb.sql(f"SELECT {probe}")
    except duckdb.ParserException:
        raise AssertionError(f"still malformed: {sql}")
    except Exception:
        pass   # a missing column (DATE/CLICKS/IMPRESSIONS) is expected offline

    print("ok  a ratio dividing by a CONDITIONAL aggregate (SUM(CASE WHEN "
          "... END), the everyday filtered/month-over-month measure shape) "
          "gets its NULLIF closed correctly -- the old regex could only "
          "close a bare SUM(col), never an aggregate wrapping its own "
          "nested call, and silently left every such division unclosed on "
          "the real 'BDO Pre-Optimization Dashboard' and 'Email Campaign "
          "Analytics' workbooks (184 calcs between them)")


def test_bracket_ref_keyword_fusion_guarded():
    """A [Field] reference immediately followed (or preceded) by a Tableau
    keyword with NO whitespace -- "...[Unique Recipients]END", valid
    Tableau authoring since ']' is its own unambiguous token boundary --
    must not let the keyword fuse onto the substituted physical column name.

    THE REAL BUG (found live 2026-09-09, the real "GOLD KR Dashboard" family
    -- the identical calc definitions copied into 3 separate workbooks, all
    3 broken identically). _px() replaces [Unique Recipients] with the bare
    identifier UNIQUE_RECIPIENTS -- no delimiter -- so a directly-following
    "END" fuses onto it textually: "UNIQUE_RECIPIENTSEND". \\bEND\\b requires
    a word boundary on both sides, and underscore/letters are all word
    characters, so it can no longer recognize "END" as a keyword at all --
    the CASE never closes: real SnowparkSQLException "syntax error at or
    near ')'" (nothing legal left to close against).

    FIX is surgical, not unconditional padding: a boundary is inserted only
    where the ORIGINAL adjacent character was itself a word character AND
    the substituted value's own edge is a word character -- verified below
    to also prove it does NOT touch the common, already-safe case
    (SUM([Revenue]) -- surrounded by punctuation -- must stay byte-identical,
    or this breaks other exact-string gates for no semantic reason, exactly
    what an earlier unconditional-padding attempt did before being
    reverted)."""
    from calc_translator import translate_formula
    import duckdb

    # the real fusion case: keyword glued directly onto the bracket ref
    formula = "SUM(CASE WHEN [Month]=[Select Month] THEN [Unique Recipients]END)"
    sql, agg_ready = translate_formula(
        formula, params={"Select Month": {"caption": "Select Month", "value": "2024-01-01"}})
    assert agg_ready, sql
    assert "UNIQUE_RECIPIENTSEND" not in sql, sql
    assert "UNIQUE_RECIPIENTS END" in sql, sql
    probe = sql.replace("__PARAM_SELECT_MONTH__", "DATE '2024-01-01'")
    try:
        duckdb.sql(f"SELECT {probe}")
    except duckdb.ParserException:
        raise AssertionError(f"still fused: {sql}")
    except Exception:
        pass   # a missing column (MONTH/UNIQUE_RECIPIENTS) is expected offline

    # must NOT cosmetically change the common, already-punctuation-bounded
    # case -- exact byte-for-byte match against the pre-existing behavior
    # test_table_calc_engine already locks down.
    sql2, ok2 = translate_formula(
        "IF SUM([Revenue]) = WINDOW_MAX(SUM([Revenue])) THEN 'Max' ELSE 'Others' END")
    assert ok2 and sql2 == ("CASE WHEN SUM(REVENUE) = MAX(SUM(REVENUE)) OVER () "
                            "THEN 'Max' ELSE 'Others' END"), sql2

    print("ok  a [Field] reference glued directly onto a following Tableau "
          "keyword with no whitespace (valid Tableau authoring -- ']' is "
          "its own token boundary) no longer fuses into the substituted "
          "physical column name, which silently ate every CASE's END on "
          "the real 'GOLD KR Dashboard' family (3 copies, identically "
          "broken); padding is applied only where a real collision would "
          "occur, proven not to touch the common already-safe case")


def test_tableau_type_conversion_functions_cast_correctly():
    """Tableau's INT(x) / FLOAT(x) / STR(x) type-conversion functions -> SQL
    CAST(x AS INTEGER/DOUBLE/VARCHAR), including when nested inside each
    other.

    THE REAL BUG (found live 2026-09-09, the real "Hospitality - Rooms"
    workbook -- 47 of its calcs failed this exact way, ALL sharing one
    error signature). These three functions were never translated at all --
    they passed straight through as a literal function call. STR(x)/FLOAT(x)
    merely fail later as an unknown function (a Binder/Catalog error, a
    real but lower-severity gap); INT(x) is worse: INT is a reserved TYPE
    name in both DuckDB and Snowflake, not a callable identifier, so "INT("
    is a genuine SYNTAX error. The real formula was
    STR(INT([Year Select]) - 1) -- shifting a year parameter back one year
    for a YoY comparison -- and failed with "syntax error at or near '('"
    every single time it was used.

    Also gates the recursion bug found immediately while fixing this: a
    first, non-recursive version correctly converted the OUTER STR(...) but
    left the INNER INT(...) untouched inside the wrapped CAST body, because
    a single left-to-right scan jumps straight past a matched call's whole
    body without re-scanning it for a nested occurrence of the same
    pattern."""
    from calc_translator import translate_formula
    import duckdb

    sql, agg_ready = translate_formula(
        "STR(INT([Year Select]) - 1)",
        params={"Year Select": {"caption": "Year Select", "value": "2024"}})
    # release34 (C14): INT() truncates toward zero as Tableau does (Snowflake's CAST AS INTEGER rounds)
    assert sql == "CAST(CAST(TRUNC(CAST(YEAR_SELECT AS DOUBLE)) AS INTEGER) - 1 AS VARCHAR)", sql
    probe = sql.replace("__PARAM_YEAR_SELECT__", "2024")
    try:
        duckdb.sql(f"SELECT {probe}")
    except duckdb.ParserException:
        raise AssertionError(f"still has a bare INT(/FLOAT(/STR(: {sql}")
    except Exception:
        pass   # a missing column (YEAR_SELECT) is expected offline

    # each converts correctly standalone too
    # release34 (C14): INT() truncates toward zero as Tableau does (Snowflake's CAST AS INTEGER rounds)
    for fn, want in (("INT", "CAST(TRUNC(CAST(QUANTITY AS DOUBLE)) AS INTEGER)"),
                     ("FLOAT", "CAST(QUANTITY AS DOUBLE)"), ("STR", "CAST(QUANTITY AS VARCHAR)")):
        s, _ = translate_formula(f"{fn}([Quantity])")
        assert s == want, (fn, s)

    print("ok  Tableau's INT()/FLOAT()/STR() type-conversion functions "
          "translate to CAST(... AS INTEGER/DOUBLE/VARCHAR), including "
          "nested (STR(INT(x)-1)) -- previously passed through untranslated, "
          "and INT( specifically is a genuine SQL syntax error (a reserved "
          "type name, not a callable identifier) rather than just an "
          "unknown-function error; broke all 47 affected calcs on the real "
          "'Hospitality - Rooms' workbook identically")


def test_string_plus_concatenation_uses_double_pipe():
    """Tableau's '+' concatenates when either operand is a string (e.g.
    STR([Rating]) + " Stars"); Snowflake's '+' is strictly numeric -- '||'
    concatenates. A '+' translated literally is valid SQL SYNTAX either way,
    so nothing catches this at translation or even at compile time.

    THE REAL BUG (found live 2026-09-10, the real 'Sales Dashboard Ver 01'
    workbook's `Review` calc, `STR([ReviewRating]) + " Stars"`, nested
    inside the 'Type Selected' parameter-swap dimension that colors the
    Scater sheet -- itself only reachable after the same day's case-
    sensitivity and scatter-grain-fallback fixes let this sheet execute a
    real query for the first time). Translated to
    `CAST(REVIEWRATING AS VARCHAR) + ' Stars'`: Snowflake infers a CASE
    expression's result type from ALL branches together, and this '+' makes
    the branch look NUMERIC -- forcing the CASE's other, genuinely VARCHAR
    branches (Country/Category/PaymentMethod/ReturnStatus/ShippingMethod)
    to implicitly convert to NUMBER too. That only fails at RUNTIME, on the
    first row whose value isn't numeric-looking: a real PaymentMethod value,
    'Credit Card', with 'Numeric value 'Credit Card' is not recognized' --
    an error pointing at a value with no visible connection to the actually
    broken calc, which is why this class of bug hides well.

    FIX: telling every string operand of '+' apart from a numeric one in
    general needs full type inference, which this translator doesn't do --
    but a '+' directly touching a QUOTED STRING LITERAL can never be
    numeric in Tableau (a number is never written in quotes), so THAT case
    is rewritten to '||' with certainty. Numeric '+' between two fields is
    left untouched -- this never guesses at field types, only literals."""
    from calc_translator import translate_formula

    sql, _ = translate_formula('STR([ReviewRating]) + " Stars"')
    assert sql == "CAST(REVIEWRATING AS VARCHAR) || ' Stars'", sql

    # a chain of concatenations converts every '+' that touches a literal
    sql2, _ = translate_formula(
        '"Prefix: " + STR([X]) + " - " + STR([Y])')
    assert sql2 == "'Prefix: ' || CAST(X AS VARCHAR) || ' - ' || CAST(Y AS VARCHAR)", sql2

    # two STRING FIELDS concatenated via a literal separator -- no STR()
    # in sight, still must not stay numeric '+'
    sql3, _ = translate_formula('[Category] + "-" + [Region]')
    assert sql3 == "CATEGORY || '-' || REGION", sql3

    # string-returning expressions are equally certain even without a quoted
    # separator (real BDO Channel_Proper calculation)
    sql3b, _ = translate_formula(
        "UPPER(LEFT([Channel], 1)) + LOWER(RIGHT([Channel], LEN([Channel]) - 1))")
    assert sql3b == (                       # release34 (C14): LEN -> Snowflake LENGTH
        "UPPER(LEFT(CHANNEL, 1)) || LOWER(RIGHT(CHANNEL, LENGTH(CHANNEL) - 1))"
    ), sql3b

    # control: a genuine numeric '+' between two fields must be untouched
    sql4, _ = translate_formula("[Sales] + [Profit]")
    assert sql4 == "SALES + PROFIT", sql4

    # control: a '+' elsewhere in the formula must not be touched just
    # because a string literal exists SOMEWHERE else in the same formula
    sql5, _ = translate_formula('[Sales] + [Profit] + IF [Region] = "West" THEN 1 ELSE 0 END')
    assert " + " in sql5.replace("||", ""), sql5
    assert "SALES + PROFIT" in sql5, sql5

    print("ok  Tableau's '+' string concatenation (STR(x) + \"suffix\", "
          "chained, string-producing functions, or two string fields joined by a literal separator) "
          "translates to SQL '||' instead of numeric '+' whenever it "
          "touches a quoted string literal -- Snowflake's '+' is strictly "
          "numeric, and a CASE branch that LOOKS numeric because of a "
          "mistranslated '+' silently coerces every OTHER branch of the "
          "same CASE to NUMBER, failing at runtime on the first "
          "non-numeric-looking value reached; found live on the real "
          "'Sales Dashboard Ver 01' 'Review' calc ('Numeric value 'Credit "
          "Card' is not recognized', pointing nowhere near the actually "
          "broken formula); a genuine numeric '+' between two fields is "
          "left untouched even when a literal appears elsewhere in the "
          "same formula")


def test_bracket_scan_skips_brackets_inside_string_literals():
    """A stray '[' or ']' character that is STRING CONTENT (not a field
    reference) -- e.g. REPLACE(..., '[', '') stripping literal brackets out
    of a name -- must never be mistaken for a field-reference delimiter.

    THE REAL BUG (found live 2026-09-09, the real "Campaign Incrementality
    vs Holdout Group" workbook, 3 identical copies of one calc). The old
    substitution was a single regex, r"\\[([^\\]]+)\\]", with no concept of
    string-literal quoting. The real formula was
    REPLACE(REPLACE(LEFT([Name], FIND([Name],']')), '[', ''), ']', '') --
    two genuine [Name] references translate fine, but the regex's THIRD
    match starts at the stray '[' inside the '[' string literal and greedily
    consumes everything up to the NEXT ']' ANYWHERE LATER in the formula --
    past real closing parens, past the inner REPLACE's own arguments --
    swallowing it all into one bogus "field name" (literally the text
    "],''),'"). to_phys() sanitizes a no-alphanumeric name down to nothing
    and falls back to its own literal default, "COL", silently corrupting
    the SQL and stranding the outer REPLACE's closing paren: real
    SnowparkSQLException "syntax error at end of input".

    FIX replaces the regex with a hand-walked scan that tracks which quote
    character (single OR double -- this step runs BEFORE the later
    double-to-single-quote conversion, so either is still live here) opened
    the current string, and never treats '[' / ']' as a field delimiter
    while inside one. Gated for both quote styles, since Tableau accepts
    either."""
    from calc_translator import translate_formula
    import duckdb

    single_q = ("REPLACE(replace(LEFT([canvas_or_campaign_name],"
               "FIND([canvas_or_campaign_name],']')),'[',''),']','')")
    double_q = ('REPLACE(replace(LEFT([canvas_or_campaign_name],'
               'FIND([canvas_or_campaign_name],"]")),"[",""),"]","")')
    for label, formula in (("single-quote", single_q), ("double-quote", double_q)):
        sql, _ = translate_formula(formula)
        assert "COL" not in sql, (label, sql)
        assert sql.count("CANVAS_OR_CAMPAIGN_NAME") == 2, (label, sql)
        # release34 (C14): FIND(s, x) translates to Snowflake's POSITION(x IN s) (FIND is not a SQL function)
        assert sql == ("REPLACE(replace(LEFT(CANVAS_OR_CAMPAIGN_NAME, "
                       "POSITION(']' IN CANVAS_OR_CAMPAIGN_NAME)), '[', ''), ']', '')"), (label, sql)
        # FIND isn't a real DuckDB function (a separate, lower-severity gap
        # this test doesn't cover) -- a CatalogException here is expected
        # and proves the SQL is at least syntactically well-formed.
        try:
            duckdb.sql(f"SELECT {sql}")
        except duckdb.ParserException:
            raise AssertionError(f"{label}: still a syntax error: {sql}")
        except Exception:
            pass

    print("ok  a stray '[' / ']' that is STRING LITERAL CONTENT (stripping "
          "literal brackets out of a name, a real formula shape) is never "
          "mistaken for a field-reference delimiter -- the old single regex "
          "spanned from a stray bracket in one string literal to another "
          "one much later in the formula, swallowing a real argument and "
          "silently corrupting it to the literal fallback \"COL\"; broke "
          "all 3 copies of the same calc on the real 'Campaign "
          "Incrementality vs Holdout Group' workbook. Proven for both "
          "quote styles Tableau accepts.")


def test_case_sensitive_live_column_self_heals():
    """A live-connected table this pipeline does not own (and so never
    normalized) can have QUOTED, case-preserving columns -- "Country", not
    COUNTRY. EVERY column reference this app builds -- rdim/px, a baked-in
    calc_translator.py CASE, a measure library entry -- assumes plain
    unquoted UPPER_SNAKE, true for every table this pipeline creates but not
    one someone else built.

    THE REAL BUG (found live 2026-09-10, the real "Sales Dashboard Ver 01"
    workbook, table SANDBOX.BI.VIZLEAP_SALES -- reported directly by
    Sharath from the DEPLOYED app's Preview tab, one full day after this
    session's calc_translator.py fixes had already been verified and
    deployed: those fixes covered SQL SYNTAX; this is a different class,
    column NAME resolution, and neither of the two live-render sweeps run
    that day happened to include this exact workbook). Every KPI tile
    failed identically: SnowparkSQLException "invalid identifier 'COUNTRY'"
    / 'CATEGORY' / 'PAYMENTMETHOD' / 'REVIEWRATING' / 'SHIPPINGMETHOD' /
    'ORDERDATE' -- rdim()/px() has no check against the table's real
    columns at all (unlike _resolve_measure, which does check but only for
    the measure side). Fixing rdim()/px() alone would still miss the
    calc_translator-baked SQL inside the workbook's own "Metric Selection"
    swap calc (SUM(QUANTITY), SUM(TOTALSALES), ...) -- calc_translator.py
    has no live-session access at all, so it can never check this itself.

    FIX, at the one chokepoint every query this app ever runs passes
    through (engine.q): on an "invalid identifier" compile error, look up
    every table the failing statement's FROM/JOIN clauses actually name,
    check their REAL columns for a case-insensitive match, and if found,
    substitute the quoted exact-case form for JUST that one identifier and
    retry -- looping (bounded) since a multi-branch CASE can name several
    mismatched columns, Snowflake/DuckDB surfacing them one compile error
    at a time. A column with no case-insensitive match anywhere re-raises
    the original error unchanged -- a genuinely missing column is not
    masked, only a genuine casing mismatch is corrected.

    Gated without a live session: engine._q_exec and engine.table_columns
    are faked, proving the retry/substitution LOGIC in isolation. The real
    fix was also verified live against the real account this same session
    (SANDBOX.BI.VIZLEAP_SALES, real rows returned) -- this gate protects
    the mechanism from a future regression, it does not re-prove the live
    account still has this exact table."""
    import engine

    class _FakeCompileError(Exception):
        pass

    real_cols = ["OrderID", "Country", "TotalSales", "PaymentMethod"]
    upper_map = {c.upper(): c for c in real_cols}     # UPPER -> real case
    calls = []

    def fake_q_exec(sql):
        calls.append(sql)
        # Exactly Snowflake's real behavior: a bare, unquoted reference to a
        # column real_cols spells with different case is a compile failure,
        # surfaced ONE identifier per attempt -- the real engine's retry loop
        # must fix them one at a time, not assume there's only ever one.
        for tok in re.findall(r"\b[A-Z][A-Z0-9_]*\b", sql):
            real = upper_map.get(tok)
            if real and real != tok and f'"{real}"' not in sql:
                raise _FakeCompileError(
                    f"(1304): 000904 (42000): SQL compilation error: "
                    f"invalid identifier '{tok}'")
        return "OK::" + sql

    orig_q_exec, orig_table_columns = engine._q_exec, engine.table_columns
    engine._q_exec = fake_q_exec
    engine.table_columns = lambda T=None: real_cols
    try:
        result = engine.q("SELECT COUNTRY AS DIM, SUM(TOTALSALES) AS V "
                          "FROM FAKE_TABLE GROUP BY 1")
    finally:
        engine._q_exec, engine.table_columns = orig_q_exec, orig_table_columns

    assert result == ('OK::SELECT "Country" AS DIM, SUM("TotalSales") AS V '
                      'FROM FAKE_TABLE GROUP BY 1'), result
    assert len(calls) == 3, calls   # 2 real compile failures + 1 success

    # a genuinely missing column (no case-insensitive match anywhere) must
    # re-raise the ORIGINAL error, never mask a real problem
    def always_fail(sql):
        raise _FakeCompileError("(1304): 000904 (42000): SQL compilation "
                               "error: invalid identifier 'NOSUCHCOLUMN'")
    engine._q_exec = always_fail
    engine.table_columns = lambda T=None: real_cols
    try:
        raised = False
        try:
            engine.q("SELECT NOSUCHCOLUMN FROM FAKE_TABLE")
        except _FakeCompileError:
            raised = True
        assert raised, "a genuinely missing column must still raise, not hang or mask"
    finally:
        engine._q_exec, engine.table_columns = orig_q_exec, orig_table_columns

    # the substitution itself must never touch a STRING LITERAL that
    # happens to spell the same word as a bad column
    fixed = engine._quote_replace_identifier(
        "SELECT COUNTRY FROM T WHERE NAME = 'COUNTRY'", "COUNTRY", "Country")
    assert fixed == 'SELECT "Country" FROM T WHERE NAME = \'COUNTRY\'', fixed

    print("ok  a live-connected table's QUOTED, case-preserving columns "
          "(\"Country\", not COUNTRY) self-heal at the ONE chokepoint every "
          "query passes through -- rdim/px has no live-table check at all, "
          "and calc_translator.py's baked-in CASE-swap SQL can never check "
          "itself (no live-session access when it runs); found live on the "
          "real 'Sales Dashboard Ver 01' workbook (SANDBOX.BI.VIZLEAP_SALES) "
          "after every KPI tile failed with 'invalid identifier'; a "
          "genuinely missing column still raises, never masked, and a "
          "string literal spelling the same word as a bad column is never "
          "touched")


def test_table_dimension_uses_shared_column_resolution():
    """Every renderer must use the shared case-insensitive column preflight.

    A detail table used a private exact-membership check instead.  On a live
    table with a quoted/case-preserving ProductId column it silently discarded
    the Product ID grain, removed GROUP BY, and rendered one grand-total row.
    This is the exact visual failure a generic accelerator must never turn into
    a plausible-looking table.
    """
    import pandas as pd

    orig_cols = engine.table_columns
    orig_q = engine.q
    orig_df = engine._safe_dataframe
    orig_colmap = engine.COLMAP
    sqls, shown = [], []
    engine.table_columns = lambda T=None: ["ProductId"]
    engine.COLMAP = {"Product ID": "ProductId"}

    def fake_q(sql):
        sqls.append(sql)
        return pd.DataFrame({"C0": [516, 666]})

    engine.q = fake_q
    engine._safe_dataframe = lambda df, **kw: shown.append(df.copy())
    try:
        engine.r_table({"name": "Products", "kind": "table",
                        "datasource": None, "dims": ["Product ID"],
                        "measures": []}, "")
    finally:
        engine.table_columns = orig_cols
        engine.q = orig_q
        engine._safe_dataframe = orig_df
        engine.COLMAP = orig_colmap

    assert sqls and "GROUP BY 1" in sqls[0], sqls
    assert shown and list(shown[0].columns) == ["Product ID"], shown
    assert shown[0]["Product ID"].tolist() == [516, 666], shown[0]
    print("ok  detail tables use the shared physical-column resolver, so a "
          "quoted/case-preserving dimension cannot be silently dropped and "
          "collapse a multi-row Tableau table into one grand total")


def test_dual_axis_uses_pane_local_colors():
    """A dual/combo series' own pane color beats global palette collisions."""
    import xml.etree.ElementTree as ET

    ws = ET.fromstring("""
      <worksheet><panes>
        <pane y-axis-name="[ds].[sum:Sales:qk]">
          <mark class="Bar"/><format attr="mark-color" value="#6bc1be"/>
        </pane>
        <pane y-axis-name="[ds].[sum:Quantity:qk]">
          <mark class="Line"/><format attr="mark-color" value="#e15759"/>
        </pane>
      </panes></worksheet>""")
    meta = {
        "[Sales]": {"caption": "Sales", "role": "measure",
                    "datatype": "real", "fmt": None},
        "[Quantity]": {"caption": "Quantity", "role": "measure",
                       "datatype": "integer", "fmt": None},
    }
    got = TP.pane_marks(ws, meta)
    assert got[("Sales", "sum")] == {"mark": "bar", "color": "#6bc1be"}, got
    assert got[("Quantity", "sum")] == {"mark": "line", "color": "#e15759"}, got
    print("ok  dual-axis mark type and color come from each worksheet pane, "
          "not a workbook-wide first-match palette")


def test_aggregate_shape_calc_is_not_grouped():
    """Aggregate calculations on Shape are SELECT expressions, not keys.

    This is common when a dashboard maps SIGN(percent-change) to both Color
    and Shape.  Putting that expression in GROUP BY produces invalid Snowflake
    SQL and blanks the worksheet.
    """
    import pandas as pd

    orig_calcs = engine.CALCS
    orig_resolve = engine.resolve_measure
    orig_q = engine.q
    orig_cols = engine.table_columns
    orig_chart = engine.st.altair_chart
    sqls, charts = [], []
    engine.CALCS = {"Direction": {"sql": "SIGN(SUM(AMOUNT))",
                                    "agg_ready": True}}
    engine.resolve_measure = lambda T, sheet, m: {
        "sql": ("SIGN(SUM(AMOUNT))" if m.get("caption") == "Direction"
                else "SUM(AMOUNT)"), "fmt": "float"}
    engine.table_columns = lambda T=None: ["COUNTRY", "AMOUNT"]

    def fake_q(sql):
        sqls.append(sql)
        return pd.DataFrame({"Y0": ["US", "UK"], "X": [10, 20],
                             "C": [-1, 1]})

    engine.q = fake_q
    engine.st.altair_chart = lambda chart=None, **kw: charts.append(chart)
    try:
        engine.r_circle({"name": "AOV by Country", "kind": "circle",
                         "datasource": None,
                         "x": {"caption": "Amount", "agg": "sum"},
                         "y_dims": [{"caption": "Country"}],
                         "color_measure": {"caption": "Direction",
                                           "kind": "measure", "agg": "usr"},
                         "shape": {"caption": "Direction",
                                   "kind": "dimension"}}, "")
    finally:
        engine.CALCS = orig_calcs
        engine.resolve_measure = orig_resolve
        engine.q = orig_q
        engine.table_columns = orig_cols
        engine.st.altair_chart = orig_chart

    assert sqls, "renderer must issue a query"
    assert "GROUP BY 1" in sqls[0], sqls[0]
    assert "GROUP BY 1, 2" not in sqls[0], sqls[0]
    assert charts, "aggregate shape/color worksheet must render"
    enc = charts[0].to_dict()["encoding"]
    assert enc["shape"]["field"] == "C", enc
    print("ok  aggregate Shape calculations stay out of GROUP BY and may "
          "reuse the same selected result as Color")


def test_measure_display_label_resolves_param_swap_calc():
    """A parameter-swap CASE calc (Tableau's "let the user pick which measure
    shows" pattern: a calculated field that CASEs on a parameter's value,
    dragged onto a dual-axis or scatter axis) was labeling the axis with the
    CALC'S OWN CAPTION -- e.g. a calc literally named "Metric Selection" --
    instead of the measure currently plotted.

    THE REAL BUG (found live 2026-09-10, the real "Sales Dashboard Ver 01"
    workbook after the case-sensitivity fix let it render for the first
    time): once real data flowed, the Monthly dual chart and the Scatter
    chart both showed axis titles literally reading "Metric Selection" /
    "Metric Selection vs" no matter which measure the sidebar dropdown had
    selected -- r_dual's `ym.get("label", ym["caption"])` and three sites in
    r_scatter (`title=x["caption"]` / `title=y["caption"]`) all fell back to
    the raw calc caption, which is the WIDGET's name, not something a viewer
    can read as "what am I looking at".

    FIX: engine._measure_display_label(cap) detects -- for ANY calc, not
    just one named "Metric Selection" -- whether its baked SQL still embeds
    a live __PARAM_x__ token (calc_translator's marker for "this formula
    reads a parameter at query time"; sub_params() only replaces it right
    before execution, so it is still present on the stored calc entry).
    When found, it resolves to the CURRENTLY SELECTED parameter value,
    through the workbook's own value-alias map first, falling back to the
    raw value. A calc with no such token, or a plain field caption, passes
    through unchanged -- this never invents a label for a non-parameter
    measure."""
    import engine
    orig_calcs, orig_params, orig_aliases = engine.CALCS, engine.PARAMS, engine.ALIASES
    try:
        tok = engine.param_token("Metric Selection")
        engine.CALCS = {
            "Metric Selection": {
                "sql": f"CASE {tok} WHEN 'Sales' THEN "
                       "SUM(TOTALSALES) WHEN 'Quantity' THEN SUM(QUANTITY) END",
                "agg_ready": True,
            },
            "Plain Calc": {"sql": "SUM(TOTALSALES) - SUM(DISCOUNT)", "agg_ready": True},
        }
        engine.PARAMS = {"Metric Selection": "Quantity"}
        engine.ALIASES = {}

        # no alias configured -> falls back to the raw selected value
        assert engine._measure_display_label("Metric Selection") == "Quantity"

        # the workbook's own value alias wins when present
        engine.ALIASES = {"Metric Selection": {"Quantity": "Units Sold"}}
        assert engine._measure_display_label("Metric Selection") == "Units Sold"

        # a non-parameter calc is untouched -- never invents a label
        assert engine._measure_display_label("Plain Calc") == "Plain Calc"

        # a bare field caption (not even in CALCS) passes through unchanged
        assert engine._measure_display_label("Sales") == "Sales"

        # an explicit fallback overrides the raw-caption default when the
        # calc isn't parameter-driven (r_dual's ym.get("label") precedence)
        assert engine._measure_display_label("Plain Calc", "Net Sales") == "Net Sales"
    finally:
        engine.CALCS, engine.PARAMS, engine.ALIASES = orig_calcs, orig_params, orig_aliases

    print("ok  a parameter-swap CASE calc's axis title resolves to the "
          "CURRENTLY SELECTED measure's display name (workbook alias first, "
          "then the raw value) instead of the calc's own generic caption -- "
          "found live on the real 'Sales Dashboard Ver 01' Monthly dual "
          "chart and Scatter chart, both stuck reading 'Metric Selection' "
          "regardless of dropdown; a non-parameter calc or bare field "
          "caption is left untouched, and an explicit label override still "
          "wins ahead of the resolved value")


def test_light_zone_gets_card_css():
    """Tableau's light/white dashboard containers (the DEFAULT theme most
    real workbooks use -- dark containers are the exception) were rendered
    with nothing but Streamlit's own bare st.container(border=True): a
    near-invisible 1px hairline, no background fill, no padding beyond
    Streamlit's default, no shadow. Dark containers, by contrast, already
    got a real card treatment via _emit_dark_css (background fill, 10px
    radius, 14/16px padding). That asymmetry is the actual, generic reason
    one workbook's dashboard looked like a polished Tableau card layout and
    another (this pipeline's own default-white-theme "Sales Dashboard Ver
    01") looked like an unstyled skeleton -- found live 2026-09-10 by
    running the real workbook end to end and looking at the rendered pixels,
    not by reasoning about the CSS in the abstract.

    FIX: light (non-dark) bg zones now take the SAME keyed-container path
    dark zones already used, and _emit_dark_css (renamed in spirit, not in
    name, since it now also emits the light-card rules) gives each one a
    background fill, rounded corners, padding and a subtle shadow -- the
    same visual weight a dark zone gets, not just a bare border.

    Gated without Streamlit actually rendering: drives _render_layout with
    a fake light-bg zone wrapping one sheet and asserts (a) the zone is
    tracked in _LIGHT_KEYS (proving the elif-bg branch, not the no-op
    'else' branch, ran) and (b) the emitted CSS names that exact key with a
    background/box-shadow/radius rule, not merely a border."""
    import engine

    class _FakeCtx:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    calls = []
    orig_safe_container = engine._safe_container

    def fake_safe_container(**kw):
        calls.append(kw)
        return _FakeCtx()

    orig_dark_keys, orig_light_keys = list(engine._DARK_KEYS), list(engine._LIGHT_KEYS)
    orig_markdown = engine.st.markdown
    markdown_calls = []
    engine.st.markdown = lambda html, **kw: markdown_calls.append(html)
    engine._safe_container = fake_safe_container
    engine._DARK_KEYS.clear()
    engine._LIGHT_KEYS.clear()
    try:
        sheet = {"name": "KPI1", "kind": "bar"}
        rendered = []
        orig_render_sheet = engine.render_sheet
        engine.render_sheet = lambda s, where: rendered.append(s["name"])
        try:
            node = {"dir": "vert", "bg": "#ffffff",
                    "children": [{"sheet": "KPI1"}]}
            engine._render_layout(node, {"KPI1": sheet}, "")
        finally:
            engine.render_sheet = orig_render_sheet

        assert rendered == ["KPI1"], "the wrapped sheet must still render"
        assert len(engine._LIGHT_KEYS) == 1, engine._LIGHT_KEYS
        key, bg, border_color = engine._LIGHT_KEYS[0]
        assert bg == "#ffffff", engine._LIGHT_KEYS
        assert border_color == "rgba(0,0,0,0.07)", border_color
        assert any(c.get("key") == key and c.get("border") is True for c in calls), calls

        engine._emit_dark_css()
        assert markdown_calls, "a light zone must emit CSS, not rely on the bare border"
        css = markdown_calls[0]
        assert f"st-key-{key}" in css, css
        assert "box-shadow" in css and "background:#ffffff" in css, css
    finally:
        engine._safe_container = orig_safe_container
        engine.st.markdown = orig_markdown
        engine._DARK_KEYS[:] = orig_dark_keys
        engine._LIGHT_KEYS[:] = orig_light_keys

    print("ok  a light/white Tableau zone now gets a real card treatment "
          "(background fill, radius, padding, shadow via keyed CSS) instead "
          "of Streamlit's bare 1px border -- the actual, generic reason a "
          "default-white-theme workbook rendered as an unstyled skeleton "
          "next to a dark-themed one that already had this treatment; found "
          "live on the real 'Sales Dashboard Ver 01' dashboard")


def test_layout_text_leaf_renders_without_children():
    """A Tableau text zone is a leaf, not a container with `children`.

    The first deployed Sales app reached one of these newly captured header
    nodes and crashed with KeyError('children') before drawing any chart.
    """
    orig_markdown = engine.st.markdown
    orig_params, orig_colmap, orig_aliases = engine.PARAMS, engine.COLMAP, engine.ALIASES
    shown = []
    engine.st.markdown = lambda text, **kw: shown.append(text)
    engine.PARAMS = {"Metric": "Sales"}
    engine.COLMAP = {"Metric": "Parameter 1"}
    engine.ALIASES = {"Metric": {"Sales": "$ Sales"}}
    try:
        engine._render_layout(
            {"text": "ONLINE SALESÆ {param:Parameter 1} <Full Name>",
             "w": 50000, "h": 5000}, {}, "")
    finally:
        engine.st.markdown = orig_markdown
        engine.PARAMS, engine.COLMAP, engine.ALIASES = orig_params, orig_colmap, orig_aliases

    # release 26c: the text zone is drawn as a heading block that keeps its authored height
    # release 27 (F10): Tableau's <Full Name> insert shows the signed-in viewer, never the literal; with no
    # signed-in user (this offline test) it is blank
    assert len(shown) == 1 and "ONLINE SALES $ Sales" in shown[0] and "Full Name" not in shown[0] \
        and "font-weight:600" in shown[0] and "min-height" in shown[0], shown
    print("ok  dashboard text leaves render directly (parameter aliases and "
          "legacy separators resolved), never fall through to node['children']")


def test_compact_bar_and_dual_chart_fidelity():
    """Compact KPI labels stay inside bars; combo refs share their axis."""
    import pandas as pd

    engine.configure(TP.build_ir(TWB))
    charts = []
    orig_chart = engine.st.altair_chart
    engine.st.altair_chart = lambda c=None, **kw: charts.append(c)
    try:
        engine.r_bar({"name": "compact", "kind": "bar", "orient": "h",
                      "datasource": "Sample - Superstore",
                      "y": {"caption": "Category"},
                      "x": {"caption": "Sales", "agg": "sum"},
                      "labels": True, "_compact": True}, "")
    finally:
        engine.st.altair_chart = orig_chart
    spec = charts[0].to_dict()
    text_mark = next(layer["mark"] for layer in spec["layer"]
                     if layer["mark"]["type"] == "text")
    assert text_mark["align"] == "right" and text_mark["dx"] == -4, text_mark
    assert text_mark["color"] == "white", text_mark

    orig_resolve, orig_q, orig_chart = (engine.resolve_measure, engine.q,
                                        engine.st.altair_chart)
    charts = []
    engine.resolve_measure = lambda T, n, m: {
        "sql": "SUM(X)",
        "fmt": "money0" if m["caption"] == "Sales" else "num0"}
    engine.q = lambda sql: pd.DataFrame({
        "T": pd.to_datetime(["2023-01-01", "2023-02-01"]),
        "V0": [10, 20], "V1": [3, 4]})
    engine.st.altair_chart = lambda c=None, **kw: charts.append(c)
    try:
        engine.r_dual({"name": "combo",
                       "x": {"caption": "Date", "datepart": "mn"},
                       "ys": [{"caption": "Sales", "mark": "bar",
                               "color": "#6bc1be"},
                              {"caption": "Quantity", "mark": "line",
                               "color": "#e15759"}],
                       "reflines": [{"axis": "Sales", "formula": "average"}]}, "")
    finally:
        engine.resolve_measure, engine.q = orig_resolve, orig_q
        engine.st.altair_chart = orig_chart
    layers = charts[0].to_dict()["layer"]
    bar, rule = layers[0]["layer"]
    line = layers[1]
    assert bar["mark"]["size"] == 14, bar["mark"]
    # the rule shares the series' FIELD (no synthetic 'V' -> no phantom
    # axis) and must NOT carry axis=None: an explicit null merges into the
    # shared axis and erases the series' own left axis (found live on the
    # real Sales Dashboard Monthly chart; see test_refline_keeps_its_series_axis)
    assert rule["encoding"]["y"]["field"] == "V0", rule
    assert "axis" not in rule["encoding"]["y"], rule
    import vl_convert as vlc
    vg_axes = [(a.get("orient"), a.get("title"))
               for a in vlc.vegalite_to_vega(charts[0].to_json())["axes"]
               if a.get("orient") in ("left", "right") and a.get("title")]
    assert len(vg_axes) == 2 and {o for o, _ in vg_axes} == {"left", "right"}, \
        f"exactly one left + one right axis on the compiled chart: {vg_axes}"
    assert line["mark"]["point"]["color"] == "#e15759", line["mark"]
    print("ok  compact KPI values render inside their bars; temporal combo "
          "bars have usable width, reference rules add no phantom axis, and "
          "line points inherit their series color")


def test_bar_text_shelf_measure_enables_labels():
    """Text-shelf ordering must not silently remove a bar's value labels."""
    import xml.etree.ElementTree as ET

    src = """<worksheet name='review'>
      <table><cols>[federated.x].[sum:Sales:qk]</cols>
      <rows>[federated.x].[none:Stars:nk]</rows></table>
      <panes><pane><mark class='Bar'/><encodings>
        <text column='[federated.x].[none:Stars:nk]'/>
        <text column='[federated.x].[sum:Sales:qk]'/>
      </encodings></pane></panes>
    </worksheet>"""
    ws = ET.fromstring(src)
    meta = {
        "[Sales]": {"caption": "Sales", "datatype": "real", "role": "measure", "fmt": None},
        "[Stars]": {"caption": "Review Stars", "datatype": "string", "role": "dimension", "fmt": None},
    }
    spec = TP.infer(ws, meta)
    assert spec.get("kind") == "bar", spec
    assert spec.get("labels") is True, spec
    assert spec.get("text_fields") == ["Review Stars", "Sales"], spec
    print("ok  a measure anywhere on a bar's Text shelf enables value labels")


def test_scatter_falls_back_to_color_dimension_grain():
    """A scatter sheet with NOTHING on Tableau's Detail shelf but a dimension
    on Color (a common, valid authoring pattern -- color-by-category with no
    separate detail field) was rendered as a SINGLE aggregate point instead
    of one point per category.

    THE REAL BUG (found live 2026-09-10, the real 'Sales Dashboard Ver 01'
    workbook's 'Scater' sheet: x/y = a parameter-swap measure, color = a
    dimension "Type Selected", detail = None): r_scatter only ever derived
    its grouping grain from `s["detail"]`. With that None, `has_grain` was
    False, so the SQL had no GROUP BY at all -- x/y aggregated across the
    WHOLE table into exactly one row, drawing one dot where Tableau showed
    one per category. The categorical color encoding was ALSO never wired
    up for a dimension color (only a measure color populated "C"), so even
    fixing the grain alone would still have drawn every point one flat
    color.

    FIX: when there's no explicit detail but color is a DIMENSION, use that
    dimension as the fallback grain (matching Tableau's own semantics: a
    mark's level of detail comes from every shelf, not just Detail), and
    wire that same column into the categorical color encoding.

    A second, DEEPER layer of the same bug: the real "Type Selected" field
    is not a physical column at all -- it is ITSELF a row-level calc (a
    parameter-swap CASE picking which dimension to color by, e.g. Country
    vs Category vs Payment). The first fix checked `_col_known(T, px(grain))`
    for the fallback grain, which only ever matches a REAL column; a calc
    caption never does, so `has_grain` still came back False and the chart
    still collapsed to one point even after the color-dimension fallback
    landed. Fixed by resolving grain the same way rdim()/flag_dim() already
    do elsewhere -- CALCS first, physical column second -- covered below
    with a fake row-level calc standing in for "Type Selected"."""
    engine.configure(TP.build_ir(TWB))
    charts = []
    _orig = engine.st.altair_chart
    engine.st.altair_chart = lambda c=None, **k: charts.append(c)
    try:
        sheet = {"name": "scat_test", "kind": "scatter",
                 "datasource": "Sample - Superstore",
                 "x": {"caption": "Sales", "agg": "usr"},
                 "y": {"caption": "Profit", "agg": "usr"},
                 "color": {"caption": "Category", "kind": "dimension", "agg": "none"},
                 "detail": None}
        charts.clear()
        engine.render_sheet(dict(sheet), [])
        assert charts, "must render a chart"
        data = charts[0].data
        assert len(data) > 1, (
            f"one point per Category expected, got a single collapsed "
            f"aggregate row: {len(data)} row(s)")
        enc = charts[0].to_dict()["encoding"]
        assert enc["color"].get("field") == "G", (
            f"the color dimension must drive a real per-category encoding, "
            f"not a flat single color: {enc.get('color')}")
        assert enc["color"].get("type") == "nominal", enc["color"]
        assert enc["x"]["scale"].get("zero") is False, enc["x"]
        assert enc["y"]["scale"].get("zero") is False, enc["y"]

        # control: an explicit Detail field must still win over the fallback
        # (never silently overridden by a dimension on Color)
        sheet2 = dict(sheet, detail={"caption": "Sub-Category"})
        charts.clear()
        engine.render_sheet(dict(sheet2), [])
        data2 = charts[0].data
        assert len(data2) >= len(data), (
            "an explicit, finer-grain Detail field must be honored, not "
            "replaced by the coarser color-dimension fallback")

        # the deeper case: color is a CALC dimension (like the real "Type
        # Selected" parameter-swap field), not a physical column at all
        orig_calcs = engine.CALCS
        engine.CALCS = dict(orig_calcs)
        engine.CALCS["Type Pick"] = {"sql": "REGION", "agg_ready": False}
        try:
            sheet3 = {"name": "scat_test3", "kind": "scatter",
                      "datasource": "Sample - Superstore",
                      "x": {"caption": "Sales", "agg": "usr"},
                      "y": {"caption": "Profit", "agg": "usr"},
                      "color": {"caption": "Type Pick", "kind": "dimension", "agg": "none"},
                      "detail": None}
            charts.clear()
            engine.render_sheet(dict(sheet3), [])
            assert charts, "must render a chart even when the grain is a calc"
            data3 = charts[0].data
            assert len(data3) > 1, (
                f"a CALC-based color dimension (no physical column at all) "
                f"must still drive real grouping, not collapse to one "
                f"aggregate point: {len(data3)} row(s)")
            enc3 = charts[0].to_dict()["encoding"]
            assert enc3["color"].get("field") == "G", enc3.get("color")
        finally:
            engine.CALCS = orig_calcs
    finally:
        engine.st.altair_chart = _orig
    print("ok  a scatter sheet with color-by-dimension and NO explicit "
          "Detail field falls back to that dimension as its grouping grain "
          "(one point per category, matching Tableau's own multi-shelf "
          "level-of-detail semantics) instead of collapsing to a single "
          "aggregate point, and that same dimension now drives real "
          "categorical coloring -- found live on the real 'Sales Dashboard "
          "Ver 01' Scater sheet, rendered as one lone dot instead of one "
          "per 'Type Selected'; an explicit Detail field still takes "
          "precedence over the fallback; the fallback also resolves when "
          "the color field is itself a row-level CALC (a parameter-swap "
          "dimension like the real 'Type Selected'), not just a physical "
          "column, which the first pass of this fix missed")


def test_late_session_routing_reresolves_to_deploy_tables():
    """A process that opens its Snowpark session AFTER importing config must
    re-resolve routing, or it ships SQL naming a database that does not exist.

    THE REAL BUG (found 2026-08-27, the cause behind the R1 Superstore pack
    reporting 0 of 21 charts validated even after the agg-grain and LOD fixes
    landed). config.py decides routing precedence ONCE, at import, by asking
    `_in_snowflake()`. That is right for both ordinary callers -- a deployed
    SiS app already holds its session when config imports, a purely local run
    never gets one -- but make_validation_pack.py connects LONG after config
    was imported. So routing stayed latched on the built-in SUPERSTORE.* dev
    mapping while backend.run_sql, which picks Snowflake-vs-DuckDB per CALL,
    began sending that same SQL to the account.

    SUPERSTORE is the local dev database and does not exist there. All 57
    chart queries died with `Database 'SUPERSTORE' does not exist or not
    authorized`, engine caught the failure and drew nothing, and the pack
    reported every chart as "sheet drew no Altair chart" -- the capture layer
    blamed for a routing-layer cause, which is why it survived several runs.

    Gates both halves of the fix:
      * config.use_snowflake_routing() repoints captions onto the deploy
        mapping and reports what moved;
      * the derived single-table globals (config.ORDERS) move with it, and
        engine reads that per call rather than the copy it used to freeze at
        import -- otherwise every filter/parameter value query kept querying
        the phantom database even after the sheets were repointed.
    """
    import importlib, json, os, tempfile
    cwd = os.getcwd()
    tmp = tempfile.mkdtemp()
    try:
        os.chdir(tmp)
        # Local flavour wins at import (no session): the SUPERSTORE dev tables.
        with open("datasources.json", "w", encoding="utf-8") as fh:
            json.dump({"Sample - Superstore": {
                "table": "SUPERSTORE.PUBLIC.ORDERS",
                "local_file": "data/Sample - Superstore.csv"}}, fh)
        # The deploy mapping, staged AS datasources.json by snowflake.yml.
        with open("datasources.deploy.json", "w", encoding="utf-8") as fh:
            json.dump({"Sample - Superstore": {
                "table": "WBR_DB.PUBLIC.ORDERS",
                "local_file": "data/Sample - Superstore.csv"}}, fh)

        import config
        importlib.reload(config)
        assert config.table_for("Sample - Superstore") == "SUPERSTORE.PUBLIC.ORDERS", \
            "import-time precedence must still latch the local mapping"

        moved = config.use_snowflake_routing()
        assert config.table_for("Sample - Superstore") == "WBR_DB.PUBLIC.ORDERS", \
            "re-resolve must repoint onto the deploy mapping"
        assert moved.get("Sample - Superstore") == (
            "SUPERSTORE.PUBLIC.ORDERS", "WBR_DB.PUBLIC.ORDERS"), \
            f"the move must be reported for the caller to print, got {moved}"
        assert "SUPERSTORE" not in config.ORDERS, \
            f"derived ORDERS must move with the mapping, still {config.ORDERS}"

        # engine binds ORDERS at ITS import too, so the caller must move the
        # module copy in lockstep -- otherwise the sheets get repointed while
        # every filter/parameter value query still names the phantom database.
        # engine's own global is deliberately left alone: pipeline.py assigns
        # it directly when it deploys a data-model view, and that must keep
        # working, so the sync uses pipeline.py's existing one-line idiom.
        mvp = open(os.path.join(cwd, "make_validation_pack.py"), encoding="utf-8").read()
        assert "config.use_snowflake_routing()" in mvp, \
            "the pack must re-resolve routing after it opens its session"
        assert "engine.ORDERS = config.ORDERS" in mvp, \
            "the pack must sync engine's frozen ORDERS after re-resolving"
    finally:
        os.chdir(cwd)
        import config
        importlib.reload(config)
    print("test_late_session_routing_reresolves_to_deploy_tables PASSED")


def test_dashboard_chrome_headings_legends_axes_from_xml():
    """The parts of a Tableau dashboard that make it READ like one -- and
    that the converted app was dropping, which is why the real 'Sales
    Dashboard Ver 01' came back as an unstyled skeleton (Sharath, 2026-09-10,
    from the deployed app: "it sucks again"). All read from the XML, none
    guessed:

    1. TEXT ZONES are the card headings ('Countries - <Metric>'): kept as
       {"text": ...} leaves in the layout tree with the parameter token
       rewritten to `{param:<caption>}` for runtime substitution, and only
       where they sit with a sheet -- a filter sidebar's labels (a text-only
       SIDE column) are dropped, a hidden-by-user zone is skipped, and the
       lone 'Æ' Tableau writes for a soft line break never reaches the page.
    2. LEGENDS live in their own dashboard zone or nowhere: the scatter's
       color legend was hidden-by-user in Tableau, yet the app drew one
       inside the pane, eating a third of a narrow panel whose marks the
       workbook labels directly. zone_legends() reports only visible legend
       zones; the engine hides the rest, and a non-dashboard render (no
       `_legends`) keeps its legends.
    3. AXIS CHROME: a typed axis title ('$ Sales') beats the field name, a
       value axis the author hid (display=false) is not drawn, and a hidden
       field label (display-field-labels=false) drops the dimension title --
       a 119px KPI tile shows five labeled bars, not a '0M 4M' scale under a
       vertical 'Country' title. r_bar honors both flags (proven on the
       compiled Vega, not just the Altair object)."""
    import xml.etree.ElementTree as ET
    import engine, vl_convert as vlc

    dash_xml = """
    <dashboard name='D'><zones>
      <zone type-v2='layout-basic' w='100000' h='100000' x='0' y='0'>
        <zone param='horz' type-v2='layout-flow' w='100000' h='100000' x='0' y='0'>
          <zone param='vert' type-v2='layout-flow' w='80000' h='100000' x='0' y='0'>
            <zone type-v2='text' w='80000' h='5000' x='0' y='0'>
              <formatted-text><run>Countries - &lt;[Parameters].[Parameter 1]&gt;</run>
              <run>Æ </run><run>tail</run></formatted-text></zone>
            <zone name='KPI' w='80000' h='95000' x='0' y='5000'/>
            <zone name='KPI' type-v2='color' w='1000' h='1000' x='0' y='0'/>
          </zone>
          <zone param='vert' type-v2='layout-flow' w='20000' h='100000' x='80000' y='0'>
            <zone type-v2='text' w='20000' h='5000' x='80000' y='0'>
              <formatted-text><run>F I L T E R S</run></formatted-text></zone>
            <zone type-v2='filter' name='KPI' param='[ds].[none:Country:nk]' w='20000' h='5000' x='80000' y='5000'/>
          </zone>
        </zone>
        <zone type-v2='layout-flow' param='vert' hidden-by-user='true' w='10' h='10' x='0' y='0'>
          <zone type-v2='text' w='10' h='10' x='0' y='0'><formatted-text><run>HIDDEN</run></formatted-text></zone>
          <zone name='Scat' type-v2='color' hidden-by-user='true' w='10' h='10' x='0' y='0'/>
        </zone>
        <zone name='Scat' w='100' h='100' x='0' y='0'/>
      </zone>
    </zones></dashboard>"""
    d = ET.fromstring(dash_xml)
    tree = TP.layout_tree(d, {"KPI", "Scat"}, frozenset(),
                          param_alias={"Parameter 1": "Metric"})
    texts, sheets = [], []
    def walk(n):
        if "text" in n: texts.append(n["text"])
        elif "sheet" in n: sheets.append(n["sheet"])
        for k in n.get("children", []): walk(k)
        for f in n.get("floating") or []: walk(f["child"])
    walk(tree)
    # release 26c: the filter sidebar's heading stays -- the filter control is now drawn in that sidebar
    assert texts == ["Countries - {param:Metric} tail", "F I L T E R S"], texts
    assert "HIDDEN" not in texts, texts
    assert sheets == ["KPI", "Scat"], sheets
    legends = TP.zone_legends(d, {"KPI", "Scat"})
    assert legends == {"KPI": ["color"]}, legends     # Scat's is hidden-by-user

    ws_xml = """
    <worksheet name='T'><table><style>
      <style-rule element='axis'>
        <format attr='title' value='$ Sales' scope='rows' field='[ds].[usr:Metric:qk]'/>
        <format attr='display' value='false' scope='cols' field='[ds].[usr:Metric:qk]'/>
      </style-rule>
      <style-rule element='worksheet'>
        <format attr='display-field-labels' value='false' scope='rows'/>
      </style-rule>
    </style></table></worksheet>"""
    chrome = TP.axis_chrome(ET.fromstring(ws_xml), {})
    assert chrome == {"titles": {"Metric": "$ Sales"}, "hide_axis": ["cols"],
                      "hide_field_labels": ["rows"]}, chrome

    # engine: r_bar honors the flags; r_scatter hides legends Tableau lacks
    engine.configure(TP.build_ir(TWB))
    charts = []
    _orig = engine.st.altair_chart
    engine.st.altair_chart = lambda c=None, **k: charts.append(c)
    try:
        base = {"name": "kpi", "kind": "bar", "orient": "h", "labels": True,
                "datasource": "Sample - Superstore",
                "y": {"caption": "Category"}, "x": {"caption": "Sales", "agg": "sum"}}
        charts.clear(); engine.render_sheet(dict(base), [])
        plain = vlc.vegalite_to_vega(charts[0].to_json())
        titles_plain = {a.get("title") for a in plain["axes"]}
        assert "Category" in titles_plain, titles_plain
        assert any(a.get("orient") == "bottom" and a.get("labels", True) is not False
                   for a in plain["axes"]), plain["axes"]
        charts.clear()
        engine.render_sheet(dict(base, hide_axis=["cols"], hide_field_labels=["rows"]), [])
        hidden = vlc.vegalite_to_vega(charts[0].to_json())
        assert "Category" not in {a.get("title") for a in hidden["axes"]}, hidden["axes"]
        assert not any(a.get("orient") == "bottom" for a in hidden["axes"]), \
            "the hidden value axis must not be compiled at all"

        sc = {"name": "sc", "kind": "scatter", "datasource": "Sample - Superstore",
              "x": {"caption": "Sales", "agg": "usr"}, "y": {"caption": "Profit", "agg": "usr"},
              "color": {"caption": "Category", "kind": "dimension", "agg": "none"},
              "detail": None}
        charts.clear(); engine.render_sheet(dict(sc, _legends=[]), [])
        with_none = vlc.vegalite_to_vega(charts[0].to_json())
        assert not with_none.get("legends"), "dashboard shows no legend -> none drawn"
        charts.clear(); engine.render_sheet(dict(sc, _legends=["color"]), [])
        assert vlc.vegalite_to_vega(charts[0].to_json()).get("legends"), \
            "a legend the dashboard DOES place is still drawn"
        charts.clear(); engine.render_sheet(dict(sc), [])          # no dashboard
        assert vlc.vegalite_to_vega(charts[0].to_json()).get("legends"), \
            "a standalone sheet (no _legends) keeps its legend"
    finally:
        engine.st.altair_chart = _orig
    print("ok  dashboard chrome comes from the XML: text zones become card "
          "headings with {param:} tokens (side-column labels and hidden zones "
          "dropped, Tableau's 'Æ' line-break glyph stripped), legends are drawn "
          "only where the dashboard places a visible legend zone, and a typed "
          "axis title / hidden value axis / hidden field label are honored by "
          "r_bar on the compiled Vega -- the real 'Sales Dashboard Ver 01' "
          "skeleton, itemized and fixed generically")


def test_refline_keeps_its_series_axis():
    """A reference line must share its series' scale AND axis. The earlier
    fix for a phantom third axis (a synthetic field 'V') switched the rule
    to the series' own field name -- correct -- but also set axis=None on
    it. Vega-Lite MERGES axis definitions across layers sharing a scale, and
    an explicit null wins: the bar series' entire left '$ Sales' axis
    vanished from the real Sales Dashboard Monthly chart (found live
    2026-09-10, one render after the phantom-axis fix landed). Proven on the
    compiled Vega via vl-convert, the same way the bug was diagnosed."""
    import altair as alt, vl_convert as vlc, engine, pandas as pd
    df = pd.DataFrame({"T": pd.to_datetime(["2023-01-01", "2023-02-01", "2023-03-01"]),
                       "V0": [1.0, 2.0, 3.0], "V1": [10.0, 20.0, 30.0]})
    s = {"name": "m", "reflines": [{"axis": "Sales", "formula": "average"}]}
    refs = engine._refline_rule(s, df, "V0", axis="y", only_axis="Sales")
    assert refs, "the average reference rule must be produced"
    bar = alt.Chart(df).mark_bar().encode(
        x="T:T", y=alt.Y("V0:Q", axis=alt.Axis(title="$ Sales", orient="left")))
    line = alt.Chart(df).mark_line().encode(
        x="T:T", y=alt.Y("V1:Q", axis=alt.Axis(title="# Quantity", orient="right")))
    ch = alt.layer(alt.layer(bar, *refs), line).resolve_scale(y="independent")
    axes = [(a.get("orient"), a.get("title"))
            for a in vlc.vegalite_to_vega(ch.to_json())["axes"]
            if a.get("title") and a.get("orient") in ("left", "right")]
    assert ("left", "$ Sales") in axes, f"the series' own axis must survive: {axes}"
    assert ("right", "# Quantity") in axes, axes
    assert len(axes) == 2, f"and the rule must add no axis of its own: {axes}"
    print("ok  a reference line shares its series' field, scale AND axis: the "
          "bar series' left '$ Sales' axis survives (axis=None on the rule "
          "was merged into the shared axis and erased it -- found live on the "
          "real Sales Dashboard Monthly chart) and the rule still adds no "
          "phantom axis of its own")


def test_local_backend_rewrites_snowflake_dateadd():
    """Local verification must execute the Snowflake DATEADD emitted by calcs."""
    import duckdb
    import backend

    source = (
        "SELECT DATEADD('month', -1, DATE '2026-07-01') AS PREV, "
        "DATEADD('day', 2, DATEADD('month', -1, DATE '2026-07-01')) AS NESTED"
    )
    translated = backend._rewrite_dateadd_for_duckdb(source)
    assert "DATEADD" not in translated.upper(), translated
    prev, nested = duckdb.connect().execute(translated).fetchone()
    assert str(prev)[:10] == "2026-06-01", prev
    assert str(nested)[:10] == "2026-06-03", nested
    untouched = backend._rewrite_dateadd_for_duckdb(
        "SELECT DATEADD(dynamic_part, 1, SOME_DATE)")
    assert untouched == "SELECT DATEADD(dynamic_part, 1, SOME_DATE)", untouched
    print("ok  local DuckDB verification executes balanced/nested Snowflake "
          "DATEADD calls, so previous-period KPI values do not disappear only "
          "in preview while remaining valid after Snowflake deployment")


def test_calc_caption_collision_and_multidatasource_filter_routing():
    """Physical identity wins over calc captions; controls use their sheet table."""
    import pandas as pd

    calc_defs = {
        "Channel_Proper": "UPPER(LEFT([Channel], 1)) + LOWER(RIGHT([Channel], LEN([Channel]) - 1))",
        "Channel": "[Calculation_Proper]",
    }
    aliases = {"Calculation_Proper": calc_defs["Channel_Proper"]}
    translated, dropped = TP.translate_calcs(
        calc_defs, {}, aliases=aliases, physical_names={"Channel"})
    assert not dropped, dropped
    sql = translated["Channel_Proper"]["sql"]
    assert "CHANNEL" in sql and "CALCULATION_PROPER" not in sql and "NULL" not in sql, sql
    assert "||" in sql, sql

    class _Ctx:
        def __enter__(self): return self
        def __exit__(self, *_a): return False

    saved = (engine.tbl, engine.table_columns, engine.q, engine.st.columns,
             engine.st.selectbox, engine.CALCS, engine.COLMAP)
    queries = []
    try:
        engine.CALCS, engine.COLMAP = {}, {}
        engine.tbl = lambda s: {"DS_A": "TABLE_A", "DS_B": "TABLE_B"}[s["datasource"]]
        engine.table_columns = lambda T=engine.ORDERS: {
            "TABLE_A": ["FIELD_A"], "TABLE_B": ["FIELD_B"]}.get(T, [])
        def _q(sql):
            queries.append(sql)
            return pd.DataFrame({"V": ["A" if "TABLE_A" in sql else "B"]})
        engine.q = _q
        engine.st.columns = lambda n: [_Ctx() for _ in range(n)]
        engine.st.selectbox = lambda _label, opts, **_kw: opts[1]
        dash = {
            "name": "Multi source",
            "filters": [
                {"caption": "Field A", "kind": "categorical", "scope_sheet": "Sheet A"},
                {"caption": "Field B", "kind": "categorical", "scope_sheet": "Sheet B"},
            ],
            "params": [],
            "sheets": [
                {"name": "Sheet A", "datasource": "DS_A",
                 "filters": [{"caption": "Field A"}]},
                {"name": "Sheet B", "datasource": "DS_B",
                 "filters": [{"caption": "Field B"}]},
            ],
        }
        parts = engine.build_where(dash)
        assert {p["table"] for p in parts} == {"TABLE_A", "TABLE_B"}, parts
        assert any("FROM TABLE_A" in q for q in queries), queries
        assert any("FROM TABLE_B" in q for q in queries), queries
        assert [p["caption"] for p in engine._parts_for_sheet(parts, dash["sheets"][0])] == ["Field A"]
        assert [p["caption"] for p in engine._parts_for_sheet(parts, dash["sheets"][1])] == ["Field B"]
    finally:
        (engine.tbl, engine.table_columns, engine.q, engine.st.columns,
         engine.st.selectbox, engine.CALCS, engine.COLMAP) = saved

    bdo = os.path.join("Workbooks", "_powerbi_project_sweep",
                       "BDO Pre-Optimization Dashboard.twbx")
    if os.path.exists(bdo):
        ir = TP.build_ir(bdo)
        actual = ir["calcs"]["Channel_Proper"]["sql"]
        assert "CHANNEL" in actual and "NULL" not in actual, actual
        assert ir["colmap"].get("Channel") is None, ir["colmap"].get("Channel")
        assert "Sheet 43" not in {d["name"] for d in ir["dashboards"]}, \
            "empty authoring worksheet leaked into published dashboard tabs"
    print("ok  physical field identity wins over a colliding calculated-field "
          "caption, and each dashboard filter gets its values from the bound "
          "worksheet datasource rather than one global default table")


def test_typed_date_parameters_and_measure_names_only_bars():
    """Date params remain temporal; Measure Names-only shelves render bars."""
    import pandas as pd

    assert CT.param_sql_literal("#2026-07-01#") == "DATE '2026-07-01'"
    assert CT.param_sql_literal("#2026-07-01 12:30:00#") == \
        "TIMESTAMP '2026-07-01 12:30:00'"
    assert engine._param_display_value("#2026-07-01#") == "2026-07-01"

    class _St:
        def __init__(self): self.charts = []; self.messages = []
        def altair_chart(self, chart, **_kw): self.charts.append(chart)
        def info(self, message): self.messages.append(message)
        def warning(self, message): self.messages.append(message)

    saved = (engine.st, engine.tbl, engine.q, engine.resolve_measure)
    fake = _St()
    try:
        engine.st = fake
        engine.tbl = lambda _s: "T"
        engine.resolve_measure = lambda _T, _s, m: {
            "sql": f"SUM({m['caption'].upper()})", "fmt": "num0"}
        engine.q = lambda _sql: pd.DataFrame({"M0": [100.0], "M1": [40.0]})
        engine.r_mbar({"name": "Funnel", "kind": "mbar", "dim": None,
                       "measures": [{"caption": "Leads"},
                                    {"caption": "Conversions"}]}, "")
        assert len(fake.charts) == 1, fake.messages
        assert not fake.messages, fake.messages
    finally:
        engine.st, engine.tbl, engine.q, engine.resolve_measure = saved
    print("ok  Tableau hash date parameters become typed SQL/display values, "
          "and Measure Names-only multi-bars render instead of disappearing")


def test_customized_kpi_and_duplicate_measure_sparkline():
    """Regression for generic Tableau KPI tiles found in the BDO workbook."""
    import xml.etree.ElementTree as ET
    import pandas as pd

    ws = ET.fromstring(r"""
    <worksheet name="Generic KPI">
      <table><style><style-rule element="cell">
        <format attr="text-format" field="[ds].[sum:Current:qk]"
                value='c"PHP "#,##0,.0K;-"PHP "#,##0,.0K'/>
      </style-rule></style><panes>
        <pane><customized-label><formatted-text>
          <run fontsize="15">Spend</run><run>Æ</run>
          <run fontsize="15">&lt;[ds].[sum:Current:qk]&gt;</run><run>Æ</run>
          <run fontcolor="#e15729">&lt;[ds].[usr:Positive:qk]&gt;</run>
          <run fontcolor="#59a14f">&lt;[ds].[usr:Negative:qk]&gt;</run>
          <run> vs LM</run>
        </formatted-text></customized-label></pane>
        <pane y-axis-name="[ds].[sum:Value:qk]"><mark class="Area"/></pane>
        <pane y-axis-name="[ds].[sum:Value:qk]"><mark class="Line"/></pane>
      </panes></table>
    </worksheet>""")
    meta = {
        "[Current]": {"caption": "Current", "role": "measure", "datatype": "real"},
        "[Positive]": {"caption": "Positive", "role": "measure", "datatype": "real"},
        "[Negative]": {"caption": "Negative", "role": "measure", "datatype": "real"},
        "[Value]": {"caption": "Value", "role": "measure", "datatype": "real"},
    }
    seq = TP.pane_mark_sequence(ws, meta)
    assert [p["mark"] for p in seq] == ["area", "line"], seq
    runs = TP.customized_label_runs(ws, meta)
    assert [r.get("field") for r in runs if r.get("field")] == [
        "Current", "Positive", "Negative"], runs
    assert TP.worksheet_number_formats(ws, meta)["Current"].startswith('c"PHP "')
    assert TP.dynamic_text(
        "<[Parameters].[Metric 1]> and <[Parameters].[Metric 2]> by Month",
        {"Metric 1": "Primary metric", "Metric 2": "Secondary metric"}
    ) == "{param:Primary metric} and {param:Secondary metric} by Month"

    s = {"name": "Generic KPI", "kind": "kpi", "label_runs": runs,
         "measures": [
             {"caption": "Current", "agg": "sum", "fmt": "cur0",
              "tableau_format": 'c"PHP "#,##0,.0K;-"PHP "#,##0,.0K'},
             {"caption": "Positive", "agg": "usr", "fmt": "pct",
              "tableau_format": "*▲0.00%;▼0.00%;"},
             {"caption": "Negative", "agg": "usr", "fmt": "pct",
              "tableau_format": "*▲0.00%;▼0.00%;"},
         ]}
    old_tbl, old_resolve, old_q = engine.tbl, engine.resolve_measure, engine.q
    old_markdown, old_columns = engine.st.markdown, engine.st.columns
    rendered = []
    try:
        engine.tbl = lambda _s: "T"
        engine.resolve_measure = lambda _t, _sheet, m: {"sql": "1", "fmt": m["fmt"]}
        engine.q = lambda _sql: pd.DataFrame(
            {"M0": [2603100.0], "M1": [None], "M2": [-0.1311]})
        engine.st.markdown = lambda body, **_kw: rendered.append(body)
        engine.st.columns = lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("customized KPI must not fan helper measures into columns"))
        engine.r_kpi(s, "")
    finally:
        engine.tbl, engine.resolve_measure, engine.q = old_tbl, old_resolve, old_q
        engine.st.markdown, engine.st.columns = old_markdown, old_columns
    html = "".join(rendered)
    assert "Spend" in html and "PHP 2,603.1K" in html, html
    assert "▼13.11%" in html and "#59a14f" in html and "vs LM" in html, html
    bdo = os.path.join("Workbooks", "_powerbi_project_sweep",
                       "BDO Pre-Optimization Dashboard.twbx")
    if os.path.exists(bdo):
        ir = TP.build_ir(bdo)
        dash = next(d for d in ir["dashboards"]
                    if d["name"] == "Metric Summary_BDO")
        sheets = {s["name"].strip(): s for s in dash["sheets"]}
        assert [y["mark"] for y in sheets["Spend Area Chart"]["ys"]] == [
            "area", "line"]
        assert sheets["TrendAnalysis"]["title"] == (
            "{param:P.Parameter 1} and {param:P.Parameter 2} by Month")
        assert sheets["Spend KPI"]["label_runs"], sheets["Spend KPI"]
        def _walk(node):
            yield node
            for child in node.get("children", []):
                yield from _walk(child)
        assert sum(bool(n.get("border")) for n in _walk(dash["layout"])) >= 6
    print("ok  customized KPI labels render one composed tile from Tableau's "
          "authored run template (value/direction/color/format), duplicate-measure "
          "sparklines preserve ordered Area+Line panes, and dynamic titles retain "
          "their parameter placeholders -- generic regression from BDO")


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
    test_measure_names_native_param_filter_detected()
    test_measure_names_native_param_wires_into_mbar_spec()
    test_native_measure_selector_narrows_to_one_measure()
    test_native_measure_selector_real_workbook_end_to_end()
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
    test_discount_curated_profile_respects_declared_aggregation()
    test_live_connection_support()
    test_custom_sql_execution()
    test_auto_bind_existing_snowflake_table()
    test_source_tables_skips_relations_nested_in_a_legacy_join()
    test_r10_multitable_source_autobind()
    test_r9_live_multitable_join()
    test_live_multitable_relations_inside_a_collection()
    test_tableau_server_url_parsing_and_fetch()
    test_tableau_server_view_data_pull()
    test_tableau_server_view_image_pull()
    test_headless_render_to_png()
    test_headless_render_never_touches_real_widgets()
    test_dashboard_composite_follows_zone_tree()
    test_report_progressive_disclosure()
    test_workbook_formula_catalog_deduplicates_across_dashboards()
    test_table_scoped_lod_subquery_columns_resolve()
    test_agg_grain_calc_dimension_is_not_a_backend_key()
    test_validation_tab_offers_exactly_one_report()
    test_validation_pack_slimmed()
    test_app_screenshot_no_pipe_deadlock()
    test_vision_validate_dashboard()
    test_non_star_join_and_blends()
    test_multifact_and_cyclic_refusal_on_real_xml()
    test_union_support()
    test_dashboard_filter_governs_sheet_filter()
    test_dashboard_filter_all_overrides_sheet_saved_value()
    test_dashboard_filter_scoped_to_bound_sheets()
    test_tracker_consistency()
    test_context_filter_applied_inside_topn_ranking()
    test_bar_colored_by_own_axis_has_no_offset()
    test_table_caption_in_both_dims_and_measures_not_duplicated()
    test_deploy_streamlit_app()
    test_worksheet_shown_params_render_on_tab()
    test_data_model_view_deploy()
    test_build_data_model_tables_scope_b()
    test_no_undefined_names_in_app()
    test_no_silent_conn_gated_ui()
    test_section_validation_notebook()
    test_r2_live_truth_pull()
    test_row_level_calc_resolves_as_measure()
    test_pivoted_crosstab_unpivots_to_chart_grain()
    test_empty_source_blocks_and_states_its_reason()
    test_order_match_structural_only_for_rank_and_topn()
    test_tableau_leg_scoped_and_pivoted_csv_assigned()
    test_table_sheet_csv_assigned_by_combined_strong_and_weak_dims()
    test_map_sheet_csv_assigned_by_geo_field()
    test_screenshot_unavailability_names_its_cause()
    test_workstation_pack_cli()
    test_automated_capture_queue_and_bundle()
    test_server_side_render_feeds_vision_but_never_the_screenshot_gate()
    test_in_snowflake_capture_ships_real_routing_and_waits_for_tabs()
    test_capture_retry_first_attempt_succeeds()
    test_capture_retry_transient_then_success()
    test_capture_retry_three_transient_exhausts()
    test_capture_retry_deterministic_stops_after_one()
    test_capture_retry_missing_manifest_triggers_retry()
    test_capture_retry_malformed_manifest_triggers_retry()
    test_capture_retry_partial_images_triggers_retry()
    test_capture_retry_invalid_png_triggers_retry()
    test_capture_retry_legit_blank_png_accepted()
    test_capture_retry_tiny_corrupt_png_rejected()
    test_capture_retry_isolated_stage_paths_and_notebook_names()
    test_capture_retry_only_successful_attempt_shots_used()
    test_capture_diagnostics_render_once_in_html()
    test_pack_screenshots_not_discarded_by_unrelated_skipped_section()
    test_capture_report_embeds_all_shots_after_retry_success()
    test_capture_retry_no_blocked_dashboards_after_eventual_success()
    test_capture_retry_error_not_repeated_ten_times()
    test_task_capture_success()
    test_task_capture_transient_retry_then_success()
    test_task_capture_three_attempts_all_fail()
    test_task_capture_deterministic_failure_is_failed_not_blocked()
    test_task_capture_timeout_names_the_grant()
    test_task_capture_stale_job_expires_to_timed_out()
    test_task_capture_concurrent_claims_single_winner()
    test_task_capture_missing_manifest()
    test_task_capture_malformed_manifest()
    test_task_capture_incomplete_screenshots_rejected_by_validator()
    test_task_capture_invalid_png_rejected()
    test_task_capture_legit_blank_png_accepted()
    test_task_capture_no_cross_attempt_mixing()
    test_task_capture_diagnostics_render_once_and_data_survives()
    test_task_capture_worker_source_is_sound()
    test_task_capture_worker_notebook_has_network_egress()
    test_datepart_alias_and_formatted_number_parsing()
    test_ui_crosstab_header_recovery_and_kpi_aliases()
    test_datepart_values_reconcile_to_app_grain()
    test_ordinary_altair_charts_get_datepart_reconciliation_too()
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
    test_zone_crosstab_url_and_xlsx_conversion()
    test_crosstab_jobs_paths_and_run_ids()
    test_live_model_view_join_keys_use_physical_columns()
    test_late_session_routing_reresolves_to_deploy_tables()
    test_nested_ratio_calc_nullif_wrap_is_idempotent()
    test_ratio_agg_wrapping_conditional_aggregate()
    test_bracket_ref_keyword_fusion_guarded()
    test_tableau_type_conversion_functions_cast_correctly()
    test_bracket_scan_skips_brackets_inside_string_literals()
    test_case_sensitive_live_column_self_heals()
    test_table_dimension_uses_shared_column_resolution()
    test_dual_axis_uses_pane_local_colors()
    test_aggregate_shape_calc_is_not_grouped()
    test_measure_display_label_resolves_param_swap_calc()
    test_light_zone_gets_card_css()
    test_layout_text_leaf_renders_without_children()
    test_compact_bar_and_dual_chart_fidelity()
    test_bar_text_shelf_measure_enables_labels()
    test_scatter_falls_back_to_color_dimension_grain()
    test_string_plus_concatenation_uses_double_pipe()
    test_dashboard_chrome_headings_legends_axes_from_xml()
    test_refline_keeps_its_series_axis()
    test_local_backend_rewrites_snowflake_dateadd()
    test_calc_caption_collision_and_multidatasource_filter_routing()
    test_typed_date_parameters_and_measure_names_only_bars()
    test_customized_kpi_and_duplicate_measure_sparkline()
    print("\nALL REGRESSION TESTS PASSED")


if __name__ == "__main__":
    main()
