"""Release 27 (F1): Tableau number-format strings, implemented once and used by every renderer.

The workbook XML stores a field's display format as an Excel-style pattern, optionally prefixed with Tableau's
type letter, e.g.

    c"$"#,##0;-"$"#,##0         currency, explicit negative section
    n#,##0" days";-#,##0" days" number with literal text
    p0.0%                       percent (value x 100)
    *0.0%; 0.0%                 custom: the negative section has NO minus (Tableau shows 5.6% for -0.056)
    *▲ 0.0%                     custom: literal glyph + space before the number
    #,##0.0,"K"                 trailing comma = divide by 1,000 (display units)

format_number(value, pattern) returns the text Tableau shows, or None when the pattern is not a number format this
module implements (dates, scientific notation, Tableau locale tokens such as C1033), so the caller keeps its own
fallback instead of showing something invented.
"""
import math
import re
from decimal import Decimal, ROUND_HALF_UP

_TYPE_LETTERS = set('cnpCNP*')
_DIGITS = set('0#')


def _split_sections(pattern):
    out, cur, quoted = [], [], False
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == '\\' and i + 1 < len(pattern):
            cur.append(pattern[i:i + 2]); i += 2; continue
        if ch == '"':
            quoted = not quoted
        if ch == ';' and not quoted:
            out.append(''.join(cur)); cur = []
        else:
            cur.append(ch)
        i += 1
    out.append(''.join(cur))
    return out


def _tokens(section):
    """Section -> (prefix literal, core placeholder string, suffix literal, percent?)."""
    lits, core, after, pct = [[], []], [], False, False
    i = 0
    while i < len(section):
        ch = section[i]
        if ch == '"':
            j = section.find('"', i + 1)
            j = len(section) if j < 0 else j
            lits[1 if after or core else 0].append(section[i + 1:j]); i = j + 1; continue
        if ch == '\\' and i + 1 < len(section):
            lits[1 if after or core else 0].append(section[i + 1]); i += 2; continue
        if not after and (ch in _DIGITS or (core and ch in ',.') or (ch == '.' and i + 1 < len(section) and section[i + 1] in _DIGITS)):
            core.append(ch); i += 1; continue
        if core:
            after = True
        if ch == '%':
            pct = True
        lits[1 if core else 0].append(ch)
        i += 1
    return ''.join(lits[0]), ''.join(core), ''.join(lits[1]), pct


def _format_core(x, core):
    """Digits for |x| from a placeholder core like '#,##0.0' or '#,##0,,' (trailing commas scale by 1,000)."""
    scale = 0
    while core.endswith(','):
        scale += 1; core = core[:-1]
    int_part, _, dec_part = core.partition('.')
    while int_part.endswith(','):                    # '#,##0,.0K': commas just left of the point scale too
        scale += 1; int_part = int_part[:-1]
    x = x / (1000 ** scale)
    zeros = dec_part.count('0'); hashes = dec_part.count('#')
    decimals = zeros + hashes
    grouping = ',' in int_part
    # Tableau rounds half away from zero (1234.5 -> 1,235); Python's format rounds half to even
    q = Decimal(repr(x)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    text = f'{q:,.{decimals}f}' if grouping else f'{q:.{decimals}f}'
    if hashes and '.' in text:                       # optional decimals: drop trailing zeros beyond the 0s
        whole, frac = text.split('.')
        frac = frac[:zeros] + frac[zeros:].rstrip('0')
        text = whole + ('.' + frac if frac else '')
    min_int = int_part.replace(',', '').count('0')
    if min_int == 0 and text.startswith('0') and (len(text) == 1 or text[1] == '.'):
        text = text[1:] if text != '0' else text     # '#.00' shows .50, '#' shows 0 as 0
    return text


def format_number(value, pattern):
    """The text Tableau displays for value with this format pattern, or None (unsupported / not a number)."""
    if pattern is None or value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    pat = str(pattern)
    if not pat or re.fullmatch(r'[CNP]\d+%?', pat) or re.search(r'[yYdDhHsS]{1,4}', pat.replace('"', '')[:1]):
        return None
    if pat[0] in _TYPE_LETTERS and len(pat) > 1:
        pat = pat[1:]
    if re.search(r'(?<!")[eE][+-]', pat):
        return None                                   # scientific notation: caller's fallback
    secs = _split_sections(pat)
    if v < 0 and len(secs) >= 2 and secs[1] != '':
        sec, x, sign = secs[1], abs(v), ''
    elif v == 0 and len(secs) >= 3 and secs[2] != '':
        sec, x, sign = secs[2], 0.0, ''
    else:
        sec, x, sign = secs[0], abs(v), ('-' if v < 0 else '')
    prefix, core, suffix, pct = _tokens(sec)
    if not core:
        # a section of pure text ('#,##0;-;-' shows negatives and zero as '-'): Tableau shows that text as is
        return (prefix + suffix) if (len(secs) > 1 and sec is not secs[0]) else None
    if pct:
        x *= 100
    digits = _format_core(x, core)
    if sign and float(re.sub(r'[^0-9.]', '', digits) or 0) == 0:
        sign = ''                                     # -0.0 shows as 0.0
    return f'{sign}{prefix}{digits}{suffix}'
