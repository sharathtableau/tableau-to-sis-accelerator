-- SL07 (release 20a): Semantic View inventory — every Semantic View in the account, with its owner,
-- its physical tables and join keys, grouped so views over the same tables and keys sit together.
--
-- Run in Snowsight as WBR_OWNER with Run All (Ctrl+Shift+Enter). It only READS views and writes the
-- result to two tables in the evidence schema (replaced on every run). Nothing is dropped or granted.
-- Views this role cannot see are not listed; run the script again as their owner role (e.g. DS_BASIC)
-- to include them.

USE ROLE WBR_OWNER;
CREATE SCHEMA IF NOT EXISTS WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE;

SHOW SEMANTIC VIEWS IN ACCOUNT;

CREATE OR REPLACE TABLE WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE.SV_INVENTORY COMMENT = 'SL07: Semantic Views in the account (latest inventory run)' AS
SELECT "database_name" AS DB, "schema_name" AS SCH, "name" AS NAME, "owner" AS OWNER,
       "created_on" AS CREATED_ON, "comment" AS COMMENT, CURRENT_ROLE() AS LISTED_BY_ROLE,
       CURRENT_TIMESTAMP() AS INVENTORIED_AT
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()));

CREATE OR REPLACE TABLE WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE.SV_INVENTORY_PARTS (
  DB STRING, SCH STRING, NAME STRING, PART STRING, ALIAS STRING, A STRING, B STRING, C STRING, D STRING
) COMMENT = 'SL07: tables and relationships of each Semantic View (latest inventory run)';

EXECUTE IMMEDIATE $$
DECLARE
  c CURSOR FOR SELECT DB, SCH, NAME FROM WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE.SV_INVENTORY;
  db STRING; sch STRING; nm STRING; err STRING;
BEGIN
  FOR r IN c DO
    db := r.DB; sch := r.SCH; nm := r.NAME;
    BEGIN
      EXECUTE IMMEDIATE 'DESCRIBE SEMANTIC VIEW "' || db || '"."' || sch || '"."' || nm || '"';
      INSERT INTO WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE.SV_INVENTORY_PARTS
        SELECT :db, :sch, :nm, "object_kind", "object_name",
               MAX(IFF("property" IN ('BASE_TABLE_DATABASE_NAME', 'TABLE'), "property_value", NULL)),
               MAX(IFF("property" IN ('BASE_TABLE_SCHEMA_NAME', 'FOREIGN_KEY'), "property_value", NULL)),
               MAX(IFF("property" IN ('BASE_TABLE_NAME', 'REF_TABLE'), "property_value", NULL)),
               MAX(IFF("property" IN ('PRIMARY_KEY', 'REF_KEY'), "property_value", NULL))
        FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))
        WHERE "object_kind" IN ('TABLE', 'RELATIONSHIP')
        GROUP BY "object_kind", "object_name";
    EXCEPTION
      WHEN OTHER THEN
        err := SQLERRM;
        INSERT INTO WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE.SV_INVENTORY_PARTS
          SELECT :db, :sch, :nm, 'ERROR', NULL, LEFT(:err, 300), NULL, NULL, NULL;
    END;
  END FOR;
  RETURN 'Inventory done: see the report below';
END;
$$;

-- The report: one row per view. Views with the same TABLES and KEYS are duplicates of each other.
CREATE OR REPLACE VIEW WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE.V_SV_DUPLICATES COMMENT = 'SL07: Semantic Views grouped by physical tables and join keys (latest inventory run)' AS
WITH t AS (
  SELECT DB, SCH, NAME, ALIAS, A || '.' || B || '.' || C AS BASE_TABLE
  FROM WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE.SV_INVENTORY_PARTS WHERE PART = 'TABLE'),
k AS (
  SELECT p.DB, p.SCH, p.NAME, f.BASE_TABLE || REGEXP_REPLACE(p.B, '[\\[\\]" ]', '') || ' -> ' ||
         r.BASE_TABLE || REGEXP_REPLACE(p.D, '[\\[\\]" ]', '') AS KEY_TEXT
  FROM WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE.SV_INVENTORY_PARTS p
  JOIN t f ON f.DB = p.DB AND f.SCH = p.SCH AND f.NAME = p.NAME AND f.ALIAS = p.A
  JOIN t r ON r.DB = p.DB AND r.SCH = p.SCH AND r.NAME = p.NAME AND r.ALIAS = p.C
  WHERE p.PART = 'RELATIONSHIP'),
tt AS (SELECT DB, SCH, NAME, LISTAGG(DISTINCT BASE_TABLE, ', ') WITHIN GROUP (ORDER BY BASE_TABLE) AS TABLES
       FROM t GROUP BY DB, SCH, NAME),
kk AS (SELECT DB, SCH, NAME, LISTAGG(DISTINCT KEY_TEXT, '; ') WITHIN GROUP (ORDER BY KEY_TEXT) AS KEYS
       FROM k GROUP BY DB, SCH, NAME),
ee AS (SELECT DB, SCH, NAME, MAX(A) AS DESCRIBE_ERROR
       FROM WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE.SV_INVENTORY_PARTS WHERE PART = 'ERROR' GROUP BY DB, SCH, NAME),
g AS (
  SELECT i.DB, i.SCH, i.NAME, i.OWNER, i.CREATED_ON, i.COMMENT, tt.TABLES, kk.KEYS, ee.DESCRIBE_ERROR
  FROM WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE.SV_INVENTORY i
  LEFT JOIN tt ON tt.DB = i.DB AND tt.SCH = i.SCH AND tt.NAME = i.NAME
  LEFT JOIN kk ON kk.DB = i.DB AND kk.SCH = i.SCH AND kk.NAME = i.NAME
  LEFT JOIN ee ON ee.DB = i.DB AND ee.SCH = i.SCH AND ee.NAME = i.NAME)
SELECT DB || '.' || SCH || '.' || NAME AS VIEW_NAME, OWNER, CREATED_ON, TABLES, KEYS,
       DENSE_RANK() OVER (ORDER BY TABLES, COALESCE(KEYS, '')) AS GROUP_NO,
       COUNT(*) OVER (PARTITION BY TABLES, COALESCE(KEYS, '')) AS VIEWS_IN_GROUP,
       IFF(NAME RLIKE '.*_SV[A-Z]*_F[0-9A-F]{10}(_R[2-9])?(_CORTEX)?', 'shared (release 20+)', 'per-workbook (before release 20)') AS NAMING,
       IFF(ENDSWITH(NAME, '_CORTEX'), 'Cortex-designed', 'code-built') AS BUILT_BY,
       IFF(COMMENT ILIKE '%[CERTIFIED]%', TRUE, FALSE) AS CERTIFIED,
       DESCRIBE_ERROR
FROM g;

SELECT * FROM WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE.V_SV_DUPLICATES
ORDER BY VIEWS_IN_GROUP DESC, GROUP_NO, BUILT_BY, CREATED_ON DESC;
