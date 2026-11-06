"""Bugs per Epics report for Google Sheets - epic breakdown by bug sub-type.

Fetches ALL project bugs across the four sub-type issuetypes this project
tracks (BE BUG / Bug (the "UAT" category elsewhere in this codebase) /
Mobile bug / FE bug - confirmed against real Jira data and this tab's own
pre-existing cell contents/chart specs, not guessed), resolves each bug's
Epic, and (re)writes seven tables into the "Bugs per Epics" sheet tab:

- "Opened Bugs (current status)" - Epic x open-bug count per sub-type,
  snapshot (refreshed every run, never grows).
- "All Bugs (current status)" - Epic x total-bug count per sub-type,
  snapshot.
- "Сравнение по эпикам: всего и открытых багов (по типу бага)" - Epic x
  Total/Opened pairs per sub-type, with a COLUMN chart (chart series/domain
  exclude the "Total Unique Issues:" row and the grand-total pair columns -
  confirmed against the real chart spec already on this tab, not guessed).
- "Opened Bugs Total" - Epic x run-date (ALL sub-types combined, open only),
  growing by one column per run, with a LINE chart.
- "Opened Mobile bug" / "Opened BE BUG" / "Opened FE bug" - same shape as
  "Opened Bugs Total" but each scoped to one sub-type only.

"Open" means status not in (Cancelled/"Отменено", PASSED) - matching this
tab's own real historical hyperlink convention, confirmed by inspecting the
real .xlsx's cell hyperlinks directly - per explicit user request, overriding
an earlier `resolution = Unresolved` simplification. "Отменено" is this Jira
instance's live (Russian) rename of the status registered as "Cancelled" -
see CLOSED_STATUSES.

Per explicit user decision: "FE bug" (a 4th sub-type with real, currently
open issues in Jira - confirmed via live query - that the historical version
of this tab's columns never accounted for) is included as a 4th column in
every table, same as the other three, AND gets its own "Opened FE bug"
growing time series, same as Mobile bug/BE BUG. Also per explicit user
decision, this report's hyperlinks are simplified to an epic/sub-type/open
JQL filter rather than also replicating the real tab's extra "Phasa"
cascading-field filter, whose meaning wasn't understood well enough to
replicate correctly.

Table layout (settled 2026-10-19, per explicit user decision, after this
module went through several redesigns this same week - see memory for the
full history): every table's position is FIXED and FROZEN forever, matching
exactly where each one already sits on this tab's real spreadsheet (two
independent column bands - the three snapshot tables in column A
(SNAPSHOT_COL0), the four growing time-series tables in column U (TS_COL0),
spaced 40 rows apart from each other within each band) - a run NEVER moves
any table to a different position, no matter how large it grows; it only
clears and rewrites that one table's own fixed, generously-budgeted range
(ROW_BUDGET/TS_ROW_BUDGET = 35, a 5-row safety margin under the 40-row
spacing) and, for the four growing tables, appends one new date column.
Explicitly NOT in scope (per the same user decision): this tab's own
separate, older "Сравнение по эпикам" 3-sub-type snapshot, which on some
real spreadsheets still lingers near row 66 in column A, overlapping part
of Table2's fixed range - a known, deliberately-untouched pre-existing
issue, left for a dedicated follow-up.

Changing the open-bug definition only affects how TODAY's (and future) date
columns get computed from live Jira data - the time-series merge logic
(_merge_timeseries) only ever recomputes the current run's own date column
in place and never touches previously-written historical columns, so no
code change was needed to keep old snapshots exactly as they were.

Epic resolution, snapshot/time-series merge semantics (append-order, same-
day in-place overwrite, never losing history), and hyperlink/cell-formatting
mechanics are all identical in approach to packages/workflows/uat_bugs_report
- duplicated here rather than imported, per this project's own convention of
keeping each workflow package self-contained with no cross-imports.

Read-only on Jira; only writes to Google Sheets.
"""

import re
from datetime import date, datetime
from typing import Any, Callable, Optional
from urllib.parse import quote_plus

from packages.common import get_logger

logger = get_logger(__name__)

DEFAULT_JQL = 'project = MB AND issuetype in ("BE BUG", Bug, "Mobile bug", "FE bug") ORDER BY parent ASC'
IDEAS_JQL = "project = MB20PB"
EPICS_JQL = "project = MB AND issuetype = Epic"
IDEAS_EPIC_FIELD_ID = "customfield_10168"
SHEET_TITLE = "Bugs per Epics"

BUGS_FIELDS = "summary,status,resolution,parent,issuelinks,issuetype"
IDEAS_FIELDS = f"summary,{IDEAS_EPIC_FIELD_ID}"
EPICS_FIELDS = "summary"

NO_EPIC_LABEL = "(без эпика)"
# "Open" means not Cancelled/PASSED - matching this tab's own real historical
# convention (confirmed via its real hyperlinks: `status not in (PASSED,
# Cancelled)`) - per explicit user request, overriding this workflow's
# earlier `resolution = Unresolved` simplification. "Cancelled" is this
# Jira instance's old (English) status name - it's been renamed to
# "Отменено" live (confirmed via `GET /rest/api/3/project/MB/statuses`),
# same rename uat_bugs_report already tracks for the same reason.
#
# `_is_open()` below matches by NAME against already-fetched issue data -
# that's a plain Python string comparison and works correctly. The JQL
# hyperlink clause is different: matching `status` BY NAME in JQL
# (`status != "Отменено"`, `status not in ("Отменено", PASSED)`, even
# `status = "Отменено"` alone) silently fails to exclude/match real
# "Отменено" issues - confirmed directly against live Jira (a query scoped
# to a single known-Cancelled issue key still returned it). Matching by the
# status's numeric ID works correctly and was confirmed the same way
# (`status != 10253` correctly excludes it). This looks like a stale
# name-to-ID index on Jira's side left over from the Cancelled->Отменено
# rename, not anything fixable from the client side - so the JQL clause
# uses IDs while the Python-side check keeps using the name (no bug there,
# no reason to change it). IDs confirmed live: 10253 = "Отменено",
# 10571 = "PASSED" - re-verify both if this project's workflow is ever
# re-configured (a scheme change could reassign these).
CLOSED_STATUSES = {"Отменено", "PASSED"}
CLOSED_STATUS_IDS = ("10253", "10571")  # Отменено, PASSED - see note above on why IDs, not names
# Belt-and-suspenders, per explicit user request: also list both the current
# (Отменено) and pre-rename (Cancelled) names alongside the IDs in the same
# `not in` list - harmless if a name never matches (JQL just contributes no
# extra matches for it), but covers this status ever getting renamed again
# in either direction without anyone remembering to update the ID here.
OPEN_JQL_CLAUSE = f'status not in ({", ".join(CLOSED_STATUS_IDS)}, "Отменено", Cancelled, PASSED)'

