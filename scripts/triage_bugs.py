#!/usr/bin/env python3
"""
Triage Jira bug tickets from the command line - no UI/server needed.

Uses Confluence User Story specs + Claude to classify severity/impact/priority
for each bug matched by a JQL query, and optionally applies the result back to
Jira (field updates + status transition + optional comment).

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/triage_bugs.py
    PYTHONPATH=$(pwd) venv/bin/python scripts/triage_bugs.py --apply --add-comment

Prerequisites: CONFLUENCE_*, JIRA_*, ANTHROPIC_* configured in .env.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from cli_common import build_confluence_client, build_anthropic_client, build_jira_client, load_env
from packages.workflows import triage

DATA_DIR = Path(__file__).parent / "data"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Triage Jira bugs via Confluence specs + Claude (no UI).")
    parser.add_argument("--jql", default=triage.DEFAULT_BUG_JQL, help="JQL query selecting bug issues to triage")
    parser.add_argument("--max-results", type=int, default=50, help="Maximum bugs to process")
    parser.add_argument(
        "--apply", action="store_true",
        help="Write severity/impact/priority + status transition to Jira (default: dry-run preview only)",
    )
    parser.add_argument(
        "--add-comment", action="store_true",
        help="When --apply, also add a triage reasoning comment to each Jira issue",
    )
    parser.add_argument("--severity-field-id", default=triage.DEFAULT_SEVERITY_FIELD_ID)
    parser.add_argument("--impact-field-id", default=triage.DEFAULT_IMPACT_FIELD_ID)
    parser.add_argument("--target-status", default=triage.DEFAULT_TARGET_STATUS)
    parser.add_argument("--batch-delay-seconds", type=int, default=10, help="Seconds to wait between Claude API calls")
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/triage_result.json)")
    args = parser.parse_args()

    load_env()
    jira_client = build_jira_client()
    confluence_client = build_confluence_client()
    anthropic_client = build_anthropic_client()

    for name, client in (("Jira", jira_client), ("Confluence", confluence_client), ("Claude", anthropic_client)):
        ok, err = await client.test_connection()
        if not ok:
            print(f"[!] Не удалось подключиться к {name}: {err}")
            sys.exit(1)
        print(f"[OK] Подключение к {name} проверено.")

    result = await triage.run_triage_bugs_workflow(
        jira_client=jira_client,
        confluence_client=confluence_client,
        llm_client=anthropic_client,
        jql=args.jql,
        max_results=args.max_results,
        apply=args.apply,
        add_comment=args.add_comment,
        severity_field_id=args.severity_field_id,
        impact_field_id=args.impact_field_id,
        target_status=args.target_status,
        batch_delay_seconds=args.batch_delay_seconds,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") == "failed":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / "triage_result.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = result.get("summary", {})
    print("\n=== SUMMARY ===")
    print(
        f"Fetched: {summary.get('bugs_fetched', 0)} | Triaged: {summary.get('bugs_triaged', 0)} | "
        f"Not real bug: {summary.get('skipped_not_real_bug', 0)} | Errors: {summary.get('errors', 0)}"
    )
    print(f"Результат сохранён: {output_path}")

    if not args.apply:
        print("[i] Dry-run: изменения НЕ отправлены в Jira. Запустите с --apply, чтобы применить их.")


if __name__ == "__main__":
    asyncio.run(main())
