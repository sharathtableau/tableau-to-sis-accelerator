"""Cortex token usage and cost for a migration run.

Snowflake reports exact token counts when AI_COMPLETE is called with show_details => TRUE
(usage.prompt_tokens / usage.completion_tokens). unwrap() takes that detailed response apart and
returns the same payload the caller always used, plus the usage; a response without details (older
callers, test doubles) passes through unchanged with usage None.

Cost = tokens x the model's AI-credit rate per million tokens x the price of an AI credit. Rates are
list prices to confirm for the customer's contract and region; override with V3_AI_RATES (JSON
{"model": [input, output]}), V3_AI_CREDIT_USD, V3_ANALYST_CREDITS_PER_MESSAGE and
V3_PLATFORM_CREDIT_USD. Image tokens in the vision step are not reported by this call shape, so
those rows are estimates and labelled as such.
"""
import json
import os

DEFAULT_RATES = {                      # AI credits per million tokens: (input, output)
    'claude-sonnet-4-5': (1.80, 9.00),
    'claude-opus-5': (3.00, 15.00),
}
RATE_NOTE = ('List rates (Snowflake Service Consumption Table, Oct 2026) to confirm for the account; '
             'warehouse time is billed separately.')


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def rates():
    out = dict(DEFAULT_RATES)
    try:
        extra = json.loads(os.environ.get('V3_AI_RATES') or '{}')
        for k, v in extra.items():
            out[str(k)] = (float(v[0]), float(v[1]))
    except (ValueError, TypeError, IndexError, KeyError):
        pass
    return out


def credit_usd():
    return _env_float('V3_AI_CREDIT_USD', 2.00)


def analyst_credits_per_message():
    return _env_float('V3_ANALYST_CREDITS_PER_MESSAGE', 0.067)


def platform_credit_usd():
    return _env_float('V3_PLATFORM_CREDIT_USD', 3.00)


def _load(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return raw
    if isinstance(raw, str):            # VARIANT rendered as a JSON string
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    return raw


def unwrap(raw):
    """(payload, usage) from an AI_COMPLETE result. usage = {'input': n, 'output': n} or None."""
    obj = _load(raw)
    if not isinstance(obj, dict) or not ('usage' in obj or 'structured_output' in obj or 'choices' in obj):
        return raw, None
    u = obj.get('usage') if isinstance(obj.get('usage'), dict) else None
    try:
        usage = None if u is None else {'input': int(u.get('prompt_tokens') or 0),
                                        'output': int(u.get('completion_tokens') or 0)}
    except (TypeError, ValueError):
        usage = None
    if 'structured_output' in obj:
        so = obj['structured_output']
        item = so[0] if isinstance(so, list) and so else so
        payload = item.get('raw_message', item) if isinstance(item, dict) else item
        return payload, usage
    if 'choices' in obj:
        ch = obj['choices']
        item = ch[0] if isinstance(ch, list) and ch else ch
        if isinstance(item, dict):
            payload = item.get('messages', item.get('message', item.get('text')))
            if isinstance(payload, dict):
                payload = payload.get('content', payload)
            return payload, usage
        return item, usage
    return raw, None


def credits(model, input_tokens, output_tokens):
    rate = rates().get(str(model or '').lower())
    if rate is None or input_tokens is None:
        return None
    return (input_tokens * rate[0] + (output_tokens or 0) * rate[1]) / 1_000_000


def annotate(entry, model, usage, source='Reported by Snowflake'):
    """Add token and cost columns to an activity-log entry."""
    if not usage:
        return entry
    c = credits(model, usage.get('input'), usage.get('output'))
    entry.update({'Input tokens': usage.get('input'), 'Output tokens': usage.get('output'),
                  'AI credits': None if c is None else round(c, 5),
                  'Cost USD': None if c is None else round(c * credit_usd(), 4),
                  'Token source': source})
    return entry


def analyst_cost(messages=1):
    c = analyst_credits_per_message() * messages
    return round(c, 4), round(c * platform_credit_usd(), 4)


def image_tokens(png_bytes):
    """Rough image token estimate (width x height / 750), used only for labelled estimates."""
    try:
        if png_bytes[:8] != b'\x89PNG\r\n\x1a\n':
            return 0
        w = int.from_bytes(png_bytes[16:20], 'big')
        h = int.from_bytes(png_bytes[20:24], 'big')
        return (w * h) // 750
    except Exception:
        return 0


def totals(rows):
    """Run totals from activity rows that carry token columns."""
    t = {'calls': 0, 'input': 0, 'output': 0, 'credits': 0.0, 'usd': 0.0, 'estimated_calls': 0,
         'analyst_messages': 0, 'analyst_usd': 0.0}
    for r in rows or []:
        if r.get('Input tokens') is not None:
            t['calls'] += 1
            t['input'] += int(r.get('Input tokens') or 0)
            t['output'] += int(r.get('Output tokens') or 0)
            t['credits'] += float(r.get('AI credits') or 0)
            t['usd'] += float(r.get('Cost USD') or 0)
            if str(r.get('Token source', '')).startswith('Estimated'):
                t['estimated_calls'] += 1
        if r.get('Messages'):
            t['analyst_messages'] += int(r['Messages'])
            t['analyst_usd'] += float(r.get('Cost USD') or 0)
    t['credits'] = round(t['credits'], 5)
    t['usd'] = round(t['usd'], 4)
    t['analyst_usd'] = round(t['analyst_usd'], 4)
    return t


def stage_totals(rows):
    """EN03: cost per workflow stage -- calls, tokens, credits, USD, models, failed and ceiling-stopped calls."""
    out = {}
    for r in rows or []:
        st = str(r.get('Step') or '—')
        o = out.setdefault(st, {'Stage': st, 'Calls': 0, 'Tokens in': 0, 'Tokens out': 0, 'AI credits': 0.0,
                                'Cost USD': 0.0, 'Failed': 0, 'Stopped by ceiling': 0, '_models': []})
        if r.get('Status') == 'Failed':
            o['Failed'] += 1
        if 'CEILING' in str(r.get('Note') or ''):
            o['Stopped by ceiling'] += 1
        if r.get('Input tokens') is not None or r.get('Messages'):
            o['Calls'] += 1
            o['Tokens in'] += int(r.get('Input tokens') or 0)
            o['Tokens out'] += int(r.get('Output tokens') or 0)
            o['AI credits'] += float(r.get('AI credits') or 0)
            o['Cost USD'] += float(r.get('Cost USD') or 0)
            m = r.get('Model')
            if m and m not in o['_models']:
                o['_models'].append(m)
    rows_out = []
    for o in out.values():
        o['Models'] = ', '.join(o.pop('_models'))
        o['AI credits'] = round(o['AI credits'], 5)
        o['Cost USD'] = round(o['Cost USD'], 4)
        rows_out.append(o)
    return rows_out