# This Jira instance's live issuetype display name for "Bug" has been
# Russian-renamed (confirmed via live query: JQL `issuetype = Bug` still
# matches correctly - it's the canonical registered name - but the fetched
# issue's own `fields.issuetype.name` now reads "Баг") - same class of
# rename this project already handles for statuses (see STATUS_ALIAS in
# uat_bugs_report). The other three sub-type names are unrenamed.
SUBTYPE_ALIAS = {"Баг": "Bug"}
SUBTYPE_COLUMNS = ["BE BUG", "Bug", "Mobile bug", "FE bug"]

# Template/example bugs some of this project's bugs are cloned from - never
# real bugs, excluded from every table (same marker as uat_bugs_report).
EXCLUDE_SUMMARY_MARKER = "[BUG EXAMPLE"

_DARK_TEXT = {"red": 0.16078432, "green": 0.16470589, "blue": 0.18039216}
_TITLE_BG = {"red": 0.42745098, "green": 0.61960787, "blue": 0.92156863}
_HEADER_BG = {"red": 0.7882353, "green": 0.85490197, "blue": 0.972549}
_WHITE_BG = {"red": 1, "green": 1, "blue": 1}

# Matches this tab's own real cell formatting (read directly via the Sheets
# API before writing this, not guessed) - a merged blue title banner, a
# light-blue bold centered header row, plain white data rows, a light-blue
# bold "Total" row. One deliberate simplification vs. the original: the
# original also bolds just the per-row "T:" (sum) cell within otherwise
# plain data rows - `write_cells_with_links` only supports one style per
# whole row, so that per-cell nuance is dropped in favor of the simpler,
# already-established uniform-row-style convention.
TITLE_STYLE = {"backgroundColor": _TITLE_BG, "horizontalAlignment": "CENTER", "textFormat": {"bold": True, "foregroundColor": _DARK_TEXT}}
HEADER_STYLE = {"backgroundColor": _HEADER_BG, "horizontalAlignment": "CENTER", "textFormat": {"bold": True, "foregroundColor": _DARK_TEXT}}
DATA_STYLE = {"backgroundColor": _WHITE_BG, "horizontalAlignment": "LEFT", "textFormat": {"bold": False, "foregroundColor": _DARK_TEXT}}
TOTAL_STYLE = {"backgroundColor": _HEADER_BG, "horizontalAlignment": "LEFT", "textFormat": {"bold": True, "foregroundColor": _DARK_TEXT}}


def _row_styles(num_epic_rows: int) -> list[dict[str, Any]]:
    """[title, header, data..., total] - matches grid shape: row0=title,
    row1=header, rows2..2+n-1=epics, last row=Total Unique Issues."""
    return [TITLE_STYLE, HEADER_STYLE] + [DATA_STYLE] * num_epic_rows + [TOTAL_STYLE]


_US_CODE_PREFIX_RE = re.compile(r"\s*(US-[\d.]+)\s*\|")
_US_CODE_ANYWHERE_RE = re.compile(r"(US-[\d.]+)")
_ORDER_BY_RE = re.compile(r"\s+ORDER BY\s+.*$", re.IGNORECASE)

# Row/column layout within the "Bugs per Epics" tab - FIXED forever (see
# module docstring), confirmed cell-by-cell against the real spreadsheet
# (per explicit, detailed user specification, 2026-10-19). Each table's own
# position never recomputed, never migrated. SNAPSHOT_COL0 (column A) for
# the three snapshot tables, each paired at the SAME row with a growing
# table in a DIFFERENT column on that same row band: "Opened Bugs Total"
# sits in column H (TS_TOTAL_COL0) right next to Table1 - this tab's own
# original pairing - while "Opened Mobile bug"/"Opened BE BUG"/"Opened FE
# bug" sit in column U (TS_COL0), paired with Table2/Table3's row bands.
# Each band is 40 rows apart, with a 35-row clear/write budget per table (a
# 5-row safety margin) - a fixed ceiling on how many epic/date rows a single
# table can hold before it would start overlapping the next one in its own
# band; accepted as-is per explicit user decision (no auto-migration anymore
# if a table ever outgrows it).
SNAPSHOT_COL0 = 0  # column A
TABLE1_START = 1  # "Opened Bugs (current status)"
TABLE2_START = 41  # "All Bugs (current status)"
TABLE3_START = 81  # "Сравнение по эпикам..." + its chart
ROW_BUDGET = 35

