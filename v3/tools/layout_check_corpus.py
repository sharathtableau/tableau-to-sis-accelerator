"""Release 26c: the automatic layout check over every corpus workbook (parse only; no data, no Tableau call)."""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))


def main(argv=None):
    import corpus_runner as CR
    import tableau_parser as T
    import v3_layout_check as L
    from v3_local import published_scope
    out = Path((argv or sys.argv[1:] or ['reports/release26_exact/layout_check'])[0])
    out.mkdir(parents=True, exist_ok=True)
    total, rows_all = Counter(), []
    for path in CR.corpus([]):
        entry = CR.cloud_entry(path)
        try:
            ir = published_scope(T.build_ir(str(path), scoped=True), (entry or {}).get('published_views'))
        except Exception as exc:
            print(f'{path.name}: parse failed {type(exc).__name__}')
            continue
        rows, summary = L.check_ir(ir)
        for r in rows:
            r['Workbook'] = path.name
        rows_all += rows
        total.update({k: v for k, v in summary.items()})
        print(f"{path.name[:48]:48} {summary}")
    (out / 'layout_check.json').write_text(json.dumps(rows_all, indent=1), encoding='utf-8')
    print('TOTAL', dict(total))
    return 0


if __name__ == '__main__':
    sys.exit(main())
