"""Release 20b (SL01): estate catalogue. Read every workbook in a Tableau project (or a folder) without
building any app, and record its business logic in Snowflake catalogue tables.

What is recorded, per workbook (all from the parsed workbook, never from Cortex):
- datasources: connections, tables, shape, a source key, and the route a migration run takes. The route
  comes from the same code the migration uses (v3_source_binding.source_candidates and
  v3_data_load.locate), so the catalogue and a run cannot disagree on what is bound read-only, loaded
  after approval, loaded from a laptop, or named for a later release;
- fields (physical column, type, role) and calculations (original formula, translated SQL, kind,
  dependencies, parameters) with a formula fingerprint. Field references are replaced by physical
  column names, references to other calculations are expanded, and spacing, case and comments are
  ignored, so the same formula written in several workbooks gets the same fingerprint;
- metrics: every calculation that aggregates, and every measure a sheet aggregates (SUM of Sales), with
  a metric fingerprint;
- usage: which worksheet (and dashboard) uses which field or metric, on which shelf, with which
  aggregation; filters are usage rows on the 'filter' shelf;
- Tableau view counts per published view (Tableau REST, read-only).

Cortex (AI_COMPLETE) names, describes and classifies each DISTINCT metric (one call per batch of up to
40, at most 25 calls per harvest, cached by fingerprint). Its answer is checked by code (known metric
id, closed intent list, length, no numbers that are not in the formula) and stored as a proposal; it
never changes a fingerprint, a route or lineage.

Rules: read-only on Tableau; no app is built; no workbook or sheet name is used as logic; the app only
INSERTs and SELECTs (tables are created by sql/en01_evidence_store.sql). A workbook whose .twb content
has not changed since the last harvest (same catalogue version) is not parsed again.
"""
import datetime
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
import io

CATALOG_VERSION = 'cat/3'          # cat/2: extract on Tableau -> laptop route; cat/3: Snowflake bindings (release 22)
INTENTS = ('revenue', 'cost', 'profit_margin', 'count', 'ratio_rate', 'average', 'growth', 'other')
DESCRIBE_BATCH = 40
DESCRIBE_MAX_CALLS = 25
ROW_BATCH = 200
APP_MAX_WORKBOOKS = 50          # the app harvests up to this many per run; larger projects use the laptop tool

S, N, B, T, V = 'STRING', 'NUMBER', 'BOOLEAN', 'TIMESTAMP_TZ', 'VARIANT'

ROUTES = {
    'bind_snowflake': 'Bound read-only from Snowflake (one table)',
    'bind_model': 'Bound read-only from Snowflake (relationship model with a shared Semantic View, or a verified '
                  'physical-join / custom-SQL plan)',
    'load': 'Loaded into Snowflake after approval (bundled Excel / CSV / union, release 21)',
    'laptop': 'Loaded from a laptop (Tableau extract, release 21 loader)',
    'release24': 'Named: joins, custom SQL, published or non-Snowflake sources (release 24)',
    'no_data': 'Named: the data is not inside the workbook and no Snowflake source is declared',
    'unknown': 'Not classified (see reason)',
}

TABLES = {
    'CAT_HARVESTS': ('One row when a harvest starts and one when it finishes', [
        ('HARVEST_ID', S), ('RECORDED_AT', T), ('STATUS', S), ('STARTED_BY', S), ('VIA', S), ('SCOPE', S),
        ('SITE', S), ('CATALOG_VERSION', S), ('WORKBOOKS_FOUND', N), ('HARVESTED', N), ('UNCHANGED', N),
        ('FAILED', N), ('METRICS_DESCRIBED', N), ('CORTEX_CALLS', N), ('NOTE', S)]),
    'CAT_WORKBOOKS': ('One row per workbook per harvest; written after its detail rows, so a workbook row means '
                      'its catalogue is complete', [
        ('HARVEST_ID', S), ('RECORDED_AT', T), ('WORKBOOK_KEY', S), ('WORKBOOK_NAME', S), ('SCOPE', S),
        ('PROJECT', S), ('ORIGIN', S), ('SITE', S), ('SOURCE_REF', V), ('STATUS', S), ('CATALOG_VERSION', S),
        ('WORKBOOK_SHA256', S), ('TABLEAU_VERSION', S), ('DATASOURCES', N), ('WORKSHEETS', N), ('DASHBOARDS', N),
        ('CALCULATIONS', N), ('METRICS', N), ('PARAMETERS', N), ('TOTAL_VIEWS', N), ('ERROR_CLASS', S),
        ('NOTE', S)]),
    'CAT_DATASOURCES': ('Datasources with their shape, source key and the route a migration run takes', [
        ('HARVEST_ID', S), ('WORKBOOK_KEY', S), ('DATASOURCE_ID', S), ('DATASOURCE', S), ('CONNECTIONS', V),
        ('SHAPE', S), ('TABLES', V), ('SOURCE_KEY', S), ('HAS_EXTRACT', B), ('BLENDED_WITH', V), ('ROUTE', S),
        ('ROUTE_REASON', S), ('FIELDS', N), ('CALCULATIONS', N)]),
    'CAT_FIELDS': ('Source fields (not calculations) per datasource', [
        ('HARVEST_ID', S), ('WORKBOOK_KEY', S), ('DATASOURCE_ID', S), ('FIELD', S), ('CAPTION', S),
        ('PHYSICAL', S), ('DATATYPE', S), ('ROLE', S)]),
    'CAT_CALCULATIONS': ('Calculations: original formula, normalised formula and fingerprint, kind, '
                         'dependencies and the translated SQL', [
        ('HARVEST_ID', S), ('WORKBOOK_KEY', S), ('DATASOURCE_ID', S), ('DATASOURCE', S), ('FIELD', S),
        ('CAPTION', S), ('KIND', S), ('FORMULA', S), ('NORMALISED', S), ('FORMULA_FP', S), ('DEPENDS_ON', V),
        ('PARAMETERS', V), ('DATATYPE', S), ('ROLE', S), ('TRANSLATED_SQL', S), ('TRANSLATION', S)]),
    'CAT_METRICS': ('Metrics per workbook: aggregating calculations and the measures sheets aggregate', [
        ('HARVEST_ID', S), ('WORKBOOK_KEY', S), ('METRIC_FP', S), ('NAME', S), ('ORIGIN_KIND', S), ('KIND', S),
        ('EXPRESSION', S), ('NORMALISED', S), ('DATASOURCE_ID', S), ('DATASOURCE', S), ('SOURCE_KEY', S),
        ('FIELD', S)]),
    'CAT_USAGE': ('Which worksheet uses which field or metric, on which shelf, with which aggregation; filters '
                  'are rows on the filter shelf', [
        ('HARVEST_ID', S), ('WORKBOOK_KEY', S), ('WORKSHEET', S), ('DASHBOARDS', V), ('DATASOURCE_ID', S),
        ('FIELD', S), ('CAPTION', S), ('SHELF', S), ('AGGREGATION', S), ('IS_MEASURE', B), ('METRIC_FP', S),
        ('FILTER_CLASS', S)]),
    'CAT_BINDINGS': ('The Snowflake relation a migration run would read for a datasource (one table, or the '
                     'code-built relationship query), with its columns; release 22 tests metrics on it', [
        ('HARVEST_ID', S), ('WORKBOOK_KEY', S), ('DATASOURCE_ID', S), ('KIND', S), ('RELATION', S), ('COLUMNS', V)]),
    'CAT_VIEWS': ('Published Tableau views with their total view count (Tableau REST usage statistics)', [
        ('HARVEST_ID', S), ('RECORDED_AT', T), ('WORKBOOK_KEY', S), ('VIEW_ID', S), ('VIEW_NAME', S),
        ('CONTENT_URL', S), ('TOTAL_VIEWS', N)]),
    'CAT_METRIC_DESCRIPTIONS': ("Cortex's business name, description and intent per metric fingerprint, with "
                                "code's check; only rows that passed are shown", [
        ('METRIC_FP', S), ('RECORDED_AT', T), ('HARVEST_ID', S), ('BUSINESS_NAME', S), ('DESCRIPTION', S),
        ('INTENT', S), ('MODEL', S), ('PROMPT_SHA256', S), ('PASSED_CODE_CHECK', B), ('CODE_CHECK', S)]),
}

