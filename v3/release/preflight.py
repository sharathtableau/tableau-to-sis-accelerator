"""Read-only preflight for the existing V3 target.

Records live context, configuration and grants, downloads the live build as a
rollback copy and proves it matches the expected previous build manifest.
Usage: python release/preflight.py <evidence_dir> <previous_candidate_build.json>
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


def main(evidence, previous_manifest):
    out = ROOT / evidence / 'deployment'
    out.mkdir(parents=True, exist_ok=True)
    first = previous_manifest.upper() == 'NEW'
    expected = None if first else json.loads((ROOT / previous_manifest).read_text(encoding='utf-8'))
    with contextlib.redirect_stdout(io.StringIO()):
        session = snow_session('wbr')

    def query(sql):
        return [row.as_dict() for row in session.sql(sql).collect()]

    def save(name, value):
        (out / name).write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')

    save('context_before.json', query('SELECT CURRENT_ACCOUNT() AS ACCOUNT, CURRENT_ROLE() AS ROLE, CURRENT_REGION() AS REGION'))
    if first:
        # First deploy of the Cortex lab app: it must NOT exist yet (never overwrite something unknown).
        exists = query("SHOW STREAMLITS LIKE '" + TARGET.split('.')[-1] + "' IN SCHEMA " + '.'.join(TARGET.split('.')[:2]))
        assert not exists, 'Target already exists; pass the previous candidate_build.json instead of NEW'
        save('description_before.json', None)
        save('grants_before.json', None)
        save('previous_runtime_verified.json', {'previous_build': None, 'first_deploy': True,
                                                'runtime_files': 0, 'runtime_match': True, 'mismatches': []})
        session.close()
        print(json.dumps({'state': 'PREFLIGHT_OK', 'first_deploy': True, 'target': TARGET}))
        return
    desc = query('DESCRIBE STREAMLIT ' + TARGET)
    save('description_before.json', desc)
    save('grants_before.json', query('SHOW GRANTS ON STREAMLIT ' + TARGET))
    assert desc[0]['runtime_name'] == 'SYSTEM$WAREHOUSE_RUNTIME', desc[0]['runtime_name']
    assert desc[0]['query_warehouse'] == 'POWERHOUSE', desc[0]['query_warehouse']
    rollback = out / 'rollback_live'
    rollback.mkdir(exist_ok=True)
    save('rollback_download.json', [str(x) for x in session.file.get(desc[0]['live_version_location_uri'], str(rollback))])
    hashes = {str(p.relative_to(rollback)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in rollback.rglob('*') if p.is_file()}
    save('rollback_hashes.json', hashes)
    mismatches = []
    for name, digest in expected['files'].items():
        matches = list(rollback.rglob(name))
        if len(matches) != 1 or hashlib.sha256(matches[0].read_bytes()).hexdigest() != digest:
            mismatches.append(name)
    save('previous_runtime_verified.json', {'previous_build': expected['build_sha256'],
                                            'runtime_files': len(expected['files']),
                                            'runtime_match': not mismatches, 'mismatches': mismatches})
    session.close()
    assert not mismatches, mismatches
    print(json.dumps({'state': 'PREFLIGHT_OK', 'previous_build': expected['build_sha256'],
                      'backup_files': len(hashes)}))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
