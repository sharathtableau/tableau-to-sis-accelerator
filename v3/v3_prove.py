"""Cortex proposes, code proves: typed hypotheses for one Tableau/app difference.

AI06.3 (narrow slice). For a sheet whose values differ from Tableau's captured values:

1. Code computes facts from the evidence it already has (e.g. whether every gap is larger than
   Tableau's display precision, which rules rounding out; which FIXED level-of-detail keys the
   sheet's calculations use).
2. Cortex (AI_COMPLETE, structured output) ranks hypotheses from a FIXED, typed menu. It cannot
   add a hypothesis, and it never writes SQL.
3. Code tests each ranked hypothesis that has an automatic test:
   - display_rounding: a fact computed from the compared values;
   - lod_key_grain: the sheet is re-rendered with the engine's own query rewritten so every
     FIXED value is counted once per (mark, LOD key) instead of once per row. The rewrite is a
     deterministic transform of SQL our engine generated; the result is compared with Tableau's
     captured values by the same comparison the Validation page uses.
   A baseline render (no rewrite) is compared too, so the harness is shown to reproduce the app.
4. Nothing here changes a verdict, the IR or the app. A proven hypothesis is reported as a
   proposed fix ("not applied"); the engine fix ships separately and is validated again.

Read-only: every query is a SELECT produced by our engine (or its deterministic rewrite).
"""
import contextlib
import re

MENU = {
    'lod_key_grain': {
        'label': 'FIXED values counted once per LOD key',
        'meaning': 'The measure is built from FIXED level-of-detail values. Tableau counts each '
                   'LOD key (e.g. each customer) once per mark; the app counts one value per '
                   'underlying row, so keys with several rows are over-weighted.',
        'test': 'query'},
    'display_rounding': {
        'label': 'Display rounding',
        'meaning': 'The values are equal and only differ by how Tableau rounds them for display.',
        'test': 'fact'},
    'filter_vs_fixed_order': {
        'label': 'Filter applied before or after FIXED',
        'meaning': 'A dimension filter is applied at a different stage relative to the FIXED '
                   'calculation than in Tableau (context filters run before FIXED).',
        'test': None},
    'null_handling': {
        'label': 'Null handling',
        'meaning': 'Nulls are included, excluded or defaulted differently.',
        'test': None},
    'date_grain': {
        'label': 'Date grain or boundary',
        'meaning': 'Dates are truncated, bucketed or bounded differently (time zone, week start, '
                   'period edges).',
        'test': None},
    'aggregation_type': {
        'label': 'Aggregation type',
        'meaning': 'A different aggregation (e.g. SUM vs AVG, COUNT vs COUNTD) is applied.',
        'test': None},
}

CHOICE_SCHEMA = {'type': 'object', 'properties': {
    'ranked': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'id': {'type': 'string', 'enum': list(MENU)},
        'reason': {'type': 'string'}},
        'required': ['id', 'reason'], 'additionalProperties': False}},
    'summary': {'type': 'string'},
    'proposed_fix': {'type': 'object', 'properties': {
        'hypothesis': {'type': 'string', 'enum': [k for k, v in MENU.items() if v['test'] == 'query']},
        'lod_key': {'type': 'string'},
        'explanation': {'type': 'string'}},
        'required': ['hypothesis', 'lod_key', 'explanation'], 'additionalProperties': False}},
    'required': ['ranked', 'summary'], 'additionalProperties': False}
MAX_EVIDENCE_CALCS = 25

MAX_TESTED = 3

from query_fixes import (_top_level, _split_commas, _alias, _norm, partition_keys,  # noqa: F401
                         lod_grain_sql, read_only, TRANSFORMS, key_norm, lod_grain_for)

# --------------------------------------------------------------------------- #
# Facts computed by code
# --------------------------------------------------------------------------- #


