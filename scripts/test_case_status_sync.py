#!/usr/bin/env python3
"""
Sync Azure DevOps "macOS Apidog" test point statuses from the qa-api-tests
repo's actual .skip state - no UI/server needed.

For every test point Failed on the given configuration under a suite tree:
- Looks up the matching automated test by test case id in the local
  qa-api-tests repo's test titles (e.g. `test("6579: ...")`).
- If found and NOT `.skip`/`.fixme`, sets the test point back to Passed
  (only with --apply - default is preview only).
- If found but still disabled, leaves it Failed (reported).
- If no automated test exists (not yet automated, or "(Manual Only)"),
  leaves it Failed and just reports it - not an error.

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/test_case_status_sync.py
    PYTHONPATH=$(pwd) venv/bin/python scripts/test_case_status_sync.py --apply
    PYTHONPATH=$(pwd) venv/bin/python scripts/test_case_status_sync.py --plan-id 2015 --root-suite-id 2016 --configuration-name "macOS Apidog"

Prerequisites: AZURE_DEVOPS_* configured in .env.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from cli_common import build_azure_client, load_env
from packages.workflows import test_case_status_sync

DATA_DIR = Path(__file__).parent / "data"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Sync 'macOS Apidog' test point statuses with qa-api-tests .skip state.")
    parser.add_argument("--plan-id", default=test_case_status_sync.DEFAULT_PLAN_ID, help="Azure DevOps Test Plan ID")
    parser.add_argument("--root-suite-id", default=test_case_status_sync.DEFAULT_ROOT_SUITE_ID, help="Root suite id to scan")
    parser.add_argument("--configuration-name", default=test_case_status_sync.DEFAULT_CONFIGURATION_NAME, help="Test configuration name")
    parser.add_argument("--tests-root", default=test_case_status_sync.DEFAULT_TESTS_ROOT, help="qa-api-tests 'tests' directory to scan")
    parser.add_argument("--apply", action="store_true", help="Actually set matching test points to Passed (default: preview only)")
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/test_case_status_sync_report.json)")
    args = parser.parse_args()

    load_env()
    azure_client = build_azure_client()

    ok, err = await azure_client.test_connection()
    if not ok:
        print(f"[!] Не удалось подключиться к Azure DevOps: {err}")
        sys.exit(1)
    print("[OK] Подключение к Azure DevOps проверено.")

    if not args.apply:
        print("[i] Режим предпросмотра (--apply не указан) - в Azure DevOps ничего не будет записано.\n")

    result = await test_case_status_sync.run_test_case_status_sync_workflow(
        azure_client=azure_client,
        plan_id=args.plan_id,
        root_suite_id=args.root_suite_id,
        configuration_name=args.configuration_name,
        tests_root=args.tests_root,
        apply=args.apply,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") == "failed":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / "test_case_status_sync_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    txt_path = output_path.with_suffix(".txt")
    report_text = test_case_status_sync.render_text_report(result)
    txt_path.write_text(report_text, encoding="utf-8")

    print("\n=== ОТЧЁТ ===")
    print(report_text)
    print(f"\nJSON сохранён: {output_path}")
    print(f"Текстовый отчёт: {txt_path}")


if __name__ == "__main__":
    asyncio.run(main())
