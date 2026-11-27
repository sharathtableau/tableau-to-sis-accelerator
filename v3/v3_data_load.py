"""Release 21a: put a workbook's own data into Snowflake, with a person's approval, and find it again.

Datasources whose data lives in files inside the .twbx (Excel, CSV, unions of files, and Tableau
extracts) are refused by the read-only binders. This module:

1. works out, per datasource, where its data is: bundled Excel/CSV (incl. relationships between
   Excel sheets and unions), a bundled .hyper extract, or nothing usable;
2. builds the table in memory and checks, BEFORE anything is written, that it has every physical
   column the workbook uses with a compatible type (the same rule as v3_source_binding);
3. loads it into WBR_DB.PIPELINE_DEMO only after a person approves (hosted app) or confirms
   (laptop tool, tools/load_workbook_data.py, for .hyper extracts the hosted app cannot decode);
4. tags every loaded table with a fingerprint of its source bytes (table COMMENT), so a later run
   finds it again without reloading. A changed file is loaded as <NAME>_V2 (the old table stays,
   because a deployed app may still read it).

Never: overwrite a table, issue USE, create a database, use workbook or sheet names as logic.
"""
import datetime
import hashlib
import io
import json
import os
import re
import zipfile

LOAD_DB, LOAD_SCHEMA = 'WBR_DB', 'PIPELINE_DEMO'
MAX_ROWS = 2_000_000
TAG = 'v3load'
VERSIONS = range(2, 10)
FILE_EXT = ('.csv', '.xls', '.xlsx')
EXTRACT_EXT = ('.hyper', '.tde')
LAPTOP_COMMAND = ('Set-Location "C:\\Users\\SharathKumarKammari\\Downloads\\Tableau to SiS_Cowork - Cortex\\'
                  'tableau_accelerator_v3_cortex"; python tools\\load_workbook_data.py "<path to the .twbx>"')


# --------------------------------------------------------------------------- #
# Where each datasource's data is
# --------------------------------------------------------------------------- #
def _members(raw):
    """basename (casefold) -> zip member name, for data files and extracts. Duplicate basenames refused."""
    out, dup = {}, set()
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        return out, dup                       # a plain .twb carries no data files
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for n in z.namelist():
            base = os.path.basename(n)
            if os.path.splitext(base)[1].lower() in FILE_EXT + EXTRACT_EXT:
                key = base.casefold()
                if key in out:
                    dup.add(key)
                out[key] = n
    return out, dup


def _read_member(raw, name):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return z.read(name)


def _ds_by_caption(root, ir, cap):
    ids = [d for d, s in (ir.get('field_scopes') or {}).items() if s.get('caption') == cap]
    if len(ids) != 1:
        raise ValueError('Datasource identity is ambiguous')
    ds = next((d for d in root.findall('./datasources/datasource') if d.get('name') == ids[0]), None)
    if ds is None:
        raise ValueError('Datasource declaration unavailable')
    return ids[0], ds


def fields_for(ir, did):
    return [f for f in (ir.get('fields') or {}).values() if f.get('datasource_id') == did
            and not f.get('is_calculation') and f.get('datatype') != 'table'
            and not str(f.get('internal') or '').startswith(':')]          # Tableau-generated (':Measure Names')


def _authored_path(c, table=None):
    """The file a connection names, as authored: excel 'filename' (may carry folders); text 'directory' +
    'filename', or + the table's own file ('[Calender Brazil#csv]' -> 'Calender Brazil.csv')."""
    fn = (c.get('filename') or '').replace('\\', '/')
    if c.get('class') == 'textscan':
        if table and '#' in table.strip('[]'):
            t = table.strip('[]')
            fn = t.rsplit('#', 1)[0] + '.' + t.rsplit('#', 1)[1]
        d = (c.get('directory') or '').replace('\\', '/').rstrip('/')
        if d and '/' not in fn:
            fn = d + '/' + fn
    return fn


