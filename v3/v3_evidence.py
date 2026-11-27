"""Sheet-level validation, Tableau-vs-app visual comparison and the migration report.

Reuses the original accelerator's evidence machinery unchanged:
- headless_render._capture_all / render_dashboard_to_png: the app's OWN chart objects
  rendered without a browser (never a screenshot of the SSO-gated app);
- tableau_server.pull_all_view_images: Tableau's own REST-rendered view images;
- parity.vision_validate_dashboard: Cortex vision describes each image independently,
  then a text comparison judges them (UNKNOWN, never a silent pass).

Verdict rules: a sheet "renders" only if the app produced output for it on the bound
data; Tableau value agreement comes only from v3_reference; the Cortex visual
judgement is advisory and never changes another status. Nothing here writes data;
the visual comparison stages two PNGs in a dedicated V3 stage.
"""
import base64
import contextlib
import datetime
import html
import json

import os

VISION_STAGE = 'WBR_DB.TABLEAU_ACCELERATOR_V3.V3_VISION_STAGE'
# The original accelerator's live-proven vision model (AI_COMPLETE + PROMPT/TO_FILE).
VISION_MODEL = os.environ.get('V3_VISION_MODEL', 'claude-opus-5')


@contextlib.contextmanager
def offscreen():
    """Headless capture still creates a few real widgets (e.g. chart-selection
    buttons); draw them into a placeholder that is cleared afterwards so they
    never appear on the Validation page."""
    try:
        import streamlit as st
        slot = st.empty()
    except Exception:
        yield
        return
    try:
        with slot.container():
            yield
    finally:
        slot.empty()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def _native(sheet):
    return dict(sheet, _presentation='native')


def _rows_in(cap):
    import validation_adapter as V
    counts = []
    for table in cap.get('tables') or []:
        try:
            counts.append(len(table))
        except TypeError:
            pass
    for chart in cap.get('altair') or []:
        try:
            counts.extend(len(df) for df in V._candidate_dataframes(chart))
        except Exception:
            pass
    return max(counts) if counts else None


def _fmt(v):
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip('0').rstrip('.')
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    return '' if v is None else str(v)


def _tooltip_rows(chart):
    """Rows of a chart's own data, named by the titles its tooltip shows (Tableau captions)."""
    data = getattr(chart, 'data', None)
    if not hasattr(data, 'to_dict') or not len(data):
        return []
    spec = chart.to_dict()
    encs = [spec.get('encoding') or {}] + [(l.get('encoding') or {}) for l in spec.get('layer') or []]
    tips = next((e['tooltip'] for e in encs if isinstance(e.get('tooltip'), list) and e['tooltip']), None)
    if not tips:
        return []
    names = {t['field']: t.get('title') or t['field'] for t in tips if isinstance(t, dict) and t.get('field') in data.columns}
    if len(names) < 2:
        return []
    frame = data[list(names)].rename(columns=names)
    return [{str(k): _fmt(v.item() if hasattr(v, 'item') else v) for k, v in r.items()} for r in frame.to_dict('records')]


def _app_values(sheet, cap, limit=60):
    """The app's own displayed values for a sheet, with Tableau captions as column names."""
    import validation_adapter as V
    for chart in cap.get('altair') or []:
        try:
            dims, _m, rename = V.resolve_chart_columns(_native(sheet), chart)
            if not isinstance(rename, dict) or not rename:
                continue
            rows = V.streamlit_rows(chart, rename) or []
        except Exception:
            continue
        if rows:
            first = [c for c in (dims or []) if c in rows[0]]
            order = first + [c for c in rows[0] if c not in first]
            return [{k: _fmt(r.get(k)) for k in order} for r in rows[:limit]], len(rows), \
                [{k: _fmt(r.get(k)) for k in order} for r in rows[:5000]]
    for chart in cap.get('altair') or []:     # e.g. scatter keyed by a detail field: use tooltip titles
        rows = _tooltip_rows(chart)
        if rows:
            return rows[:limit], len(rows), rows[:5000]
    for table in cap.get('tables') or []:
        if hasattr(table, 'head') and len(table):
            frame = table.head(limit)
            conv = lambda f: [{str(k): _fmt(v.item() if hasattr(v, 'item') else v) for k, v in r.items()}
                              for r in f.to_dict('records')]
            return conv(frame), len(table), conv(table.head(5000))
    if cap.get('kpis'):
        kpis = [{'KPI': str(k), 'Value': _fmt(v)} for k, v in cap['kpis']]
        return kpis[:limit], len(kpis), kpis
    return [], 0, []


