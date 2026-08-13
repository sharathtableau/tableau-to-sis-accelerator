-- Grant request for R1 (Tableau Server/Cloud pull, inside the deployed
-- pipeline_demo Streamlit-in-Snowflake app).
--
-- WHY: the pipeline_demo app currently has zero outbound network access and
-- no PAT credential -- this is Snowflake's default security posture for
-- Streamlit-in-Snowflake apps, not a bug. Fixing it needs one CREATE SECRET,
-- one CREATE NETWORK RULE, one CREATE EXTERNAL ACCESS INTEGRATION, and one
-- ALTER STREAMLIT binding both to the app (see tableau_server_sis_setup.sql
-- in the same repo for the exact statements).
--
-- WHO NEEDS TO RUN THIS: someone with ACCOUNTADMIN (or an equivalent role
-- holding these three privileges already). These are the ONLY grants
-- requested -- not ACCOUNTADMIN itself, not ownership of anything beyond
-- what WBR_OWNER already has.
--
--   1. CREATE INTEGRATION -- account-level (External Access Integrations
--      aren't schema-scoped objects in Snowflake, so this can't be narrowed
--      to WBR_DB.PUBLIC the way the other two can).
--   2. CREATE NETWORK RULE  -- scoped to WBR_DB.PUBLIC only.
--   3. CREATE SECRET        -- scoped to WBR_DB.PUBLIC only.
--
-- WBR_OWNER already owns the pipeline_demo Streamlit app itself (it was
-- deployed under that role), so no extra grant is needed for the final
-- ALTER STREAMLIT ... SET EXTERNAL_ACCESS_INTEGRATIONS/SECRETS step.

GRANT CREATE INTEGRATION ON ACCOUNT TO ROLE WBR_OWNER;
GRANT CREATE NETWORK RULE ON SCHEMA WBR_DB.PUBLIC TO ROLE WBR_OWNER;
GRANT CREATE SECRET ON SCHEMA WBR_DB.PUBLIC TO ROLE WBR_OWNER;

-- After these are granted, WBR_OWNER can run tableau_server_sis_setup.sql
-- directly (skip its "USE ROLE ACCOUNTADMIN" line -- WBR_OWNER will now have
-- everything that script needs) with no further admin involvement.
