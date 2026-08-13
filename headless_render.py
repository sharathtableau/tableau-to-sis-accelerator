"""
headless_render.py -- R8: render the generated app's charts to REAL PNG
images without a live Streamlit session, so they can be compared against
Tableau's own REST-pulled view images (tableau_server.query_view_image).

WHY THIS MODULE EXISTS: screenshotting the DEPLOYED app was considered and
rejected (2026-07-28, see NEW_CHAT.md's R8 redesign entry) -- the deployed
app sits behind Snowflake SSO, and a Streamlit-in-Snowflake app has zero
outbound access and no browser runtime inside the sandbox, so headless-
browser screenshotting is a dead end both from inside and outside it without
automating SSO credentials, which this project avoids everywhere else.

INSTEAD: capture the SAME Altair chart OBJECT engine.py already builds,
using the exact monkeypatch-and-capture technique this project's own
regression suite already proves works OUTSIDE a live Streamlit runtime
(tests/test_regression.py's test_bar_colored_by_own_axis_has_no_offset calls
engine.render_sheet(...) directly with st.altair_chart monkeypatched, no
`streamlit run` or AppTest harness needed) -- then convert the captured
Vega-Lite spec to a real PNG via vl-convert-python (pure Python/Rust, no
browser, no headless Chrome).

SCOPE, stated honestly: a sheet is captured through whatever channel it
actually draws -- Altair (vl-convert), Plotly/maps (kaleido), KPI tiles and
tables (PIL) -- and returns (None, reason) when it draws nothing capturable,
never a blank image passed off as real.

Dashboard-level compositing walks the dashboard's OWN zone tree: either
`dash["layout"]` (the same tree engine._render_layout hands to st.columns)
or one rebuilt from per-sheet `geom` rectangles via engine._rows_from_geom.
Zone order, grouping and width share therefore come from the workbook, not
from a second independently-guessed layout. What is still approximate, and
is never presented otherwise: zone HEIGHTS are each chart's natural
rendered height rather than the workbook's zone height, and Tableau's
right-hand filter/legend cards are not drawn (every input widget is mocked
away here -- see _mocked_widgets).
"""
import contextlib
import io

import altair as alt

import engine

# INPUT WIDGET functions that persist state via a `key=` and therefore
# require that key to be globally unique across the WHOLE script run.
# FOUND LIVE 2026-07-29: the live app already renders each dashboard once
# (its own preview), registering widgets like a date-range picker keyed
# "<dashboard>::<field>::<part>" (engine.build_where) or a Drill dropdown /
# worksheet-shown parameter keyed by sheet name (engine.render_sheet's own
# internal calls). Calling engine.build_where/render_sheet a SECOND time for
# the SAME dashboard -- exactly what headless rendering does -- re-creates
# those same keys and Streamlit raises StreamlitDuplicateElementKey. Chart
# functions (altair_chart/plotly_chart) don't have this problem (no
# `key=`/persisted state), which is why the original design only mocked
# those; this covers every INPUT widget the same way.
_WIDGET_ATTRS = ("selectbox", "date_input", "number_input", "text_input",
                 "multiselect", "checkbox", "slider")


@contextlib.contextmanager
def _mocked_widgets(pick_real=False):
    """Monkeypatch every Streamlit INPUT WIDGET function to return a
    sensible non-interactive DEFAULT instead of registering a real widget --
    sidesteps the duplicate-key collision entirely rather than trying to
    make two independent render passes agree on globally unique keys.
    Defaults are chosen to mean "no filter / no override": selectbox
    returns its first option (this project's own convention: index 0 of a
    filter dropdown is always "All", see engine.build_where), date_input
    returns its given `value` unchanged (the full min/max range it was
    passed), multiselect/checkbox/number_input/text_input/slider all return
    their given default unchanged. Always restores the real functions,
    even on exception -- this patches the shared `streamlit` module object,
    so leaving it patched would affect any other code importing streamlit
    in the same process.

    `pick_real=True` (INTERACTION PROOF's use, R11): selectbox returns its
    SECOND option (the first REAL value, index 1) instead of "All" (index
    0) whenever one exists -- drives engine.build_where() through its own
    real code path with an ACTUAL filter selection, so the resulting WHERE
    clause can be proven against the live table, not just proven not to
    crash. Every other widget stays at its no-op default."""
    reals = {a: getattr(engine.st, a) for a in _WIDGET_ATTRS}
    _sel_index = 1 if pick_real else 0
    engine.st.selectbox = lambda label, options, index=_sel_index, **kw: (
        options[index] if len(options) > index else
        (options[0] if options else None))
    engine.st.date_input = lambda label, value=None, **kw: value
    engine.st.number_input = lambda label, value=0.0, **kw: value
    engine.st.text_input = lambda label, value="", **kw: value
    engine.st.multiselect = lambda label, options, default=None, **kw: (
        list(default) if default is not None else [])
    engine.st.checkbox = lambda label, value=False, **kw: value
    engine.st.slider = lambda label, *a, **kw: kw.get(
        "value", a[0] if a else None)
    try:
        yield
    finally:
        for attr, fn in reals.items():
            setattr(engine.st, attr, fn)


