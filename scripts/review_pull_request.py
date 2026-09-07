#!/usr/bin/env python3
"""
Review an Azure DevOps Pull Request with Claude from the command line - no
UI/server needed.

Builds a line-numbered diff for the PR's latest iteration and asks Claude to
review it for bugs, inconsistencies and other functional issues. Always
analyzes first; pass --apply to also post the findings as PR line comments
(comments are attributed to whichever account owns AZURE_DEVOPS_PAT in .env).

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/review_pull_request.py --repo my-repo --pr 1234
    PYTHONPATH=$(pwd) venv/bin/python scripts/review_pull_request.py --repo my-repo --pr 1234 --apply

Prerequisites: AZURE_DEVOPS_*, ANTHROPIC_* configured in .env.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from cli_common import build_azure_client, build_anthropic_client, load_env
from packages.workflows import review

DATA_DIR = Path(__file__).parent / "data"
_SEVERITY_ICON = {"critical": "🔴", "major": "🟠", "minor": "🟡"}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Review an Azure DevOps Pull Request with Claude (no UI).")
    parser.add_argument("--repo", required=True, help="Repository name (or ID)")
    parser.add_argument("--pr", required=True, type=int, help="Pull Request ID")
    parser.add_argument("--project", help="Azure DevOps project (defaults to AZURE_DEVOPS_PROJECT)")
    parser.add_argument(
        "--apply", action="store_true",
        help="Post the findings as PR line comments (default: analyze only, no comments posted)",
    )
    parser.add_argument(
        "--no-anonymize", action="store_true",
        help="Skip anonymization of diff content before sending it to Claude",
    )
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/PR-<id>_review.json)")
    args = parser.parse_args()

    load_env()
    azure_client = build_azure_client(args.project)
    anthropic_client = build_anthropic_client()

    for name, client in (("Azure DevOps", azure_client), ("Claude", anthropic_client)):
        ok, err = await client.test_connection()
        if not ok:
            print(f"[!] Не удалось подключиться к {name}: {err}")
            sys.exit(1)
        print(f"[OK] Подключение к {name} проверено.")

    result = await review.run_review_pull_request_workflow(
        azure_client=azure_client,
        llm_client=anthropic_client,
        repo=args.repo,
        pr_id=args.pr,
        no_anonymize=args.no_anonymize,
        log_fn=lambda level, message: print(f"[{level}] {message}"),
    )

    if result.get("status") == "failed":
        print(f"[!] {result.get('error')}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else DATA_DIR / f"PR-{args.pr}_review.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    findings = result.get("findings", [])
    print(f"\n=== {result.get('summary', '')} ===")
    for f in findings:
        icon = _SEVERITY_ICON.get(str(f.get("severity", "")).lower(), "⚪")
        print(f"- {icon} {f.get('file')}:{f.get('line')} - {f.get('comment', '')}")
    print(f"\nНайдено замечаний: {len(findings)}. Результат сохранён: {output_path}")

    if not args.apply:
        print("[i] Dry-run: комментарии НЕ отправлены в Azure DevOps. Запустите с --apply, чтобы опубликовать их.")
        return

    apply_result = await review.post_review_comments(
        azure_client, args.repo, args.pr, findings, result.get("files", [])
    )
    print(f"\n[OK] Опубликовано {apply_result['posted_count']}/{len(findings)} комментариев к строкам.")
    if apply_result["errors"]:
        print(f"[!] Ошибок: {apply_result['error_count']}")


if __name__ == "__main__":
    asyncio.run(main())
