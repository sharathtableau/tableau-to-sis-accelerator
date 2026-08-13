"""
make_datamodel_workbooks.py -- author the two data-model test workbooks the
corpus does not cover (see DATA_MODEL_STATUS.md recommended-workbooks list):

  1. R3_Extract_Over_Existing_Table.twbx -- a single-table EXTRACT whose
     declared source is WBR_DB.PUBLIC.SUPERSTORE_ORDERS, a table that already
     exists on the target account. Built by taking the REAL, working
     Superstore_KPI_Parameter_Dashboard_Live.twbx (5 worksheets, 1 dashboard,
     3 params -- all proven live already) and adding a real bundled <extract>
     to its one datasource, so it becomes extract-based while still declaring
     the same live source. Reusing a real, already-verified workbook wholesale
     means every worksheet/dashboard/param is guaranteed valid Tableau XML --
     only the data-model question (extract vs live) changes.

  2. R7_Chain_Orders_Product_Category.twbx -- a genuine depth-2 SNOWFLAKE
     SCHEMA (Orders -> Product -> Category; Category hangs off Product, not
     the fact) built from Superstore's own rows, so SUM(sales) per category
     is a KNOWN NUMBER to verify the join against.

  3. R10_Chain_Over_Existing_Tables.twbx -- THE ACTUAL R10 TEST. Same
     Orders->Product->Category chain, but this time each object's relation
     declares WBR_DB.PIPELINE_DEMO.R10_ORDERS/R10_PRODUCT/R10_CATEGORY --
     tables that must ALREADY exist SEPARATELY in the account (load them
     first with `python tests/make_datamodel_workbooks.py --load-r10-tables`,
     which write_pandas's them directly, matching this generator's own data).
     Unlike #2 (whose 3 tables are brand-new and were never pre-loaded, so it
     exercises R7's join planner via the ordinary decode+flatten path), this
     workbook's tables are pre-existing, separate, real Snowflake objects --
     the ONLY thing that proves R10's auto-bind (skip decode+copy, deploy the
     view straight at the originals), as opposed to R7's join-planning alone.

All three bundle REAL .hyper extracts (via tableauhyperapi, confirmed
installed) so they are genuinely uploadable, not just structurally plausible
XML -- #3 still carries a real extract too, so if auto-bind somehow didn't
fire it would fall back to the ordinary decode+flatten path instead of
failing outright.

Usage:  python tests/make_datamodel_workbooks.py
"""

import os
import sys
import tempfile
import uuid
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

OUT_DIR = os.path.join(ROOT, "Workbooks")


def _guid():
    return uuid.uuid4().hex.upper()[:32]


def write_hyper(tables, path):
    """{hyper table name: DataFrame} -> a real .hyper extract, schema 'Extract'
    (matching every real corpus extract's convention)."""
    from tableauhyperapi import (Connection, CreateMode, HyperProcess, Inserter,
                                 SqlType, TableDefinition, TableName, Telemetry,
                                 Nullability)
    import pandas as pd

    def _sqltype(s):
        if pd.api.types.is_integer_dtype(s):
            return SqlType.big_int()
        if pd.api.types.is_float_dtype(s):
            return SqlType.double()
        if pd.api.types.is_datetime64_any_dtype(s):
            return SqlType.date()
        return SqlType.text()

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hp:
        with Connection(hp.endpoint, path, CreateMode.CREATE_AND_REPLACE) as con:
            con.catalog.create_schema("Extract")
            for name, df in tables.items():
                tdef = TableDefinition(
                    TableName("Extract", name),
                    [TableDefinition.Column(c, _sqltype(df[c]), Nullability.NULLABLE)
                     for c in df.columns])
                con.catalog.create_table(tdef)
                rows = []
                for rec in df.itertuples(index=False, name=None):
                    out = []
                    for v, c in zip(rec, df.columns):
                        if pd.isna(v):
                            out.append(None)
                        elif pd.api.types.is_datetime64_any_dtype(df[c]):
                            out.append(v.date() if hasattr(v, "date") else v)
                        elif pd.api.types.is_integer_dtype(df[c]):
                            out.append(int(v))
                        elif pd.api.types.is_float_dtype(df[c]):
                            out.append(float(v))
                        else:
                            out.append(str(v))
                    rows.append(out)
                with Inserter(con, tdef) as ins:
                    ins.add_rows(rows)
                    ins.execute()
    return path


