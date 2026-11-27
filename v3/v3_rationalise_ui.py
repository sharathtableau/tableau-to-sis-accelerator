"""Rationalisation tab of the Estate catalogue page (release 22a). Errors show the exception class only."""
import json

import streamlit as st

import v3_rationalise as R

K = 'v3rat-'


def _j(v):
    if v is None or isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return v


def _run(sess, scope):
    import v3_store
    run = R.Run(sess, scope, via='app', who=v3_store.viewer()[0])
    box = st.empty()
    try:
        with st.spinner('Comparing metrics on Snowflake data…'):
            counts = run.execute(progress=lambda t: box.caption(t))
    except Exception as exc:
        box.empty()
        st.error(f'Rationalisation stopped ({type(exc).__name__}). Nothing was changed; run it again.')
        return
    box.empty()
    st.success('Rationalisation finished: ' + ', '.join(f'{R.LABELS[c]} {n}' for c, n in counts.items() if n) + '.')


def render(sess, scope):
    st.caption('Every metric of this scope in exactly one class. Code runs the definitions that should agree on the '
               'same Snowflake data (total, by month, by a shared dimension) and only those numbers decide Equivalent '
               'or Conflicting. Cortex suggests which metrics mean the same, explains conflicts and recommends one '
               'definition; a person decides what is certified (release 23).')
    if not R.ready(sess):
        st.warning('The rationalisation tables are not set up yet: run the release 22 set-up SQL in Snowsight, then '
                   'harvest this project again (it records the Snowflake relation of each datasource).')
        return
    if st.button('Run rationalisation for this scope', type='primary', key=K + 'run-' + scope):
        _run(sess, scope)
    try:
        found = R.latest(sess, scope)
    except Exception as exc:
        st.error(f'Rationalisation could not be read ({type(exc).__name__}).')
        return
    if not found:
        st.info('No rationalisation for this scope yet.')
        return
    run, metrics, groups = found
    st.caption(f"Latest run {run.get('RUN_ID')} · {str(run.get('RECORDED_AT'))[:19]} · {run.get('METRICS')} metrics · "
               f"{run.get('GROUPS_COMPARED')} groups compared")
    counts = metrics['CLASS'].value_counts()
    cols = st.columns(len(R.CLASSES))
    for col, c in zip(cols, R.CLASSES):
        col.metric(R.LABELS[c], int(counts.get(c, 0)))
    st.download_button('Download the report (Excel)', R.excel(run, metrics, groups),
                       file_name=f"rationalisation_{scope.replace(' ', '_')}.xlsx", key=K + 'xlsx',
                       mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    names = dict(zip(metrics['METRIC_FP'], metrics['NAME']))
    forms = dict(zip(metrics['METRIC_FP'], metrics['EXPRESSION']))
    for title, verdicts in (('Conflicts', ('conflicting',)), ('Equivalent definitions', ('equivalent',)),
                            ('Cortex suggested the same meaning; numbers differ (review)', ('differs',)),
                            ('Could not be compared yet', ('not_testable',))):
        part = groups[groups['VERDICT'].isin(verdicts)] if not groups.empty else groups
        if part.empty:
            continue
        st.markdown(f'**{title}** ({len(part)})')
        for _i, g in part.iterrows():
            members = _j(g['MEMBERS']) or []
            with st.expander(f"{g['GROUP_ID']} · {g['NAME']} · {len(members)} definitions · {g['SOURCE']}"):
                for fp in members:
                    tag = '  ← recommended by Cortex' if g.get('RECOMMENDED_FP') == fp else ''
                    st.markdown(f"- `{forms.get(fp) or fp}` ({names.get(fp, fp)}){tag}")
                if g.get('RELATION'):
                    st.caption(f"Run on {g['RELATION']} · " + ', '.join(_j(g.get('GRAINS')) or []))
                diffs = _j(g.get('DIFFERENCES')) or []
                if diffs:
                    rows = [dict({'Grain': d.get('grain'), 'Key': d.get('key')},
                                 **{names.get(fp, fp)[:40] + f' ({n + 1})': d.get('values', {}).get(fp)
                                    for n, fp in enumerate(members)}) for d in diffs]
                    st.dataframe(rows, use_container_width=True, hide_index=True)
                if g.get('EXPLANATION'):
                    st.markdown('**Cortex:** ' + str(g['EXPLANATION']) +
                                (f" Recommendation: {g.get('CORTEX_REASON')}" if g.get('CORTEX_REASON') else ''))
                elif g.get('CODE_CHECK') and g.get('CORTEX_PASSED_CHECK') is False:
                    st.caption("Cortex's explanation did not pass code's check and is not shown.")
                if g.get('SOURCE') == 'cortex' and g.get('CORTEX_REASON'):
                    st.caption('Why Cortex grouped them: ' + str(g['CORTEX_REASON']))
                if g.get('REASON'):
                    st.caption(str(g['REASON']))
    pick = st.multiselect('Show classes', [R.LABELS[c] for c in R.CLASSES], default=[R.LABELS[c] for c in R.CLASSES],
                          key=K + 'classes')
    table = metrics.assign(Class=metrics['CLASS'].map(R.LABELS))
    table = table[table['Class'].isin(pick)].sort_values(['CLASS', 'WORKBOOKS', 'NAME'], ascending=[True, False, True])
    st.dataframe(table[['NAME', 'Class', 'REASON', 'WORKBOOKS', 'SHEETS', 'TABLEAU_VIEWS', 'EXPRESSION', 'GROUP_ID',
                        'METRIC_FP']], use_container_width=True, hide_index=True)
