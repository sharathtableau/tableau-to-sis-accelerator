"""Release 26c: automatic layout check, run for every migration (no screenshot, no Tableau call needed).

The workbook itself defines the expected layout: every dashboard zone has an authored rectangle (x, y, w, h in
Tableau units, dashboard = 100000 x 100000). The app draws the dashboard from the layout tree the parser builds
from those zones, with the engine's sizing rules (columns split by zone width; zones stacked with their zone
height; controls, text and titles at Streamlit's own heights). This module computes where each zone lands with
those same rules and compares it with the authored rectangle:

  Placed     the zone is drawn, in its Tableau position and size (within tolerance)
  Moved      drawn, but its position or size differs more than the tolerance (the difference is reported)
  Not drawn  an authored zone that the app does not draw (with the reason)

Nothing here changes the app; it reports. Names of workbooks, dashboards or sheets are never used as logic.
"""
TOL_W = 0.04        # horizontal: share of the dashboard width
TOL_H = 0.08        # vertical: share of the dashboard height
CONTROL_PX = 74     # a Streamlit selectbox / date control with its label
TEXT_PX = 30
TITLE_PX = 46
SHEET_TITLE_PX = 36
def canvas_zone_px(canvas):
    """Same rule as engine.canvas_zone_px (kept equal by tests/test_v3_layout_check.py)."""
    canvas = canvas or {}
    lo, hi = canvas.get('minheight'), canvas.get('maxheight')
    if isinstance(hi, int) and hi > 0 and lo == hi:
        return hi / 100000
    w, h = canvas.get('minwidth'), canvas.get('minheight')
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        return max(w, 1100) * (h / w) / 100000
    return 0.011


LEAVES = ('sheet', 'control', 'legend', 'title', 'text', 'blank', 'image', 'nav', 'toggle', 'unsupported_zone')


def _kind(node):
    return next((k for k in LEAVES if k in node), None)


def _label(node):
    k = _kind(node)
    if k == 'sheet':
        return node['sheet']
    if k == 'control':
        return f"{node.get('control')} control: {node.get('caption')}"
    if k == 'legend':
        return f"{node.get('legend_type') or 'color'} legend: {node.get('legend')}"
    if k == 'text':
        return 'text: ' + str(node.get('text'))[:40]
    if k == 'image':
        return 'image: ' + str(node.get('image'))
    if k == 'unsupported_zone':
        return str(node.get('unsupported_zone'))
    if k == 'nav':
        return 'button: ' + str(node.get('caption'))
    if k == 'toggle':
        return 'show/hide button: ' + str(node.get('caption'))
    return k


def _leaf_px(node, zone_px):
    """Height the app gives a leaf (the engine's own sizing rules)."""
    k = _kind(node)
    raw = int((node.get('h') or 0) * zone_px)
    if k == 'sheet':
        return max(48, min(1600, raw - SHEET_TITLE_PX)) + SHEET_TITLE_PX
    if k == 'control':
        return max(60, raw)             # the app keeps the control zone's authored height
    if k == 'title':
        return max(TITLE_PX, raw)       # the engine keeps the zone's authored height (min-height)
    if k == 'text':
        return max(TEXT_PX, raw)
    if k in ('nav', 'toggle'):
        return max(16, raw)
    if k == 'blank':
        return min(raw, 400) if raw >= 8 else 0
    if k == 'legend':
        return max(60, raw)
    if k == 'image':
        return raw
    return 0


def _hidden_leaves(node):
    if _kind(node):
        return [node]
    return [x for k in node.get('children') or [] for x in _hidden_leaves(k)] + \
        [x for f in node.get('floating') or [] for x in _hidden_leaves(f['child'])]


