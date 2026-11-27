"""
profile_default.py  --  NEUTRAL client profile: the fallback for any workbook
that has no client-specific profile file of its own.

Every field is empty on purpose. A workbook's own calculated fields and raw
columns already resolve correctly without this (see engine._resolve_measure's
resolution order: workbook calc -> profile library -> physical column) --
this profile exists only so an unrecognized/foreign workbook does NOT silently
inherit another client's curated measure SQL, formats, KPI order, or colors
(profile_superstore.py's MEASURE_LIBRARY uses generic captions like "Sales"/
"Profit"/"Discount" that a genuinely different client's workbook could
coincidentally also use for an unrelated raw field).

A new client that wants curated polish (formats, ordering, color overrides)
gets a real profile: copy this file to profile_<client>.py, fill in what's
needed, and register it in config.py's profile registry.
"""

MEASURE_LIBRARY = {}
CAPTION_ALIASES = {}
KPI_ORDER = []
DIM_VALUE_LABELS = {}
DIM_VALUE_COLORS = {}