_CUR = 'JOIN {S}.V_CAT_WORKBOOKS w ON w.WORKBOOK_KEY = x.WORKBOOK_KEY AND w.HARVEST_ID = x.HARVEST_ID'
VIEWS = {
    'V_CAT_WORKBOOKS': ('Each workbook\'s current catalogue (its latest complete harvest), last time seen and '
                        'latest view count', """
WITH h AS (SELECT * FROM {S}.CAT_WORKBOOKS WHERE STATUS = 'harvested'
           QUALIFY ROW_NUMBER() OVER (PARTITION BY WORKBOOK_KEY ORDER BY RECORDED_AT DESC) = 1),
     seen AS (SELECT WORKBOOK_KEY, MAX(RECORDED_AT) AS LAST_SEEN, MAX_BY(TOTAL_VIEWS, RECORDED_AT) AS TOTAL_VIEWS,
                     MAX_BY(SCOPE, RECORDED_AT) AS SCOPE, MAX_BY(PROJECT, RECORDED_AT) AS PROJECT,
                     MAX_BY(WORKBOOK_NAME, RECORDED_AT) AS WORKBOOK_NAME
              FROM {S}.CAT_WORKBOOKS WHERE STATUS IN ('harvested', 'unchanged') GROUP BY WORKBOOK_KEY)
SELECT h.WORKBOOK_KEY, seen.WORKBOOK_NAME, seen.SCOPE, seen.PROJECT, h.ORIGIN, h.SITE, h.SOURCE_REF, h.HARVEST_ID,
       h.RECORDED_AT AS HARVESTED_AT, seen.LAST_SEEN, h.WORKBOOK_SHA256, h.TABLEAU_VERSION, h.DATASOURCES,
       h.WORKSHEETS, h.DASHBOARDS, h.CALCULATIONS, h.METRICS, h.PARAMETERS, seen.TOTAL_VIEWS, h.NOTE
FROM h JOIN seen ON seen.WORKBOOK_KEY = h.WORKBOOK_KEY"""),
    'V_CAT_WORKBOOK_STATUS': ('Latest harvest result per workbook, including failures', """
SELECT * FROM {S}.CAT_WORKBOOKS
QUALIFY ROW_NUMBER() OVER (PARTITION BY WORKBOOK_KEY ORDER BY RECORDED_AT DESC) = 1"""),
    'V_CAT_DATASOURCES': ('Current datasources of every catalogued workbook with their route', """
SELECT w.SCOPE, w.PROJECT, w.WORKBOOK_NAME, x.* FROM {S}.CAT_DATASOURCES x """ + _CUR),
    'V_CAT_CALCULATIONS': ('Current calculations of every catalogued workbook', """
SELECT w.SCOPE, w.PROJECT, w.WORKBOOK_NAME, x.* FROM {S}.CAT_CALCULATIONS x """ + _CUR),
    'V_CAT_METRICS': ('Current metrics of every catalogued workbook', """
SELECT w.SCOPE, w.PROJECT, w.WORKBOOK_NAME, x.* FROM {S}.CAT_METRICS x """ + _CUR),
    'V_CAT_USAGE': ('Current sheet usage of every catalogued workbook', """
SELECT w.SCOPE, w.PROJECT, w.WORKBOOK_NAME, x.* FROM {S}.CAT_USAGE x """ + _CUR),
    'V_CAT_FIELDS': ('Current source fields of every catalogued workbook', """
SELECT w.SCOPE, w.PROJECT, w.WORKBOOK_NAME, x.* FROM {S}.CAT_FIELDS x """ + _CUR),
    'V_CAT_BINDINGS': ('Current Snowflake relations of every catalogued datasource', """
SELECT w.SCOPE, w.PROJECT, w.WORKBOOK_NAME, x.* FROM {S}.CAT_BINDINGS x """ + _CUR),
    'V_METRIC_CATALOG': ('One row per distinct metric (formula fingerprint): its names, workbooks, datasources, '
                         'sheets and the latest checked Cortex description', """
WITH u AS (SELECT METRIC_FP, COUNT(DISTINCT WORKBOOK_KEY || '|' || WORKSHEET) AS SHEETS
           FROM {S}.V_CAT_USAGE WHERE METRIC_FP IS NOT NULL GROUP BY METRIC_FP),
     d AS (SELECT * FROM {S}.CAT_METRIC_DESCRIPTIONS WHERE PASSED_CODE_CHECK
           QUALIFY ROW_NUMBER() OVER (PARTITION BY METRIC_FP ORDER BY RECORDED_AT DESC) = 1)
SELECT m.METRIC_FP, ANY_VALUE(m.KIND) AS KIND, ARRAY_AGG(DISTINCT m.NAME) AS NAMES,
       COUNT(DISTINCT m.NAME) AS NAME_VARIANTS, COUNT(DISTINCT m.WORKBOOK_KEY) AS WORKBOOKS,
       ARRAY_AGG(DISTINCT m.WORKBOOK_NAME) AS WORKBOOK_NAMES, ARRAY_AGG(DISTINCT m.SCOPE) AS SCOPES,
       ARRAY_AGG(DISTINCT m.DATASOURCE) AS DATASOURCES, ANY_VALUE(m.EXPRESSION) AS EXAMPLE_FORMULA,
       ANY_VALUE(m.NORMALISED) AS NORMALISED, COALESCE(ANY_VALUE(u.SHEETS), 0) AS SHEETS,
       ANY_VALUE(d.BUSINESS_NAME) AS BUSINESS_NAME, ANY_VALUE(d.DESCRIPTION) AS DESCRIPTION,
       ANY_VALUE(d.INTENT) AS INTENT, ANY_VALUE(d.MODEL) AS DESCRIBED_BY
FROM {S}.V_CAT_METRICS m
LEFT JOIN u ON u.METRIC_FP = m.METRIC_FP
LEFT JOIN d ON d.METRIC_FP = m.METRIC_FP
GROUP BY m.METRIC_FP"""),
    'V_METRIC_NAME_VARIANTS': ('Calculations with the same name but a different formula (input to release 22; '
                               'not yet proven to give different numbers)', """
SELECT UPPER(TRIM(NAME)) AS NAME_KEY, ANY_VALUE(NAME) AS NAME, COUNT(DISTINCT METRIC_FP) AS FORMULAS,
       ARRAY_AGG(DISTINCT EXPRESSION) AS EXPRESSIONS, ARRAY_AGG(DISTINCT WORKBOOK_NAME) AS WORKBOOK_NAMES,
       ARRAY_AGG(DISTINCT SCOPE) AS SCOPES
FROM {S}.V_CAT_METRICS WHERE ORIGIN_KIND = 'calculation'
GROUP BY UPPER(TRIM(NAME)) HAVING COUNT(DISTINCT METRIC_FP) > 1"""),
    'V_DATASOURCE_ROUTES': ('Datasources per scope and route', """
SELECT SCOPE, ROUTE, COUNT(*) AS DATASOURCES, COUNT(DISTINCT WORKBOOK_KEY) AS WORKBOOKS,
       COUNT(DISTINCT SOURCE_KEY) AS DISTINCT_SOURCES
FROM {S}.V_CAT_DATASOURCES GROUP BY SCOPE, ROUTE"""),
    'V_ESTATE_SUMMARY': ('Per scope: workbooks, views, datasources, calculations, distinct and shared metrics', """
WITH w AS (SELECT SCOPE, COUNT(*) AS WORKBOOKS, SUM(DATASOURCES) AS DATASOURCES, SUM(WORKSHEETS) AS WORKSHEETS,
                  SUM(DASHBOARDS) AS DASHBOARDS, SUM(CALCULATIONS) AS CALCULATIONS, SUM(TOTAL_VIEWS) AS TOTAL_VIEWS,
                  COUNT_IF(COALESCE(TOTAL_VIEWS, 0) = 0) AS WORKBOOKS_WITHOUT_VIEWS
           FROM {S}.V_CAT_WORKBOOKS GROUP BY SCOPE),
     m AS (SELECT SCOPE, METRIC_FP, COUNT(DISTINCT WORKBOOK_KEY) AS N FROM {S}.V_CAT_METRICS GROUP BY SCOPE, METRIC_FP),
     ms AS (SELECT SCOPE, COUNT(*) AS DISTINCT_METRICS, COUNT_IF(N > 1) AS SHARED_METRICS FROM m GROUP BY SCOPE),
     r AS (SELECT SCOPE, COUNT_IF(ROUTE IN ('bind_snowflake', 'bind_model', 'load', 'laptop')) AS DATASOURCES_WITH_ROUTE
           FROM {S}.V_CAT_DATASOURCES GROUP BY SCOPE)
SELECT w.*, ms.DISTINCT_METRICS, ms.SHARED_METRICS, r.DATASOURCES_WITH_ROUTE
FROM w LEFT JOIN ms ON ms.SCOPE = w.SCOPE LEFT JOIN r ON r.SCOPE = w.SCOPE"""),
    'V_CAT_VIEW_USAGE': ('Latest view count per published Tableau view', """
SELECT * FROM {S}.CAT_VIEWS
QUALIFY ROW_NUMBER() OVER (PARTITION BY WORKBOOK_KEY, VIEW_ID ORDER BY RECORDED_AT DESC) = 1"""),
}


