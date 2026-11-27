"""EN10: smoke test of the deployed app after every deploy -- the files Snowflake serves (downloaded by verify.py into
deployment/downloaded_live) are started headless, every console page is opened, and no page may raise or show
error text. Writes deployment/smoke.json; exits 1 on any failure. Runs without a Snowflake session (pages that need
one say so with an info message, which is not an error).

Usage: python release/smoke.py <evidence_dir>
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = ['Overview', 'New migration', 'Inventory', 'Preview', 'Validation', 'Cortex AI', 'Deploy', 'Estate catalogue']
ERROR_WORDS = ('Traceback', 'Exception', 'Error:', 'could not render', 'NameError', 'KeyError')

_SCRIPT = r'''
import json, sys
from streamlit.testing.v1 import AppTest
pages, words = json.loads(sys.argv[1]), json.loads(sys.argv[2])
out = []
at = AppTest.from_file(sys.argv[3], default_timeout=90).run()
for page in pages:
    row = {'page': page, 'ok': True, 'problems': []}
    try:
        at.radio(key='v3c_page').set_value(page).run()
        if at.exception:
            row['problems'] += ['exception: ' + str(e.value)[:200] for e in at.exception]
        for e in at.error:
            text = str(e.value)
            if any(w in text for w in words):
                row['problems'].append('error text: ' + text[:200])
    except Exception as exc:
        row['problems'].append('harness: ' + type(exc).__name__ + ': ' + str(exc)[:200])
    row['ok'] = not row['problems']
    out.append(row)
print('SMOKE_JSON=' + json.dumps(out))
'''


def smoke(folder, main='v3_console.py', pages=PAGES, timeout=900):
    """[{page, ok, problems}] for the app in folder, run in a separate process (only folder's modules)."""
    folder = Path(folder)
    entry = next(iter(folder.rglob(main)), None)
    if entry is None:
        return [{'page': p, 'ok': False, 'problems': ['entry file missing: ' + main]} for p in pages]
    env = dict(os.environ, PYTHONPATH=str(entry.parent))
    proc = subprocess.run([sys.executable, '-c', _SCRIPT, json.dumps(pages), json.dumps(ERROR_WORDS), str(entry)],
                          cwd=str(entry.parent), env=env, capture_output=True, text=True, timeout=timeout)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith('SMOKE_JSON=')), None)
    if line is None:
        tail = (proc.stderr or proc.stdout)[-300:]
        return [{'page': p, 'ok': False, 'problems': ['app did not start: ' + tail]} for p in pages]
    return json.loads(line[len('SMOKE_JSON='):])


def main(evidence):
    out = ROOT / evidence / 'deployment'
    rows = smoke(out / 'downloaded_live')
    record = {'pages': len(rows), 'failed': [r['page'] for r in rows if not r['ok']], 'rows': rows}
    (out / 'smoke.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps({'pages': record['pages'], 'failed': record['failed']}))
    return 0 if not record['failed'] else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
