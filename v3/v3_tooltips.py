"""V10: Tableau's custom tooltips (<customized-tooltip>) on the plan's charts.

Tableau's tooltip is formatted text with field references (<[ds].[sum:Sales:qk]>), one line per paragraph. The chart
shows each line as one tooltip row: a line that starts with a label ('Sales:') becomes that label and its value text;
any other line is shown as its text. Field references are replaced by the mark's value in the field's own format.
<Sheet name=...> (viz in tooltip) and references to fields that are not on the view are left out."""
import re

_RUN = re.compile(r'<run\b[^>]*>(.*?)</run>', re.S)
_REF = re.compile(r'<(\[[^\]]+\]\.\[[^\]]+\])>')


def _text(run):
    t = run
    m = re.match(r'\s*<!\[CDATA\[(.*)\]\]>\s*$', t, re.S)
    if m:
        t = m.group(1)
    else:
        t = (t.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&apos;', "'")
             .replace('&#10;', '\n').replace('&#9;', '\t').replace('&amp;', '&'))
    return t.replace('Æ', '')


def parse(xml):
    """The custom tooltip as lines of parts: [[{'text': ...} | {'token': '[ds].[agg:name:qk]'}]], or None."""
    m = re.search(r'<customized-tooltip\b[^>]*>(.*?)</customized-tooltip>', xml or '', re.S)
    if not m:
        return None
    text = ''.join(_text(r) for r in _RUN.findall(m.group(1)))
    lines = []
    for raw in text.split('\n'):
        if '<Sheet name=' in raw:
            continue                                   # viz in tooltip: not drawn
        parts, pos = [], 0
        for ref in _REF.finditer(raw):
            if ref.start() > pos:
                parts.append({'text': raw[pos:ref.start()]})
            parts.append({'token': ref.group(1)})
            pos = ref.end()
        if pos < len(raw):
            parts.append({'text': raw[pos:]})
        if any(p.get('token') or p['text'].strip() for p in parts):
            lines.append(parts)
    return lines or None


def bind(lines, by_token):
    """Field references -> plan field ids (by the occurrence token on the view)."""
    out = []
    for line in lines or []:
        bound = []
        for p in line:
            if 'token' in p:
                f = by_token.get(p['token'])
                bound.append({'field': f['id']} if f else {'missing': p['token']})
            else:
                bound.append(p)
        out.append(bound)
    return out


def _value_texts(plan, df, engine):
    """{field id: [display text per row]} for every field of the plan present in the result."""
    import v3_sheet_plan as P
    out = {}
    for f in plan['fields']:
        try:
            if f['kind'] == 'measure' and not f.get('measure_values'):
                col = P.col_name(f, True) + ('|tc' if f.get('table_calc') else '')
                if col in df.columns:
                    out[f['id']] = [P.format_value(v, f.get('format')) for v in df[col].tolist()]
            elif f['kind'] == 'dim':
                col = P.col_name(f)
                if col in df.columns:
                    shown = P.formatted_frame(dict(plan, fields=[f]), df[[col]], engine)
                    t = P._title(f)
                    if t in shown.columns:
                        out[f['id']] = ['' if v is None else str(v) for v in shown[t].tolist()]
        except Exception:
            continue
    return out


def columns(plan, df, engine):
    """[(title, [text per row])] -- one per tooltip line, in order."""
    values = _value_texts(plan, df, engine)
    n = len(df)
    out = []
    for i, line in enumerate(plan.get('tooltip_lines') or []):
        if any('missing' in p for p in line):
            if not any('field' in p for p in line):
                continue                      # its only fields are not on the view: Tableau would show no value
            line = [p for p in line if 'missing' not in p]
        if not line:
            continue
        title = None
        if 'text' in line[0] and re.match(r'^\s*[^:\t]+:\s*$', line[0]['text']) and len(line) > 1:
            title = line[0]['text'].strip().rstrip(':').strip()
            line = line[1:]
        texts = []
        for r in range(n):
            s = ''.join(p['text'] if 'text' in p else (values.get(p['field']) or [''] * n)[r] for p in line)
            texts.append(' '.join(s.replace('\t', ' ').split()))
        if not any(texts):
            continue
        out.append((title if title is not None else '​' * (i + 1), texts))
    return out