def rounding_fact(detail):
    """Is display rounding able to explain the mismatches? Uses the comparison's own parser."""
    import v3_sheet_capture as SC
    gaps, beyond, tol_max = [], 0, 0.0
    for d in detail or []:
        if d.get('Match'):
            continue
        tv, tol = SC._number(d.get('Tableau'))
        av, _ = SC._number(d.get('App'))
        if tv is None or av is None:
            continue
        gap = abs(tv - av)
        gaps.append(gap)
        tol_max = max(tol_max, tol)
        beyond += gap > tol + 1e-9 * max(1.0, abs(tv))
    if not gaps:
        return {'status': 'No numeric gaps to test', 'refuted': False, 'gaps': 0}
    refuted = beyond == len(gaps)
    return {'status': ('Ruled out' if refuted else 'Possible'), 'refuted': refuted, 'gaps': len(gaps),
            'beyond_precision': beyond, 'largest_gap': round(max(gaps), 4), 'smallest_gap': round(min(gaps), 4),
            'display_precision': round(tol_max, 4),
            'text': (f'{beyond} of {len(gaps)} differences are larger than Tableau\'s display precision '
                     f'(±{round(tol_max, 4)}); gaps range {round(min(gaps), 4)} to {round(max(gaps), 4)}.')}


def sheet_calcs(run, sheet, limit=MAX_EVIDENCE_CALCS):
    """Calculations on the sheet's own fields, then every calculation they reference (code-side,
    deterministic). A FIXED part often sits one level down (a measure built on a per-customer FIXED calc).
    Exact field-name matching: the sheet's workbook-wide metadata is not searched."""
    import v3_cortex_calc as CC
    ir = (run.get('result') or {}).get('ir') or {}
    formulas = (run.get('result') or {}).get('calc_formulas') or {}
    names = list(ir.get('calcs') or {}) + [n for n in formulas if n not in (ir.get('calcs') or {})]
    return CC.sheet_calc_names([sheet], names, formulas)[:limit]


_FIXED_DIMS = re.compile(r'\{\s*FIXED\s+([^:}]+):', re.I)


def tableau_fixed_keys(run, sheet):
    """Dimensions named in {FIXED ...} in the sheet's Tableau formulas (and the ones they reference)."""
    formulas = (run.get('result') or {}).get('calc_formulas') or {}
    out = []
    for n in sheet_calcs(run, sheet):
        for m in _FIXED_DIMS.finditer(formulas.get(n) or ''):
            for dim in re.findall(r'\[([^\[\]]+)\]', m.group(1)):
                if dim not in out:
                    out.append(dim)
    return out


def facts(run, sheet, detail):
    ir = (run.get('result') or {}).get('ir') or {}
    keys = []
    for name in sheet_calcs(run, sheet):
        c = (ir.get('calcs') or {}).get(name)
        if isinstance(c, dict):
            for k in partition_keys(c.get('sql') or ''):
                if k not in keys:
                    keys.append(k)
    rows = run.get('binding_rows') or []
    model = 'relationship model' if any('relationship' in str(r).lower() or 'semantic' in str(r).lower()
                                        for r in rows) else 'single table'
    return {'rounding': rounding_fact(detail), 'fixed_lod_keys_in_sheet_calcs': keys,
            'tableau_fixed_dimensions': tableau_fixed_keys(run, sheet), 'data_model': model}


_DATEPART = {'tdy': 'day', 'twk': 'week', 'tmn': 'month', 'tqr': 'quarter', 'tyr': 'year'}


_DATE_FORMATS = ('%B %d, %Y', '%b %d, %Y', '%Y-%m-%d', '%m/%d/%Y', '%d %B %Y', '%B %Y', '%b %Y', '%Y')


def _chart_dates(detail):
    """(all chart-point dates, differing dates) parsed from the compared keys, or (None, None)."""
    import datetime
    def parse(k):
        for f in _DATE_FORMATS:
            try:
                return datetime.datetime.strptime(str(k).strip(), f).date()
            except ValueError:
                continue
        return None
    keys = [(parse(d.get('Key')), bool(d.get('Match'))) for d in detail or []]
    good = [(k, m) for k, m in keys if k]
    if not keys or len(good) * 2 < len(keys):
        return None, None
    return sorted({k for k, _m in good}), sorted({k for k, m in good if not m})


