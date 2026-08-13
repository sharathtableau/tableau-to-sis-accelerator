"""
tests/test_app_interactions.py  --  drives the GENERATED app like a user.

Uses streamlit.testing.v1.AppTest: renders app_superstore.py headless,
changes the 'New Quota' parameter in the sidebar, reruns, and asserts the
'OTE (Variable)' KPI actually moves (142,000 -> 160,400 @ 600K quota).
This is the guard for "the sidebar parameters do nothing".

Run:  python tests/test_app_interactions.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from streamlit.testing.v1 import AppTest   # noqa: E402


def _metric_values(at):
    return {m.label: m.value for m in at.main.get("metric")}


def main():
    at = AppTest.from_file("app_superstore.py", default_timeout=300)
    at.run()
    assert not at.exception, f"app crashed on load: {at.exception}"

    # Parameter controls live where the WORKBOOK puts them: a param placed on
    # a dashboard (<zone type='paramctrl'>, as Commission Model does for New
    # Quota) renders in that dashboard's control row; unplaced-but-used params
    # fall back to the sidebar. This test guards the GUARANTEE -- the control
    # exists and drives the numbers -- not the placement we used to invent.
    widgets = {w.label: w for w in at.number_input}
    assert "New Quota" in widgets, \
        f"'New Quota' has no parameter control anywhere: {list(widgets)}"

    before = _metric_values(at)
    assert "OTE (Variable)" in before, f"OTE KPI not found: {list(before)}"
    print(f"ok  {len(widgets)} numeric parameter controls reachable")
    print(f"ok  OTE before: {before['OTE (Variable)']}")
    assert before["OTE (Variable)"] == "$142,000", before["OTE (Variable)"]

    widgets["New Quota"].set_value(600000).run()
    assert not at.exception, f"app crashed on param change: {at.exception}"
    after = _metric_values(at)
    print(f"ok  OTE after New Quota=600000: {after['OTE (Variable)']}")
    assert after["OTE (Variable)"] == "$160,400", \
        f"parameter change did NOT propagate: {after['OTE (Variable)']}"

    print("\nAPP INTERACTION TEST PASSED (parameters drive the numbers)")


if __name__ == "__main__":
    main()
