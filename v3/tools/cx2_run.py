"""CX2 Cortex visual review runner (developer tool; advisory only -- the owner approves every fix).

usage:
  python -X utf8 tools/cx2_run.py "<abs workbook path>" "<dashboard>" [--luid <Tableau workbook LUID>]
         [--server https://prod-useast-b.online.tableau.com] [--site b360bi] [--model claude-sonnet-4-5]
         [--max-zones 20] [--sf] [--port 8601] [--out reports/cx2] [--no-cortex] [--tableau-png file]

Fully automatic:
  1. Tableau side: REST sign-in with the PAT (env TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET or the SiS secret; never
     printed), finds the workbook (by --luid, else by name) and the view named like the dashboard, pulls its image
     at the dashboard's authored size.
  2. App side: the local preview (tools/preview_app.py on --port; started if down) -> canvas screenshot + zone
     geometry + rendered text + rendered-check issues per zone.
  3. Per zone (largest first, up to --max-zones; container boxes that hold a sheet skipped): Tableau crop | app
     crop side by side -> stage @WBR_DB.CX_VISUAL_REVIEW.CX2_IMAGES/<run>/ -> AI_COMPLETE(model, text, TO_FILE(...)).
  4. cx2_visual_review.review (the same core the in-app panel runs: CX9 token cap, injection guard, per-call
     cost) / corpus_reach / fix_queue -> findings.json + report.html in --out/<run>/; evaluation rows appended
     to --out/_evaluation.jsonl (owner labels them later).
"""
import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import cx2_visual_review as cx2  # noqa: E402

STAGE = '@WBR_DB.CX_VISUAL_REVIEW.CX2_IMAGES'
SKIP_KINDS = {'blank', 'image', 'web', 'bitmap'}


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- workbook facts (zones, XML per zone)
def workbook_facts(path, dashboard):
    from v3_local import prepare_payload
    raw = Path(path).read_bytes()
    ir = prepare_payload(raw, Path(path).name, published_views=[dashboard])['ir']
    dash = next((d for d in ir['dashboards'] if d.get('name') == dashboard), None)
    if not dash:
        raise SystemExit(f'dashboard not found in workbook: {dashboard}')
    if Path(path).suffix.lower() == '.twbx':
        with zipfile.ZipFile(path) as z:
            twb = z.read(next(n for n in z.namelist() if n.lower().endswith('.twb'))).decode('utf-8', 'replace')
    else:
        twb = Path(path).read_text(encoding='utf-8', errors='replace')
    root = ET.fromstring(twb)
    ws_xml = {w.get('name'): ET.tostring(w, encoding='unicode') for w in root.iter('worksheet')}
    d_el = next((d for d in root.iter('dashboard') if d.get('name') == dashboard), None)
    zone_xml = {z.get('id'): ET.tostring(z, encoding='unicode') for z in (d_el.iter('zone') if d_el is not None else [])}
    from engine import canvas_size
    W, H = canvas_size(dash.get('canvas'))
    zones = []
    for i, z in enumerate(dash.get('canvas_zones') or []):
        if not isinstance(z, dict):
            continue
        label = z.get('sheet') or z.get('name') or z.get('kind') or 'zone'
        xml = (ws_xml.get(z.get('sheet')) or '') + (zone_xml.get(str(z.get('id'))) or '')
        zones.append({'key': f'v3cz{i}', 'id': z.get('id'), 'kind': z.get('kind'), 'label': f"{z.get('kind')}: {label}",
                      'xml': xml})
    return dash, W, H, zones


