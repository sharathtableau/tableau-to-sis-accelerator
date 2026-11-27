"""Gap register (G00): every refused sheet, its blocking reasons grouped into fix classes, and how
many sheets each fix would unlock.

    python tools/gap_register.py reports/<corpus run folder> [--out reports/<folder>]

Reads corpus_sheets.csv written by tools/corpus_runner.py. Read-only: no code paths change. The
classes come from the engine's own refusal reasons (no workbook or sheet names). "Unlocks alone"
counts sheets whose ONLY blocking class is that one, so fixing it moves them out of Refused.
"""
import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (class id, label, proposed route, regex over one refusal fragment). First match wins.
CLASSES = [
    ('calc', 'Calculation not translated', 'Cortex first translator (CF05): allowlisted expression, values checked by code',
     r'^Untranslated field dependencies'),
    ('table_calc', 'Table calculations / dynamic grain', 'Typed query plan in the engine; Cortex proposes the plan from a fixed menu (window, rank, running, percent of total), code compares values',
     r'table calculations|dynamic grain|table-calculation|derived layers|Unresolved, generated'),
    ('data_model', 'Joins / relationships / custom SQL on this renderer path', 'Use the verified relationship model (Semantic View, Cortex-designed model already proven) on every renderer path',
     r'Joins, relationships|Multiple physical source identities|datasource joins|data-model plan|Datasource relationships or filters'),
    ('filters', 'Filters not planned (non-categorical, calculated, context)', 'Typed filter plans in the engine (range, relative date, calculated boolean, context order)',
     r'filter|Calculated filter'),
    ('aliases', 'Member aliases', 'Display plan from the workbook aliases (engine, deterministic)',
     r'member aliases'),
    ('crosstab', 'Crosstab / Measure Names / multi-measure axes', 'Crosstab and multi-measure renderer plans; Cortex chart plan as fallback',
     r'Crosstab|Measure Names|multi-measure axis|Discrete aggregate headers'),
    ('stacking', 'Stacked area/bar with labels', 'Stacked renderer with label plan; Cortex chart plan as fallback',
     r'stacking|authored labels'),
    ('temporal', 'Date axes and temporal encodings', 'Date-aware adapter (calendar axis, date grain on colour/label); Cortex chart plan as fallback',
     r'^Temporal|Date-grain|temporal adapter|calendar-date|discrete-date'),
    ('renderer', 'No renderer for this chart type', 'Cortex chart plan from a fixed menu (bar, line, area, scatter, map, pie, table, KPI, heatmap, gantt); code checks fields and values',
     r'Occurrence adapter for .* is not implemented|renderer contract|requires a separate adapter'),
    ('shelf', 'A shelf the renderer does not use (detail, colour, text, tooltip, size)', 'Renderer consumes or explicitly discloses every encoding; Cortex chart plan maps encodings it cannot draw',
     r'Unconsumed|Unplanned .* occurrence'),
]


def classify(fragment):
    for cid, _label, _route, rx in CLASSES:
        if re.search(rx, fragment, re.I):
            return cid
    return 'other'


def slot_of(fragment):
    m = re.search(r'Unconsumed (?:temporal )?([a-z-]+) occurrence|Unplanned (?:\w+ )?([a-z-]+) occurrence', fragment)
    return (m.group(1) or m.group(2)) if m else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    run = ROOT / args.run
    out = ROOT / (args.out or (args.run.rstrip('/\\') + '_gaps'))
    out.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader((run / 'corpus_sheets.csv').open(encoding='utf-8')))
    refused = [r for r in rows if r['outcome'] == 'Refused']
    labels = {c[0]: c[1] for c in CLASSES} | {'other': 'Other'}
    routes = {c[0]: c[2] for c in CLASSES} | {'other': 'Review'}
    per_sheet, slots = [], Counter()
    for r in refused:
        frags = [f.strip() for f in (r['reason'] or '').split(';') if f.strip()]
        classes = sorted({classify(f) for f in frags})
        for f in frags:
            if classify(f) == 'shelf' and slot_of(f):
                slots[slot_of(f)] += 1
        per_sheet.append(dict(r, classes=classes, fragments=frags))
    affected, alone = Counter(), Counter()
    for s in per_sheet:
        affected.update(s['classes'])
        if len(s['classes']) == 1:
            alone[s['classes'][0]] += 1
    combos = Counter(' + '.join(s['classes']) for s in per_sheet)
    kinds = defaultdict(Counter)
    for s in per_sheet:
        for c in s['classes']:
            kinds[c][s['kind']] += 1
    by_wb = defaultdict(Counter)
    for r in rows:
        by_wb[r['workbook']][r['outcome']] += 1
    with (out / 'gap_register.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['workbook', 'dashboard', 'sheet', 'kind', 'classes', 'blocking reasons'])
        for s in per_sheet:
            w.writerow([s['workbook'], s['dashboard'], s['sheet'], s['kind'], ' + '.join(s['classes']),
                        ' | '.join(s['fragments'])])
    total = len(rows)
    L = [f'# Gap register — {run.name}', '',
         f'{total} sheet occurrences; {len(refused)} refused. Source: engine refusal reasons from the EN07 run '
         '(no workbook or sheet names used). A sheet can have several blocking classes; "unlocks alone" = sheets '
         'whose only blocking class is that one.', '',
         '| Fix class | Sheets affected | Unlocks alone | Chart kinds | Proposed route |', '|---|---|---|---|---|']
    for cid, n in affected.most_common():
        kk = ', '.join(f'{k} {v}' for k, v in kinds[cid].most_common(5))
        L.append(f'| {labels[cid]} | {n} | {alone[cid]} | {kk} | {routes[cid]} |')
    L += ['', 'Shelves the renderers do not use yet: ' + ', '.join(f'{k} {v}' for k, v in slots.most_common()), '',
          '## Most common combinations', '', '| Classes | Sheets |', '|---|---|']
    L += [f'| {k} | {v} |' for k, v in combos.most_common(12)]
    L += ['', '## Per workbook', '', '| Workbook | Sheets | Drawn | Refused | Needs a console run |', '|---|---|---|---|---|']
    for wb, c in sorted(by_wb.items()):
        L.append(f"| {wb.split('/')[-1]} | {sum(c.values())} | {c['Converted'] + c['Converted (0 rows)'] + c['Converted (warning)']} | "
                 f"{c['Refused']} | {c['Needs Snowflake']} |")
    (out / 'gap_register.md').write_text('\n'.join(L) + '\n', encoding='utf-8')
    print('\n'.join(L))


if __name__ == '__main__':
    main()
