"""Build the workbench candidate from source and deploy it to the existing V3 target.

Requires, in <evidence_dir>: regression.log (ALL REGRESSION TESTS PASSED),
v3_tests.log (unittest OK) and a successful preflight. Deploys only the fixed
target from v3_package.TARGET with the existing connection 'wbr'.
Usage: python release/deploy.py <evidence_dir>
"""
import hashlib
import io
import json
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from v3_package import workbench_bundle  # noqa: E402

SNOW = r'C:\Users\SharathKumarKammari\Scripts\snow.exe'


def main(evidence):
    checks = ROOT / evidence
    out = checks / 'deployment'
    regression = (checks / 'regression.log').read_text(encoding='utf-8-sig', errors='replace')
    assert 'ALL REGRESSION TESTS PASSED' in regression, 'regression.log does not show a pass'
    tests = (checks / 'v3_tests.log').read_text(encoding='utf-8-sig', errors='replace')
    # Judge the unittest summary itself; tests may print after it (stdout is unbuffered differently).
    assert re.search(r'^Ran \d+ tests? in ', tests, re.M), 'v3_tests.log has no unittest summary'
    assert re.search(r'^OK( \(.*\))?\s*$', tests, re.M), 'v3_tests.log does not show OK'
    assert not re.search(r'^FAILED \(', tests, re.M), 'v3_tests.log shows failures'
    assert json.loads((out / 'previous_runtime_verified.json').read_text())['runtime_match'], 'preflight missing'
    data, build = workbench_bundle()
    (out / 'candidate.zip').write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(out / 'candidate')
    manifest = json.loads((out / 'candidate' / 'candidate_build.json').read_text())
    for name, digest in manifest['files'].items():
        assert hashlib.sha256((out / 'candidate' / name).read_bytes()).hexdigest() == digest, name
        if name.endswith('.py'):
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, 'source drift: ' + name
    print('CANDIDATE_READY', build, flush=True)
    cmd = [SNOW, 'streamlit', 'deploy', 'v3_candidate', '--replace', '--connection', 'wbr',
           '--project', str((out / 'candidate').resolve())]
    start = time.monotonic()
    run = subprocess.run(cmd, capture_output=True)
    log = (run.stdout + run.stderr).decode('utf-8', errors='replace')
    log = re.sub(r'https://[^\s]*SAMLRequest[^\s]*', '[authentication URL redacted]', log)
    (out / 'deploy.log').write_text(log, encoding='utf-8')
    record = {'command': cmd, 'exit_code': run.returncode,
              'duration_seconds': round(time.monotonic() - start, 2),
              'build_sha256': manifest['build_sha256'], 'acceptance': 'NOT_VERIFIED'}
    (out / 'deploy_result.json').write_text(json.dumps(record, indent=2))
    print(json.dumps(record))
    raise SystemExit(run.returncode)


if __name__ == '__main__':
    main(sys.argv[1])
