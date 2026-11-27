"""Fast deploy: a fingerprint of everything the release gates check, so code that already passed them is not checked
again. Usage:
  python release/gate_fingerprint.py check [evidence] exit 0 when this exact code passed every gate before
                                                      (and copy the passing logs into [evidence])
  python release/gate_fingerprint.py record <evidence> after every gate passed: remember this code's fingerprint
  python release/gate_fingerprint.py show             print the fingerprint
The fingerprint covers the content of every code, test, tool, SQL and configuration file (not the evidence reports
or backups), the corpus workbooks (name and size) and the Python version. Change one byte of any of them and the
next release runs every gate again. Nothing here talks to Snowflake."""
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / 'reports' / 'gates_passed.json'
CODE = {'.py', '.yml', '.yaml', '.json', '.sql', '.cmd', '.toml', '.cfg', '.ini', '.txt', '.md'}
# what the gates run: the app's own modules (top level), its tests, tools, release scripts, SQL and worker code;
# the corpus and the local data are taken by name and size. Evidence reports, backups and screenshots are not.
CODE_DIRS = ('tests', 'tools', 'release', 'sql', 'worker', 'metadata', 'cortex_code_skill')
DATA_DIRS = ('Workbooks', 'data')
PRUNE = {'__pycache__', '.git', '.pytest_cache', 'node_modules'}


def _walk(base):
    import os
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE and not d.startswith('.'))
        for f in sorted(filenames):
            yield Path(dirpath) / f


def fingerprint():
    h = hashlib.sha256()
    h.update(('python ' + sys.version.split()[0]).encode())
    files = [p for p in sorted(ROOT.iterdir()) if p.is_file()]
    for d in CODE_DIRS:
        if (ROOT / d).is_dir():
            files += list(_walk(ROOT / d))
    for p in files:
        if p.suffix.lower() in CODE:
            h.update(p.relative_to(ROOT).as_posix().encode() + b'\0')
            h.update(hashlib.sha256(p.read_bytes()).digest())
    for d in DATA_DIRS:
        if (ROOT / d).is_dir():
            for p in _walk(ROOT / d):
                h.update(f'data:{p.relative_to(ROOT).as_posix()}:{p.stat().st_size}'.encode())
    return h.hexdigest()


LOGS = ('regression.log', 'corpus_gate.log', 'xml_coverage.log', 'v3_tests.log', 'gates_summary.txt')


def main(argv, fp=None, also=None):
    cmd = argv[1] if len(argv) > 1 else 'show'
    fp = fp or fingerprint()
    if cmd == 'show':
        print(fp)
        return 0
    if cmd == 'check':
        try:
            rec = json.loads(RECORD.read_text(encoding='utf-8'))
        except Exception:
            print('No gate record yet: every gate runs.')
            return 1
        if rec.get('fingerprint') == fp:
            src = ROOT / rec.get('evidence', '')
            if len(argv) > 2:  # carry the passing logs into the new evidence folder: deploy.py checks them there
                dst = Path(argv[2])
                dst = dst if dst.is_absolute() else ROOT / dst
                if not all((src / n).is_file() for n in LOGS[:4]):
                    print('The passing gate logs are gone: every gate runs.')
                    return 1
                dst.mkdir(parents=True, exist_ok=True)
                if dst.resolve() != src.resolve():
                    for n in LOGS:
                        if (src / n).is_file():
                            (dst / n).write_bytes((src / n).read_bytes())
                (dst / 'gates_reused.txt').write_text(f"Gates skipped: code fingerprint {fp} passed every gate on "
                                                      f"{rec.get('passed_at')} in {rec.get('evidence')}.\n",
                                                      encoding='utf-8')
            print(f"Gates skipped: this exact code passed every gate on {rec.get('passed_at')} ({rec.get('evidence')}).")
            return 0
        print('Code changed since the last passed gates: every gate runs.')
        return 1
    if cmd == 'record':
        RECORD.parent.mkdir(parents=True, exist_ok=True)
        RECORD.write_text(json.dumps({'fingerprint': fp, 'after_gates': also or fp, 'evidence': argv[2] if len(argv) > 2 else '',
                                      'passed_at': time.strftime('%Y-%m-%d %H:%M:%S')}, indent=1), encoding='utf-8')
        print('Gates recorded for fingerprint ' + fp[:12])
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