def _member(raw, authored):
    """The package member an authored path names: the one member ending in that path (folders included), else
    the one member with that file name; None when absent or ambiguous."""
    if not raw or not authored or not zipfile.is_zipfile(io.BytesIO(raw)):
        return None
    want = authored.replace('\\', '/').lstrip('./').casefold()
    base = os.path.basename(want)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = [n for n in z.namelist() if os.path.splitext(n)[1].lower() in FILE_EXT + EXTRACT_EXT]
    for cut in range(want.count('/') + 1):          # longest authored suffix first: 'a/b/f', 'b/f'
        tail = want.split('/', cut)[-1]
        hit = [n for n in names if n.casefold() == tail or n.casefold().endswith('/' + tail)]
        if len(hit) == 1:
            return hit[0]
        if len(hit) > 1:
            break
    hit = [n for n in names if os.path.basename(n).casefold() == base]
    return hit[0] if len(hit) == 1 else None


def _object_files(ds):
    """R47: logical table id -> the file name it reads ('Calender Brazil.csv', 'Sample - Superstore.xls'), from
    its table relation's connection; absent when that cannot be told (the caller keeps the datasource's file)."""
    conns = {}
    for nc in ds.iter('named-connection'):
        c = nc.find('connection')
        if c is not None and nc.get('name'):
            conns[nc.get('name')] = c
    out = {}
    for el in ds.iter('object'):
        oid = el.get('id')
        rel = next((r for r in el.iter('relation') if r.get('type') == 'table' and r.get('connection')), None)
        if not oid or rel is None:
            continue
        c = conns.get(rel.get('connection'))
        if c is None:
            continue
        if c.get('class') in ('textscan', 'excel-direct'):
            path = _authored_path(c, rel.get('table'))
            if path:
                out[oid] = path
    return out


def _sheet(source):
    """'[Orders$]' -> 'Orders'; '[Sales Commission#csv]' -> None (a CSV has no sheet)."""
    s = (source or '').strip('[]')
    return s[:-1] if s.endswith('$') else None


def locate(raw, root, ir, cap):
    """Where this datasource's data is. Returns a dict with kind = file | union | extract | none."""
    import tableau_parser
    from semantic_layer import _objects
    did, ds = _ds_by_caption(root, ir, cap)
    members, dup = _members(raw) if raw else ({}, set())
    out = {'caption': cap, 'datasource_id': did, 'kind': 'none', 'files': [], 'reason': None}
    conns = [c for c in ds.iter('connection') if c.get('class') not in (None, 'federated')]
    databases = sorted({c.get('class') for c in conns} - {'excel-direct', 'textscan', 'hyper', 'dataengine'})
    if databases:
        out['reason'] = (f"Data comes from {', '.join(databases)}: bound read-only from Snowflake when it is one "
                         'table or a relationship model; joins, custom SQL, published and non-Snowflake sources '
                         'are release 24')
        return out
    unions = tableau_parser.union_members(root)
    if cap in unions:
        names = [_member(raw, m) for m in unions[cap]]
        if not names or not all(names):
            out['reason'] = 'A file of this union is not inside the workbook'
            return out
        out.update(kind='union', files=[{'member': n} for n in names])
        return _fingerprint(raw, out)
    files = []
    for c in conns:
        if c.get('class') in ('excel-direct', 'textscan') and c.get('filename'):
            files.append(_authored_path(c))
    if files:
        # R47: matched by the authored path first (two folders of one package may hold files with the same name,
        # e.g. Data/en_US-US/ and Data/Superstore/), then by a file name that is unique in the package
        found = [_member(raw, f) for f in files]
        if all(found):
            objs = _objects(ds)
            if objs:
                # R47: each logical table reads its OWN file (a relationship between two CSVs / workbooks): the
                # file its relation's connection names -- a text connection names a folder, the table the file
                own = _object_files(ds)
                parts = []
                for oid, o in objs.items():
                    fn = own.get(oid)
                    member = _member(raw, fn) if fn else found[0]
                    if member is None:
                        out['reason'] = f'The data file {os.path.basename(fn)} is not inside the workbook'
                        return out
                    parts.append({'member': member, 'object': oid, 'sheet': _sheet(o['source'])})
            else:
                rel = next((r for r in ds.iter('relation') if r.get('type') == 'table'), None)
                parts = [{'member': found[0], 'object': None, 'sheet': _sheet(rel.get('table') if rel is not None else '')}]
            out.update(kind='file', files=parts)
            return _fingerprint(raw, out)
    extract = ds.find('.//extract/connection')
    if extract is None:
        # R47: a live connection to a .hyper file in the package (World Indicators) is the same data as an extract
        extract = next((c for c in conns if c.get('class') == 'hyper' and c.get('dbname')), None)
    hyper = _member(raw, (extract.get('dbname') or '').replace('\\', '/')) if extract is not None else None
    if hyper:
        out.update(kind='extract', files=[{'member': hyper}],
                   reason='Tableau extract (.hyper): the hosted app cannot decode it. Load it once from a laptop: '
                          + LAPTOP_COMMAND)
        return _fingerprint(raw, out)
    out['reason'] = ('The data file is not inside the workbook' if files else
                     'No data file or extract for this datasource is inside the workbook')
    return out


