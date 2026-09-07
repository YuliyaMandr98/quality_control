#!/usr/bin/env python3
"""
Upload a validated test case CSV into an Azure DevOps Test Plan suite, from
the command line - no UI/server needed.

Resolves (or creates) the root -> [Админ Панель] -> Epic -> User Story suite
chain automatically from the User Story's Confluence ancestors, then uploads
the CSV's test cases into it. Always previews first; pass --apply to
actually write to Azure DevOps.

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/upload_test_cases.py \\
        --us 20.1.1 --plan web --csv path/to/test_cases.csv

    PYTHONPATH=$(pwd) venv/bin/python scripts/upload_test_cases.py \\
        --us 20.1.1 --plan web --csv path/to/test_cases.csv --apply

    # Remove existing test cases from the resolved suite before re-uploading
    # (unlinks them from the suite - does NOT delete the work items):
    PYTHONPATH=$(pwd) venv/bin/python scripts/upload_test_cases.py \\
        --us 20.1.1 --plan web --csv path/to/test_cases.csv --apply --replace-existing

Prerequisites: AZURE_DEVOPS_*, CONFLUENCE_* configured in .env.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from cli_common import build_azure_client, build_confluence_client, load_env
from packages.workflows import upload_test_cases as upload_workflow

DATA_DIR = Path(__file__).parent / "data"


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload a validated test case CSV into an Azure DevOps Test Plan suite (no UI)."
    )
    parser.add_argument("--us", required=True, help="User Story number, e.g. 20.1.1 or US-20.1.1 or AUS-7.2")
    parser.add_argument("--plan", choices=sorted(upload_workflow.TEST_PLANS), help="Test Plan key (web/mobile/api)")
    parser.add_argument("--plan-id", help="Azure DevOps Test Plan ID directly, instead of --plan")
    parser.add_argument("--csv", required=True, help="Path to the validated test case CSV")
    parser.add_argument("--specs-folder", default=upload_workflow.DEFAULT_SPECS_FOLDER_TITLE,
                         help="Confluence page title of the specifications folder")
    parser.add_argument("--admin-specs-folder-id", default=upload_workflow.DEFAULT_ADMIN_SPECS_FOLDER_ID,
                         help="Fallback Confluence page id for admin-panel specs (AUS-<n> pages)")
    parser.add_argument("--admin-group-title", default=upload_workflow.DEFAULT_ADMIN_GROUP_SUITE_TITLE,
                         help="Azure suite name for the admin-panel grouping level")
    parser.add_argument("--epic-suite-name", help="Override the Epic suite name")
    parser.add_argument("--us-suite-name", help="Override the US suite name")
    parser.add_argument("--state", default=upload_workflow.DEFAULT_STATE,
                         help="Azure DevOps State to set on each created test case")
    parser.add_argument(
        "--replace-existing", action="store_true",
        help="Remove ALL existing test cases from the resolved suite first (unlinks them - does not delete "
             "the work items, only needs Test Plan permissions), then re-create everything from the CSV "
             "(default: add new ones alongside existing test cases, skipping title duplicates)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually create test cases in Azure DevOps (default: preview only, nothing is written)",
    )
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/US-<n>_upload_result.json)")
    args = parser.parse_args()

    if not args.plan and not args.plan_id:
        print("[!] Укажите --plan (web/mobile/api) или --plan-id напрямую.")
        sys.exit(1)
    plan_id = args.plan_id or upload_workflow.TEST_PLANS[args.plan]["plan_id"]

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        print(f"[!] CSV-файл не найден: {csv_path}")
        sys.exit(1)
    csv_text = csv_path.read_text(encoding="utf-8-sig")

    load_env()
    azure_client = build_azure_client()
    confluence_client = build_confluence_client()

    for name, client in (("Azure DevOps", azure_client), ("Confluence", confluence_client)):
        ok, err = await client.test_connection()
        if not ok:
            print(f"[!] Не удалось подключиться к {name}: {err}")
            sys.exit(1)
        print(f"[OK] Подключение к {name} проверено.")

    result = await upload_workflow.run_upload_test_cases_workflow(
        azure_client=azure_client,
        confluence_client=confluence_client,
        us=args.us,
        plan_id=plan_id,
        csv_text=csv_text,
        specs_folder=args.specs_folder,
        admin_specs_folder_id=args.admin_specs_folder_id,
        admin_group_title=args.admin_group_title,
        epic_suite_name=args.epic_suite_name,
        us_suite_name=args.us_suite_name,
        state=args.state,
        force=args.replace_existing,
        dry_run=not args.apply,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") == "failed":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / f"US-{result.get('us_number')}_upload_result.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nSuite: {result.get('us_suite_id')} (plan {plan_id}) - {result.get('epic_title')} / {result.get('us_suite_name')}")

    if not args.apply:
        print(f"\n[DRY-RUN] {result.get('test_cases_total', 0)} тест-кейс(ов) из CSV:")
        for row in result.get("preview", []):
            dup = " (уже существует - будет пропущен)" if row.get("duplicate") else ""
            print(f"  • [{row.get('priority')}] {row.get('title')} ({row.get('steps_count')} шаг(ов)){dup}")
        print(f"\nРезультат сохранён: {output_path}")
        print("[i] Preview: ничего не записано в Azure DevOps. Запустите с --apply, чтобы реально загрузить.")
        return

    print(
        f"\nTotal: {result.get('test_cases_total', 0)} | Created: {result.get('created_count', 0)} | "
        f"Skipped (dup): {result.get('skipped_count', 0)} | Failed: {result.get('failed_count', 0)}"
    )
    print(f"Результат сохранён: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
