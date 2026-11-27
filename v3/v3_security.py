"""E11: secret custody, endpoint allowlist and redaction.

tableau_url_allowed()  the app talks to Tableau only over https, to Tableau Cloud (*.online.tableau.com / *.tableau.com)
                       or to a host the owner lists in TABLEAU_ALLOWED_HOSTS (comma separated).
redact()               masks credentials in any text that can reach the screen, a log or a report.
scan()                 finds credential-shaped literals in source / artifacts (the release gate runs it over the
                       packaged modules)."""
import os
import re
from urllib.parse import urlparse

TABLEAU_SUFFIXES = ('.online.tableau.com', '.tableau.com')
MASK = '***'

_PATTERNS = [
    # XML / attribute / key=value credentials (keep the name, drop the value)
    (re.compile(r'((?:personalAccessTokenSecret|password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)'
                r'\s*[=:]\s*["\']?)([^"\'\s,;<>&]{4,})', re.I), r'\1' + MASK),
    (re.compile(r'(X-Tableau-Auth\s*[:=]\s*)(\S+)', re.I), r'\1' + MASK),
    (re.compile(r'(Authorization\s*[:=]\s*(?:Bearer|Basic|Snowflake Token=)?\s*)(\S+)', re.I), r'\1' + MASK),
    # JWTs and Tableau PAT secrets (base64 'xxxx==:yyyy')
    (re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'), MASK),
    (re.compile(r'\b[A-Za-z0-9+/]{16,}={0,2}:[A-Za-z0-9]{20,}\b'), MASK),
    # private keys
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S),
     '-----PRIVATE KEY ' + MASK + '-----'),
]


def allowed_hosts():
    return [h.strip().lower() for h in (os.environ.get('TABLEAU_ALLOWED_HOSTS') or '').split(',') if h.strip()]


def tableau_url_allowed(url):
    try:
        u = urlparse(str(url or '').strip())
    except ValueError:
        return False
    host = (u.hostname or '').lower()
    if u.scheme != 'https' or not host:
        return False
    if host in allowed_hosts():
        return True
    return any(host.endswith(s) for s in TABLEAU_SUFFIXES)


def redact(text):
    out = str(text if text is not None else '')
    for rx, rep in _PATTERNS:
        out = rx.sub(rep, out)
    return out


# a credential written into source: a quoted literal assigned to a credential-named variable, a PAT, a JWT, a key
_LITERAL = re.compile(r'(?:password|passwd|pwd|secret|token|api[_-]?key)\w*\s*[=:]\s*["\']([^"\']{6,})["\']', re.I)
_SAFE_VALUES = re.compile(r'^(tableau_pat|[A-Z0-9_]+_(?:SECRET|TOKEN|PASSWORD)|[\w.]*SECRET[\w.]*|\*+|<[^>]+>|'
                          r'\{[^}]*\}|\$\w+|%\(\w+\)s|xxx+|changeme|redacted|\.\.\.)$', re.I)


def scan(text):
    """[findings] -- credential-shaped literals in text (variable names, env lookups and secret object names pass)."""
    found = []
    for m in _LITERAL.finditer(text or ''):
        v = m.group(1)
        if _SAFE_VALUES.match(v) or ' ' in v or v.startswith(('http', '/', '[', 'SELECT', 'select')):
            continue
        found.append('credential literal')
    for rx, _rep in _PATTERNS[3:]:
        if rx.search(text or ''):
            found.append('credential pattern')
    return found