def _fingerprint(raw, out):
    h = hashlib.sha256()
    for part in sorted({p['member'] for p in out['files']}):
        h.update(part.encode())
        h.update(hashlib.sha256(_read_member(raw, part)).digest())
    out['sha256'] = h.hexdigest()
    return out


# --------------------------------------------------------------------------- #
# Build the table in memory and check it before writing
# --------------------------------------------------------------------------- #
def _read(data, member, sheet=None):
    import pandas as pd
    ext = os.path.splitext(member)[1].lower()
    # DM6 (E03): every cell as written (only an empty cell is NULL); shape() applies the workbook's types
    keep = {'keep_default_na': False, 'na_values': ['']}
    if ext in ('.xls', '.xlsx'):
        return pd.read_excel(io.BytesIO(data), sheet_name=sheet if sheet else 0, **keep)
    try:
        return pd.read_csv(io.BytesIO(data), encoding='utf-8-sig', dtype=str, **keep)
    except UnicodeDecodeError:
        return pd.read_csv(io.BytesIO(data), encoding='latin-1', dtype=str, **keep)


def frame(raw, root, loc, decoded=None):
    """One DataFrame for the datasource, at the workbook's grain (relationships flattened at fact grain,
    the same way local preview and the original accelerator did). decoded: a frame from a laptop decode."""
    import pandas as pd
    if isinstance(decoded, dict):
        return flatten(dict(decoded), root, loc) if len(decoded) > 1 else next(iter(decoded.values()))
    if decoded is not None:
        return decoded
    if loc['kind'] == 'union':
        frames = []
        for p in loc['files']:
            df = _read(_read_member(raw, p['member']), p['member']).copy()
            df['Table Name'] = os.path.splitext(os.path.basename(p['member']))[0]
            frames.append(df)
        return pd.concat(frames, ignore_index=True, sort=False)
    if loc['kind'] != 'file':
        raise ValueError(loc.get('reason') or 'No loadable file')
    data = _read_member(raw, loc['files'][0]['member'])
    if len(loc['files']) == 1:
        return _read(data, loc['files'][0]['member'], loc['files'][0]['sheet'])
    tables = {p['object']: _read(data if p['member'] == loc['files'][0]['member'] else _read_member(raw, p['member']),
                                 p['member'], p['sheet']) for p in loc['files']}       # R47: each from its own file
    return flatten(tables, root, loc)


def flatten(tables, root, loc=None):
    """Relationship tables -> one frame at fact grain (same planner as local preview). Exact duplicate rows
    in a DIMENSION table are removed first and counted in loc['notes']; if keys still repeat, refused."""
    import init_workbook as IW
    rels = [r for r in IW.parse_relationships(root, strict=True, composite=True) if r['first'] in tables and r['second'] in tables]
    dims = {r['second'] for r in rels}
    for oid in dims:
        before = len(tables[oid])
        tables[oid] = tables[oid].drop_duplicates().reset_index(drop=True)
        if loc is not None and len(tables[oid]) < before:
            loc.setdefault('notes', []).append(
                f"{IW._obj_caption(oid)}: {before - len(tables[oid])} exact duplicate rows removed before joining")
    df, _note = IW.flatten_tables(tables, rels, strict=True)
    return df


