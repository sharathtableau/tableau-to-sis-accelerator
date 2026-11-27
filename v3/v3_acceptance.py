"""AC01 (E08, milestone M7): ONE acceptance verdict per dashboard, built by code from the run's evidence.

Checks (closed, in order):
    numbers_default        every sheet's values equal Tableau in the default state (v3_exact_loop proof: Verified)
    numbers_filter_states  every sheet Verified in every sampled quick-filter state (SL06) -- n/a without quick filters
    visual_geometry        the rendered page has zones and no spill / clipping / error box (tools/rendered_check.py)
    visual_review          the CX2 review ran; no XML-backed finding is confirmed or left unlabelled by a person
    interactions           every dashboard action replayed in the app matches Tableau (AC02) -- n/a without actions
    sign_off               the owner signs last; the signature is tied to a fingerprint of the evidence and goes STALE
                           when the evidence changes

ACCEPTED only when every check is PASS or NOT_APPLICABLE. A missing check is MISSING and blocks; nothing is assumed.
Cortex is not consulted here; its findings count only through the code verdicts and the owner's labels.
"""
import hashlib
import html
import json

VERSION = 'acceptance/1'
CHECKS = ('numbers_default', 'numbers_filter_states', 'visual_geometry', 'visual_review', 'interactions', 'sign_off')
OK = ('PASS', 'NOT_APPLICABLE')
ACTION = 'ACCEPT_DASHBOARD'


def _c(check, status, detail):
    return {'check': check, 'status': status, 'detail': detail}


def _sheets(dash):
    return [s['name'] for s in dash.get('sheets') or [] if s.get('worksheet_semantics')]


def _numbers_default(run, dash):
    exact = run.get('exact')
    if not exact:
        return _c('numbers_default', 'MISSING', 'Load the Tableau values and run the comparison (Validation, tab 2).')
    missing, bad = [], []
    for name in _sheets(dash):
        e = exact.get((dash['name'], name))
        if e is None:
            missing.append(name)
        elif e.get('status') != 'Verified':
            bad.append(f"{name}: {e.get('status')}")
    if bad:
        return _c('numbers_default', 'FAIL', 'Not equal to Tableau: ' + '; '.join(bad))
    if missing:
        return _c('numbers_default', 'MISSING', 'No Tableau values for: ' + ', '.join(missing))
    return _c('numbers_default', 'PASS', f'{len(_sheets(dash))} sheet(s) equal to Tableau in the default state')


def _numbers_states(run, dash):
    if not any(f.get('kind') == 'categorical' for f in dash.get('filters') or []):
        return _c('numbers_filter_states', 'NOT_APPLICABLE', 'No categorical quick filter on this dashboard')
    res = (run.get('sl06') or {}).get('result') or {}
    if res.get('status') != 'OK' or res.get('dashboard') != dash['name']:
        return _c('numbers_filter_states', 'MISSING', 'Run the filter-state comparison for this dashboard (SL06).')
    rows = {r['sheet']: r['verdict'] for r in res.get('rows') or []}
    bad = [f'{s}: {v}' for s, v in rows.items() if v != 'VERIFIED_ALL_STATES']
    if bad:
        return _c('numbers_filter_states', 'FAIL', 'Not verified in every state: ' + '; '.join(bad))
    missing = [s for s in _sheets(dash) if s not in rows]
    if missing:
        return _c('numbers_filter_states', 'MISSING', 'No filter-state capture for: ' + ', '.join(missing))
    return _c('numbers_filter_states', 'PASS', f'{len(rows)} sheet(s) equal to Tableau in every sampled state')


def _geometry(run, dash):
    rc = (run.get('rendered_checks') or {}).get(dash['name'])
    if rc is None:
        return _c('visual_geometry', 'MISSING', 'Load the rendered_check --json result for this dashboard.')
    if not rc.get('zones'):
        return _c('visual_geometry', 'FAIL', 'The rendered page drew no zones (nothing was checked)')
    n = len(rc.get('problems') or []) + len(rc.get('errors') or [])
    if n:
        return _c('visual_geometry', 'FAIL', f"{len(rc.get('problems') or [])} zone problem(s), "
                                             f"{len(rc.get('errors') or [])} error box(es)")
    return _c('visual_geometry', 'PASS', f"{rc['zones']} zones, no spill, clipping or error box")


