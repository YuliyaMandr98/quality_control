#!/usr/bin/env python3
"""
Convert UAT bugs (found by the customer) into Azure DevOps regression test
cases - no UI/server needed.

Fetches bugs matched by a JQL query (Phase-1 UAT bugs by default), keeps only
those in status "PASSED" (confirmed fixed and ready for regression), resolves
the "UAT Bugs" suite under a "Sprint N" folder (the highest-numbered one by
default, or a specific one via --sprint), and creates one Test Case per bug
that doesn't already have one there.

Each test case: title and description both contain the bug's Jira key, the
description has a clickable link back to the bug plus its precondition
(extracted from the bug's description text), and the steps are the bug's
reproduction steps with the expected result on the final step.

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/uat_bug_test_cases.py
    PYTHONPATH=$(pwd) venv/bin/python scripts/uat_bug_test_cases.py --sprint 23 --apply

Defaults to a dry run (preview only). Pass --apply to actually create test
cases in Azure DevOps. Prerequisites: JIRA_*, AZURE_DEVOPS_*, ANTHROPIC_* configured in .env.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from cli_common import build_anthropic_client, build_azure_client, build_jira_client, load_env
from packages.workflows import uat_bug_test_cases

DATA_DIR = Path(__file__).parent / "data"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Create Azure DevOps test cases from UAT bugs (no UI).")
    parser.add_argument("--jql", default=uat_bug_test_cases.DEFAULT_JQL, help="JQL query selecting UAT bugs")
    parser.add_argument("--plan-id", default=uat_bug_test_cases.DEFAULT_PLAN_ID, help="Azure DevOps test plan id")
    parser.add_argument(
        "--root-suite-id", default=uat_bug_test_cases.DEFAULT_ROOT_SUITE_ID,
        help="Root 'Regression' suite id containing the Sprint N folders",
    )
    parser.add_argument("--sprint", type=int, default=None, help="Target Sprint N folder (default: highest-numbered)")
    parser.add_argument("--max-results", type=int, default=200, help="Maximum bugs to fetch from Jira")
    parser.add_argument("--priority", default=uat_bug_test_cases.DEFAULT_PRIORITY, choices=["High", "Medium", "Low"])
    parser.add_argument("--apply", action="store_true", help="Actually create test cases (default: preview only)")
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/uat_bug_test_cases_report.json)")
    args = parser.parse_args()

    load_env()
    jira_client = build_jira_client()
    azure_client = build_azure_client()
    anthropic_client = build_anthropic_client()

    ok, err = await jira_client.test_connection()
    if not ok:
        print(f"[!] Не удалось подключиться к Jira: {err}")
        sys.exit(1)
    print("[OK] Подключение к Jira проверено.")

    ok, err = await azure_client.test_connection()
    if not ok:
        print(f"[!] Не удалось подключиться к Azure DevOps: {err}")
        sys.exit(1)
    print("[OK] Подключение к Azure DevOps проверено.")

    if not args.apply:
        print("[INFO] Режим предпросмотра — в Azure DevOps ничего не будет записано. Передайте --apply для реального создания.")

    result = await uat_bug_test_cases.run_uat_bug_test_cases_workflow(
        jira_client=jira_client,
        azure_client=azure_client,
        llm_client=anthropic_client,
        jql=args.jql,
        plan_id=args.plan_id,
        root_suite_id=args.root_suite_id,
        sprint_number=args.sprint,
        max_results=args.max_results,
        priority=args.priority,
        dry_run=not args.apply,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") == "failed":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / "uat_bug_test_cases_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    txt_path = output_path.with_suffix(".txt")
    report_text = uat_bug_test_cases.render_text_report(result)
    txt_path.write_text(report_text, encoding="utf-8")

    print("\n=== ОТЧЁТ ===")
    print(report_text)
    print(f"\nJSON сохранён: {output_path}")
    print(f"Текстовый отчёт: {txt_path}")


if __name__ == "__main__":
    asyncio.run(main())
