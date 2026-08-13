# Deploying the generated app to Streamlit in Snowflake

The same code runs locally (DuckDB) and in Snowflake (Snowpark) — no edits.

## 1. Load the data

Create one table per Tableau datasource (names must match `config.DATASOURCES`):

```sql
CREATE DATABASE IF NOT EXISTS SUPERSTORE;
USE SCHEMA SUPERSTORE.PUBLIC;

-- Column names are the UPPER_SNAKE versions of the CSV headers
-- (that is exactly what backend.py produces locally).
CREATE OR REPLACE TABLE ORDERS (...);            -- from "Sample - Superstore.csv"
CREATE OR REPLACE TABLE SALES_COMMISSION (...);  -- from "Sales Commission.csv"
CREATE OR REPLACE TABLE SALES_TARGET (...);      -- from "Sales Target.xlsx"
```

Fastest path: load the CSVs in `data/` with Snowsight's "Load Data" wizard and
rename columns to UPPER_SNAKE (`Order Date` -> `ORDER_DATE`, `Sub-Category` ->
`SUB_CATEGORY`, ...), or use `snow object stage copy` + `COPY INTO`.

Verify with the same numbers the local harness checks:

```sql
SELECT SUM(SALES), SUM(PROFIT), SUM(QUANTITY) FROM ORDERS;
-- expect 2,326,534 | 292,297 | 38,654 for the sample dataset
```

## 2. Deploy the app

With [Snowflake CLI](https://docs.snowflake.com/en/developer-guide/snowflake-cli):

```bash
snow streamlit deploy --replace
```

(Uses `snowflake.yml`; set your connection with `snow connection add` first,
and adjust `query_warehouse` in `snowflake.yml`.)

Manual alternative: create a Streamlit app in Snowsight, upload
`app.py, engine.py, backend.py, config.py, calc_translator.py, findings.py,
profile_superstore.py`, and add packages `altair, pandas, plotly`.

## 3. What switches automatically

`backend.run_sql()` detects the active Snowpark session. In Snowflake it runs
the SQL through `get_active_session()`; locally it loads `data/*` into DuckDB
and rewrites the fully-qualified table names. The SQL text is identical in
both environments.

## 4. Re-generating after a workbook change

```bash
python tableau_parser.py YourBook.twb -o workbook_ir.json
python codegen.py workbook_ir.json -o app.py
python report.py YourBook.twb          # refresh the compatibility report
snow streamlit deploy --replace
```
