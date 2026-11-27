"""Generic-fix guard: product code never dispatches on a workbook, dashboard or sheet name.

Every fix is a rule on a Tableau construct (XML element, function, filter kind ...), proven on a second workbook and
on the whole corpus. This test fails the release gate when a string literal in product code equals the name of a
corpus workbook, dashboard or worksheet -- the shape of a workbook-specific special case. Comments and docstrings
are not code and are ignored (they may say where a problem was first found). Generic words that are also sheet names
('Overview', 'Sheet 1' ...) are ignored when they are used as ordinary UI / Tableau vocabulary."""
import ast
import glob
import re
import zipfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

# names that are also ordinary Tableau / UI vocabulary, legitimately written in code
GENERIC = {'overview', 'dashboard', 'sheet 1', 'sheet 2', 'sheet 3', 'sheet 4', 'sheet 5', 'dashboard 1', 'table',
           'details', 'detail', 'product', 'customers', 'shipping', 'performance', 'forecast', 'trend', 'map',
           'sales', 'profit', 'kpi', 'summary', 'info', 'show', 'hide', 'country', 'segment', 'channel', 'region',
           'category', 'sub category', 'customer', 'brand', 'property', 'revenue', 'quantity', 'aov', 'clv',
           'view1', 'view2', 'sales by region', 'total sales', 'order details', 'commission model', 'what if forecast',
           'sale map', 'index', 'last', 'pie', 'histogram', 'sankey', 'legend', 'title', 'text', 'filters',
           'date', 'year', 'line', 'start', 'definitions', 'month', 'week', 'quarter', 'bar', 'area', 'end'}
# reference data (Tableau's own captured numbers), not logic
DATA_MODULES = {'tableau_reference_bundles.py'}
# Known, reviewed exceptions -- (module, literal): reason. Anything NOT listed here fails the gate.
ALLOWED = {
    ('config.py', 'SUPERSTORE'): 'legacy local dev database of the inherited numeric harness; V3 clears '
                                 'config.DATASOURCES per workbook and refuses unbound datasources (DM2)',
    ('init_workbook.py', 'SUPERSTORE'): 'legacy CLI --db default (laptop tool), not used by the V3 app',
    ('semantic_layer.py', 'SUPERSTORE'): 'legacy CLI / DDL default database name, not used by the V3 app',
}


def corpus_names():
    names = set()
    for p in glob.glob(str(ROOT / 'Workbooks' / '**' / '*.tw*'), recursive=True):
        path = Path(p)
        names.add(path.stem.casefold())
        try:
            if path.suffix == '.twbx':
                z = zipfile.ZipFile(path)
                xml = z.read(next(n for n in z.namelist() if n.endswith('.twb'))).decode('utf-8', 'replace')
            else:
                xml = path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        names |= {n.casefold() for n in re.findall(r"<(?:worksheet|dashboard) name='([^']+)'", xml)}
    return {n for n in names if len(n) >= 4 and n not in GENERIC}


def product_files():
    """The deployed modules: every .py the Streamlit-in-Snowflake package lists (snowflake.yml)."""
    listed = sorted(set(re.findall(r'[\w/]+\.py', (ROOT / 'snowflake.yml').read_text(encoding='utf-8'))))
    for rel in listed:
        p = ROOT / rel
        if p.exists() and p.name not in DATA_MODULES:
            yield p


def literals(path):
    """String constants used as code (docstrings excluded)."""
    tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, 'value', None), ast.Constant):
                docs.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.lineno, node.value


class Guard(unittest.TestCase):
    def test_no_default_table_for_a_sheet_without_a_datasource(self):
        """V3: a sheet that names no datasource refuses -- never the built-in default table (another workbook's
        data); an unbound datasource already refuses (DM2)."""
        import engine
        old = dict(engine.IR)
        engine.IR.clear()
        engine.IR.update({'field_scopes': {}})
        try:
            with self.assertRaises(ValueError):
                engine.tbl({'name': 'S'})
        finally:
            engine.IR.clear()
            engine.IR.update(old)

    def test_no_workbook_or_sheet_name_in_product_code(self):
        names = corpus_names()
        self.assertGreater(len(names), 50)
        hits = []
        for p in product_files():
            for line, value in literals(p):
                if value.strip().casefold() in names and (p.name, value.strip()) not in ALLOWED:
                    hits.append(f'{p.name}:{line}: {value!r}')
        self.assertEqual(hits, [], 'workbook-specific literals in product code:\n' + '\n'.join(hits))


if __name__ == '__main__':
    unittest.main()