def _place(node, x, y, w, zone_px, out):
    """Lay out `node` at (x, y) with width w (px); return its height (px). Leaves are appended to out."""
    if node.get('zid') is not None and node.get('start_hidden'):
        for leaf in _hidden_leaves(node):            # Tableau hides it too until its show/hide button is used
            out.append((leaf, None))
        return 0
    k = _kind(node)
    if k:
        h = _leaf_px(node, zone_px)
        out.append((node, (x, y, w, h)))
        return h
    kids = node.get('children') or []
    if not kids:
        return 0
    if node.get('dir') == 'horz' and len(kids) > 1:
        weights = [max(1, k_.get('w', 1)) for k_ in kids]
        total = sum(weights)
        cx, tallest = x, 0
        for child, wt in zip(kids, weights):
            cw = w * wt / total
            tallest = max(tallest, _place_node(child, cx, y, cw, zone_px, out))
            cx += cw
        return tallest
    cy = y
    for child in kids:
        cy += _place_node(child, x, cy, w, zone_px, out)
    return cy - y


def _place_node(node, x, y, w, zone_px, out):
    h = _place(node, x, y, w, zone_px, out)
    if isinstance(node.get('backdrop'), dict):          # an image drawn as the container's background
        out.append((node['backdrop'], (x, y, w, h)))
    for f in node.get('floating') or []:                # floating zones: drawn at their rectangle in the layer
        fx = x + w * (f['x'] - f['rx']) / max(1, f['rw'])
        fy = y + (f['y'] - f['ry']) * zone_px
        _place_node(f['child'], fx, fy, w * f['w'] / max(1, f['rw']), zone_px, out)
    return h


def _canvas_rows(dash):
    """Release 26f: the app pins every zone at its own XML rectangle, so position and size equal Tableau's by
    construction; what remains to report is a zone the app cannot draw."""
    rows = []
    images = dash.get('_images')
    for z in dash.get('canvas_zones') or []:
        kind = z.get('kind')
        if kind == 'box':
            continue
        label = {'sheet': z.get('sheet'), 'control': f"{z.get('control')} control: {z.get('caption')}",
                 'legend': f"{z.get('legend_type')} legend: {z.get('legend')}", 'image': f"image: {z.get('image')}",
                 'nav': f"button: {z.get('caption') or z.get('nav')}", 'toggle': 'show/hide button',
                 'unsupported': str(z.get('what'))}.get(kind, kind)
        rect = f"x {z['x'] / 1000:.0f}% y {z['y'] / 1000:.0f}% w {z['w'] / 1000:.0f}% h {z['h'] / 1000:.0f}%"
        status, note = 'Placed', 'Drawn at its XML rectangle'
        if kind == 'unsupported':
            status, note = 'Not drawn', f"Tableau {z.get('what')} zone"
        elif kind == 'image' and images is not None and not images.get(z.get('image')):
            status, note = 'Not drawn', 'image not packaged with the workbook'
        elif any(h.get('start_hidden') for h in z.get('hide_with') or []):
            note = 'Hidden at open in Tableau too; its show/hide button shows it'
        rows.append({'Dashboard': dash.get('name'), 'Zone': label, 'Status': status, 'Tableau': rect,
                     'App': rect if status == 'Placed' else '-', 'Note': note})
    return rows