def ddl(schema):
    """Statements for sql/en01_evidence_store.sql (called by v3_store.ddl)."""
    q = lambda s: s.replace("'", "''")
    out = ['-- Release 20b: estate catalogue (SL01). Harvests only INSERT; the views show the latest complete '
           'harvest of each workbook.']
    for name, (comment, cols) in TABLES.items():
        body = ',\n'.join(f'  {c} {t}' for c, t in cols)
        out.append(f"CREATE TABLE IF NOT EXISTS {schema}.{name} (\n{body}\n) COMMENT = '{q(comment)}';")
    for name, (comment, sql) in VIEWS.items():
        out.append(f"CREATE OR REPLACE VIEW {schema}.{name} COMMENT = '{q(comment)}' AS" + sql.format(S=schema) + ';')
    return out


# --------------------------------------------------------------------------- #
# Formula normalisation and fingerprints
# --------------------------------------------------------------------------- #
_TOKEN = re.compile(r"""("(?:[^"]|"")*"|'(?:[^']|'')*')"""            # string literal
                    r"""|(\[(?:[^\]]|\]\])*\](?:\.\[(?:[^\]]|\]\])*\])?)"""  # [field] or [ds].[field]
                    r"""|(//[^\n]*|/\*.*?\*/)""", re.S)                # comment
_PART = re.compile(r'\[((?:[^\]]|\]\])*)\]')
AGG_FUNCS = r'\b(SUM|AVG|COUNT|COUNTD|MIN|MAX|MEDIAN|ATTR|STDEV|STDEVP|VAR|VARP|PERCENTILE|COLLECT|CORR|COVAR|COVARP)\s*\('
TABLE_FUNCS = (r'\b(WINDOW_\w+|RUNNING_\w+|INDEX|RANK|RANK_DENSE|RANK_MODIFIED|RANK_PERCENTILE|RANK_UNIQUE|LOOKUP|'
               r'FIRST|LAST|SIZE|TOTAL|PREVIOUS_VALUE|SCRIPT_\w+)\s*\(')
LOD = r'\{\s*(FIXED|INCLUDE|EXCLUDE)\b'
DERIVATIONS = {'sum': 'SUM', 'avg': 'AVG', 'count': 'COUNT', 'countd': 'COUNTD', 'min': 'MIN', 'max': 'MAX',
               'median': 'MEDIAN', 'stdev': 'STDEV', 'stdevp': 'STDEVP', 'var': 'VAR', 'varp': 'VARP',
               'attr': 'ATTR', 'attribute': 'ATTR'}