def _output_label(cap):
    parts = []
    for key, label in (('altair', 'chart'), ('tables', 'table'), ('kpis', 'KPI tile'), ('plotly', 'map/figure')):
        n = len(cap.get(key) or [])
        if n:
            parts.append(f"{n} {label}{'s' if n > 1 else ''}")
    return ', '.join(parts) or 'no captured output'


class _ShownMessages:
    """What a sheet showed the user while it was drawn (st.error / st.exception / st.warning)."""

    def __enter__(self):
        import streamlit as st
        self.said, self.saved = {'error': [], 'warning': []}, {}
        for name, kind in (('error', 'error'), ('exception', 'error'), ('warning', 'warning')):
            real = getattr(st, name, None)
            if real is None:
                continue
            self.saved[name] = real

            def spy(*a, _real=real, _kind=kind, **k):
                self.said[_kind].append(str(a[0])[:300] if a else '')
                return _real(*a, **k)
            setattr(st, name, spy)
        return self.said

    def __exit__(self, *exc):
        import streamlit as st
        for name, real in self.saved.items():
            setattr(st, name, real)
        return False


def render_status(error, produced, rows, said):
    """(Renders on data, note) for one drawn sheet. ONE rule for the console, the report and the EN07
    gate: an exception, an error shown to the user, or a sheet-level warning shown INSTEAD of the content
    is an Error; a filter that could not be applied is listed but the sheet still counts as drawn."""
    errors = [e for e in (said or {}).get('error', []) if e]
    warnings = [w for w in (said or {}).get('warning', []) if w]
    sheet_warnings = [w for w in warnings if not w.startswith('Filter ')]
    filter_warnings = [w for w in warnings if w.startswith('Filter ')]
    if error:
        return 'Error', str(error)[:200]
    if errors:
        return 'Error', 'The sheet showed an error: ' + errors[0][:180]
    if sheet_warnings and not produced:
        return 'Error', 'The sheet showed a warning instead of its content: ' + sheet_warnings[0][:160]
    if filter_warnings and produced:
        return 'Yes (filter warning)', filter_warnings[0][:200]
    if produced and rows == 0:
        return 'Yes (0 rows)', 'Rendered with no rows in the default state; check against Tableau'
    if produced:
        return 'Yes', sheet_warnings[0][:200] if sheet_warnings else ''
    return 'Yes (text/HTML only)', 'Rendered as text/HTML; not captured for row counts'


def sheet_checks(run, snowflake):
    """Render every sheet headlessly on the bound data (default state) and record what it produced."""
    import engine
    import headless_render as H
    import parameter_actions
    from v3_local import preview_context
    result = run['result']
    ir = result['ir']
    states = {(r.get('dashboard'), r.get('sheet')): r for r in result['assessment']['sheets']}
    rows = []
    run['sheet_values'] = {}
    with preview_context(ir, run['bindings'], snowflake=snowflake), offscreen():
        parameter_actions.reset(parameter_actions.context(engine))
        for dash in ir.get('dashboards', []):
            for sheet in dash.get('sheets', []):
                a = states.get((dash.get('name'), sheet.get('name')), {})
                row = {'Dashboard': dash.get('name'), 'Sheet': sheet.get('name'),
                       'Kind': a.get('kind') or sheet.get('type') or '—', 'Engine status': a.get('state', '—'),
                       'Renders on data': '—', 'Output': '—', 'Rows': None,
                       'Tableau values': '—', 'Visual (Cortex)': '—', 'Note': ''}
                if a.get('state') in ('UNSUPPORTED', 'INVALID'):
                    row.update({'Renders on data': 'Refused', 'Note': (a.get('reason') or '')[:200]})
                    rows.append(row)
                    continue
                with _ShownMessages() as said:
                    cap = H._capture_all(_native(sheet), None, dash.get('name'))
                produced = any(cap.get(k) for k in ('altair', 'tables', 'kpis', 'plotly'))
                try:
                    values, total, every = _app_values(sheet, cap)
                except Exception:
                    values, total, every = [], 0, []
                run.setdefault('sheet_values', {})[(dash.get('name'), sheet.get('name'))] = {
                    'rows': values, 'total': total, 'all': every}
                row['Output'] = _output_label(cap)
                row['Rows'] = _rows_in(cap)
                status, note = render_status(cap.get('error'), produced, row['Rows'], said)
                row.update({'Renders on data': status, 'Note': note})
                if said['error'] or said['warning']:
                    row['Messages'] = ' | '.join((said['error'] + said['warning'])[:3])[:400]
                rows.append(row)
    run['sheet_checks'] = rows
    run['sheet_checks_at'] = _now()
    merge_statuses(run)
    return rows


