-- Egress + runtime for browser-based capture INSIDE Snowflake.
--
-- PROVEN 2026-08-14: no Docker, no image build, no local machine. A Snowflake
-- Notebook on CONTAINER RUNTIME running on SYSTEM_COMPUTE_POOL_CPU installs
-- Playwright + Chromium at run time and screenshots a real localhost page.
-- Result: 3200x1800 PNG, 2144 distinct colours, written to the stage.
--
-- Container runtime has NO outbound network by default -- pip fails with a DNS
-- error -- so these two objects are what make it work. WBR_OWNER already holds
-- CREATE INTEGRATION, so no admin is required.

CREATE OR REPLACE NETWORK RULE WBR_DB.DBX.PLAYWRIGHT_EGRESS_RULE
  MODE = EGRESS TYPE = HOST_PORT
  VALUE_LIST = (
    -- pip
    'pypi.org:443','files.pythonhosted.org:443','pypi.python.org:443',
    -- Playwright's Chromium download (the CDN host is easy to miss: the
    -- install fails with ENOTFOUND on this exact name)
    'cdn.playwright.dev:443','playwright.azureedge.net:443',
    'playwright.download.prss.microsoft.com:443',
    -- apt, for Chromium's system libraries. The runtime is Ubuntu NOBLE and
    -- runs as root (euid 0), so `playwright install-deps chromium` works --
    -- do NOT hand-list packages, noble renamed several to *t64 (libasound2 ->
    -- libasound2t64) and a hand-list fails with "no installation candidate".
    'archive.ubuntu.com:80','security.ubuntu.com:80','ports.ubuntu.com:80',
    'azure.archive.ubuntu.com:80');

CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION PLAYWRIGHT_EAI
  ALLOWED_NETWORK_RULES = (WBR_DB.DBX.PLAYWRIGHT_EGRESS_RULE)
  ENABLED = TRUE
  COMMENT = 'pypi + Playwright CDN + apt egress for browser-based validation capture';

-- Register and run the capture notebook. ADD LIVE VERSION is required before
-- EXECUTE, otherwise it fails with "Live version is not found".
CREATE OR REPLACE NOTEBOOK WBR_DB.DBX.BROWSER_SMOKE
  FROM '@WBR_DB.DBX.VALIDATION_CAPTURE_STAGE/smoke2'
  MAIN_FILE = 'browser_smoke.ipynb'
  QUERY_WAREHOUSE = POWERHOUSE
  RUNTIME_NAME = 'SYSTEM$BASIC_RUNTIME'
  COMPUTE_POOL = 'SYSTEM_COMPUTE_POOL_CPU';

ALTER NOTEBOOK WBR_DB.DBX.BROWSER_SMOKE ADD LIVE VERSION FROM LAST;
ALTER NOTEBOOK WBR_DB.DBX.BROWSER_SMOKE SET EXTERNAL_ACCESS_INTEGRATIONS = (PLAYWRIGHT_EAI);

EXECUTE NOTEBOOK WBR_DB.DBX.BROWSER_SMOKE();

LIST @WBR_DB.DBX.VALIDATION_CAPTURE_STAGE/smoke-test/;
