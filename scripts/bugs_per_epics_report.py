#!/usr/bin/env python3
"""
Write/refresh the "Bugs per Epics" report (by-epic tables + charts) in the
project's Google Sheet - no UI/server needed.

Fetches ALL project bugs across the four sub-type issuetypes this project
tracks (BE BUG / Bug / Mobile bug / FE bug) from Jira, resolves each bug's
Epic, and (re)writes six tables into the "Bugs per Epics" tab: current
open/total counts per epic per sub-type, a sub-type comparison table, and
three per-epic "open bugs" time series (all sub-types combined, Mobile bug
only, BE BUG only) that each gain one new dated column every run (history is
never overwritten). Only those three time series and their charts grow
between runs; the other three tables/chart are simply refreshed to the
latest snapshot every time.

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/bugs_per_epics_report.py
    PYTHONPATH=$(pwd) venv/bin/python scripts/bugs_per_epics_report.py --jql "project = MB AND issuetype in (\"BE BUG\", Bug, \"Mobile bug\", \"FE bug\") AND statusCategory != Done"

Prerequisites: JIRA_* and GOOGLE_SHEETS_* configured in .env (see .env.example
for the Google Sheets setup notes - the target spreadsheet must be a native
Google Sheet, not a .xlsx merely opened in the Sheets UI).
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from cli_common import build_google_sheets_client, build_jira_client, load_env
from packages.workflows import bugs_per_epics_report

DATA_DIR = Path(__file__).parent / "data"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Write/refresh the Bugs per Epics report in Google Sheets (no UI).")
    parser.add_argument("--jql", default=bugs_per_epics_report.DEFAULT_JQL, help="JQL query selecting bugs to report on")
    parser.add_argument("--max-results", type=int, default=2000, help="Maximum bugs to fetch")
    parser.add_argument("--sheet-title", default=bugs_per_epics_report.SHEET_TITLE, help="Target tab name")
    parser.add_argument("--run-date", help="Override the snapshot date (YYYY-MM-DD); defaults to today")
    parser.add_argument(
        "--spreadsheet",
        help="Google Sheets URL or spreadsheet ID to write to (overrides GOOGLE_SHEETS_SPREADSHEET_ID in .env for this run only)",
    )
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/bugs_per_epics_report.json)")
    args = parser.parse_args()

    load_env()
    jira_client = build_jira_client()
    sheets_client = build_google_sheets_client(args.spreadsheet)

    ok, err = await jira_client.test_connection()
    if not ok:
        print(f"[!] Не удалось подключиться к Jira: {err}")
        sys.exit(1)
    print("[OK] Подключение к Jira проверено.")

    ok, err = await sheets_client.test_connection()
    if not ok:
        print(f"[!] Не удалось подключиться к Google Sheets: {err}")
        sys.exit(1)
    print("[OK] Подключение к Google Sheets проверено.")

    result = await bugs_per_epics_report.run_bugs_per_epics_report_workflow(
        jira_client=jira_client,
        sheets_client=sheets_client,
        jql=args.jql,
        max_results=args.max_results,
        sheet_title=args.sheet_title,
        run_date=args.run_date,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") != "succeeded":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / "bugs_per_epics_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== ОТЧЁТ ===")
    print(bugs_per_epics_report.render_text_report(result))
    print(f"\nJSON сохранён: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