# ---------------------------------------------------------------- Tableau image over REST
def tableau_image(args, workbook_name, dashboard, W, H, out_png):
    import tableau_server as ts
    name, secret = ts._resolve_pat(None, None)
    errs = []
    for version in ('3.24', ts.DEFAULT_API_VERSION):        # newer API honours vizWidth/vizHeight
        client = ts.TableauRestClient(args.server, api_version=version)
        try:
            client.sign_in_with_pat(name, secret, args.site)
            luid = args.luid
            if not luid:
                hits = [w for w in client.list_workbooks() if w['name'] == workbook_name]
                if len(hits) != 1:
                    raise SystemExit(f'{len(hits)} Tableau workbooks named "{workbook_name}"; pass --luid one of: '
                                     + ', '.join(f"{w['id']} ({w.get('project_name')})" for w in hits))
                luid = hits[0]['id']
            views = client.list_views(luid)
            view = next((v for v in views if v['name'] == dashboard), None)
            if not view:
                raise SystemExit(f'no published view "{dashboard}" in workbook {luid}: ' + ', '.join(v['name'] for v in views))
            png = client.query_view_image(view['id'], high_resolution=True, viz_width=W, viz_height=H)
            Path(out_png).write_bytes(png)
            return {'workbook_luid': luid, 'view_id': view['id'], 'api': version}
        except SystemExit:
            raise
        except Exception as exc:                                  # never print service text (may echo tokens)
            errs.append(f'{version}: {type(exc).__name__}')
        finally:
            try:
                client.sign_out()
            except Exception:
                pass
    raise SystemExit('Tableau image unavailable (' + '; '.join(errs) + ')')


# ---------------------------------------------------------------- app canvas, geometry, text, issues
def ensure_preview(port):
    try:
        urlopen(f'http://127.0.0.1:{port}/_stcore/health', timeout=5)
        return
    except Exception:
        pass
    log('starting preview on', port)
    subprocess.Popen([sys.executable, '-m', 'streamlit', 'run', str(ROOT / 'tools' / 'preview_app.py'), '--server.port',
                      str(port), '--server.headless', 'true', '--server.address', '127.0.0.1'], cwd=str(ROOT),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(90):
        try:
            urlopen(f'http://127.0.0.1:{port}/_stcore/health', timeout=5)
            return
        except Exception:
            time.sleep(2)
    raise SystemExit('preview did not start')


def app_capture(workbook, dashboard, sf, port, out_png):
    from playwright.sync_api import sync_playwright
    from rendered_check import MEASURE
    url = f'http://localhost:{port}/?wb={quote(workbook)}&d={quote(dashboard)}' + ('&sf=1' if sf else '')
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={'width': 2000, 'height': 1600})
        for attempt in range(2):                                   # first run after a restart can be cold
            pg.goto(url, timeout=600000)
            time.sleep(3)
            for _ in range(600):
                if not pg.query_selector('[data-testid="stStatusWidget"]'):
                    break
                time.sleep(1)
            time.sleep(4)
            if pg.query_selector('div.st-key-v3canvas'):
                break
        else:
            b.close()
            raise SystemExit('the app drew no canvas for this dashboard')
        rows = pg.evaluate(MEASURE)
        texts = pg.evaluate("""() => Object.fromEntries([...document.querySelectorAll('[class*="st-key-v3cz"]')].map(z =>
            [(z.className.match(/st-key-(v3cz[\\w-]+)/) || [])[1], z.innerText.slice(0, 4000)]))""")
        pg.evaluate("""() => { let e = document.querySelector('div.st-key-v3canvas');
            while (e && e !== document.body) { e.style.overflow = 'visible'; e.style.maxWidth = 'none'; e = e.parentElement; } }""")
        box = pg.evaluate("() => { const r = document.querySelector('div.st-key-v3canvas').getBoundingClientRect(); return {x: r.left, y: r.top}; }")
        # geometry measured BEFORE the overflow change; re-measure positions relative to the canvas now
        pos = pg.evaluate("""() => { const c = document.querySelector('div.st-key-v3canvas').getBoundingClientRect();
            return Object.fromEntries([...document.querySelectorAll('[class*="st-key-v3cz"]')].map(z => { const r = z.getBoundingClientRect();
            return [(z.className.match(/st-key-(v3cz[\\w-]+)/) || [])[1], [r.left - c.left, r.top - c.top, r.width, r.height]]; })); }""")
        pg.locator('div.st-key-v3canvas').first.screenshot(path=out_png)
        b.close()
    geo = {}
    for r in rows:
        geo[r['zone']] = {'issues': r['issues'], 'rect': pos.get(r['zone']), 'dom_text': texts.get(r['zone'], '')}
    return geo, box


