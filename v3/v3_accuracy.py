"""EN05 / AI08: Cortex accuracy per release on a fixed, seeded set (generic constructs, no workbook names).

Cause seeds   one built-in difference per cause-proof hypothesis; Cortex ranks through the production choose();
              top-1 / top-3 hit rates; each case asked twice (repeatable = same top answer, EN38).
Translation   Tableau formulas with a reference Snowflake expression; Cortex translates through the production CF05
seeds         propose() (allowlist); code compares both on the same rows in ONE read-only query over VALUES.
Result        one row per run in WBR_DB.CX_EVAL.CORTEX_ACCURACY (owner SQL); gate() compares with the previous
              release: a drop of more than 10 points in any rate is REGRESSION. Advisory for the product; a gate
              for the release (the owner decides)."""
import json
import re

TABLE = 'WBR_DB.CX_EVAL.CORTEX_ACCURACY'
TOLERANCE = 0.10
RATES = ('cause_top1', 'cause_top3', 'translation_agree', 'repeatable')
_ROLE = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]{0,254}$')


def _d(key, measure, tableau, app, match=False):
    return {'Key': key, 'Measure': measure, 'Tableau': tableau, 'App': app, 'Match': match}


# --------------------------------------------------------------------------- cause seeds
CAUSE_SEEDS = [
    {'id': 'lod', 'seeded': 'lod_key_grain',
     'formulas': {'First Purchase': '{FIXED [Customer ID]: MIN([Order Date])}',
                  'Days to 2nd Purchase': 'AVG(DATEDIFF("day", [First Purchase], [Second Purchase]))'},
     'measure': 'Days to 2nd Purchase',
     'detail': [_d('2024-01', 'Days to 2nd Purchase', '41.2', '35.8'), _d('2024-02', 'Days to 2nd Purchase', '39.0',
                                                                        '33.1'), _d('2024-03', 'Days to 2nd Purchase', '44.5', '44.5', True)],
     'facts': {'fixed_lod_keys_in_sheet_calcs': ['CUSTOMER_ID'],
               'grain': {'status': 'computed', 'rows_per_key': {'min': 1, 'max': 9, 'avg': 3.4}}}},
    {'id': 'round', 'seeded': 'display_rounding',
     'formulas': {'Margin': 'SUM([Profit]) / SUM([Sales])'}, 'measure': 'Margin',
     'detail': [_d('East', 'Margin', '0.13', '0.1349'), _d('West', 'Margin', '0.15', '0.1462')],
     'facts': {'rounding': {'all_gaps_within_display_precision': True, 'display_decimals': 2}}},
    {'id': 'filter', 'seeded': 'filter_vs_fixed_order',
     'formulas': {'Region Total': '{FIXED [Region]: SUM([Sales])}', 'Share': 'SUM([Sales]) / MIN([Region Total])'},
     'measure': 'Share',
     'detail': [_d('Group A', 'Share', '0.31', '0.22'), _d('Group B', 'Share', '0.40', '0.29')],
     'facts': {'filters': [{'field': 'Category', 'context': False, 'kind': 'categorical'}],
               'note': 'the sheet filters Category; Region Total ignores it in Tableau only if the filter is not '
                       'a context filter'}},
    {'id': 'null', 'seeded': 'null_handling',
     'formulas': {'Avg Discount': 'AVG(ZN([Discount]))'}, 'measure': 'Avg Discount',
     'detail': [_d('East', 'Avg Discount', '0.08', '0.12'), _d('West', 'Avg Discount', '0.05', '0.09')],
     'facts': {'nulls': {'Discount': {'null_rows': 412, 'rows': 1200}}}},
    {'id': 'date', 'seeded': 'date_grain',
     'formulas': {'Weekly Orders': 'COUNTD([Order ID])'}, 'measure': 'Weekly Orders',
     'detail': [_d('Week 1', 'Weekly Orders', '120', '97'), _d('Week 2', 'Weekly Orders', '131', '154'),
                _d('Week 3', 'Weekly Orders', '118', '118', True)],
     'facts': {'date_axis': {'part': 'week', 'tableau_week_start': 'Sunday', 'app_week_start': 'Monday'}}},
    {'id': 'agg', 'seeded': 'aggregation_type',
     'formulas': {'Discount Level': '[Discount]'}, 'measure': 'Discount Level',
     'detail': [_d('East', 'Discount Level', '0.15', '45.20'), _d('West', 'Discount Level', '0.11', '38.70')],
     'facts': {'declared_aggregation': {'Discount': 'AVG'}, 'app_aggregation': {'Discount': 'SUM'}}},
]


def _seed_run(case):
    calcs = {n: {'sql': ''} for n in case['formulas']}
    return {'name': 'accuracy seed', 'ai_log': [],
            'result': {'calc_formulas': dict(case['formulas']), 'ir': {'calcs': calcs}}}