def merge_statuses(run):
    """Attach Tableau value and visual results (computed elsewhere) to the sheet rows."""
    rows = run.get('sheet_checks') or []
    ref = run.get('reference')
    by_sheet = {}
    published = set()
    if ref:
        for r in ref['rows']:
            if r.get('Sheet') and r['Sheet'] != '—':
                by_sheet[r['Sheet']] = r['Status']
            published.add(str(r.get('View')))
    visual = run.get('visual') or {}
    captured = (run.get('sheet_reference') or {}).get('results') or {}
    for row in rows:
        if row['Sheet'] in captured:
            summary = captured[row['Sheet']][0]
            counts = ''
            if summary.get('Matched values') or summary.get('Mismatched values'):
                counts = f" ({summary.get('Matched values', 0)}/{summary.get('Matched values', 0) + summary.get('Mismatched values', 0)})"
            row['Tableau values'] = summary['Status'] + counts
        elif ref:
            if row['Sheet'] in by_sheet:
                row['Tableau values'] = by_sheet[row['Sheet']]
            elif row['Dashboard'] in published or row['Sheet'] in published:
                row['Tableau values'] = 'Not in published view data'
            else:
                row['Tableau values'] = 'View not published'
        v = visual.get(row['Dashboard'])
        if v:
            row['Visual (Cortex)'] = VERDICT_LABEL.get(v.get('verdict'), v.get('verdict') or '—')
    return rows


VERDICT_LABEL = {'PASS': 'Consistent', 'BUG': 'Differences described', 'UNKNOWN': 'Undetermined',
                 None: 'Images only'}


def app_dashboard_png(run, dash, snowflake):
    import engine
    import headless_render as H
    import parameter_actions
    from v3_local import preview_context
    native = dict(dash, sheets=[_native(s) for s in dash.get('sheets', [])])

    def render(hidden):
        with preview_context(run['result']['ir'], run['bindings'], snowflake=snowflake), \
                (offscreen() if hidden else contextlib.nullcontext()):
            parameter_actions.reset(parameter_actions.context(engine))
            # Same default state as the sheet checks (whose values are compared with Tableau):
            # no dashboard filter clause. Rebuilding the filter widgets headlessly on Snowflake
            # produced a clause that matched no rows (blank app image, 2026-10-04).
            return H.render_dashboard_to_png(native, where_parts=[])
    png, notes = render(True)
    if png is None:                      # never lose the image to the hidden-placeholder path
        png, notes = render(False)
    return png, notes


def visual_compare(run, session, snowflake, use_cortex=True, model=None):
    """Pull Tableau view images, render the app dashboards, and (optionally) ask Cortex vision."""
    import tableau_server as TS
    import v3_ai
    from v3_reference import _sheets_for_view
    result = run['result']
    model = model or VISION_MODEL
    views = TS.pull_all_view_images(result['cloud_server_url'], result['cloud_site_content_url'],
                                    result['cloud_workbook_id'])
    out = {}
    for view in views:
        dash, _ = _sheets_for_view(result['ir'], view.get('view'))
        name = dash.get('name') if dash else str(view.get('view'))
        rec = {'dashboard': name, 'view': view.get('view'), 'tableau_png': view.get('png'),
               'app_png': None, 'verdict': None, 'explanation': None, 'omitted': [],
               'tableau_description': None, 'app_description': None, 'error': None, 'at': _now()}
        if view.get('error') or not view.get('png'):
            rec['error'] = 'Tableau image unavailable: ' + (view.get('error') or 'no image')[:300]
        if dash is None:
            rec['error'] = (rec['error'] or '') + ' No dashboard in this workbook matches the view name.'
            out[name] = rec
            continue
        try:
            png, notes = app_dashboard_png(run, dash, snowflake)
            rec['app_png'] = png
            rec['notes'] = notes
            rec['omitted'] = [n.get('sheet') for n in notes if not n.get('rendered')]
        except Exception as exc:
            rec['error'] = (rec['error'] or '') + f' App render failed: {type(exc).__name__}: {exc}'
            notes = []
        if use_cortex and session is not None and rec['tableau_png'] and rec['app_png']:
            import parity
            try:
                v = parity.vision_validate_dashboard(session, VISION_STAGE, dash.get('title') or name,
                                                     rec['tableau_png'], rec['app_png'], model=model,
                                                     app_render_notes=notes)
                v3_ai.note_vision(run, name, v, model, images=(rec['tableau_png'], rec['app_png']))
                rec.update(verdict=v.get('verdict'), explanation=v.get('explanation'),
                           tableau_description=v.get('tableau_description'),
                           app_description=v.get('app_description'))
                if v.get('errors'):
                    rec['error'] = '; '.join(v['errors'])[:400]
            except Exception as exc:
                v3_ai.record(run, 'Validation', f'Visual comparison — {name}', model, 'Failed',
                             note=type(exc).__name__)
                rec['verdict'] = 'UNKNOWN'
                rec['error'] = f'Cortex vision unavailable: {type(exc).__name__}: {str(exc)[:200]}'
        out[name] = rec
    run['visual'] = out
    merge_statuses(run)
    return out


