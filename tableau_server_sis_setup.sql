-- One-time account setup so the DEPLOYED pipeline_demo Streamlit app (running
-- inside Snowflake, not on your laptop) can call the Tableau REST API.
--
-- WHY THIS IS NEEDED (not needed for local `streamlit run pipeline_app.py`):
--   1. Streamlit-in-Snowflake apps have ZERO outbound internet access by
--      default -- reaching Tableau's domain requires an EXTERNAL ACCESS
--      INTEGRATION naming an allowed host.
--   2. SiS apps have no .env / OS-env-var mechanism -- TABLEAU_PAT_NAME /
--      TABLEAU_PAT_SECRET only work for local dev. The real mechanism is a
--      Snowflake SECRET object, bound to the app and read via `_snowflake.
--      get_username_password()` (see tableau_server.py's _pat_from_sis_secret).
--
-- RUN THIS YOURSELF in a Snowsight worksheet under a role with CREATE
-- INTEGRATION / CREATE NETWORK RULE / CREATE SECRET privilege -- the
-- assistant does not run this for you, since it embeds your literal Tableau
-- PAT secret. Replace the two placeholders below before running.
--
-- If your role is WBR_OWNER and you don't have those privileges yet, see
-- tableau_server_sis_grants_request.sql -- it's the exact, narrow grant an
-- account admin needs to run ONCE (not ACCOUNTADMIN itself) so WBR_OWNER can
-- run everything below directly, no "USE ROLE ACCOUNTADMIN" needed.
--
-- Adjust the network rule's VALUE_LIST if your Tableau site is on a
-- different pod/host than prod-useast-b.online.tableau.com.

-- Uncomment if you actually have ACCOUNTADMIN; leave commented and just run
-- as WBR_OWNER once the grants from tableau_server_sis_grants_request.sql
-- have landed.
-- USE ROLE ACCOUNTADMIN;

CREATE OR REPLACE NETWORK RULE WBR_DB.PUBLIC.TABLEAU_NETWORK_RULE
  MODE = EGRESS
  TYPE = HOST_PORT
  VALUE_LIST = ('prod-useast-b.online.tableau.com');  -- add more pods/hosts as needed

CREATE OR REPLACE SECRET WBR_DB.PUBLIC.TABLEAU_PAT_SECRET
  TYPE = PASSWORD
  USERNAME = 'tableau'        -- e.g. 'tableau'
  PASSWORD = '<PASTE_YOUR_TABLEAU_PAT_SECRET_HERE>';  -- the PAT secret value --
                                      -- fill this in yourself, do not paste it
                                      -- to an assistant or commit it to git.
                                      --
                                      -- 2026-08-13: a REAL PAT secret was
                                      -- committed here and GitHub push
                                      -- protection rejected the push. That
                                      -- token must be treated as compromised
                                      -- and revoked in Tableau
                                      -- (Account Settings -> Personal Access
                                      -- Tokens). Never restore a live value
                                      -- to this line -- run this script from
                                      -- a local copy instead.

CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION TABLEAU_ACCESS_INTEGRATION
  ALLOWED_NETWORK_RULES = (WBR_DB.PUBLIC.TABLEAU_NETWORK_RULE)
  ALLOWED_AUTHENTICATION_SECRETS = (WBR_DB.PUBLIC.TABLEAU_PAT_SECRET)
  ENABLED = TRUE;

-- Bind both to the deployed app. The SECRETS key 'tableau_pat' MUST match
-- tableau_server.SIS_SECRET_NAME exactly -- it's how the Python code finds it.
ALTER STREAMLIT WBR_DB.PUBLIC.TABLEAU_TO_SIS_PIPELINE_DEMO
  SET EXTERNAL_ACCESS_INTEGRATIONS = (TABLEAU_ACCESS_INTEGRATION)
  SECRETS = ('tableau_pat' = WBR_DB.PUBLIC.TABLEAU_PAT_SECRET);

-- Verify (should show the integration + secret attached):
DESCRIBE STREAMLIT WBR_DB.PUBLIC.TABLEAU_TO_SIS_PIPELINE_DEMO;
