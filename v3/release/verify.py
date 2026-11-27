"""Verify the deployed V3 target: every packaged file hash, configuration and grants unchanged.

Usage: python release/verify.py <evidence_dir>
"""
import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline import snow_session  # noqa: E402

TARGET = 'WBR_DB.TABLEAU_ACCELERATOR_V3.TABLEAU_TO_SIS_V3_CORTEX'   # Cortex lab app
KEYS = ['runtime_name', 'query_warehouse', 'external_access_integrations',
        'external_access_secrets', 'main_file']


def main(evidence):
    out = ROOT / evidence / 'deployment'
    assert json.loads((out / 'deploy_result.json').read_text())['exit_code'] == 0, 'deploy did not succeed'
    with contextlib.redirect_stdout(io.StringIO()):
        session = snow_session('wbr')

    def query(sql):
        return [row.as_dict() for row in session.sql(sql).collect()]

    def save(name, value):
        (out / name).write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')

    desc = query('DESCRIBE STREAMLIT ' + TARGET)
    save('description_after.json', desc)
    grants = query('SHOW GRANTS ON STREAMLIT ' + TARGET)
    save('grants_after.json', grants)
    before = json.loads((out / 'description_before.json').read_text())
    if before is None:      # first deploy of the Cortex lab app: check the intended runtime instead
        config_ok = (desc[0]['runtime_name'] == 'SYSTEM$WAREHOUSE_RUNTIME' and desc[0]['query_warehouse'] == 'POWERHOUSE')
        grants_ok = True    # new object: grants recorded in grants_after.json
    else:
        config_ok = all(desc[0][k] == before[0][k] for k in KEYS)
        grants_ok = json.loads(json.dumps(grants, default=str)) == json.loads((out / 'grants_before.json').read_text())
    folder = out / 'downloaded_live'
    folder.mkdir(exist_ok=True)
    save('download_result.json', [str(x) for x in session.file.get(desc[0]['live_version_location_uri'], str(folder))])
    manifest = json.loads((out / 'candidate' / 'candidate_build.json').read_text())
    mismatches = []
    for name, expected in manifest['files'].items():
        matches = list(folder.rglob(name))
        if len(matches) != 1 or hashlib.sha256(matches[0].read_bytes()).hexdigest() != expected:
            mismatches.append(name)
    record = {'build_sha256': manifest['build_sha256'], 'verified_files': len(manifest['files']),
              'mismatches': mismatches, 'config_unchanged': config_ok, 'grants_unchanged': grants_ok}
    save('verification.json', record)
    session.close()
    assert not mismatches and config_ok and grants_ok, record
    print(json.dumps(record))


if __name__ == '__main__':
    main(sys.argv[1])
