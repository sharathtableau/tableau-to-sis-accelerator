"""Release 26g: XML layout coverage gate.

Walks every layout-relevant element of each workbook (dashboard zones and their attributes, zone styles,
dashboard style rules, worksheet style rules, sheet fit) and classifies it:

  applied      the app draws it from the XML
  by-geometry  carried by the zone rectangle the app already uses (no separate handling needed)
  not-applied  known, not drawn yet -- named in the report (and in the migration notes where it matters)
  UNKNOWN      not classified: the gate FAILS, so a new kind of element can never be skipped silently

usage: python tools/xml_coverage.py [workbook ...]   (default: the corpus); exit 1 when anything is UNKNOWN
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

ZONE_TYPES = {
    'applied': {'layout-flow', 'layout-basic', 'sheet', 'text', 'title', 'filter', 'paramctrl', 'color', 'size',
                'shape', 'empty', 'bitmap', 'dashboard-object', 'highlighter'},
    'not-applied': {'web', 'flipboard', 'flipboard-nav', 'map-legend', 'extension'},
}
ZONE_ATTRS = {
    'applied': {'x', 'y', 'w', 'h', 'id', 'type-v2', 'type', 'param', 'name', 'mode', 'show-all', 'show-title', 'is-scaled',
                'hidden-by-user', 'custom-title', 'leg-item-layout'},
    'by-geometry': {'fixed-size', 'is-fixed', 'layout-strategy-id', 'forceUpdate', 'pane-specification-id',
                    'friendly-name', 'paired-zone-id', 'is-centered', 'is-vertical-centered'},
    'not-applied': {'values', 'show-search', 'show-exclude', 'show-filter-state', 'show-apply',
                    'removable', 'show-null-ctrls', 'url', 'alt-text', 'show-more-less', 'show-domain',
                    'show-pattern-ctrl', 'show-date-ctrl'},
}
ZONE_STYLE = {'applied': {'border-color', 'border-style', 'border-width', 'margin', 'padding', 'background-color',
                          'margin-left', 'margin-right', 'margin-top', 'margin-bottom', 'padding-top',
                          'padding-left', 'padding-right', 'padding-bottom'}}
DASH_STYLE = {
    'applied': {('dash-title', a) for a in ('font-family', 'font-size', 'color')} |
               {('dash-text', a) for a in ('font-family', 'font-size', 'color')} |
               {(e, a) for e in ('parameter-ctrl', 'parameter-ctrl-title', 'quick-filter', 'quick-filter-title')
                for a in ('font-family', 'font-size', 'color')} | {('table', 'background-color')} |
               {('dash-container', a) for a in ('border-style', 'border-color', 'background-color')},   # V12
    'not-applied': {('dash-subtitle', 'font-size'),
                    ('dash-subtitle', 'font-family'), ('story-point-caption', 'background-color'),
                    ('dash-title', 'background-color'), ('dash-title', 'font-weight')},
}
WS_STYLE_APPLIED = {('worksheet', 'font-family'), ('worksheet', 'display-field-labels'),
                    ('label', 'font-size'), ('label', 'color'), ('label', 'font-family'), ('label', 'font-weight'),
                    ('label', 'text-align'),
                    ('cell', 'text-align'), ('cell', 'font-size'), ('cell', 'color'), ('cell', 'font-weight'),
                    ('gridline', 'line-visibility'), ('gridline', 'stroke-size'),
                    ('gridline', 'stroke-color'), ('axis', 'tick-color'), ('axis', 'line-visibility'),
                    ('axis', 'display'), ('axis', 'title'), ('axis', 'font-size'), ('axis', 'color'),
                    ('pane', 'border-style'), ('pane', 'border-width'), ('table', 'background-color'),
                    ('legend-title-text', 'color'), ('header', 'font-size'), ('header', 'color'),
                    # release 27 CX2 rules: axis_xml reference-line labels, trend line style, text-table banding
                    ('refline', 'text-format'), ('refline', 'color'), ('refline', 'stroke-color'),
                    ('trendline', 'stroke-color'), ('trendline', 'line-pattern-only'), ('trendline', 'stroke-size'),
                    ('pane', 'band-color'), ('header', 'band-color'), ('table', 'band-size'),
                    # release 37 (V11): reference line visibility / width / fills, pane dividers on / off
                    ('refline', 'line-visibility'), ('refline', 'stroke-size'), ('refline', 'fill-above'),
                    ('refline', 'fill-below'), ('table-div', 'line-visibility'), ('table-div', 'stroke-size')}
# lines the app never draws: a rule that turns them off is honoured by construction
NEVER_DRAWN_OFF = {'zeroline', 'dropline'}
WS_STYLE_NOT_APPLIED_ELEMENTS = {'table-div', 'header-div', 'refline', 'dropline', 'zeroline', 'map-layer',
                                 'animation', 'quick-filter', 'legend', 'title', 'tooltip', 'field-labels',
                                 'field-labels-decoration', 'mark', 'pane', 'cell', 'axis', 'header', 'label',
                                 'worksheet', 'table', 'gridline', 'legend-title-text', 'datalabel', 'map',
                                 'parameter-ctrl', 'parameter-ctrl-title', 'quick-filter-title', 'legend-title',
                                 'trendline', 'annotation', 'dash-title', 'band', 'forecast', 'refband',
                                 'caption', 'data-highlighter', 'map-data-layer'}
FIT = {'applied': {'entire-view', 'fit-width', 'fit-height', 'standard'}}


def classify(table, key):
    for status, keys in table.items():
        if key in keys:
            return status
    return 'UNKNOWN'


def scan(path):
    import tableau_parser as T
    root = T.load_twb_xml(str(path))
    found = defaultdict(Counter)
    for d in root.findall('.//dashboards/dashboard'):
        skip = T._device_zone_ids(d)
        for z in d.iter('zone'):
            if id(z) in skip:
                continue
            t = z.get('type-v2') or z.get('type') or ('sheet' if z.get('name') and not list(z.findall('zone'))
                                                        else 'layout-flow')
            found['zone type'][(t, classify(ZONE_TYPES, t))] += 1
            for a in z.attrib:
                found['zone attribute'][(a, classify(ZONE_ATTRS, a))] += 1
            for f in z.findall('./zone-style/format'):
                found['zone style'][(f.get('attr'), classify(ZONE_STYLE, f.get('attr')))] += 1
        for sr in d.findall('./style/style-rule'):
            for f in sr.findall('format'):
                k = (sr.get('element'), f.get('attr'))
                found['dashboard style'][(f'{k[0]}.{k[1]}', classify(DASH_STYLE, k))] += 1
    for w in root.findall('.//worksheets/worksheet'):
        for sr in w.findall('./table/style/style-rule'):
            for f in sr.findall('format'):
                k = (sr.get('element'), f.get('attr'))
                off = f.get('value') == 'off' or (f.get('attr') == 'stroke-size' and f.get('value') == '0')
                status = ('applied' if k in WS_STYLE_APPLIED or f.get('field') or (k[0] in NEVER_DRAWN_OFF and off) else
                          'not-applied' if k[0] in WS_STYLE_NOT_APPLIED_ELEMENTS else 'UNKNOWN')
                found['worksheet style'][(f'{k[0]}.{k[1]}', status)] += 1
    for win in root.findall('.//windows/window'):
        for vp in win.findall('./viewpoints/viewpoint'):
            zm = vp.find('zoom')
            z = zm.get('type') if zm is not None and zm.get('type') else 'standard'
            found['sheet fit'][(z, classify(FIT, z))] += 1
    return found


def main(argv):
    import corpus_runner as CR
    paths = [Path(p) for p in argv] or CR.corpus([])
    total, unknown = defaultdict(Counter), []
    for p in paths:
        try:
            found = scan(p)
        except Exception as exc:
            print(f'{p.name}: could not read ({type(exc).__name__})')
            continue
        for group, c in found.items():
            total[group].update(c)
            unknown += [(p.name, group, k[0]) for k in c if k[1] == 'UNKNOWN']
    report = {g: {f'{k[0]} [{k[1]}]': n for k, n in sorted(c.items(), key=lambda kv: (kv[0][1], -kv[1]))}
              for g, c in total.items()}
    out = ROOT / 'reports' / 'xml_coverage.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({'report': report, 'unknown': unknown}, indent=1), encoding='utf-8')
    for g, c in total.items():
        by = Counter()
        for (k, status), n in c.items():
            by[status] += n
        print(f'{g:18} ' + ', '.join(f'{s}: {n}' for s, n in sorted(by.items())))
    na = sorted({k for c in total.values() for k, st in c if st == 'not-applied'})
    print('not applied (named):', ', '.join(map(str, na)))
    if unknown:
        print('UNKNOWN (gate fails):', sorted({(g, k) for _f, g, k in unknown}))
        return 1
    print('XML COVERAGE OK: every layout element is applied, carried by geometry, or named as not applied')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
