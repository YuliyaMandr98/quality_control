"""Report all Failed/Blocked test points across the Mobile and Web UI test
suites in Azure DevOps Test Plans.

Read-only - no LLM, never writes to Azure DevOps. For each test point whose
outcome is Failed or Blocked, reports the test case id, its title, which
configuration it's failing/blocked on, and where it lives in the suite tree:
the immediate suite containing it ("subfolder", typically a User Story suite)
and that suite's own parent ("parent folder", typically the Epic suite one
level up) - mirrors the breadcrumb a QA engineer sees in the Test Plans UI.

Default scope is the two suites given directly from the Test Plans "Execute"
URLs for this project's Mobile and Web UI test suites:
- Mobile: https://dev.azure.com/FINCA-KG/MB20/_testPlans/execute?planId=438&suiteId=439
- Web:    https://dev.azure.com/FINCA-KG/MB20/_testPlans/execute?planId=15751&suiteId=17762
"""

import asyncio
from typing import Any, Callable, Optional

MOBILE_PLAN_ID = "438"
MOBILE_ROOT_SUITE_ID = "439"
WEB_PLAN_ID = "15751"
WEB_ROOT_SUITE_ID = "17762"

_FAILING_OUTCOMES = {"failed", "blocked"}
_SUITE_FETCH_CONCURRENCY = 8