def check_dashboard(dash, page_px=1100):
    """Rows {Dashboard, Zone, Status, Tableau, App, Note} for one dashboard."""
    if dash.get('canvas_zones') and any(z.get('kind') == 'sheet' for z in dash['canvas_zones']):
        return _canvas_rows(dash)
    layout = dash.get('layout')
    if not layout:
        sheets = dash.get('sheets') or []
        if len(sheets) == 1:                # a published worksheet: the sheet is the whole view
            return [{'Dashboard': dash.get('name'), 'Zone': sheets[0].get('name'), 'Status': 'Placed',
                     'Tableau': 'whole view', 'App': 'whole page', 'Note': 'Worksheet view'}]
        return [{'Dashboard': dash.get('name'), 'Zone': '(dashboard)', 'Status': 'Moved',
                 'Note': 'No zone layout in the workbook; sheets are placed by their sheet geometry'}]
    canvas = dash.get('canvas') or {}
    zone_px = canvas_zone_px(canvas)
    width = canvas.get('maxwidth') if canvas.get('maxwidth') and canvas.get('maxwidth') == canvas.get('minwidth') else None
    page = width or max(canvas.get('minwidth') or 0, page_px)
    placed = []
    total_h = _place_node(layout, 0.0, 0.0, float(page), zone_px, placed) or 1
    # the authored dashboard rectangle (the tree's outer zones)
    leaves = [n for n, b in placed if b is not None and all(k in n for k in ('x', 'y', 'w', 'h'))]
    if not leaves:
        return []
    x0 = min(n['x'] for n in leaves)
    y0 = min(n['y'] for n in leaves)
    x1 = max(n['x'] + n['w'] for n in leaves)
    y1 = max(n['y'] + n['h'] for n in leaves)
    if all(k in layout for k in ('x', 'y', 'w', 'h')):      # the dashboard's own rectangle, when the tree has it
        x0, y0 = min(x0, layout['x']), min(y0, layout['y'])
        x1, y1 = max(x1, layout['x'] + layout['w']), max(y1, layout['y'] + layout['h'])
    aw, ah = max(1, x1 - x0), max(1, y1 - y0)
    rows = []
    for node, box in placed:
        k = _kind(node)
        if box is None:
            rows.append({'Dashboard': dash.get('name'), 'Zone': _label(node), 'Status': 'Placed',
                         'Tableau': 'hidden at open', 'App': 'hidden at open',
                         'Note': 'Hidden at open in Tableau too; its show/hide button shows it'})
            continue
        x, y, w, h = box
        if not all(key in node for key in ('x', 'y', 'w', 'h')):
            continue
        tx, ty = (node['x'] - x0) / aw, (node['y'] - y0) / ah
        tw, th = node['w'] / aw, node['h'] / ah
        ax, ay, aw_, ah_ = x / page, y / total_h, w / page, h / total_h
        notes = []
        if abs(ax - tx) > TOL_W:
            notes.append(f'x {ax:.0%} vs Tableau {tx:.0%}')
        if abs(aw_ - tw) > TOL_W:
            notes.append(f'width {aw_:.0%} vs Tableau {tw:.0%}')
        if abs(ay - ty) > TOL_H:
            notes.append(f'y {ay:.0%} vs Tableau {ty:.0%}')
        if abs(ah_ - th) > TOL_H and k in ('sheet', 'legend', 'image'):
            notes.append(f'height {ah_:.0%} vs Tableau {th:.0%}')
        status = 'Placed' if not notes else 'Moved'
        if k == 'unsupported_zone':
            status, notes = 'Not drawn', [f"Tableau {node.get('unsupported_zone')} zone"]
        if k == 'image' and not (dash.get('_images') or {}).get(node.get('image')):
            status, notes = ('Not drawn', ['image not packaged with the workbook']) if dash.get('_images') is not None \
                else (status, notes)
        rows.append({'Dashboard': dash.get('name'), 'Zone': _label(node), 'Status': status,
                     'Tableau': f'x {tx:.0%} y {ty:.0%} w {tw:.0%} h {th:.0%}',
                     'App': f'x {ax:.0%} y {ay:.0%} w {aw_:.0%} h {ah_:.0%}',
                     'Note': '; '.join(notes)})
    return rows


def check_ir(ir):
    """Every dashboard of a migration. Returns (rows, summary dict)."""
    rows = []
    for d in ir.get('dashboards') or []:
        rows += check_dashboard(dict(d, _images=ir.get('images') or {}))
    counts = {s: sum(1 for r in rows if r['Status'] == s) for s in ('Placed', 'Moved', 'Not drawn')}
    return rows, dict(counts, zones=len(rows))