def grain_fact(session, run, sheet, keys, detail=None):
    """Code-computed fact (read-only): per chart point, how many rows each LOD key has.
    More rows than keys means a FIXED value per key is repeated on every one of its rows, so an
    aggregate over rows weights keys by their row count (Tableau counts each key once per mark)."""
    if session is None or not keys:
        return {'status': 'not computed', 'reason': 'needs the hosted Snowflake session and a FIXED key'}
    import v3_cortex_calc as CC
    from urllib.parse import unquote
    from v3_model_binding import compile_query
    bindings = run.get('bindings') or {}
    binding = bindings.get(sheet.get('datasource')) or next((b for b in bindings.values() if b), None)
    if not binding:
        return {'status': 'not computed', 'reason': 'no verified binding'}
    rel, columns, _cmap = CC.relation(binding, compile_query)
    if columns is None:
        columns = list(session.sql(f'SELECT * FROM {rel} LIMIT 0').columns)
    by_upper = {re.sub(r'["\s]', '', c).upper(): c for c in columns}
    key = by_upper.get(key_norm(keys[0]))
    x = sheet.get('x') if isinstance(sheet.get('x'), dict) else {}
    raw = unquote(str(x.get('field_id') or '').split('::')[-1]) or str(x.get('caption') or '')
    xcol = by_upper.get(re.sub(r'\W+', '_', raw).strip('_').upper())
    if not key:
        return {'status': 'not computed', 'reason': f'key {keys[0]} not found in the bound columns'}
    if xcol:
        part = _DATEPART.get(str(x.get('datepart') or ''))
        bucket = f"DATE_TRUNC('{part}', {xcol})" if part else xcol
        label = f"{xcol} by {part}" if part else xcol
    else:
        bucket, label = "'all'", 'the whole sheet'
    chart, differing = _chart_dates(detail) if xcol else (None, None)
    lit = lambda ds: ', '.join(f"DATE '{d.isoformat()}'" for d in ds) or 'NULL'
    scope = f' AND B::DATE IN ({lit(chart)})' if chart else ''
    q = (f'SELECT COUNT(*) AS POINTS, AVG(N / K) AS AVG_ROWS_PER_KEY, MAX(N / K) AS MAX_ROWS_PER_KEY, '
         f'COUNT_IF(N > K) AS POINTS_WITH_REPEATS, '
         + (f'COUNT_IF(B::DATE IN ({lit(differing)})) AS DIFF_POINTS, '
            f'AVG(IFF(B::DATE IN ({lit(differing)}), N / K, NULL)) AS DIFF_AVG_ROWS_PER_KEY, ' if chart else '')
         + f'1 AS ONE FROM (SELECT {bucket} AS B, COUNT(*) AS N, '
         f'COUNT(DISTINCT {key}) AS K FROM {rel} __g GROUP BY 1) WHERE K > 0{scope}')
    if not read_only(q):
        return {'status': 'not computed', 'reason': 'query failed the read-only guard'}
    row = session.sql(q).collect()[0]
    cols = ['POINTS', 'AVG_ROWS_PER_KEY', 'MAX_ROWS_PER_KEY', 'POINTS_WITH_REPEATS'] + \
        (['DIFF_POINTS', 'DIFF_AVG_ROWS_PER_KEY'] if chart else []) + ['ONE']
    d = row.as_dict() if hasattr(row, 'as_dict') else dict(zip(cols, row))
    pts, rep_pts = int(d.get('POINTS') or 0), int(d.get('POINTS_WITH_REPEATS') or 0)
    avg, mx = round(float(d.get('AVG_ROWS_PER_KEY') or 0), 2), round(float(d.get('MAX_ROWS_PER_KEY') or 0), 2)
    where = f'on the {len(chart)} chart points Tableau shows' if chart else 'over all data'
    text = (f'Per chart point ({label}), {where}: {rep_pts} of {pts} points have more rows than distinct {key}; '
            f'on average each {key} has {avg} rows per point (max {mx}).')
    out = {'status': 'computed', 'key': key, 'chart_point': label, 'scope': where, 'points': pts,
           'points_with_more_rows_than_keys': rep_pts, 'avg_rows_per_key': avg, 'max_rows_per_key': mx}
    if chart and differing:
        davg = round(float(d.get('DIFF_AVG_ROWS_PER_KEY') or 0), 2)
        out.update(differing_points=int(d.get('DIFF_POINTS') or 0), differing_avg_rows_per_key=davg)
        text += f' On the {len(differing)} points that differ from Tableau: {davg} rows per {key} on average.'
    out['text'] = text + (f' A FIXED value per {key} is repeated on each of those rows, so an aggregate over rows '
                          'weights each key by its row count.')
    return out

