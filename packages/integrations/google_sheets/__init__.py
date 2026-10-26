"""Google Sheets integration client (service-account auth).

Works only against *native* Google Sheets spreadsheets (Drive mimeType
application/vnd.google-apps.spreadsheet). A file kept in Excel-compatibility
mode on Drive (mimeType application/vnd.openxmlformats-officedocument.
spreadsheetml.sheet, i.e. a plain .xlsx merely opened in the Sheets UI) is not
reachable through this API at all - convert it first via Drive's "File -> Save
as Google Sheets".
"""

import re
from typing import Any, Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from packages.common import IntegrationConnectionStatus, IntegrationType, get_logger
from packages.integrations import IntegrationClient

logger = get_logger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SPREADSHEET_URL_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")


def extract_spreadsheet_id(value: str) -> str:
    """Accept either a bare spreadsheet ID or a full Google Sheets URL
    (.../spreadsheets/d/<ID>/edit?...) and return just the ID - lets callers
    (CLI flags, the web UI's per-run override field) take whatever a user
    pastes from their browser's address bar without making them extract the
    ID by hand."""
    value = (value or "").strip()
    match = _SPREADSHEET_URL_ID_RE.search(value)
    return match.group(1) if match else value


class GoogleSheetsClient(IntegrationClient):
    """Google Sheets API v4 client, authenticated with a service account key file."""

    provider_type = IntegrationType.GOOGLE_SHEETS

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.spreadsheet_id = config.get("spreadsheet_id", "")
        self.credentials_file = config.get("credentials_file", "")
        self._service = None

    def _get_service(self):
        if self._service is None:
            creds = Credentials.from_service_account_file(self.credentials_file, scopes=_SCOPES)
            self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._service

    async def test_connection(self) -> tuple[bool, Optional[str]]:
        if not self.spreadsheet_id or not self.credentials_file:
            return False, "spreadsheet_id or credentials_file not configured"
        try:
            service = self._get_service()
            service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id, fields="spreadsheetId"
            ).execute()
            return True, None
        except HttpError as e:
            return False, f"HTTP {e.resp.status}: {e.reason}"
        except Exception as e:
            return False, str(e)

    async def get_health_status(self) -> IntegrationConnectionStatus:
        if not self.spreadsheet_id or not self.credentials_file:
            return IntegrationConnectionStatus.UNCONFIGURED
        ok, _ = await self.test_connection()
        return IntegrationConnectionStatus.HEALTHY if ok else IntegrationConnectionStatus.UNHEALTHY

    def _sheets_metadata(self) -> list[dict[str, Any]]:
        service = self._get_service()
        meta = service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets(properties(sheetId,title),charts(chartId,spec(title)))",
        ).execute()
        return meta.get("sheets", [])

    def list_sheet_titles(self) -> list[str]:
        """Tab names in this spreadsheet, in their current left-to-right
        order - powers the "pick a tab" dropdown on the report run forms so
        users select from the real tabs instead of typing a name by hand."""
        return [
            (sheet.get("properties") or {}).get("title", "")
            for sheet in self._sheets_metadata()
            if (sheet.get("properties") or {}).get("title")
        ]

    def ensure_sheet(self, title: str) -> int:
        """Return the sheetId (gid) for the tab named `title`, creating it if missing.
        Matches case-insensitively - Sheets enforces tab-name uniqueness
        case-insensitively even though it reports/accepts the original casing,
        so an exact-case match here could miss an existing tab and then fail
        to create a "new" one that collides with it."""
        for sheet in self._sheets_metadata():
            props = sheet.get("properties", {})
            if (props.get("title") or "").casefold() == title.casefold():
                return props["sheetId"]

        service = self._get_service()
        result = service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
        return result["replies"][0]["addSheet"]["properties"]["sheetId"]

    def ensure_grid_size(self, sheet_id: int, min_rows: int, min_cols: int) -> None:
        """Grow (never shrink) a sheet's grid dimensions so writes far down/
        right don't fail - a freshly-converted-from-.xlsx sheet can start with
        far fewer rows/columns than a growing report needs. Only issues an
        update when the current size is actually smaller, and only ever
        raises rowCount/columnCount, never lowers them (lowering would
        delete any data sitting beyond the new bound)."""
        service = self._get_service()
        meta = service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets(properties(sheetId,gridProperties))",
        ).execute()
        current = None
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("sheetId") == sheet_id:
                current = props.get("gridProperties") or {}
                break
        if current is None:
            return
        rows = max(current.get("rowCount", 0), min_rows)
        cols = max(current.get("columnCount", 0), min_cols)
        if rows == current.get("rowCount", 0) and cols == current.get("columnCount", 0):
            return
        service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": sheet_id, "gridProperties": {"rowCount": rows, "columnCount": cols}},
                            "fields": "gridProperties.rowCount,gridProperties.columnCount",
                        }
                    }
                ]
            },
        ).execute()

    def read_values(self, sheet_title: str, a1_range: str) -> list[list[str]]:
        service = self._get_service()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=f"'{sheet_title}'!{a1_range}")
            .execute()
        )
        return result.get("values", [])

    def clear_values(self, sheet_title: str, a1_range: str) -> None:
        service = self._get_service()
        service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{sheet_title}'!{a1_range}",
            body={},
        ).execute()

    def write_values(self, sheet_title: str, a1_range: str, values: list[list[Any]]) -> None:
        service = self._get_service()
        service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{sheet_title}'!{a1_range}",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()

    @staticmethod
    def _cell_data(value: Any, link: Optional[str], style: Optional[dict[str, Any]]) -> dict[str, Any]:
        """Build a CellData payload for `updateCells`: a plain value (number,
        string, or "=..." formula) plus an optional cell-level format.

        A hyperlink on a cell is attached via `userEnteredFormat.textFormat.
        link.uri` - this is how native Google Sheets stores "Insert link" on
        ANY value including a plain number (confirmed empirically: the
        cell's `userEnteredValue`/`effectiveValue` stays a true numberValue,
        not a HYPERLINK() formula). This is a different, cell-wide mechanism
        from `textFormatRuns` (per-character-run formatting within one cell),
        which the API rejects on non-string values - don't confuse the two."""
        if value is None:
            cell: dict[str, Any] = {}
        elif isinstance(value, str) and value.startswith("="):
            cell = {"userEnteredValue": {"formulaValue": value}}
        elif isinstance(value, (int, float)):
            cell = {"userEnteredValue": {"numberValue": value}}
        else:
            cell = {"userEnteredValue": {"stringValue": str(value)}}

        fmt: dict[str, Any] = {k: v for k, v in (style or {}).items() if k != "textFormat"}
        text_format = dict((style or {}).get("textFormat") or {})
        if link:
            # Let Sheets render its own default hyperlink color (blue,
            # underlined) - an explicit foregroundColor here would override
            # it and the link would render in plain body-text color instead.
            text_format.pop("foregroundColor", None)
            text_format.pop("foregroundColorStyle", None)
            text_format["link"] = {"uri": link}
        if text_format:
            fmt["textFormat"] = text_format
        if fmt:
            cell["userEnteredFormat"] = fmt
        return cell

    def write_cells_with_links(
        self,
        sheet_id: int,
        start_row0: int,
        start_col0: int,
        rows: list[list[tuple[Any, Optional[str]]]],
        row_styles: Optional[list[Optional[dict[str, Any]]]] = None,
    ) -> None:
        """Write a 2D block of (value, link_or_None) pairs starting at
        (start_row0, start_col0), zero-indexed - one `updateCells` request per
        row, batched into a single API call. `link` may be None for cells
        with no hyperlink. `row_styles`, if given, is one cell-format dict
        (or None) per row, applied uniformly to every cell in that row (e.g.
        a title banner, a bold header row, a bold "Total" row) - matches this
        project's own "Bugs per Epics" tab styling, read directly from its
        real cell formatting via the Sheets API before writing this."""
        requests = []
        for r, row in enumerate(rows):
            style = row_styles[r] if row_styles and r < len(row_styles) else None
            requests.append({
                "updateCells": {
                    "rows": [{"values": [self._cell_data(value, link, style) for value, link in row]}],
                    "fields": "userEnteredValue,userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start_row0 + r,
                        "endRowIndex": start_row0 + r + 1,
                        "startColumnIndex": start_col0,
                        "endColumnIndex": start_col0 + len(row),
                    },
                }
            })
        if not requests:
            return
        service = self._get_service()
        service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests}
        ).execute()

    def clear_formatting(self, sheet_id: int, row0: int, num_rows: int, col0: int, num_cols: int) -> None:
        """Resets cell FORMATTING (background color, text format, borders)
        back to default for this range - `values().clear()` (used by
        `clear_values`) only ever clears cell VALUES, never formatting, so a
        table's own blue title banner / light-blue header-row fill lingers
        forever at its OLD position after the table itself has been moved
        (or merged into) a new one, looking like an empty, highlighted,
        "abandoned" table even though the real data is gone. Call this on
        any range a table used to occupy but no longer does."""
        service = self._get_service()
        service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": row0,
                                "endRowIndex": row0 + num_rows,
                                "startColumnIndex": col0,
                                "endColumnIndex": col0 + num_cols,
                            },
                            "cell": {"userEnteredFormat": {}},
                            "fields": "userEnteredFormat",
                        }
                    }
                ]
            },
        ).execute()

    def unmerge_row_band(self, sheet_id: int, row0: int, num_rows: int) -> None:
        """Unmerges the ENTIRE column width of this row band (column 0
        through a wide end column) - must be called BEFORE writing new
        values into that band, not after: a cell that's a non-top-left
        member of an existing merge silently discards any `updateCells`
        write aimed at it (the merge keeps redirecting/absorbing it), so if
        a previous run's merge used a *different* column position than this
        run's new target - e.g. a table's column got reassigned between runs
        - writing first and unmerging/re-merging after is too late: the
        value never lands. `unmergeCells` requires its range to fully
        contain every merge it touches (a partial-overlap range errors with
        "You must select all cells in a merged range..."), so this
        deliberately spans the full row width rather than just one table's
        own columns. CAUTION: if several tables share this exact row (e.g.
        laid out side by side in one shared header row), call this ONCE
        before writing any of them, not once per table - a second call for
        the same row would wipe out the merge a previous table in that row
        just created, since this sweeps the full row width by design.
        `values().clear()` never removes merges, so this can't be avoided by
        just clearing values first."""
        service = self._get_service()
        service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "unmergeCells": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": row0,
                                "endRowIndex": row0 + num_rows,
                                "startColumnIndex": 0,
                                "endColumnIndex": 10000,
                            }
                        }
                    }
                ]
            },
        ).execute()

    def merge_cells(self, sheet_id: int, row0: int, col0: int, num_rows: int, num_cols: int) -> None:
        """Merges the precise target range - call `unmerge_row_band` for the
        same row band BEFORE writing values into it (not after), then this
        AFTER writing them; see `unmerge_row_band` for why the ordering
        matters."""
        target_range = {
            "sheetId": sheet_id,
            "startRowIndex": row0,
            "endRowIndex": row0 + num_rows,
            "startColumnIndex": col0,
            "endColumnIndex": col0 + num_cols,
        }
        service = self._get_service()
        service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"mergeCells": {"range": target_range, "mergeType": "MERGE_ALL"}}]},
        ).execute()

    def cut_paste_range(
        self, sheet_id: int, row0: int, num_rows: int, col0: int, num_cols: int, dest_row0: int, dest_col0: int
    ) -> None:
        """Move a rectangular range - values, formatting, hyperlinks, notes -
        to a new location via the Sheets API's own `cutPaste`, NOT a
        read-then-rewrite (which would lose cell-level hyperlinks - see
        `_cell_data`'s own note on how those are stored, and `write_values`/
        `write_cells_with_links` which don't round-trip them). The source
        range is cleared automatically as part of the "cut" - don't call
        `clear_values` on it separately."""
        service = self._get_service()
        service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{
                "cutPaste": {
                    "source": {
                        "sheetId": sheet_id,
                        "startRowIndex": row0,
                        "endRowIndex": row0 + num_rows,
                        "startColumnIndex": col0,
                        "endColumnIndex": col0 + num_cols,
                    },
                    "destination": {"sheetId": sheet_id, "rowIndex": dest_row0, "columnIndex": dest_col0},
                    "pasteType": "PASTE_NORMAL",
                }
            }]},
        ).execute()

    @staticmethod
    def row_series_chart_spec(
        chart_type: str,
        sheet_id: int,
        header_row_index: int,
        first_data_row_index: int,
        num_series_rows: int,
        label_col_index: int,
        num_data_cols: int,
        x_title: str,
        y_title: str,
    ) -> dict[str, Any]:
        """A chart where the X-axis (domain) is one header ROW of labels (e.g.
        dates) and each subsequent ROW becomes its own series (e.g. one epic's
        counts over time). `headerCount: 1` at the top level means "the first
        CELL of every domain/series source range is a label, not a data point" -
        NOT "the first row is a header" as the name suggests - so both the
        domain and each series range must start at `label_col_index` (the
        column holding the row's own text label, e.g. the epic name / the
        "Parent"/"period" header cell) and span `1 + num_data_cols` columns:
        the chart then reads that first cell as the series' legend name (for
        each series) or simply skips it (for the domain), plotting only the
        remaining `num_data_cols` cells. Confirmed empirically against this
        project's own sheet - omitting headerCount renders unlabeled lines;
        setting it without including the label column in the range instead
        silently drops the first real data column."""
        col_span = {
            "startColumnIndex": label_col_index,
            "endColumnIndex": label_col_index + 1 + num_data_cols,
        }
        domain_source = {"sheetId": sheet_id, "startRowIndex": header_row_index, "endRowIndex": header_row_index + 1, **col_span}
        series = [
            {
                "series": {
                    "sourceRange": {
                        "sources": [
                            {
                                "sheetId": sheet_id,
                                "startRowIndex": first_data_row_index + r,
                                "endRowIndex": first_data_row_index + r + 1,
                                **col_span,
                            }
                        ]
                    }
                },
                "targetAxis": "LEFT_AXIS",
            }
            for r in range(num_series_rows)
        ]
        return {
            "chartType": chart_type,
            "legendPosition": "RIGHT_LEGEND",
            "headerCount": 1,
            "axis": [
                {"position": "BOTTOM_AXIS", "title": x_title},
                {"position": "LEFT_AXIS", "title": y_title},
            ],
            "domains": [{"domain": {"sourceRange": {"sources": [domain_source]}}}],
            "series": series,
        }

    @staticmethod
    def column_series_chart_spec(
        chart_type: str,
        sheet_id: int,
        header_row_index: int,
        num_rows_incl_header: int,
        first_col_index: int,
        num_series_cols: int,
        x_title: str,
        y_title: str,
    ) -> dict[str, Any]:
        """A chart where the X-axis (domain) is one COLUMN of row labels (e.g.
        epic names) and each subsequent COLUMN becomes its own series, with the
        header row supplying each series' legend name (verified against this
        project's own "Сравнение по эпикам..." column chart on "Bugs per Epics")."""
        domain_source = {
            "sheetId": sheet_id,
            "startRowIndex": header_row_index,
            "endRowIndex": header_row_index + num_rows_incl_header,
            "startColumnIndex": first_col_index,
            "endColumnIndex": first_col_index + 1,
        }
        series = [
            {
                "series": {
                    "sourceRange": {
                        "sources": [
                            {
                                "sheetId": sheet_id,
                                "startRowIndex": header_row_index,
                                "endRowIndex": header_row_index + num_rows_incl_header,
                                "startColumnIndex": first_col_index + c,
                                "endColumnIndex": first_col_index + c + 1,
                            }
                        ]
                    }
                },
                "targetAxis": "LEFT_AXIS",
            }
            for c in range(1, num_series_cols + 1)
        ]
        return {
            "chartType": chart_type,
            "legendPosition": "RIGHT_LEGEND",
            "headerCount": 1,
            "axis": [
                {"position": "BOTTOM_AXIS", "title": x_title},
                {"position": "LEFT_AXIS", "title": y_title},
            ],
            "domains": [{"domain": {"sourceRange": {"sources": [domain_source]}}}],
            "series": series,
        }

    def upsert_chart(
        self,
        sheet_id: int,
        chart_title: str,
        basic_chart: dict[str, Any],
        anchor_row: int,
        anchor_col: int,
    ) -> None:
        """Create the chart titled `chart_title` on `sheet_id` if it doesn't exist
        yet, or fully replace its spec (via updateChartSpec) if it does - charts
        are always kept in sync with the current table extent rather than left
        stale, since both row/column counts can change run to run (new epics,
        new dates, new severities)."""
        existing_chart_id = None
        for sheet in self._sheets_metadata():
            if sheet.get("properties", {}).get("sheetId") != sheet_id:
                continue
            for chart in sheet.get("charts", []):
                if (chart.get("spec") or {}).get("title") == chart_title:
                    existing_chart_id = chart.get("chartId")
                    break

        spec = {"title": chart_title, "basicChart": basic_chart}
        if existing_chart_id is not None:
            requests = [{"updateChartSpec": {"chartId": existing_chart_id, "spec": spec}}]
        else:
            requests = [
                {
                    "addChart": {
                        "chart": {
                            "spec": spec,
                            "position": {
                                "overlayPosition": {
                                    "anchorCell": {
                                        "sheetId": sheet_id,
                                        "rowIndex": anchor_row,
                                        "columnIndex": anchor_col,
                                    }
                                }
                            },
                        }
                    }
                }
            ]

        service = self._get_service()
        service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests}
        ).execute()


__all__ = ["GoogleSheetsClient"]
