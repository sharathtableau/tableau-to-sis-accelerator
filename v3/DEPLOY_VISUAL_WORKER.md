# Automated visual validation — deployment

> **Historical deployment design.** The warning already present below remains authoritative for this document. Use [`CURRENT_STATUS.md`](CURRENT_STATUS.md), `snowflake.yml`, and the current deployment scripts for the live path.

> ## ⚠️ READ THIS FIRST — the route below is NOT the one in use (2026-08-14)
>
> Everything in this document describes an **SPCS service built from a
> container image**. It works as a design, but it is **not how visual capture
> actually happens**, and following it will waste a day.
>
> **The live route is `notebook_capture.py`:** a Snowflake **notebook on
> container runtime** (`SYSTEM_COMPUTE_POOL_CPU`) installs Playwright +
> Chromium at run time, starts the generated app and captures it with the same
> `app_screenshot.capture_app`. **No Docker, no image build, no registry push,
> no admin grant.** Proven standalone: 10/10 Superstore dashboards in ~160s.
> See `ARCHITECTURE.md` §24 and `sql/validation_capture_egress.sql`.
>
> **The compute-pool blocker described below was wrong.** `USAGE` on
> `SYSTEM_COMPUTE_POOL_CPU` has been granted to `PUBLIC` since 2024-12-10 —
> `WBR_OWNER` always had it. The claim came from grepping `SHOW GRANTS TO ROLE`
> (which cannot see `PUBLIC` grants) rather than running the command.
>
> **Keep this document** for the case where a long-lived service genuinely
> earns its cost — scheduled runs, or moving capture off the request path. Do
> not treat it as the current deployment path.

One user action: **click "Run Validation"**. No download, no local command, no
upload, no manual browser, no SSO prompt.

The screenshot step runs in a browser-enabled worker because Streamlit in
Snowflake cannot run Playwright, Chromium, a subprocess Streamlit server, or
bind a local port. Everything else already runs inside SiS.

```
SiS "Run Validation"
   │ builds bundle (app + IR + manifest + Tableau evidence), SHA-256 each
   │ PUT  → @VALIDATION_CAPTURE_STAGE/<run_id>/input/
   │ INSERT one QUEUED row
   ▼ returns immediately, then polls
VALIDATION_CAPTURE_JOBS  ◄──────── atomic claim ────────┐
   │                                                     │
   ▼                                                     │
SPCS worker (Chromium + Playwright + Streamlit)          │
   verify hashes → run app → capture → upload → COMPLETE ┘
```

## 1. Create the queue, stage and role

```bash
snow sql -c wbr -f sql/validation_capture.sql
```

Creates `WBR_DB.PIPELINE_DEMO.VALIDATION_CAPTURE_JOBS`,
`@WBR_DB.PIPELINE_DEMO.VALIDATION_CAPTURE_STAGE` (SSE-encrypted, so the PNGs
are a valid `AI_COMPLETE(TO_FILE(...))` source later), and
`VALIDATION_WORKER_ROLE` — read/update on the queue, read/write on the stage,
`SELECT` on the source tables, warehouse usage, **nothing administrative**.

## 2. Build and push the worker image

Requires Docker **once**, by whoever builds the accelerator release — never by
a client.

```bash
snow sql -c wbr -q "CREATE IMAGE REPOSITORY IF NOT EXISTS WBR_DB.PIPELINE_DEMO.ACCELERATOR"
snow spcs image-registry login -c wbr

REPO=wb19670-c2gpartners.registry.snowflakecomputing.com/wbr_db/pipeline_demo/accelerator
docker build -f worker/Dockerfile -t $REPO/validation-worker:1 .
docker push $REPO/validation-worker:1
```

## 3. Grant a compute pool  ← **the one blocker**

`WBR_OWNER` currently has **`CREATE SERVICE` and `CREATE IMAGE REPOSITORY`**
on `WBR_DB.PIPELINE_DEMO`, but **no compute-pool privilege**. `CREATE COMPUTE
POOL` is account-level, so an `ACCOUNTADMIN` must run one of:

```sql
-- preferred: a dedicated small pool
CREATE COMPUTE POOL VALIDATION_CAPTURE_POOL
  MIN_NODES = 1 MAX_NODES = 1
  INSTANCE_FAMILY = CPU_X64_M          -- Chromium wants ~2 vCPU / 4 GB
  AUTO_SUSPEND_SECS = 600;
GRANT USAGE ON COMPUTE POOL VALIDATION_CAPTURE_POOL TO ROLE WBR_OWNER;

-- or reuse an existing idle pool
GRANT USAGE ON COMPUTE POOL CONNECTIVITY_TEST_POOL_A TO ROLE WBR_OWNER;

GRANT ROLE VALIDATION_WORKER_ROLE TO ROLE WBR_OWNER;
```