# --------------------------------------------------------------------------- #
# Cortex: rank hypotheses from the typed menu
# --------------------------------------------------------------------------- #


def choose(session, run, row, detail, sheet, known_facts, app_queries=None):
    import v3_ai as A
    ev = {'view': row, 'mismatched_values': [d for d in detail if not d.get('Match')][:30],
          'matched_sample': [d for d in detail if d.get('Match')][:5],
          'sheet': {k: (sheet or {}).get(k) for k in ('name', 'type', 'kind', 'mark', 'filters', 'x', 'y', 'color')},
          'tableau_formulas_on_sheet_and_referenced': {n: f[:800] for n in sheet_calcs(run, sheet)
                                                       if (f := ((run.get('result') or {}).get('calc_formulas') or {}).get(n))},
          'calculations_on_sheet_and_referenced': {n: ((((run.get('result') or {}).get('ir') or {}).get('calcs') or {})
                                                      .get(n, {}).get('sql') or '')[:600]
                                                   for n in sheet_calcs(run, sheet)
                                                   if isinstance((((run.get('result') or {}).get('ir') or {}).get('calcs') or {}).get(n), dict)},
          'queries_the_app_runs_for_this_sheet': [q[:1500] for q in (app_queries or [])[:3]],
          'facts_computed_by_code': known_facts,
          'hypothesis_menu': {k: {'label': v['label'], 'meaning': v['meaning'],
                                  'automatic_test': bool(v['test'])} for k, v in MENU.items()}}
    call = lambda: A.call(session, run, 'Validation', 'Cause proof: rank hypotheses — ' + str(row.get('View')),
                 A._wrap('Tableau and the migrated app disagree on the values below. Rank the hypotheses '
                         'from hypothesis_menu that best explain the difference, most likely first (at most '
                         '4). Use ONLY ids from the menu. Respect facts_computed_by_code: a hypothesis a fact '
                         'rules out must not be ranked first. One short reason each, citing the evidence '
                         '(formulas, the queries the app runs, facts_computed_by_code). Then propose the fix '
                         'for your top hypothesis that has an automatic test: for lod_key_grain, lod_key is the '
                         'column whose FIXED values must count once per chart point (it must be one of '
                         'facts_computed_by_code.fixed_lod_keys_in_sheet_calcs). Omit proposed_fix if none applies. '
                         'Code will test your choices and your fix; you do not decide the outcome. Be concise: '
                         'summary under 70 words, each reason under 35 words, explanation under 40 words.', ev),
                 CHOICE_SCHEMA, max_tokens=2500)
    try:
        out = call()
    except A.CortexUnavailable as exc:            # a cut-off or malformed answer is retried once, then reported
        if 'ValueError' not in str(exc) and 'JSONDecodeError' not in str(exc):
            raise
        out = call()
    ranked, seen = [], set()
    for r in out.get('ranked') or []:
        hid = r.get('id') if isinstance(r, dict) else None
        if hid in MENU and hid not in seen:
            seen.add(hid)
            ranked.append({'id': hid, 'reason': str(r.get('reason') or '')[:400]})
    fix = out.get('proposed_fix') if isinstance(out.get('proposed_fix'), dict) else None
    return {'ranked': ranked[:4], 'summary': str(out.get('summary') or '')[:600],
            'proposed_fix': ({'hypothesis': fix.get('hypothesis'), 'lod_key': str(fix.get('lod_key') or '')[:120],
                              'explanation': str(fix.get('explanation') or '')[:300]}
                             if fix and fix.get('hypothesis') in MENU else None),
            'rejected_ids': [r.get('id') for r in out.get('ranked') or []
                             if isinstance(r, dict) and r.get('id') not in MENU]}

