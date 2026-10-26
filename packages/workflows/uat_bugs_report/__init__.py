"""UAT bugs report for Google Sheets - epic breakdown + severity trends.

Fetches bugs matched by a JQL query (all MB project bugs by default - this
project's "Bug" issuetype *is* the UAT-bug category, as distinct from "BE BUG"
and "Mobile bug"; confirmed against the existing "Bugs per Epics" tab's own
"Total Bug (UAT)" column), resolves each bug's Epic, and writes four tables
into the "UAT BUGs" sheet tab, mirroring this project's pre-existing "Bugs per
Epics" tab byte-for-byte in naming/layout/hyperlink convention (verified
against its real cell contents and chart specs via the original .xlsx, not
guessed - see packages/integrations/google_sheets and the project memory):

- "Opened Bugs (current status)" - Epic x open-bug count, snapshot (no
  chart, same as the analogous table on Bugs per Epics).
- "All Bugs (current status)" - Epic x total-bug count, snapshot.
- "Сравнение по эпикам: всего и открытых багов (по типу бага)" - Epic x
  Total/Opened pairs per Severity level, with a COLUMN chart - this exact
  title already exists on Bugs per Epics, there pairing Total/Opened per bug
  *sub-type* (BE BUG/Bug(UAT)/Mobile bug); UAT bugs don't have a sub-type
  split of their own, so this report pairs by Severity instead (per explicit
  user confirmation) while keeping the original title text.
- "Opened Bugs Total" - Epic x run-date, growing by one column per run (not
  overwritten), with a LINE chart - the per-run "report for that date".
  Dates start from the first automated run.

All four tables share one header row near the top of the sheet, side by side
left to right in column AF onward: "All Bugs" -> "Opened Bugs" -> "Сравнение
по эпикам..." -> "Opened Bugs Total" (rightmost, since it's the only one that
grows wider every run) - close enough to the left edge to reach with a short
scroll, while still never colliding with the pre-automation legacy tables
(column A / column Q-AE) no matter how far either one grows downward - see
BLOCK_B_COL0/BLOCK_A_COL0/BLOCK_C_COL0/TIMESERIES_COL0.

Every data cell in all four tables is a `HYPERLINK(...)` formula pointing at
the matching live Jira search (epic + open/severity filter), exactly like
every number in the original Bugs per Epics/UAT BUGs tables - confirmed by
inspecting the original .xlsx's cell hyperlinks directly, not assumed.

Epic resolution, in priority order (verified against real MB bugs before
coding, not assumed):
1. `fields.parent.fields.summary` - correct for the large majority of bugs.
2. The bug's own summary carries a "US-X.Y[.Z...]" prefix - resolved against
   the Jira Product Discovery "MB20PB" project ("Idea" issues, field
   customfield_10168 holds the full epic label, e.g. "[E2]: Settings").
3. A linked issue (any issuelinks direction) whose own summary carries a
   "US-X.Y[.Z...]" prefix - resolved the same way via MB20PB (covers bugs
   reported without a direct Epic link but linked to a "UAT test execution"
   task that itself names the originating User Story).
4. Otherwise bucketed under `NO_EPIC_LABEL` (no Jira key -> no hyperlink).

Read-only on Jira; only writes to Google Sheets.
"""

import re
from datetime import date, datetime
from typing import Any, Callable, Optional
from urllib.parse import quote_plus

from packages.common import get_logger

logger = get_logger(__name__)

DEFAULT_JQL = "project = MB AND issuetype = Bug ORDER BY parent ASC"
IDEAS_JQL = "project = MB20PB"
EPICS_JQL = "project = MB AND issuetype = Epic"
IDEAS_EPIC_FIELD_ID = "customfield_10168"
SEVERITY_FIELD_ID = "customfield_10865"
SHEET_TITLE = "UAT BUGs"

BUGS_FIELDS = f"summary,status,resolution,parent,issuelinks,{SEVERITY_FIELD_ID}"
IDEAS_FIELDS = f"summary,{IDEAS_EPIC_FIELD_ID}"
EPICS_FIELDS = "summary"

NO_EPIC_LABEL = "(без эпика)"
NO_SEVERITY_LABEL = "(не указано)"
SEVERITY_ORDER = ["Critical", "Major", "Minor", NO_SEVERITY_LABEL]
OPEN_JQL_CLAUSE = "resolution = Unresolved"

# Template/example bugs some of this project's bugs are cloned from - never
# real UAT bugs, excluded from every table.
EXCLUDE_SUMMARY_MARKER = "[BUG EXAMPLE"

_DARK_TEXT = {"red": 0.16078432, "green": 0.16470589, "blue": 0.18039216}
_TITLE_BG = {"red": 0.42745098, "green": 0.61960787, "blue": 0.92156863}
_HEADER_BG = {"red": 0.7882353, "green": 0.85490197, "blue": 0.972549}
_WHITE_BG = {"red": 1, "green": 1, "blue": 1}
# FFFCE5CD, the original "period x status" table's header/latest-row
# highlight - confirmed by reading the real .xlsx's cell fill colors
# directly (openpyxl `cell.fill.fgColor.rgb`), not guessed.
_PEACH_BG = {"red": 0.9882353, "green": 0.8980392, "blue": 0.8078431}

# Cell-level formatting matching this project's own "Bugs per Epics" tab,
# read directly via the Sheets API before writing this (not guessed):
# a merged blue title banner, a light-blue bold centered header row, plain
# white data rows, and a light-blue bold "Total" row.
TITLE_STYLE = {"backgroundColor": _TITLE_BG, "horizontalAlignment": "CENTER", "textFormat": {"bold": True, "foregroundColor": _DARK_TEXT}}
HEADER_STYLE = {"backgroundColor": _HEADER_BG, "horizontalAlignment": "CENTER", "textFormat": {"bold": True, "foregroundColor": _DARK_TEXT}}
DATA_STYLE = {"backgroundColor": _WHITE_BG, "horizontalAlignment": "LEFT", "textFormat": {"bold": False, "foregroundColor": _DARK_TEXT}}
TOTAL_STYLE = {"backgroundColor": _HEADER_BG, "horizontalAlignment": "LEFT", "textFormat": {"bold": True, "foregroundColor": _DARK_TEXT}}

