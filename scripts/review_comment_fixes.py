#!/usr/bin/env python3
"""
Verify whether Azure DevOps PR review comments were actually fixed, from the
command line - no UI/server needed.

Looks at PR comment threads a colleague has already responded to (marked
fixed/closed, or with a reply) and asks Claude whether the underlying code
actually addresses what was raised. Always analyzes first; pass --apply to
also reply into (and reopen) threads flagged as not actually fixed.

Usage:
    PYTHONPATH=$(pwd) venv/bin/python scripts/review_comment_fixes.py --repo my-repo --pr 1234
    PYTHONPATH=$(pwd) venv/bin/python scripts/review_comment_fixes.py --repo my-repo --pr 1234 --apply

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
_VERDICT_ICON = {"fixed": "✅", "not_fixed": "❌", "unclear": "❓"}


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify with Claude whether PR review comments were actually fixed (no UI)."
    )
    parser.add_argument("--repo", required=True, help="Repository name (or ID)")
    parser.add_argument("--pr", required=True, type=int, help="Pull Request ID")
    parser.add_argument("--project", help="Azure DevOps project (defaults to AZURE_DEVOPS_PROJECT)")
    parser.add_argument(
        "--apply", action="store_true",
        help="Reply to and reopen threads flagged as not actually fixed (default: analyze only)",
    )
    parser.add_argument(
        "--no-anonymize", action="store_true",
        help="Skip anonymization of code/comments before sending them to Claude",
    )
    parser.add_argument("--output", help="Path to save the JSON result (default: scripts/data/PR-<id>_comment_fix.json)")
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

    result = await review.run_review_comment_fixes_workflow(
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

    output_path = Path(args.output) if args.output else DATA_DIR / f"PR-{args.pr}_comment_fix.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    contexts_by_id = {c["thread_id"]: c for c in result.get("contexts", [])}
    results = result.get("results", [])
    print()
    for r in results:
        ctx = contexts_by_id.get(r.get("thread_id"), {})
        icon = _VERDICT_ICON.get(r.get("verdict", ""), "❓")
        print(f"{icon} Тред #{r.get('thread_id')} - {ctx.get('path')}:{ctx.get('line')} - {r.get('verdict')}: {r.get('reasoning', '')}")

    not_fixed = [r for r in results if r.get("verdict") == "not_fixed"]
    print(f"\nПроверено тредов: {len(results)}, не исправлено: {len(not_fixed)}. Результат сохранён: {output_path}")

    if not args.apply:
        print("[i] Dry-run: ответы НЕ отправлены, треды НЕ переоткрыты. Запустите с --apply для этого.")
        return

    apply_result = await review.apply_comment_fix_results(
        azure_client, args.repo, args.pr, result.get("contexts", []), results,
    )
    print(f"\n[OK] Обработано {apply_result['handled_count']}/{len(not_fixed)} не исправленных тред(ов).")
    if apply_result["errors"]:
        print(f"[!] Ошибок: {apply_result['error_count']}")


if __name__ == "__main__":
    asyncio.run(main())
