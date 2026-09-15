#!/usr/bin/env python3
"""
Review pasted test cases for a User Story for coverage completeness - no UI/server needed.

Fetches the specification page you paste (and, for API test cases, the technical
implementation page you paste) from Confluence, and asks Claude to review the test
cases you pass in for missing requirements, missing validations, missing alternative
scenarios and missing edge cases - tuned to whether the test cases are web, mobile or
API. Read-only: never writes to Confluence or anywhere else.

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/review_test_cases.py \\
        --us 20.1.1 --test-type web \\
        --spec-url "https://yourdomain.atlassian.net/wiki/spaces/SPACE/pages/123456789/US-20.1.1" \\
        --test-cases-file path/to/test_cases.txt

    # API test cases additionally require --tech-impl-url
    PYTHONPATH=$(pwd) venv/bin/python scripts/review_test_cases.py \\
        --us 20.1.1 --test-type api \\
        --spec-url "https://.../pages/123456789/US-20.1.1+API" \\
        --tech-impl-url "https://.../pages/987654321/Technical+design" \\
        --test-cases-file path/to/test_cases.csv

Prerequisites: CONFLUENCE_*, ANTHROPIC_* configured in .env.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from cli_common import build_anthropic_client, build_confluence_client, load_env
from packages.workflows import review_test_cases

DATA_DIR = Path(__file__).parent / "data"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Review test case coverage for a User Story (no UI).")
    parser.add_argument("--us", required=True, help="User Story number/code (used to label the report)")
    parser.add_argument("--test-type", required=True, choices=review_test_cases.TEST_TYPES, help="Test case type")
    parser.add_argument("--spec-url", required=True, help="Confluence link (or bare page ID) to the specification")
    parser.add_argument(
        "--tech-impl-url",
        help="Confluence link (or bare page ID) to the technical implementation (required for --test-type api)",
    )
    parser.add_argument(
        "--test-cases-file", required=True,
        help="Path to a text/CSV file containing the pasted test cases to review",
    )
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/review_test_cases_report.json)")
    args = parser.parse_args()

    if args.test_type == "api" and not args.tech_impl_url:
        print("[!] --tech-impl-url обязателен при --test-type api")
        sys.exit(1)

    test_cases_path = Path(args.test_cases_file)
    if not test_cases_path.is_file():
        print(f"[!] Файл с тест-кейсами не найден: {test_cases_path}")
        sys.exit(1)
    test_cases_text = test_cases_path.read_text(encoding="utf-8")

    load_env()
    confluence_client = build_confluence_client()
    anthropic_client = build_anthropic_client()

    for name, client in (("Confluence", confluence_client), ("Claude", anthropic_client)):
        ok, err = await client.test_connection()
        if not ok:
            print(f"[!] Не удалось подключиться к {name}: {err}")
            sys.exit(1)
        print(f"[OK] Подключение к {name} проверено.")

    result = await review_test_cases.run_review_test_cases_workflow(
        confluence_client=confluence_client,
        llm_client=anthropic_client,
        us=args.us,
        test_type=args.test_type,
        spec_url=args.spec_url,
        tech_impl_url=args.tech_impl_url,
        test_cases_text=test_cases_text,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") == "failed":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / "review_test_cases_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    txt_path = output_path.with_suffix(".txt")
    report_text = review_test_cases.render_text_report(result)
    txt_path.write_text(report_text, encoding="utf-8")

    print("\n=== ОТЧЁТ ===")
    print(report_text)
    print(f"\nJSON сохранён: {output_path}")
    print(f"Текстовый отчёт: {txt_path}")


if __name__ == "__main__":
    asyncio.run(main())
