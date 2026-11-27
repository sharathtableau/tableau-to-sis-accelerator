"""Bounded source-derived typed expressions for INCLUDE planning, not execution authorization."""
import math
import re
import xml.etree.ElementTree as ET

NUMERIC = {'integer', 'real'}
TOKEN = re.compile(r"\s*(\[[^\]]+\]|'(?:day)'|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|>=|<=|<>|!=|[+*(),.<>/=\-]|[A-Za-z_][A-Za-z_0-9]*)", re.I)


def parameter_declarations(sheet):
    source = sheet.get('worksheet_semantics', {}).get('source_xml', '')
    root = ET.fromstring(source or '<worksheet/>')
    result = {}
    for column in root.findall(".//datasource-dependencies[@datasource='Parameters']/column"):
        name = column.get('name', '')
        if not re.fullmatch(r'\[[^\]]+\]', name):
            raise ValueError('Invalid source parameter identity')
        name = name[1:-1]
        declaration = {'datatype': column.get('datatype'), 'domain': column.get('param-domain-type')}
        if name in result and result[name] != declaration:
            raise ValueError('Conflicting source parameter declarations')
        result[name] = declaration
    return result


class Resolver:
    def __init__(self, fields, owner, parameters=None):
        self.fields, self.owner, self.parameters = fields, owner, parameters or {}
        self.dependencies, self.used_parameters, self.stack = set(), set(), []
        self.budget = 0

    def field(self, name):
        matches = [f for f in self.fields.values() if f.get('datasource_id') == self.owner and f.get('internal') == name]
        if len(matches) != 1:
            raise ValueError('Dependency must resolve uniquely within its source')
        field = matches[0]; fid = field['id']; datatype = field.get('datatype')
        self.dependencies.add(fid)
        if len(self.dependencies) > 64 or fid in self.stack or len(self.stack) >= 16:
            raise ValueError('Cyclic calculation or dependency budget exceeded')
        if datatype not in NUMERIC | {'date', 'datetime', 'string', 'boolean'}:
            raise ValueError('Unsupported dependency datatype')
        if field.get('is_calculation') is False:
            return ['field', fid], datatype, 'row'
        if field.get('is_calculation') is not True:
            raise ValueError('Dependency calculation identity is missing')
        self.stack.append(fid)
        try:
            node, actual, level = self.parse(field.get('formula'))
        finally:
            self.stack.pop()
        if actual != datatype and not (actual in NUMERIC and datatype in NUMERIC):
            raise ValueError('Calculation result contradicts its declared datatype')
        return node, datatype, level

    def parse(self, formula):
        if not isinstance(formula, str) or not formula.strip() or len(formula) > 4000:
            raise ValueError('Calculation formula budget exceeded')
        tokens = []; pos = 0
        while pos < len(formula):
            if not formula[pos:].strip(): break
            match = TOKEN.match(formula, pos)
            if not match: raise ValueError('Unsupported calculation syntax')
            tokens.append(match.group(1)); pos = match.end()
        self.budget += len(tokens)
        if self.budget > 2048: raise ValueError('Expanded expression budget exceeded')
        parser = Parser(tokens, self)
        result = parser.expr()
        if parser.pos != len(tokens): raise ValueError('Unexpected calculation suffix')
        return result


