"""Release 27: rendered-page gate (Python port of tools/rendered_check.js, plus hidden-content checks).

Opens each dashboard in tools/preview_app.py (port 8601) and measures what the browser drew in every canvas zone:
  - content clipped by its zone / spilling below it            (as in the 26f JS check)
  - a zone that drew nothing, error boxes on the page            (as in the 26f JS check)
  - NEW: text the user cannot see because a clipping ancestor cuts it off (a table row, a title, a footer line):
         every visible text box must lie inside every overflow-clipping ancestor up to its zone
  - NEW: text cut short horizontally (scrollWidth > clientWidth on a clipping element: "Categories - $")

usage: python tools/rendered_check.py <workbook path> <dashboard> [<dashboard> ...] [--sf] [--port 8601] [--json out]
exit 1 when any dashboard reports a problem.
"""
import argparse
from pathlib import Path
import json
import sys
import time
from urllib.parse import quote

from playwright.sync_api import sync_playwright

MEASURE = r"""() => {
  const clips = e => { const s = getComputedStyle(e); return /hidden|clip|auto|scroll/.test(s.overflowX + s.overflowY); };
  const out = [];
  for (const z of document.querySelectorAll('[class*="st-key-v3cz"]')) {
    const key = (z.className.match(/st-key-(v3cz[\w-]+)/) || [])[1];
    const r = z.getBoundingClientRect(); const cs = getComputedStyle(z);
    const text = z.innerText.trim().slice(0, 40).replace(/\n/g, ' ');
    const drew = z.innerText.trim().length > 0 || z.querySelector('svg, canvas, table, [data-testid=stDataFrame], [data-testid=stMetric], img, [data-baseweb], button, [role=radiogroup], p, span');
    const issues = [];
    let maxB = r.bottom, maxR = r.right;
    for (const e of z.querySelectorAll('*')) {
      if (String(e.className || '').includes('dvn-') || e.closest('[class*=dvn-]')) continue;
      if (e.closest('svg') && e.tagName.toLowerCase() !== 'svg') continue;
      const st = getComputedStyle(e);
      if (st.visibility === 'hidden' || st.display === 'none' || st.position === 'fixed') continue;
      const q = e.getBoundingClientRect();
      if (q.width === 0 || q.height === 0) continue;
      maxB = Math.max(maxB, q.bottom); maxR = Math.max(maxR, q.right);
    }
    const clipY = Math.round(maxB - r.bottom), clipX = Math.round(maxR - r.right);
    if (cs.overflowY === 'hidden' && clipY > 10) issues.push(`content clipped by ${clipY}px at the bottom`);
    if (cs.overflowX === 'hidden' && clipX > 10) issues.push(`content clipped by ${clipX}px at the right`);
    const paints = (cs.backgroundColor && cs.backgroundColor !== 'rgba(0, 0, 0, 0)') || parseFloat(cs.borderTopWidth) > 0;
    if (!drew && !paints) issues.push('zone drew nothing');
    if (cs.overflowY === 'visible' && clipY > 6) issues.push(`spills ${clipY}px below its zone`);
    // hidden text: leaf text boxes (outside data grids, which scroll by design) cut off by a clipping ancestor
    const hidden = [], cut = [];
    const walker = document.createTreeWalker(z, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const t = n.textContent.trim(); if (!t) continue;
      const el = n.parentElement; if (!el || el.closest('[class*=dvn-], [data-testid=stDataFrame], .v3-xtab, [data-testid=stToolbar], style, script')) continue;
      const st = getComputedStyle(el); if (st.visibility === 'hidden' || st.display === 'none') continue;
      const range = document.createRange(); range.selectNodeContents(n); const q = range.getBoundingClientRect();
      if (q.width === 0 || q.height === 0) continue;
      for (let a = el; a && a !== z.parentElement; a = a.parentElement) {
        if (a !== el && !clips(a)) continue;
        const b = a.getBoundingClientRect();
        const outside = Math.max(0, b.top - q.top) + Math.max(0, q.bottom - b.bottom);
        if (a !== el && outside > q.height * 0.4) { hidden.push(t.slice(0, 30)); break; }
      }
      if (clips(el) && el.scrollWidth > el.clientWidth + 2 && /hidden|clip/.test(st.overflowX)) cut.push(t.slice(0, 30));
    }
    if (hidden.length) issues.push(`${hidden.length} text item(s) hidden by clipping: ${[...new Set(hidden)].slice(0, 4).join(' | ')}`);
    if (cut.length) issues.push(`${cut.length} text item(s) cut short: ${[...new Set(cut)].slice(0, 4).join(' | ')}`);
    out.push({zone: key, x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height), text, issues});
  }
  return out;
}"""


