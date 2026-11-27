"""Evidence-based refusal inventory. Diagnostic only; never changes support gates.

Reason families are deliberately coarse because existing reasons combine several
constructs. Counts describe reported obstacles, not predicted fixes or fidelity.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

RULES = {
    'renderer_contract': r'Occurrence adapter|Authored mark|renderer contract|KPI measure|KPI has row|Multi-measure adapter|Discrete aggregate headers',
    'pane_and_shelf_plan': r'independent column|Independent axes|Multiple or absent panes|layer plan|axis selector|Axis selector|Pane axes|Pane requires|inheritance|Bar requires exactly|multi-measure axis',
    'filter_sort_style_or_view_settings': r'filters/sorts/fiscal|view aggregation|Runtime filters|Dynamic grain|Dynamic, blended',
    'relationships_and_sources': r'relationship|physical table relation|Cross-datasource|datasource.*ambig',
    'date_grain': r'Date-grain|date-aware',
    'unresolved_query_semantics': r'Unresolved, generated|field/calculation/date planning|row-level dimensions|query plan',
    'unconsumed_encoding': r'Unconsumed|Unplanned (?:color|cols|rows|text|shape|size|detail|lod|path|tooltip)|stacking plan',
    'field_or_calculation_failure': r'untranslated|not convertible|ambiguous.*caption|missing.*field|calculation.*failed|Occurrence extraction failed|Unknown datasource|Cannot bind|unresolved.*reference',
}
COMPILED = {key: re.compile(pattern, re.I) for key, pattern in RULES.items()}


def reason_families(reason):
    """Keep uncategorized clauses visible instead of guessing a root cause."""
    found = set()
    for clause in str(reason or '').split(';'):
        if not clause.strip(): continue
        matches = {key for key, pattern in COMPILED.items() if pattern.search(clause)}
        found.update(matches or {'unclassified'})
    return found or {'unclassified'}


def summarize(assessments):
    if not isinstance(assessments, list):
        raise ValueError('Expected a list of assessment records')
    states, buckets = Counter(), {}
    files_blocked = intake_failures = issue_count = 0
    total = 0
    for index, record in enumerate(assessments):
        if not isinstance(record, dict): raise ValueError('Each assessment must be an object')
        source = record.get('file') or f'assessment:{index}'
        rows = record.get('sheets', [])
        if not isinstance(rows, list): raise ValueError('Assessment sheets must be a list')
        issue_count += len(record.get('issues') or [])
        intake_failures += bool(record.get('error'))
        blocked = bool(record.get('error') or record.get('issues') or
                       record.get('untranslated_calculations') or record.get('state') == 'BLOCKED')
        for row in rows:
            if not isinstance(row, dict): raise ValueError('Each sheet assessment must be an object')
            state = row.get('state', 'UNKNOWN')
            if state not in ('UNSUPPORTED', 'INVALID', 'PARTIAL', 'UNVERIFIED'):
                state = 'UNKNOWN'
            states[state] += 1
            total += 1
            if state not in ('UNSUPPORTED', 'INVALID', 'PARTIAL', 'UNKNOWN'):
                continue
            blocked = True
            families = reason_families(row.get('reason'))
            for family in families:
                bucket = buckets.setdefault(family, {'family': family, 'sheet_occurrences': 0,
                    'only_reported_family': 0, 'files': set(), 'examples': []})
                bucket['sheet_occurrences'] += 1
                bucket['only_reported_family'] += len(families) == 1
                bucket['files'].add(source)
                if len(bucket['examples']) < 3:
                    bucket['examples'].append({'file': source, 'dashboard': row.get('dashboard'),
                        'sheet': row.get('sheet'), 'reason': str(row.get('reason', ''))[:1200]})
        files_blocked += blocked
    families = []
    for bucket in buckets.values():
        bucket['file_count'] = len(bucket.pop('files'))
        families.append(bucket)
    families.sort(key=lambda b: (-b['sheet_occurrences'], b['family']))
    return {'version': 'coverage-diagnostics/1', 'assessment_records': len(assessments),
            'assessment_sha256': hashlib.sha256(json.dumps(assessments, sort_keys=True).encode()).hexdigest(),
            'sheet_occurrences': total, 'states': dict(sorted(states.items())),
            'blocked_or_partial_records': files_blocked, 'intake_failures': intake_failures,
            'structural_issue_count': issue_count,
            'families': families,
            'release_readiness': 'NOT_ESTABLISHED',
            'verification': {key: 'NOT_EVALUATED' for key in ('numeric', 'visual', 'interaction', 'snowflake_runtime', 'held_out_workbooks')},
            'limitations': [
                'Counts are dashboard sheet occurrences, not unique independent workbook constructs.',
                'Related/versioned files and sheets repeated across dashboards remain in the counts.',
                'Families overlap; their counts must not be summed as distinct sheets.',
                'Combined refusal messages cannot establish which single construct caused the failure.',
                'Only-reported-family counts are not a prediction of sheets unlocked by a fix.',
                'Assessment snapshots do not establish execution, parity or partner-demo readiness.',
            ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('assessment', type=Path)
    parser.add_argument('--output', type=Path, default=Path('coverage_priorities.json'))
    args = parser.parse_args()
    report = summarize(json.loads(args.assessment.read_text(encoding='utf-8-sig')))
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in ('families', 'limitations')}, indent=2))
    for row in report['families']:
        print(f'{row["family"]}: {row["sheet_occurrences"]} sheet occurrences / {row["file_count"]} files')


if __name__ == '__main__': main()
