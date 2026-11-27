-- Durable job queue + artifact stage for AUTOMATED visual validation.
--
-- The SiS accelerator cannot run Playwright/Chromium/a second Streamlit
-- server, so the screenshot step runs in a browser-enabled SPCS worker. This
-- file creates the two objects both sides share, plus a least-privilege role
-- for the worker.
--
-- Run once, as a role that can create in WBR_DB.PIPELINE_DEMO.
--   snow sql -c wbr -f sql/validation_capture.sql

USE DATABASE WBR_DB;
USE SCHEMA PIPELINE_DEMO;

-- ---------------------------------------------------------------- job queue
-- One row per "Run Validation" click. The SiS app INSERTs QUEUED and then
-- only ever reads; the worker owns every subsequent transition.
CREATE TABLE IF NOT EXISTS VALIDATION_CAPTURE_JOBS (
    RUN_ID                STRING        NOT NULL PRIMARY KEY,
    WORKBOOK_ID           STRING,
    STATUS                STRING        NOT NULL,
    CREATED_AT            TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    STARTED_AT            TIMESTAMP_LTZ,
    COMPLETED_AT          TIMESTAMP_LTZ,
    HEARTBEAT_AT          TIMESTAMP_LTZ,
    ATTEMPT               NUMBER        NOT NULL DEFAULT 0,
    APP_ARTIFACT_PATH     STRING,
    INPUT_MANIFEST_PATH   STRING,
    RESULT_MANIFEST_PATH  STRING,
    REPORT_PATH           STRING,
    ERROR_CODE            STRING,
    ERROR_MESSAGE         STRING,
    WORKER_ID             STRING,
    -- Progress for the SiS poller: free-form label plus a fraction, so the UI
    -- can say "Capturing dashboard 3 of 10" without parsing logs.
    PROGRESS_LABEL        STRING,
    PROGRESS_DONE         NUMBER,
    PROGRESS_TOTAL        NUMBER,
    -- Cancellation is a REQUEST, not a status: the worker owns STATUS, so a
    -- cancel must not race a transition. The worker checks this flag at every
    -- heartbeat and stops cleanly.
    CANCEL_REQUESTED      BOOLEAN       NOT NULL DEFAULT FALSE
);

COMMENT ON TABLE VALIDATION_CAPTURE_JOBS IS
  'Visual-validation capture queue. STATUS in (QUEUED, CLAIMED, STARTING_APP, '
  'CAPTURING, GENERATING_REPORT, COMPLETED, FAILED, BLOCKED). BLOCKED means '
  'evidence could not be produced and is NOT a pass -- same rule as the pack.';

-- ------------------------------------------------------------------- stage
-- Server-side encrypted so AI_COMPLETE(TO_FILE(...)) can read the PNGs later
-- for the Cortex-vision comparison (a default client-side-encrypted stage is
-- NOT a valid AI_COMPLETE file source -- learned the hard way on R8).
CREATE STAGE IF NOT EXISTS VALIDATION_CAPTURE_STAGE
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    DIRECTORY = (ENABLE = TRUE)
    COMMENT = 'Per-run bundles: @VALIDATION_CAPTURE_STAGE/<run_id>/{input,output}/';

-- ------------------------------------------------------- least-privilege role
-- The worker gets exactly what it needs and nothing administrative. It never
-- needs CREATE, never needs ACCOUNTADMIN, and never needs write access to the
-- source tables it validates.
CREATE ROLE IF NOT EXISTS VALIDATION_WORKER_ROLE;

GRANT USAGE ON DATABASE WBR_DB                        TO ROLE VALIDATION_WORKER_ROLE;
GRANT USAGE ON SCHEMA WBR_DB.PIPELINE_DEMO            TO ROLE VALIDATION_WORKER_ROLE;
GRANT USAGE ON SCHEMA WBR_DB.PUBLIC                   TO ROLE VALIDATION_WORKER_ROLE;

-- Queue: read + update only. It must never DELETE history.
GRANT SELECT, UPDATE ON TABLE WBR_DB.PIPELINE_DEMO.VALIDATION_CAPTURE_JOBS
    TO ROLE VALIDATION_WORKER_ROLE;

-- Artifacts: read the input bundle, write the results.
GRANT READ, WRITE ON STAGE WBR_DB.PIPELINE_DEMO.VALIDATION_CAPTURE_STAGE
    TO ROLE VALIDATION_WORKER_ROLE;

-- The generated app must answer its own queries with the SAME data the
-- validation used, so the worker reads the source tables -- SELECT only.
GRANT SELECT ON ALL TABLES    IN SCHEMA WBR_DB.PUBLIC TO ROLE VALIDATION_WORKER_ROLE;
GRANT SELECT ON FUTURE TABLES IN SCHEMA WBR_DB.PUBLIC TO ROLE VALIDATION_WORKER_ROLE;
GRANT SELECT ON ALL VIEWS     IN SCHEMA WBR_DB.PUBLIC TO ROLE VALIDATION_WORKER_ROLE;
GRANT SELECT ON ALL TABLES    IN SCHEMA WBR_DB.PIPELINE_DEMO TO ROLE VALIDATION_WORKER_ROLE;
GRANT SELECT ON FUTURE TABLES IN SCHEMA WBR_DB.PIPELINE_DEMO TO ROLE VALIDATION_WORKER_ROLE;

GRANT USAGE ON WAREHOUSE POWERHOUSE TO ROLE VALIDATION_WORKER_ROLE;

-- The service identity that runs the container assumes this role.
-- GRANT ROLE VALIDATION_WORKER_ROLE TO ROLE <spcs_service_owner_role>;

-- ------------------------------------------------------- operational queries
-- Stale-claim recovery: a worker that dies mid-job stops heart-beating. Only
-- INFRASTRUCTURE-ish states are recoverable; a job is never retried forever
-- (see MAX_ATTEMPTS in validation_jobs.py).
--   UPDATE VALIDATION_CAPTURE_JOBS
--      SET STATUS = 'QUEUED', WORKER_ID = NULL
--    WHERE STATUS IN ('CLAIMED','STARTING_APP','CAPTURING','GENERATING_REPORT')
--      AND HEARTBEAT_AT < DATEADD('second', -180, CURRENT_TIMESTAMP())
--      AND ATTEMPT < 3;