class Parser:
    def __init__(self, tokens, resolver):
        self.tokens, self.resolver, self.pos, self.depth = tokens, resolver, 0, 0

    def peek(self):
        return self.tokens[self.pos].upper() if self.pos < len(self.tokens) else ''

    def take(self, expected=None):
        if self.pos >= len(self.tokens): raise ValueError('Incomplete calculation')
        value = self.tokens[self.pos]; self.pos += 1
        if expected is not None and value.upper() != expected:
            raise ValueError('Unexpected calculation token')
        return value

    @staticmethod
    def row(*args):
        if any(item[2] != 'row' for item in args):
            raise ValueError('Mixed or nested aggregation is unsupported')

    def expr(self, minimum=0):
        self.depth += 1
        if self.depth > 32: raise ValueError('Expression nesting budget exceeded')
        try:
            left = self.atom()
            operators = {'=': (1, 'eq'), '<>': (1, 'ne'), '!=': (1, 'ne'), '>': (1, 'gt'), '>=': (1, 'ge'), '<': (1, 'lt'), '<=': (1, 'le'), '+': (2, 'add'), '-': (2, 'subtract'), '*': (3, 'multiply')}
            while self.peek() in operators and operators[self.peek()][0] >= minimum:
                priority, op = operators[self.take()]
                right = self.expr(priority + 1); self.row(left, right)
                if left[1] not in NUMERIC or right[1] not in NUMERIC:
                    raise ValueError('Only numeric arithmetic and comparisons are supported')
                datatype = 'boolean' if priority == 1 else ('real' if 'real' in (left[1], right[1]) else 'integer')
                left = [op, left[0], right[0]], datatype, 'row'
            return left
        finally:
            self.depth -= 1

    def atom(self):
        token = self.take(); upper = token.upper()
        if token.startswith('['):
            name = token[1:-1]
            if self.peek() == '.':
                self.take('.')
                parameter = self.take()
                if name != 'Parameters' or not re.fullmatch(r'\[[^\]]+\]', parameter):
                    raise ValueError('Cross-source reference is unsupported')
                name = parameter[1:-1]
                if self.resolver.parameters.get(name) != {'datatype': 'integer', 'domain': 'any'}:
                    raise ValueError('Parameter requires an explicit unrestricted integer declaration')
                self.resolver.used_parameters.add(name)
                return ['parameter', name], 'integer', 'row'
            return self.resolver.field(name)
        if token == '(':
            result = self.expr(); self.take(')'); return result
        if token in ('+', '-'):
            result = self.expr(4); self.row(result)
            if result[1] not in NUMERIC: raise ValueError('Unary operator requires numeric input')
            return ['positive' if token == '+' else 'negative', result[0]], result[1], 'row'
        if re.fullmatch(r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', token):
            value = float(token) if any(c in token.lower() for c in '.e') else int(token)
            if not math.isfinite(value) or abs(value) > 1e15: raise ValueError('Numeric literal outside supported range')
            return ['number', value], 'real' if isinstance(value, float) else 'integer', 'row'
        if upper == 'NULL': return ['null'], 'null', 'row'
        if upper == 'IF':
            condition = self.expr(); self.take('THEN'); yes = self.expr()
            no = (['null'], 'null', 'row')
            if self.peek() == 'ELSE': self.take(); no = self.expr()
            self.take('END'); self.row(condition, yes, no)
            if condition[1] != 'boolean' or yes[1] not in NUMERIC or no[1] not in NUMERIC | {'null'}:
                raise ValueError('IF requires a row condition and numeric branches')
            return ['if', condition[0], yes[0], no[0]], ('real' if 'real' in (yes[1], no[1]) else 'integer'), 'row'
        if upper == 'DATEADD':
            self.take('('); self.take("'DAY'"); self.take(','); increment = self.expr(); self.take(','); date = self.expr(); self.take(')')
            self.row(increment, date)
            if increment[1] != 'integer' or date[1] not in {'date', 'datetime'}:
                raise ValueError('DATEADD requires integer days and a date or datetime')
            return ['dateadd', 'day', increment[0], date[0]], date[1], 'row'
        if upper in {'SUM', 'AVG', 'MIN', 'MAX', 'ABS', 'ZN'}:
            self.take('('); child = self.expr(); self.take(')'); self.row(child)
            if child[1] not in NUMERIC: raise ValueError('Numeric function requires numeric input')
            aggregate = upper in {'SUM', 'AVG', 'MIN', 'MAX'}
            return [upper.lower(), child[0]], ('real' if upper == 'AVG' else child[1]), 'aggregate' if aggregate else 'row'
        raise ValueError('Unsupported calculation function or token')


def expression_sql(node, physical, parameters, dialect='snowflake'):
    op = node[0]
    emit = lambda child: expression_sql(child, physical, parameters, dialect)
    if op == 'field': return physical(node[1])
    if op == 'number': return str(node[1])
    if op == 'null': return 'NULL'
    if op == 'parameter':
        value = parameters.get(node[1])
        if type(value) is not int or abs(value) > 1000000:
            raise ValueError('Runtime day parameter must be a bounded integer')
        return str(value)
    operators = {'add': '+', 'subtract': '-', 'multiply': '*', 'eq': '=', 'ne': '<>', 'gt': '>', 'ge': '>=', 'lt': '<', 'le': '<='}
    if op in operators: return '(' + emit(node[1]) + ' ' + operators[op] + ' ' + emit(node[2]) + ')'
    if op == 'if': return '(CASE WHEN ' + emit(node[1]) + ' THEN ' + emit(node[2]) + ' ELSE ' + emit(node[3]) + ' END)'
    if op == 'dateadd':
        if dialect == 'duckdb': return '(' + emit(node[3]) + ' + (' + emit(node[2]) + ") * INTERVAL '1 day')"
        if dialect != 'snowflake': raise ValueError('Unsupported SQL dialect')
        return 'DATEADD(day, ' + emit(node[2]) + ', ' + emit(node[3]) + ')'
    if op == 'datepart_expr': return 'EXTRACT(' + node[1].upper() + ' FROM ' + emit(node[2]) + ')'
    if op == 'datepart': return 'EXTRACT(' + node[1].upper() + ' FROM ' + physical(node[2]) + ')'
    if op in {'positive', 'negative'}: return '(' + ('+' if op == 'positive' else '-') + emit(node[1]) + ')'
    if op == 'abs': return 'ABS(' + emit(node[1]) + ')'
    if op == 'zn': return 'COALESCE(' + emit(node[1]) + ', 0)'
    raise ValueError('Unsupported expression node')