def _chunk(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _suite_descendants(suites: list[dict[str, Any]], root_suite_id: str) -> list[str]:
    """Return the ids of `root_suite_id` and every suite nested under it (any
    depth). Suites carry their parent under `parent` (older `test/Plans` API)
    or `parentSuite` (newer `testplan/Plans` API) - checked in that order.
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


async def _scan_platform(
    azure_client,
    *,
    platform: str,
    plan_id: str,
    root_suite_id: str,
    log_fn: Optional[Callable[[str, str], None]],
    should_cancel_fn: Optional[Callable[[], bool]],
) -> tuple[list[dict[str, Any]], int, bool]:
    """Scan one platform's suite tree and return (rows, suites_scanned, canceled)."""

    def _log(level: str, msg: str) -> None:
        if log_fn:
            log_fn(level, f"[{platform}] {msg}")

    suites = await azure_client.fetch_suites(plan_id)
    if not suites:
        _log("ERROR", f"Не удалось получить suite тест-плана {plan_id} (пустой список)")
        return [], 0, False

    suite_by_id = {str(s["id"]): s for s in suites}
    if str(root_suite_id) not in suite_by_id:
        _log("ERROR", f"Suite {root_suite_id} не найден в тест-плане {plan_id}")
        return [], 0, False

    descendant_ids = _suite_descendants(suites, root_suite_id)
    suites_with_cases = [sid for sid in descendant_ids if suite_by_id[sid].get("testCaseCount", 0) > 0]
    _log("INFO", f"Suite в дереве: {len(descendant_ids)}, с тест-кейсами: {len(suites_with_cases)}")

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

    org_url = str(getattr(azure_client, "org_url", "") or "").rstrip("/")
    project = str(getattr(azure_client, "project", "") or "")

    rows: list[dict[str, Any]] = []
    for p in all_points:
        outcome = str((p.get("results") or {}).get("outcome", "")).lower()
        if outcome not in _FAILING_OUTCOMES:
            continue

        case_ref = p.get("testCaseReference") or {}
        suite_ref = p.get("testSuite") or {}
        subfolder_id = str(suite_ref.get("id") or "")
        subfolder_name = suite_ref.get("name") or suite_by_id.get(subfolder_id, {}).get("name", "")

        parent_suite = suite_by_id.get(subfolder_id, {})
        parent_ref = parent_suite.get("parent") or parent_suite.get("parentSuite") or {}
        parent_id = str(parent_ref.get("id") or "")
        parent_name = parent_ref.get("name") or suite_by_id.get(parent_id, {}).get("name", "")

        case_id = str(case_ref.get("id") or "")
        rows.append({
            "platform": platform,
            "case_id": case_id,
            "case_title": case_ref.get("name") or "",
            "outcome": outcome.capitalize(),
            "configuration": (p.get("configuration") or {}).get("name", ""),
            "parent_folder": parent_name,
            "subfolder": subfolder_name,
            "url": f"{org_url}/{project}/_workitems/edit/{case_id}" if org_url and project and case_id else "",
        })

    _log(
        "INFO",
        ("Остановлено пользователем. " if canceled else "Готово: ")
        + f"Failed/Blocked найдено: {len(rows)}",
    )
    return rows, processed, canceled


async def run_failed_blocked_test_cases_report_workflow(
    azure_client,
    *,
    include_mobile: bool = True,
    include_web: bool = True,
    mobile_plan_id: str = MOBILE_PLAN_ID,
    mobile_root_suite_id: str = MOBILE_ROOT_SUITE_ID,
    web_plan_id: str = WEB_PLAN_ID,
    web_root_suite_id: str = WEB_ROOT_SUITE_ID,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Scan the Mobile and/or Web UI test suites for Failed/Blocked test
    points and report each one's test case id/title/configuration and its
    location in the suite tree (parent folder + subfolder).
    """

    def _log(level: str, msg: str) -> None:
        if log_fn:
            log_fn(level, msg)

    if not include_mobile and not include_web:
        return {"status": "failed", "error": "Нужно выбрать хотя бы одну платформу: Mobile или Web"}

    _log(
        "INFO",
        f"Поиск Failed/Blocked тест-кейсов: Mobile={include_mobile}, Web={include_web}",
    )

    if should_cancel_fn and should_cancel_fn():
        return {"status": "canceled", "error": "Остановлено пользователем"}

    rows: list[dict[str, Any]] = []
    platforms_scanned: list[str] = []
    canceled = False

    if include_mobile:
        platform_rows, _, platform_canceled = await _scan_platform(
            azure_client,
            platform="Mobile",
            plan_id=mobile_plan_id,
            root_suite_id=mobile_root_suite_id,
            log_fn=log_fn,
            should_cancel_fn=should_cancel_fn,
        )
        rows.extend(platform_rows)
        platforms_scanned.append("Mobile")
        if platform_canceled:
            canceled = True

    if include_web and not canceled:
        platform_rows, _, platform_canceled = await _scan_platform(
            azure_client,
            platform="Web",
            plan_id=web_plan_id,
            root_suite_id=web_root_suite_id,
            log_fn=log_fn,
            should_cancel_fn=should_cancel_fn,
        )
        rows.extend(platform_rows)
        platforms_scanned.append("Web")
        if platform_canceled:
            canceled = True

    summary = {
        "platforms_scanned": platforms_scanned,
        "mobile_plan_id": mobile_plan_id,
        "mobile_root_suite_id": mobile_root_suite_id,
        "web_plan_id": web_plan_id,
        "web_root_suite_id": web_root_suite_id,
        "total_count": len(rows),
        "failed_count": sum(1 for r in rows if r["outcome"] == "Failed"),
        "blocked_count": sum(1 for r in rows if r["outcome"] == "Blocked"),
    }
    _log(
        "INFO",
        ("Остановлено пользователем. " if canceled else "Готово: ")
        + f"всего Failed/Blocked={len(rows)} (Failed={summary['failed_count']}, Blocked={summary['blocked_count']})",
    )

    return {
        "status": "canceled" if canceled else "succeeded",
        "error": "Остановлено пользователем" if canceled else None,
        "summary": summary,
        "results": rows,
        "correlation_id": correlation_id,
    }


def render_text_report(result: dict[str, Any]) -> str:
    """Render the report as human-readable text."""
    if result.get("status") not in ("succeeded", "canceled"):
        return f"Отчёт не построен: {result.get('error', 'неизвестная ошибка')}"

    summary = result.get("summary", {})
    lines: list[str] = []
    lines.append("FAILED/BLOCKED ТЕСТ-КЕЙСЫ (Mobile + Web)")
    lines.append(f"Платформы: {', '.join(summary.get('platforms_scanned', []))}")
    lines.append(
        f"Всего: {summary.get('total_count', 0)} "
        f"(Failed: {summary.get('failed_count', 0)}, Blocked: {summary.get('blocked_count', 0)})"
    )
    lines.append("")

    for platform in summary.get("platforms_scanned", []):
        rows = [r for r in result.get("results", []) if r["platform"] == platform]
        lines.append(f"{platform.upper()} ({len(rows)})")
        if not rows:
            lines.append("  Нет.")
        for row in rows:
            lines.append(
                f"  - {row['case_id']} [{row['outcome']}, {row['configuration']}]: {row['case_title']} "
                f"— {row['parent_folder']} / {row['subfolder']}"
            )
        lines.append("")

    return "\n".join(lines).rstrip()


__all__ = [
    "MOBILE_PLAN_ID",
    "MOBILE_ROOT_SUITE_ID",
    "WEB_PLAN_ID",
    "WEB_ROOT_SUITE_ID",
    "run_failed_blocked_test_cases_report_workflow",
    "render_text_report",
]