def _iso_date(s):
    import pandas as pd
    d = pd.to_datetime(s, errors='coerce')
    # object dtype: a missing date is None (a NULL in Snowflake) on pandas 2 and 3 alike, never NaN
    return d.dt.strftime('%Y-%m-%d').astype(object).where(d.notna(), None)


def _iso_ts(s):
    import pandas as pd
    d = pd.to_datetime(s, errors='coerce')
    return d.dt.strftime('%Y-%m-%d %H:%M:%S.%f').astype(object).where(d.notna(), None)


def _numbers(s):
    """Declared numeric column -> numbers; text keeps Tableau's reading of thousands separators ("74,814")."""
    import pandas as pd
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors='coerce')
    return pd.to_numeric(s.astype(str).str.replace(',', '', regex=False).str.strip().where(s.notna()), errors='coerce')


def shape(df, fields, extract=False):
    """Normalise column names (to_phys, as the workbook fields use) and types to the workbook's fields.
    Returns (frame, type fixes, fields to bind, absent columns). Raises with every problem listed.
    extract=True: a Tableau extract only holds the fields Tableau kept ("hide unused fields"), so a declared
    column missing from it is listed, not required."""
    import pandas as pd
    from backend import _normalize_columns
    from calc_translator import to_phys as _tp
    # DM6: protect declared TEXT (codes like '001') from inference; numbers and dates are converted and checked
    # below, where an unreadable value is a reported problem, never a silent NULL (E03)
    df = _normalize_columns(df, types={_tp(f['physical']): 'string' for f in fields
                                       if f.get('physical') and f.get('datatype') == 'string'})
    if len(df) > MAX_ROWS:
        raise ValueError(f'{len(df):,} rows: above the hosted load limit of {MAX_ROWS:,}; load it from a laptop')
    problems, fixes, kept, absent = [], {}, [], []
    from calc_translator import to_phys
    for f in fields:
        col, kind = to_phys(f['physical']), f.get('datatype')
        if col not in df.columns:
            (absent if extract else problems).append(col if extract else 'missing column ' + col)
            continue
        kept.append(f)
        s = df[col]
        if kind in ('integer', 'real'):
            num = _numbers(s)
            unreadable = s[s.notna() & num.isna() & (s.astype(str).str.strip() != '')]
            if len(unreadable):                  # never a silent NULL (E03)
                problems.append(f'{col}: {len(unreadable)} value(s) not a number (e.g. {str(unreadable.iloc[0])[:40]!r})')
            elif kind == 'integer' and (num.dropna() % 1 != 0).any():
                problems.append(f'{col}: expected whole numbers')
            else:
                df[col] = num.astype('Int64') if kind == 'integer' else num.astype('float64')
        elif kind == 'string':
            whole = str(s.dtype).startswith('float') and (s.dropna() % 1 == 0).all()   # 42420.0 -> '42420'
            df[col] = pd.Series([None if pd.isna(v) else (str(int(v)) if whole else str(v)) for v in s],
                                index=df.index, dtype=object)
        elif kind == 'boolean':
            df[col] = s.map(lambda v: None if pd.isna(v) else str(v).strip().lower() in ('true', '1', 'yes', 't'))
        elif kind == 'date':
            df[col], fixes[col] = _iso_date(s), 'DATE'
        elif kind == 'datetime':
            df[col], fixes[col] = _iso_ts(s), 'TIMESTAMP_NTZ'
    for c in df.columns:                      # columns the workbook does not use: dates kept as text-free types
        if c not in fixes and str(df[c].dtype).startswith('datetime'):
            df[c], fixes[c] = _iso_ts(df[c]), 'TIMESTAMP_NTZ'
    if problems:
        raise ValueError('; '.join(problems))
    for f in kept:
        # R47: a column with no value at all (BDO: TOTAL_15S_FOCUSED_VIEWS) carried no type, so the new table's
        # column got none and the workbook field could not bind to it; it takes the workbook's declared type
        col = to_phys(f['physical'])
        if col in df.columns and df[col].isna().all():
            df[col] = df[col].astype({'integer': 'Int64', 'real': 'float64', 'boolean': 'boolean'}
                                     .get(f.get('datatype'), 'string'))
    return df, fixes, kept, sorted(set(absent))