# "Parent - DATE" blocks (column Q) are plain in the original - no fill
# color, just a bold block-title/"Total" cell - unlike the new tables' blue-
# banner style above.
LEGACY_BLOCK_HEADER_STYLE = {"textFormat": {"bold": True, "foregroundColor": _DARK_TEXT}}
LEGACY_BLOCK_TOTAL_STYLE = {"textFormat": {"bold": True, "foregroundColor": _DARK_TEXT}}

# The "period x status" table (column A) DOES carry fill color in the
# original: the header row and whichever data row is the most-recently-added
# one are both peach (`_PEACH_BG`) + bold; every other data row is plain
# white, not bold. Re-applied explicitly on every write (not left to
# whatever a previous run happened to leave behind) since `updateCells`'
# fields mask resets any unset style property to its default - the earlier
# version of this code passed `style=None` for data rows, which silently
# wiped this highlight instead of "leaving it alone".
LEGACY_PERIOD_HEADER_STYLE = {"backgroundColor": _PEACH_BG, "textFormat": {"bold": True, "foregroundColor": _DARK_TEXT}}
LEGACY_PERIOD_LATEST_STYLE = {"backgroundColor": _PEACH_BG, "textFormat": {"bold": True, "foregroundColor": _DARK_TEXT}}
LEGACY_PERIOD_DATA_STYLE = {"backgroundColor": _WHITE_BG, "textFormat": {"bold": False, "foregroundColor": _DARK_TEXT}}


def _row_styles(num_epic_rows: int) -> list[dict[str, Any]]:
    """[title, header, data..., total] - matches grid shape: row0=title,
    row1=header, rows2..2+n-1=epics, last row=Total Unique Issues."""
    return [TITLE_STYLE, HEADER_STYLE] + [DATA_STYLE] * num_epic_rows + [TOTAL_STYLE]

_US_CODE_PREFIX_RE = re.compile(r"\s*(US-[\d.]+)\s*\|")
_US_CODE_ANYWHERE_RE = re.compile(r"(US-[\d.]+)")
_ORDER_BY_RE = re.compile(r"\s+ORDER BY\s+.*$", re.IGNORECASE)
_BLOCK_HEADER_RE = re.compile(r"^Parent\s*-\s*(.+)$")

# Row/column layout within the "UAT BUGs" tab. The legacy pre-automation
# content occupies two areas that both grow *downward* without bound (a new
# row per run for the period/status table in column A, a whole new
# "Parent - DATE" block every run in column Q, which in practice never uses
# more than ~15 columns, Q through AE). All four auto-generated tables sit in
# ONE shared header row (`NEW_SECTION_ROW0`), side by side left to right -
# far enough past column W that NEITHER the period table (column A) NOR the
# "Parent - DATE" blocks (column Q-AE) can ever collide with them at ANY row,
# no matter how far either one grows down, while staying close enough to the
# left edge to reach with a short horizontal scroll. Order (left to right),
# per explicit user request: "All Bugs" -> "Opened Bugs" -> "Сравнение по
# эпикам..." -> "Opened Bugs Total" LAST - that last one grows by one column
# per run (a new date), so it has to be the rightmost table or it would walk
# into its neighbor within a few days of runs; the others are fixed-width
# snapshots, refreshed in place every run, that never grow.
NEW_SECTION_ROW0 = 2  # 1-indexed row shared by all four tables

BLOCK_B_COL0 = 31  # column AF - "All Bugs (current status)" (2 cols: label + value), per explicit user-requested column
BLOCK_A_COL0 = 34  # column AI - "Opened Bugs (current status)" (2 cols), one column of gap after Block B
BLOCK_C_COL0 = 37  # column AL - "Сравнение по эпикам..." + its chart, one column of gap after Block A
COMPARISON_COL_BUDGET = 11  # fixed width reserved for Block C regardless of its actual (severity-count-dependent) width (currently 9 cols for 4 severities, ~2 cols of slack), so TIMESERIES_COL0 never has to move between runs
BLOCK_ROW_BUDGET = 30

TIMESERIES_COL0 = BLOCK_C_COL0 + COMPARISON_COL_BUDGET + 1  # column AX - "Opened Bugs Total", rightmost so its per-run growth never collides with a neighbor; column per explicit user request
TIMESERIES_ROW_START = NEW_SECTION_ROW0  # same shared row as the other three
TIMESERIES_ROW_BUDGET = 60
TIMESERIES_COL_BUDGET = 120

# Legacy pre-automation tables (column A / column Q) - now kept growing by
# this workflow too, per explicit user request, instead of staying frozen.
LEGACY_PERIOD_ROW_BUDGET = 1000  # generous cap for the "Issues status during time" chart's domain/series
# 0-indexed column Q - the "Parent - DATE" blocks. Was column O (14) until
# 2026-10-26, when a newly-added "UAT passed" status column grew the period
# table's own header (column A) wide enough that its "Total" column landed
# exactly on column O, silently overwriting several blocks' title/epic-label
# cells with period-table numbers. Column Q leaves a 2-column buffer (O, P)
# past the period table's current width. The workflow auto-migrates any
# existing block data that's still sitting at the old column to this one on
# its own (see `_auto_migrate_legacy_blocks` and its call site) - every
# subsequent run then detects the blocks already at LEGACY_BLOCK_COL0 and
# this is a no-op, per explicit user request ("следующие запуски уже не
# должны ничего сдвигать").
LEGACY_BLOCK_COL0 = 16
LEGACY_BLOCK_SCAN_ROWS = 9000  # how far down to scan for the last existing block before appending a new one
LEGACY_CHART_TITLE = "Issues status during time"