def check(workbook, dashboards, sf=False, port=8601):
    report = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        for d in dashboards:
            pg = b.new_page(viewport={'width': 1700, 'height': 1600})
            url = f'http://localhost:{port}/?wb={quote(workbook)}&d={quote(d)}' + ('&sf=1' if sf else '')
            print(f'{d}: opening the dashboard page (a Microsoft sign-in may open in your browser with --sf)', flush=True)
            pg.goto(url, timeout=600000)
            # wait until the dashboard has drawn zones (or shown an error box); with --sf the app first opens the
            # Snowflake session, which may wait for a sign-in in the browser. Measuring earlier gave '0 zones ... OK'.
            print(f'{d}: waiting for the dashboard to draw (with --sf, finish any Snowflake sign-in in your browser)',
                  flush=True)
            t0 = time.time()
            while time.time() - t0 < 900:
                if pg.query_selector('[class*="st-key-v3cz"], [data-testid=stException], .stAlert'):
                    break
                if int(time.time() - t0) % 60 == 59:
                    print(f'  still waiting ({int(time.time() - t0) + 1}s)', flush=True)
                time.sleep(1)
            time.sleep(3)
            for _ in range(600):
                if not pg.query_selector('[data-testid="stStatusWidget"]'):
                    break
                time.sleep(1)
            time.sleep(4)
            rows = pg.evaluate(MEASURE)
            errs = pg.evaluate("() => [...document.querySelectorAll('[data-testid=stException], .stAlert')].map(e => e.innerText.slice(0, 160))")
            report.append({'dashboard': d, 'zones': len(rows), 'problems': [r for r in rows if r['issues']], 'errors': errs})
            pg.close()
        b.close()
    return report


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('workbook'); ap.add_argument('dashboards', nargs='+')
    ap.add_argument('--sf', action='store_true'); ap.add_argument('--port', type=int, default=8601); ap.add_argument('--json')
    a = ap.parse_args()
    # the preview app is started when it is not running (it used to fail with ERR_CONNECTION_REFUSED)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cx2_run import ensure_preview
    print('checking the preview app on port', a.port, flush=True)
    ensure_preview(a.port)
    print('preview app is up', flush=True)
    rep = check(a.workbook, a.dashboards, a.sf, a.port)
    if a.json:
        json.dump(rep, open(a.json, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
    bad = 0
    for r in rep:
        n = len(r['problems']) + len(r['errors']) + (0 if r['zones'] else 1); bad += n
        print(f"{r['dashboard']}: {r['zones']} zones, {len(r['problems'])} zone problem(s), {len(r['errors'])} error box(es)")
        for z in r['problems']:
            print(f"  {z['zone']} @{z['x']},{z['y']} {z['w']}x{z['h']} [{z['text']}]: " + '; '.join(z['issues']))
        for e in r['errors']:
            print('  ERROR BOX:', e.replace('\n', ' '))
        if not r['zones']:
            print('  NO ZONES: the dashboard did not draw (nothing was checked)')
    print('RENDERED CHECK OK' if not bad else f'RENDERED CHECK: {bad} problem(s)')
    sys.exit(1 if bad else 0)
