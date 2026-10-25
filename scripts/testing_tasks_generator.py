#!/usr/bin/env python3
"""
Generate the missing QA testing tasks (API/Mobile/Web - test cases, manual
testing, automation) for a "История" (User Story) issue - no UI/server
needed.

Looks at the story's "is Tested by" linked testing tasks, figures out which
of the standard set are missing (an unprefixed existing task counts as the
earliest activity, test cases), and creates those - each a copy of the
story's own summary with the matching prefix (e.g. "[auto API]"), linked
back to the story the same way the reference tasks are.

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/testing_tasks_generator.py MB-3454
    PYTHONPATH=$(pwd) venv/bin/python scripts/testing_tasks_generator.py MB-3454 --apply
    PYTHONPATH=$(pwd) venv/bin/python scripts/testing_tasks_generator.py MB-3454 --no-mobile --no-web

Prerequisites: JIRA_* configured in .env.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from cli_common import build_jira_client, load_env
from packages.workflows import testing_tasks_generator

DATA_DIR = Path(__file__).parent / "data"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Generate missing QA testing tasks for a User Story.")
    parser.add_argument("story_key", help="Jira key of the 'История' (User Story) issue, e.g. MB-3454")
    parser.add_argument("--no-api", dest="include_api", action="store_false", help="Skip API testing tasks")
    parser.add_argument("--no-mobile", dest="include_mobile", action="store_false", help="Skip Mobile testing tasks")
    parser.add_argument("--no-web", dest="include_web", action="store_false", help="Skip Web testing tasks")
    parser.add_argument("--apply", action="store_true", help="Actually create missing issues in Jira (default: preview only)")
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/testing_tasks_generator_report.json)")
    parser.set_defaults(include_api=True, include_mobile=True, include_web=True)
    args = parser.parse_args()

    load_env()
    jira_client = build_jira_client()

    ok, err = await jira_client.test_connection()
    if not ok:
        print(f"[!] Не удалось подключиться к Jira: {err}")
        sys.exit(1)
    print("[OK] Подключение к Jira проверено.")

    if not args.apply:
        print("[i] Режим предпросмотра (--apply не указан) - в Jira ничего не будет создано.\n")

    result = await testing_tasks_generator.run_testing_tasks_generator_workflow(
        jira_client=jira_client,
        story_key=args.story_key,
        include_api=args.include_api,
        include_mobile=args.include_mobile,
        include_web=args.include_web,
        apply=args.apply,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") == "failed":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / "testing_tasks_generator_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    txt_path = output_path.with_suffix(".txt")
    report_text = testing_tasks_generator.render_text_report(result)
    txt_path.write_text(report_text, encoding="utf-8")

    print("\n=== ОТЧЁТ ===")
    print(report_text)
    print(f"\nJSON сохранён: {output_path}")
    print(f"Текстовый отчёт: {txt_path}")


if __name__ == "__main__":
    asyncio.run(main())
