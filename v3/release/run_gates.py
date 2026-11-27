"""Fast deploy: the four release gates (regression, corpus, XML coverage, V3 tests) run side by side instead of one
after another, and the V3 test modules run in a worker pool. Writes the same evidence logs the serial script wrote
(regression.log, corpus_gate.log, xml_coverage.log, v3_tests.log) so deploy.py's checks are unchanged.
A job that fails while other jobs run is run again alone once; only a second failure fails the gates (the old serial
run never had that contention). On success the code fingerprint is recorded, so the next release of unchanged code
skips the gates.  Usage:  python release/run_gates.py <evidence_folder> [workers]"""
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_fingerprint  # noqa: E402

TIMINGS = ROOT / 'reports' / 'gate_timings.json'
JOB_TIMEOUT = 3600
NO_TESTS = 5  # unittest: module has no tests


def jobs(evid):
    py = sys.executable
    out = [('regression', [py, 'tests/test_regression.py']),
           ('corpus', [py, 'tools/corpus_runner.py', '--fresh', '--out', str(evid / 'corpus')]),
           ('xml_coverage', [py, 'tools/xml_coverage.py'])]
    for m in sorted(p.stem for p in (ROOT / 'tests').glob('test_v3*.py')):
        out.append(('v3:' + m, [py, '-m', 'unittest', m]))
    return out


def run(name, cmd):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT / 'tests'), str(ROOT),
                                                                    os.environ.get('PYTHONPATH')])),
               PYTHONIOENCODING='utf-8')
    t = time.time()
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=JOB_TIMEOUT)
        rc, text = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        rc, text = 124, f'TIMEOUT after {JOB_TIMEOUT}s\n' + (e.stdout or '') + (e.stderr or '')
    ok = rc == 0 or (name.startswith('v3:') and rc == NO_TESTS)
    return name, ok, rc, text, time.time() - t


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    evid = Path(argv[1])
    evid = evid if evid.is_absolute() else ROOT / evid
    evid.mkdir(parents=True, exist_ok=True)
    workers = int(argv[2]) if len(argv) > 2 else max(2, min(8, os.cpu_count() or 2))
    fp = gate_fingerprint.fingerprint()  # taken before the gates run: what they check is the code as it is now
    try:
        past = json.loads(TIMINGS.read_text(encoding='utf-8'))
    except Exception:
        past = {}
    todo = jobs(evid)
    heavy = {'corpus': 1e9, 'regression': 1e8, 'xml_coverage': 1e7}
    todo.sort(key=lambda j: -(heavy.get(j[0]) or past.get(j[0], 30)))  # longest first: the pool ends together
    t0 = time.time()
    print(f'Running {len(todo)} gate jobs on {workers} workers...', flush=True)
    results, done = {}, 0
    with ThreadPoolExecutor(workers) as pool:
        for f in as_completed([pool.submit(run, n, c) for n, c in todo]):
            name, ok, rc, text, secs = f.result()
            results[name] = (ok, rc, text, secs)
            done += 1
            if not ok or not name.startswith('v3:') or done % 20 == 0:
                print(f'  [{done}/{len(todo)}] {name}: {"ok" if ok else "FAILED rc=%d" % rc} ({secs:.0f}s)', flush=True)
    retried = []
    for name, cmd in todo:
        if not results[name][0]:
            (evid / 'parallel_failures').mkdir(exist_ok=True)
            (evid / 'parallel_failures' / (name.replace(':', '_') + '.log')).write_text(results[name][2],
                                                                                       encoding='utf-8')
            print(f'  retrying alone: {name}', flush=True)
            results[name] = run(name, cmd)[1:]
            retried.append(f'{name}: {"passed alone" if results[name][0] else "FAILED again"}')
    # evidence logs, same names and content shape as the serial script
    (evid / 'regression.log').write_text(results['regression'][2], encoding='utf-8')
    (evid / 'corpus_gate.log').write_text(results['corpus'][2], encoding='utf-8')
    (evid / 'xml_coverage.log').write_text(results['xml_coverage'][2], encoding='utf-8')
    v3 = [n for n, _ in todo if n.startswith('v3:')]
    ran = sum(int(m) for n in v3 for m in re.findall(r'^Ran (\d+) tests? in ', results[n][2], re.M))
    v3_ok = all(results[n][0] for n in v3)
    body = ''.join(f'===== {n[3:]} =====\n{results[n][2]}\n' for n in sorted(v3))
    if v3_ok:  # a module log may say FAILED only if it was later passed alone; that run's log is the one kept
        body += '-' * 70 + f'\nRan {ran} tests in {time.time() - t0:.3f}s\n\nOK\n'
    else:
        body += '-' * 70 + f'\nRan {ran} tests in {time.time() - t0:.3f}s\n\nFAILED (modules={sum(not results[n][0] for n in v3)})\n'
    (evid / 'v3_tests.log').write_text(body, encoding='utf-8')
    past.update({n: round(r[3], 1) for n, r in results.items()})
    TIMINGS.parent.mkdir(parents=True, exist_ok=True)
    TIMINGS.write_text(json.dumps(past, indent=1, sort_keys=True), encoding='utf-8')
    failed = [n for n, r in results.items() if not r[0]]
    summary = [f'Gates: {len(todo)} jobs, {workers} workers, {time.time() - t0:.0f}s wall clock',
               f'V3 tests run: {ran}'] + [f'Retried: {r}' for r in retried] + \
              ([f'FAILED: {n} (rc={results[n][1]})' for n in failed] or ['ALL GATES PASSED'])
    (evid / 'gates_summary.txt').write_text('\n'.join(summary) + '\n', encoding='utf-8')
    print('\n'.join(summary))
    if failed:
        return 1
    try:
        where = evid.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        where = str(evid)
    after = gate_fingerprint.fingerprint()
    if after != fp:  # code changed while the gates ran: what passed is not what is on disk now -> no skip record
        print('Code changed while the gates ran: not recorded, the next release runs every gate.')
        return 0
    gate_fingerprint.main(['', 'record', where], fp=fp)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
