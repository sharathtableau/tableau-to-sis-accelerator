"""Local evidence for newly accepted sheets; no Tableau parity inference."""
import hashlib
import argparse
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from contextlib import redirect_stdout

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('repo', type=Path)
parser.add_argument('--before', default='panes_corpus_assessment.json')
parser.add_argument('--after', default='temporal_corpus_assessment.json')
args = parser.parse_args()
repo = args.repo
sys.path[:0] = [str(repo), str(repo / 'tests')]
import engine
import findings
from test_v3_field_identity import UI
from v3_local import prepare, preview_context

old = json.loads((repo / args.before).read_text())
new = json.loads((repo / args.after).read_text())
previous = {(r['file'], s['dashboard'], s['sheet']): s['state'] for r in old for s in r['sheets']}
results = []
for record in new:
    gains = [s for s in record['sheets'] if s['state'] == 'UNVERIFIED' and
             previous.get((record['file'], s['dashboard'], s['sheet'])) == 'UNSUPPORTED']
    if not gains: continue
    raw = (repo / record['file']).read_bytes()
    with tempfile.TemporaryDirectory(prefix='v3-temporal-') as folder:
        target = Path(folder) / (hashlib.sha256(raw).hexdigest() + Path(record['file']).suffix)
        target.write_bytes(raw)
        with redirect_stdout(sys.stderr):
            prepared = prepare(target, raw)
        with preview_context(prepared['ir'], prepared['bindings']):
            for gain in gains:
                sheet = next(s for d in prepared['ir']['dashboards'] if d['name'] == gain['dashboard']
                             for s in d['sheets'] if s['name'] == gain['sheet'])
                findings.clear()
                ui = UI()
                with patch.object(engine, 'st', ui): engine.render_sheet(sheet, '')
                blockers = [f for f in findings.all_findings() if f['severity'] == 'BLOCKER']
                results.append({'file': record['file'], 'dashboard': gain['dashboard'], 'sheet': gain['sheet'],
                    'workbook_sha256': hashlib.sha256(raw).hexdigest(), 'charts': len(ui.charts),
                    'status': 'LOCAL_RENDERED' if ui.charts and not blockers and not ui.errors else 'NOT_VERIFIED',
                    'blockers': blockers, 'warnings': ui.errors, 'intake_errors': prepared['intake_errors'],
                    'findings': findings.all_findings(),
                    'missing_datasources': [cap for cap, path in prepared['bindings'].items()
                                            if not path or not Path(path).is_file()],
                    'numeric_parity': 'NOT_EVALUATED', 'visual_parity': 'NOT_EVALUATED'})
print(json.dumps(results, indent=2))
