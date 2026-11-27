"""Release 27: screenshot the dashboard canvas drawn by tools/preview_app.py (for side-by-side with Tableau).

usage: python tools/preview_shot.py <workbook path> <dashboard> <out.png> [--sf] [--port 8601]
"""
import argparse
import time
from urllib.parse import quote

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument('workbook'); ap.add_argument('dashboard'); ap.add_argument('out')
ap.add_argument('--sf', action='store_true'); ap.add_argument('--port', type=int, default=8601)
a = ap.parse_args()
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={'width': 2000, 'height': 1400})
    pg.goto(f'http://localhost:{a.port}/?wb={quote(a.workbook)}&d={quote(a.dashboard)}' + ('&sf=1' if a.sf else ''), timeout=600000)
    time.sleep(3)
    for _ in range(600):
        if not pg.query_selector('[data-testid="stStatusWidget"]'):
            break
        time.sleep(1)
    time.sleep(4)
    if not pg.query_selector('div.st-key-v3canvas'):
        pg.screenshot(path=a.out, full_page=True)
        print('NO CANVAS; page says:', pg.inner_text('body')[:3000])
        b.close()
        raise SystemExit(1)
    pg.evaluate("""() => { let e = document.querySelector('div.st-key-v3canvas');
        while (e && e !== document.body) { e.style.overflow = 'visible'; e.style.maxWidth = 'none'; e = e.parentElement; } }""")
    pg.locator('div.st-key-v3canvas').first.screenshot(path=a.out)
    b.close()