def capture_sheet_chart(sheet, where_parts=None, dashboard_name=None):
    """The shared capture step render_sheet_to_png and the R11 tooltip proof
    both need: call engine.render_sheet directly with st.altair_chart /
    st.plotly_chart monkeypatched to CAPTURE the chart object instead of
    drawing it, every INPUT widget mocked via _mocked_widgets(). Factored
    out so both callers exercise the EXACT same capture step rather than
    two copies that could silently diverge (this project's standing rule
    against two paths disagreeing).

    `dashboard_name`, when given, is threaded onto `engine._EVIDENCE_DASHBOARD`
    for the duration of this ONE call -- the validation evidence bridge's
    (§ engine.EVIDENCE_CAPTURE) explicit REGISTRY.record_chart calls inside
    r_map/r_treemap/r_table/_rank_table read it to tag the chart evidence
    with its real dashboard. A caller that never passes it (render_sheet_to_png,
    the R11 tooltip proof) leaves engine._EVIDENCE_DASHBOARD untouched, and
    since EVIDENCE_CAPTURE defaults to False for those callers anyway, this
    is a no-op for them either way.

    A sheet kind rendered as multiple SIDE-BY-SIDE panels (r_mbar's one
    small-multiple per measure, r_strips' one panel per measure, r_circle's
    one sub-chart per facet value -- each via st.columns(...) + one
    st.altair_chart(...) call per column) makes MULTIPLE altair_chart calls
    during ONE render_sheet(). Every call is captured, in order, and
    combined with alt.hconcat(...) into ONE chart object when there is more
    than one -- reproducing the actual side-by-side layout instead of
    silently keeping only the LAST call and dropping the rest (found live
    2026-08-07: Superstore's CustomerOverview, a 6-measure-panel mbar sheet,
    rendered its dashboard PNG with 5 of 6 KPI panels missing and the lone
    survivor full-width instead of in its real column -- capturing only the
    final st.altair_chart call and discarding every one before it).

    Whether those panels end up side by side or stacked is READ from the
    render, not assumed -- see _arrange_altair.

    Returns (altair_chart_or_None, reason_or_None). reason is set when the
    sheet drew nothing capturable (Plotly, KPI/text-only, or a real
    exception) -- never returns a chart AND a reason together."""
    cap = _capture_all(sheet, where_parts, dashboard_name)
    if cap["error"]:
        return None, cap["error"]
    charts = cap["altair"]
    if not charts:
        if cap["plotly"]:
            return None, ("Plotly-rendered sheet (e.g. a map) -- no Altair chart "
                          "object to inspect (render_sheet_to_png CAN still "
                          "export it as an image)")
        return None, "sheet drew no Altair chart (KPI/text-only sheet, or nothing matched the filter)"
    return _arrange_altair(charts, cap.get("altair_cols")), None


def _arrange_altair(charts, groups=None):
    """Combine a sheet's captured Altair charts into ONE chart laid out the
    way the app actually drew them.

    `groups[i]` is the id of the st.columns() call the i-th chart was drawn
    into, or None when it was drawn at module level. Charts sharing a column
    group ran side by side, so they hconcat; charts drawn at module level ran
    one after another down the page, so they vconcat.

    FIXED 2026-08-07: this used to hconcat unconditionally, which turned any
    sheet that stacks its panels vertically into a horizontal strip -- the
    capture inventing a layout the app never rendered. The arrangement is now
    read off the render itself, so a side-by-side sheet stays side by side
    and a stacked one stays stacked."""
    if len(charts) == 1:
        return charts[0]
    groups = list(groups or [None] * len(charts))
    if len(groups) != len(charts):
        groups = [None] * len(charts)
    blocks, run, run_gid = [], [], object()
    for ch, gid in zip(charts, groups):
        if gid is not None and gid == run_gid:
            run.append(ch)
            continue
        if run:
            blocks.append(run)
        run, run_gid = [ch], gid
    if run:
        blocks.append(run)
    parts = [b[0] if len(b) == 1 else alt.hconcat(*b) for b in blocks]
    return parts[0] if len(parts) == 1 else alt.vconcat(*parts)


