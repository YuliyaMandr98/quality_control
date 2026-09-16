#!/usr/bin/env python3
"""
Audit backlog bugs for required-field and required-link completeness - no UI/server needed.

Checks each bug matched by a JQL query (Backlog bugs by default) for:
- Required fields filled: Фаза, Метки, Компоненты, ENV (Полигон), Team.
- At least one "is Bug for" (or "blocks" - either counts) link to a User Story
  ("История") issue.
- At least one "is Bug for" (or "blocks") link to a QA task issue.

Reference for a bug that passes every check: MB-6419
(https://fincabank-kg.atlassian.net/browse/MB-6419).

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/bug_backlog_audit.py
    PYTHONPATH=$(pwd) venv/bin/python scripts/bug_backlog_audit.py --jql "status = Backlog" --max-results 50

Read-only: never writes to Jira. Prerequisites: JIRA_* configured in .env.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from cli_common import build_jira_client, load_env
from packages.workflows import bug_backlog_audit

DATA_DIR = Path(__file__).parent / "data"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Audit backlog bugs for required fields/links (no UI).")
    parser.add_argument("--jql", default=bug_backlog_audit.DEFAULT_JQL, help="JQL query selecting bug issues to audit")
    parser.add_argument("--max-results", type=int, default=100, help="Maximum bugs to check")
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/bug_backlog_audit_report.json)")
    args = parser.parse_args()

    load_env()
    jira_client = build_jira_client()

    ok, err = await jira_client.test_connection()
    if not ok:
        print(f"[!] Не удалось подключиться к Jira: {err}")
        sys.exit(1)
    print("[OK] Подключение к Jira проверено.")

    result = await bug_backlog_audit.run_bug_backlog_audit_workflow(
        jira_client=jira_client,
        jql=args.jql,
        max_results=args.max_results,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") == "failed":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / "bug_backlog_audit_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    txt_path = output_path.with_suffix(".txt")
    report_text = bug_backlog_audit.render_text_report(result)
    txt_path.write_text(report_text, encoding="utf-8")

    print("\n=== ОТЧЁТ ===")
    print(report_text)
    print(f"\nJSON сохранён: {output_path}")
    print(f"Текстовый отчёт: {txt_path}")


if __name__ == "__main__":
    asyncio.run(main())
