"""
findings.py  --  conversion-findings registry (the "report, don't guess" layer).

Every place the accelerator would previously guess or silently skip now records
a finding here instead. The engine surfaces them in-app, verify/report tooling
dumps them into the compatibility report.

Severities:
  BLOCKER  sheet (or a measure on it) cannot render -- shown in-app
  WARNING  rendered, but with a documented approximation
  INFO     informational (e.g. cosmetic constructs not carried over)
"""

_FINDINGS = []


def clear():
    del _FINDINGS[:]


def record(severity, sheet, code, message):
    f = {"severity": severity, "sheet": sheet, "code": code, "message": message}
    if f not in _FINDINGS:          # dedupe: renderers re-run on every rerun
        _FINDINGS.append(f)
    return f


def all_findings():
    return list(_FINDINGS)


def for_sheet(sheet):
    return [f for f in _FINDINGS if f["sheet"] == sheet]