def fp(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def phys(name):
    from calc_translator import to_phys
    return to_phys(name)


def unbracket(ref):
    return ref[1:-1].replace(']]', ']') if ref.startswith('[') and ref.endswith(']') else ref


def _code(text):
    """Code (not strings or field references): one case, no meaningless spacing."""
    t = re.sub(r'\s+', ' ', text.upper())
    return re.sub(r'\s*([^\w\s])\s*', r'\1', t)


def _literal(s):
    body = s[1:-1].replace(s[0] * 2, s[0])
    return "'" + body.replace("'", "''") + "'"


def normalise(formula, resolve):
    """Formula -> canonical text. `resolve(parts)` turns a field reference (list of names) into text."""
    out = []
    pos = 0
    for m in _TOKEN.finditer(formula or ''):
        out.append(_code(formula[pos:m.start()]))
        lit, ref, _comment = m.groups()
        if lit:
            out.append(_literal(lit))
        elif ref:
            out.append(resolve([p.replace(']]', ']') for p in _PART.findall(ref)]))
        else:
            out.append(' ')
        pos = m.end()
    out.append(_code(formula[pos:] if formula else ''))
    parts = re.split(r"('(?:[^']|'')*')", ''.join(out))      # tidy spacing outside string literals
    return ''.join(p if i % 2 else re.sub(r'\s*([^\w\s])\s*', r'\1', re.sub(r'\s+', ' ', p))
                   for i, p in enumerate(parts)).strip()


def _closes_at_end(text, start):
    """True when the bracket opened at `start` closes at the last character (strings skipped)."""
    depth, quote = 0, None
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch == "'":
            quote = ch
        elif ch in '({':
            depth += 1
        elif ch in ')}':
            depth -= 1
            if depth == 0:
                return i == len(text) - 1
    return False


def _wrap(text):
    """An expanded calculation inside another formula: brackets only when it is not already one term, so
    SUM([Sales copy]) and SUM([Sales]) give the same text."""
    if re.fullmatch(r"[\w.]+|'(?:[^']|'')*'", text):
        return text
    m = re.match(r'[A-Z_][A-Z_0-9]*\(', text)
    if m and _closes_at_end(text, m.end() - 1):
        return text
    if text[:1] in '({' and _closes_at_end(text, 0):
        return text
    return '(' + text + ')'


def kind_of(expanded, own_formula, has_table_calc=False):
    text = (expanded or '') + ' ' + (own_formula or '')
    if has_table_calc or re.search(TABLE_FUNCS, text, re.I):
        return 'table_calc'
    if re.search(LOD, text, re.I):
        return 'lod'
    if re.search(AGG_FUNCS, text, re.I):
        return 'aggregate'
    return 'row'


# --------------------------------------------------------------------------- #
# Workbook parsing
# --------------------------------------------------------------------------- #
def twb_bytes(raw):
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            twbs = [n for n in z.namelist() if n.lower().endswith('.twb')]
            if len(twbs) != 1:
                raise ValueError('Workbook package must contain exactly one .twb file')
            return z.read(twbs[0])
    return raw


class _Datasource:
    def __init__(self, ds, params):
        self.node = ds
        self.id = ds.get('name')
        self.caption = (ds.get('caption') or self.id).strip()
        self.params = params
        self.columns = {}
        self.remote = {}
        for mr in ds.findall('.//metadata-records/metadata-record'):
            if mr.get('class') == 'column' and mr.findtext('local-name'):
                self.remote.setdefault(unbracket(mr.findtext('local-name')), mr.findtext('remote-name') or '')
        self.add_columns(ds.findall('./column'))
        self._norm = {}

    def add_columns(self, cols):
        for c in cols:
            name = unbracket(c.get('name') or '')
            if not name or name in self.columns:
                continue
            calc = c.find('calculation')
            self.columns[name] = {
                'caption': (c.get('caption') or name).strip() or name, 'datatype': c.get('datatype'), 'role': c.get('role'),
                'calc_class': calc.get('class') if calc is not None else None,
                'formula': calc.get('formula') if calc is not None else None,
                'table_calc': calc is not None and calc.find('table-calc') is not None}

    def is_calc(self, name):
        c = self.columns.get(name)
        return bool(c and c['calc_class'] == 'tableau' and c['formula'] is not None)

    def caption_of(self, name):
        return (self.columns.get(name) or {}).get('caption') or name

    def resolver(self, stack, deps, params):
        def resolve(parts):
            if len(parts) == 2 and parts[0] == 'Parameters':
                cap = self.params.get(parts[1], parts[1])
                params.add(cap)
                return 'PARAM(' + phys(cap) + ')'
            name = parts[-1]
            if len(parts) == 1 or parts[0] == self.id:
                if self.is_calc(name):
                    deps.add(self.caption_of(name))
                    return _wrap(self.norm(name, stack))
                deps.add(self.caption_of(name))
                return 'COL(' + phys(self.remote.get(name) or name) + ')'
            return 'COL(' + phys(name) + ')'
        return resolve

    def norm(self, name, stack=()):
        """Expanded, canonical formula of one calculation (cycles are named, not expanded)."""
        if name in self._norm:
            return self._norm[name][0]
        if name in stack or len(stack) > 25:
            return 'CALC(' + phys(name) + ')'
        deps, params = set(), set()
        text = normalise(self.columns[name]['formula'], self.resolver(stack + (name,), deps, params))
        self._norm[name] = (text, deps, params)
        return text

    def norm_info(self, name):
        self.norm(name)
        return self._norm[name]

    def field_norm(self, name):
        if self.is_calc(name):
            return self.norm(name)
        return 'COL(' + phys(self.remote.get(name) or name) + ')'


def _params(root):
    out = {}
    for ds in root.findall('./datasources/datasource'):
        if ds.get('name') == 'Parameters':
            for c in ds.findall('./column'):
                out[unbracket(c.get('name') or '')] = c.get('caption') or unbracket(c.get('name') or '')
    return out


def _instance(name):
    """'sum:Sales:qk' -> ('Sales', 'Sum'); 'pcto:sum:Sales:qk' -> ('Sales', 'Sum')."""
    parts = name.split(':')
    if len(parts) >= 3:
        derivs = [p for p in parts[:-2]]
        return parts[-2], (derivs[-1] if derivs else 'None')
    return name, 'None'


_REF2 = re.compile(r'\[((?:[^\]]|\]\])*)\]\.\[((?:[^\]]|\]\])*)\]')
SHELF_TAGS = ('color', 'size', 'text', 'label', 'tooltip', 'lod', 'shape', 'path', 'wedge-size', 'geometry', 'detail')


def _shelf_refs(view, table):
    """(shelf, datasource id, instance name, filter class) for every field a worksheet places."""
    out = []
    for shelf in ('rows', 'cols'):
        node = table.find(shelf)
        if node is not None and node.text:
            out += [(shelf, a, b, None) for a, b in _REF2.findall(node.text)]
    for enc in table.iter('encodings'):
        for el in enc:
            m = _REF2.fullmatch(el.get('column') or '')
            if m:
                out.append((el.tag if el.tag in SHELF_TAGS else 'mark:' + el.tag, m.group(1), m.group(2), None))
    for flt in view.findall('filter'):
        m = _REF2.fullmatch(flt.get('column') or '')
        if not m:
            continue
        if m.group(2) == ':Measure Names':
            for gf in flt.iter('groupfilter'):
                mm = _REF2.fullmatch((gf.get('member') or '').strip('"'))
                if gf.get('function') == 'member' and mm:
                    out.append(('measure_values', mm.group(1), mm.group(2), None))
            continue
        out.append(('filter', m.group(1), m.group(2), flt.get('class')))
    return out


def _dashboards(root, names):
    out = {}
    for d in root.findall('./dashboards/dashboard'):
        for z in d.iter('zone'):
            n = z.get('name')
            if n in names:
                out.setdefault(n, [])
                if d.get('name') not in out[n]:
                    out[n].append(d.get('name'))
    return out


def _ir_for(raw, filename):
    import tableau_parser
    ext = '.twbx' if zipfile.is_zipfile(io.BytesIO(raw)) else '.twb'
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'workbook' + ext)
        with open(path, 'wb') as fh:
            fh.write(raw)
        return tableau_parser.build_ir(path, scoped=True)


def _relations(ds):
    out = []
    for r in ds.iter('relation'):
        if r.get('type') == 'table' and r.get('table'):
            out.append(r.get('table'))
    return sorted(set(out))


def _shape(ds, cls, blends_secondary):
    types = {r.get('type') for r in ds.iter('relation')}
    if 'sqlproxy' in cls:
        return 'published'
    if 'text' in types:
        return 'custom_sql'
    if 'join' in types:
        return 'join'
    if 'union' in types:
        return 'union'
    if ds.find('.//relationships/relationship') is not None:
        return 'relationships'
    if not _relations(ds) and ds.find('.//extract') is not None:
        return 'extract_only'
    return 'single_table'


def binding(item):
    """The relation a run reads for a datasource bound read-only (code-built; nothing executed here)."""
    if item.get('candidate'):
        return {'KIND': 'table', 'RELATION': item['candidate'], 'COLUMNS': None}
    plan = item.get('model_candidate')
    if plan:
        try:
            import v3_model_binding
            return {'KIND': 'model', 'RELATION': v3_model_binding.compile_query(plan),
                    'COLUMNS': sorted(f['output'] for f in plan['fields'].values())}
        except Exception:
            return None
    return None


def route(raw, root, ir, did, cap, candidates):
    """The route a migration run takes for this datasource (same code as the run, no Snowflake call)."""
    import v3_data_load as DL
    item = candidates.get(did) or {}
    if (item.get('model_candidate') or {}).get('kind') in ('physical-join', 'custom-sql'):
        kind = item['model_candidate']['kind']
        return 'bind_model', (('Physical join' if kind == 'physical-join' else 'Author custom SQL') +
                             ' over Snowflake; the run checks the account, every column and type read-only, and '
                             'reads the joined rows as Tableau does')
    if item.get('model_candidate'):
        return 'bind_model', ('Relationship model over Snowflake; the run checks the account, keys and fact grain '
                              'read-only and shares one Semantic View')
    if item.get('candidate'):
        return 'bind_snowflake', ('Snowflake table ' + item['candidate'] + '; the run checks the account, access and '
                                  'columns read-only')
    try:
        loc = DL.locate(raw, root, ir, cap)
    except Exception as exc:
        return 'unknown', f'{type(exc).__name__}: {exc}'[:300]
    if loc['kind'] in ('file', 'union'):
        return 'load', 'Bundled ' + ('union of files' if loc['kind'] == 'union' else 'data file') + \
            '; loaded into Snowflake after a person approves (or an existing loaded copy is reused)'
    if loc['kind'] == 'extract':
        return 'laptop', 'Tableau extract inside the workbook; load it once with tools/load_workbook_data.py'
    ds = next((d for d in root.findall('./datasources/datasource') if d.get('name') == did), None)
    reason = loc.get('reason') or ''
    if ds is not None and ds.find('.//extract') is not None and not reason.startswith('Data comes from'):
        return 'laptop', ('Tableau extract (downloaded without data); download the workbook with its extract and '
                          'load it with tools/load_workbook_data.py')
    if reason.startswith('Data comes from'):
        return 'release24', (item.get('reason') or reason)[:500]
    return 'no_data', reason[:500]


