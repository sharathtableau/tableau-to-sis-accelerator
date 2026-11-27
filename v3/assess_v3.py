"""Read-only assessment: python assess_v3.py workbook.twbx --output assessment.json

With no paths, assesses root/Workbooks corpus files. Never executes source SQL,
loads Snowflake tables, or deploys. Results describe compatibility, not fidelity.
"""
import argparse
import json
from pathlib import Path

from ir_contract import assess_ir
from tableau_parser import build_ir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='*', type=Path)
    parser.add_argument('--output', type=Path, default=Path('v3_corpus_assessment.json'))
    args = parser.parse_args()
    paths = args.paths or sorted({p for folder in (Path('.'), Path('Workbooks'))
                                  for pattern in ('*.twb', '*.twbx') for p in folder.glob(pattern)})
    results = []
    for path in paths:
        try:
            assessment = assess_ir(build_ir(str(path), scoped=True))
            results.append({'file': str(path), **assessment})
        except Exception as exc:
            results.append({'file': str(path), 'state': 'BLOCKED',
                            'error': f'{type(exc).__name__}: {exc}'})
    args.output.write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(f'Assessed {len(results)} files; results: {args.output}')
    print('Structural errors:', sum(bool(r.get('issues') or r.get('error')) for r in results))
    print('Unsupported/invalid sheets:', sum(sum(s['state'] in {'UNSUPPORTED', 'INVALID'}
          for s in r.get('sheets', [])) for r in results))
    print('No data or visual fidelity PASS has been inferred from parsing.')


if __name__ == '__main__':
    main()