def _capture_all(sheet, where_parts=None, dashboard_name=None):
    """ONE render pass capturing EVERY visual channel engine.py can draw:
    Altair charts, Plotly figures (maps/treemaps) AND KPI tiles (st.metric,
    and columns().metric).

    Why one function rather than a capture per channel: this project's
    standing rule against two code paths that can silently diverge. Before
    this, `capture_sheet_chart` captured Altair only and reported a Plotly or
    KPI sheet as "nothing capturable", while `capture_sheet_kpis` ran a
    SECOND full render of the same sheet just to catch the tiles. That split
    is why 5 of 10 Superstore dashboards produced NO app-side image at all
    (they are KPI/table/map-only), leaving the visual comparison with nothing
    to judge -- found 2026-08-07 by actually opening every generated image
    pair instead of trusting the similarity score.

    Returns {"altair": [...], "altair_cols": [...], "plotly": [...],
    "kpis": [(label, value)], "error": str_or_None}. `altair_cols` is
    parallel to `altair`: the id of the st.columns() call each chart was
    drawn into (None for a module-level call), which is what lets
    _arrange_altair reproduce side-by-side vs stacked instead of guessing.

    Never raises: a sheet that blows up mid-render returns its exception
    text as `error` with whatever was captured before it, rather than losing
    the whole dashboard."""
    out = {"altair": [], "altair_cols": [], "plotly": [], "kpis": [],
           "tables": [], "error": None}
    st_mod = engine.st
    real = {a: getattr(st_mod, a, None)
            for a in ("altair_chart", "plotly_chart", "metric", "columns",
                      "dataframe", "table")}
    prev_dashboard = getattr(engine, "_EVIDENCE_DASHBOARD", None)
    col_group = [0]

    def _cap_altair(chart=None, _group=None, **kw):
        out["altair"].append(chart)
        out["altair_cols"].append(_group)

    def _cap_plotly(fig=None, **kw):
        if fig is not None:          # a None fig carries nothing to export
            out["plotly"].append(fig)

    def _cap_metric(label=None, value=None, **kw):
        out["kpis"].append((label, value))

    def _cap_table(data=None, **kw):
        if data is not None:
            out["tables"].append(data)

    class _FakeCol:
        """A stand-in for a st.columns() column. Chart/metric/table calls
        made ON the column object are captured exactly like module-level
        ones -- tagged with the id of the st.columns() call they belong to,
        so a side-by-side panel row stays recognisable as one. Any OTHER
        Streamlit call on it (col.warning, col.caption, ...) becomes a no-op
        so a sheet that writes prose beside its chart still renders."""

        def __init__(self, group):
            self._group = group

        metric = staticmethod(_cap_metric)
        plotly_chart = staticmethod(_cap_plotly)
        dataframe = staticmethod(_cap_table)
        table = staticmethod(_cap_table)

        def altair_chart(self, chart=None, **kw):
            _cap_altair(chart, _group=self._group, **kw)

        def __enter__(self):
            _CUR_COL.append(self._group)
            return self

        def __exit__(self, *a):
            if _CUR_COL:
                _CUR_COL.pop()
            return False

        def __getattr__(self, name):
            return lambda *a, **k: None

    # `with col:` makes st.altair_chart(...) at MODULE level land inside that
    # column -- the form engine.py actually uses (r_mbar, r_strips, r_circle).
    # Without this the panels would look module-level and stack.
    _CUR_COL = []

    def _cap_altair_module(chart=None, **kw):
        _cap_altair(chart, _group=(_CUR_COL[-1] if _CUR_COL else None), **kw)

    def _cap_columns(spec=None, **kw):
        n = spec if isinstance(spec, int) else (len(spec) if spec else 1)
        col_group[0] += 1
        gid = col_group[0]
        return [_FakeCol(gid) for _ in range(max(int(n or 1), 1))]

    # engine._rank_html draws a RANK TABLE as hand-built HTML through
    # st.markdown, so patching st.* alone cannot see it -- the sheet came back
    # "drew nothing capturable" even though the app renders it perfectly.
    # FOUND 2026-08-07 on E-Commerce: TWELVE of that workbook's 27 sheets are
    # rank tables (Tableau's constant-placeholder-axis trick, MIN(0)), so more
    # than half the dashboard was invisible to the visual comparison. It gets
    # the already-formatted DataFrame, which is exactly what _table_to_png
    # wants, so capture that frame and reuse the existing table channel.
    real_rank = getattr(engine, "_rank_html", None)

    def _cap_rank(disp, dim=None, numeric=None, *a, **kw):
        _cap_table(disp)

    st_mod.altair_chart = _cap_altair_module
    st_mod.plotly_chart = _cap_plotly
    st_mod.metric = _cap_metric
    st_mod.columns = _cap_columns
    st_mod.dataframe = _cap_table
    st_mod.table = _cap_table
    if real_rank is not None:
        engine._rank_html = _cap_rank
    if dashboard_name is not None:
        engine._EVIDENCE_DASHBOARD = dashboard_name
    try:
        with _mocked_widgets():
            engine.render_sheet(sheet, where_parts or [])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        for attr, fn in real.items():
            if fn is not None:
                setattr(st_mod, attr, fn)
        if real_rank is not None:      # must be restored even on exception --
            engine._rank_html = real_rank   # this patches the LIVE app's engine
        engine._EVIDENCE_DASHBOARD = prev_dashboard
    return out


