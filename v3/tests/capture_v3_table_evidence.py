"""Local numeric/render evidence only; no Tableau or Snowflake parity claim."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from test_v3_tables import fixture, parse, bindings, TableUI, engine, preview_context


def capture():
    results = []
    for multi, reverse in ((False, False), (True, True)):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(fixture(multi=multi, reverse=reverse, duplicate=multi), folder)
            with preview_context(ir, bindings(folder)):
                for sheet in ir['dashboards'][0]['sheets']:
                    ui = TableUI()
                    with patch.object(engine, 'st', ui), patch.object(engine, 'q', wraps=engine.q) as query:
                        engine.render_sheet(sheet, '')
                    assert not ui.errors and len(ui.tables) == 1, ui.errors
                    results.append({'shape': 'nested-repeated' if multi else 'single-dimension',
                                    'datasource_id': sheet['datasource_id'],
                                    'plan': sheet['worksheet_semantics']['table_execution'],
                                    'sql': query.call_args.args[0], 'display_labels': ui.labels[0],
                                    'result': json.loads(ui.tables[0].to_json(orient='split')),
                                    'caption': ui.captions})
    return {'status': 'LOCAL_FIXTURE_EXECUTION', 'tableau_parity': 'NOT_VERIFIED',
            'snowflake_runtime': 'NOT_VERIFIED', 'complete_migration': 'NOT_VERIFIED',
            'results': results}


if __name__ == '__main__':
    Path(sys.argv[1]).write_text(json.dumps(capture(), indent=2), encoding='utf-8')
