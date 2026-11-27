"""V5: approximate placement of map locations from the bundled base maps (v3_basemaps).

A verified map plan draws each location at Tableau's own coordinates (taken from the capture). Without them, a country
or US state is placed at the centroid of its largest polygon in the base maps. Drawing only: the rows and values are
never changed, and a location the base maps do not know is left without coordinates (not drawn)."""
import json
import math

_CACHE = {}


def _topo(name):
    import v3_basemaps as B
    t = getattr(B, name)
    return json.loads(t) if isinstance(t, str) else t


def _arcs(topo):
    tr = topo.get('transform') or {}
    sx, sy = (tr.get('scale') or [1, 1])
    tx, ty = (tr.get('translate') or [0, 0])
    out = []
    for arc in topo['arcs']:
        x = y = 0
        pts = []
        for dx, dy in arc:
            if tr:
                x, y = x + dx, y + dy
                pts.append((x * sx + tx, y * sy + ty))
            else:
                pts.append((dx, dy))
        out.append(pts)
    return out


def _ring(indices, arcs):
    pts = []
    for i in indices:
        a = arcs[i] if i >= 0 else list(reversed(arcs[~i]))
        pts.extend(a if not pts else a[1:])
    return pts


def _centroid(ring):
    """(area, (lat, lon)) of a ring (shoelace on lon / lat)."""
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-12:
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        return 0.0, (sum(ys) / len(ys), sum(xs) / len(xs))
    return abs(a) / 2, (cy / (3 * a), cx / (3 * a))


def centroids(name):
    """{lower-case feature name: (lat, lon)} for 'WORLD_COUNTRIES' or 'US_STATES'."""
    if name in _CACHE:
        return _CACHE[name]
    topo = _topo(name)
    arcs = _arcs(topo)
    out = {}
    for obj in topo['objects'].values():
        for g in obj.get('geometries') or []:
            polys = g['arcs'] if g.get('type') == 'MultiPolygon' else [g['arcs']] if g.get('type') == 'Polygon' else []
            best = None
            for poly in polys:
                if not poly:
                    continue
                area, c = _centroid(_ring(poly[0], arcs))
                if best is None or area > best[0]:
                    best = (area, c)
            nm = ((g.get('properties') or {}).get('name') or '').strip().lower()
            if best and nm and all(math.isfinite(v) for v in best[1]):
                out[nm] = best[1]
    _CACHE[name] = out
    return out


def locate(value, kind):
    """(lat, lon) of a country / US state name, or None."""
    import v3_basemaps as B
    key = str(value or '').strip().lower()
    if not key:
        return None
    if kind == 'state':
        return centroids('US_STATES').get(key)
    table = centroids('WORLD_COUNTRIES')
    alias = (B.COUNTRY_ALIASES.get(key) or '').lower()
    return table.get(alias) or table.get(key)


def place(plan, df):
    """The view's rows with __lat / __lon from the base maps (a state view by state, else by country); rows are kept."""
    if '__lat' in df.columns or not any(f.get('kind') == 'geo' for f in plan.get('fields') or []):
        return df
    import v3_sheet_plan as P
    dims = [f for f in plan['fields'] if f.get('kind') == 'dim']
    state = next((d for d in dims if any(w in d['caption'].lower() for w in ('state', 'province'))), None)
    country = next((d for d in dims if d['caption'].lower().startswith('country')), None)
    out = df.copy()
    lat, lon = [], []
    for i in range(len(out)):
        p = None
        if state is not None and P.col_name(state) in out.columns:
            p = locate(out[P.col_name(state)].iloc[i], 'state')
        if p is None and state is None and country is not None and P.col_name(country) in out.columns:
            p = locate(out[P.col_name(country)].iloc[i], 'country')
        lat.append(p[0] if p else None)
        lon.append(p[1] if p else None)
    out['__lat'] = lat
    out['__lon'] = lon
    out.attrs['placement'] = 'approximate'
    return out
