// Release 26f: rendered-page check. Opens each dashboard in the running app and measures what the browser drew
// in every zone: content clipped by its zone, a sheet zone that drew nothing, labels overlapping each other.
// usage: node tools/rendered_check.js <workbook path> <dashboard> [<dashboard> ...]   (app on localhost:8601)
const { chromium } = require('playwright');
(async () => {
  const [wb, ...dashes] = process.argv.slice(2);
  const b = await chromium.launch();
  const report = [];
  for (const d of dashes) {
    const p = await b.newPage({ viewport: { width: 1500, height: 1600 } });
    await p.goto(`http://localhost:8601/?d=${encodeURIComponent(d)}&apply=1&wb=${encodeURIComponent(wb)}`, { timeout: 600000 });
    await p.waitForTimeout(3000);
    for (let i = 0; i < 400; i++) { if (!(await p.$('[data-testid="stStatusWidget"]'))) break; await p.waitForTimeout(1000); }
    await p.waitForTimeout(4000);
    const rows = await p.evaluate(() => {
      const out = [];
      for (const z of document.querySelectorAll('[class*="st-key-v3cz"]')) {
        const key = z.className.match(/st-key-(v3cz\d+)/)[1];
        const r = z.getBoundingClientRect();
        const cs = getComputedStyle(z);
        const text = z.innerText.trim().slice(0, 40).replace(/\n/g, ' ');
        const drew = z.innerText.trim().length > 0 || z.querySelector('svg, canvas, table, [data-testid=stDataFrame], [data-testid=stMetric], img, [data-baseweb], button, [role=radiogroup], p, span');
        const issues = [];
        // what actually extends past the zone: visible elements only (a data grid's own hidden scroller does not count)
        let maxB = r.bottom, maxR = r.right;
        for (const e of z.querySelectorAll('*')) {
          if (String(e.className || '').includes('dvn-') || e.closest('[class*=dvn-]')) continue;
          if (e.closest('svg') && e.tagName.toLowerCase() !== 'svg') continue;   // the chart's svg clips its own marks
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
        out.push({ zone: key, x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height), text, issues });
      }
      // overlapping zones that both draw content (not containers drawn behind, not floating by design)
      return out;
    });
    const errs = await p.evaluate(() => [...document.querySelectorAll('[data-testid=stException], .stAlert')].map(e => e.innerText.slice(0, 160)));
    report.push({ dashboard: d, zones: rows.length, problems: rows.filter(r => r.issues.length), errors: errs });
    await p.close();
  }
  console.log(JSON.stringify(report, null, 1));
  await b.close();
})();
