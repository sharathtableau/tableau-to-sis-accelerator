"""EN29: record the EN05 seeded Cortex evaluation as a run in Snowflake AI Observability (owner's laptop tool).

Never part of the deployed app (tools/ is not packaged). The documented path is the TruLens SDK (>= 2.1.2:
trulens-core, trulens-connectors-snowflake, trulens-providers-cortex): an External Agent per app version and runs that
execute as Snowflake tasks. Scores come from OUR code (the EN05 scorer), not from an LLM judge.

  python tools/en29_observability.py --release release39 --connection <snow connection name>
(the owner signs in through their own Snow CLI connection; no credential is read or stored here)

TruLens call shapes follow the AI Observability docs (Nov 2026); confirm on the first owner run."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v3_accuracy as AC  # noqa: E402

APP_NAME = 'V3_CORTEX_ACCURACY'


def dataset():
    rows = [{'kind': 'cause', 'id': c['id'], 'input': json.dumps({'kind': 'cause', 'id': c['id']}),
             'ground_truth': c['seeded']} for c in AC.CAUSE_SEEDS]
    rows += [{'kind': 'translation', 'id': t['id'], 'input': json.dumps({'kind': 'translation', 'id': t['id']}),
              'ground_truth': t['reference']} for t in AC.TRANSLATION_SEEDS]
    return rows


def _load(text):
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def cause_top1(output, ground_truth):
    return 1.0 if _load(output).get('top') == ground_truth else 0.0


def translation_agree(output, ground_truth):
    return 1.0 if _load(output).get('result') == 'Agree' else 0.0


class App:
    """The evaluated app: one seeded input -> the production answer, scored by code."""
    def __init__(self, chooser, translate):
        self.chooser, self.translate = chooser, translate

    def answer(self, text):
        q = _load(text)
        if q.get('kind') == 'cause':
            case = next(c for c in AC.CAUSE_SEEDS if c['id'] == q.get('id'))
            ranked = [r.get('id') for r in (self.chooser(case, 1) or {}).get('ranked') or []]
            return json.dumps({'top': ranked[0] if ranked else None})
        t = next(t for t in AC.TRANSLATION_SEEDS if t['id'] == q.get('id'))
        return json.dumps({'result': self.translate(t)})


def production_app(session):
    import v3_cortex_calc as CC
    import v3_first_translator as FT
    ai_run = {'name': 'EN29', 'ai_log': []}
    ask = FT.cortex_ask(session, ai_run)

    def translate(t):
        item = {'field_id': t['id'], 'caption': t['id'], 'formula': t['formula'], 'engine_sql': None, 'refusal': None}
        p = FT.propose(item, AC.COLUMNS, ask)
        if p['state'] != 'PROPOSED':
            return p['state']
        return CC.compare_one(session, AC.values_relation(), t['reference'], p['sql'], t['level'] == 'aggregate')[0]
    return App(AC.production_chooser(session, ai_run), translate)


def owner_setup(role='WBR_OWNER'):
    return ['-- once, as an owner (laptop): pip install "trulens-core>=2.1.2" trulens-connectors-snowflake '
            'trulens-providers-cortex',
            f'GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE {role};',
            f'GRANT CREATE EXTERNAL AGENT ON SCHEMA WBR_DB.CX_EVAL TO ROLE {role};',
            f'GRANT CREATE TASK ON SCHEMA WBR_DB.CX_EVAL TO ROLE {role};',
            f'-- account level (an account admin grants it): GRANT EXECUTE TASK ON ACCOUNT TO ROLE {role};']


def run(session, release):
    """Register the app version and record one run; scores from cause_top1 / translation_agree."""
    import pandas as pd
    from trulens.apps.app import TruApp                                   # noqa: imported only when run
    from trulens.connectors.snowflake import SnowflakeConnector
    from trulens.core import Metric
    from trulens.core.otel.instrument import instrument
    from trulens.core.run import RunConfig
    connector = SnowflakeConnector(snowpark_session=session)
    app = production_app(session)

    class Instrumented:
        @instrument()
        def answer(self, text):
            return app.answer(text)
    tru = TruApp(Instrumented(), app_name=APP_NAME, app_version=str(release), connector=connector,
                 main_method=Instrumented.answer)
    df = pd.DataFrame(dataset())
    cfg = RunConfig(run_name=f'{release}-seeded', dataset_name=f'V3_SEEDED_{release}', source_type='DATAFRAME',
                    dataset_spec={'RECORD_ROOT.INPUT': 'input', 'RECORD_ROOT.GROUND_TRUTH_OUTPUT': 'ground_truth'})
    r = tru.add_run(run_config=cfg)
    r.start(input_df=df)
    r.compute_metrics([Metric(name='cause_top1', implementation=cause_top1),
                       Metric(name='translation_agree', implementation=translation_agree)])
    return {'app': APP_NAME, 'version': str(release), 'run': cfg.run_name, 'rows': len(df)}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--release', required=True)
    ap.add_argument('--connection', required=True, help='Snow CLI connection name (you sign in)')
    ap.add_argument('--print-setup', action='store_true')
    a = ap.parse_args()
    if a.print_setup:
        print('\n'.join(owner_setup()))
        sys.exit(0)
    from snowflake.snowpark import Session
    print(json.dumps(run(Session.builder.config('connection_name', a.connection).create(), a.release)))