# --------------------------------------------------------------------------- #
# Code: run a query hypothesis by re-rendering the sheet with the rewrite
# --------------------------------------------------------------------------- #


LAST_QUERIES = []           # the queries the as-migrated sheet ran in the latest baseline render


@contextlib.contextmanager
def _query_rewrite(transform, applied, seen=None):
    import engine
    original = engine._q_exec

    def hooked(sql):
        result = original(sql)          # our engine's own query (raises -> engine hoists, retries)
        if seen is not None and sql not in seen:
            seen.append(sql)
        new = transform(sql) if transform else None
        if new and new != sql and read_only(new):
            applied.append({'engine_sql': sql, 'tested_sql': new})
            return original(new)
        return result
    engine._q_exec = hooked
    try:
        yield
    finally:
        engine._q_exec = original


def render_compare(run, dash_name, sheet, entry, transform=None, snowflake=True):
    """Re-render one sheet (optionally with a query rewrite) and compare with Tableau's values."""
    import engine
    import headless_render as H
    import parameter_actions
    import v3_evidence as EV
    import v3_sheet_capture as SC
    from v3_local import preview_context
    ir = run['result']['ir']
    applied = []
    with preview_context(ir, run['bindings'], snowflake=snowflake), EV.offscreen():
        parameter_actions.reset(parameter_actions.context(engine))
        if transform is None:
            LAST_QUERIES.clear()
        with _query_rewrite(transform, applied, LAST_QUERIES if transform is None else None):
            cap = H._capture_all(EV._native(sheet), None, dash_name)
    try:
        values, _total, every = EV._app_values(sheet, cap)
    except Exception:
        values, every = [], []
    summary, detail = SC.compare_sheet(entry, every or values, sheet)
    return summary, detail, applied, cap.get('error')


def _verdict(summary, baseline):
    m, x = summary.get('Matched values') or 0, summary.get('Mismatched values') or 0
    missing = summary.get('Rows missing in app') or 0
    total = m + x
    if summary.get('Status') == 'Match':
        return 'Proven', f'Reproduces Tableau on {m} of {total} values'
    if total and baseline is not None and m > (baseline.get('Matched values') or 0):
        return 'Partly', f'Improves the match to {m} of {total} values'
    if total:
        return 'Not reproduced', f'{m} of {total} values match' + (f'; {missing} rows missing' if missing else '')
    return 'Not comparable', summary.get('Note') or summary.get('Status') or ''