def _seed_sheet(case):
    return {'name': 'seed sheet', 'type': 'bar', 'kind': 'bar', 'mark': 'bar', 'filters': case['facts'].get('filters'),
            'x': {'field': 'Key'}, 'y': {'field': case['measure']}}


def score_causes(chooser):
    """chooser(case, attempt) -> {'ranked': [{'id': ...}]}. Returns rates over CAUSE_SEEDS."""
    top1 = top3 = rep = 0
    rows = []
    for case in CAUSE_SEEDS:
        tops = []
        for attempt in (1, 2):
            try:
                ranked = [r.get('id') for r in (chooser(case, attempt) or {}).get('ranked') or []]
            except Exception as exc:                       # codes only
                ranked = ['CORTEX_FAILED:' + type(exc).__name__]
            tops.append(ranked)
        first = tops[0]
        hit1, hit3 = bool(first) and first[0] == case['seeded'], case['seeded'] in first[:3]
        same = bool(first) and bool(tops[1]) and first[0] == tops[1][0]
        top1, top3, rep = top1 + hit1, top3 + hit3, rep + same
        rows.append({'Case': case['id'], 'Seeded cause': case['seeded'], 'Cortex top': first[0] if first else '—',
                     'Top-1': hit1, 'Top-3': hit3, 'Repeatable': same})
    n = len(CAUSE_SEEDS)
    return {'cases': n, 'top1': top1 / n, 'top3': top3 / n, 'repeatable': rep / n, 'rows': rows}


def production_chooser(session, ai_run):
    import v3_prove

    def chooser(case, attempt):
        run = _seed_run(case)
        run['ai_log'] = ai_run.setdefault('ai_log', [])         # calls and cost logged on the accuracy run
        row = {'View': 'seed ' + case['id'], 'Status': 'Difference found'}
        return v3_prove.choose(session, run, row, case['detail'], _seed_sheet(case), dict(case['facts']), [])
    return chooser


# --------------------------------------------------------------------------- translation seeds
COLUMNS = ['SALES', 'QUANTITY', 'PROFIT', 'DISCOUNT', 'CUSTOMER', 'NAME', 'ORDER_DATE', 'SHIP_DATE']
_ROWS = [(100.0, 4, 20.0, 0.1, 'C1', 'Alpha', '2024-01-03', '2024-01-07'),
         (250.0, 5, -15.0, None, 'C2', 'Bravo', '2024-02-10', '2024-02-11'),
         (75.5, 1, 5.5, 0.2, 'C1', 'Charlie', '2024-02-28', '2024-03-04'),
         (310.0, 7, 0.0, 0.0, 'C3', 'Delta', '2024-03-15', '2024-03-15')]
TRANSLATION_SEEDS = [
    {'id': 'ratio', 'formula': 'SUM([Sales]) / SUM([Quantity])', 'reference': 'SUM(SALES) / NULLIF(SUM(QUANTITY), 0)',
     'level': 'aggregate'},
    {'id': 'countd', 'formula': 'COUNTD([Customer])', 'reference': 'COUNT(DISTINCT CUSTOMER)', 'level': 'aggregate'},
    {'id': 'avg_null', 'formula': 'AVG([Discount])', 'reference': 'AVG(DISCOUNT)', 'level': 'aggregate'},
    {'id': 'iif', 'formula': 'IIF([Profit] > 0, "Profit", "Loss")', 'reference': "IFF(PROFIT > 0, 'Profit', 'Loss')",
     'level': 'row'},
    {'id': 'datediff', 'formula': 'DATEDIFF("day", [Order Date], [Ship Date])',
     'reference': 'DATEDIFF(day, ORDER_DATE, SHIP_DATE)', 'level': 'row'},
    {'id': 'zn', 'formula': 'ZN([Discount])', 'reference': 'ZEROIFNULL(DISCOUNT)', 'level': 'row'},
    {'id': 'left', 'formula': 'LEFT([Name], 3)', 'reference': 'LEFT(NAME, 3)', 'level': 'row'},
    {'id': 'month', 'formula': 'DATETRUNC("month", [Order Date])', 'reference': "DATE_TRUNC('MONTH', ORDER_DATE)",
     'level': 'row'},
]


def _lit(v):
    if v is None:
        return 'NULL'
    if isinstance(v, (int, float)):
        return repr(v)
    if re.match(r'^\d{4}-\d{2}-\d{2}$', str(v)):
        return f"TO_DATE('{v}')"
    return "'" + str(v).replace("'", "''") + "'"


def values_relation():
    rows = ', '.join('(' + ', '.join(_lit(v) for v in r) + ')' for r in _ROWS)
    return f"(SELECT * FROM VALUES {rows} AS T({', '.join(COLUMNS)}))"


