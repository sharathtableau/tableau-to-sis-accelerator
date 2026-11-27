"""L1: measure a migrated dashboard as a browser draws it -- every canvas zone against its content -- so layout faults
(a gap beside the canvas, a scroll bar Tableau does not show, a chart drawn 0 px wide or cut off) are found by a
check, not by eye. Local preview data only; nothing is written.

  python tools/layout_audit.py <workbook.twbx> <dashboard> [page width px] [--python <python for streamlit>]

Prints JSON: page and canvas widths and, per zone, its size, its content size, its charts' drawn sizes and
'problems': GAP (canvas narrower than 95 % of an Automatic / Range dashboard's page), CHART_EMPTY (a chart drawn
0 px wide or tall), CUT_OFF (Entire View / Fit Height content taller than the zone), SCROLL_X (a zone wider than
itself), ACTIONS_OVERSIZE (the chart's menu button drawn larger than a button). A Standard-fit sheet or a quick-filter list longer than its zone scrolls, as in Tableau (not a problem).
Needs Python Playwright with Chromium (the cloud workspace has it)."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

APP = r'''
import os, sys
ROOT = os.environ['LA_ROOT']
sys.path[:0] = [ROOT, os.path.join(ROOT, 'tools')]
os.chdir(ROOT)
import streamlit as st
st.set_page_config(layout='wide')
@st.cache_resource
def load(wb):
    import plan_census as PC
    return PC.intake(wb)
r = load(os.environ['LA_WB'])
import engine
from v3_local import preview_context
d = next(x for x in r['ir']['dashboards'] if x['name'] == os.environ['LA_DASH'])
with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
    engine.render_dashboard(d)
'''

PROBE = """() => {
  const main = document.querySelector('.st-key-v3canvaswrap') || document.querySelector('[data-testid=stMainBlockContainer]');
  const canv = document.querySelector('.st-key-v3canvas');
  const zones = [...document.querySelectorAll('[class*=st-key-v3cz]')].map(z => {
    const r = z.getBoundingClientRect();
    const kids = [...z.querySelectorAll(':scope [data-testid=stElementContainer]')].filter(e => e.offsetParent)
      .map(e => { const b = e.getBoundingClientRect(); return b.bottom - r.top; });
    return {key: [...z.classList].find(c => c.startsWith('st-key-v3cz')), w: r.width, h: r.height,
            client_w: z.clientWidth, scroll_w: z.scrollWidth, overflow: getComputedStyle(z).overflowY,
            bottom: kids.length ? Math.max(...kids) : 0,
            actions: [...z.querySelectorAll('.vega-embed summary, .vega-embed .vega-actions')].map(s => {
              const b = s.getBoundingClientRect(); return [Math.round(b.width), Math.round(b.height)]; }),
            charts: [...z.querySelectorAll('.vega-embed svg.marks, .vega-embed canvas.marks')].map(s => {
              const b = s.getBoundingClientRect(); return [Math.round(b.width), Math.round(b.height)]; }),
            text: (z.innerText || '').slice(0, 40).replace(/\\n/g, ' ')};
  });
  return {page: main ? main.clientWidth : null,
          canvas: canv ? canv.getBoundingClientRect().width : null, zones};
}"""


def _port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def audit(workbook, dashboard, width=1500, python=None, fixed=False, shot=None):
    from playwright.sync_api import sync_playwright
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    port = _port()
    with tempfile.TemporaryDirectory() as tmp:
        app = os.path.join(tmp, 'app.py')
        open(app, 'w').write(APP)
        env = dict(os.environ, LA_ROOT=root, LA_WB=os.path.abspath(workbook), LA_DASH=dashboard)
        proc = subprocess.Popen([python or sys.executable, '-m', 'streamlit', 'run', app, '--server.port', str(port),
                                 '--server.headless', 'true', '--browser.gatherUsageStats', 'false'],
                                env=env, cwd=tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with sync_playwright() as pw:
                b = pw.chromium.launch()
                pg = b.new_page(viewport={'width': width, 'height': 1000})
                for _ in range(90):
                    try:
                        pg.goto(f'http://127.0.0.1:{port}', timeout=5000)
                        break
                    except Exception:
                        time.sleep(1)
                pg.wait_for_selector('[class*=st-key-v3cz]', timeout=180000)
                time.sleep(6)
                res = pg.evaluate(PROBE)
                if shot:
                    first = pg.query_selector('.vega-embed')
                    if first:
                        first.hover()           # the chart's hover-only controls show in the picture too
                        time.sleep(1)
                        res['actions_on_hover'] = pg.evaluate(
                            "() => [...document.querySelectorAll('.vega-embed summary')].filter(e => e.offsetParent)"
                            ".map(e => { const b = e.getBoundingClientRect(); return [Math.round(b.width), Math.round(b.height)]; })")
                    pg.screenshot(path=shot, full_page=True)
                b.close()
        finally:
            proc.terminate()
    problems = []
    if res['canvas'] and res['page'] and not fixed and res['canvas'] < 0.95 * min(res['page'], 1100 * 1.6):
        problems.append({'problem': 'GAP', 'detail': f"canvas {res['canvas']:.0f}px of a {res['page']}px page"})
    for z in res['zones']:
        for w, h in z['charts']:
            if (w < 2) != (h < 2):            # drawn on one axis only (a hidden 0 x 0 chart is not drawn at all)
                problems.append({'problem': 'CHART_EMPTY', 'zone': z['key'], 'detail': z['text']})
        if z['overflow'] == 'hidden' and z['bottom'] > z['h'] + 2 and z['charts']:
            problems.append({'problem': 'CUT_OFF', 'zone': z['key'], 'detail': f"{z['bottom']:.0f} > {z['h']:.0f}"})
        if any(w > 40 or h > 40 for w, h in z.get('actions') or []):
            # the chart's own menu button blown up by a chart-sizing rule (it covered the bars and their labels)
            problems.append({'problem': 'ACTIONS_OVERSIZE', 'zone': z['key'], 'detail': z['text']})
        if z['scroll_w'] > z['client_w'] + 2:
            problems.append({'problem': 'SCROLL_X', 'zone': z['key'], 'detail': z['text']})
    res['problems'] = problems
    return res


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    py = sys.argv[sys.argv.index('--python') + 1] if '--python' in sys.argv else None
    shot = sys.argv[sys.argv.index('--shot') + 1] if '--shot' in sys.argv else None
    for v in (py, shot):
        if v in args:
            args.remove(v)
    out = audit(args[0], args[1], int(args[2]) if len(args) > 2 else 1500, python=py, shot=shot)
    print(json.dumps(out, indent=1))