def _review(run, dash):
    rec = (run.get('cx2') or {}).get(dash['name'])
    if not rec or rec.get('status') != 'OK':
        return _c('visual_review', 'MISSING', 'Run the visual review for this dashboard (Validation, tab 4).')
    evals = rec.get('evaluation') or []
    backed = [(f, evals[i] if i < len(evals) else {}) for i, f in enumerate(rec.get('findings') or [])
              if f.get('verdict') == 'XML_BACKED']
    confirmed = [f for f, e in backed if (e or {}).get('owner_label') == 'confirmed']
    open_ = [f for f, e in backed if (e or {}).get('owner_label') not in ('confirmed', 'rejected')]
    if confirmed:
        return _c('visual_review', 'FAIL', f'{len(confirmed)} XML-backed difference(s) confirmed by a person')
    if open_:
        return _c('visual_review', 'OPEN', f'{len(open_)} XML-backed finding(s) not yet labelled (Confirm / Reject)')
    return _c('visual_review', 'PASS', f'{len(backed)} XML-backed finding(s), all rejected by a person'
              if backed else 'No XML-backed difference')


def _interactions(run, dash):
    actions = [a.get('id') for a in dash.get('actions') or []]
    if not actions:
        return _c('interactions', 'NOT_APPLICABLE', 'No dashboard action')
    rec = (run.get('ac02') or {}).get(dash['name'])
    if not rec or rec.get('status') != 'OK':
        return _c('interactions', 'MISSING', 'Capture and replay the dashboard actions (AC02).')
    got = {a.get('id'): a.get('verdict') for a in rec.get('actions') or []}
    bad = [f'{n}: {v}' for n, v in got.items() if v != 'VERIFIED']
    if bad:
        return _c('interactions', 'FAIL', 'Differs from Tableau or not covered: ' + '; '.join(map(str, bad)))
    missing = [n for n in actions + ['reset'] if n not in got]
    if missing:
        return _c('interactions', 'MISSING', 'Not replayed: ' + ', '.join(map(str, missing)))
    return _c('interactions', 'PASS', f'{len(actions)} action(s) and Reset match Tableau')


def _fingerprint(checks):
    return hashlib.sha256(json.dumps([[c['check'], c['status'], c['detail']] for c in checks],
                                     sort_keys=True).encode('utf-8')).hexdigest()


def evaluate(run, dashboard):
    dash = next((d for d in run['result']['ir'].get('dashboards') or [] if d.get('name') == dashboard), None)
    if dash is None:
        return {'verdict': 'UNKNOWN_DASHBOARD', 'checks': [], 'ready_to_sign': False}
    checks = [_numbers_default(run, dash), _numbers_states(run, dash), _geometry(run, dash), _review(run, dash),
              _interactions(run, dash)]
    ready = all(c['status'] in OK for c in checks)
    fp = _fingerprint(checks)
    signed = (run.get('acceptance') or {}).get(dashboard)
    if signed and signed.get('fingerprint') == fp:
        checks.append(_c('sign_off', 'PASS', f"Signed (approval {signed.get('approval_id')})"))
    elif signed:
        checks.append(_c('sign_off', 'STALE', 'The evidence changed after the sign-off; sign again'))
    else:
        checks.append(_c('sign_off', 'PENDING', 'The owner signs once every other check passes'))
    accepted = ready and checks[-1]['status'] == 'PASS'
    return {'version': VERSION, 'dashboard': dashboard, 'checks': checks, 'ready_to_sign': ready,
            'fingerprint': fp, 'verdict': 'ACCEPTED' if accepted else 'NOT_ACCEPTED'}


