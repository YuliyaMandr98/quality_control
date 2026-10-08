#!/usr/bin/env python3
"""
Set Azure DevOps test case Priority for test cases that already exist, from
an LLM classification (P0/P1/P2) of the User Story spec each one's suite maps
to - from the command line, no UI/server needed.

Companion to upload_test_cases.py: never creates suites or test cases, only
updates the Priority field on what's already there. Always previews first;
pass --apply to actually write to Azure DevOps.

Usage:
    # One User Story's existing suite:
    PYTHONPATH=$(pwd) venv/bin/python scripts/prioritize_test_cases.py \\
        --scope single_us --us 20.1.1 --plan web

    PYTHONPATH=$(pwd) venv/bin/python scripts/prioritize_test_cases.py \\
        --scope single_us --us 20.1.1 --plan web --apply

    # Every US/AUS suite in the whole Test Plan:
    PYTHONPATH=$(pwd) venv/bin/python scripts/prioritize_test_cases.py \\
        --scope whole_plan --plan web --apply

Prerequisites: AZURE_DEVOPS_*, CONFLUENCE_*, ANTHROPIC_API_KEY configured in .env.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from cli_common import build_anthropic_client, build_azure_client, build_confluence_client, load_env
from packages.workflows import prioritize_test_cases as prioritize_workflow
from packages.workflows import upload_test_cases as upload_workflow

DATA_DIR = Path(__file__).parent / "data"


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set Azure DevOps Priority on existing test cases, from an LLM P0/P1/P2 classification (no UI)."
    )
    parser.add_argument("--scope", required=True, choices=prioritize_workflow.SCOPES,
                         help="single_us: one User Story's existing suite. whole_plan: every US/AUS suite in the plan.")
    parser.add_argument("--us", help="User Story number, e.g. 20.1.1 or US-20.1.1 or AUS-7.2 - required for --scope single_us")
    parser.add_argument("--plan", choices=sorted(upload_workflow.TEST_PLANS), help="Test Plan key (web/mobile/api)")
    parser.add_argument("--plan-id", help="Azure DevOps Test Plan ID directly, instead of --plan")
    parser.add_argument("--specs-folder", default=upload_workflow.DEFAULT_SPECS_FOLDER_TITLE,
                         help="Confluence page title of the specifications folder")
    parser.add_argument("--admin-specs-folder-id", default=upload_workflow.DEFAULT_ADMIN_SPECS_FOLDER_ID,
                         help="Fallback Confluence page id for admin-panel specs (AUS-<n> pages)")
    parser.add_argument("--admin-group-title", default=upload_workflow.DEFAULT_ADMIN_GROUP_SUITE_TITLE,
                         help="Azure suite name for the admin-panel grouping level")
    parser.add_argument("--batch-delay-seconds", type=int, default=10,
                         help="Seconds to wait between LLM calls (relevant for --scope whole_plan)")
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually update Priority in Azure DevOps (default: preview only, nothing is written)",
    )
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/prioritize_<scope>_result.json)")
    args = parser.parse_args()

    if args.scope == "single_us" and not args.us:
        print("[!] --us обязателен для --scope single_us.")
        sys.exit(1)
    if not args.plan and not args.plan_id:
        print("[!] Укажите --plan (web/mobile/api) или --plan-id напрямую.")
        sys.exit(1)
    plan_id = args.plan_id or upload_workflow.TEST_PLANS[args.plan]["plan_id"]

    load_env()
    azure_client = build_azure_client()
    confluence_client = build_confluence_client()
    anthropic_client = build_anthropic_client()

    for name, client in (("Azure DevOps", azure_client), ("Confluence", confluence_client), ("Claude", anthropic_client)):
        ok, err = await client.test_connection()
        if not ok:
            print(f"[!] Не удалось подключиться к {name}: {err}")
            sys.exit(1)
        print(f"[OK] Подключение к {name} проверено.")

    if args.apply:
        scope_label = "ВСЕХ подходящих suite в выбранном Test Plan" if args.scope == "whole_plan" else "suite выбранной User Story"
        confirm = input(f"Вы собираетесь ИЗМЕНИТЬ приоритет тест-кейсов в Azure DevOps для {scope_label}. Продолжить? [y/N] ")
        if confirm.strip().lower() != "y":
            print("Отменено.")
            sys.exit(0)

    result = await prioritize_workflow.run_prioritize_test_cases_workflow(
        azure_client=azure_client,
        confluence_client=confluence_client,
        llm_client=anthropic_client,
        scope=args.scope,
        plan_id=plan_id,
        us=args.us,
        specs_folder=args.specs_folder,
        admin_specs_folder_id=args.admin_specs_folder_id,
        admin_group_title=args.admin_group_title,
        batch_delay_seconds=args.batch_delay_seconds,
        dry_run=not args.apply,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") == "failed":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / f"prioritize_{args.scope}_result.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"\nSuite(ов) обработано: {result.get('suites_processed', 0)}/{result.get('suites_total', 0)} | "
        f"Тест-кейсов: {result.get('test_cases_total', 0)}"
    )
    for suite in result.get("results", []):
        if suite.get("error"):
            print(f"  ⚠️ {suite.get('us_number')} / '{suite.get('suite_name')}': {suite.get('error')}")
            continue
        print(
            f"  • {suite.get('us_number')} / '{suite.get('suite_name')}': "
            f"{suite.get('business_priority_tier')} -> {suite.get('business_priority')} "
            f"({suite.get('business_priority_reasoning')})"
        )
        for tc in suite.get("test_cases", []):
            if not args.apply:
                status = "будет изменён" if tc.get("changed") else "без изменений"
            elif tc.get("error"):
                status = f"ошибка: {tc.get('error')}"
            elif tc.get("updated"):
                status = "обновлён"
            else:
                status = "—"
            print(f"      - [{tc.get('old_priority')} -> {tc.get('new_priority')}] {tc.get('title')} ({status})")

    print(f"\nРезультат сохранён: {output_path}")
    if not args.apply:
        print("[i] Preview: ничего не записано в Azure DevOps. Запустите с --apply, чтобы реально обновить приоритеты.")
    else:
        print(f"Обновлено: {result.get('updated_count', 0)} | Ошибок: {result.get('failed_count', 0)}")


if __name__ == "__main__":
    asyncio.run(main())
