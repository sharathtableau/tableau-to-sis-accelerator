-- SL07 (release 20a): cleanup plan for duplicate Semantic Views. This script WRITES NO DROP ITSELF:
-- it lists the statements for you to review, copy and run.
--
-- Before you start
--   1. Run sql/sl07_semantic_view_inventory.sql (same session not needed).
--   2. Re-run each affected workbook in the lab app once (release 20+). The run reuses or creates the
--      shared view (name ..._SVLAB_F<fingerprint>) and the dashboard reads through it; redeploy any app
--      that should move to it. A deployed app still reading an old view breaks if that view is dropped.
--   3. Put the views you want to KEEP in the list below (one per group). If you list none for a group,
--      the shared view (release 20+) is kept, else the newest code-built view.

USE ROLE WBR_OWNER;

CREATE OR REPLACE TEMPORARY TABLE SV_KEEP (VIEW_NAME STRING);
INSERT INTO SV_KEEP (VIEW_NAME) VALUES
  ('WBR_DB.TABLEAU_ACCELERATOR_V3.REPLACE_WITH_THE_VIEW_TO_KEEP');   -- add one line per group you decide

WITH d AS (SELECT * FROM WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE.V_SV_DUPLICATES WHERE VIEWS_IN_GROUP > 1),
keep AS (
  SELECT GROUP_NO, COALESCE(
           MAX(IFF(VIEW_NAME IN (SELECT VIEW_NAME FROM SV_KEEP), VIEW_NAME, NULL)),
           MAX_BY(VIEW_NAME, IFF(NAMING LIKE 'shared%', 2, 0) + IFF(BUILT_BY = 'code-built', 1, 0)
                             + DATE_PART(EPOCH_SECOND, CREATED_ON) / 1e10)) AS KEEP_VIEW
  FROM d GROUP BY GROUP_NO)
SELECT d.GROUP_NO, d.TABLES, k.KEEP_VIEW, d.VIEW_NAME AS RETIRE_VIEW, d.OWNER, d.CERTIFIED,
       IFF(d.CERTIFIED, '-- certified: not retired by this script',
           'USE ROLE ' || d.OWNER || '; DROP SEMANTIC VIEW IF EXISTS ' || d.VIEW_NAME || ';') AS STATEMENT_TO_REVIEW
FROM d JOIN keep k ON k.GROUP_NO = d.GROUP_NO
WHERE d.VIEW_NAME <> k.KEEP_VIEW
ORDER BY d.GROUP_NO, d.VIEW_NAME;