# --------------------------------------------------------------------------- #
# Migration report (self-contained HTML; print to PDF from the browser)
# --------------------------------------------------------------------------- #
def _table(rows, cols=None, limit=200):
    rows = list(rows or [])
    if not rows:
        return '<p class="muted">None.</p>'
    if not cols:
        cols = []
        for r in rows:                      # union of keys, in first-seen order
            cols += [k for k in r.keys() if k not in cols]
    head = ''.join(f'<th>{html.escape(str(c))}</th>' for c in cols)
    body = ''.join('<tr>' + ''.join(f'<td>{html.escape("" if r.get(c) is None else str(r.get(c)))}</td>'
                                    for c in cols) + '</tr>' for r in rows[:limit])
    more = f'<p class="muted">{len(rows) - limit} more rows not shown.</p>' if len(rows) > limit else ''
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{more}'


def _img(png):
    if not png:
        return '<p class="muted">Not available.</p>'
    return f'<img src="data:image/png;base64,{base64.b64encode(png).decode()}"/>'


def _list(items):
    return '<ul>' + ''.join(f'<li>{html.escape(str(i))}</li>' for i in items or []) + '</ul>'


_KIND = {'kpi': 'KPI tile', 'dtbar': 'Bar chart over time', 'bar': 'Bar chart', 'line': 'Line chart',
         'area': 'Area chart', 'pie': 'Pie chart', 'scatter': 'Scatter plot', 'dual': 'Combined chart',
         'table': 'Table', 'text': 'Text table', 'map': 'Map'}

_CSS = """body{font-family:Segoe UI,Arial,sans-serif;color:#1f2933;margin:0;background:#f5f7fa}
.wrap{max-width:1100px;margin:0 auto;padding:32px 28px 48px}
h1{color:#0b3d5c;margin:0 0 4px;font-size:26px}.wb{font-size:15px;color:#3e4c59;word-break:break-word;margin:0 0 6px}
h2{color:#0b3d5c;font-size:19px;margin:34px 0 10px}h3{color:#0b3d5c;font-size:15px;margin:18px 0 8px}
.muted{color:#62717f;font-size:12px}.card{background:#fff;border:1px solid #d9e2ec;border-radius:10px;padding:16px 18px}
.verdict{border-radius:10px;padding:18px 20px;margin:18px 0;color:#fff}.verdict b{font-size:20px;display:block;margin-bottom:4px}
.ok{background:#0f7b5f}.warn{background:#b7791f}.none{background:#52606d}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0}
.tile{background:#fff;border:1px solid #d9e2ec;border-radius:10px;padding:14px 16px}
.tile span{display:block;font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:#62717f}
.tile b{display:block;font-size:24px;color:#0b3d5c;margin-top:4px}.tile i{font-style:normal;font-size:12px;color:#62717f}
ul.att{margin:6px 0 0;padding-left:20px}ul.att li{margin:6px 0}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff;margin:6px 0}
th,td{border-bottom:1px solid #e4e7eb;padding:7px 9px;text-align:left;vertical-align:top}th{background:#eef4f8;color:#243b53;font-weight:600}
td.num,th.num{text-align:right}.good{color:#0f7b5f;font-weight:600}.bad{color:#b42318;font-weight:600}.na{color:#62717f}
img{max-width:100%;border:1px solid #d9e2ec;border-radius:6px;background:#fff}.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.ai{border-left:4px solid #8e5cf7}details{background:#fff;border:1px solid #d9e2ec;border-radius:10px;margin:10px 0;padding:10px 16px}
summary{cursor:pointer;font-weight:600;color:#0b3d5c}details table{font-size:12px}
@media print{body{background:#fff}details{border:none;padding:0}details>*{display:block}}"""


