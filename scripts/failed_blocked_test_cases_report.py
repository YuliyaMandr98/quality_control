#!/usr/bin/env python3
"""
List every Failed/Blocked test case in the Mobile and/or Web UI test suites
in Azure DevOps Test Plans - no UI/server needed.

For each Failed/Blocked test point, reports the test case id, title,
configuration, and where it lives in the suite tree (parent folder = Epic
suite, subfolder = User Story suite). Read-only - never writes to Azure
DevOps.

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/failed_blocked_test_cases_report.py
    PYTHONPATH=$(pwd) venv/bin/python scripts/failed_blocked_test_cases_report.py --no-web
    PYTHONPATH=$(pwd) venv/bin/python scripts/failed_blocked_test_cases_report.py --mobile-plan-id 438 --mobile-root-suite-id 439

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
from packages.workflows import failed_blocked_test_cases_report

DATA_DIR = Path(__file__).parent / "data"


async def main() -> None:
    parser = argparse.ArgumentParser(description="List Failed/Blocked test cases in the Mobile/Web UI test suites.")
    parser.add_argument("--no-mobile", dest="include_mobile", action="store_false", help="Skip the Mobile suite")
    parser.add_argument("--no-web", dest="include_web", action="store_false", help="Skip the Web suite")
    parser.add_argument("--mobile-plan-id", default=failed_blocked_test_cases_report.MOBILE_PLAN_ID)
    parser.add_argument("--mobile-root-suite-id", default=failed_blocked_test_cases_report.MOBILE_ROOT_SUITE_ID)
    parser.add_argument("--web-plan-id", default=failed_blocked_test_cases_report.WEB_PLAN_ID)
    parser.add_argument("--web-root-suite-id", default=failed_blocked_test_cases_report.WEB_ROOT_SUITE_ID)
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/failed_blocked_test_cases_report.json)")
    parser.set_defaults(include_mobile=True, include_web=True)
    args = parser.parse_args()

    load_env()
    azure_client = build_azure_client()

    ok, err = await azure_client.test_connection()
    if not ok:
        print(f"[!] Не удалось подключиться к Azure DevOps: {err}")
        sys.exit(1)
    print("[OK] Подключение к Azure DevOps проверено.")

    result = await failed_blocked_test_cases_report.run_failed_blocked_test_cases_report_workflow(
        azure_client=azure_client,
        include_mobile=args.include_mobile,
        include_web=args.include_web,
        mobile_plan_id=args.mobile_plan_id,
        mobile_root_suite_id=args.mobile_root_suite_id,
        web_plan_id=args.web_plan_id,
        web_root_suite_id=args.web_root_suite_id,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") == "failed":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / "failed_blocked_test_cases_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    txt_path = output_path.with_suffix(".txt")
    report_text = failed_blocked_test_cases_report.render_text_report(result)
    txt_path.write_text(report_text, encoding="utf-8")

    print("\n=== ОТЧЁТ ===")
    print(report_text)
    print(f"\nJSON сохранён: {output_path}")
    print(f"Текстовый отчёт: {txt_path}")


if __name__ == "__main__":
    asyncio.run(main())
