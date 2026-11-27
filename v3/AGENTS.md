# V3 implementation guidance

## Session start (mandatory, every new session)
1. Read V3_ENTERPRISE_MASTER_PLAN.md first. It is the single working tracker: priorities, status, decisions and the "Next session starts here" block.
2. Verify the current state before acting: rebuild the package with `python v3_package.py` logic (workbench_bundle) and compare its build SHA256 with the deployed build recorded in the master plan; read only the evidence folder that block names.
3. Continue the first OPEN item in the master plan unless the user's latest message says otherwise.
4. Do not re-read every historical document. Other V3_*.md files are detailed references; open one only when the item you are working on links to it.

## Session end (mandatory)
Update exactly these: (1) V3_ENTERPRISE_MASTER_PLAN.md — the item rows you touched, the "Next session starts here" block (build hash, live target, last evidence path, next item, open questions) and one change-log row; (2) append one entry to V3_FIX_JOURNAL.md using its "Copy for every increment" template; (3) put commands, hashes and outputs in a new dated reports/<item>_<date>/ folder. Update a feature-contract document only when that contract changed. V3_STATUS, V3_NEXT_IMPLEMENTATION, V3_ENTERPRISE_FIX_TRACKER, V3_SESSION_MEMORY, V3_GAP_LEDGER and V3_GENERALIZATION_HANDOVER are frozen references: do not update them. Never prepend checkpoint blocks to documents.

## Standing rules
The enterprise objective and full background are in V3_ENTERPRISE_HANDOVER.md (reference). Use actual source/evidence over prose; older checkpoint narratives in other documents are historical.

Work only in this V3 directory. Do not create another product version. Preserve original/V2, concurrent work and existing deployments. Check git state and destination hashes before edits. For authorized converter work, preserve original capability, use generic structural fixes, and run the before/after regression, V3 tests and relevant independent numeric/corpus/UI checks specified in the handover. Never infer acceptance from parsing or rendering alone.

AI proposes bounded data; independent source semantics, bindings and validation control execution and acceptance. No arbitrary model SQL/Python or workbook-name special cases. Keep secrets out of prompts, reports and shared bundles.

The documentation checkpoint records an implementation pause. A new user request to continue implementation resumes it; reconcile the latest request and current state before acting. Recorded deployment approval is scoped to the existing V3 target after checks and must be reconciled with the current user/session; this file grants no broader authorization.

After meaningful work follow the Session end rule above; update a feature contract document only when its contract actually changed. Preserve dated evidence. Completing documentation or a bounded feature does not complete the enterprise product. Do not spawn additional agents or schedule background work without explicit user authorization.
