#!/usr/bin/env python3
"""
Fill in the "QA: AccCriterias are passed" checklist on a Jira issue from the
real number of Acceptance Criteria in its Confluence spec - no UI/server needed.

Finds the issue's "US-X.Y.Z" reference, resolves the matching Confluence spec
page, parses its "Критерии Приемки" table, and adds one checklist item per AC
missing from the "MB: precheck ACs..." and "WEB (IB): precheck ACs..."
sections (removing any that are now stale, i.e. the spec dropped that AC) -
existing, still-valid items (and their done/status) are left untouched.

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/ac_checklist_sync.py MB-1666
    PYTHONPATH=$(pwd) venv/bin/python scripts/ac_checklist_sync.py MB-1666 --write
    PYTHONPATH=$(pwd) venv/bin/python scripts/ac_checklist_sync.py MB-1666 --feature-scope mb_only --write
    PYTHONPATH=$(pwd) venv/bin/python scripts/ac_checklist_sync.py MB-1666 --platforms MB --write

By default this is a dry run (only reports what would change). Pass --write
to actually update Jira. Prerequisites: JIRA_* and CONFLUENCE_* configured in .env.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from cli_common import build_confluence_client, build_jira_client, load_env
from packages.workflows import ac_checklist_sync

DATA_DIR = Path(__file__).parent / "data"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Sync a Jira issue's AC checklist from its Confluence spec (no UI).")
    parser.add_argument("issue_key", help="Jira issue key, e.g. MB-1666")
    parser.add_argument(
        "--specs-folder", default="Фаза 1: спецификации", help="Confluence folder (page title) containing spec pages"
    )
    parser.add_argument("--write", action="store_true", help="Actually write to Jira (default: dry run / preview only)")
    parser.add_argument(
        "--feature-scope", choices=["both", "mb_only", "web_only"], default="both",
        help="Which platform(s) this feature ships on; the other platform's section is cleared entirely if not 'both'",
    )
    parser.add_argument(
        "--platforms", nargs="+", choices=["MB", "WEB"], default=None,
        help="Restrict which section(s) get written this run (e.g. --platforms MB) - for applying one platform at a time",
    )
    parser.add_argument(
        "--mark-dor-dod", action="store_true",
        help="Also mark Definition of Ready / Definition of Done items as done (excluding platform-specific items not covered by --feature-scope)",
    )
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/ac_checklist_sync_report.json)")
    args = parser.parse_args()

    load_env()
    jira_client = build_jira_client()
    confluence_client = build_confluence_client()

    for name, client in (("Jira", jira_client), ("Confluence", confluence_client)):
        ok, err = await client.test_connection()
        if not ok:
            print(f"[!] Не удалось подключиться к {name}: {err}")
            sys.exit(1)
        print(f"[OK] Подключение к {name} проверено.")

    result = await ac_checklist_sync.run_ac_checklist_sync_workflow(
        jira_client=jira_client,
        confluence_client=confluence_client,
        issue_key=args.issue_key,
        specs_folder_title=args.specs_folder,
        dry_run=not args.write,
        feature_scope=args.feature_scope,
        platforms=args.platforms,
        mark_dor_dod=args.mark_dor_dod,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") == "failed":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / "ac_checklist_sync_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    txt_path = output_path.with_suffix(".txt")
    report_text = ac_checklist_sync.render_text_report(result)
    txt_path.write_text(report_text, encoding="utf-8")

    print("\n=== ОТЧЁТ ===")
    print(report_text)
    print(f"\nJSON сохранён: {output_path}")
    print(f"Текстовый отчёт: {txt_path}")
    if not args.write:
        print("\n(Это был предпросмотр — ничего не записано в Jira. Добавьте --write, чтобы применить изменения.)")


if __name__ == "__main__":
    asyncio.run(main())
