# Running the Converter inside Snowflake (Snowsight)

The **Converter** is one Streamlit-in-Snowflake app. You upload the accelerator
code to it **once**; after that you just use its UI — upload a Tableau workbook,
it parses, loads the data into Snowflake tables, and renders the dashboard
live. Nothing runs on a laptop.

## A. Try it locally first (optional, proves the concept)

Already running at **http://localhost:8505** — drag a `.twbx` onto the uploader
and watch it convert + render. Same code runs in Snowflake.

## B. Deploy the Converter to Snowsight (one time)

1. Snowsight → **Projects → Streamlit → + Streamlit App**.
   Name it e.g. `Tableau Converter`. Pick a database/schema and warehouse.
2. Open the app's **Files** panel (left of the code editor) and **upload these
   9 files** from this folder:
   - `converter_app.py`   ← set this as the **main file**
   - `tableau_parser.py`
   - `init_workbook.py`
   - `engine.py`
   - `backend.py`
   - `config.py`
   - `calc_translator.py`
   - `findings.py`
   - `profile_superstore.py`
3. **Packages** (top-right of the editor): add
   `altair`, `pandas`, `plotly`, `openpyxl`.
4. Grant the app's role rights to create tables (once). A Streamlit-in-
   Snowflake app runs with its **owner role's** rights, which is usually
   locked well below ACCOUNTADMIN — `pipeline.ensure_target()` tries `USE
   SCHEMA` first and only falls back to `CREATE`, so if the schema below
   already exists and the owner role has USAGE on it, **no grant is needed
   at all**. If it doesn't exist yet, run once (replace `<APP_OWNER_ROLE>`
   with the app's actual owner role — `DESCRIBE STREAMLIT <name>` shows it):
   ```sql
   CREATE SCHEMA IF NOT EXISTS WBR_DB.PIPELINE_DEMO;
   GRANT USAGE, CREATE TABLE ON SCHEMA WBR_DB.PIPELINE_DEMO TO ROLE <APP_OWNER_ROLE>;
   ```
   (Deliberately a DEDICATED schema, not `WBR_DB.PUBLIC` — that schema holds
   the real corpus tables the deployed E-Commerce app and the Cortex semantic
   views depend on; a demo re-upload with `write_pandas(overwrite=True)`
   would silently replace them if pointed there instead.)
5. Click **Run**.

## C. Use it

Upload a `.twb`/`.twbx` in the app. It will:
- parse the workbook,
- load each datasource's data into `TABLEAU_MIGRATION.PUBLIC.*` (write_pandas),
- render the converted dashboard in the same window.

## What converts fully in-Snowflake vs. needs a pre-step

| Workbook data | In Snowflake? |
|---|---|
| Live Snowflake-connected (data already in Snowflake) | ✅ fully — map to existing tables |
| CSV / Excel bundled in the `.twbx` | ✅ fully — loaded via write_pandas |
| `.hyper` extract | ⚠️ decode `.hyper` → CSV once locally (`python init_workbook.py Book.twbx`), then upload; `tableauhyperapi` is not in Snowflake's Anaconda channel |

## Notes

- `converter_app.py` runs identically locally (DuckDB) and in Snowflake
  (Snowpark) — the only difference is where the tables live.
- The accelerator-only tooling (`convert.py`, `report.py`, `audit_coverage.py`,
  `codegen.py`, `load_snowflake.py`) is NOT needed in Snowsight — those are for
  the laptop workflow. The Converter app needs only the 9 files above.
