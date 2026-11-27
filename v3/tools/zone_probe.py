"""F7 diagnosis: for the given zones, list every element that reaches below its zone, and whether the compact-filter
CSS reached the page. Read-only; prints no data values beyond the zone's own text.

  python -X utf8 tools/zone_probe.py <workbook> <dashboard> <zone key> [<zone key> ...] [--sf] [--port 8601]
"""
import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROBE = """(keys) => keys.map(k => {
  const z = document.querySelector('div.st-key-' + k);
  if (!z) return {zone: k, found: false};
  const zr = z.getBoundingClientRect();
  const css = [...document.querySelectorAll('style')].map(s => s.textContent).join('');
  const below = [...z.querySelectorAll('*')].map(e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return {el: e.tagName + '.' + (e.getAttribute('data-baseweb') || e.getAttribute('data-testid') || ''),
            bottom_over: Math.round(r.bottom - zr.bottom), h: Math.round(r.height), min_h: s.minHeight,
            pos: s.position, display: s.display};
  }).filter(x => x.bottom_over > 0 && x.h > 0);
  return {zone: k, found: true, zone_h: Math.round(zr.height),
          squeeze_css: css.includes('st-key-' + k + ' [data-baseweb=select]>div'),
          tag_css: css.includes('st-key-' + k + ' [data-baseweb=tag]'),
          widgets: [...z.querySelectorAll('[data-testid]')].map(e => e.getAttribute('data-testid'))
                     .filter(t => /stMultiSelect|stSelectbox|stRadio|stSlider|stDateInput/.test(t)),
          below: below.slice(0, 25)};
})"""

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('workbook'); ap.add_argument('dashboard'); ap.add_argument('zones', nargs='+')
    ap.add_argument('--sf', action='store_true'); ap.add_argument('--port', type=int, default=8601)
    a = ap.parse_args()
    import streamlit
    print('streamlit', streamlit.__version__, '| python', sys.version.split()[0])
    from cx2_run import ensure_preview
    ensure_preview(a.port)
    from playwright.sync_api import sync_playwright
    url = f'http://localhost:{a.port}/?wb={quote(a.workbook)}&d={quote(a.dashboard)}' + ('&sf=1' if a.sf else '')
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={'width': 1700, 'height': 1600})
        pg.goto(url, timeout=600000)
        print('waiting for the dashboard to draw (with --sf, finish any Snowflake sign-in in your browser)', flush=True)
        t0 = time.time()
        while time.time() - t0 < 900 and not pg.query_selector('[class*="st-key-v3cz"], [data-testid=stException], .stAlert'):
            time.sleep(1)
        time.sleep(3)
        for _ in range(600):
            if not pg.query_selector('[data-testid="stStatusWidget"]'):
                break
            time.sleep(1)
        time.sleep(4)
        print(json.dumps(pg.evaluate(PROBE, a.zones), indent=1, ensure_ascii=False))
        b.close()