def sign(run, dashboard, approve):
    """The owner's sign-off. approve(action, subject, detail dict) -> approval id (v3_store.approve in the app).
    Refused unless every check passes; not recorded unless the approval is stored."""
    res = evaluate(run, dashboard)
    if not res['ready_to_sign']:
        return False
    detail = {'version': VERSION, 'fingerprint': res['fingerprint'],               # a dict, stored as VARIANT
              'verdict_checks': {c['check']: c['status'] for c in res['checks'] if c['check'] != 'sign_off'},
              'details': {c['check']: c['detail'] for c in res['checks'] if c['check'] != 'sign_off'}}
    aid = approve(ACTION, dashboard, detail)
    if not aid:
        return False
    run.setdefault('acceptance', {})[dashboard] = {'fingerprint': res['fingerprint'], 'approval_id': aid}
    return True


def load_rendered_check(data):
    """tools/rendered_check.py --json output -> {dashboard: {zones, problems, errors}}. Raises ValueError."""
    try:
        rows = json.loads(data.decode('utf-8') if isinstance(data, (bytes, bytearray)) else data)
    except ValueError:
        raise ValueError('Not a rendered_check result')
    if not isinstance(rows, list) or not rows or any(
            not isinstance(r, dict) or not isinstance(r.get('dashboard'), str) or not isinstance(r.get('zones'), int)
            or not isinstance(r.get('problems'), list) or not isinstance(r.get('errors'), list) for r in rows):
        raise ValueError('Not a rendered_check result')
    return {r['dashboard']: {'zones': r['zones'], 'problems': r['problems'], 'errors': r['errors']} for r in rows}


# ---------------------------------------------------------------- report and UI
def report_section(run):
    e = lambda v: html.escape(str(v if v is not None else ''))
    out = ['<h2>Acceptance <span class="muted">(code verdict per dashboard; the owner signs last)</span></h2>']
    for dash in run['result']['ir'].get('dashboards') or []:
        res = evaluate(run, dash.get('name'))
        out.append(f"<h3>{e(dash.get('name'))} — {e(res['verdict'])}</h3><table><thead><tr><th>Check</th><th>Status</th>"
                   '<th>Detail</th></tr></thead><tbody>'
                   + ''.join(f"<tr><td>{e(c['check'])}</td><td>{e(c['status'])}</td><td class=\"muted\">{e(c['detail'])}</td></tr>"
                             for c in res['checks']) + '</tbody></table>')
    return ''.join(out)


def render(run, st, approve):
    """Validation -> Acceptance. approve(action, subject, detail) records the sign-off (v3_store.approve)."""
    st.caption('One verdict per dashboard. Code combines the evidence from the other tabs; a missing check blocks; '
               'you sign last, and a signature goes stale when the evidence changes.')
    dashboards = [d['name'] for d in run['result']['ir'].get('dashboards') or []]
    if not dashboards:
        st.info('No dashboard in this run.')
        return
    pick = st.selectbox('Dashboard', dashboards, key='ac01-dash')
    up = st.file_uploader('rendered_check --json result (from tools/rendered_check.py on the laptop)', type=['json'],
                          key='ac01-rc')
    if up is not None:
        try:
            run.setdefault('rendered_checks', {}).update(load_rendered_check(up.getvalue()))
        except ValueError:
            st.warning('That file is not a rendered_check --json result.')
    res = evaluate(run, pick)
    tone = st.success if res['verdict'] == 'ACCEPTED' else st.warning
    tone(f"**{pick}: {res['verdict']}**" + ('' if res['verdict'] == 'ACCEPTED' else
                                             (' — ready for your sign-off' if res['ready_to_sign'] else '')))
    st.dataframe([{'Check': c['check'], 'Status': c['status'], 'Detail': c['detail']} for c in res['checks']],
                 hide_index=True, use_container_width=True)
    if st.button('Sign off this dashboard as accepted', key='ac01-sign', disabled=not res['ready_to_sign']
                 or res['verdict'] == 'ACCEPTED', type='primary'):
        if not sign(run, pick, approve):
            st.warning('Sign-off was not recorded (the evidence store did not accept it).')
        else:
            st.rerun()
