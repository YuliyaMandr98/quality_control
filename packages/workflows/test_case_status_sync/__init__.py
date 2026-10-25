"""Sync Azure DevOps "macOS Apidog" test point statuses from the automated
test repo's actual `.skip` state.

Reality this closes a gap on: a bug breaks an automated API test, the test
case's "macOS Apidog" configuration point in Azure DevOps gets marked Failed,
the bug eventually gets fixed and the test goes back to passing in CI - but
nobody remembers to flip that Azure DevOps test point back to Passed by hand.

For every test point under a suite tree that is Failed for a given
configuration:
- Find the automated test whose title starts with that test case's numeric id
  (e.g. `test("6579: ...")`) in the local qa-api-tests repo.
- If found and NOT `.skip`/`.fixme`-disabled, set that point's outcome to
  Passed (only in apply mode - dry-run just previews it).
- If found but still disabled, leave it Failed (reported, not touched).
- If no automated test exists at all (not yet automated, or a
  "(Manual Only)" test case that never will be), leave it Failed and just
  report it - this is an expected, not an error, case.

Read-only on the qa-api-tests repo (only scanned, never written); writes to
Azure DevOps only when apply=True.
"""

import asyncio
import re
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_PLAN_ID = "2015"
DEFAULT_ROOT_SUITE_ID = "2016"
DEFAULT_CONFIGURATION_NAME = "macOS Apidog"
DEFAULT_TESTS_ROOT = "/Users/oadmin/PROJECTS/FINCA/qa-api-tests/tests"
DEFAULT_FILE_GLOB = "**/*.spec.ts"

MANUAL_ONLY_MARKER = "(manual only)"

_SUITE_FETCH_CONCURRENCY = 8

# Matches a top-level `test(`, `test.skip(`, `test.only(` or `test.fixme(`
# declaration whose first argument is the test's title string - deliberately
# does NOT match `test.describe(...)` since neither alternative in the group
# accepts "describe", so a describe block never gets mis-read as a test.
_TEST_START_RE = re.compile(r"^\s*test(\.skip|\.only|\.fixme)?\(\s*(['\"`])?(.*)$")
_QUOTED_STRING_RE = re.compile(r"^\s*(['\"`])(.*?)\1")
_ID_PREFIX_RE = re.compile(r"^(\d+):")

_DISABLED_VARIANTS = {"skip", "fixme"}


def _match_test_start(lines: list[str], i: int) -> Optional[tuple[str, str]]:
    """Return (variant, title) for the test declaration opened at line `i`,
    or None. Handles both the common single-line title (`test("123: ...", ...)`
    and the multi-line call form (`test(\\n  "123: ...",\\n  async () => {`).
    """
    match = _TEST_START_RE.match(lines[i])
    if not match:
        return None
    variant = (match.group(1) or "").lstrip(".") or "plain"
    quote_char, rest = match.group(2), match.group(3)

    if quote_char:
        return variant, rest.split(quote_char)[0]

    if rest.strip() != "":
        return None

    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j < len(lines):
        quoted = _QUOTED_STRING_RE.match(lines[j])
        if quoted:
            return variant, quoted.group(2)
    return None


