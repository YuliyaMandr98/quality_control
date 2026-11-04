#!/usr/bin/env python3
"""
Remove the Severity custom field from cancelled bugs - no UI/server needed.

Finds bugs matched by a JQL query (cancelled bugs in project MB by default)
that still have the Severity field set and clears it, since a cancelled bug
no longer needs a severity classification.

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/cancelled_bug_severity_cleanup.py
    PYTHONPATH=$(pwd) venv/bin/python scripts/cancelled_bug_severity_cleanup.py --apply
    PYTHONPATH=$(pwd) venv/bin/python scripts/cancelled_bug_severity_cleanup.py --jql "project = MB AND status = Cancelled" --max-results 50

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
from packages.workflows import cancelled_bug_severity_cleanup

DATA_DIR = Path(__file__).parent / "data"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Clear the Severity field on cancelled bugs.")
    parser.add_argument("--jql", default=cancelled_bug_severity_cleanup.DEFAULT_JQL, help="JQL query selecting cancelled bug issues")
    parser.add_argument("--max-results", type=int, default=200, help="Maximum bugs to check")
    parser.add_argument("--apply", action="store_true", help="Actually clear the Severity field in Jira (default: preview only)")
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/cancelled_bug_severity_cleanup_report.json)")
    args = parser.parse_args()

    load_env()
    jira_client = build_jira_client()

    ok, err = await jira_client.test_connection()
    if not ok:
        print(f"[!] Не удалось подключиться к Jira: {err}")
        sys.exit(1)
    print("[OK] Подключение к Jira проверено.")

    if not args.apply:
        print("[i] Режим предпросмотра (--apply не указан) - в Jira ничего не будет изменено.\n")

    result = await cancelled_bug_severity_cleanup.run_cancelled_bug_severity_cleanup_workflow(
        jira_client=jira_client,
        jql=args.jql,
        max_results=args.max_results,
        apply=args.apply,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") == "failed":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / "cancelled_bug_severity_cleanup_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    txt_path = output_path.with_suffix(".txt")
    report_text = cancelled_bug_severity_cleanup.render_text_report(result)
    txt_path.write_text(report_text, encoding="utf-8")

    print("\n=== ОТЧЁТ ===")
    print(report_text)
    print(f"\nJSON сохранён: {output_path}")
    print(f"Текстовый отчёт: {txt_path}")


if __name__ == "__main__":
    asyncio.run(main())
