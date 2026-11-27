"""CX7 laptop tool: explain what xml_coverage reports as not applied / UNKNOWN for one or more workbooks.

usage: python -X utf8 tools/cx7_explain.py "<abs workbook>" [...] [--no-cortex] [--out reports/cx7]
Cortex runs through the `wbr` connection profile (you approve the sign-in). xml_coverage stays the source of truth.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))


def main():
    import cx2_safety as S
    import cx7_inventory as I
    import xml_coverage as XC
    ap = argparse.ArgumentParser()
    ap.add_argument('workbooks', nargs='+'); ap.add_argument('--no-cortex', action='store_true')
    ap.add_argument('--out', default=str(ROOT / 'reports' / 'cx7'))
    a = ap.parse_args()
    session = None
    if not a.no_cortex:
        from snowflake.snowpark import Session
        session = Session.builder.config('connection_name', os.environ.get('V3_PREVIEW_CONNECTION', 'wbr')).create()
    budget = S.Budget(S.DEFAULT_BUDGET_TOKENS)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    for wb in a.workbooks:
        p = Path(wb)
        items = I.open_items(XC.scan(p))
        if session is None:
            explained = {'status': 'NOT_RUN', 'rows': []}
        else:
            import v3_ai as A
            run = {}
            explained = I.explain(items, lambda prompt, schema: A.call(session, run, 'Inventory', 'CX7 explain XML',
                                                                     prompt, schema, max_tokens=4000), budget)
        out = Path(a.out) / (p.stem + '_cx7.md')
        out.write_text(I.report_md(items, explained, p.name), encoding='utf-8')
        print(f'{p.name}: {len(items)} open item(s); Cortex {explained["status"]}; report {out}')
    if session is not None:
        session.close()


if __name__ == '__main__':
    main()