def plan(raw, root, ir, cap, decoded=None):
    """Everything the person sees before approving: source, rows, columns, target, and whether it fits."""
    import v3_data_entities as ENT
    loc = locate(raw, root, ir, cap)
    out = dict(loc, rows=None, columns=None, ok=False, target=target_base(cap))
    if loc['kind'] not in ('file', 'union') and decoded is None:
        return out
    frames = ENT.frames_for(raw, loc, decoded)
    fallback = None
    if frames:
        try:     # release 21b: one table per logical table, shared across workbooks
            ents, notes = ENT.plan_summary(root, ir, loc, frames, extract=loc['kind'] == 'extract')
            out.update(ok=True, mode='entities', entities=ents, notes=notes, reason=None,
                       rows=max(e['rows'] for e in ents), columns=sum(e['columns'] for e in ents))
            return out
        except Exception as exc:
            fallback = f'Loaded as one joined table, because the separate tables could not be modelled: {str(exc)[:200]}'
    try:
        df, _fixes, _kept, absent = shape(frame(raw, root, loc, decoded), fields_for(ir, loc['datasource_id']),
                                          extract=loc['kind'] == 'extract')
        notes = list(loc.get('notes') or [])
        if absent:
            notes.append('Not in the extract (Tableau keeps only the fields it uses): ' + ', '.join(absent))
        if fallback:
            notes.insert(0, fallback)
        out.update(rows=len(df), columns=len(df.columns), ok=True, reason=None, notes=notes, mode='joined')
    except Exception as exc:
        out.update(ok=False, reason=f'Not loadable as it stands: {str(exc)[:300]}')
    return out


# --------------------------------------------------------------------------- #
# Snowflake: find a loaded table again, or load a new one
# --------------------------------------------------------------------------- #
def target_base(cap):
    from calc_translator import to_phys
    name = re.sub(r'[^A-Z0-9_]', '_', to_phys(cap).upper()).strip('_') or 'DATA'
    return ('T_' + name if name[0].isdigit() else name)[:200]


def _tables(session, base):
    rows = session.sql(f"SHOW TABLES LIKE '{base}%' IN SCHEMA {LOAD_DB}.{LOAD_SCHEMA}").collect()
    out = {}
    for r in rows:
        d = r.as_dict() if hasattr(r, 'as_dict') else dict(r)
        d = {str(k).lower(): v for k, v in d.items()}
        if d.get('name'):
            out[str(d['name']).upper()] = str(d.get('comment') or '')
    return out


def _tagged(session):
    """Every table this accelerator loaded (name -> comment), whatever its name."""
    rows = session.sql(f'SHOW TABLES IN SCHEMA {LOAD_DB}.{LOAD_SCHEMA}').collect()
    out = {}
    for r in rows:
        d = {str(k).lower(): v for k, v in (r.as_dict() if hasattr(r, 'as_dict') else dict(r)).items()}
        c = str(d.get('comment') or '')
        if d.get('name') and c.startswith(TAG):
            out[str(d['name']).upper()] = c
    return out


def _first(tables, needle, prefer=None):
    hits = sorted(n for n, c in tables.items() if re.search(re.escape(needle) + r'(\s|$)', c))
    if not hits:
        return None
    best = next((n for n in hits if prefer and (n == prefer or re.fullmatch(re.escape(prefer) + r'_V\d', n))), hits[0])
    return f'{LOAD_DB}.{LOAD_SCHEMA}.{best}', tables[best]


def _own(name, base):
    return name == base or bool(re.fullmatch(re.escape(base) + r'_V\d', name))


