"""
profile_superstore.py  --  CLIENT PROFILE for the Sample - Superstore workbook.

Everything workbook/client-specific that is NOT derivable from the .twb lives
here: curated measure SQL + display formats, caption aliases, KPI ordering,
friendly labels for boolean calc dimensions. Core accelerator code (parser /
translator / engine) must stay workbook-agnostic and only consult this through
config.PROFILE. A new client gets a new profile file; core code is untouched.

NOTE: the measure library is a FALLBACK for format/label polish. Measures that
exist as calculated fields in the workbook translate automatically and take
precedence -- see engine.measure_sql resolution order.
"""

# Curated measure SQL (physical UPPER_SNAKE columns) + display format.
MEASURE_LIBRARY = {
    "Sales":              {"sql": "SUM(SALES)",  "fmt": "money0"},
    "Profit":             {"sql": "SUM(PROFIT)", "fmt": "money0"},
    "Quantity":           {"sql": "SUM(QUANTITY)", "fmt": "int"},
    "Discount":           {"sql": "AVG(DISCOUNT)", "fmt": "pct"},
    "Profit Ratio":       {"sql": "SUM(PROFIT) / NULLIF(SUM(SALES), 0)", "fmt": "pct"},
    "Avg. Discount":      {"sql": "AVG(DISCOUNT)", "fmt": "pct"},
    "Avg Discount":       {"sql": "AVG(DISCOUNT)", "fmt": "pct"},
    "Profit per Order":   {"sql": "SUM(PROFIT) / NULLIF(COUNT(DISTINCT ORDER_ID), 0)", "fmt": "money2"},
    "Sales per Customer": {"sql": "SUM(SALES) / NULLIF(COUNT(DISTINCT CUSTOMER_NAME), 0)", "fmt": "money2"},
}

CAPTION_ALIASES = {"Sales per Customer (copy)": "Sales per Customer"}

KPI_ORDER = ["Sales", "Profit", "Profit Ratio", "Profit per Order",
             "Sales per Customer", "Avg. Discount", "Quantity"]

# OVERRIDE hook for value labels. Normally EMPTY: the parser extracts the
# workbook's own aliases (ir["aliases"]) and the engine applies them. Only add
# entries here when a client wants labels that differ from the workbook.
# caption -> {raw value as string: display label}
DIM_VALUE_LABELS = {}

# OVERRIDE hook for categorical value COLORS (wins over the workbook's own
# color maps). The downloaded World Indicators twbx declares a different
# Region palette than the stock Tableau Desktop sample the user compares
# against -- these are the stock (Tableau 10) assignments.
DIM_VALUE_COLORS = {
    "Region": {
        "Europe":       "#76b7b2",   # teal
        "The Americas": "#59a14a",   # green
        "Asia":         "#edc949",   # yellow
        "Africa":       "#f28e2b",   # orange
        "Middle East":  "#9c755f",   # brown
        "Oceania":      "#4e79a7",   # dark blue
    },
}
