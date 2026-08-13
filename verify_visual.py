"""
verify_visual.py  --  render the ENGINE's actual charts to PNG so they can be
compared against the Tableau screenshots BEFORE delivering a tab.

This exists because headless checks (no exception / correct SQL) do NOT prove a
chart renders. Always eyeball the PNGs vs the Tableau screenshot.

Captures EVERY output channel a renderer can use -- Altair charts, plotly
figures (maps/treemaps, saved via kaleido), dataframes (tables) and metrics
(KPIs). A sheet that produces NO output at all is a [WARN], never a silent
skip (the Population-map blind spot: 0 charts, 0 messages, nothing printed).

Usage:
  python verify_visual.py                 # all dashboards
  python verify_visual.py "Product"       # one dashboard (matches name or title)
Outputs PNGs to _preview/<dashboard>__<sheet>__<n>.png
"""

import json
import os
import re
import sys

import engine

OUT = "_preview"
os.makedirs(OUT, exist_ok=True)


class _FakeSt:
    """Captures every visual output channel AND warnings (a silent warning --
    or a silently un-captured output type -- would hide a failed sheet; the
    whole point of this tool is visibility)."""
    def __init__(self):
        self.charts = []          # altair
        self.figs = []            # plotly
        self.tables = []          # dataframes
        self.metrics = []         # KPI (label, value)
        self.msgs = []
    def columns(self, n): return [_FakeSt() for _ in range(n if isinstance(n, int) else len(n))]
    def container(self, **k): return self
    def altair_chart(self, ch, **k): self.charts.append(ch)
    def plotly_chart(self, fig, **k): self.figs.append(fig)
    def dataframe(self, df, **k): self.tables.append(df)
    def metric(self, label, value, *a, **k): self.metrics.append((label, value))
    def warning(self, m): self.msgs.append(("WARN", str(m)))
    def info(self, m): self.msgs.append(("INFO", str(m)))
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __getattr__(self, name): return lambda *a, **k: None
    def collect(self):
        """Merge captures from child containers/columns created during render."""
        return self


def _slug(s):
    return re.sub(r"[^0-9A-Za-z]+", "_", str(s)).strip("_")


def main():
    ir = json.load(open("workbook_ir.json"))
    engine.configure(ir)
    want = sys.argv[1] if len(sys.argv) > 1 else None
    for dash in ir["dashboards"]:
        if want and want.lower() not in (dash["name"].lower(), dash["title"].lower()) \
           and want.lower() not in dash["title"].lower():
            continue
        for sheet in dash["sheets"]:
            fake = _FakeSt()
            # child containers share the parent's capture lists so nothing
            # rendered inside st.columns()/st.container() is lost
            def _shared_child(n=None):
                c = _FakeSt()
                c.charts, c.figs, c.tables, c.metrics, c.msgs = \
                    fake.charts, fake.figs, fake.tables, fake.metrics, fake.msgs
                return c
            fake.columns = lambda n: [_shared_child() for _ in
                                      range(n if isinstance(n, int) else len(n))]
            fake.container = lambda **k: _shared_child()
            engine.st = fake
            try:
                engine.render_sheet(sheet, "")
            except Exception as e:
                print(f"  [ERR] {dash['name']}/{sheet['name']}: {e}")
                continue
            for sev, m in fake.msgs:
                print(f"  [{sev}] {dash['name']}/{sheet['name']}: {m}")
            base = f"{_slug(dash['name'])}__{_slug(sheet['name'])}"
            n_out = 0
            for i, ch in enumerate(fake.charts):
                path = os.path.join(OUT, f"{base}__{i}.png")
                try:
                    ch.properties(width=820).save(path)
                    print("  saved", path)
                    n_out += 1
                except Exception as e:
                    print(f"  [save-err] {path}: {e}")
            for i, fig in enumerate(fake.figs):
                path = os.path.join(OUT, f"{base}__plotly{i}.png")
                try:
                    fig.write_image(path, width=820, height=520)
                    print("  saved", path)
                    n_out += 1
                except Exception as e:
                    print(f"  [save-err] {path}: {e}")
            for i, df in enumerate(fake.tables):
                print(f"  [OK] {dash['name']}/{sheet['name']}: table renders "
                      f"{len(df)} rows x {len(df.columns)} cols (no PNG)")
                n_out += 1
            for label, value in fake.metrics:
                print(f"  [OK] {dash['name']}/{sheet['name']}: KPI {label} = {value}")
                n_out += 1
            if n_out == 0:
                print(f"  [WARN] {dash['name']}/{sheet['name']}: NO visual output "
                      f"captured (kind={sheet.get('kind')}) -- investigate")


if __name__ == "__main__":
    main()