# ---------------------------------------------------------------- Cortex session
def cortex_session():
    from snowflake.snowpark import Session
    return Session.builder.config('connection_name', os.environ.get('V3_PREVIEW_CONNECTION', 'wbr')).create()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('workbook'); ap.add_argument('dashboard')
    ap.add_argument('--luid'); ap.add_argument('--server', default='https://prod-useast-b.online.tableau.com')
    ap.add_argument('--site', default='b360bi'); ap.add_argument('--model', default=cx2.DEFAULT_MODEL)
    ap.add_argument('--max-zones', type=int, default=20); ap.add_argument('--sf', action='store_true')
    ap.add_argument('--port', type=int, default=8601); ap.add_argument('--out', default=str(ROOT / 'reports' / 'cx2'))
    ap.add_argument('--no-cortex', action='store_true'); ap.add_argument('--tableau-png')
    ap.add_argument('--budget', type=int, default=None, help='CX9 input-token cap for this run')
    a = ap.parse_args()
    from PIL import Image
    run = time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
    slug = re.sub(r'[^A-Za-z0-9]+', '_', Path(a.workbook).stem + '_' + a.dashboard).strip('_')[:60]
    out = Path(a.out) / f'{slug}_{run}'
    (out / 'zones').mkdir(parents=True, exist_ok=True)
    log('workbook facts ...')
    dash, W, H, zones = workbook_facts(a.workbook, a.dashboard)
    log(f'canvas {W}x{H}, {len(zones)} zones')
    meta = {'version': cx2.VERSION, 'run': run, 'workbook': Path(a.workbook).name, 'dashboard': a.dashboard,
            'canvas': [W, H], 'model': a.model}
    if a.tableau_png:
        shutil.copy(a.tableau_png, out / 'tableau.png'); meta['tableau'] = {'source': 'file'}
    else:
        log('Tableau image over REST ...')
        wb_name = Path(a.workbook).stem
        meta['tableau'] = tableau_image(a, wb_name, a.dashboard, W, H, out / 'tableau.png')
    ensure_preview(a.port)
    log('app canvas ...')
    geo, _ = app_capture(a.workbook, a.dashboard, a.sf, a.port, str(out / 'app.png'))
    tab, app = Image.open(out / 'tableau.png').convert('RGB'), Image.open(out / 'app.png').convert('RGB')
    meta['sizes'] = {'tableau': tab.size, 'app': app.size}
    cand = []
    for z in zones:
        g = geo.get(z['key'])
        if not g or not g.get('rect') or z['kind'] in SKIP_KINDS:
            continue
        x, y, w, h = g['rect']
        if w < 30 or h < 16:
            continue
        cand.append({**z, **g, 'area': w * h})
    before = len(cand)
    cand = cx2.drop_container_boxes(cand)
    log(f'{before - len(cand)} container box zone(s) holding a sheet skipped')
    cand.sort(key=lambda z: (-bool(z['issues']), -z['area']))
    cand = cand[:a.max_zones]
    for z in cand:
        cx2.pair_image(tab, app, z['rect'], W, H).save(out / 'zones' / f"{z['key']}.png")
    log(f'{len(cand)} zone pairs built')
    findings, calls = [], []
    if not a.no_cortex:
        session = cortex_session()
        stage_dir = run
        tmp = Path(tempfile.mkdtemp(prefix='cx2_', dir=os.environ.get('SystemDrive', 'C:') + '\\' if os.name == 'nt' else None))
        try:
            for z in cand:                                          # space-free path for PUT
                shutil.copy(out / 'zones' / f"{z['key']}.png", tmp / f"{z['key']}.png")
            session.file.put(str(tmp / '*.png').replace('\\', '/'), f'{STAGE}/{stage_dir}', auto_compress=False, overwrite=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        import cx2_safety
        budget = cx2_safety.Budget(a.budget or cx2_safety.DEFAULT_BUDGET_TOKENS)
        for z in cand:
            z['png'] = (out / 'zones' / f"{z['key']}.png").read_bytes()
        findings, calls, guarded = cx2.review(session, a.model, stage_dir, cand, budget, log=log)
        for z in cand:
            z.pop('png', None)
        meta['cx9'] = {'version': cx2_safety.VERSION, **budget.summary(), 'guarded_findings': len(guarded)}
        session.close()
    from corpus_runner import corpus
    books = corpus(None)
    reach = cx2.corpus_reach({f['construct'] for f in findings if f.get('construct')}, books,
                             cache_path=Path(a.out) / '_corpus_construct_cache.json')
    queue = cx2.fix_queue(findings, reach, len(books))
    meta.update({'calls': calls, 'findings': findings, 'fix_queue': queue,
                 'zones': [{k: z[k] for k in ('key', 'label', 'rect', 'issues')} for z in cand]})
    import cx2_safety
    meta['evaluation'] = cx2_safety.evaluation_rows(run, meta['workbook'], a.dashboard, findings)
    with open(Path(a.out) / '_evaluation.jsonl', 'a', encoding='utf-8') as ev:   # the CX9 evaluation set
        for r in meta['evaluation']:
            ev.write(json.dumps(r, ensure_ascii=False) + '\n')
    (out / 'findings.json').write_text(json.dumps(meta, indent=1, ensure_ascii=False, default=list), encoding='utf-8')
    write_html(out, meta)
    counts = {}
    for f in findings:
        counts[f['verdict']] = counts.get(f['verdict'], 0) + 1
    log(f'\nCX2 {a.dashboard}: {len(findings)} findings {counts}; fix queue {len(queue)} rule(s); report: {out / "report.html"}')
    for g in queue[:12]:
        log(f"  [{g['category']}] {g['construct']}  zones={len(g['zones'])}  reach={g['reach']}/{g['corpus']}  {g['note']}")


def write_html(out, meta):
    e = lambda s: html.escape(str(s if s is not None else ''))
    rows = ''.join(
        f"<tr class='{e(f['verdict'])}'><td>{e(f['zone_label'])}</td><td>{e(f['category'])}</td><td>{e(f['severity'])}</td>"
        f"<td>{e(f['tableau'])}</td><td>{e(f['app'])}</td><td><b>{e(f['verdict'])}</b><br><small>{e(f['evidence'])}</small></td>"
        f"<td>{e(f['construct'])}</td><td><small>{e(f['xml_hint'])}</small></td></tr>" for f in meta['findings'])
    q = ''.join(f"<tr><td>{e(g['category'])}</td><td>{e(g['construct'])}</td><td>{len(g['zones'])}</td>"
                f"<td>{g['reach']}/{g['corpus']}</td><td>{'general' if g['general'] else e(g['note'])}</td>"
                f"<td>{'<br>'.join(e(x['tableau']) + ' &rarr; ' + e(x['app']) for x in g['examples'])}</td></tr>" for g in meta['fix_queue'])
    imgs = ''.join(f"<figure><img src='zones/{e(z['key'])}.png'><figcaption>{e(z['key'])} {e(z['label'])}</figcaption></figure>"
                   for z in meta['zones'])
    page = f"""<!doctype html><meta charset=utf-8><title>CX2 visual review</title>
<style>body{{font:13px system-ui;margin:16px;background:#fff;color:#222}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ddd;padding:4px;vertical-align:top}}.ADVISORY{{color:#888}}.CONFIRMED_GEOMETRY td:nth-child(6),
.CONFIRMED_DOM td:nth-child(6){{background:#fde2e1}}.XML_BACKED td:nth-child(6){{background:#fff4d6}}
figure{{display:inline-block;margin:6px;max-width:48%}}figure img{{max-width:100%;border:1px solid #ccc}}</style>
<h1>CX2 visual review: {e(meta['workbook'])} / {e(meta['dashboard'])}</h1>
<p>Run {e(meta['run'])}, model {e(meta['model'])}. Cortex proposes; verdicts come only from measured geometry, rendered
text and the workbook XML. ADVISORY rows are never acted on. Every fix needs the owner's approval.</p>
<h2>Fix queue (general rules, ranked)</h2><table><tr><th>Category</th><th>XML construct</th><th>Zones</th>
<th>Corpus reach</th><th>Scope</th><th>Examples (Tableau &rarr; app)</th></tr>{q}</table>
<h2>Findings</h2><table><tr><th>Zone</th><th>Category</th><th>Sev.</th><th>Tableau</th><th>App</th><th>Verdict</th>
<th>Construct</th><th>Model's XML hint</th></tr>{rows}</table>
<h2>Whole dashboards</h2><figure><img src='tableau.png'><figcaption>Tableau</figcaption></figure>
<figure><img src='app.png'><figcaption>App</figcaption></figure><h2>Zone pairs</h2>{imgs}"""
    (out / 'report.html').write_text(page, encoding='utf-8')


if __name__ == '__main__':
    main()