def _stack_pngs(png_list, gap=10, bg="white"):
    """Stack several PNGs vertically into one, centred. Shared by every
    multi-channel path so there is ONE compositor, not one per channel."""
    from PIL import Image

    images = [Image.open(io.BytesIO(p)) for p in png_list if p]
    if not images:
        return None
    if len(images) == 1:
        return png_list[0]
    max_w = max(im.width for im in images)
    total_h = sum(im.height for im in images) + gap * (len(images) - 1)
    canvas = Image.new("RGB", (max_w, total_h), bg)
    y = 0
    for im in images:
        canvas.paste(im.convert("RGB"), ((max_w - im.width) // 2, y))
        y += im.height + gap
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


_GLYPH_FALLBACK = {
    "▲": "^", "▼": "v", "↑": "^", "↓": "v",
    "▴": "^", "▾": "v", "→": "->", "←": "<-",
    "—": "-", "–": "-", "•": "*", "·": "*",
    "Δ": "Chg",        # engine.py's rank tables head the delta column "Δ%"
    "\xa0": " ",       # nbsp IS latin-1, so it survives the filter below and
                       # the bundled font draws it as a box -- normalise first
}


def _ascii_glyphs(text):
    """Swap glyphs PIL's bundled default font cannot draw for ASCII
    equivalents, then drop anything still unrenderable.

    Without this, engine.py's rank tables (which use up/down arrows for the
    period-over-period delta) drew a row of TOFU BOXES -- `4.1%<box><box>` --
    in every table PNG. A reviewer reading that image cannot tell a missing
    glyph from corrupted data, so it is worse than plain text. Found
    2026-08-07 on E-Commerce, whose dashboard is twelve rank tables."""
    for bad, good in _GLYPH_FALLBACK.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "ignore").decode("latin-1")


def _table_to_png(frames, max_rows=25, max_cols=10, row_h=26, pad=10,
                  bg="white"):
    """A rendered detail/list TABLE -> PNG, so table-only sheets stop being
    invisible to the visual comparison (Superstore's 'Order Details' and
    'What if Forecast' dashboards are a single table sheet each, so they
    produced no app-side image at all until this existed).

    Deliberately capped at the top `max_rows` x `max_cols` -- the same
    reasoning as the detail-table evidence cap: a reviewer (human or vision
    model) checks the leading rows in displayed order, and a 200-row PNG is
    both unreadable and pointless to diff. The cap is STATED on the image
    itself rather than silently truncating."""
    from PIL import Image, ImageDraw, ImageFont

    frame = next((f for f in frames if f is not None and hasattr(f, "columns")),
                 None)
    if frame is None:
        return None, "no tabular data captured"
    try:
        # POSITIONAL slice, not label selection. `frame[list_of_names]` on a
        # frame with DUPLICATE column names returns every column matching each
        # name, so the body came back WIDER than the header and the draw loop
        # walked off the end of col_w (IndexError). Found 2026-08-07 on Global
        # Sales Dashboard's View2 -- Superstore has no duplicate-named table,
        # so this had been latent since the table channel was added.
        body = frame.iloc[:max_rows, :max_cols]
        cols = [_ascii_glyphs(str(c)) for c in body.columns]
        rows = [[_ascii_glyphs(("" if v is None else str(v)))[:26] for v in rec]
                for rec in body.itertuples(index=False, name=None)]
    except Exception as e:
        return None, f"table capture unreadable: {type(e).__name__}: {e}"

    try:
        font = ImageFont.load_default(size=14)
        head_font = ImageFont.load_default(size=14)
    except TypeError:
        font = head_font = ImageFont.load_default()

    col_w = [max(90, min(240, 9 * max([len(c)] + [len(r[i]) for r in rows] or [0]) + 18))
             for i, c in enumerate(cols)]
    width = sum(col_w) + pad * 2
    truncated = (len(frame) > max_rows) or (len(frame.columns) > max_cols)
    height = pad * 2 + row_h * (len(rows) + 1) + (22 if truncated else 0)
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    x = pad
    for i, c in enumerate(cols):                      # header
        draw.text((x + 6, pad + 6), c[:26], fill="#3a3a3a", font=head_font)
        x += col_w[i]
    draw.line([(pad, pad + row_h - 2), (width - pad, pad + row_h - 2)],
              fill="#c8c8c8", width=1)
    for r_i, row in enumerate(rows):                  # body
        y = pad + row_h * (r_i + 1)
        x = pad
        for c_i, cell in enumerate(row[:len(col_w)]):   # never walk off col_w
            draw.text((x + 6, y + 6), cell, fill="#1a1a1a", font=font)
            x += col_w[c_i]
        draw.line([(pad, y + row_h - 2), (width - pad, y + row_h - 2)],
                  fill="#efefef", width=1)
    if truncated:
        draw.text((pad + 6, height - 18),
                  f"(showing first {len(rows)} of {len(frame)} rows, "
                  f"{len(cols)} of {len(frame.columns)} columns)",
                  fill="#7a7a7a", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), None


def _plotly_to_png(figs, width=900, height=520):
    """Plotly figures -> ONE PNG via kaleido (the same exporter
    verify_visual.py already uses for maps/treemaps). Stacks vertically when
    a sheet drew more than one. Returns (png_bytes_or_None, reason)."""
    from PIL import Image

    images = []
    for fig in figs:
        try:
            images.append(Image.open(io.BytesIO(
                fig.to_image(format="png", width=width, height=height))))
        except Exception as e:
            return None, (f"Plotly PNG export failed (kaleido): "
                          f"{type(e).__name__}: {e}")
    if not images:
        return None, "no Plotly figure captured"
    if len(images) == 1:
        buf = io.BytesIO()
        images[0].save(buf, format="PNG")
        return buf.getvalue(), None
    total_h = sum(im.height for im in images)
    max_w = max(im.width for im in images)
    canvas = Image.new("RGB", (max_w, total_h), "white")
    y = 0
    for im in images:
        canvas.paste(im, ((max_w - im.width) // 2, y))
        y += im.height
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue(), None


def _kpi_tiles_to_png(pairs, per_tile_w=230, height=140, bg="white",
                      canvas_w=None):
    """KPI tiles -> a real PNG laid out as the app's own KPI row.

    These are a dashboard's MOST-READ numbers and were previously absent
    from every app-side image, so a vision comparison could not see them at
    all (and, worse, reported them as 'missing from the migrated app'). The
    values drawn are exactly the strings the app displays, so a reader --
    human or vision model -- sees the same figures the user would."""
    from PIL import Image, ImageDraw, ImageFont

    tiles = [(str(l), "" if v is None else str(v)) for l, v in pairs]
    if not tiles:
        return None, "no KPI tile captured"
    # A KPI row fills its zone in the app, so spread the tiles across the
    # zone width when one is given -- never draw them narrow and let the
    # compositor magnify them (that is how a $15,357,898 tile ended up
    # rendered five times the size of the chart beside it).
    if canvas_w:
        per_tile_w = max(per_tile_w, int(canvas_w / len(tiles)))
    width = max(per_tile_w * len(tiles), per_tile_w)
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    try:                                  # Pillow >= 10.1 sizes the default font
        label_font = ImageFont.load_default(size=17)
        value_font = ImageFont.load_default(size=31)
    except TypeError:                     # older Pillow: unsized bitmap default
        label_font = value_font = ImageFont.load_default()
    for i, (label, value) in enumerate(tiles):
        x = i * per_tile_w + 14
        draw.text((x, 34), label[:30], fill="#5a5a5a", font=label_font)
        draw.text((x, 66), value[:22], fill="#1a1a1a", font=value_font)
        if i:                             # separator between tiles
            draw.line([(i * per_tile_w, 24), (i * per_tile_w, height - 24)],
                      fill="#e2e2e2", width=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), None


def capture_sheet_kpis(sheet, dashboard_name=None):
    """R12b -- the REAL KPI tiles a sheet renders, as [(label, shown_value)].

    A KPI/text-only sheet draws no Altair chart, so capture_sheet_chart
    reports it uncapturable and it was excluded from validation entirely --
    yet KPI tiles are a dashboard's most-read numbers. engine.py renders
    them through `st.metric` (single-metric path) and `st.columns(...)` +
    `col.metric` (multi-metric path), so BOTH are captured here.

    This is a SECOND render_sheet() call for a sheet capture_sheet_chart
    already tried (validation_adapter.build_chart_spec only reaches this
    probe when that first call's Altair capture came back empty -- true for
    every KPI/text sheet, but ALSO for the map/table/rank-table kinds that
    never produce an Altair object at all). `dashboard_name` is threaded the
    same way capture_sheet_chart threads it, so engine.py's own evidence
    recorder sees the SAME (dashboard, sheet) key on both calls and its
    already-recorded guard skips the second one -- without this, that guard
    would key its own call under dashboard="" instead of the real dashboard,
    which produced an untethered duplicate registry entry (found running
    this for real against the Superstore corpus: `Product Detail Sheet`,
    `Sales by Geography`, and `What if Forecast Based on` each left a
    dashboard="" ghost entry that Streamlit's KPI-metric probe recorded
    after render_sheet's own r_table/r_map call had already recorded the
    correctly-tagged one).

    Returns (pairs, reason). `pairs` are the label and the value string
    EXACTLY as the app displays it -- deliberately the displayed (rounded)
    value, because that is what a user reads and what the validation
    engine's precision-derived tolerance is designed to reconcile against a
    full-precision backend figure."""
    cap = _capture_all(sheet, None, dashboard_name)
    if cap["error"]:
        return None, cap["error"]
    pairs = [(str(l), v) for l, v in cap["kpis"]
             if l is not None and v is not None]
    if not pairs:
        return None, "sheet rendered no KPI tile"
    return pairs, None


def _tooltip_channels(spec):
    """Every `tooltip` encoding channel in a Vega-Lite spec, including
    inside `layer`/`facet`/`concat` composites (a layered chart's tooltip
    can live on any layer, not just the top level)."""
    out = list(spec.get("encoding", {}).get("tooltip") or [])
    for key in ("layer", "concat", "hconcat", "vconcat"):
        for child in spec.get(key, []) or []:
            out.extend(_tooltip_channels(child))
    return out


def extract_tooltip_titles(chart):
    """The REAL field titles/names shown in a captured Altair chart's
    tooltip -- read from its OWN Vega-Lite spec (chart.to_dict()), never
    guessed from the sheet's shelf pills. Prefers each channel's `title`
    (charts built with alt.Tooltip(..., title=caption) set this to the
    exact Tableau caption); falls back to the raw `field` id when no title
    was set. Used to prove whether Tableau's declared tooltip_fields
    (parsed from the .twb) actually appear in what the app renders."""
    spec = chart.to_dict()
    titles = []
    for ch in _tooltip_channels(spec):
        if not isinstance(ch, dict):
            titles.append(str(ch))          # Altair shorthand string, unusual post-to_dict()
            continue
        title = ch.get("title")
        if isinstance(title, list):         # Vega-Lite allows a multi-line title
            title = " ".join(str(x) for x in title)
        titles.append(str(title) if title else str(ch.get("field", "")))
    return [t for t in titles if t]


def _fit_spec_width(spec, px, gap=20):
    """Give a Vega-Lite spec's LEAF views an explicit pixel width, so the
    export reproduces the width the app actually gives the chart.

    REAL BUG, found by looking at the output (2026-08-07): engine.py draws
    with `use_container_width=True`, so in the app a chart fills its zone --
    but nothing carries that to the export, and Vega-Lite's default for a
    discrete axis is a 20px STEP. Superstore's Product Drilldown heatmap
    (12 month columns, a `$16,946`-style label in every cell) therefore
    exported ~240px wide with every label overlapping its neighbours, and
    the compositor then UPSCALED that to the zone width -- magnifying the
    collision into an unreadable smear that looked like an app bug. The app
    renders it fine; the capture was making it up.

    An existing `{"step": n}` width is left alone (an intentional per-band
    size), and so is a facet/repeat spec's own header sizing."""
    if not isinstance(spec, dict) or px <= 0:
        return spec
    if "hconcat" in spec:
        kids = spec["hconcat"] or []
        if kids:
            each = max(40, int((px - gap * (len(kids) - 1)) / len(kids)))
            for k in kids:
                _fit_spec_width(k, each, gap)
        return spec
    for key in ("vconcat", "concat"):
        if key in spec:
            for k in spec[key] or []:
                _fit_spec_width(k, px, gap)
            return spec
    if "spec" in spec:                       # facet / repeat wrapper
        _fit_spec_width(spec["spec"], px, gap)
        return spec
    if isinstance(spec.get("width"), dict):  # {"step": n} -- intentional
        return spec
    spec["width"] = px                       # unit or layer view
    return spec


def render_sheet_to_png(sheet, where_parts=None, scale=2.0, width=None):
    """Render ONE sheet to PNG bytes, WHATEVER channel it draws through:
    Altair (via vl-convert), Plotly/maps (via kaleido) or KPI tiles (drawn
    with PIL). `_capture_all` does the single render+capture pass.

    EXPANDED 2026-08-07 from Altair-only. The old scope note ("Altair-
    rendered sheets only... never a blank image passed off as real") was
    honest but had a cost nobody had measured until every generated image
    pair was actually opened: 5 of Superstore's 10 dashboards are KPI /
    table / map-only, so they produced NO app-side image whatsoever and
    their visual validation was auto-BLOCKED -- and on the dashboards that
    DID render, the missing KPI tiles were the single most-read thing on
    the page. Both exporters were already proven in this repo (kaleido in
    verify_visual.py; PIL in render_dashboard_to_png's compositor), so the
    limit was scope, not capability.

    `width`, when given, is the pixel width of the zone this sheet occupies.
    Every channel that CAN honour it renders at that size (see
    _fit_spec_width) rather than being drawn small and upscaled afterwards --
    upscaling is what turned a correctly-rendered heatmap into an unreadable
    smear of overlapping labels.

    Returns (png_bytes_or_None, reason_or_None)."""
    cap = _capture_all(sheet, where_parts)
    if cap["error"]:
        return None, cap["error"]

    parts, reasons = [], []
    # KPI tiles first: Tableau puts them as the sheet's header row, and they
    # are the numbers a reviewer reads first.
    if cap["kpis"]:
        png, why = _kpi_tiles_to_png(cap["kpis"], canvas_w=width)
        parts.append(png) if png else reasons.append(why)
    if cap["altair"]:
        import vl_convert as vlc
        chart = _arrange_altair(cap["altair"], cap.get("altair_cols"))
        spec = chart.to_dict()
        if width:
            # vl-convert multiplies by `scale`, so ask for width/scale and
            # let the supersampling happen on top of the RIGHT layout.
            _fit_spec_width(spec, max(120, int(width / max(scale, 1))))
        try:
            parts.append(vlc.vegalite_to_png(spec, scale=scale))
        except Exception as e:
            reasons.append(f"vl-convert failed: {type(e).__name__}: {e}")
    if cap["plotly"]:
        png, why = _plotly_to_png(cap["plotly"],
                                  **({"width": int(width)} if width else {}))
        parts.append(png) if png else reasons.append(why)
    if cap["tables"]:
        png, why = _table_to_png(cap["tables"])
        parts.append(png) if png else reasons.append(why)

    if not parts:
        return None, ("; ".join(r for r in reasons if r) or
                      "sheet drew nothing capturable (no chart, no map, no "
                      "KPI tile, no table -- a text/blank scaffolding sheet, "
                      "or nothing matched the filter)")
    stacked = _stack_pngs(parts)
    return stacked, ("; ".join(r for r in reasons if r) or None)


_COMPOSITE_W = 1800       # composite canvas width in px


def _geom_layout_tree(sheets):
    """Build a zone tree in the SAME shape as `dash["layout"]` out of raw
    per-sheet `geom` rectangles, so both dashboard shapes composite through
    one code path instead of two independently-guessed ones. Returns None
    when the sheets carry no usable geometry."""
    rows = engine._rows_from_geom(sheets)
    if not rows:
        return None
    children = []
    for row in rows:
        cells = [{"sheet": s.get("name"), "w": (s.get("geom") or {}).get("w") or 1}
                 for s in row]
        children.append(cells[0] if len(cells) == 1
                        else {"dir": "horz",
                              "w": sum(c["w"] for c in cells),
                              "children": cells})
    return {"dir": "vert", "children": children}


def _composite_zone(node, by_name, where_parts, width, gap, bg, notes, seen):
    """Composite ONE zone of the dashboard's layout tree into a PIL image
    exactly `width` px wide.

    This is the image-side twin of engine._render_layout: it walks the SAME
    `dash["layout"]` tree the live app walks, splits a `horz` zone by the
    SAME `w` weights the app hands to st.columns, and stacks a `vert` zone
    in the SAME child order. FIXED 2026-08-07: the previous compositor threw
    that tree away for `[[s] for s in dash["sheets"]]` -- one sheet per row
    in sheet-list order -- so a correctly-rendering app was captured with
    its zones in the wrong order and side-by-side charts stacked vertically
    (Superstore's Customer Analysis captured as scatter / rank / KPI-row
    instead of KPI-row on top with scatter and rank beside each other).
    That made every Tableau-vs-app image pair mismatch on LAYOUT before a
    single mark was compared.

    Leaf images are scaled to their zone's allocated width, so a zone's
    share of the canvas comes from the workbook's own proportions rather
    than from whatever pixel size vl-convert happened to emit. Heights stay
    natural (each chart keeps its aspect ratio) -- stretching a chart to its
    Tableau zone height would distort the marks being compared.

    Returns a PIL.Image or None when nothing in the zone rendered."""
    from PIL import Image

    if "sheet" in node:
        s = by_name.get(node["sheet"])
        if s is None:
            return None
        seen.add(node["sheet"])
        png, reason = render_sheet_to_png(s, where_parts, width=width)
        notes.append({"sheet": s.get("name"), "rendered": png is not None,
                      "reason": reason})
        if png is None:
            return None
        im = Image.open(io.BytesIO(png)).convert("RGB")
        if im.width > width:            # supersampled (scale=2) -> downscale
            h = max(1, round(im.height * width / im.width))
            im = im.resize((width, h), Image.LANCZOS)
        elif im.width < width:
            # PAD, never magnify. A channel that can't be told a width (a
            # rendered table) keeps its real size and sits at the left of
            # its zone, exactly as it does in the app -- blowing it up would
            # invent detail and, on text, destroy legibility.
            pad = Image.new("RGB", (width, im.height), bg)
            pad.paste(im, (0, 0))
            im = pad
        return im

    kids = node.get("children") or []
    if not kids:
        return None

    if node.get("dir") == "horz" and len(kids) > 1:
        weights = [max(1, k.get("w") or 1) for k in kids]
        avail = max(1, width - gap * (len(kids) - 1))
        widths = [max(1, int(avail * w / sum(weights))) for w in weights]
        widths[-1] = max(1, avail - sum(widths[:-1]))
        cells = [(_composite_zone(k, by_name, where_parts, w, gap, bg, notes, seen), w)
                 for k, w in zip(kids, widths)]
        if not any(im is not None for im, _ in cells):
            return None
        height = max(im.height for im, _ in cells if im is not None)
        canvas = Image.new("RGB", (width, height), bg)
        x = 0
        for im, w in cells:
            # advance by the ALLOCATED width even when a cell didn't render,
            # so the surviving zones stay in their real horizontal positions
            # instead of sliding left into the gap.
            if im is not None:
                canvas.paste(im, (x, 0))     # top-aligned, as Tableau rows are
            x += w + gap
        return canvas

    parts = [_composite_zone(k, by_name, where_parts, width, gap, bg, notes, seen)
             for k in kids]
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    total_h = sum(p.height for p in parts) + gap * (len(parts) - 1)
    canvas = Image.new("RGB", (width, total_h), bg)
    y = 0
    for p in parts:
        canvas.paste(p, (0, y))
        y += p.height + gap
    return canvas


def _title_band(text, width, bg="white", pad=14):
    """Draw the dashboard's own title above the composite. Tableau's REST
    view image carries the dashboard title, so without one the two images
    differ on the first thing a reviewer (or a vision model) reads. The text
    is the workbook's own `title`/`name` -- nothing invented."""
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.load_default(size=34)
    except TypeError:
        font = ImageFont.load_default()
    img = Image.new("RGB", (width, 34 + pad * 2), bg)
    ImageDraw.Draw(img).text((pad, pad), str(text)[:90], fill="#1a1a1a", font=font)
    return img


def render_dashboard_to_png(dash, where_parts=None, gap=16, bg="white",
                            width=_COMPOSITE_W, title=True):
    """Render a WHOLE dashboard to ONE composite PNG -- the unit that maps
    onto a Tableau VIEW/tab, matching what tableau_server.pull_all_view_images
    pulls per dashboard.

    The composite walks the dashboard's REAL zone tree (`dash["layout"]`, the
    same tree engine._render_layout hands to st.columns; or one rebuilt from
    per-sheet `geom` rectangles via engine._rows_from_geom when the workbook
    declares no container tree), so zone ORDER, GROUPING and WIDTH SHARE all
    come from the workbook itself.

    STILL AN APPROXIMATION, stated plainly: zone HEIGHTS are each chart's
    natural rendered height, not the workbook's zone height, and Tableau's
    right-hand filter/legend cards are not drawn (the headless path mocks
    every input widget away -- see _mocked_widgets). It is not pixel-exact
    and is never presented as such.

    Returns (png_bytes_or_None, notes). notes is a list of {"sheet",
    "rendered": bool, "reason": str_or_None} -- one entry per sheet in the
    dashboard, so a caller can see exactly which sheets contributed to the
    composite and which didn't, never a silent partial image. Returns
    (None, notes) when NO sheet rendered (a caption-only/empty dashboard, or
    every sheet failed) rather than an empty canvas passed off as real."""
    from PIL import Image

    if where_parts is None:
        # engine.build_where() itself creates real INPUT widgets (a date-
        # range picker, filter dropdowns) keyed by dashboard/field name --
        # mocked here for the same reason render_sheet_to_png mocks them
        # (see _mocked_widgets' docstring): the live app already rendered
        # this dashboard once elsewhere in the same script run, so calling
        # build_where for real a second time collides on those same keys.
        with _mocked_widgets():
            where_parts = engine.build_where(dash)

    sheets = dash.get("sheets") or []
    by_name = {s.get("name"): s for s in sheets}
    tree = dash.get("layout") or _geom_layout_tree(sheets)
    if not tree:                                # no geometry at all -> 2 per row
        tree = {"dir": "vert", "children": [
            {"dir": "horz", "children": [{"sheet": s.get("name"), "w": 1}
                                         for s in sheets[i:i + 2]]}
            for i in range(0, len(sheets), 2)]}

    notes, seen = [], set()
    body = _composite_zone(tree, by_name, where_parts, width, gap, bg, notes, seen)

    # A sheet the layout tree never referenced must still be rendered and
    # noted -- never silently dropped because the workbook's container tree
    # and its sheet list disagree.
    for s in sheets:
        if s.get("name") in seen:
            continue
        extra = _composite_zone({"sheet": s.get("name")}, by_name, where_parts,
                                width, gap, bg, notes, seen)
        if extra is None:
            continue
        if body is None:
            body = extra
        else:
            canvas = Image.new("RGB", (width, body.height + gap + extra.height), bg)
            canvas.paste(body, (0, 0))
            canvas.paste(extra, (0, body.height + gap))
            body = canvas

    if body is None:
        return None, notes

    if title and (dash.get("title") or dash.get("name")):
        band = _title_band(dash.get("title") or dash.get("name"), width, bg)
        canvas = Image.new("RGB", (width, band.height + body.height), bg)
        canvas.paste(band, (0, 0))
        canvas.paste(body, (0, band.height))
        body = canvas

    buf = io.BytesIO()
    body.save(buf, format="PNG")
    return buf.getvalue(), notes