def score_translations(session, ask):
    """ask(prompt, schema) -> CF05 reply. Agreement only from code's comparison over VALUES."""
    import v3_cortex_calc as CC
    import v3_first_translator as FT
    rel, agree, rows = values_relation(), 0, []
    for t in TRANSLATION_SEEDS:
        item = {'field_id': t['id'], 'caption': t['id'], 'formula': t['formula'], 'engine_sql': None, 'refusal': None}
        p = FT.propose(item, COLUMNS, ask)
        result = p['state']
        if p['state'] == 'PROPOSED':
            try:
                result, _ev = CC.compare_one(session, rel, t['reference'], p['sql'], t['level'] == 'aggregate')
            except Exception as exc:                        # codes only
                result = 'QUERY_FAILED:' + type(exc).__name__
        agree += result == 'Agree'
        rows.append({'Case': t['id'], 'Tableau formula': t['formula'], 'Cortex': p.get('sql', ''), 'Result': result})
    n = len(TRANSLATION_SEEDS)
    return {'cases': n, 'agree': agree / n, 'rows': rows}


# --------------------------------------------------------------------------- run, store, gate
def run(session, release, ai_run=None):
    import v3_ai as A
    import v3_first_translator as FT
    import v3_usage as U
    ai_run = ai_run if ai_run is not None else {'name': 'Cortex accuracy ' + str(release), 'ai_log': []}
    causes = score_causes(production_chooser(session, ai_run))
    trans = score_translations(session, FT.cortex_ask(session, ai_run))
    t = U.totals(A.log(ai_run))
    return {'release': str(release)[:40], 'model': A.model_for('Validation', 'Cause proof'),
            'cause_top1': round(causes['top1'], 3), 'cause_top3': round(causes['top3'], 3),
            'translation_agree': round(trans['agree'], 3), 'repeatable': round(causes['repeatable'], 3),
            'calls': t['calls'], 'tokens': t['input'] + t['output'], 'credits': t['credits'],
            'cause_rows': causes['rows'], 'translation_rows': trans['rows']}


def gate(current, previous, tolerance=TOLERANCE):
    if not previous:
        return {'status': 'FIRST_RUN', 'reasons': []}
    reasons = [f"{k}: {previous[k]:.0%} -> {current[k]:.0%}" for k in RATES
               if previous.get(k) is not None and current.get(k) is not None
               and float(current[k]) < float(previous[k]) - tolerance]
    return {'status': 'REGRESSION' if reasons else 'PASS', 'reasons': reasons}


def setup_sql(role='WBR_OWNER'):
    if not _ROLE.match(str(role or '')):
        raise ValueError('role rejected')
    return ['CREATE SCHEMA IF NOT EXISTS WBR_DB.CX_EVAL;',
            f'CREATE TABLE IF NOT EXISTS {TABLE} (RELEASE STRING, MODEL STRING, CAUSE_TOP1 FLOAT, CAUSE_TOP3 FLOAT, '
            'TRANSLATION_AGREE FLOAT, REPEATABLE FLOAT, CALLS NUMBER, TOKENS NUMBER, CREDITS FLOAT, DETAIL VARIANT, '
            'RUN_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP());',
            f'GRANT SELECT, INSERT ON TABLE {TABLE} TO ROLE {role.upper()};']


def save(session, row):
    sql = (f'INSERT INTO {TABLE} (RELEASE, MODEL, CAUSE_TOP1, CAUSE_TOP3, TRANSLATION_AGREE, REPEATABLE, CALLS, '
           'TOKENS, CREDITS, DETAIL) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, PARSE_JSON(?)')
    params = [str(row.get('release') or '')[:40], str(row.get('model') or '')[:80]] + \
             [row.get(k) for k in RATES] + [row.get('calls'), row.get('tokens'), row.get('credits'),
                                            json.dumps({k: row.get(k) for k in ('cause_rows', 'translation_rows')},
                                                       default=str)[:60000]]
    try:
        session.sql(sql, params=params).collect()
        return 'OK'
    except Exception as exc:
        return 'ACCURACY_WRITE_FAILED:' + type(exc).__name__


def history(session, limit=20):
    try:
        rows = session.sql(f'SELECT RELEASE, MODEL, CAUSE_TOP1, CAUSE_TOP3, TRANSLATION_AGREE, REPEATABLE, CALLS, '
                           f'TOKENS, CREDITS, RUN_AT FROM {TABLE} ORDER BY RUN_AT DESC LIMIT ?',
                           params=[int(limit)]).collect()
    except Exception as exc:
        return [], 'NO_ACCURACY_TABLE:' + type(exc).__name__
    keys = ('release', 'model') + RATES + ('calls', 'tokens', 'credits', 'run_at')
    return [dict(zip(keys, list(r))) for r in rows], 'OK'
