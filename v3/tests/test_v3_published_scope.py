"""Cloud migrations cover the dashboards Tableau Cloud publishes as views, nothing hidden."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v3_local import published_scope


def _ir():
    return {'dashboards': [{'name': 'Dashboard'}, {'name': 'Product'}, {'name': 'Details'}]}


class PublishedScopeTests(unittest.TestCase):
    def test_only_published_dashboards_are_migrated_and_the_rest_listed(self):
        ir = published_scope(_ir(), ['Dashboard'])
        self.assertEqual([d['name'] for d in ir['dashboards']], ['Dashboard'])
        self.assertEqual(ir['unpublished_dashboards'], ['Product', 'Details'])

    def test_no_view_list_or_no_dashboard_match_keeps_everything(self):
        for views in (None, [], ['Some worksheet']):
            ir = published_scope(_ir(), views)
            self.assertEqual(len(ir['dashboards']), 3)
            self.assertNotIn('unpublished_dashboards', ir)


if __name__ == '__main__':
    unittest.main()