def find_loaded(session, cap, sha):
    """A table loaded earlier from exactly these source bytes for this datasource, or None. A table with the
    datasource's own name matches on the source fingerprint; a table with another name (the same data reused,
    release 23) matches only on the fingerprint recorded together with this datasource's name, because one
    file can feed several datasources (two sheets of one workbook)."""
    base = target_base(cap)
    tables = _tagged(session)
    own = {n: c for n, c in tables.items() if _own(n, base)}
    hit = _first(own, f'src_sha256={sha}', base) or _first(tables, f'src_sha256={sha}@{base}', base)
    return hit[0] if hit else None


def find_by_data(session, data_sha, cap=None):
    """(table, comment) of a loaded table holding exactly this data, or None (release 23: reuse by data)."""
    return _first(_tagged(session), f'data_sha256={data_sha}', target_base(cap) if cap else None)


def find_reusable(session, raw, root, ir, cap):
    """Read-only (release 23): for data the hosted app can read itself (bundled Excel / CSV / unions), a table or
    relationship model already loaded that holds exactly the same data, found by data fingerprint under any name,
    so the run binds it without asking to load. None when nothing matches. Nothing is written."""
    import v3_data_entities as ENT
    loc = locate(raw, root, ir, cap)
    if loc['kind'] not in ('file', 'union'):
        return None
    flds = fields_for(ir, loc['datasource_id'])
    frames = ENT.frames_for(raw, loc)
    if frames:
        try:
            plan = ENT.find_by_data(session, root, ir, loc, frames)
        except Exception:
            plan = None
        if plan:
            return plan
    df, _fixes, _kept, _absent = shape(frame(raw, root, loc), flds)
    hit = find_by_data(session, ENT.data_sha(df), cap)
    return check_loaded(session, hit[0], flds) if hit else None


def free_name(session, cap):
    base = target_base(cap)
    taken = _tables(session, base)
    for name in [base] + [f'{base}_V{n}' for n in VERSIONS]:
        if name not in taken:
            return f'{LOAD_DB}.{LOAD_SCHEMA}.{name}', (name != base)
    raise RuntimeError(f'{base} and versions _V2.._V9 all exist; retire old versions before loading again')


def _ensure_schema(session):
    n = session.sql(f"SELECT COUNT(*) AS N FROM {LOAD_DB}.INFORMATION_SCHEMA.SCHEMATA "
                    f"WHERE SCHEMA_NAME = '{LOAD_SCHEMA}'").collect()[0][0]
    if not n:
        session.sql(f'CREATE SCHEMA IF NOT EXISTS {LOAD_DB}.{LOAD_SCHEMA}').collect()


def _fix_types(session, fqn, fixes):
    """Text columns written as ISO strings become DATE / TIMESTAMP_NTZ, in place."""
    for col, typ in fixes.items():
        conv = 'TO_DATE' if typ == 'DATE' else 'TO_TIMESTAMP_NTZ'
        tmp = col + '__T'
        session.sql(f'ALTER TABLE {fqn} ADD COLUMN {tmp} {typ}').collect()
        session.sql(f'UPDATE {fqn} SET {tmp} = {conv}({col})').collect()
        session.sql(f'ALTER TABLE {fqn} DROP COLUMN {col}').collect()
        session.sql(f'ALTER TABLE {fqn} RENAME COLUMN {tmp} TO {col}').collect()