def prove(session, run, row, detail, sheet, dash_name, entry, snowflake=True, render=None, chooser=None):
    """Cortex ranks typed hypotheses; code tests them. Advisory: never changes a verdict. `chooser` replaces the
    AI_COMPLETE ranking (CX4: the diagnosis Cortex Agent) with the same output contract; code still tests every
    hypothesis it can, whatever the chooser ranked."""
    render = render or render_compare
    LAST_QUERIES.clear()
    base_summary, _bd, _ba, base_err = render(run, dash_name, sheet, entry, None, snowflake)
    app_queries = list(LAST_QUERIES)
    known = facts(run, sheet, detail)
    try:
        known['grain'] = grain_fact(session, run, sheet, known['fixed_lod_keys_in_sheet_calcs'], detail)
    except Exception as exc:                          # a fact we could not compute is stated, never guessed
        known['grain'] = {'status': 'not computed', 'reason': f'{type(exc).__name__}: {str(exc)[:120]}'}
    memory = run.get('_memory')                       # EN02: fixes proven before for the same formulas (a fact)
    if hasattr(memory, 'fixes_for'):
        formulas = (run.get('result') or {}).get('calc_formulas') or {}
        hits = memory.fixes_for([formulas[n] for n in sheet_calcs(run, sheet) if formulas.get(n)])
        if hits:
            known['proven_before'] = hits
    chosen = (chooser or choose)(session, run, row, detail, sheet, known, app_queries)
    proposal = chosen.get('proposed_fix') or {}
    valid_keys = {key_norm(k) for k in known['fixed_lod_keys_in_sheet_calcs']}
    harness = {'summary': base_summary, 'error': base_err,
               'reproduces_app': base_err is None and (base_summary.get('Mismatched values') or 0) ==
               sum(1 for d in detail if not d.get('Match'))}
    results = []
    picks = [(rank, p['id'], p['reason']) for rank, p in enumerate(chosen['ranked'], 1)]
    ranked_ids = {p[1] for p in picks}
    # Code tests EVERY hypothesis it can test, whether or not Cortex ranked it.
    picks += [('Not ranked', hid, '—') for hid, spec in MENU.items() if spec['test'] and hid not in ranked_ids]
    for rank, hid, reason in picks:
        spec = MENU[hid]
        res = {'Cortex rank': rank, 'Hypothesis': spec['label'], 'id': hid, 'Cortex reason': reason}
        if not spec['test']:
            res.update({'Test': 'No automatic test yet', 'Result': 'Untested', 'Evidence': 'Check manually'})
        elif spec['test'] == 'fact':
            f = known['rounding']
            res.update({'Test': 'Fact from compared values',
                        'Result': 'Ruled out' if f.get('refuted') else 'Not ruled out',
                        'Evidence': f.get('text') or f.get('status')})
        else:
            transform, test_name, key = TRANSFORMS[hid], 'Read-only query rewrite (code\'s general test)', None
            if proposal.get('hypothesis') == hid and hid == 'lod_key_grain':
                if key_norm(proposal.get('lod_key')) in valid_keys:
                    key = proposal['lod_key']
                    transform = lod_grain_for(key)
                    test_name = f"Cortex's proposed fix: count each {key} once per chart point (read-only rewrite)"
                else:
                    res['Cortex reason'] = (reason + f" [Cortex proposed key {proposal.get('lod_key')!r}, which is not a "
                                            "FIXED key of this sheet's calculations; refused, general test run]")
            try:
                summary, _d, applied, err = render(run, dash_name, sheet, entry, transform, snowflake)
            except Exception as exc:
                summary, applied, err = {}, [], f'{type(exc).__name__}: {str(exc)[:160]}'
            if err or not applied:
                res.update({'Test': test_name, 'Result': 'Not applicable',
                            'Evidence': err or "This sheet's queries are not an aggregate of FIXED values"})
            else:
                verdict, text = _verdict(summary, base_summary)
                res.update({'Test': test_name, 'Result': verdict, 'Evidence': text, 'fix_key': key,
                            'sql': applied[-1]['tested_sql'], 'engine_sql': applied[-1]['engine_sql']})
        results.append(res)
    proven = next((r for r in results if r['Result'] == 'Proven'), None)
    if proven:
        if proven['Cortex rank'] != 'Not ranked' and proven.get('fix_key'):
            who = (f"Cortex identified this cause (ranked #{proven['Cortex rank']}) and proposed the fix: count each "
                   f"{proven['fix_key']} once per chart point. Code proved Cortex's fix.")
        elif proven['Cortex rank'] != 'Not ranked':
            who = f"Cortex identified this cause (ranked #{proven['Cortex rank']}) and code proved it."
        else:
            who = ('Cortex did not rank this cause. Code tests every cause it can, so a Cortex miss '
                   'does not lose the fix.')
        conclusion = (f"Proven by code: {proven['Hypothesis']}. {proven['Evidence']}. {who} "
                      'Ready for a reviewer to apply below.')
    else:
        conclusion = 'No hypothesis was proven by a query. Diagnosis stays open.'
    return {'chosen': chosen, 'facts': known, 'harness': harness, 'results': results, 'conclusion': conclusion}
