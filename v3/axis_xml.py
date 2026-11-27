"""Release 27 (F3 / F8): axis, mark and reference-line rules read straight from a worksheet's XML.

Used by every chart renderer (dual axis, scatter, ...) so a chart draws what Tableau draws:

  <style-rule element='axis'><format attr='display' field=F value='false'>   that field's axis is hidden
  <style-rule element='axis'><format attr='title'   field=F value=''>        that axis has NO title
  <encoding attr='space' field=F synchronized='true'>                       dual axes share one scale
  <encoding attr='space' field=F domain-expand='false'>                     axis fits the data (no zero)
      (absent: Tableau's default "include zero" for a quantitative axis)
  <pane><style><style-rule element='mark'> mark-color / mark-transparency / has-stroke / stroke-color
  <reference-line formula= label-type= label=> + <style-rule element='refline'> text-format / color / font

Field matching is by the full column token ([datasource].[agg:name:qk]) or its last bracket part.
"""
import xml.etree.ElementTree as ET

_TIME_FMT = {"DAY": "%b %-d", "WEEK": "%b %-d", "MONTH": "%b %Y", "QUARTER": "Q%q %Y", "YEAR": "%Y"}


def _last(token):
    token = str(token or "")
    return token[token.rfind("[") + 1:].rstrip("]") if "[" in token else token


def _same(a, b):
    return bool(a and b) and (a == b or _last(a) == _last(b))


def parse(source_xml):
    """Worksheet XML text -> rules dict (empty pieces when absent; never raises on missing parts)."""
    try:
        ws = ET.fromstring(source_xml) if isinstance(source_xml, str) else source_xml
    except ET.ParseError:
        return {"axes": {}, "space": {}, "marks": {}, "refline_style": {}, "reflines": [], "trend_style": {},
                "field_labels": {}, "label_angle": {}}
    axes = {}
    for sr in ws.findall("./table/style/style-rule"):
        if sr.get("element") != "axis":
            continue
        for f in sr.findall("format"):
            if f.get("field"):
                axes.setdefault(f.get("field"), {})[f.get("attr")] = f.get("value")
    space = {}
    for e in ws.iter("encoding"):
        if e.get("attr") == "space" and e.get("field"):
            space[e.get("field")] = {"synchronized": e.get("synchronized") == "true",
                                     "zero": e.get("domain-expand") != "false", "scope": e.get("scope")}
    marks = {}
    for pane in ws.findall(".//panes/pane"):
        for sr in pane.findall("./style/style-rule"):
            if sr.get("element") == "mark":
                marks[pane.get("id") or "0"] = {f.get("attr"): f.get("value") for f in sr.findall("format")}
    for sr in ws.findall("./table/style/style-rule"):
        if sr.get("element") == "mark":                       # worksheet-level mark style: every pane
            marks.setdefault("table", {f.get("attr"): f.get("value") for f in sr.findall("format")})
    trend_style = {}
    for sr in ws.iter("style-rule"):                       # worksheet or pane level
        if sr.get("element") == "trendline":
            for f in sr.findall("format"):
                trend_style[f.get("attr")] = f.get("value")
    ref_style = {}
    for sr in ws.findall("./table/style/style-rule"):
        if sr.get("element") == "refline":
            for f in sr.findall("format"):
                ref_style[f.get("attr")] = f.get("value")        # later formats win, as in Tableau
    reflines = [dict(r.attrib) for r in ws.iter("reference-line")]
    field_labels = {}
    for sr in ws.iter("style-rule"):
        if sr.get("element") == "worksheet":
            for f in sr.findall("format"):
                if f.get("attr") == "display-field-labels":
                    field_labels[f.get("scope") or "*"] = f.get("value") != "false"
    label_angle = {}
    for sr in ws.findall("./table/style/style-rule"):
        if sr.get("element") == "label":
            for f in sr.findall("format"):
                if f.get("attr") == "text-orientation" and f.get("field"):
                    try:
                        label_angle[f.get("field")] = -int(float(f.get("value")))
                    except (TypeError, ValueError):
                        pass
    return {"axes": axes, "space": space, "marks": marks, "refline_style": ref_style, "reflines": reflines,
            "trend_style": trend_style, "field_labels": field_labels, "label_angle": label_angle}


def axis_hidden(rules, token):
    return any(_same(f, token) and v.get("display") == "false" for f, v in rules["axes"].items())


def axis_title(rules, token, default):
    """'' authored -> None (no title); otherwise the default title."""
    for f, v in rules["axes"].items():
        if _same(f, token) and "title" in v:
            return None if v["title"] == "" else v["title"]
    return default


def include_zero(rules, token):
    for f, v in rules["space"].items():
        if _same(f, token):
            return v["zero"]
    return True


def shelf_zero(rules, scope):
    """Include zero on the quantitative axis of a shelf ('rows' / 'cols'): False when every authored space
    encoding on that shelf says domain-expand='false'; Tableau's default (True) otherwise."""
    vals = [v["zero"] for v in rules["space"].values() if v.get("scope") == scope]
    return all(vals) if vals else True


def synchronized(rules):
    return any(v["synchronized"] for v in rules["space"].values())


def mark_style(rules, pane_id=None):
    """(fill colour, fill opacity, stroke colour or None) of a pane's marks (pane id, else the first styled)."""
    m = rules["marks"].get(str(pane_id)) if pane_id is not None else None
    m = m or next(iter(rules["marks"].values()), {})
    color = m.get("mark-color")
    try:
        opacity = int(m.get("mark-transparency", 255)) / 255.0
    except ValueError:
        opacity = 1.0
    stroke = m.get("stroke-color") if m.get("has-stroke") == "true" else None
    return color, opacity, stroke