# This Jira instance renamed these statuses at some point (the English names
# no longer exist in the project's current workflow at all, confirmed via
# `GET /rest/api/3/project/MB/statuses`) - merge today's live (Russian) status
# name into the existing (English) legacy column instead of starting a
# disjoint new one.
STATUS_ALIAS = {"Отменено": "Cancelled", "К выполнению": "To Do"}


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
    """Strict: only a "US-X.Y | ..." prefix, as used by bugs' own summaries -
    a real tagging convention, not an incidental substring."""
    if not summary:
        return None
    m = _US_CODE_PREFIX_RE.match(summary)
    return m.group(1) if m else None


def _extract_us_code_anywhere(summary: Optional[str]) -> Optional[str]:
    """Loose: a "US-X.Y" substring anywhere - for scanning linked issues as a
    best-effort fallback, since linked tasks name the originating User Story
    with looser punctuation (e.g. "Исполнение UAT тестов US-2.1.8 (ex.US-5.2) |
    ...") rather than a clean leading tag; also used for MB20PB idea summaries
    ("[E2]: US-2.1.2.1 | ..." - code sits after the epic-code prefix)."""
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
    """Epic display-label -> Jira issue key (e.g. "[E2]: Settings" -> "MB-40"),
    used to build per-cell hyperlinks - fetched fresh each run rather than
    inferred only from fetched bugs' own `parent`, so an epic with zero
    *currently fetched* bugs still resolves correctly if it ever needs to."""
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
    return not ((issue.get("fields") or {}).get("resolution"))


def _severity(issue: dict[str, Any]) -> str:
    sev = (issue.get("fields") or {}).get(SEVERITY_FIELD_ID)
    if isinstance(sev, dict):
        return sev.get("value") or NO_SEVERITY_LABEL
    return sev or NO_SEVERITY_LABEL


def _legacy_status_name(issue: dict[str, Any]) -> str:
    name = ((issue.get("fields") or {}).get("status") or {}).get("name") or ""
    return STATUS_ALIAS.get(name, name)


def _epic_sort_key(epic: str) -> tuple[Any, ...]:
    m = re.match(r"\[E(\d+)\]", epic)
    if m:
        return (0, int(m.group(1)))
    return (1 if epic != NO_EPIC_LABEL else 2, epic)


def _aggregate(issues: list[dict[str, Any]], us_map: dict[str, str]) -> dict[str, Any]:
    total: dict[str, int] = {}
    opened: dict[str, int] = {}
    severity_total: dict[str, dict[str, int]] = {}
    severity_open: dict[str, dict[str, int]] = {}
    status_by_epic: dict[str, dict[str, int]] = {}
    status_totals: dict[str, int] = {}

    for issue in issues:
        epic = _resolve_epic(issue, us_map)
        total[epic] = total.get(epic, 0) + 1
        severity_total.setdefault(epic, {})
        sev = _severity(issue)
        severity_total[epic][sev] = severity_total[epic].get(sev, 0) + 1
        if _is_open(issue):
            opened[epic] = opened.get(epic, 0) + 1
            severity_open.setdefault(epic, {})
            severity_open[epic][sev] = severity_open[epic].get(sev, 0) + 1

        status = _legacy_status_name(issue)
        status_by_epic.setdefault(epic, {})
        status_by_epic[epic][status] = status_by_epic[epic].get(status, 0) + 1
        status_totals[status] = status_totals.get(status, 0) + 1

    epics = sorted(total.keys(), key=_epic_sort_key)
    return {
        "epics": epics,
        "total": total,
        "opened": opened,
        "severity_total": severity_total,
        "severity_open": severity_open,
        "status_by_epic": status_by_epic,
        "status_totals": status_totals,
    }


