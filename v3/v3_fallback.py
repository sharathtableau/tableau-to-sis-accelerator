"""Release 25a (G01, "no blank sheet"): draw sheets the V3 contracts refuse with the original accelerator's engine.

The scoped (V3) parse refuses a worksheet it cannot plan exactly (detail/LOD occurrences, several measures on one
axis, table calculations, member aliases, scatter over relationships ...). The original accelerator drew those
sheets from the legacy parse with the same engine renderers. Here, for every refused sheet, the legacy description
of the SAME worksheet (same dashboard, same sheet name) replaces the refused one when it has a registered renderer
and passes the IR contract. It is marked:

  support.status = PARTIAL  (never UNVERIFIED: nothing approximated is ever shown as verified)
  support.reason = 'Approximated with the original engine — not verified against Tableau. V3 could not plan it
                    exactly: <the V3 refusal reason>'
  approximated   = True, v3_refusal = <the V3 refusal reason>

so preview, the app package and validation treat it like any partial sheet, the app shows the approximation note on
the sheet, and Tableau value comparison decides whether it matches. A refused sheet whose legacy description has no
renderer, or fails the contract, stays refused with its reason. No workbook or sheet names are used as logic.
"""
from copy import deepcopy

PREFIX = 'Approximated with the original engine — not verified against Tableau. V3 could not plan it exactly: '
KEEP = ('geom', 'title', 'name')            # layout identity comes from the V3 parse


def refused(sheet):
    support = sheet.get('support') or {}
    return sheet.get('kind') == 'unsupported' or support.get('status') in ('UNSUPPORTED', 'INVALID')


def legacy_lookup(path):
    """(dashboard, sheet) -> the original accelerator's description of every sheet, or None."""
    import tableau_parser
    try:
        legacy = tableau_parser.build_ir(str(path), scoped=False)
    except Exception:
        return None
    lookup = {}
    for d in legacy.get('dashboards') or []:
        for s in d.get('sheets') or []:
            lookup.setdefault((d.get('name'), s.get('name')), s)
    return lookup


def approximation(sheet, old):
    """The labelled original-engine description standing in for a refused V3 sheet, or None."""
    from ir_contract import CHART_HANDLERS, sheet_issues
    if not old or old.get('kind') not in CHART_HANDLERS or old.get('kind') in ('unsupported', 'planned'):
        return None
    reason = sheet.get('v3_refusal') or (sheet.get('support') or {}).get('reason') or 'no exact plan'
    new = deepcopy(old)
    for k in KEEP:
        if sheet.get(k) is not None:
            new[k] = sheet[k]
    new.pop('worksheet_semantics', None)          # V3 pane/table plans do not apply to the legacy description
    new['support'] = {'status': 'PARTIAL', 'reason': PREFIX + reason}
    new['approximated'] = True
    new['v3_refusal'] = reason
    return None if sheet_issues(new) else new


def approximate(ir, path):
    """Replace refused sheets of `ir` (scoped) with their legacy description where the original engine can draw
    them. Returns the list of (dashboard, sheet, legacy kind) approximated. `ir` is changed in place."""
    targets = [(d, i, s) for d in ir.get('dashboards') or [] for i, s in enumerate(d.get('sheets') or []) if refused(s)]
    if not targets:
        return []
    lookup = legacy_lookup(path)
    if lookup is None:
        return []
    done = []
    for dash, i, sheet in targets:
        new = approximation(sheet, lookup.get((dash.get('name'), sheet.get('name'))))
        if new is None:
            continue
        dash['sheets'][i] = new
        done.append((dash.get('name'), sheet.get('name'), new.get('kind')))
    if done:
        ir['approximated_sheets'] = [{'dashboard': d, 'sheet': s, 'kind': k} for d, s, k in done]
    return done