def parse_workbook(raw, filename):
    """Everything the catalogue records for one workbook (no Snowflake, no Tableau calls)."""
    import v3_source_binding
    xml = twb_bytes(raw)
    import tableau_parser
    root = tableau_parser.parse_xml(xml)
    ir = _ir_for(raw, filename)
    params = _params(root)
    nodes = {d.get('name'): d for d in root.findall('./datasources/datasource') if d.get('name') != 'Parameters'}
    sources = {did: _Datasource(ds, params) for did, ds in nodes.items()}
    worksheets = root.findall('./worksheets/worksheet')
    for ws in worksheets:                                   # calculations declared only on a sheet
        for dep in ws.findall('./table/view/datasource-dependencies'):
            src = sources.get(dep.get('datasource'))
            if src is not None:
                src.add_columns(dep.findall('column'))
    try:
        candidates = v3_source_binding.source_candidates(root, ir)
    except Exception:
        candidates = {}
    scopes = ir.get('field_scopes') or {}
    secondary = {}
    for b in ir.get('blends') or []:
        secondary.setdefault(b.get('secondary_name'), []).append(b.get('primary'))
        secondary.setdefault(b.get('primary_name'), []).append(b.get('secondary'))
    out = {'datasources': [], 'fields': [], 'calculations': [], 'metrics': [], 'usage': [], 'bindings': []}
    keys = {}
    for did, src in sources.items():
        ds = src.node
        cls = sorted({c.get('class') for c in ds.iter('connection') if c.get('class') and c.get('class') != 'federated'})
        tables = _relations(ds)
        files = sorted({os.path.basename((c.get('filename') or '').replace('\\', '/'))
                        for c in ds.iter('connection') if c.get('filename')} - {''})
        conn = [(c.get('class'), (c.get('dbname') or '').upper(), (c.get('schema') or '').upper())
                for c in ds.iter('connection') if c.get('class') not in (None, 'federated', 'hyper', 'dataengine')]
        keys[did] = fp(json.dumps({'conn': sorted(set(conn)), 'tables': [t.upper() for t in tables],
                                   'files': [f.casefold() for f in files if not f.lower().endswith(('.hyper', '.tde'))]}))
        scope = scopes.get(did) or {}
        if scope:
            rt, why = route(raw, root, ir, did, scope.get('caption') or src.caption, candidates)
        else:
            rt, why = 'unknown', 'Datasource not in the parsed field scopes'
        fields = [f for f in (ir.get('fields') or {}).values() if f.get('datasource_id') == did
                  and not f.get('is_calculation') and f.get('datatype') != 'table'
                  and not str(f.get('internal') or '').startswith(':')]
        calc_names = [n for n in src.columns if src.is_calc(n)]
        out['datasources'].append({
            'DATASOURCE_ID': did, 'DATASOURCE': src.caption, 'CONNECTIONS': cls,
            'SHAPE': _shape(ds, cls, did in secondary), 'TABLES': tables or files, 'SOURCE_KEY': keys[did],
            'HAS_EXTRACT': ds.find('.//extract') is not None, 'BLENDED_WITH': sorted(set(secondary.get(did) or [])),
            'ROUTE': rt, 'ROUTE_REASON': why, 'FIELDS': len(fields), 'CALCULATIONS': len(calc_names)})
        bound = binding(candidates.get(did) or {})
        if bound:
            out['bindings'].append(dict(bound, DATASOURCE_ID=did))
        for f in fields:
            out['fields'].append({'DATASOURCE_ID': did, 'FIELD': f.get('internal'), 'CAPTION': f.get('caption'),
                                  'PHYSICAL': f.get('physical'), 'DATATYPE': f.get('datatype'), 'ROLE': f.get('role')})
        sql = scope.get('calcs') or {}
        drops = scope.get('calc_drops') or {}
        for name in calc_names:
            col = src.columns[name]
            text, deps, used = src.norm_info(name)
            kind = kind_of(text, col['formula'], col['table_calc'])
            translated = sql.get(name) or sql.get(col['caption'])
            drop = drops.get(name) or drops.get(col['caption'])
            out['calculations'].append({
                'DATASOURCE_ID': did, 'DATASOURCE': src.caption, 'FIELD': name, 'CAPTION': col['caption'],
                'KIND': kind, 'FORMULA': col['formula'], 'NORMALISED': text[:4000], 'FORMULA_FP': fp(text),
                'DEPENDS_ON': sorted(deps), 'PARAMETERS': sorted(used), 'DATATYPE': col['datatype'], 'ROLE': col['role'],
                'TRANSLATED_SQL': (translated or {}).get('sql') if isinstance(translated, dict) else None,
                'TRANSLATION': 'translated' if translated else ('not translated: ' + str(drop)[:300] if drop else 'not translated')})
            if kind != 'row':
                out['metrics'].append({'METRIC_FP': fp(text), 'NAME': col['caption'], 'ORIGIN_KIND': 'calculation',
                                       'KIND': kind, 'EXPRESSION': col['formula'], 'NORMALISED': text[:4000],
                                       'DATASOURCE_ID': did, 'DATASOURCE': src.caption, 'SOURCE_KEY': keys[did],
                                       'FIELD': name})
    names = {ws.get('name') for ws in worksheets}
    on = _dashboards(root, names)
    seen = set()
    for ws in worksheets:
        table = ws.find('table')
        view = table.find('view') if table is not None else None
        if view is None:
            continue
        inst = {}
        for dep in view.findall('datasource-dependencies'):
            for ci in dep.findall('column-instance'):
                inst[(dep.get('datasource'), unbracket(ci.get('name') or ''))] = (unbracket(ci.get('column') or ''),
                                                                                   ci.get('derivation') or 'None')
        rows = set()
        for shelf, did, iname, fclass in _shelf_refs(view, table):
            src = sources.get(did)
            if src is None or iname.startswith(':') or iname == 'Multiple Values':
                continue
            field, deriv = inst.get((did, iname)) or _instance(iname)
            if field.startswith(':'):
                continue
            agg = DERIVATIONS.get((deriv or '').lower())
            metric = None
            calc = src.is_calc(field)
            ckind = kind_of(src.norm(field), src.columns[field]['formula']) if calc else None
            if agg:
                text = agg + '(' + src.field_norm(field) + ')'
                metric = fp(text)
                expr = f'{agg}([{src.caption_of(field)}])'
                mk = (metric, did, src.caption_of(field))
                if mk not in seen:
                    seen.add(mk)
                    out['metrics'].append({'METRIC_FP': metric, 'NAME': src.caption_of(field),
                                           'ORIGIN_KIND': 'sheet aggregation', 'KIND': 'aggregate', 'EXPRESSION': expr,
                                           'NORMALISED': text[:4000], 'DATASOURCE_ID': did, 'DATASOURCE': src.caption,
                                           'SOURCE_KEY': keys.get(did), 'FIELD': field})
            elif calc and ckind != 'row':
                metric = fp(src.norm(field))
            key = (ws.get('name'), did, field, shelf, deriv, fclass)
            if key in rows:
                continue
            rows.add(key)
            out['usage'].append({'WORKSHEET': ws.get('name'), 'DASHBOARDS': on.get(ws.get('name'), []),
                                 'DATASOURCE_ID': did, 'FIELD': field, 'CAPTION': src.caption_of(field), 'SHELF': shelf,
                                 'AGGREGATION': deriv, 'IS_MEASURE': metric is not None, 'METRIC_FP': metric,
                                 'FILTER_CLASS': fclass})
    out['summary'] = {
        'WORKBOOK_SHA256': hashlib.sha256(xml).hexdigest(), 'TABLEAU_VERSION': root.get('source-build') or root.get('version'),
        'DATASOURCES': len(sources), 'WORKSHEETS': len(worksheets), 'DASHBOARDS': len(root.findall('./dashboards/dashboard')),
        'CALCULATIONS': len(out['calculations']), 'METRICS': len({m['METRIC_FP'] for m in out['metrics']}),
        'PARAMETERS': len(params)}
    return out