def _severity_columns(agg: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    for sevs in agg["severity_total"].values():
        seen.update(sevs.keys())
    ordered = [s for s in SEVERITY_ORDER if s in seen]
    extra = sorted(s for s in seen if s not in SEVERITY_ORDER)
    return ordered + extra


class _LinkBuilder:
    """Builds `/issues/?jql=...` Jira search URLs matching the configured base
    JQL, exactly mirroring the per-cell hyperlink convention found in the
    original .xlsx (epic + status/severity filter) - except using
    `resolution = Unresolved` for "open" rather than the original's literal
    `status not in (PASSED, Cancelled)`, since JQL status-name matching is
    locale-sensitive (this Jira instance's live statuses are partly in
    Russian, e.g. "Отменено") while `resolution` is not."""

    def __init__(self, base_jql: str, jira_base_url: str, epic_key_map: dict[str, str]):
        self._base = _ORDER_BY_RE.sub("", base_jql).strip()
        self._jira_base_url = jira_base_url.rstrip("/")
        self._epic_key_map = epic_key_map

    def url(
        self,
        *,
        epic: Optional[str] = None,
        open_only: bool = False,
        severity: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[str]:
        clauses = [self._base]
        if epic is not None:
            key = self._epic_key_map.get(epic)
            if not key:
                return None
            clauses.append(f"parent = {key}")
        if open_only:
            clauses.append(OPEN_JQL_CLAUSE)
        if severity is not None:
            clauses.append(f'cf[{SEVERITY_FIELD_ID.removeprefix("customfield_")}] is EMPTY' if severity == NO_SEVERITY_LABEL else f'cf[{SEVERITY_FIELD_ID.removeprefix("customfield_")}] = "{severity}"')
        if status is not None:
            # `status` may carry the legacy sheet column's exact (occasionally
            # whitespace-quirky, e.g. " Code Review") text - strip it so the
            # JQL always matches Jira's real status name.
            clauses.append(f'status = "{status.strip()}"')
        jql = " AND ".join(clauses) + " ORDER BY parent ASC"
        return f"{self._jira_base_url}/issues/?jql={quote_plus(jql)}"


def _snapshot_grid(
    title: str, value_label: str, epics: list[str], counts: dict[str, int], open_only: bool, links: _LinkBuilder
) -> list[list[tuple[Any, Optional[str]]]]:
    grid: list[list[tuple[Any, Optional[str]]]] = [[(title, None)], [("Parent", None), (value_label, None)]]
    for epic in epics:
        grid.append([(epic, None), (counts.get(epic, 0), links.url(epic=epic, open_only=open_only))])
    grid.append([("Total Unique Issues:", None), (sum(counts.get(e, 0) for e in epics), links.url(open_only=open_only))])
    return grid


def _comparison_grid(
    epics: list[str], severity_cols: list[str], agg: dict[str, Any], links: _LinkBuilder
) -> list[list[tuple[Any, Optional[str]]]]:
    header: list[tuple[Any, Optional[str]]] = [("Parent", None)]
    for sev in severity_cols:
        header.append((f"Total {sev}", None))
        header.append((f"Opened {sev}", None))
    grid: list[list[tuple[Any, Optional[str]]]] = [
        [("Сравнение по эпикам: всего и открытых багов (по типу бага)", None)],
        header,
    ]
    for epic in epics:
        row: list[tuple[Any, Optional[str]]] = [(epic, None)]
        for sev in severity_cols:
            row.append((agg["severity_total"].get(epic, {}).get(sev, 0), links.url(epic=epic, severity=sev)))
            row.append((agg["severity_open"].get(epic, {}).get(sev, 0), links.url(epic=epic, open_only=True, severity=sev)))
        grid.append(row)
    total_row: list[tuple[Any, Optional[str]]] = [("Total Unique Issues:", None)]
    for sev in severity_cols:
        total_row.append((sum(agg["severity_total"].get(e, {}).get(sev, 0) for e in epics), links.url(severity=sev)))
        total_row.append((sum(agg["severity_open"].get(e, {}).get(sev, 0) for e in epics), links.url(open_only=True, severity=sev)))
    grid.append(total_row)
    return grid


def _merge_timeseries(
    existing_header: list[str],
    existing_rows: list[list[Any]],
    display_date: str,
    epics_today: list[str],
    opened_today: dict[str, int],
) -> tuple[list[str], list[str], dict[str, dict[str, int]]]:
    """Returns (ordered_epics, ordered_dates, data[epic][date] = count)."""
    data: dict[str, dict[str, int]] = {}
    epic_row_order: list[str] = []
    dates: set[str] = set(existing_header[1:]) if len(existing_header) > 1 else set()

    for row in existing_rows:
        if not row or not row[0] or row[0] == "Total Unique Issues:":
            continue
        epic = row[0]
        epic_row_order.append(epic)
        data[epic] = {}
        for i, d in enumerate(existing_header[1:]):
            value = row[i + 1] if i + 1 < len(row) else 0
            try:
                data[epic][d] = int(value) if value not in ("", None) else 0
            except (TypeError, ValueError):
                data[epic][d] = 0

    for epic in epics_today:
        if epic not in data:
            data[epic] = {}
            epic_row_order.append(epic)

    dates.add(display_date)
    for epic in epic_row_order:
        data[epic][display_date] = opened_today.get(epic, 0)

    try:
        ordered_dates = sorted(dates, key=lambda d: datetime.strptime(d, "%d.%m.%Y"))
    except ValueError:
        ordered_dates = sorted(dates)

    epic_row_order.sort(key=_epic_sort_key)
    return epic_row_order, ordered_dates, data


def _timeseries_grid(
    epics: list[str], dates: list[str], data: dict[str, dict[str, int]], links: _LinkBuilder
) -> list[list[tuple[Any, Optional[str]]]]:
    grid: list[list[tuple[Any, Optional[str]]]] = [
        [("Opened Bugs Total", None)],
        [("Parent", None)] + [(d, None) for d in dates],
    ]
    for epic in epics:
        link = links.url(epic=epic, open_only=True)
        grid.append([(epic, None)] + [(data.get(epic, {}).get(d, 0), link) for d in dates])
    total_link = links.url(open_only=True)
    grid.append(
        [("Total Unique Issues:", None)]
        + [(sum(data.get(e, {}).get(d, 0) for e in epics), total_link) for d in dates]
    )
    return grid


def _merge_legacy_period(
    existing_header: list[str], existing_rows: list[list[Any]], display_date: str, counts: dict[str, int]
) -> tuple[list[str], list[tuple[str, dict[str, int]]]]:
    """Merge today's status counts into the legacy "period x status" table
    (column A), keyed by date row like `_merge_timeseries` - but status
    *columns*, not epics, grow over time here, and a "Total" column always
    stays last. Returns (header, [(date_str, {status: count})])."""
    orig_header = list(existing_header) if existing_header else ["period"]
    has_total = len(orig_header) > 1 and orig_header[-1] == "Total"
    orig_status_cols = orig_header[1:-1] if has_total else orig_header[1:]

    parsed_rows: list[tuple[str, dict[str, int]]] = []
    for row in existing_rows:
        if not row or not row[0]:
            continue
        values: dict[str, int] = {}
        for i, col in enumerate(orig_status_cols):
            raw = row[i + 1] if i + 1 < len(row) else 0
            try:
                values[col] = int(raw) if raw not in ("", None) else 0
            except (TypeError, ValueError):
                values[col] = 0
        parsed_rows.append((row[0], values))

    # Match status names ignoring leading/trailing whitespace - the legacy
    # header has at least one column with a stray leading space (" Code
    # Review"), which otherwise fails to match today's live Jira status name
    # ("Code Review") and spawns a disjoint duplicate column every run.
    norm_to_col = {col.strip(): col for col in orig_status_cols}
    new_statuses = []
    for s in counts:
        key = s.strip()
        if key not in norm_to_col:
            norm_to_col[key] = s
            new_statuses.append(s)
    status_cols = orig_status_cols + sorted(new_statuses)
    header = ["period"] + status_cols + ["Total"]

    today_values = {col: 0 for col in status_cols}
    for s, v in counts.items():
        today_values[norm_to_col[s.strip()]] += v
    # Append order, not a chronological re-sort: a re-run for today's date
    # updates that row IN PLACE (same physical position it already had,
    # normally last), and a brand-new date is always added at the true
    # bottom. The original data has at least one mistyped historical date
    # (a "2027" that should read "2026") - sorting by parsed date buried
    # today's row somewhere in the middle, behind that typo, instead of
    # leaving it visible at the bottom where a reader actually looks for the
    # latest snapshot.
    final_rows: list[tuple[str, dict[str, int]]] = []
    replaced = False
    for date_str, values in parsed_rows:
        if date_str == display_date:
            final_rows.append((date_str, today_values))
            replaced = True
        else:
            final_rows.append((date_str, {col: values.get(col, 0) for col in status_cols}))
    if not replaced:
        final_rows.append((display_date, today_values))

    return header, final_rows


def _legacy_period_grid(
    header: list[str], rows_data: list[tuple[str, dict[str, int]]], links: _LinkBuilder
) -> list[list[tuple[Any, Optional[str]]]]:
    status_cols = header[1:-1]
    grid: list[list[tuple[Any, Optional[str]]]] = [[(c, None) for c in header]]
    total_col_letter = _col_letter(len(status_cols))
    for row_idx, (date_str, values) in enumerate(rows_data):
        row_num = row_idx + 2  # 1-indexed sheet row; row 1 is the header
        row: list[tuple[Any, Optional[str]]] = [(date_str, None)]
        for col in status_cols:
            v = values.get(col, 0)
            link = links.url(status=col) if v else None
            row.append((v, link))
        row.append((f"=SUM(B{row_num}:{total_col_letter}{row_num})", None))
        grid.append(row)
    return grid


def _legacy_block_grid(
    display_date: str, epics: list[str], status_cols: list[str], status_by_epic: dict[str, dict[str, int]], links: _LinkBuilder
) -> list[list[tuple[Any, Optional[str]]]]:
    """One "Parent - DATE" block: epic x status, "T:" total column, "Total
    Unique Issues:" row - same shape as the legacy blocks, with a consistent
    status column set every time (the original's own column set varied
    block to block, apparently just whichever statuses were non-zero that
    week - a manual-entry quirk, not a rule worth replicating)."""
    # `status_cols` carries the legacy table's canonical (possibly whitespace-
    # quirky, e.g. " Code Review") column text, while `status_by_epic` is
    # keyed by today's raw Jira status names - match them ignoring
    # leading/trailing whitespace, same fix as `_merge_legacy_period`.
    def _counts_by_col(counts: dict[str, int]) -> dict[str, int]:
        by_norm = {s.strip(): v for s, v in counts.items()}
        return {col: by_norm.get(col.strip(), 0) for col in status_cols}

    header: list[tuple[Any, Optional[str]]] = [(f"Parent - {display_date}", None)] + [(c, None) for c in status_cols] + [("T:", None)]
    grid: list[list[tuple[Any, Optional[str]]]] = [header]
    for epic in epics:
        counts = _counts_by_col(status_by_epic.get(epic, {}))
        row: list[tuple[Any, Optional[str]]] = [(epic, None)]
        for col in status_cols:
            v = counts.get(col, 0)
            row.append((v, links.url(epic=epic, status=col) if v else None))
        total = sum(counts.values())
        row.append((total, links.url(epic=epic) if total else None))
        grid.append(row)
    total_row: list[tuple[Any, Optional[str]]] = [("Total Unique Issues:", None)]
    for col in status_cols:
        col_total = sum(_counts_by_col(status_by_epic.get(e, {})).get(col, 0) for e in epics)
        total_row.append((col_total, links.url(status=col) if col_total else None))
    grand_total = sum(sum(status_by_epic.get(e, {}).values()) for e in epics)
    total_row.append((grand_total, links.url() if grand_total else None))
    grid.append(total_row)
    return grid


# How far right/down to look when detecting the "Parent - DATE" blocks'
# current column, for `_auto_migrate_legacy_blocks` below. The min bound stays
# comfortably right of the period table's own columns (so a stray match there
# is never mistaken for a block); the row bound is generous since the very
# first block's title has always been within the opening rows of the sheet.
_LEGACY_BLOCK_DETECT_MIN_COL = 8  # column I
_LEGACY_BLOCK_DETECT_SCAN_ROWS = 200


def _detect_legacy_block_col0(sheets_client, sheet_title: str, max_col0: int) -> Optional[int]:
    """Scan for the leftmost column currently holding a "Parent - DATE" block
    title/header cell (matches `_BLOCK_HEADER_RE`), so
    `_auto_migrate_legacy_blocks` can detect blocks sitting at a stale
    position and move them. Returns None if no block exists yet (a brand-new
    sheet) - the normal append logic then writes the very first one directly
    at LEGACY_BLOCK_COL0, nothing to migrate."""
    min_col_letter = _col_letter(_LEGACY_BLOCK_DETECT_MIN_COL)
    max_col_letter = _col_letter(max_col0)
    values = sheets_client.read_values(sheet_title, f"{min_col_letter}1:{max_col_letter}{_LEGACY_BLOCK_DETECT_SCAN_ROWS}")
    found_col0: Optional[int] = None
    for row in values:
        for i, cell in enumerate(row):
            if cell and _BLOCK_HEADER_RE.match(str(cell).strip()):
                col0 = _LEGACY_BLOCK_DETECT_MIN_COL + i
                if found_col0 is None or col0 < found_col0:
                    found_col0 = col0
    return found_col0


def _auto_migrate_legacy_blocks(sheets_client, sheet_title: str, sheet_id: int, log_fn: Callable[[str, str], None]) -> None:
    """One-time, idempotent fix-up: if the "Parent - DATE" blocks are still
    sitting at an older column than LEGACY_BLOCK_COL0 (a spreadsheet that
    predates the 2026-10-26 O->Q move, or one manually rolled back via
    Sheets' own Version History - both happened on the real production file),
    shift the whole block area there via a single `cutPaste` (preserves
    formatting/hyperlinks, unlike a read+rewrite) before anything else this
    run touches either area. A sheet already at LEGACY_BLOCK_COL0 - every run
    after the first - is a no-op: detection finds the blocks already in
    place and this does nothing, per explicit user request ("следующие
    запуски уже не должны ничего сдвигать")."""
    scan_max_col0 = min(LEGACY_BLOCK_COL0 + 20, BLOCK_B_COL0 - 1)
    current_col0 = _detect_legacy_block_col0(sheets_client, sheet_title, scan_max_col0)
    if current_col0 is None or current_col0 >= LEGACY_BLOCK_COL0:
        return
    width = scan_max_col0 - current_col0 + 1
    log_fn(
        "WARNING",
        f"'Parent - DATE' blocks found at column {_col_letter(current_col0)}, migrating to "
        f"{_col_letter(LEGACY_BLOCK_COL0)} (one-time fix-up, future runs will be a no-op)",
    )
    sheets_client.cut_paste_range(sheet_id, 0, LEGACY_BLOCK_SCAN_ROWS, current_col0, width, 0, LEGACY_BLOCK_COL0)


async def run_uat_bugs_report_workflow(
    jira_client,
    sheets_client,
    *,
    jql: str = DEFAULT_JQL,
    max_results: int = 500,
    sheet_title: str = SHEET_TITLE,
    run_date: Optional[str] = None,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Fetch UAT bugs, resolve each one's Epic, and (re)write the four report
    tables + their charts into `sheet_title`. Never clears/resets the sheet as
    a whole - only each table's own fixed, generously budgeted range."""

    def _log(level: str, msg: str) -> None:
        logger.info(msg) if level == "INFO" else logger.warning(msg) if level == "WARNING" else logger.debug(msg)
        if log_fn:
            log_fn(level, msg)

    run_date = run_date or date.today().isoformat()
    try:
        display_date = _display_date(run_date)
    except ValueError:
        return {"status": "failed", "error": f"run_date must be YYYY-MM-DD, got {run_date!r}"}

    _log("INFO", f"UAT bugs report started: jql={jql!r}, run_date={run_date}")

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
    severity_cols = _severity_columns(agg)
    unresolved_epic_count = sum(1 for e in epics if e == NO_EPIC_LABEL and agg["total"][e] > 0)

    jira_base_url = str(getattr(jira_client, "base_url", "") or "")
    links = _LinkBuilder(jql, jira_base_url, epic_key_map)

    try:
        sheet_id = sheets_client.ensure_sheet(sheet_title)
        sheets_client.ensure_grid_size(
            sheet_id,
            min_rows=max(LEGACY_PERIOD_ROW_BUDGET, LEGACY_BLOCK_SCAN_ROWS, TIMESERIES_ROW_START + TIMESERIES_ROW_BUDGET) + 50,
            min_cols=TIMESERIES_COL0 + TIMESERIES_COL_BUDGET + 10,
        )
        # One-time, idempotent fix-up: moves the "Parent - DATE" blocks to
        # LEGACY_BLOCK_COL0 if they're still sitting at an older column (e.g.
        # this spreadsheet predates the 2026-10-26 O->Q move, or was rolled
        # back via Sheets' own Version History). A no-op on every run after
        # the first, since the blocks are then already in place.
        _auto_migrate_legacy_blocks(sheets_client, sheet_title, sheet_id, _log)

        # All four tables below share this one header row - unmerge it ONCE,
        # up front, before any of their writes; calling unmerge_row_band
        # again per-block would wipe out the merge a previous block in this
        # same row just created (it sweeps the whole row width by design).
        sheets_client.unmerge_row_band(sheet_id, NEW_SECTION_ROW0 - 1, 1)

        # Block B: All Bugs (current status) - leftmost
        b_col_letter = _col_letter(BLOCK_B_COL0)
        sheets_client.clear_values(
            sheet_title, f"{b_col_letter}{NEW_SECTION_ROW0}:{_col_letter(BLOCK_B_COL0 + 1)}{NEW_SECTION_ROW0 + BLOCK_ROW_BUDGET}"
        )
        b_grid = _snapshot_grid("All Bugs (current status)", "Всего", epics, agg["total"], False, links)
        sheets_client.write_cells_with_links(sheet_id, NEW_SECTION_ROW0 - 1, BLOCK_B_COL0, b_grid, _row_styles(len(epics)))
        sheets_client.merge_cells(sheet_id, NEW_SECTION_ROW0 - 1, BLOCK_B_COL0, 1, 2)

        # Block A: Opened Bugs (current status)
        a_col_letter = _col_letter(BLOCK_A_COL0)
        sheets_client.clear_values(
            sheet_title, f"{a_col_letter}{NEW_SECTION_ROW0}:{_col_letter(BLOCK_A_COL0 + 1)}{NEW_SECTION_ROW0 + BLOCK_ROW_BUDGET}"
        )
        a_grid = _snapshot_grid("Opened Bugs (current status)", "Открыто", epics, agg["opened"], True, links)
        sheets_client.write_cells_with_links(sheet_id, NEW_SECTION_ROW0 - 1, BLOCK_A_COL0, a_grid, _row_styles(len(epics)))
        sheets_client.merge_cells(sheet_id, NEW_SECTION_ROW0 - 1, BLOCK_A_COL0, 1, 2)

        # Block C: Сравнение по эпикам (+ COLUMN chart)
        c_col_letter = _col_letter(BLOCK_C_COL0)
        sheets_client.clear_values(
            sheet_title, f"{c_col_letter}{NEW_SECTION_ROW0}:{_col_letter(BLOCK_C_COL0 + COMPARISON_COL_BUDGET)}{NEW_SECTION_ROW0 + BLOCK_ROW_BUDGET}"
        )
        comparison_grid = _comparison_grid(epics, severity_cols, agg, links)
        comparison_num_cols = 1 + len(severity_cols) * 2
        sheets_client.write_cells_with_links(sheet_id, NEW_SECTION_ROW0 - 1, BLOCK_C_COL0, comparison_grid, _row_styles(len(epics)))
        sheets_client.merge_cells(sheet_id, NEW_SECTION_ROW0 - 1, BLOCK_C_COL0, 1, comparison_num_cols)
        c_header_row0 = NEW_SECTION_ROW0  # header's 1-indexed row is NEW_SECTION_ROW0+1 -> 0-indexed NEW_SECTION_ROW0
        c_spec = sheets_client.column_series_chart_spec(
            "COLUMN", sheet_id,
            header_row_index=c_header_row0,
            num_rows_incl_header=len(comparison_grid) - 1,  # header + epic rows + total row, excluding the title row
            first_col_index=BLOCK_C_COL0,
            num_series_cols=len(severity_cols) * 2,
            x_title="Epic", y_title="Bugs Count",
        )
        sheets_client.upsert_chart(
            sheet_id, "Сравнение по эпикам: всего и открытых багов (по типу бага)", c_spec,
            anchor_row=NEW_SECTION_ROW0 + len(comparison_grid) + 1, anchor_col=BLOCK_C_COL0,
        )

        # Block D: Opened Bugs Total (growing time series + LINE chart)
        existing = sheets_client.read_values(
            sheet_title, f"{_a1(TIMESERIES_ROW_START + 1, TIMESERIES_COL0)}:{_col_letter(TIMESERIES_COL0 + TIMESERIES_COL_BUDGET)}{TIMESERIES_ROW_START + TIMESERIES_ROW_BUDGET}"
        )
        existing_header = existing[0] if existing else []
        existing_rows = existing[1:] if len(existing) > 1 else []
        ts_epics, ts_dates, ts_data = _merge_timeseries(existing_header, existing_rows, display_date, epics, agg["opened"])

        ts_first_col_letter = _col_letter(TIMESERIES_COL0)
        ts_last_col_letter = _col_letter(TIMESERIES_COL0 + TIMESERIES_COL_BUDGET)
        sheets_client.clear_values(
            sheet_title, f"{ts_first_col_letter}{TIMESERIES_ROW_START}:{ts_last_col_letter}{TIMESERIES_ROW_START + TIMESERIES_ROW_BUDGET}"
        )
        ts_grid = _timeseries_grid(ts_epics, ts_dates, ts_data, links)
        sheets_client.write_cells_with_links(
            sheet_id, TIMESERIES_ROW_START - 1, TIMESERIES_COL0, ts_grid, _row_styles(len(ts_epics))
        )
        sheets_client.merge_cells(sheet_id, TIMESERIES_ROW_START - 1, TIMESERIES_COL0, 1, 1 + len(ts_dates))

        d_header_row0 = TIMESERIES_ROW_START  # title row is TIMESERIES_ROW_START; header row (1-indexed) = TIMESERIES_ROW_START+1 -> 0-indexed TIMESERIES_ROW_START
        d_spec = sheets_client.row_series_chart_spec(
            "LINE", sheet_id,
            header_row_index=d_header_row0,
            first_data_row_index=d_header_row0 + 1,
            num_series_rows=len(ts_epics) + 1,  # + the "Total Unique Issues:" row
            label_col_index=TIMESERIES_COL0,
            num_data_cols=len(ts_dates),
            x_title="period", y_title="Open Issues Count",
        )
        sheets_client.upsert_chart(
            sheet_id, "Opened Bugs Total", d_spec,
            anchor_row=TIMESERIES_ROW_START + TIMESERIES_ROW_BUDGET + 2, anchor_col=TIMESERIES_COL0,
        )

        # Legacy table: "period x status" (column A) - grows by one row per
        # run, same as it did historically (manually) before automation;
        # status columns also grow if a never-before-seen status appears.
        # Must stop strictly before LEGACY_BLOCK_COL0 (column Q) - the very
        # first "Parent - DATE" block has its title+header combined on row 1
        # (a quirk of the original manual layout), so a scan range reaching
        # into column Q would contaminate the period table's own header/rows
        # with that block's unrelated columns (this happened once - see
        # project memory).
        legacy_scan_last_col = _col_letter(LEGACY_BLOCK_COL0 - 1)
        legacy_existing = sheets_client.read_values(sheet_title, f"A1:{legacy_scan_last_col}{LEGACY_PERIOD_ROW_BUDGET}")
        legacy_header = legacy_existing[0] if legacy_existing else []
        legacy_rows = legacy_existing[1:] if len(legacy_existing) > 1 else []
        new_header, new_rows_data = _merge_legacy_period(legacy_header, legacy_rows, display_date, agg["status_totals"])

        # Last-resort safety net: `_auto_migrate_legacy_blocks` (called above,
        # before this section) should make this impossible, but if it's ever
        # wrong, log loudly instead of silently overwriting "Parent - DATE"
        # block cells again (what actually happened on 2026-10-26 - see
        # LEGACY_BLOCK_COL0's comment).
        if len(new_header) - 1 >= LEGACY_BLOCK_COL0:
            _log(
                "ERROR",
                f"Period table's 'Total' column (index {len(new_header) - 1}) has reached "
                f"LEGACY_BLOCK_COL0 ({LEGACY_BLOCK_COL0}) - it will overwrite 'Parent - DATE' "
                "block cells on this write. The auto-migration should have moved the blocks "
                "further right before this point - check _auto_migrate_legacy_blocks.",
            )

        sheets_client.clear_values(sheet_title, f"A1:{legacy_scan_last_col}{LEGACY_PERIOD_ROW_BUDGET}")
        legacy_grid = _legacy_period_grid(new_header, new_rows_data, links)
        legacy_status_cols = new_header[1:-1]
        # Header row + the physically last (= most recently added/updated)
        # data row are peach+bold, matching the original; every other data
        # row is plain white, not bold - explicitly re-applied every run so
        # the highlight always lands on today's row, not whatever row used
        # to be last.
        legacy_period_styles = [LEGACY_PERIOD_HEADER_STYLE] + [LEGACY_PERIOD_DATA_STYLE] * (len(new_rows_data) - 1) + [LEGACY_PERIOD_LATEST_STYLE]
        sheets_client.write_cells_with_links(sheet_id, 0, 0, legacy_grid, legacy_period_styles)

        legacy_chart_spec = sheets_client.column_series_chart_spec(
            "LINE", sheet_id,
            header_row_index=0,
            num_rows_incl_header=LEGACY_PERIOD_ROW_BUDGET,
            first_col_index=0,
            num_series_cols=len(legacy_status_cols),
            x_title="period", y_title="Issues Counts",
        )
        sheets_client.upsert_chart(
            sheet_id, LEGACY_CHART_TITLE, legacy_chart_spec,
            anchor_row=LEGACY_PERIOD_ROW_BUDGET + 2, anchor_col=0,
        )

        # Legacy "Parent - DATE" blocks (column Q) - a whole new block
        # appended below the last existing one every run, matching how this
        # was built by hand before automation (one snapshot block per
        # update) - UNLESS the last existing block is already today's
        # (same-day re-run), in which case it's overwritten in place instead
        # of appending a duplicate.
        # Capped below the leftmost of the four new tables (BLOCK_B_COL0) -
        # the blocks' own status columns never get anywhere near that wide,
        # but without this cap a generous scan width would reach into the
        # new tables' column band and misread their rows as part of this
        # block's growth, same class of bug as the period-table/column-O
        # contamination documented in project memory.
        block_scan_last_col = _col_letter(min(LEGACY_BLOCK_COL0 + 20, BLOCK_B_COL0 - 1))
        legacy_block_scan = sheets_client.read_values(
            sheet_title, f"{_col_letter(LEGACY_BLOCK_COL0)}1:{block_scan_last_col}{LEGACY_BLOCK_SCAN_ROWS}"
        )
        last_used_row = 0
        last_block_start_row = None
        last_block_date = None
        for i, row in enumerate(legacy_block_scan, start=1):
            if row:
                last_used_row = i
                first_cell = str(row[0]).strip() if row else ""
                m = _BLOCK_HEADER_RE.match(first_cell)
                if m:
                    last_block_start_row = i
                    last_block_date = m.group(1).strip()

        if last_block_start_row is not None and last_block_date == display_date:
            block_start_row = last_block_start_row
            # Clear the old block's full footprint first - an in-place
            # overwrite whose new epic/row count is smaller than the old
            # block's would otherwise leave stale trailing rows behind.
            sheets_client.clear_values(
                sheet_title, f"{_col_letter(LEGACY_BLOCK_COL0)}{block_start_row}:{block_scan_last_col}{block_start_row + 40}"
            )
        else:
            block_start_row = last_used_row + 3 if last_used_row else 1

        block_grid = _legacy_block_grid(display_date, epics, legacy_status_cols, agg["status_by_epic"], links)
        block_styles = [LEGACY_BLOCK_HEADER_STYLE] + [None] * len(epics) + [LEGACY_BLOCK_TOTAL_STYLE]
        sheets_client.write_cells_with_links(sheet_id, block_start_row - 1, LEGACY_BLOCK_COL0, block_grid, block_styles)
    except Exception as exc:
        _log("ERROR", f"Failed to update Google Sheet: {exc}")
        return {"status": "failed", "error": str(exc)}

    _log(
        "INFO",
        f"UAT bugs report complete: {len(issues)} bug(s) across {len(epics)} epic(s) "
        f"({unresolved_epic_count} unresolved epic bucket), date '{display_date}' in time series, "
        f"legacy period row for '{display_date}', legacy block appended at row {block_start_row}",
    )

    return {
        "status": "succeeded",
        "summary": {
            "jql": jql,
            "bugs_fetched": len(issues),
            "run_date": run_date,
            "display_date": display_date,
            "epics": epics,
            "total": agg["total"],
            "opened": agg["opened"],
            "severity_columns": severity_cols,
            "timeseries_dates": ts_dates,
        },
        "correlation_id": correlation_id,
    }


def render_text_report(result: dict[str, Any]) -> str:
    """Render the report result as a human-readable text summary."""
    if result.get("status") != "succeeded":
        return f"Отчёт не сформирован: {result.get('error', 'неизвестная ошибка')}"

    summary = result.get("summary", {})
    lines: list[str] = []
    lines.append("ОТЧЁТ ПО UAT БАГАМ (по эпикам)")
    lines.append(f"JQL: {summary.get('jql', '')}")
    lines.append(f"Дата снэпшота: {summary.get('display_date', '')}")
    lines.append(f"Всего багов: {summary.get('bugs_fetched', 0)}")
    lines.append("")
    lines.append("По эпикам (всего / открыто):")
    for epic in summary.get("epics", []):
        total = summary.get("total", {}).get(epic, 0)
        opened = summary.get("opened", {}).get(epic, 0)
        lines.append(f"  - {epic}: {total} / {opened}")
    lines.append("")
    lines.append(f"Снэпшотов в таблице динамики: {len(summary.get('timeseries_dates', []))}")

    return "\n".join(lines)


__all__ = [
    "DEFAULT_JQL",
    "IDEAS_JQL",
    "EPICS_JQL",
    "SHEET_TITLE",
    "NO_EPIC_LABEL",
    "run_uat_bugs_report_workflow",
    "render_text_report",
]
