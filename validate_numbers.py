"""
validate_numbers.py  --  numeric validation harness.

Asserts that the SQL the engine generates reproduces the figures verified
against Tableau (see NEW_CHAT.md "Source + ground truth" and the Customer
Analysis session). Exits non-zero on any mismatch -- run it before trusting
a regenerated app, and in CI.

Usage:  python validate_numbers.py
"""

import json
import sys

import engine
from backend import run_sql

FAIL = []


def check(name, got, want, tol=0.005):
    ok = abs(got - want) <= abs(want) * tol + 1e-9
    print(f"  {'OK  ' if ok else 'FAIL'} {name:<42} got {got:,.2f}  want {want:,.2f}")
    if not ok:
        FAIL.append(name)


def main():
    ir = json.load(open("workbook_ir.json"))
    engine.configure(ir)

    print("Grand totals (Sample - Superstore vs Tableau):")
    df = run_sql("SELECT SUM(SALES) S, SUM(PROFIT) P, SUM(QUANTITY) Q, "
                 "SUM(PROFIT)/NULLIF(SUM(SALES),0) R FROM SUPERSTORE.PUBLIC.ORDERS")
    r = df.iloc[0]
    check("SUM(Sales)", float(r.S), 2326534, 0.0005)
    check("SUM(Profit)", float(r.P), 292297, 0.0005)
    check("SUM(Quantity)", float(r.Q), 38654, 0.0005)
    check("Profit Ratio", float(r.R), 0.126, 0.01)

    # CustomerOverview (Tableau-verified in session 2026-07-01):
    # customers West 686 / East 681 / Central 629 / South 512;
    # Sales West $739,814; Profit Ratio 15.0/13.7/7.9/11.9 %
    print("CustomerOverview per-region (with the sheet's captured filters):")
    sheet = next(s for d in ir["dashboards"] for s in d["sheets"]
                 if s["name"] == "CustomerOverview")
    T = engine.tbl(sheet)
    where = engine._apply_sheet_filters(sheet, "", T)
    df = run_sql(f"SELECT REGION, COUNT(DISTINCT CUSTOMER_NAME) CUST, SUM(SALES) SALES, "
                 f"SUM(PROFIT)/NULLIF(SUM(SALES),0) PR FROM {T} {where} GROUP BY 1")
    d = df.set_index("REGION")
    for reg, cust, pr in [("West", 686, 0.150), ("East", 681, 0.137),
                          ("Central", 629, 0.079), ("South", 512, 0.119)]:
        check(f"{reg} customer count", float(d.loc[reg, "CUST"]), cust, 0.0005)
        check(f"{reg} profit ratio", float(d.loc[reg, "PR"]), pr, 0.02)
    check("West sales", float(d.loc["West", "SALES"]), 739814, 0.001)

    # Commission Model: OTE is pure parameter math -> exactly determined:
    # Base 50,000 + 18.4% * 500,000 = 142,000
    print("Commission Model (parameter-derived):")
    ote = engine.sub_params(engine.CALCS["OTE (Variable)"]["sql"])
    df = run_sql(f"SELECT AVG({ote}) V FROM SUPERSTORE.PUBLIC.SALES_COMMISSION")
    check("OTE (Variable)", float(df.V[0]), 142000, 0.0001)

    print()
    if FAIL:
        print(f"NUMERIC VALIDATION FAILED ({len(FAIL)}): {FAIL}")
        sys.exit(1)
    print("NUMERIC VALIDATION PASSED")


if __name__ == "__main__":
    main()