def refline_for(rules, token):
    return next((r for r in rules["reflines"] if _same(r.get("value-column"), token) or _same(r.get("axis-column"), token)), None)


def refline_label(rules, ref, value, field_format=None):
    """The text Tableau prints beside a reference line ('' when it prints none)."""
    from tableau_format import format_number
    if not ref or ref.get("label-type") in (None, "none"):
        return ""
    # the field's own format first (Tableau: "Avg: $11,899" on a currency axis), the refline format otherwise
    fmt = field_format or rules["refline_style"].get("text-format")
    shown = (format_number(value, fmt) if fmt else None) or f"{value:,.1f}"
    shown = shown.strip()
    if ref.get("label-type") == "custom":
        return (ref.get("label") or "<Value>").replace("<Value>", shown)
    return shown


def d3(fmt):
    """Tableau pattern -> d3 axis format (best effort; '' = Vega default)."""
    try:
        from v3_sheet_plan import d3_format
        return d3_format(fmt) or ""
    except Exception:
        return ""


def time_format(part):
    return _TIME_FMT.get(str(part or "").upper(), "%b %-d, %Y")


# ---------------------------------------------------------------- CX2 fix queue (release 27): general rules
# Each rule is keyed off one Tableau XML construct and is used by every chart renderer.

_UNIT_DAYS = {"DAY": 1, "WEEK": 7, "MONTH": 30.44, "QUARTER": 91.3, "YEAR": 365.25}


def table_background(ws_style):
    """<style-rule element='table'><format attr='background-color'> (Format > Shading > Worksheet) ->
    'transparent' for Tableau's 'None' (#rrggbb00), '#rrggbb' for a colour, None when not authored (keep the
    app default). A worksheet with no shading lets its dashboard container show through, as in Tableau."""
    value = ((ws_style or {}).get("table") or {}).get("background-color")
    if not value or not str(value).startswith("#"):
        return None
    value = str(value)
    if len(value) == 9:
        return "transparent" if value[7:].lower() == "00" else value[:7]
    return value[:7]


def automatic_mark_size(source_xml):
    """True when every pane leaves mark size automatic (<mark-sizing use-custom-mark-size='false'> or no
    mark-sizing element): Tableau then sizes a bar on a continuous date axis to fill one date unit."""
    try:
        ws = ET.fromstring(source_xml) if isinstance(source_xml, str) else source_xml
    except ET.ParseError:
        return True
    return not any(ms.get("use-custom-mark-size") == "true" for ms in ws.iter("mark-sizing"))


def date_bar_px(dates, part, plot_px, fill=0.88):
    """Pixel width of a bar on a CONTINUOUS date axis whose size is automatic: Tableau fills each date unit
    (one day, week, month ...) with a bar and a ~1px gap. dates: the distinct plotted dates."""
    import pandas as pd
    d = pd.to_datetime(pd.Series(list(dates))).dropna()
    if d.empty or not plot_px:
        return None
    unit = _UNIT_DAYS.get(str(part or "").upper(), 1)
    span_units = max(1, round((d.max() - d.min()).days / unit) + 1)
    return max(1, int(plot_px / span_units * fill))


def axis_tick_count(plot_px, spacing=100):
    """Tableau labels a horizontal quantitative axis at about one tick per 100px ($0, $20,000, $40,000,
    $60,000 over ~400px); Vega's default is one per 40px. Used as Vega's tickCount (it still picks nice steps)."""
    try:
        return max(2, int(round(float(plot_px) / spacing)))
    except (TypeError, ValueError):
        return None


def circle_size(rules, pane_id=None):
    """<pane><style-rule element='mark'><format attr='size' value=S> -> Vega circle area in px^2, or None when
    the size is not authored. Tableau draws a circle of diameter ~10px x S at the authored slider value."""
    m = rules["marks"].get(str(pane_id)) if pane_id is not None else None
    m = m or next((v for v in rules["marks"].values() if "size" in v), {})
    try:
        s = float(m.get("size"))
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    d = 10.0 * s
    return round(3.14159 / 4 * d * d)


_DASH = {"dotted": [2, 2], "dashed": [6, 4], "solid": None}


def trend_line_style(rules):
    """<style-rule element='trendline'> -> (colour '#rrggbb', opacity, dash list or None, width). Tableau's
    default trend line is a thin solid grey line."""
    t = rules.get("trend_style") or {}
    color = t.get("stroke-color") or "#555555"
    opacity = 1.0
    if len(color) == 9:
        try:
            opacity = int(color[7:], 16) / 255.0
        except ValueError:
            pass
    try:
        width = max(0.5, float(t.get("stroke-size") or 1))
    except ValueError:
        width = 1.0
    return color[:7], opacity, _DASH.get(t.get("line-pattern-only"), None), width


def field_labels_shown(rules, scope):
    """<style-rule element='worksheet'><format attr='display-field-labels' [scope=rows|cols] value='false'>:
    the field name over a DISCRETE header (e.g. 'Order Date' under Jan..Dec) is not drawn. A format without a
    scope applies to both shelves."""
    fl = rules.get("field_labels") or {}
    if scope in fl:
        return fl[scope]
    return fl.get("*", True)


def label_angle(rules, token):
    """<style-rule element='label'><format attr='text-orientation' field=F value=V>: Tableau's label
    rotation for that field's header or axis (0 = horizontal, 90 = rotated) as a Vega labelAngle, else None."""
    for f, v in (rules.get("label_angle") or {}).items():
        if _same(f, token):
            return v
    return None