def _scan_file(path: Path, root: Path) -> list[dict[str, Any]]:
    """Extract every id-prefixed test declaration from one .spec.ts file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    rel_path = str(path.relative_to(root))
    found: list[dict[str, Any]] = []
    for i in range(len(lines)):
        test_start = _match_test_start(lines, i)
        if not test_start:
            continue
        variant, title = test_start
        id_match = _ID_PREFIX_RE.match(title.strip())
        if not id_match:
            continue
        found.append({
            "case_id": id_match.group(1),
            "title": title.strip(),
            "variant": variant,
            "file": rel_path,
            "line": i + 1,
        })
    return found


def build_automated_test_index(tests_root: str, file_glob: str = DEFAULT_FILE_GLOB) -> dict[str, dict[str, Any]]:
    """Scan `tests_root` for *.spec.ts test declarations and index them by the
    numeric test case id their title starts with. First match wins for a
    duplicate id (shouldn't normally happen - ids are unique Azure DevOps
    work item ids).
    """
    root = Path(tests_root)
    index: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob(file_glob)):
        for entry in _scan_file(path, root):
            index.setdefault(entry["case_id"], entry)
    return index


def _suite_descendants(suites: list[dict[str, Any]], root_suite_id: str) -> list[str]:
    """Return the ids of `root_suite_id` and every suite nested under it
    (any depth). `AzureDevOpsClient.fetch_suites` uses the older `test/Plans`
    API, whose suites carry their parent under `parent` (the newer
    `testplan/Plans` API calls the same thing `parentSuite`) - checked in
    that order, with a fallback, so this keeps working against either shape.
    """
    children_by_parent: dict[str, list[str]] = {}
    for suite in suites:
        parent = suite.get("parent") or suite.get("parentSuite")
        parent_id = (parent or {}).get("id")
        if parent_id is not None:
            children_by_parent.setdefault(str(parent_id), []).append(str(suite["id"]))

    result: list[str] = []
    seen: set[str] = set()
    stack = [str(root_suite_id)]
    while stack:
        suite_id = stack.pop()
        if suite_id in seen:
            continue
        seen.add(suite_id)
        result.append(suite_id)
        stack.extend(children_by_parent.get(suite_id, []))
    return result


def _chunk(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def run_test_case_status_sync_workflow(
    azure_client,
    *,
    plan_id: str = DEFAULT_PLAN_ID,
    root_suite_id: str = DEFAULT_ROOT_SUITE_ID,
    configuration_name: str = DEFAULT_CONFIGURATION_NAME,
    tests_root: str = DEFAULT_TESTS_ROOT,
    file_glob: str = DEFAULT_FILE_GLOB,
    apply: bool = False,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    def _log(level: str, message: str) -> None:
        if log_fn:
            log_fn(level, message)

    root = Path(tests_root)
    if not root.is_dir():
        return {"status": "failed", "error": f"tests_root не найден или не является директорией: {tests_root}"}

    automated_index = build_automated_test_index(tests_root, file_glob)
    _log("INFO", f"Автотестов с числовым ID в заголовке найдено: {len(automated_index)}")

    if should_cancel_fn and should_cancel_fn():
        return {"status": "canceled", "error": "Остановлено пользователем"}

    suites = await azure_client.fetch_suites(plan_id)
    if not suites:
        return {"status": "failed", "error": f"Не удалось получить suite тест-плана {plan_id} (пустой список)"}

    suite_by_id = {str(s["id"]): s for s in suites}
    if str(root_suite_id) not in suite_by_id:
        return {"status": "failed", "error": f"Suite {root_suite_id} не найден в тест-плане {plan_id}"}

    descendant_ids = _suite_descendants(suites, root_suite_id)
    # Skip suites that are pure grouping nodes (no test cases of their own,
    # only child suites) - fetch_test_points_for_suite would just come back
    # empty for them, so this materially cuts the number of API calls.
    suites_with_cases = [sid for sid in descendant_ids if suite_by_id[sid].get("testCaseCount", 0) > 0]
    _log(
        "INFO",
        f"Suite в дереве (включая корень {root_suite_id}): {len(descendant_ids)}, "
        f"с тест-кейсами: {len(suites_with_cases)}",
    )

    semaphore = asyncio.Semaphore(_SUITE_FETCH_CONCURRENCY)

    async def _fetch_one(suite_id: str) -> list[dict[str, Any]]:
        async with semaphore:
            return await azure_client.fetch_test_points_for_suite(plan_id, suite_id)

    all_points: list[dict[str, Any]] = []
    canceled = False
    processed = 0
    for batch in _chunk(suites_with_cases, _SUITE_FETCH_CONCURRENCY):
        if should_cancel_fn and should_cancel_fn():
            canceled = True
            break
        batch_results = await asyncio.gather(*[_fetch_one(sid) for sid in batch])
        for points in batch_results:
            all_points.extend(points)
        processed += len(batch)
        _log("INFO", f"Проверено suite: {processed}/{len(suites_with_cases)}")

    failed_points = [
        p
        for p in all_points
        if (p.get("configuration") or {}).get("name") == configuration_name
        and str((p.get("results") or {}).get("outcome", "")).lower() == "failed"
    ]
    _log(
        "INFO",
        f"Test point(ов) со статусом Failed для конфигурации '{configuration_name}': {len(failed_points)}",
    )

    to_update: list[dict[str, Any]] = []
    still_skipped: list[dict[str, Any]] = []
    no_automated_test: list[dict[str, Any]] = []

    for p in failed_points:
        if should_cancel_fn and should_cancel_fn():
            canceled = True
            break

        case_ref = p.get("testCaseReference") or {}
        suite_ref = p.get("testSuite") or {}
        case_id = str(case_ref.get("id") or "")
        case_title = case_ref.get("name") or ""
        row: dict[str, Any] = {
            "case_id": case_id,
            "case_title": case_title,
            "suite_id": str(suite_ref.get("id") or ""),
            "suite_name": suite_ref.get("name") or "",
            "point_id": str(p.get("id") or ""),
        }

        match = automated_index.get(case_id)
        if match is None:
            row["is_manual_only"] = MANUAL_ONLY_MARKER in case_title.lower()
            no_automated_test.append(row)
            continue

        row["automated_test_file"] = match["file"]
        row["automated_test_line"] = match["line"]

        if match["variant"] in _DISABLED_VARIANTS:
            row["automated_test_variant"] = match["variant"]
            still_skipped.append(row)
            continue

        if apply:
            update_result = await azure_client.set_test_point_outcome(
                plan_id, row["suite_id"], row["point_id"], "Passed"
            )
            row["updated"] = bool(update_result.get("success"))
            if not row["updated"]:
                row["error"] = update_result.get("error")
        to_update.append(row)

    summary = {
        "plan_id": plan_id,
        "root_suite_id": root_suite_id,
        "configuration_name": configuration_name,
        "suites_in_tree": len(descendant_ids),
        "suites_scanned": processed,
        "suites_total": len(suites_with_cases),
        "failed_points_total": len(failed_points),
        "to_update_count": len(to_update),
        "updated_count": sum(1 for r in to_update if r.get("updated")) if apply else 0,
        "still_skipped_count": len(still_skipped),
        "no_automated_test_count": len(no_automated_test),
        "apply": apply,
    }
    _log(
        "INFO",
        ("Остановлено пользователем. " if canceled else "Готово: ")
        + f"к обновлению={len(to_update)}, ещё .skip={len(still_skipped)}, без автотеста={len(no_automated_test)}",
    )

    return {
        "status": "canceled" if canceled else "succeeded",
        "error": "Остановлено пользователем" if canceled else None,
        "summary": summary,
        "to_update": to_update,
        "still_skipped": still_skipped,
        "no_automated_test": no_automated_test,
        "correlation_id": correlation_id,
    }


def render_text_report(result: dict[str, Any]) -> str:
    """Render the sync result as a human-readable text report."""
    if result.get("status") not in ("succeeded", "canceled"):
        return f"Синхронизация не выполнена: {result.get('error', 'неизвестная ошибка')}"

    summary = result.get("summary", {})
    lines: list[str] = []
    lines.append("СИНХРОНИЗАЦИЯ СТАТУСОВ ТЕСТ-КЕЙСОВ (macOS Apidog)")
    lines.append(
        f"Plan {summary.get('plan_id')} / suite {summary.get('root_suite_id')} / "
        f"конфигурация '{summary.get('configuration_name')}'"
    )
    lines.append(f"Suite просканировано: {summary.get('suites_scanned', 0)}/{summary.get('suites_total', 0)}")
    lines.append(f"Test point(ов) Failed: {summary.get('failed_points_total', 0)}")
    mode = "ПРИМЕНЕНО" if summary.get("apply") else "ПРЕДПРОСМОТР (ничего не записано)"
    lines.append(f"Режим: {mode}")
    lines.append("")

    to_update = result.get("to_update", [])
    if summary.get("apply"):
        lines.append(f"ОБНОВЛЕНО НА PASSED ({summary.get('updated_count', 0)}/{len(to_update)})")
    else:
        lines.append(f"БУДЕТ ОБНОВЛЕНО НА PASSED ({len(to_update)})")
    if not to_update:
        lines.append("  Нет.")
    for row in to_update:
        status = ""
        if summary.get("apply"):
            status = " ✅" if row.get("updated") else f" ❌ {row.get('error', '')}"
        lines.append(
            f"  - {row['case_id']}: {row['case_title']} "
            f"[{row['automated_test_file']}:{row['automated_test_line']}]{status}"
        )
    lines.append("")

    still_skipped = result.get("still_skipped", [])
    lines.append(f"АВТОТЕСТ НАЙДЕН, НО ВСЁ ЕЩЁ .skip/.fixme — ОСТАВЛЕНО FAILED ({len(still_skipped)})")
    if not still_skipped:
        lines.append("  Нет.")
    for row in still_skipped:
        lines.append(
            f"  - {row['case_id']}: {row['case_title']} "
            f"[{row['automated_test_file']}:{row['automated_test_line']}, .{row['automated_test_variant']}]"
        )
    lines.append("")

    no_automated_test = result.get("no_automated_test", [])
    lines.append(f"АВТОТЕСТ НЕ НАЙДЕН ({len(no_automated_test)})")
    if not no_automated_test:
        lines.append("  Нет.")
    for row in no_automated_test:
        manual = " (Manual Only)" if row.get("is_manual_only") else ""
        lines.append(f"  - {row['case_id']}: {row['case_title']}{manual}")

    return "\n".join(lines)


__all__ = [
    "DEFAULT_PLAN_ID",
    "DEFAULT_ROOT_SUITE_ID",
    "DEFAULT_CONFIGURATION_NAME",
    "DEFAULT_TESTS_ROOT",
    "DEFAULT_FILE_GLOB",
    "build_automated_test_index",
    "run_test_case_status_sync_workflow",
    "render_text_report",
]
