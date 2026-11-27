"""X13 / X14: per-sheet reviewer decisions for the deployed app.

review = {'held_back': {sheet: reason}, 'notes': {sheet: note}}
- held back (X14): the sheet is not drawn in the deployed app; a one-line notice with the reason shows instead.
- note (X13): a short known-difference note shows to app users under the sheet.
Decided by a person on the Deploy page (recorded with the deploy approval); never by code or Cortex."""
from copy import deepcopy

MAX_TEXT = 300


def sheet_names(ir):
    """Every worksheet drawn by the workbook's dashboards, in dashboard order, once."""
    out = []
    for d in (ir or {}).get('dashboards') or []:
        for s in d.get('sheets') or []:
            name = s.get('name') if isinstance(s, dict) else None
            if name and name not in out:
                out.append(name)
    return out


def differences(run):
    """sheet -> validation status ('Matched', 'Difference found', ...) from the Tableau value comparison."""
    results = ((run or {}).get('sheet_reference') or {}).get('results') or {}
    return {name: (row[0] or {}).get('Status') for name, row in results.items()}


def validate(review, ir):
    """The cleaned review, or ValueError with the reason."""
    review = review or {}
    extra = set(review) - {'held_back', 'notes'}
    if extra:
        raise ValueError('Unknown review entries: ' + ', '.join(sorted(extra)))
    names = set(sheet_names(ir))
    out = {'held_back': {}, 'notes': {}}
    for kind in ('held_back', 'notes'):
        for name, text in (review.get(kind) or {}).items():
            if name not in names:
                raise ValueError(f"'{name}' is not a sheet of this workbook")
            text = ' '.join(str(text or '').split())
            if len(text) > MAX_TEXT:
                raise ValueError(f"'{name}': keep the text under {MAX_TEXT} characters")
            if not text:
                if kind == 'held_back':
                    raise ValueError(f"'{name}': say why the sheet is held back")
                continue
            out[kind][name] = text
    both = set(out['held_back']) & set(out['notes'])
    if both:
        raise ValueError('A held-back sheet is not shown, so it takes no note: ' + ', '.join(sorted(both)))
    if names and set(out['held_back']) >= names:
        raise ValueError('Every sheet is held back; hold back the workbook instead')
    return out


def apply(ir, review):
    """A copy of the IR with the decisions on every occurrence of each sheet and recorded under ir['review']."""
    review = validate(review, ir)
    out = deepcopy(ir)
    for d in out.get('dashboards') or []:
        for s in d.get('sheets') or []:
            if not isinstance(s, dict):
                continue
            if s.get('name') in review['held_back']:
                s['review_hold'] = review['held_back'][s['name']]
            elif s.get('name') in review['notes']:
                s['review_note'] = review['notes'][s['name']]
    out['review'] = review
    return out
