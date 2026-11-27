# Real Tableau REST crosstab fixtures

**These are real `Query View Data` responses, pulled from the live Tableau Cloud
account on 2026-08-13 via `tableau_server.pull_all_view_csvs()`. They are not
hand-made, and they must not be hand-edited.**

That provenance is the whole point. B3 was originally tracked as "Sharath
exports a sample CSV from the Tableau UI" — and a UI export produces a
*different* layout from what the REST pull returns. Building the reshape
against a hand-export would have handled a shape the pipeline never actually
receives. Every file here is a byte-for-byte REST response.

Source workbooks (site `b360bi`, `prod-useast-b.online.tableau.com`) — all
three are this project's own test corpus, not client content:

| Fixture | Workbook / view | Shape |
|---|---|---|
| `pivoted_customers.csv` | R1 Test Upload - Superstore / Customers | Pivoted, 6 measures × 4 regions |
| `pivoted_commission_model.csv` | ” / Commission Model | Pivoted, no overlapping wide columns |
| `pivoted_order_details_trimmed.csv` | ” / Order Details | Pivoted, 5-column grain (**trimmed** — see below) |
| `pivoted_datepart_what_if_forecast.csv` | ” / What If Forecast | Pivoted **and** date-part |
| `datepart_performance.csv` | ” / Performance | `Month of Order Date`, `Year of Order Date` |
| `datepart_forecast.csv` | ” / Forecast | `Month of Order Date` |
| `wide_regional_view1.csv` | Regional Analysis Dashboard / View1 | Ordinary wide shape (control) |

`pivoted_order_details_trimmed.csv` is the only modified file: the real pull is
35,777 rows, cut to the first **40 complete grain groups** (280 rows). Whole
groups only — a partial group would misrepresent the structure the reshape is
being tested against.

## What the pivoted shape actually is

Not the textbook long format. Tableau returns the measure columns **and** a
`Measure Names`/`Measure Values` pair, repeating each grain row once per
measure:

```
Measure Names        Region  Profit Ratio  Measure Values      Sales
Count of Customers   South   11.9%         512                 $391,722
Sales                South   11.9%         391,721.905000001   $391,722
Quantity             South   11.9%         6,209               $391,722
```

Two consequences drive the fix:

1. **Row duplication** — 6 measures × 4 regions = 24 rows where the chart has 4.
2. **The precise number is only in `Measure Values`.** The wide columns carry
   Tableau's *display-formatted* string (`$391,722`, `11.9%`); `Measure Values`
   carries full precision (`391721.905000001`, `0.119343416`).

So the reshape must take its values from `Measure Values`, not from the wide
column that happens to share the measure's name.