def _zip_twbx(out_path, twb_name, twb_text, data_files):
    if os.path.exists(out_path):
        os.remove(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(twb_name, twb_text)
        for arc, real in data_files.items():
            z.write(real, arc)
    return out_path


# --------------------------------------------------------------------------- #
# 1. R3 -- single-table EXTRACT declaring an EXISTING Snowflake table
# --------------------------------------------------------------------------- #
R3_SRC = os.path.join(ROOT, "Workbooks", "Superstore_KPI_Parameter_Dashboard_Live.twbx")
R3_OUT = os.path.join(OUT_DIR, "R3_Extract_Over_Existing_Table.twbx")
R3_HYPER_ARC = "Data/Extracts/federated_r3test.hyper"

# Real columns of WBR_DB.PUBLIC.SUPERSTORE_ORDERS (confirmed via the live
# workbook's own metadata-records, same session) -- the extract must carry
# EXACTLY these so R3's column-verification guard passes.
LIVE_TABLE_COLUMNS = [
    "ROW_ID", "ORDER_ID", "ORDER_DATE", "SHIP_DATE", "SHIP_MODE", "CUSTOMER_ID",
    "CUSTOMER_NAME", "SEGMENT", "COUNTRY_REGION", "CITY", "STATE_PROVINCE",
    "POSTAL_CODE", "REGION", "PRODUCT_ID", "CATEGORY", "SUB_CATEGORY",
    "PRODUCT_NAME", "SALES", "QUANTITY", "DISCOUNT", "PROFIT"]

_EXTRACT_XML = ("      <extract count='-1' enabled='true' object-id='' units='records' "
               "user-specific='false'>\n"
               "        <connection authentication='auth-none' author-locale='en_US' "
               "class='hyper' dbname='{hyper}' default-settings='yes' schema='Extract' "
               "tablename='Extract' update-time='07/26/2026 12:00:00 PM'>\n"
               "          <relation name='Extract' table='[Extract].[Extract]' "
               "type='table' />\n"
               "        </connection>\n"
               "      </extract>\n")


def build_r3_workbook():
    import pandas as pd

    if not os.path.exists(R3_SRC):
        return None, f"source workbook missing: {R3_SRC}"
    with zipfile.ZipFile(R3_SRC) as z:
        twb_name = [n for n in z.namelist() if n.endswith(".twb")][0]
        twb = z.read(twb_name).decode("utf-8")

    marker = "<datasource caption='SUPERSTORE_ORDERS"
    i = twb.find(marker)
    if i < 0:
        return None, "could not locate the Snowflake datasource in the source .twb"
    conn_close = twb.find("</connection>", i)
    if conn_close < 0:
        return None, "malformed source .twb (no </connection>)"
    insert_at = conn_close + len("</connection>\n")
    twb = twb[:insert_at] + _EXTRACT_XML.format(hyper=R3_HYPER_ARC) + twb[insert_at:]

    csv = os.path.join(ROOT, "data", "Sample - Superstore.csv")
    if not os.path.exists(csv):
        return None, f"missing {csv}"
    df = pd.read_csv(csv)
    df.columns = LIVE_TABLE_COLUMNS  # positional -- same 21 columns, same order
    df["ORDER_DATE"] = pd.to_datetime(df["ORDER_DATE"], errors="coerce")
    df["SHIP_DATE"] = pd.to_datetime(df["SHIP_DATE"], errors="coerce")

    tmp = os.path.join(tempfile.mkdtemp(prefix="r3hyper_"), "federated_r3test.hyper")
    write_hyper({"Extract": df}, tmp)

    _zip_twbx(R3_OUT, "R3_Extract_Over_Existing_Table.twb", twb, {R3_HYPER_ARC: tmp})
    return R3_OUT, f"{len(df)} rows bundled; declares WBR_DB.PUBLIC.SUPERSTORE_ORDERS"


# --------------------------------------------------------------------------- #
# 2. R7 -- a genuine SNOWFLAKE SCHEMA (depth-2 chain)
# --------------------------------------------------------------------------- #
CHAIN_OUT = os.path.join(OUT_DIR, "R7_Chain_Orders_Product_Category.twbx")
CHAIN_HYPER_ARC = "Data/Extracts/federated_chain.hyper"

_CHAIN_TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2024.3' source-platform='win' version='18.1'>
  <datasources>
    <datasource caption='Sales Chain Model' inline='true' name='federated.chain01' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='textscan.chain01' caption='chain'>
            <connection class='hyper' dbname='{hyper}' schema='Extract' />
          </named-connection>
        </named-connections>
        <relation name='Extract' table='[Extract].[Extract]' type='table' connection='textscan.chain01' />
        <metadata-records>
{meta}        </metadata-records>
      </connection>
      <aliases enabled='yes' />
{columns}      <extract count='-1' enabled='true' object-id='' units='records' user-specific='false'>
        <connection authentication='auth-none' author-locale='en_US' class='hyper' dbname='{hyper}' default-settings='yes' schema='Extract' tablename='Extract' update-time='07/26/2026 12:00:00 PM'>
          <relation type='collection'>
{extract_rels}          </relation>
        </connection>
      </extract>
      <_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
        <objects>
{objects}        </objects>
        <relationships>
{relationships}        </relationships>
      </_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
    </datasource>
  </datasources>
  <worksheets>
{worksheets}  </worksheets>
{dashboard}</workbook>
"""


_CHAIN_WORKSHEETS_XML = """    <worksheet name='Sales by Category'>
      <table>
        <view>
          <datasources>
            <datasource caption='{cap}' name='{name}' />
          </datasources>
          <datasource-dependencies datasource='{name}'>
            <column datatype='string' name='[CATEGORY_NAME]' role='dimension' type='nominal' />
            <column datatype='real' name='[SALES]' role='measure' type='quantitative' />
            <column-instance column='[CATEGORY_NAME]' derivation='None' name='[none:CATEGORY_NAME:nk]' pivot='key' type='nominal' />
            <column-instance column='[SALES]' derivation='Sum' name='[sum:SALES:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
        </view>
        <style />
        <panes>
          <pane>
            <view><breakdown value='auto' /></view>
            <mark class='Bar' />
          </pane>
        </panes>
        <rows>[{name}].[none:CATEGORY_NAME:nk]</rows>
        <cols>[{name}].[sum:SALES:qk]</cols>
      </table>
    </worksheet>
    <worksheet name='Sales by Sub-Category'>
      <table>
        <view>
          <datasources>
            <datasource caption='{cap}' name='{name}' />
          </datasources>
          <datasource-dependencies datasource='{name}'>
            <column datatype='string' name='[SUB_CATEGORY]' role='dimension' type='nominal' />
            <column datatype='real' name='[SALES]' role='measure' type='quantitative' />
            <column-instance column='[SUB_CATEGORY]' derivation='None' name='[none:SUB_CATEGORY:nk]' pivot='key' type='nominal' />
            <column-instance column='[SALES]' derivation='Sum' name='[sum:SALES:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
        </view>
        <style />
        <panes>
          <pane>
            <view><breakdown value='auto' /></view>
            <mark class='Bar' />
          </pane>
        </panes>
        <rows>[{name}].[none:SUB_CATEGORY:nk]</rows>
        <cols>[{name}].[sum:SALES:qk]</cols>
      </table>
    </worksheet>
    <worksheet name='Product Detail'>
      <table>
        <view>
          <datasources>
            <datasource caption='{cap}' name='{name}' />
          </datasources>
          <datasource-dependencies datasource='{name}'>
            <column datatype='string' name='[PRODUCT_NAME]' role='dimension' type='nominal' />
            <column datatype='string' name='[SUB_CATEGORY]' role='dimension' type='nominal' />
            <column datatype='string' name='[CATEGORY_NAME]' role='dimension' type='nominal' />
            <column datatype='real' name='[SALES]' role='measure' type='quantitative' />
            <column-instance column='[PRODUCT_NAME]' derivation='None' name='[none:PRODUCT_NAME:nk]' pivot='key' type='nominal' />
            <column-instance column='[SUB_CATEGORY]' derivation='None' name='[none:SUB_CATEGORY:nk]' pivot='key' type='nominal' />
            <column-instance column='[CATEGORY_NAME]' derivation='None' name='[none:CATEGORY_NAME:nk]' pivot='key' type='nominal' />
            <column-instance column='[SALES]' derivation='Sum' name='[sum:SALES:qk]' pivot='key' type='quantitative' />
          </datasource-dependencies>
        </view>
        <style />
        <panes>
          <pane>
            <view><breakdown value='auto' /></view>
            <mark class='Automatic' />
          </pane>
        </panes>
        <rows>[{name}].[none:CATEGORY_NAME:nk]+[{name}].[none:SUB_CATEGORY:nk]+[{name}].[none:PRODUCT_NAME:nk]+[{name}].[sum:SALES:qk]</rows>
        <cols></cols>
      </table>
    </worksheet>
"""

_CHAIN_DASHBOARD_XML = """  <dashboards>
    <dashboard name='{dash}'>
      <style />
      <size maxheight='1400' maxwidth='1000' minheight='1400' minwidth='1000' />
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          <zone h='25000' id='2' name='Sales by Category' w='100000' x='0' y='0' />
          <zone h='25000' id='3' name='Sales by Sub-Category' w='100000' x='0' y='25000' />
          <zone h='50000' id='4' name='Product Detail' w='100000' x='0' y='50000' />
        </zone>
      </zones>
    </dashboard>
  </dashboards>
"""


def _chain_worksheets_and_dashboard(cap, name, dash_name):
    """Three worksheets pulling a field from EACH of the 3 chained tables
    (Category, Product/Sub-Category, and a combined Product Detail view mixing
    all three) so a real upload gives visual proof the depth-2 join reaches
    every table, not just Category+Orders -- the gap a bar-chart-only sheet
    left open (Product's fields were never actually rendered anywhere)."""
    return (_CHAIN_WORKSHEETS_XML.format(cap=cap, name=name),
           _CHAIN_DASHBOARD_XML.format(dash=dash_name))


_CHAIN_DTYPES = {"ORDER_DATE": "date", "SALES": "real", "PROFIT": "real",
                 "QUANTITY": "integer", "CATEGORY_ID": "integer"}


def _chain_frames():
    """Superstore's own rows, re-modelled as Orders -> Product -> Category
    (depth-2). Shared by the chain workbook, the R10 workbook, and the R10
    table pre-loader -- ONE data definition, so all three agree exactly on
    columns/values (the pre-loaded tables and the workbook's own metadata-
    records must describe the SAME thing for R10's column guard to pass)."""
    import pandas as pd
    csv = os.path.join(ROOT, "data", "Sample - Superstore.csv")
    src = pd.read_csv(csv)

    cats = sorted(src["Category"].dropna().unique())
    cat_id = {c: i + 1 for i, c in enumerate(cats)}
    category = pd.DataFrame({"CATEGORY_ID": [cat_id[c] for c in cats],
                             "CATEGORY_NAME": cats})

    prod = (src[["Product ID", "Product Name", "Sub-Category", "Category"]]
            .drop_duplicates(subset=["Product ID"]).reset_index(drop=True))
    product = pd.DataFrame({
        "PRODUCT_ID": prod["Product ID"].astype(str),
        "CATEGORY_ID": [cat_id[c] for c in prod["Category"]],
        "PRODUCT_NAME": prod["Product Name"].astype(str),
        "SUB_CATEGORY": prod["Sub-Category"].astype(str)})

    orders = pd.DataFrame({
        "ORDER_ID": src["Order ID"].astype(str),
        "ORDER_DATE": pd.to_datetime(src["Order Date"], errors="coerce"),
        "PRODUCT_ID": src["Product ID"].astype(str),
        "REGION": src["Region"].astype(str),
        "SALES": src["Sales"].astype(float),
        "QUANTITY": src["Quantity"].astype(int),
        "PROFIT": src["Profit"].astype(float)})
    truth = src.groupby("Category")["Sales"].sum().round(2).to_dict()
    return {"Orders": orders, "Product": product, "Category": category}, truth


def _chain_meta_and_columns(frames):
    """metadata-record XML + top-level <column> XML shared by both chain-shaped
    workbooks -- the workbook's own description of its columns, which R10's
    column-verification guard checks a candidate table against."""
    meta, columns, seen_col = [], [], set()
    for cap, df in frames.items():
        for c in df.columns:
            dt = _CHAIN_DTYPES.get(c, "string")
            meta.append(
                "          <metadata-record class='column'>\n"
                f"            <remote-name>{c}</remote-name>\n"
                f"            <parent-name>[{cap}]</parent-name>\n"
                f"            <local-name>[{c}]</local-name>\n"
                "          </metadata-record>\n")
            if c not in seen_col:
                seen_col.add(c)
                role = "measure" if dt in ("real", "integer") and c != "CATEGORY_ID" else "dimension"
                typ = "quantitative" if role == "measure" else "nominal"
                columns.append(
                    f"      <column datatype='{dt}' name='[{c}]' role='{role}' type='{typ}' />\n")
    return "".join(meta), "".join(columns)


def _chain_relationships(ids):
    """Orders -> Product, then Product -> Category (NOT Orders -> Category
    directly) -- the second edge is what makes this depth-2, not a star."""
    def _rel(first, second, lkey, rkey, rcap):
        return ("          <relationship>\n"
                "            <expression op='='>\n"
                f"              <expression op='[{lkey}]' />\n"
                f"              <expression op='[{rkey} ({rcap})]' />\n"
                "            </expression>\n"
                f"            <first-end-point object-id='{first}' />\n"
                f"            <second-end-point object-id='{second}' />\n"
                "          </relationship>\n")
    return "".join([
        _rel(ids["Orders"], ids["Product"], "PRODUCT_ID", "PRODUCT_ID", "Product"),
        _rel(ids["Product"], ids["Category"], "CATEGORY_ID", "CATEGORY_ID", "Category")])


def build_chain_workbook():
    frames, truth = _chain_frames()
    ids = {"Orders": "Orders_" + _guid(), "Product": "Product_" + _guid(),
           "Category": "Category_" + _guid()}
    meta, columns = _chain_meta_and_columns(frames)

    extract_rels, objects = [], []
    for cap, df in frames.items():
        oid = ids[cap]
        extract_rels.append(
            f"            <relation name='{oid}' table='[Extract].[{oid}]' type='table' />\n")
        colxml = "".join(
            f"              <column datatype='{_CHAIN_DTYPES.get(c, 'string')}' "
            f"name='{c}' ordinal='0' />\n" for c in df.columns)
        objects.append(
            f"          <object caption='{cap}' id='{oid}'>\n"
            "            <properties context=''>\n"
            f"              <relation connection='textscan.chain01' name='{cap}' table='[{cap}]' type='table'>\n"
            "                <columns header='yes'>\n" + colxml +
            "                </columns>\n              </relation>\n"
            "            </properties>\n          </object>\n")

    worksheets, dashboard = _chain_worksheets_and_dashboard(
        "Sales Chain Model", "federated.chain01", "Chain Dashboard")
    twb = _CHAIN_TWB.format(hyper=CHAIN_HYPER_ARC, meta=meta, columns=columns,
                            objects="".join(objects),
                            relationships=_chain_relationships(ids),
                            extract_rels="".join(extract_rels),
                            worksheets=worksheets, dashboard=dashboard)

    tmp = os.path.join(tempfile.mkdtemp(prefix="chainhyper_"), "federated_chain.hyper")
    write_hyper({ids[c]: frames[c] for c in frames}, tmp)
    _zip_twbx(CHAIN_OUT, "R7_Chain_Orders_Product_Category.twb", twb, {CHAIN_HYPER_ARC: tmp})
    return CHAIN_OUT, (f"Orders({len(frames['Orders'])}) -> "
                       f"Product({len(frames['Product'])}) -> "
                       f"Category({len(frames['Category'])}); "
                       f"known SUM(sales)/category: {truth}")


# --------------------------------------------------------------------------- #
# 3. R10 -- same chain, but each object declares an ALREADY-SEPARATELY-
# EXISTING Snowflake table (WBR_DB.PIPELINE_DEMO.R10_*). THE ACTUAL R10 TEST:
# #2 above proves R7's join planner via decode+flatten; this proves R10's
# auto-bind (skip decode+copy, deploy the view straight at real originals).
# --------------------------------------------------------------------------- #
R10_OUT = os.path.join(OUT_DIR, "R10_Chain_Over_Existing_Tables.twbx")
R10_HYPER_ARC = "Data/Extracts/federated_r10chain.hyper"
R10_TABLE_NAME = {"Orders": "R10_ORDERS", "Product": "R10_PRODUCT", "Category": "R10_CATEGORY"}

_R10_TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2024.3' source-platform='win' version='18.1'>
  <datasources>
    <datasource caption='R10 Chain Model' inline='true' name='federated.r10chain' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='snowflake.r10chain' caption='snowflake'>
            <connection class='snowflake' dbname='WBR_DB' schema='PIPELINE_DEMO' server='wb19670-c2gpartners.snowflakecomputing.com' warehouse='POWERHOUSE' />
          </named-connection>
        </named-connections>
        <relation name='R10_ORDERS' table='[WBR_DB].[PIPELINE_DEMO].[R10_ORDERS]' type='table' connection='snowflake.r10chain' />
        <metadata-records>
{meta}        </metadata-records>
      </connection>
      <aliases enabled='yes' />
{columns}      <extract count='-1' enabled='true' object-id='' units='records' user-specific='false'>
        <connection authentication='auth-none' author-locale='en_US' class='hyper' dbname='{hyper}' default-settings='yes' schema='Extract' tablename='Extract' update-time='07/26/2026 12:00:00 PM'>
          <relation type='collection'>
{extract_rels}          </relation>
        </connection>
      </extract>
      <_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
        <objects>
{objects}        </objects>
        <relationships>
{relationships}        </relationships>
      </_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
    </datasource>
  </datasources>
  <worksheets>
{worksheets}  </worksheets>
{dashboard}</workbook>
"""


def load_r10_support_tables(connection_name="wbr"):
    """Pre-load the 3 chain tables SEPARATELY into WBR_DB.PIPELINE_DEMO under
    R10_* names -- THE PRECONDITION R10 needs (tables that already exist
    separately in the account, not just a workbook claiming they do). Uses the
    SAME frames the workbook's own metadata-records describe, so the columns
    can never drift between "what's loaded" and "what the workbook says"."""
    import pipeline
    frames, _truth = _chain_frames()
    session = pipeline.snow_session(connection_name)
    try:
        for cap, table in R10_TABLE_NAME.items():
            df = frames[cap]
            session.write_pandas(df, table, database=pipeline.LOAD_DB,
                                 schema=pipeline.LOAD_SCHEMA,
                                 auto_create_table=True, overwrite=True,
                                 quote_identifiers=False)
            date_cols = [c for c in df.columns if str(df[c].dtype).startswith("datetime")]
            if date_cols:
                pipeline._fix_date_columns_session(
                    session, pipeline.LOAD_DB, pipeline.LOAD_SCHEMA, table, date_cols)
            n = session.sql(f"SELECT COUNT(*) FROM {pipeline.LOAD_DB}."
                            f"{pipeline.LOAD_SCHEMA}.{table}").collect()[0][0]
            print(f"  loaded {table}: {n} rows")
    finally:
        session.close()


def build_r10_workbook():
    frames, truth = _chain_frames()
    ids = {"Orders": "Orders_" + _guid(), "Product": "Product_" + _guid(),
           "Category": "Category_" + _guid()}
    meta, columns = _chain_meta_and_columns(frames)

    extract_rels, objects = [], []
    for cap, df in frames.items():
        oid = ids[cap]
        table = R10_TABLE_NAME[cap]
        extract_rels.append(
            f"            <relation name='{oid}' table='[Extract].[{oid}]' type='table' />\n")
        colxml = "".join(
            f"              <column datatype='{_CHAIN_DTYPES.get(c, 'string')}' "
            f"name='{c}' ordinal='0' />\n" for c in df.columns)
        # THE POINT: this relation declares the REAL, already-existing
        # location (WBR_DB.PIPELINE_DEMO.R10_*), not an [Extract].[...] one --
        # exactly what pipeline.resolve_source_binding must find and verify.
        objects.append(
            f"          <object caption='{cap}' id='{oid}'>\n"
            "            <properties context=''>\n"
            f"              <relation connection='snowflake.r10chain' name='{table}' "
            f"table='[WBR_DB].[PIPELINE_DEMO].[{table}]' type='table'>\n"
            "                <columns header='yes'>\n" + colxml +
            "                </columns>\n              </relation>\n"
            "            </properties>\n          </object>\n")

    worksheets, dashboard = _chain_worksheets_and_dashboard(
        "R10 Chain Model", "federated.r10chain", "R10 Dashboard")
    twb = _R10_TWB.format(hyper=R10_HYPER_ARC, meta=meta, columns=columns,
                          objects="".join(objects),
                          relationships=_chain_relationships(ids),
                          extract_rels="".join(extract_rels),
                          worksheets=worksheets, dashboard=dashboard)

    tmp = os.path.join(tempfile.mkdtemp(prefix="r10hyper_"), "federated_r10chain.hyper")
    write_hyper({ids[c]: frames[c] for c in frames}, tmp)
    _zip_twbx(R10_OUT, "R10_Chain_Over_Existing_Tables.twb", twb, {R10_HYPER_ARC: tmp})
    return R10_OUT, (f"declares WBR_DB.PIPELINE_DEMO.{{R10_ORDERS,R10_PRODUCT,"
                     f"R10_CATEGORY}}; known SUM(sales)/category: {truth}")


# --------------------------------------------------------------------------- #
# 4. R9 -- GENUINELY LIVE (no <extract> at all), joining MULTIPLE tables.
# Reuses R10's already-loaded R10_ORDERS/R10_PRODUCT/R10_CATEGORY tables --
# no new pre-load needed. THE DIFFERENCE FROM R10: R10's workbook still
# carries a real bundled .hyper extract (it's an extract-based workbook whose
# declared source happens to already exist separately); R9's workbook has NO
# extract whatsoever -- Tableau queries these 3 tables live, every time, with
# no data ever travelling in the file at all. This is what proves the fix to
# tableau_parser.live_connections() (which used to silently mis-detect a live
# multi-table model as single-table) rather than R10's onboard()-level fix.
# --------------------------------------------------------------------------- #
R9_OUT = os.path.join(OUT_DIR, "R9_Live_Join_Orders_Product_Category.twbx")

_R9_TWB = """<?xml version='1.0' encoding='utf-8' ?>
<workbook source-build='2024.3' source-platform='win' version='18.1'>
  <datasources>
    <datasource caption='R9 Live Join Model' inline='true' name='federated.r9live' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection name='snowflake.r9live' caption='snowflake'>
            <connection class='snowflake' dbname='WBR_DB' schema='PIPELINE_DEMO' server='wb19670-c2gpartners.snowflakecomputing.com' warehouse='POWERHOUSE' />
          </named-connection>
        </named-connections>
{top_relations}        <metadata-records>
{meta}        </metadata-records>
      </connection>
      <aliases enabled='yes' />
{columns}      <_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
        <objects>
{objects}        </objects>
        <relationships>
{relationships}        </relationships>
      </_.fcp.ObjectModelEncapsulateLegacy.true...object-graph>
    </datasource>
  </datasources>
  <worksheets>
{worksheets}  </worksheets>
{dashboard}</workbook>
"""


def build_r9_workbook():
    """NO write_hyper call anywhere here -- deliberately. This workbook has no
    <extract> at all, so it must be saved as a plain .twb (Tableau's own
    convention: a workbook bundles nothing when every datasource is genuinely
    live), not a .twbx. Reuses R10_TABLE_NAME's tables (already loaded by
    load_r10_support_tables) -- same data, zero new writes to the account."""
    frames, truth = _chain_frames()
    ids = {"Orders": "Orders_" + _guid(), "Product": "Product_" + _guid(),
           "Category": "Category_" + _guid()}
    meta, columns = _chain_meta_and_columns(frames)

    # Direct children of <connection> -- what live_connections() scans (the
    # R9 fix). ALL 3 tables listed here, not just one, is what makes this a
    # genuine multi-table live model instead of a single-table live one.
    top_relations = "".join(
        f"        <relation name='{table}' table='[WBR_DB].[PIPELINE_DEMO].[{table}]' "
        f"type='table' connection='snowflake.r9live' />\n"
        for table in R10_TABLE_NAME.values())

    objects = []
    for cap, df in frames.items():
        oid = ids[cap]
        table = R10_TABLE_NAME[cap]
        colxml = "".join(
            f"              <column datatype='{_CHAIN_DTYPES.get(c, 'string')}' "
            f"name='{c}' ordinal='0' />\n" for c in df.columns)
        objects.append(
            f"          <object caption='{cap}' id='{oid}'>\n"
            "            <properties context=''>\n"
            f"              <relation connection='snowflake.r9live' name='{table}' "
            f"table='[WBR_DB].[PIPELINE_DEMO].[{table}]' type='table'>\n"
            "                <columns header='yes'>\n" + colxml +
            "                </columns>\n              </relation>\n"
            "            </properties>\n          </object>\n")

    worksheets, dashboard = _chain_worksheets_and_dashboard(
        "R9 Live Join Model", "federated.r9live", "R9 Dashboard")
    twb = _R9_TWB.format(top_relations=top_relations, meta=meta, columns=columns,
                        objects="".join(objects),
                        relationships=_chain_relationships(ids),
                        worksheets=worksheets, dashboard=dashboard)

    if os.path.exists(R9_OUT):
        os.remove(R9_OUT)
    # a genuinely live workbook (no bundled data at all) is a bare .twb, not a
    # zipped .twbx -- Tableau itself only produces a .twbx when there is
    # something to bundle.
    r9_twb_path = R9_OUT.replace(".twbx", ".twb")
    with open(r9_twb_path, "w", encoding="utf-8") as f:
        f.write(twb)
    return r9_twb_path, (f"NO extract -- genuinely live, joins WBR_DB.PIPELINE_"
                        f"DEMO.{{R10_ORDERS,R10_PRODUCT,R10_CATEGORY}} "
                        f"(reused from R10); known SUM(sales)/category: {truth}")


def main():
    if "--load-r10-tables" in sys.argv:
        print("Loading R10 support tables (WBR_DB.PIPELINE_DEMO.R10_*) ...")
        load_r10_support_tables()
        return
    for fn in (build_r3_workbook, build_chain_workbook, build_r10_workbook,
              build_r9_workbook):
        path, note = fn()
        if path:
            print(f"wrote {path}\n  {note}")
        else:
            print(f"!! {fn.__name__} skipped: {note}")


if __name__ == "__main__":
    main()
