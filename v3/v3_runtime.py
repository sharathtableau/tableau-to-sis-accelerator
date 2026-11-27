"""Standalone candidate uses the same scoped binding boundary and view as V3 preview."""
import streamlit as st
import backend
import engine
from ir_contract import require_valid_ir
from v3_local import preview_context


ASK = 'Ask the data'


def _dashboard_navigation(dashboards, extra=()):
    # Selected-view execution, as in the console Preview: only the chosen dashboard queries.
    labels = [d.get('name') or engine.display_title(d.get('title')) or 'Dashboard' for d in dashboards] + list(extra)
    engine.DASH_NAV = ('v3-standalone-dashboard', [d.get('name') for d in dashboards])
    with st.container(key='v3_dashboard_navigation'):
        st.markdown("<style>.st-key-v3_dashboard_navigation [role=radiogroup]{gap:18px;border-bottom:1px solid #e5e7eb;}"
                    ".st-key-v3_dashboard_navigation label[data-baseweb=radio]{padding:8px 0;border-bottom:2px solid transparent;}"
                    ".st-key-v3_dashboard_navigation label[data-baseweb=radio]>div:first-child{display:none;}"
                    ".st-key-v3_dashboard_navigation label:has(input:checked){border-bottom-color:#3366ff;}"
                    ".st-key-v3_dashboard_navigation label:has(input:checked) p{color:#3366ff;}"
                    "</style>", unsafe_allow_html=True)
        return st.radio('Dashboard', list(range(len(labels))), format_func=lambda i: labels[i],
                        horizontal=True, key='v3-standalone-dashboard', label_visibility='collapsed')


def render_workbook(ir, ask_bindings=None, ask=None):
    """The verified console Preview view: reset, one dashboard at a time, findings.

    With ask_bindings carrying a verified Semantic View, an extra 'Ask the data' tab runs
    Cortex Analyst on it (deployed app)."""
    import parameter_actions
    import dashboard_actions
    dashboards = ir.get('dashboards', [])
    engine.DASH_NAV = None
    extra = []
    if ask_bindings or ask:
        import v3_ask
        if v3_ask.semantic_plans(ask_bindings, ask):
            extra = [ASK]
    selected = _dashboard_navigation(dashboards, extra) if len(dashboards) + len(extra) > 1 else 0
    if selected >= len(dashboards):
        import v3_ask
        v3_ask.render(backend._active_session(), ask_bindings, key='v3-app-ask', ask=ask)
        return
    dashboard_actions.compact_controls(st)
    st.button('Reset workbook controls', key='v3-standalone-reset',
              on_click=parameter_actions.reset, args=(parameter_actions.context(engine),))
    if dashboards:
        engine.render_dashboard(dashboards[selected], presentation='native')
    engine._render_findings()


def run_candidate(ir,bindings,ask=None,account=None):
    from v3_package import validate_bindings, validate_ask
    require_valid_ir(ir)
    validate_bindings(ir,bindings)
    if ask:
        validate_ask(bindings,ask)
    if backend._active_session() is None:
        st.error('This candidate requires a Snowflake session and explicit Snowflake data bindings.')
        st.stop()
    if account:
        # E14: the bindings were verified in one account; the same names in another account are other data
        from v3_source_binding import account_host
        try:
            here = account_host(backend._active_session())
        except Exception as exc:
            here = 'UNKNOWN:' + type(exc).__name__
        if here != account:
            st.error('BINDING_ACCOUNT_MISMATCH: this app was verified against data in another Snowflake account; '
                     'migrate the workbook again in this account.')
            st.stop()
    dashboards = ir.get('dashboards', [])
    title = engine.display_title(ir.get('app_title') or (dashboards[0].get('title') if dashboards else None)) or 'Dashboard'
    st.set_page_config(page_title=title, layout='wide')
    with preview_context(ir,bindings,snowflake=True):
        st.title(title)
        st.caption('Migrated from Tableau with the V3 accelerator · Tableau parity and complete '
                   'migration acceptance remain unverified.')
        render_workbook(ir, ask_bindings=bindings, **({'ask': ask} if ask else {}))