# --------------------------------------------------------------------------- #
# Storage (INSERT and SELECT only)
# --------------------------------------------------------------------------- #
def _store():
    import v3_store
    return v3_store


OPTIONAL = ('CAT_BINDINGS',)      # added in release 22: the catalogue works without it (bindings are skipped)


def ready(sess, tables=None):
    """True when every required catalogue table exists (or every table named in `tables`)."""
    if sess is None:
        return False
    st = _store()
    names = list(tables or [t for t in TABLES if t not in OPTIONAL])
    try:
        db, sch = st.SCHEMA.split('.')
        rows = sess.sql(f'SELECT COUNT(*) FROM {db}.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ? AND TABLE_NAME IN ('
                        + ','.join('?' * len(names)) + ')', params=[sch] + names).collect()
        return int(rows[0][0]) == len(names)
    except Exception:
        return False


def insert_sql(table, count):
    st = _store()
    cols = TABLES[table][1]
    return (f'INSERT INTO {st.SCHEMA}.{table} ({", ".join(c for c, _t in cols)}) SELECT '
            + ', '.join(st._cast(c, t) for c, t in cols)
            + ' FROM (SELECT PARSE_JSON(column1) AS v FROM VALUES ' + ', '.join(['(?)'] * count) + ')')


def write(sess, table, rows):
    st = _store()
    cols = [c for c, _t in TABLES[table][1]]
    for i in range(0, len(rows), ROW_BATCH):
        chunk = rows[i:i + ROW_BATCH]
        params = [json.dumps({c: st.jsonable(r.get(c)) for c in cols}, allow_nan=False) for r in chunk]
        sess.sql(insert_sql(table, len(chunk)), params=params).collect()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def last_sha(sess, key):
    st = _store()
    rows = sess.sql(f'SELECT WORKBOOK_SHA256 FROM {st.SCHEMA}.CAT_WORKBOOKS WHERE WORKBOOK_KEY = ? AND STATUS = ? '
                    'AND CATALOG_VERSION = ? ORDER BY RECORDED_AT DESC LIMIT 1',
                    params=[key, 'harvested', CATALOG_VERSION]).collect()
    return str(rows[0][0]) if rows else None


# --------------------------------------------------------------------------- #
# A harvest
# --------------------------------------------------------------------------- #
class Harvest:
    """One harvest: workbooks are recorded one at a time, so an interrupted harvest keeps what it finished
    and a re-run skips the workbooks that are unchanged."""

    def __init__(self, sess, via, scope, site=None, who=None, write_rows=True):
        self.sess, self.via, self.scope, self.site = sess, via, scope, site
        self.who = who
        self.id = 'H' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:6]
        self.write_rows = write_rows and sess is not None
        self.counts = {'found': 0, 'harvested': 0, 'unchanged': 0, 'failed': 0}
        self.results = []
        self.run = {'name': 'Estate catalogue ' + scope, 'ai_log': []}     # v3_ai log and budget for this harvest
        self.described = 0
        self.bindings = None                  # whether CAT_BINDINGS exists (checked once per harvest)

    def _harvest_row(self, status, note=None):
        return {'HARVEST_ID': self.id, 'RECORDED_AT': _now(), 'STATUS': status, 'STARTED_BY': self.who,
                'VIA': self.via, 'SCOPE': self.scope, 'SITE': self.site, 'CATALOG_VERSION': CATALOG_VERSION,
                'WORKBOOKS_FOUND': self.counts['found'], 'HARVESTED': self.counts['harvested'],
                'UNCHANGED': self.counts['unchanged'], 'FAILED': self.counts['failed'],
                'METRICS_DESCRIBED': self.described,
                'CORTEX_CALLS': sum(1 for e in self.run['ai_log'] if e.get('attempted')), 'NOTE': note}

    def start(self, found):
        self.counts['found'] = found
        if self.write_rows:
            write(self.sess, 'CAT_HARVESTS', [self._harvest_row('started')])

    def workbook(self, raw, filename, meta, views=None):
        """Record one workbook. meta: WORKBOOK_KEY, WORKBOOK_NAME, PROJECT, ORIGIN, SOURCE_REF."""
        base = {'HARVEST_ID': self.id, 'RECORDED_AT': _now(), 'SCOPE': self.scope, 'SITE': self.site,
                'CATALOG_VERSION': CATALOG_VERSION, **meta}
        total = sum(int(v.get('total_views') or 0) for v in views) if views else None
        view_rows = [{'HARVEST_ID': self.id, 'RECORDED_AT': _now(), 'WORKBOOK_KEY': meta['WORKBOOK_KEY'],
                      'VIEW_ID': v.get('id'), 'VIEW_NAME': v.get('name'), 'CONTENT_URL': v.get('content_url'),
                      'TOTAL_VIEWS': v.get('total_views')} for v in views or []]
        try:
            sha = hashlib.sha256(twb_bytes(raw)).hexdigest()
            if self.write_rows and last_sha(self.sess, meta['WORKBOOK_KEY']) == sha:
                if view_rows:
                    write(self.sess, 'CAT_VIEWS', view_rows)
                row = dict(base, STATUS='unchanged', WORKBOOK_SHA256=sha, TOTAL_VIEWS=total)
                write(self.sess, 'CAT_WORKBOOKS', [row])
                self.counts['unchanged'] += 1
                self.results.append(row)
                return row
            parsed = parse_workbook(raw, filename)
            key = {'HARVEST_ID': self.id, 'WORKBOOK_KEY': meta['WORKBOOK_KEY']}
            if self.write_rows:
                if self.bindings is None:
                    self.bindings = ready(self.sess, ['CAT_BINDINGS'])
                for table, part in (('CAT_DATASOURCES', 'datasources'), ('CAT_FIELDS', 'fields'),
                                    ('CAT_CALCULATIONS', 'calculations'), ('CAT_METRICS', 'metrics'),
                                    ('CAT_USAGE', 'usage'), ('CAT_BINDINGS', 'bindings')):
                    if table == 'CAT_BINDINGS' and not self.bindings:
                        continue
                    if parsed[part]:
                        write(self.sess, table, [dict(r, **key) for r in parsed[part]])
                if view_rows:
                    write(self.sess, 'CAT_VIEWS', view_rows)
            row = dict(base, STATUS='harvested', TOTAL_VIEWS=total, **parsed['summary'])
            if self.write_rows:
                write(self.sess, 'CAT_WORKBOOKS', [row])                 # last: marks the catalogue complete
            self.counts['harvested'] += 1
            self.results.append(dict(row, _parsed=parsed))
            return row
        except Exception as exc:
            row = dict(base, STATUS='failed', ERROR_CLASS=type(exc).__name__, NOTE=str(exc)[:300], TOTAL_VIEWS=total)
            self.counts['failed'] += 1
            self.results.append(row)
            if self.write_rows:
                try:
                    write(self.sess, 'CAT_WORKBOOKS', [row])
                except Exception:
                    pass
            return row

    def finish(self, describe_metrics=True, max_calls=DESCRIBE_MAX_CALLS):
        note = None
        if describe_metrics and self.write_rows:
            try:
                self.described = describe(self.sess, self, max_calls=max_calls)
            except Exception as exc:
                note = 'Metric descriptions not written: ' + type(exc).__name__
        if self.write_rows:
            _cortex_calls(self.sess, self)
            write(self.sess, 'CAT_HARVESTS', [self._harvest_row('finished', note)])
        return self.counts

    def describe_only(self, max_calls=DESCRIBE_MAX_CALLS):
        """Describe the scope's remaining metrics without reading workbooks (recorded as its own harvest row)."""
        self.described = describe(self.sess, self, max_calls=max_calls)
        _cortex_calls(self.sess, self)
        write(self.sess, 'CAT_HARVESTS', [self._harvest_row('described')])
        return self.described