## 4. Start the worker service

```sql
CREATE SERVICE WBR_DB.PIPELINE_DEMO.VALIDATION_CAPTURE_WORKER
  IN COMPUTE POOL VALIDATION_CAPTURE_POOL
  FROM SPECIFICATION $$
spec:
  containers:
    - name: worker
      image: /wbr_db/pipeline_demo/accelerator/validation-worker:1
      env:
        SNOWFLAKE_WAREHOUSE: POWERHOUSE
        SNOWFLAKE_DATABASE: WBR_DB
        SNOWFLAKE_SCHEMA: PIPELINE_DEMO
        SNOWFLAKE_ROLE: VALIDATION_WORKER_ROLE
        VALIDATION_POLL_SECONDS: "5"
        VALIDATION_JOB_TIMEOUT: "900"
      resources:
        requests: {cpu: "2", memory: "4Gi"}
$$
  MIN_INSTANCES = 1 MAX_INSTANCES = 1;
```

Inside SPCS the worker authenticates with the **service identity** injected at
`/snowflake/session/token` — no stored credential at all. Outside SPCS it
falls back to key-pair (`SNOWFLAKE_PRIVATE_KEY` / `_PATH`), supplied as a
container secret. `externalbrowser` is refused by design, and the gate asserts
it never appears as an authenticator value.

Watch it:

```sql
SELECT SYSTEM$GET_SERVICE_LOGS('WBR_DB.PIPELINE_DEMO.VALIDATION_CAPTURE_WORKER', 0, 'worker', 100);
```

## 5. Verify

```sql
SELECT RUN_ID, STATUS, PROGRESS_LABEL, PROGRESS_DONE, PROGRESS_TOTAL,
       WORKER_ID, ATTEMPT, ERROR_CODE, ERROR_MESSAGE
  FROM WBR_DB.PIPELINE_DEMO.VALIDATION_CAPTURE_JOBS
 ORDER BY CREATED_AT DESC LIMIT 5;
```

Expected: `QUEUED → CLAIMED → STARTING_APP → CAPTURING → GENERATING_REPORT →
COMPLETED`, ~150–250s for Superstore.

## Operational behaviour

| Concern | Behaviour |
|---|---|
| Two workers, one job | Atomic claim (`AND STATUS = 'QUEUED'`); exactly one wins. Gated. |
| Worker dies mid-job | No heartbeat for 180s → requeued, bounded by `MAX_ATTEMPTS = 3` |
| Heartbeat | Every 15s, carrying per-dashboard progress |
| Infra failure | Retried (`CHROMIUM_STARTUP`, `STAGE_IO`, `SNOWFLAKE_UNAVAILABLE`, `WORKER_LOST`) |
| App failure | **Not** retried — deterministic, fails identically forever |
| Duplicate submit | `enqueue` is idempotent on `RUN_ID`. Gated. |
| Tampered bundle | SHA-256 gate fails the job. A screenshot of different code is not evidence. Gated. |
| Nothing captured | `BLOCKED`, never `FAILED`, never a pass |
| Cancellation | `CANCEL_REQUESTED`; worker stops at its next heartbeat |
| Cleanup | Job dir removed in `finally`; Streamlit + Chromium always torn down |

## Rules that must not be relaxed

- `headless_render.py` output is **never** passed as `streamlit_screenshot`.
  It may appear separately, labelled *Server-side application render —
  non-authoritative*. Only a real Playwright capture satisfies the screenshot
  gate.
- Missing evidence stays **BLOCKED**. It is never upgraded to a pass and never
  substituted.
- The worker **reuses** `app_screenshot.capture_app`. A second browser
  implementation would drift from the first, and a screenshot is evidence.

## Current status

| Piece | State |
|---|---|
| Queue + stage + role SQL | ✅ written |
| Shared queue/bundle module (`validation_jobs.py`) | ✅ written, gated |
| Worker (`validation_worker.py`) | ✅ written, gated |
| Dockerfile + pinned requirements | ✅ written |
| SiS submission + polling (`validation_capture_ui.py`) | ✅ written, gated |
| Automated tests | ✅ `test_automated_capture_queue_and_bundle`, teeth proven |
| Compute pool grant | ❌ **blocked — needs ACCOUNTADMIN** |
| Image build + push | ❌ needs Docker once |
| End-to-end acceptance run | ❌ blocked on the two above |