def _sheet_results(run, checks):
    """One plain-language row per sheet, problems first."""
    sref = ((run.get('sheet_reference') or {}).get('results') or {})
    rows = []
    for c in checks or []:
        name = c.get('Sheet')
        s = (sref.get(name) or [None])[0]
        if s:
            ok, bad = int(s.get('Matched values') or 0), int(s.get('Mismatched values') or 0)
            missing = int(s.get('Rows missing in app') or 0)
            total = ok + bad
            status = str(s.get('Status') or '')
            if status.startswith('Match') and not missing and total:
                match, cls, rank = f'✓ Matches ({ok} of {total} values)', 'good', 2
            elif status.startswith('Difference') or bad or missing:
                match, cls, rank = f'✗ Differs ({ok} of {total} values match)', 'bad', 0
            else:
                match, cls, rank = 'Not comparable', 'na', 1
        else:
            tv = str(c.get('Tableau values') or '—')
            if tv.startswith('Match'):
                match, cls, rank = '✓ ' + tv, 'good', 2
            elif tv.startswith('Difference'):
                match, cls, rank = '✗ ' + tv, 'bad', 0
            else:
                match, cls, rank = tv if tv != '—' else 'Not compared', 'na', 1
        renders = str(c.get('Renders on data') or '')
        if renders == 'Error':
            rank = 0
        out = str(c.get('Output') or '')
        kind = _KIND.get(str(c.get('Kind') or '').lower())
        if 'no captured output' in out:
            kind = 'Text / label'
        elif not kind or 'table' in out:
            kind = 'Table' if 'table' in out else 'KPI tile' if 'KPI' in out else (kind or str(c.get('Kind') or ''))
        rows.append({'rank': rank, 'Sheet': name, 'Type': kind, 'match': match, 'cls': cls,
                     'Visual check': c.get('Visual (Cortex)') or '—',
                     'Note': c.get('Note') or ('Did not render: ' + renders if renders == 'Error' else '')})
    rows.sort(key=lambda r: r['rank'])
    return rows


def _headline(run, rows):
    compared = [r for r in rows if r['cls'] in ('good', 'bad')]
    bad = [r for r in rows if r['cls'] == 'bad' or r['rank'] == 0]
    if run.get('failed'):
        return 'warn', 'Migration did not complete', run['failed']
    if not compared:
        return 'none', 'Migrated — not yet compared with Tableau', 'Run the sheet and value checks on the Validation page.'
    good = len(compared) - len([r for r in compared if r['cls'] == 'bad'])
    if bad:
        return ('warn', f'{good} of {len(compared)} compared sheets match Tableau',
                (f'{len(bad)} sheet needs' if len(bad) == 1 else f'{len(bad)} sheets need') + ' review before sign-off — see “What needs attention”.')
    return 'ok', f'All {len(compared)} compared sheets match Tableau', 'Ready for review and approval.'


def _attention(run, rows, ir, result):
    items = []
    sref = ((run.get('sheet_reference') or {}).get('results') or {})
    for r in rows:
        if r['cls'] == 'bad':
            note = (sref.get(r['Sheet']) or [{}])[0].get('Note') or ''
            items.append(f'<b>{html.escape(str(r["Sheet"]))}</b>: {html.escape(r["match"][2:])}'
                         + (f' <span class="muted">({html.escape(note)})</span>' if note else ''))
        elif r['rank'] == 0:
            items.append(f'<b>{html.escape(str(r["Sheet"]))}</b>: did not render on the Snowflake data.')
    na = [r['Sheet'] for r in rows if r['cls'] == 'na']
    if na:
        items.append('For information — no values to compare (text or label sheets): ' + html.escape(', '.join(map(str, na))))
    for name, v in (run.get('visual') or {}).items():
        if v.get('verdict') not in ('PASS', None):
            items.append(f'Visual check for <b>{html.escape(str(name))}</b>: '
                         + html.escape(VERDICT_LABEL.get(v.get('verdict'), str(v.get('verdict')))))
    untranslated = result['assessment'].get('untranslated_calculations')
    if untranslated:
        items.append('Calculations not translated (refused, never substituted): ' + html.escape(', '.join(map(str, untranslated))))
    return items


def _usage_rows(ai_rows):
    out = []
    for r in ai_rows or []:
        if r.get('Input tokens') is None and not r.get('Messages'):
            continue
        analyst = bool(r.get('Messages'))
        out.append({'Step': r.get('Step'), 'What Cortex did': r.get('Purpose'), 'Model': r.get('Model'),
                    'Tokens in': '—' if analyst else f'{int(r.get("Input tokens") or 0):,}',
                    'Tokens out': '—' if analyst else f'{int(r.get("Output tokens") or 0):,}',
                    'Cost (USD)': f'${float(r.get("Cost USD") or 0):.3f}',
                    'Source': r.get('Token source') or ''})
    return out