def _cortex_calls(sess, harvest):
    """Record this harvest's Cortex calls in CORTEX_CALLS (RUN_ID = harvest id) when the store exists."""
    st = _store()
    if not harvest.run['ai_log'] or not st.ready(sess):
        return
    try:
        rows = [dict(r, RUN_ID=harvest.id, CALL_NO=i + 1) for i, r in enumerate(st.call_rows(harvest.run))]
        st._write(sess, 'CORTEX_CALLS', rows)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Cortex descriptions (proposals checked by code)
# --------------------------------------------------------------------------- #
def pending(sess, scope=None, limit=DESCRIBE_BATCH * DESCRIBE_MAX_CALLS):
    """Distinct metrics (of one scope, or all) that have no description that passed code's check."""
    st = _store()
    where = 'WHERE m.SCOPE = ?' if scope else 'WHERE TRUE'
    sql = (f'SELECT m.METRIC_FP, ARRAY_AGG(DISTINCT m.NAME) AS NAMES, ANY_VALUE(m.EXPRESSION) AS EXPRESSION, '
           f'ANY_VALUE(m.KIND) AS KIND, ARRAY_AGG(DISTINCT m.DATASOURCE) AS DATASOURCES '
           f'FROM {st.SCHEMA}.V_CAT_METRICS m {where} AND NOT EXISTS (SELECT 1 FROM {st.SCHEMA}.CAT_METRIC_DESCRIPTIONS d '
           f'WHERE d.METRIC_FP = m.METRIC_FP AND d.PASSED_CODE_CHECK) GROUP BY m.METRIC_FP ORDER BY m.METRIC_FP LIMIT {int(limit)}')
    rows = sess.sql(sql, params=[scope] if scope else []).collect()
    out = []
    for r in rows:
        d = r.as_dict() if hasattr(r, 'as_dict') else dict(r)
        for k in ('NAMES', 'DATASOURCES'):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    d[k] = [d[k]]
        out.append(d)
    return out


def _schema(ids):
    return {'type': 'object', 'properties': {'metrics': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'id': {'type': 'string', 'enum': ids}, 'business_name': {'type': 'string'},
        'description': {'type': 'string'}, 'intent': {'type': 'string', 'enum': list(INTENTS)}},
        'required': ['id', 'business_name', 'description', 'intent'], 'additionalProperties': False}}},
        'required': ['metrics'], 'additionalProperties': False}


TASK = ('Each item below is a metric found in Tableau workbooks: its names, its Tableau formula and the datasources '
        'that hold it. For every item write a short business name (at most 6 words), a one-sentence description of '
        'what it measures in business terms, and its intent class. Intent classes: revenue (sales, income), cost '
        '(spend, expense), profit_margin (profit or margin), count (number of records, orders, customers), '
        'ratio_rate (a share, rate or ratio), average (a mean value), growth (change over time), other. Describe '
        'only what the formula computes; do not invent thresholds, targets or numbers.')


def check(item, answer):
    """Code's ruling on one Cortex description. Returns (passed, reason)."""
    import v3_ai
    name = str(answer.get('business_name') or '').strip()
    desc = str(answer.get('description') or '').strip()
    if answer.get('intent') not in INTENTS:
        return False, 'intent outside the closed list'
    if not name or len(name) > 80:
        return False, 'business name empty or longer than 80 characters'
    if not desc or len(desc) > 400:
        return False, 'description empty or longer than 400 characters'
    bad = v3_ai.ungrounded_numbers(name + ' ' + desc, item)
    if bad:
        return False, 'numbers not in the formula: ' + ', '.join(bad[:5])
    return True, 'id, intent, length and numbers checked'


def describe(sess, harvest, max_calls=DESCRIBE_MAX_CALLS, items=None):
    """Describe pending metrics of the harvest's scope in batches; returns how many passed code's check."""
    import v3_ai
    items = pending(sess, harvest.scope) if items is None else items
    passed = 0
    for n in range(0, len(items), DESCRIBE_BATCH):
        if n // DESCRIBE_BATCH >= max_calls:
            break
        batch = items[n:n + DESCRIBE_BATCH]
        ids = {f'm{i + 1}': it for i, it in enumerate(batch)}
        evidence = [{'id': k, 'names': list(it.get('NAMES') or [])[:6], 'formula': str(it.get('EXPRESSION') or '')[:600],
                     'kind': it.get('KIND'), 'datasources': list(it.get('DATASOURCES') or [])[:4]} for k, it in ids.items()]
        prompt = v3_ai._wrap(TASK, evidence)
        try:
            answer = v3_ai.call(sess, harvest.run, 'Catalogue', 'Metric names and descriptions', prompt,
                                _schema(sorted(ids)), max_tokens=6000)
        except v3_ai.CortexUnavailable:
            break                                    # the catalogue stays complete without descriptions
        entry = harvest.run['ai_log'][-1] if harvest.run['ai_log'] else {}
        rows, done = [], set()
        for a in answer.get('metrics') or []:
            if not isinstance(a, dict) or a.get('id') not in ids or a.get('id') in done:
                continue
            done.add(a['id'])
            item = ids[a['id']]
            ok, why = check(next(e for e in evidence if e['id'] == a['id']), a)
            passed += ok
            rows.append({'METRIC_FP': item['METRIC_FP'], 'RECORDED_AT': _now(), 'HARVEST_ID': harvest.id,
                         'BUSINESS_NAME': str(a.get('business_name') or '')[:200],
                         'DESCRIPTION': str(a.get('description') or '')[:1000], 'INTENT': a.get('intent'),
                         'MODEL': entry.get('Model'), 'PROMPT_SHA256': entry.get('_prompt_sha256'),
                         'PASSED_CODE_CHECK': ok, 'CODE_CHECK': why})
        if rows:
            write(sess, 'CAT_METRIC_DESCRIPTIONS', rows)
    return passed


# --------------------------------------------------------------------------- #
# Tableau (read-only)
# --------------------------------------------------------------------------- #
def project_tree(projects, wanted_id, include_sub=True):
    """Ids of a project (and its sub-projects) and a function giving each project's path."""
    by_id = {p['id']: p for p in projects}
    ids, changed = {wanted_id}, include_sub
    while changed:
        changed = False
        for p in projects:
            if p.get('parent_project_id') in ids and p['id'] not in ids:
                ids.add(p['id'])
                changed = True

    def path(pid):
        parts, guard = [], 0
        while pid in by_id and guard < 50:
            parts.append(by_id[pid].get('name') or '')
            pid, guard = by_id[pid].get('parent_project_id'), guard + 1
        return ' / '.join(reversed(parts))
    return ids, path


def views_with_usage(client, workbook_id):
    import tableau_server as TS
    els = client._get_paginated(f'sites/{client.site_id}/workbooks/{workbook_id}/views?includeUsageStatistics=true', 'view')
    out = []
    for v in els:
        u = v.find('t:usage', TS.XMLNS)
        out.append({'id': v.get('id'), 'name': v.get('name'), 'content_url': v.get('contentUrl'),
                    'total_views': int(u.get('totalViewCount')) if u is not None and u.get('totalViewCount') else None})
    return out