def load(session, raw, root, ir, cap, who=None, via='app', decoded=None):
    """Write one datasource's data to a NEW table, fix types, tag it, prove the workbook fields bind.
    Returns the load record; the caller binds record['table'] only when record['bound']."""
    from v3_source_binding import validate_table
    import v3_data_entities as ENT
    loc = locate(raw, root, ir, cap)
    flds = fields_for(ir, loc['datasource_id'])
    if loc.get('sha256') and ENT.multi(root, ir, cap):
        try:
            model = ENT.find_model(session, root, ir, cap, loc['sha256'])
        except Exception:
            model = None
        if model:
            return dict(loc, table=model, bound=True, action='reused', rows=None, columns=None,
                        notes=model['checks']['notes'])
    frames = ENT.frames_for(raw, loc, decoded)
    if frames:
        try:     # release 21b: one table per logical table, shared across workbooks
            return ENT.load(session, raw, root, ir, cap, loc, frames, loc['kind'] == 'extract', who=who, via=via)
        except Exception as exc:
            loc = dict(loc, notes=[f'Loaded as one joined table: {str(exc)[:200]}'])
    found = find_loaded(session, cap, loc.get('sha256')) if loc.get('sha256') else None
    if found:
        table = check_loaded(session, found, flds)
        return dict(loc, table=table, bound=True, action='reused', rows=None, columns=None)
    df, fixes, kept, _absent = shape(frame(raw, root, loc, decoded), flds, extract=loc['kind'] == 'extract')
    dsha = ENT.data_sha(df)
    same = find_by_data(session, dsha, cap)
    if same:          # release 23: the same data from another file or workbook -> reuse that table
        fqn, comment = same
        token = f"src_sha256={loc['sha256']}" + ('' if _own(fqn.split('.')[-1], target_base(cap)) else '@' + target_base(cap))
        if loc.get('sha256') and not re.search(re.escape(token) + r'(\s|$)', comment):
            new = comment + ' ' + token
            session.sql(f"COMMENT ON TABLE {fqn} IS '{new.replace(chr(39), chr(39) * 2)}'").collect()
        table = check_loaded(session, fqn, flds)
        return dict(loc, table=table, bound=True, action='reused ' + fqn.split('.')[-1] + ' (same data)',
                    rows=None, columns=None)
    _ensure_schema(session)
    fqn, versioned = free_name(session, cap)
    name = fqn.split('.')[-1]
    session.write_pandas(df, name, database=LOAD_DB, schema=LOAD_SCHEMA, auto_create_table=True,
                         overwrite=False, quote_identifiers=False)
    _fix_types(session, fqn, fixes)
    n = int(session.sql(f'SELECT COUNT(*) AS N FROM {fqn}').collect()[0][0])
    if n != len(df):
        raise RuntimeError(f'{fqn}: {n} rows in Snowflake, {len(df)} in the file')
    files = ', '.join(sorted({os.path.basename(p['member']) for p in loc['files']}))
    note = (f"{TAG} data_sha256={dsha} src_sha256={loc['sha256']} files={files} rows={n} via={via} by={who or 'unknown'} "
            f"at={datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}")
    session.sql(f"COMMENT ON TABLE {fqn} IS '{note.replace(chr(39), chr(39) * 2)}'").collect()
    table = validate_table(session, fqn, engine_fields(kept))
    return dict(loc, table=table, bound=True, action='loaded as new version' if versioned else 'loaded',
                rows=n, columns=len(df.columns))


def engine_fields(fields):
    """Loaded tables have the engine's column names (to_phys), so the binding check uses that form.
    (Authored Snowflake tables keep the exact-name check in v3_source_binding.)"""
    from calc_translator import to_phys
    return [dict(f, physical=to_phys(f['physical'])) for f in fields]


def check_loaded(session, table, fields):
    """Binding check for a table this accelerator loaded: every workbook column the table has must have a
    compatible type (an extract's table lacks only the columns Tableau dropped as unused)."""
    from calc_translator import to_phys
    from v3_source_binding import validate_table
    cols = {str((r.as_dict() if hasattr(r, 'as_dict') else dict(r)).get('name')).upper()
            for r in session.sql('DESCRIBE TABLE ' + table).collect()}
    return validate_table(session, table, engine_fields([f for f in fields if to_phys(f['physical']) in cols]))


def log_row(run, rec, via='app', approval_id=None):
    table = rec.get('table')
    if isinstance(table, dict):
        table = ', '.join(t['table'] for t in table['tables'].values())
    return {'VIA': via, 'APPROVAL_ID': approval_id, 'DATASOURCE': rec.get('caption'),
            'WORKBOOK_NAME': (run or {}).get('name'), 'TABLE_NAME': table,
            'SOURCE_FILES': sorted({os.path.basename(p['member']) for p in rec.get('files') or []}),
            'SOURCE_SHA256': rec.get('sha256'), 'ROWS_LOADED': rec.get('rows'), 'COLUMNS': rec.get('columns'),
            'RESULT': rec.get('action')}