def _ptable(rows, cols, num=()):
    if not rows:
        return '<p class="muted">None.</p>'
    head = ''.join(f'<th class="{"num" if c in num else ""}">{html.escape(c)}</th>' for c in cols)
    body = ''.join('<tr>' + ''.join(f'<td class="{"num" if c in num else ""}">{html.escape(str(r.get(c, "")))}</td>'
                                    for c in cols) + '</tr>' for r in rows)
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def report_html(run, checks, engine_version, ai_rows, summary=None):
    """Readable migration report: answer first, then sheets, visuals and AI cost; full evidence
    is kept in collapsed reviewer sections. Content and verdict rules are unchanged."""
    import v3_usage
    result = run['result']
    ir = result['ir']
    ref = run.get('reference')
    e = html.escape
    rows = _sheet_results(run, checks)
    tone, head, sub = _headline(run, rows)
    t = v3_usage.totals(ai_rows)
    states = run.get('states') or {}
    sv = next((b for b in run.get('binding_rows') or [] if 'Semantic View' in str(b.get('Binding', ''))), None)
    parts = [f"""<!doctype html><html><head><meta charset="utf-8"><title>Migration report — {e(run['name'])}</title>
<style>{_CSS}</style></head><body><div class="wrap">
<h1>Migration report</h1><p class="wb">{e(run['name'])}</p>
<p class="muted">Tableau → Streamlit in Snowflake · source: {e(run.get('origin') or '')} · generated {_now()}</p>
<div class="verdict {tone}"><b>{e(head)}</b>{e(sub)}<div style="font-size:12px;opacity:.9;margin-top:6px">Status: {e(_overall(run, checks))}</div></div>"""]
    compared = [r for r in rows if r['cls'] in ('good', 'bad')]
    nbad = len([r for r in rows if r['cls'] == 'bad'])
    vis = list((run.get('visual') or {}).values())
    vis_label = VERDICT_LABEL.get(vis[0].get('verdict'), '—') if vis else 'Not run'
    parts.append('<div class="tiles">'
                 f'<div class="tile"><span>Sheets matching Tableau</span><b>{len(compared) - nbad} / {len(compared)}</b>'
                 f'<i>{len(rows)} sheets migrated</i></div>'
                 f'<div class="tile"><span>Need review</span><b>{nbad}</b><i>value differences</i></div>'
                 f'<div class="tile"><span>Visual check (Cortex)</span><b style="font-size:18px">{e(vis_label)}</b>'
                 '<i>advisory, never changes a status</i></div>'
                 f'<div class="tile"><span>AI cost of this run</span><b>${t["usd"]:.2f}</b>'
                 f'<i>{t["input"] + t["output"]:,} tokens'
                 + (f' · Ask the data ${t["analyst_usd"]:.2f}' if t['analyst_messages'] else '') + '</i></div></div>')
    att = _attention(run, rows, ir, result)
    if run.get('applied_fixes'):
        parts.append('<div class="card"><b>Fixes applied after a Cortex cause proof</b><ul>' + ''.join(
            f"<li>{e(f['Sheet'])}: {e(f['Fix'])}. {e(f['Proof'])}. Proposed by Cortex, proven by code against "
            f"Tableau's values, approved by a reviewer ({e(f['At'])}).</li>" for f in run['applied_fixes'].values())
            + '</ul></div>')
    parts.append('<div class="card"><b>What needs attention</b>' + (
        '<ul class="att">' + ''.join(f'<li>{i}</li>' for i in att) + '</ul>' if att
        else '<p class="muted">Nothing — every compared sheet matches Tableau.</p>') + '</div>')
    parts.append('<div class="card" style="margin-top:12px"><b>How it was migrated</b><p class="muted" style="font-size:13px">'
                 + e(' · '.join(f'{k}: {v}' for k, v in states.items()))
                 + ('<br>Data model: ' + e(str(sv.get('Snowflake objects'))) + ' — ' + e(str(sv.get('Status') or '')) if sv else '')
                 + '</p></div>')
    if summary:
        parts.append('<h2>Executive summary <span class="muted">(Cortex, grounded in this report)</span></h2>'
                     f'<div class="card ai"><p>{e(summary.get("summary", ""))}</p>'
                     f'<b>Verified</b>{_list(summary.get("verified"))}<b>Open risks</b>{_list(summary.get("open_risks"))}'
                     f'<b>Next steps</b>{_list(summary.get("next_steps"))}')
        flagged = summary.get('ungrounded_numbers') or []
        parts.append('<p class="muted">Grounding check: ' + (
            'every number cited appears in the evidence.' if not flagged else
            'numbers not found in the evidence — treat with care: ' + e(', '.join(flagged))) + '</p></div>')
    try:                                   # AC01 (release 30): one acceptance verdict per dashboard, escaped
        import v3_acceptance
        parts.append(v3_acceptance.report_section(run))
    except Exception:
        pass
    try:                                   # CX6 (release 29): Cortex review of the run, advisory, escaped
        import cx6_review
        parts.append(cx6_review.report_section(run))
    except Exception:
        pass
    # ---- sheets ----
    parts.append('<h2>Sheet-level validation</h2>')
    if checks:
        parts.append('<p class="muted">Each sheet’s values compared with Tableau’s own values (default state; tolerance: '
                     'half a unit at the precision Tableau displays). Problems are listed first.</p><table><thead><tr>'
                     '<th>Sheet</th><th>Type</th><th>Matches Tableau</th><th>Visual check</th><th>Note</th></tr></thead><tbody>'
                     + ''.join(f'<tr><td>{e(str(r["Sheet"]))}</td><td>{e(str(r["Type"]))}</td>'
                               f'<td class="{r["cls"]}">{e(r["match"])}</td><td>{e(str(r["Visual check"]))}</td>'
                               f'<td class="muted">{e(str(r["Note"]))}</td></tr>' for r in rows)
                     + '</tbody></table>')
    else:
        parts.append('<p class="muted">Sheet checks were not run.</p>')
    # ---- visuals ----
    parts.append('<h2>Visual comparison</h2>')
    for name, v in (run.get('visual') or {}).items():
        parts.append(f'<h3>{e(name)} — {e(VERDICT_LABEL.get(v.get("verdict"), str(v.get("verdict"))))}</h3>'
                     f'<div class="pair"><div><p class="muted">Tableau</p>{_img(v.get("tableau_png"))}</div>'
                     f'<div><p class="muted">Migrated app (default state)</p>{_img(v.get("app_png"))}</div></div>')
        if v.get('explanation'):
            parts.append(f'<div class="card ai" style="margin-top:10px"><b>Cortex vision (advisory):</b> {e(v["explanation"])}</div>')
        if v.get('omitted'):
            parts.append('<p class="muted">Not drawn in the static image (still in the app): ' + e(', '.join(map(str, v['omitted']))) + '</p>')
        if v.get('error'):
            parts.append('<p class="muted">' + e(v['error']) + '</p>')
    if not run.get('visual'):
        parts.append('<p class="muted">Not run.</p>')
    # ---- AI cost ----
    parts.append('<h2>Cortex activity, tokens and cost</h2>')
    if t['calls'] or t['analyst_messages']:
        parts.append(f'<p><b>{t["calls"]} Cortex calls · {t["input"]:,} tokens in · {t["output"]:,} tokens out · '
                     f'{t["credits"]:.4f} AI credits · about ${t["usd"]:.2f}</b>'
                     + (f'<br>Ask the data (Cortex Analyst): {t["analyst_messages"]} question(s), about ${t["analyst_usd"]:.2f} '
                        '(billed per message, not tokens)' if t['analyst_messages'] else '') + '</p>'
                     + _ptable(_usage_rows(ai_rows), ['Step', 'What Cortex did', 'Model', 'Tokens in', 'Tokens out',
                                                      'Cost (USD)', 'Source'], num=('Tokens in', 'Tokens out', 'Cost (USD)'))
                     + '<p><b>Cost per stage</b></p>'
                     + _ptable([dict(r, **{'AI credits': f"{r['AI credits']:.4f}", 'Cost USD': f"${r['Cost USD']:.3f}"})
                                for r in v3_usage.stage_totals(ai_rows)],
                               ['Stage', 'Calls', 'Tokens in', 'Tokens out', 'AI credits', 'Cost USD', 'Models',
                                'Stopped by ceiling'], num=('Calls', 'Tokens in', 'Tokens out', 'AI credits', 'Cost USD'))
                     + '<p class="muted">' + e(v3_usage.RATE_NOTE)
                     + (f' {t["estimated_calls"]} image call(s) are estimates: Snowflake does not report image tokens for that call.'
                        if t['estimated_calls'] else '') + '</p>')
    else:
        parts.append('<p class="muted">No Cortex calls in this run.</p>')
    import v3_scorecard
    score = v3_scorecard.scorecard(run)
    if score:
        cols = ['Stage', 'Decision', 'Cortex proposed', 'Passed code check', 'Rejected by code', 'Awaiting evidence',
                'Pass rate']
        parts.append('<p><b>Cortex contribution scorecard</b></p>'
                     + _ptable(score + [dict(v3_scorecard.totals(score), Stage='All', Decision='Total')], cols,
                               num=tuple(cols[2:]))
                     + '<p class="muted">Counted from the stored results; Cortex proposes, code checks, Tableau '
                       'values accept. Never changes a verdict.</p>')
    review = ir.get('ai_migration_review')
    if review:
        parts.append(f'<div class="card ai"><b>Cortex migration review:</b> {e(str(review.get("title") or ""))} — '
                     f'{e(str(review.get("reason") or ""))}</div>')
    if run.get('deployment'):
        d = run['deployment']
        parts.append(f'<h2>Deployment</h2><p>{e(d["identifier"])} from build {e(d["build_sha256"])}. '
                     'Deployment is not acceptance.</p>')
    parts.append('<h2>Not verified</h2>' + _list([
        'Visual and interaction parity (Streamlit controls adapt Tableau interactions)',
        'Values outside the published Tableau views and outside the default state',
        'Whole-workbook acceptance (needs every component verified and a reviewer sign-off)']))
    # ---- full evidence for reviewers (collapsed) ----
    parts.append('<h2>Details for reviewers</h2><p class="muted">Full evidence behind the summary above. Click a section to open it.</p>')
    parts.append('<details><summary>Workflow stages</summary>' + _table(
        [{'Stage': k, 'Status': v, 'Detail': (run.get('details') or {}).get(k)} for k, v in states.items()]) + '</details>')
    parts.append('<details><summary>Snowflake data model (read-only bindings)</summary>' + _table(run.get('binding_rows')) + '</details>')
    parts.append('<details><summary>Sheet checks — engine detail</summary>' + (
        _table(checks) if checks else '<p class="muted">Sheet checks were not run.</p>') + '</details>')
    parts.append('<details><summary>Tableau value comparison — every value compared</summary>')
    if ref:
        parts.append(f'<p class="muted">Default state, {e(ref["at"])}. Tolerance: half a unit at the precision Tableau displays.</p>'
                     + _table(ref['rows']))
        for view, detail in (ref.get('details') or {}).items():
            if detail:
                parts.append(f'<p><b>Values compared — {e(view)}</b></p>' + _table(detail))
    else:
        parts.append('<p class="muted">Not run.</p>')
    sref = run.get('sheet_reference')
    if sref:
        parts.append(f'<h3>Per-sheet Tableau values</h3><p class="muted">{e(sref["meta"])}</p>' + _table(
            [dict({'Sheet': n}, **r[0]) for n, r in sref['results'].items()],
            ['Sheet', 'Status', 'Matched values', 'Mismatched values', 'Rows missing in app', 'Note']))
        for n, (_s, detail) in sref['results'].items():
            if detail:
                parts.append(f'<p><b>{e(n)}</b></p>' + _table(detail[:60]))
    parts.append('</details>')
    parts.append('<details><summary>Calculations (translated SQL)</summary>' + _table(
        [{'Calculation': n, 'Translated SQL': (c.get('sql') or '')[:300]} for n, c in (ir.get('calcs') or {}).items()
         if isinstance(c, dict)]))
    untranslated = result['assessment'].get('untranslated_calculations')
    if untranslated:
        parts.append('<p><b>Refused (not substituted):</b> ' + e(', '.join(map(str, untranslated))) + '</p>')
    parts.append('</details>')
    parts.append('<details><summary>Cortex calls — full log</summary>' + _table(ai_rows) + '</details>')
    parts.append(f'<p class="muted" style="margin-top:24px">Engine {e(engine_version)}. Cortex output in this report is advisory. '
                 'It never changes a status, a binding or a value, and no model-generated code was executed.</p>'
                 '</div></body></html>')
    return ''.join(parts).encode('utf-8')


def _overall(run, checks):
    if run.get('failed'):
        return run['failed'] + ' failed'
    ref = run.get('reference')
    sref = ((run.get('sheet_reference') or {}).get('results') or {}).values()
    diff = (ref and any(r['Status'] == 'Difference found' for r in ref['rows'])) or \
        any(r[0]['Status'] == 'Difference found' for r in sref)
    if sref and not diff and all(r[0]['Status'].startswith('Match') for r in sref):
        return 'Ready for review — every captured sheet matches Tableau values'
    errors = sum(1 for c in checks or [] if c['Renders on data'] == 'Error')
    if diff or errors:
        return 'Needs review — differences or render errors found'
    matched = ref and any(r['Status'] == 'Match' for r in ref['rows'])
    return ('Ready for review — published Tableau values matched' if matched
            else 'Ready for review — Tableau values not yet compared')