def tableau_workbooks(client, project_id, include_sub=True):
    ids, path = project_tree(client.list_projects(), project_id, include_sub)
    books = [w for w in client.list_workbooks() if w.get('project_id') in ids]
    return sorted(books, key=lambda b: (path(b.get('project_id')), b.get('name') or '')), path


def harvest_tableau_workbook(harvest, client, w, path, server_url, site_content_url):
    """Download one workbook without its extract, read its view counts, record it."""
    import tableau_server as TS
    meta = {'WORKBOOK_KEY': f"tableau:{site_content_url}:{w['id']}", 'WORKBOOK_NAME': w.get('name'),
            'PROJECT': path(w.get('project_id')), 'ORIGIN': 'tableau-cloud',
            'SOURCE_REF': {'workbook_id': w['id'], 'server_url': server_url, 'site_content_url': site_content_url,
                           'content_url': w.get('content_url')}}
    try:
        raw = client.download_workbook_bytes(w['id'], include_extract=False)
    except Exception as exc:
        harvest.counts['failed'] += 1
        row = dict(meta, HARVEST_ID=harvest.id, RECORDED_AT=_now(), SCOPE=harvest.scope, SITE=harvest.site,
                   CATALOG_VERSION=CATALOG_VERSION, STATUS='failed', ERROR_CLASS=type(exc).__name__,
                   NOTE='Download failed')
        harvest.results.append(row)
        if harvest.write_rows:
            try:
                write(harvest.sess, 'CAT_WORKBOOKS', [row])
            except Exception:
                pass
        return row
    try:
        views = views_with_usage(client, w['id'])
    except Exception:
        views = None
    filename = TS.normalize_workbook_download(w.get('name') or w['id'], raw)
    return harvest.workbook(raw, filename, meta, views)


def folder_workbooks(folder):
    out = []
    for dirpath, _dirs, files in os.walk(folder):
        for f in sorted(files):
            if f.lower().endswith(('.twb', '.twbx')) and not f.startswith('~'):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


def harvest_file(harvest, folder, path):
    rel = os.path.relpath(path, folder).replace('\\', '/')
    project = os.path.dirname(rel) or os.path.basename(os.path.abspath(folder))
    meta = {'WORKBOOK_KEY': 'file:' + os.path.basename(os.path.abspath(folder)) + '/' + rel,
            'WORKBOOK_NAME': os.path.splitext(os.path.basename(path))[0], 'PROJECT': project, 'ORIGIN': 'folder',
            'SOURCE_REF': {'file': rel}}
    with open(path, 'rb') as fh:
        raw = fh.read()
    return harvest.workbook(raw, os.path.basename(path), meta)


# --------------------------------------------------------------------------- #
# Reading the catalogue (the app page)
# --------------------------------------------------------------------------- #
def frame(sess, view, scope=None, limit=20000):
    st = _store()
    where = ' WHERE SCOPE = ?' if scope else ''
    return sess.sql(f'SELECT * FROM {st.SCHEMA}.{view}{where} LIMIT {int(limit)}',
                    params=[scope] if scope else []).to_pandas()


def scopes(sess):
    st = _store()
    rows = sess.sql(f'SELECT DISTINCT SCOPE FROM {st.SCHEMA}.V_CAT_WORKBOOKS ORDER BY SCOPE').collect()
    return [str(r[0]) for r in rows if r[0] is not None]


def descriptions(sess):
    st = _store()
    return sess.sql(f'SELECT METRIC_FP, BUSINESS_NAME, DESCRIPTION, INTENT, MODEL FROM {st.SCHEMA}.CAT_METRIC_DESCRIPTIONS '
                    'WHERE PASSED_CODE_CHECK QUALIFY ROW_NUMBER() OVER (PARTITION BY METRIC_FP ORDER BY RECORDED_AT DESC) = 1'
                    ).to_pandas()


def metric_table(metrics, usage, desc=None):
    """One row per distinct metric for a set of current metric rows (pandas)."""
    import pandas as pd
    if metrics is None or metrics.empty:
        return pd.DataFrame(columns=['METRIC_FP', 'Name', 'Kind', 'Names', 'Workbooks', 'Sheets', 'Example formula'])
    sheets = (usage[usage['METRIC_FP'].notna()].assign(_s=lambda d: d['WORKBOOK_KEY'] + '|' + d['WORKSHEET'])
              .groupby('METRIC_FP')['_s'].nunique()) if usage is not None and not usage.empty else pd.Series(dtype=int)
    g = metrics.groupby('METRIC_FP')
    out = pd.DataFrame({
        'Name': g['NAME'].agg(lambda s: s.value_counts().index[0]),
        'Kind': g['KIND'].first(),
        'Names': g['NAME'].agg(lambda s: ', '.join(sorted(set(map(str, s))))),
        'Workbooks': g['WORKBOOK_KEY'].nunique(),
        'Workbook names': g['WORKBOOK_NAME'].agg(lambda s: ', '.join(sorted(set(map(str, s))))),
        'Datasources': g['DATASOURCE'].agg(lambda s: ', '.join(sorted(set(map(str, s))))),
        'Example formula': g['EXPRESSION'].first(),
    })
    out['Sheets'] = sheets.reindex(out.index).fillna(0).astype(int)
    out = out.reset_index()
    if desc is not None and not desc.empty:
        out = out.merge(desc.rename(columns={'BUSINESS_NAME': 'Business name', 'DESCRIPTION': 'Description',
                                             'INTENT': 'Intent', 'MODEL': 'Described by'}), on='METRIC_FP', how='left')
    return out.sort_values(['Workbooks', 'Sheets', 'Name'], ascending=[False, False, True]).reset_index(drop=True)


def as_dict(value):
    if isinstance(value, dict):
        return value
    try:
        out = json.loads(value) if value else {}
        return out if isinstance(out, dict) else {}
    except (TypeError, ValueError):
        return {}


def name_variants(metrics):
    """Calculations with the same name (case and spacing ignored) and more than one formula fingerprint."""
    import pandas as pd
    cols = ['Name', 'Formulas', 'Workbooks', 'Definitions']
    if metrics is None or metrics.empty:
        return pd.DataFrame(columns=cols)
    calc = metrics[metrics['ORIGIN_KIND'] == 'calculation'].assign(
        _k=lambda d: d['NAME'].astype(str).str.strip().str.casefold())
    rows = []
    for _k, g in calc.groupby('_k'):
        if g['METRIC_FP'].nunique() > 1:
            rows.append({'Name': g['NAME'].iloc[0], 'Formulas': g['METRIC_FP'].nunique(),
                         'Workbooks': ', '.join(sorted(set(map(str, g['WORKBOOK_NAME'])))),
                         'Definitions': ' | '.join(sorted(set(map(str, g['EXPRESSION']))))[:2000]})
    return pd.DataFrame(rows, columns=cols).sort_values('Formulas', ascending=False) if rows else pd.DataFrame(columns=cols)


def harvests(sess, scope):
    st = _store()
    return sess.sql(f'SELECT RECORDED_AT, STATUS, VIA, STARTED_BY, WORKBOOKS_FOUND, HARVESTED, UNCHANGED, FAILED, '
                    f'METRICS_DESCRIBED, CORTEX_CALLS, NOTE, HARVEST_ID FROM {st.SCHEMA}.CAT_HARVESTS WHERE SCOPE = ? '
                    'ORDER BY RECORDED_AT DESC LIMIT 40', params=[scope]).to_pandas()


def failures(sess, scope):
    st = _store()
    return sess.sql(f'SELECT WORKBOOK_NAME, PROJECT, RECORDED_AT, ERROR_CLASS, NOTE FROM {st.SCHEMA}.V_CAT_WORKBOOK_STATUS '
                    "WHERE SCOPE = ? AND STATUS = 'failed' ORDER BY WORKBOOK_NAME", params=[scope]).to_pandas()
