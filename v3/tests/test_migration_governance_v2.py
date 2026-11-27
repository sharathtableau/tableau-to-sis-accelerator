import io
import pathlib
import zipfile

import migration_governance_v2 as gov
import tableau_server


ROOT = pathlib.Path(__file__).resolve().parents[1]


def fixture_ir():
    return {
        "source_file": "book.twbx",
        "datasources": ["Orders"],
        "calcs": {"Profit Ratio": {"sql": "SUM(PROFIT)/SUM(SALES)"}},
        "calc_drops": {},
        "params": {"Top N": 10},
        "param_domains": {"Top N": [5, 10, 20]},
        "dashboards": [{
            "name": "Executive", "title": "Executive",
            "filters": [{"caption": "Region", "kind": "categorical"}],
            "sheets": [{"name": "Sales", "kind": "bar", "geom": {"x": 0, "y": 0, "w": 100, "h": 80},
                        "filters": [{"caption": "Region", "kind": "categorical"}]}],
        }],
    }


def test_contracts_and_hard_gates():
    ir = fixture_ir()
    assessment = gov.assess_compatibility(ir, {"blocked": []}, [])
    canonical = gov.build_canonical_ir(ir, {}, [])
    matrix = gov.build_dashboard_matrix(ir, {"summary": {"measures_checked": 1, "measures_bug": 0}})
    decision = gov.decide(assessment, matrix)
    assert assessment["status"] == "SUPPORTED"
    assert set(canonical) >= {"semantic", "visual", "layout", "interaction"}
    assert canonical["schema_version"] == gov.IR_SCHEMA_VERSION
    assert len(matrix) == 1 and matrix[0]["layout"] == "NOT_TESTED"
    assert decision["state"] == "NEEDS_REVIEW"

    blocked = gov.assess_compatibility(ir, {"blocked": ["Orders"]}, [])
    assert gov.decide(blocked, matrix)["state"] == "BLOCKED"


def test_v2_isolation_manifest():
    manifest = (ROOT / "snowflake_ai_v2.yml").read_text(encoding="utf-8")
    setup = (ROOT / "sql" / "practical_demo_v2_setup.sql").read_text(encoding="utf-8")
    assert "TABLEAU_TO_SIS_AI_ACCELERATOR_DEMO" in manifest
    assert "TABLEAU_ACCELERATOR_V2" in setup
    sql_statements = "\n".join(
        line for line in setup.splitlines() if not line.lstrip().startswith("--"))
    manifest_values = "\n".join(
        line for line in manifest.splitlines() if not line.lstrip().startswith("#"))
    forbidden = ("DROP ", "ALTER STREAMLIT", "TABLEAU_TO_SIS_PIPELINE_DEMO")
    for token in forbidden:
        assert token not in manifest_values.upper()
        assert token not in sql_statements.upper()


def test_cortex_is_advisory_when_unavailable():
    result = gov.cortex_critic(None, {"assessment": {}})
    assert result["status"] == "UNAVAILABLE"
    assert result["discrepancies"] == []


def test_component_pass_is_not_presented_as_workbook_pass():
    source = (ROOT / "pipeline_app_ai_v2.py").read_text(encoding="utf-8")
    assert "Validation passed. No numerical mismatches" not in source
    assert "This is not the overall workbook verdict" in source
    assert "Eligibility means" in source


def test_validation_ui_keeps_reports_and_demotes_engineering_downloads():
    source = (ROOT / "pipeline_app_ai_v2.py").read_text(encoding="utf-8")
    advanced = source.index('st.expander("Advanced / engineering artifacts"')
    notebook = source.index('st.download_button("Download validation notebook"')
    proof = source.index('st.expander("Proof-first validation"')
    migration = source.index('st.expander("Migration report (structured + PDF)"')
    assert advanced < notebook < proof < migration
    assert "DV.render_proof_first_validation(" in source
    assert "simplified_ui=True" in source
    assert "DV.render_migration_report(" in source
    assert '"decision": run.get("decision")' in source
    assert '"run_id": run.get("run_id")' in source


