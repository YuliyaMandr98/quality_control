#!/usr/bin/env python3
"""
Audit skip/todo/bug-flagged autotests from the command line - no UI/server needed.

Scans a Playwright/TS test repo for tests disabled with `.skip` or flagged with a
`todo` / `жду` comment or a bug reference (`MB-1234` / `МВ-1234`), asks Claude to work
out the actual reason for each (a nearby comment doesn't always describe that test's
own reason), and cross-checks referenced bugs against Jira so tests whose blocking bug
is already resolved show up in their own category. Read-only: never touches Jira or
the test files.

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/audit_skipped_tests.py
    PYTHONPATH=$(pwd) venv/bin/python scripts/audit_skipped_tests.py --tests-root /path/to/tests

Prerequisites: JIRA_*, ANTHROPIC_* configured in .env.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from cli_common import build_anthropic_client, build_jira_client, load_env
from packages.workflows import skipped_tests

DATA_DIR = Path(__file__).parent / "data"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Audit skip/todo/bug-flagged autotests (no UI).")
    parser.add_argument("--tests-root", default=skipped_tests.DEFAULT_TESTS_ROOT, help="Root directory to scan for *.spec.ts files")
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/skipped_tests_report.json)")
    args = parser.parse_args()

    load_env()
    jira_client = build_jira_client()
    anthropic_client = build_anthropic_client()

    for name, client in (("Jira", jira_client), ("Claude", anthropic_client)):
        ok, err = await client.test_connection()
        if not ok:
            print(f"[!] Не удалось подключиться к {name}: {err}")
            sys.exit(1)
        print(f"[OK] Подключение к {name} проверено.")

    result = await skipped_tests.run_skipped_tests_audit_workflow(
        jira_client=jira_client,
        llm_client=anthropic_client,
        tests_root=args.tests_root,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") == "failed":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / "skipped_tests_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    md_path = output_path.with_suffix(".md")
    md_path.write_text(skipped_tests.render_markdown_report(result), encoding="utf-8")

    summary = result.get("summary", {})
    print("\n=== SUMMARY ===")
    print(f"Файлов просканировано: {summary.get('files_scanned', 0)} | Кандидатов: {summary.get('total_candidates', 0)}")
    for key in skipped_tests.CATEGORY_ORDER:
        label = skipped_tests.CATEGORY_LABELS[key]
        print(f"  {label}: {summary.get(f'count_{key}', 0)}")
    print(f"Результат сохранён: {output_path}")
    print(f"Markdown-отчёт: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