TS_TOTAL_COL0 = 7  # column H - paired with Table1 on the same row band
TS_COL0 = 20  # column U - "Opened Mobile bug"/"Opened BE BUG"/"Opened FE bug"
TS_TOTAL_START = 1  # "Opened Bugs Total"
TS_MOBILE_START = 41  # "Opened Mobile bug"
TS_BE_START = 81  # "Opened BE BUG"
TS_FE_START = 121  # "Opened FE bug"
# "Opened Bugs Total" shares column H with "Opened Mobile bug"'s OLD legacy
# adoption-fallback position (row 26 - see LEGACY_POSITION_BY_TITLE) - a
# generous clear budget here would overreach into that slot and silently
# destroy Mobile's not-yet-migrated history before Mobile's own turn to run
# even starts (confirmed as a real bug in practice: this exact collision
# already happened once this session with the old two-column-band layout,
# and reappeared the moment Total moved back into column H). Total's own
# real footprint comfortably fits in far fewer than 35 rows (22, per the
# real spreadsheet), so a tight, permanent 24-row budget costs nothing and
# removes the collision risk entirely - Mobile/BE/FE's own column U has no
# such neighbour, so they keep the generous budget.
TS_TOTAL_ROW_BUDGET = 24
TS_ROW_BUDGET = 35
TS_COL_BUDGET = 150  # generous width for growing dates - nothing else shares these columns' rows anymore

# One-time-only adoption fallback: checked ONLY when nothing is found at a
# growing table's fixed position above (never an ongoing migration decision
# - once a table exists at its fixed position, this is never consulted
# again for it). Each entry is (col0, row1, row_budget, col_budget) for
# wherever that table's real history might still be sitting from BEFORE its
# current fixed position:
# - "Opened Mobile bug"/"Opened BE BUG": this tab's own ORIGINAL tight
#   hand-maintained position (column H, rows 26/45 - confirmed directly
#   against a real spreadsheet). The narrow row budget (18) is this tab's
#   own tight spacing to the next legacy table, kept small so this one-time
#   clear can never reach this tab's separate, older "Сравнение по эпикам"
#   snapshot (which on some real spreadsheets starts around row 66 in
#   column A but happens to use column H too for its own "Total T:"/"Opened
#   T:" columns).
# - "Opened Bugs Total": this workflow's OWN earlier position (column U,
#   before 2026-10-19 - see module docstring) - not a hand-maintained
#   original, just this code's own prior design, migrated to column H
#   (TS_TOTAL_COL0, matching this tab's real original pairing with Table1)
#   per explicit user decision.
# "Opened FE bug" never existed before this workflow created it, so it has
# no fallback entry.
LEGACY_COL0 = 7  # column H - used by the Mobile/BE fallback entries below
LEGACY_COL_BUDGET = TS_COL0 - LEGACY_COL0 - 2  # = 11, stays short of column U

# One-time cleanup (not an adoption - this table is a pure snapshot, nothing
# to carry forward): this tab's own ORIGINAL "Сравнение по эпикам" position
# (label row 65, title row 66, 3 sub-types, 9 columns A-I) left over from
# BEFORE this table's own fixed position moved to row 81 (TABLE3_START,
# with the 4th "FE bug" sub-type added) - confirmed directly against a real
# spreadsheet: this old block's title/header/VLOOKUP-formula rows otherwise
# linger forever (rows 65-80 are never touched by TABLE3_START's own
# clear/write, which only reaches row 81 onward) - both blue title-banner
# FORMATTING (`clear_values` alone never removes it) and literal leftover
# VLOOKUP formula cells. Row 81 itself needs no separate handling - it's
# already the new table's own title row, overwritten every run regardless.
LEGACY_TABLE3_TITLE_ROW = 66
LEGACY_TABLE3_CLEAR_FROM_ROW = 65  # includes the "на сегодня" label row above the title
LEGACY_TABLE3_CLEAR_ROWS = 16  # rows 65-80 inclusive, stopping short of row 81 (the new table's own title row)
LEGACY_TABLE3_COL_BUDGET = 9  # this old table's real width (A-I, 9 cols)

LEGACY_POSITION_BY_TITLE = {
    "Opened Mobile bug": (LEGACY_COL0, 26, 18, LEGACY_COL_BUDGET),
    "Opened BE BUG": (LEGACY_COL0, 45, 18, LEGACY_COL_BUDGET),
    "Opened Bugs Total": (TS_COL0, TS_TOTAL_START, TS_ROW_BUDGET, TS_COL_BUDGET),
}

# One-time cleanup, per explicit user decision (2026-10-19): column G, rows
# 3-15 / 28-39 / 47-58, holds leftover VLOOKUP formulas from this tab's
# ORIGINAL file (e.g. `=IFERROR(VLOOKUP(A3, $A$3:$E$13, 5, FALSE),"")`) that
# mirror each epic's own "T:" total back into column G for some original
# presentation purpose - this workflow never writes to column G at all, so
# they linger untouched forever. Confirmed stale: they're hardcoded to a
# ~11-row range ($A$3:$E$13 etc.) from when this tab tracked far fewer
# epics than the ~19 it has now, so rows beyond that range have no formula
# at all and the ones that DO exist can reference the wrong epic once
# today's live epic list reorders Table1/Table2's rows.
LEGACY_COLUMN_G_ROW_RANGES = [(3, 15), (28, 39), (47, 58)]