def test_manual_crosstab_is_a_conditional_fallback():
    source = (ROOT / "deep_validation.py").read_text(encoding="utf-8")
    assert 'st.expander("Manual evidence fallback", expanded=fallback_needed)' in source
    assert "unresolved_tableau = any(" in source
    assert "if enable_manual:" in source
    assert "uploaded_crosstabs = st.file_uploader(" in source


def test_migration_report_uses_governed_decision():
    import deep_validation as validation

    report_run = {
        "ir": {"datasources": [], "dashboards": [], "calcs": {},
               "calc_drops": {}, "params": {}, "hierarchies": {}},
        "parity": {"summary": {"measures_bug": 0, "measures_checked": 2,
                                "measures_pass": 2, "calcs_translated": 0},
                   "datasources": []},
        "app_name": "app.py", "target": "DB.SCHEMA", "workbook": "book.twbx",
        "stem": "book", "ts": "2026-09-11T00:00:00Z", "run_id": "run-123",
        "architecture_version": gov.ARCHITECTURE_VERSION,
        "decision": {"state": "NEEDS_REVIEW"},
    }
    sections = dict(validation._report_sections(report_run, []))
    validation_rows = dict(sections["Validation result"])
    output_rows = dict(sections["Output"])
    assert validation_rows["Governed migration decision"] == "NEEDS_REVIEW"
    assert output_rows["Run ID"] == "run-123"
    assert output_rows["Accelerator version"] == gov.ARCHITECTURE_VERSION

    legacy = dict(validation._report_sections({
        **report_run,
        "decision": None,
        "run_id": None,
        "architecture_version": None,
    }, []))
    legacy_validation = dict(legacy["Validation result"])
    legacy_output = dict(legacy["Output"])
    assert legacy_validation["Verdict"] == "PASS"
    assert "Governed migration decision" not in legacy_validation
    assert "Run ID" not in legacy_output


def test_report_package_copy_is_v2_only():
    import validation_report_dashboard as dashboard_report

    run = {"workbook": "book", "run_id": "run", "environment": "UAT",
           "generated_at": "now"}
    legacy = dashboard_report._evidence(run, {})
    simplified = dashboard_report._evidence(
        run, {"simplified_downloads": True})
    assert "Generated package" in legacy
    assert "validation_summary.json" in legacy
    assert "Audit evidence retained" in simplified
    assert "validation_summary.json" not in simplified


def test_v2_deploy_bundle_matches_validation_sources():
    for filename in ("pipeline_app_ai_v2.py", "deep_validation.py",
                     "validation_report_dashboard.py"):
        assert (ROOT / filename).read_bytes() == (
            ROOT / "tableau_sis_accelerator_v2" / filename).read_bytes()


def test_tableau_download_uses_payload_format_not_catalogue_name():
    live_twb = b'<?xml version="1.0"?><workbook version="2026.1"></workbook>'
    assert tableau_server.normalize_workbook_download(
        "Superstore_KPI_Parameter_Dashboard_Live.twbx", live_twb
    ) == "Superstore_KPI_Parameter_Dashboard_Live.twb"

    packaged = io.BytesIO()
    with zipfile.ZipFile(packaged, "w") as archive:
        archive.writestr("book.twb", live_twb)
    assert tableau_server.normalize_workbook_download(
        "Packaged workbook.twb", packaged.getvalue()
    ) == "Packaged_workbook.twbx"

    try:
        tableau_server.normalize_workbook_download(
            "not-a-workbook", b"<html>Access denied</html>")
    except tableau_server.TableauDownloadError as exc:
        assert "rather than a Tableau <workbook>" in str(exc)
    else:
        raise AssertionError("an HTML response must not reach the workbook parser")