def _col_letter(col0: int) -> str:
    col0 += 1
    letters = ""
    while col0 > 0:
        col0, rem = divmod(col0 - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _a1(row1: int, col0: int) -> str:
    return f"{_col_letter(col0)}{row1}"


def _display_date(run_date: str) -> str:
    return datetime.strptime(run_date, "%Y-%m-%d").strftime("%d.%m.%Y")


def _extract_us_code(summary: Optional[str]) -> Optional[str]:
    if not summary:
        return None
    m = _US_CODE_PREFIX_RE.match(summary)
    return m.group(1) if m else None


def _extract_us_code_anywhere(summary: Optional[str]) -> Optional[str]:
    if not summary:
        return None
    m = _US_CODE_ANYWHERE_RE.search(summary)
    return m.group(1) if m else None


def _build_us_code_epic_map(ideas: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for idea in ideas:
        fields = idea.get("fields") or {}
        code = _extract_us_code_anywhere(fields.get("summary"))
        epic_field = fields.get(IDEAS_EPIC_FIELD_ID)
        epic_name = epic_field.get("value") if isinstance(epic_field, dict) else None
        if code and epic_name:
            mapping[code] = epic_name
    return mapping


def _build_epic_key_map(epics: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for epic in epics:
        summary = (epic.get("fields") or {}).get("summary")
        if summary and epic.get("key"):
            mapping[summary.strip()] = epic["key"]
    return mapping


def _resolve_epic_via_us_code(code: str, us_map: dict[str, str]) -> Optional[str]:
    parts = code.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in us_map:
            return us_map[candidate]
    return None


def _resolve_epic(issue: dict[str, Any], us_map: dict[str, str]) -> str:
    fields = issue.get("fields") or {}

    parent = fields.get("parent")
    if parent:
        summary = ((parent.get("fields") or {}).get("summary") or "").strip()
        if summary:
            return summary

    own_code = _extract_us_code(fields.get("summary"))
    if own_code:
        epic = _resolve_epic_via_us_code(own_code, us_map)
        if epic:
            return epic

    for link in fields.get("issuelinks") or []:
        for side in ("outwardIssue", "inwardIssue"):
            linked = link.get(side)
            if not linked:
                continue
            linked_code = _extract_us_code_anywhere((linked.get("fields") or {}).get("summary"))
            if linked_code:
                epic = _resolve_epic_via_us_code(linked_code, us_map)
                if epic:
                    return epic

    return NO_EPIC_LABEL


def _is_open(issue: dict[str, Any]) -> bool:
    status = ((issue.get("fields") or {}).get("status") or {}).get("name") or ""
    return status not in CLOSED_STATUSES


def _subtype_name(issue: dict[str, Any]) -> str:
    name = ((issue.get("fields") or {}).get("issuetype") or {}).get("name") or ""
    return SUBTYPE_ALIAS.get(name, name)


def _epic_sort_key(epic: str) -> tuple[Any, ...]:
    m = re.match(r"\[E(\d+)\]", epic)
    if m:
        return (0, int(m.group(1)))
    return (1 if epic != NO_EPIC_LABEL else 2, epic)


def _aggregate(issues: list[dict[str, Any]], us_map: dict[str, str]) -> dict[str, Any]:
    total: dict[str, dict[str, int]] = {}
    opened: dict[str, dict[str, int]] = {}

    for issue in issues:
        epic = _resolve_epic(issue, us_map)
        subtype = _subtype_name(issue)
        total.setdefault(epic, {})
        total[epic][subtype] = total[epic].get(subtype, 0) + 1
        if _is_open(issue):
            opened.setdefault(epic, {})
            opened[epic][subtype] = opened[epic].get(subtype, 0) + 1

    epics = sorted(total.keys(), key=_epic_sort_key)
    return {"epics": epics, "total": total, "opened": opened}


class _LinkBuilder:
    """Builds `/issues/?jql=...` Jira search URLs (epic + sub-type + open
    filter) - simplified vs. this tab's real historical links, which also
    filter on a "Phasa" cascading custom field per explicit user decision
    (that field's meaning wasn't understood well enough to replicate
    correctly). Uses `resolution = Unresolved` for "open" rather than the
    original's locale-sensitive `status not in (PASSED, Cancelled)`, same
    reasoning as uat_bugs_report."""

    def __init__(self, base_jql: str, jira_base_url: str, epic_key_map: dict[str, str]):
        self._base = _ORDER_BY_RE.sub("", base_jql).strip()
        self._jira_base_url = jira_base_url.rstrip("/")
        self._epic_key_map = epic_key_map

    def url(
        self,
        *,
        epic: Optional[str] = None,
        open_only: bool = False,
        subtype: Optional[str] = None,
    ) -> Optional[str]:
        clauses = [self._base]
        if epic is not None:
            key = self._epic_key_map.get(epic)
            if not key:
                return None
            clauses.append(f"parent = {key}")
        if open_only:
            clauses.append(OPEN_JQL_CLAUSE)
        if subtype is not None:
            clauses.append(f'issuetype = "{subtype}"')
        jql = " AND ".join(clauses) + " ORDER BY parent ASC"
        return f"{self._jira_base_url}/issues/?jql={quote_plus(jql)}"


def _snapshot_grid(
    title: str, epics: list[str], counts: dict[str, dict[str, int]], open_only: bool, links: _LinkBuilder
) -> list[list[tuple[Any, Optional[str]]]]:
    header: list[tuple[Any, Optional[str]]] = [("Parent", None)] + [(s, None) for s in SUBTYPE_COLUMNS] + [("T:", None)]
    grid: list[list[tuple[Any, Optional[str]]]] = [[(title, None)], header]
    for epic in epics:
        row: list[tuple[Any, Optional[str]]] = [(epic, None)]
        row_total = 0
        for subtype in SUBTYPE_COLUMNS:
            v = counts.get(epic, {}).get(subtype, 0)
            row_total += v
            row.append((v, links.url(epic=epic, open_only=open_only, subtype=subtype)))
        row.append((row_total, links.url(epic=epic, open_only=open_only)))
        grid.append(row)
    total_row: list[tuple[Any, Optional[str]]] = [("Total Unique Issues:", None)]
    grand_total = 0
    for subtype in SUBTYPE_COLUMNS:
        subtype_total = sum(counts.get(e, {}).get(subtype, 0) for e in epics)
        grand_total += subtype_total
        total_row.append((subtype_total, links.url(open_only=open_only, subtype=subtype)))
    total_row.append((grand_total, links.url(open_only=open_only)))
    grid.append(total_row)
    return grid


def _comparison_grid(
    epics: list[str], agg: dict[str, Any], links: _LinkBuilder
) -> list[list[tuple[Any, Optional[str]]]]:
    header: list[tuple[Any, Optional[str]]] = [("Parent", None)]
    for subtype in SUBTYPE_COLUMNS:
        header.append((f"Total {subtype}", None))
        header.append((f"Opened {subtype}", None))
    header.append(("Total T:", None))
    header.append(("Opened T:", None))
    grid: list[list[tuple[Any, Optional[str]]]] = [
        [("Сравнение по эпикам: всего и открытых багов (по типу бага)", None)],
        header,
    ]
    for epic in epics:
        row: list[tuple[Any, Optional[str]]] = [(epic, None)]
        grand_total = grand_opened = 0
        for subtype in SUBTYPE_COLUMNS:
            t = agg["total"].get(epic, {}).get(subtype, 0)
            o = agg["opened"].get(epic, {}).get(subtype, 0)
            grand_total += t
            grand_opened += o
            row.append((t, links.url(epic=epic, subtype=subtype)))
            row.append((o, links.url(epic=epic, open_only=True, subtype=subtype)))
        row.append((grand_total, links.url(epic=epic)))
        row.append((grand_opened, links.url(epic=epic, open_only=True)))
        grid.append(row)
    total_row: list[tuple[Any, Optional[str]]] = [("Total Unique Issues:", None)]
    gt = go = 0
    for subtype in SUBTYPE_COLUMNS:
        st = sum(agg["total"].get(e, {}).get(subtype, 0) for e in epics)
        so = sum(agg["opened"].get(e, {}).get(subtype, 0) for e in epics)
        gt += st
        go += so
        total_row.append((st, links.url(subtype=subtype)))
        total_row.append((so, links.url(open_only=True, subtype=subtype)))
    total_row.append((gt, links.url()))
    total_row.append((go, links.url(open_only=True)))
    grid.append(total_row)
    return grid


def _merge_timeseries(
    existing_header: list[str],
    existing_rows: list[list[Any]],
    display_date: str,
    epics_today: list[str],
    counts_today: dict[str, int],
) -> tuple[list[str], list[str], dict[str, dict[str, int]]]:
    """Returns (ordered_epics, ordered_dates, data[epic][date] = count).
    Append-order, not a chronological re-sort - a re-run for today's date
    updates that column in place (same physical position), a brand-new date
    is always added at the true right edge. Identical approach to
    uat_bugs_report._merge_timeseries."""
    data: dict[str, dict[str, int]] = {}
    epic_row_order: list[str] = []
    dates: list[str] = list(existing_header[1:]) if len(existing_header) > 1 else []

    for row in existing_rows:
        if not row or not row[0] or row[0] == "Total Unique Issues:":
            continue
        epic = row[0]
        epic_row_order.append(epic)
        data[epic] = {}
        for i, d in enumerate(dates):
            value = row[i + 1] if i + 1 < len(row) else 0
            try:
                data[epic][d] = int(value) if value not in ("", None) else 0
            except (TypeError, ValueError):
                data[epic][d] = 0

    for epic in epics_today:
        if epic not in data:
            data[epic] = {}
            epic_row_order.append(epic)

    if display_date not in dates:
        dates.append(display_date)
    for epic in epic_row_order:
        data[epic][display_date] = counts_today.get(epic, 0)

    epic_row_order.sort(key=_epic_sort_key)
    return epic_row_order, dates, data


def _timeseries_grid(
    title: str, epics: list[str], dates: list[str], data: dict[str, dict[str, int]], links: _LinkBuilder, subtype: Optional[str]
) -> list[list[tuple[Any, Optional[str]]]]:
    grid: list[list[tuple[Any, Optional[str]]]] = [
        [(title, None)],
        [("Parent", None)] + [(d, None) for d in dates],
    ]
    for epic in epics:
        link = links.url(epic=epic, open_only=True, subtype=subtype)
        grid.append([(epic, None)] + [(data.get(epic, {}).get(d, 0), link) for d in dates])
    total_link = links.url(open_only=True, subtype=subtype)
    grid.append(
        [("Total Unique Issues:", None)]
        + [(sum(data.get(e, {}).get(d, 0) for e in epics), total_link) for d in dates]
    )
    return grid


def _read_existing_table(
    sheets_client, sheet_title: str, row1: int, col0: int, title: str, row_budget: int, col_budget: int,
) -> Optional[tuple[list[Any], list[list[Any]]]]:
    """Read a growing table's header+data+total at its own FIXED position
    (row1, col0 - 0-indexed column, 1-indexed row) - IF a cell exactly
    matching `title` actually sits there; returns None otherwise (nothing
    to continue - first run, or this exact table was never written here
    yet). The read deliberately stops right after the row where column 0
    reads "Total Unique Issues:" - never reads a row further - so it can't
    bleed into a neighbouring table's own rows even if the two are packed
    tightly together in the same column."""
    col_letter = _col_letter(col0)
    last_col_letter = _col_letter(col0 + col_budget)
    raw = sheets_client.read_values(sheet_title, f"{col_letter}{row1}:{last_col_letter}{row1 + row_budget - 1}")
    if not raw or not raw[0] or (raw[0][0] or "").strip() != title:
        return None
    header = raw[1] if len(raw) > 1 else []
    data_and_total: list[list[Any]] = []
    for row in raw[2:]:
        data_and_total.append(row)
        if row and row[0] == "Total Unique Issues:":
            break
    return header, data_and_total


async def _write_timeseries_table(
    sheets_client, sheet_id: int, sheet_title: str, title: str,
    row1: int, col0: int,
    display_date: str, epics_today: list[str], counts_today: dict[str, int], links: _LinkBuilder, subtype: Optional[str],
    row_budget: int = TS_ROW_BUDGET,
) -> list[str]:
    """Shared read-merge-write-chart logic for the four growing tables
    ("Opened Bugs Total" / "Opened Mobile bug" / "Opened BE BUG" / "Opened
    FE bug") - each at its own FIXED (row1, col0), never migrated. Only the
    per-epic counts and the sub-type JQL filter differ between them.
    `row_budget` defaults to the generous TS_ROW_BUDGET but "Opened Bugs
    Total" passes the tighter TS_TOTAL_ROW_BUDGET instead (it shares column
    H with Mobile's legacy adoption position - see TS_TOTAL_ROW_BUDGET).

    One-time-only exception: if nothing is found at the fixed position, this
    also checks wherever this table's real history might still be sitting
    from BEFORE its current fixed position (see LEGACY_POSITION_BY_TITLE) -
    if found there, it's adopted here (written forward at the fixed
    position, same as every other run from now on) and the old slot is
    cleared so it doesn't linger as an abandoned-looking duplicate. Once a
    table exists at its fixed position, this fallback is never consulted
    again for it."""
    existing = _read_existing_table(sheets_client, sheet_title, row1, col0, title, row_budget, TS_COL_BUDGET)
    legacy_to_clear: Optional[tuple[int, int, int, int]] = None
    if existing is None and title in LEGACY_POSITION_BY_TITLE:
        legacy_col0, legacy_row1, legacy_row_budget, legacy_col_budget = LEGACY_POSITION_BY_TITLE[title]
        legacy_existing = _read_existing_table(sheets_client, sheet_title, legacy_row1, legacy_col0, title, legacy_row_budget, legacy_col_budget)
        if legacy_existing is not None:
            existing = legacy_existing
            legacy_to_clear = (legacy_col0, legacy_row1, legacy_row_budget, legacy_col_budget)
    existing_header, existing_rows = existing if existing is not None else ([], [])
    ts_epics, ts_dates, ts_data = _merge_timeseries(existing_header, existing_rows, display_date, epics_today, counts_today)

    first_col_letter = _col_letter(col0)
    last_col_letter = _col_letter(col0 + TS_COL_BUDGET)
    sheets_client.clear_values(sheet_title, f"{first_col_letter}{row1}:{last_col_letter}{row1 + row_budget - 1}")
    sheets_client.unmerge_row_band(sheet_id, row1 - 1, 1)
    grid = _timeseries_grid(title, ts_epics, ts_dates, ts_data, links, subtype)
    sheets_client.write_cells_with_links(sheet_id, row1 - 1, col0, grid, _row_styles(len(ts_epics)))
    sheets_client.merge_cells(sheet_id, row1 - 1, col0, 1, 1 + len(ts_dates))

    header_row0 = row1  # title row is row1; header row (1-indexed) = row1+1 -> 0-indexed row1
    spec = sheets_client.row_series_chart_spec(
        "LINE", sheet_id,
        header_row_index=header_row0,
        first_data_row_index=header_row0 + 1,
        num_series_rows=len(ts_epics) + 1,  # + the "Total Unique Issues:" row
        label_col_index=col0,
        num_data_cols=len(ts_dates),
        x_title="period", y_title="Open Issues Count",
    )
    chart_title = {
        None: "Opened Bugs Total",
        "Mobile bug": "Динамика наличия открытых Mobile багов по фичам",
        "BE BUG": "Динамика наличия открытых BE багов по фичам",
        "FE bug": "Динамика наличия открытых FE багов по фичам",
    }[subtype]
    sheets_client.upsert_chart(
        sheet_id, chart_title, spec,
        anchor_row=row1 + row_budget + 2, anchor_col=col0,
    )

    if legacy_to_clear is not None:
        # One-time adoption: the old slot's history has just been copied
        # forward to the fixed position above - clear the now-redundant old
        # copy's VALUES and FORMATTING so it doesn't linger as an
        # abandoned-looking, still-highlighted duplicate (`clear_values`
        # alone only clears values, never the title/header background fill
        # - see `clear_formatting`).
        legacy_col0, legacy_row1, legacy_row_budget, legacy_col_budget = legacy_to_clear
        legacy_first_col = _col_letter(legacy_col0)
        legacy_last_col = _col_letter(legacy_col0 + legacy_col_budget)
        sheets_client.clear_values(sheet_title, f"{legacy_first_col}{legacy_row1}:{legacy_last_col}{legacy_row1 + legacy_row_budget - 1}")
        sheets_client.clear_formatting(sheet_id, legacy_row1 - 1, legacy_row_budget, legacy_col0, legacy_col_budget + 1)
        sheets_client.unmerge_row_band(sheet_id, legacy_row1 - 1, 1)

    return ts_dates


async def run_bugs_per_epics_report_workflow(
    jira_client,
    sheets_client,
    *,
    jql: str = DEFAULT_JQL,
    max_results: int = 2000,
    sheet_title: str = SHEET_TITLE,
    run_date: Optional[str] = None,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Fetch all project bugs (BE BUG/Bug/Mobile bug/FE bug), resolve each
    one's Epic, and (re)write all seven report tables + their charts into
    `sheet_title`, each at its own FIXED position (see module docstring) -
    never migrated, never repositioned. Only clears/rewrites each table's
    own fixed, generously budgeted range; the four growing tables also
    append one new date column via `_merge_timeseries`."""

    def _log(level: str, msg: str) -> None:
        logger.info(msg) if level == "INFO" else logger.warning(msg) if level == "WARNING" else logger.debug(msg)
        if log_fn:
            log_fn(level, msg)

    run_date = run_date or date.today().isoformat()
    try:
        display_date = _display_date(run_date)
    except ValueError:
        return {"status": "failed", "error": f"run_date must be YYYY-MM-DD, got {run_date!r}"}

    _log("INFO", f"Bugs per Epics report started: jql={jql!r}, run_date={run_date}")

    if should_cancel_fn and should_cancel_fn():
        _log("WARNING", "Остановлено пользователем")
        return {"status": "canceled", "error": "Остановлено пользователем"}

    try:
        issues = await jira_client.fetch_bugs(jql, max_results=max_results, fields=BUGS_FIELDS)
        ideas = await jira_client.fetch_bugs(IDEAS_JQL, max_results=1000, fields=IDEAS_FIELDS)
        epics_raw = await jira_client.fetch_bugs(EPICS_JQL, max_results=500, fields=EPICS_FIELDS)
    except Exception as exc:
        _log("ERROR", f"Failed to fetch data from Jira: {exc}")
        return {"status": "failed", "error": str(exc)}

    before_exclusion = len(issues)
    issues = [i for i in issues if EXCLUDE_SUMMARY_MARKER not in ((i.get("fields") or {}).get("summary") or "")]
    excluded_count = before_exclusion - len(issues)

    _log(
        "INFO",
        f"Fetched {before_exclusion} bug(s) ({excluded_count} excluded as '{EXCLUDE_SUMMARY_MARKER}'), "
        f"{len(ideas)} MB20PB idea(s), {len(epics_raw)} epic(s)",
    )

    if should_cancel_fn and should_cancel_fn():
        _log("WARNING", "Остановлено пользователем")
        return {"status": "canceled", "error": "Остановлено пользователем"}

    us_map = _build_us_code_epic_map(ideas)
    epic_key_map = _build_epic_key_map(epics_raw)
    agg = _aggregate(issues, us_map)
    epics = agg["epics"]

    jira_base_url = str(getattr(jira_client, "base_url", "") or "")
    links = _LinkBuilder(jql, jira_base_url, epic_key_map)

    opened_total_today = {e: sum(agg["opened"].get(e, {}).values()) for e in epics}
    opened_mobile_today = {e: agg["opened"].get(e, {}).get("Mobile bug", 0) for e in epics}
    opened_be_today = {e: agg["opened"].get(e, {}).get("BE BUG", 0) for e in epics}
    opened_fe_today = {e: agg["opened"].get(e, {}).get("FE bug", 0) for e in epics}

    try:
        sheet_id = sheets_client.ensure_sheet(sheet_title)
        sheets_client.ensure_grid_size(
            sheet_id,
            min_rows=max(TABLE3_START, TS_FE_START) + ROW_BUDGET + 50,
            min_cols=TS_COL0 + TS_COL_BUDGET + 10,
        )
        snap_col_letter = _col_letter(SNAPSHOT_COL0)

        # One-time cleanup: this tab's own ORIGINAL "Сравнение по эпикам"
        # position (row 66, 3 sub-types) - see LEGACY_TABLE3_TITLE_ROW for
        # why this lingers forever otherwise (TABLE3_START's own clear/write
        # never reaches back up to row 66). Must run BEFORE Table2's own
        # write below - Table2's generous 35-row budget (rows 41-76)
        # overlaps part of this legacy block and silently clears ITS title
        # cell's TEXT (not its formatting) as a side effect, which would
        # make this exact title-text check below find nothing on the very
        # next run otherwise.
        legacy_table3_title = sheets_client.read_values(
            sheet_title, f"{snap_col_letter}{LEGACY_TABLE3_TITLE_ROW}:{snap_col_letter}{LEGACY_TABLE3_TITLE_ROW}"
        )
        if legacy_table3_title and legacy_table3_title[0] and (legacy_table3_title[0][0] or "").strip() == "Сравнение по эпикам: всего и открытых багов (по типу бага)":
            legacy_last_col = _col_letter(SNAPSHOT_COL0 + LEGACY_TABLE3_COL_BUDGET - 1)
            sheets_client.clear_values(
                sheet_title,
                f"{snap_col_letter}{LEGACY_TABLE3_CLEAR_FROM_ROW}:{legacy_last_col}{LEGACY_TABLE3_CLEAR_FROM_ROW + LEGACY_TABLE3_CLEAR_ROWS - 1}",
            )
            sheets_client.clear_formatting(
                sheet_id, LEGACY_TABLE3_CLEAR_FROM_ROW - 1, LEGACY_TABLE3_CLEAR_ROWS, SNAPSHOT_COL0, LEGACY_TABLE3_COL_BUDGET
            )
            sheets_client.unmerge_row_band(sheet_id, LEGACY_TABLE3_TITLE_ROW - 1, 1)

        # One-time cleanup, per explicit user decision: column G's leftover
        # VLOOKUP formulas (see LEGACY_COLUMN_G_ROW_RANGES) - checked via a
        # marker read on the first range's own first cell so this is a
        # no-op on every run after the first. Deliberately narrow, exact
        # row ranges (not a wide sweep) - column G also holds this run's OWN
        # legitimate "Opened Mobile bug" data from the new Comparison table
        # (rows 82+, within TABLE3_START's own range) and must not be
        # touched.
        g_col_letter = _col_letter(SNAPSHOT_COL0 + 6)
        legacy_g_marker = sheets_client.read_values(sheet_title, f"{g_col_letter}{LEGACY_COLUMN_G_ROW_RANGES[0][0]}:{g_col_letter}{LEGACY_COLUMN_G_ROW_RANGES[0][0]}")
        if legacy_g_marker and legacy_g_marker[0] and legacy_g_marker[0][0] not in (None, ""):
            for g_row_start, g_row_end in LEGACY_COLUMN_G_ROW_RANGES:
                sheets_client.clear_values(sheet_title, f"{g_col_letter}{g_row_start}:{g_col_letter}{g_row_end}")
                sheets_client.clear_formatting(sheet_id, g_row_start - 1, g_row_end - g_row_start + 1, SNAPSHOT_COL0 + 6, 1)

        # Table 1: Opened Bugs (current status) - fixed position, never moves
        sheets_client.clear_values(
            sheet_title, f"{snap_col_letter}{TABLE1_START}:{_col_letter(SNAPSHOT_COL0 + len(SUBTYPE_COLUMNS) + 1)}{TABLE1_START + ROW_BUDGET}"
        )
        sheets_client.unmerge_row_band(sheet_id, TABLE1_START - 1, 1)
        t1_grid = _snapshot_grid("Opened Bugs (current status)", epics, agg["opened"], True, links)
        sheets_client.write_cells_with_links(sheet_id, TABLE1_START - 1, SNAPSHOT_COL0, t1_grid, _row_styles(len(epics)))
        sheets_client.merge_cells(sheet_id, TABLE1_START - 1, SNAPSHOT_COL0, 1, len(SUBTYPE_COLUMNS) + 2)
        t1_spec = sheets_client.column_series_chart_spec(
            "COLUMN", sheet_id,
            header_row_index=TABLE1_START,
            num_rows_incl_header=len(t1_grid) - 2,  # header + epic rows, excluding title AND total rows
            first_col_index=SNAPSHOT_COL0,
            num_series_cols=len(SUBTYPE_COLUMNS),
            x_title="Epic", y_title="Number of Bugs",
        )
        sheets_client.upsert_chart(
            sheet_id, "Bug Status by Feature (BE vs Mobile)", t1_spec,
            anchor_row=TABLE1_START + ROW_BUDGET + 2, anchor_col=SNAPSHOT_COL0,
        )

        # Table 2: All Bugs (current status) - fixed position, never moves
        sheets_client.clear_values(
            sheet_title, f"{snap_col_letter}{TABLE2_START}:{_col_letter(SNAPSHOT_COL0 + len(SUBTYPE_COLUMNS) + 1)}{TABLE2_START + ROW_BUDGET}"
        )
        sheets_client.unmerge_row_band(sheet_id, TABLE2_START - 1, 1)
        t2_grid = _snapshot_grid("All Bugs (current status)", epics, agg["total"], False, links)
        sheets_client.write_cells_with_links(sheet_id, TABLE2_START - 1, SNAPSHOT_COL0, t2_grid, _row_styles(len(epics)))
        sheets_client.merge_cells(sheet_id, TABLE2_START - 1, SNAPSHOT_COL0, 1, len(SUBTYPE_COLUMNS) + 2)

        # Table 3: Сравнение по эпикам (+ COLUMN chart, excluding the grand-
        # total pair columns and the Total row - matches this tab's real
        # chart, confirmed via its actual spec before writing this) - fixed
        # position, never moves.
        comparison_num_cols = 1 + len(SUBTYPE_COLUMNS) * 2 + 2
        last_col = _col_letter(SNAPSHOT_COL0 + comparison_num_cols - 1)
        sheets_client.clear_values(sheet_title, f"{snap_col_letter}{TABLE3_START}:{last_col}{TABLE3_START + ROW_BUDGET}")
        sheets_client.unmerge_row_band(sheet_id, TABLE3_START - 1, 1)
        comparison_grid = _comparison_grid(epics, agg, links)
        sheets_client.write_cells_with_links(sheet_id, TABLE3_START - 1, SNAPSHOT_COL0, comparison_grid, _row_styles(len(epics)))
        sheets_client.merge_cells(sheet_id, TABLE3_START - 1, SNAPSHOT_COL0, 1, comparison_num_cols)
        t3_spec = sheets_client.column_series_chart_spec(
            "COLUMN", sheet_id,
            header_row_index=TABLE3_START,
            num_rows_incl_header=len(comparison_grid) - 2,  # header + epic rows, excluding title AND total rows
            first_col_index=SNAPSHOT_COL0,
            num_series_cols=len(SUBTYPE_COLUMNS) * 2,  # excludes the grand-total pair columns
            x_title="Epic", y_title="Bugs Count",
        )
        sheets_client.upsert_chart(
            sheet_id, "Сравнение по эпикам: всего и открытых багов (по типу бага)", t3_spec,
            anchor_row=TABLE3_START + ROW_BUDGET + 2, anchor_col=SNAPSHOT_COL0,
        )

        # Four growing time-series tables - each at its own FIXED position
        # (never migrated); a new date column is simply appended to
        # whatever's already there via _merge_timeseries.
        ts_total_dates = await _write_timeseries_table(
            sheets_client, sheet_id, sheet_title, "Opened Bugs Total",
            TS_TOTAL_START, TS_TOTAL_COL0,
            display_date, epics, opened_total_today, links, None,
            row_budget=TS_TOTAL_ROW_BUDGET,
        )
        await _write_timeseries_table(
            sheets_client, sheet_id, sheet_title, "Opened Mobile bug",
            TS_MOBILE_START, TS_COL0,
            display_date, epics, opened_mobile_today, links, "Mobile bug",
        )
        await _write_timeseries_table(
            sheets_client, sheet_id, sheet_title, "Opened BE BUG",
            TS_BE_START, TS_COL0,
            display_date, epics, opened_be_today, links, "BE BUG",
        )
        await _write_timeseries_table(
            sheets_client, sheet_id, sheet_title, "Opened FE bug",
            TS_FE_START, TS_COL0,
            display_date, epics, opened_fe_today, links, "FE bug",
        )
    except Exception as exc:
        _log("ERROR", f"Failed to update Google Sheet: {exc}")
        return {"status": "failed", "error": str(exc)}

    _log(
        "INFO",
        f"Bugs per Epics report complete: {len(issues)} bug(s) across {len(epics)} epic(s), "
        f"date '{display_date}' in all three time series",
    )

    return {
        "status": "succeeded",
        "summary": {
            "jql": jql,
            "bugs_fetched": len(issues),
            "run_date": run_date,
            "display_date": display_date,
            "epics": epics,
            "timeseries_dates": ts_total_dates,
        },
        "correlation_id": correlation_id,
    }


def render_text_report(result: dict[str, Any]) -> str:
    """Render the report result as a human-readable text summary."""
    if result.get("status") != "succeeded":
        return f"Отчёт не сформирован: {result.get('error', 'неизвестная ошибка')}"

    summary = result.get("summary", {})
    lines: list[str] = []
    lines.append("ОТЧЁТ ПО БАГАМ (Bugs per Epics)")
    lines.append(f"JQL: {summary.get('jql', '')}")
    lines.append(f"Дата снэпшота: {summary.get('display_date', '')}")
    lines.append(f"Всего багов: {summary.get('bugs_fetched', 0)}")
    lines.append(f"Эпиков: {len(summary.get('epics', []))}")
    lines.append(f"Снэпшотов в таблицах динамики: {len(summary.get('timeseries_dates', []))}")

    return "\n".join(lines)


__all__ = [
    "DEFAULT_JQL",
    "IDEAS_JQL",
    "EPICS_JQL",
    "SHEET_TITLE",
    "NO_EPIC_LABEL",
    "run_bugs_per_epics_report_workflow",
    "render_text_report",
]
