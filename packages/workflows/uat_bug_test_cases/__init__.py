"""Convert UAT bugs found by the customer into Azure DevOps regression test cases.

Fetches bugs matched by a JQL query (Phase-1 UAT bugs by default), keeps only
those in status "PASSED" (confirmed fixed and ready for regression - every
other status, including "Отменено", is not yet actionable), resolves the "UAT
Bugs" suite under the highest-numbered "Sprint N" folder in the Regression
suite tree, and creates one Test Case per bug that doesn't already have one
there (matched by an "MB-XXXX" key found in existing test case titles - no
duplicates across repeated runs).

The user clones the whole Sprint folder structure (including an empty "UAT
Bugs" suite) by hand at the start of each new sprint (starting Sprint 23) -
this workflow only ever targets the latest such folder, it never creates suites
itself. If the latest sprint has no "UAT Bugs" suite yet, it fails with a clear
message asking the user to create it first.

Each generated test case:
- Title and description both contain the bug's Jira key ("MB-XXXX: <summary>").
- A clickable link back to the Jira bug plus the bug's precondition (Test Case
  has no dedicated precondition field on this process template, so this
  doubles as one) is posted in TWO places: the System.Description field (only
  visible under the "Summary" tab of the classic Test Case form) AND as a
  Discussion comment (visible regardless of which tab is active) - see
  AzureDevOpsClient.create_test_case_in_suite for why both are needed.
- Steps are the bug's reproduction steps, with the expected result on the
  final step - regression-testing the fix means walking the same steps and
  now seeing the originally-expected (not the buggy) behavior.

Real bug descriptions vary too much in formatting (real headings, inline bold
labels, plain "1. 2. 3." text, or no structure at all) for a reliable regex/
heading parser, so an LLM (`AnthropicClient.extract_bug_repro_steps`) extracts
precondition/steps/expected-result from the flattened description text.

Always previews first (`dry_run=True` default), same convention as
upload_test_cases - review the list of test cases that would be created before
writing anything to Azure DevOps.
"""

import re
from typing import Any, Callable, Optional

from packages.common import get_logger

logger = get_logger(__name__)

DEFAULT_JQL = 'project = MB and issuetype in (Bug) and Phasa in (1) ORDER BY parent ASC'
DEFAULT_PLAN_ID = "12296"
DEFAULT_ROOT_SUITE_ID = "12297"  # "Regression" suite - parent of all "Sprint N" folders
UAT_BUGS_SUITE_NAME = "UAT Bugs"
DEFAULT_PRIORITY = "Medium"
DEFAULT_STATE = "Ready"

# Client-side filter, matched case-insensitively against the Jira status name -
# only bugs the customer/QA has confirmed fixed (status "PASSED") are ready to
# become regression test cases. Never trust JQL string-literal status filtering
# passed through URL query params - it has been observed to silently not filter
# as expected, especially with Cyrillic status names.
_REQUIRED_STATUS = "passed"

_BUG_KEY_PATTERN = re.compile(r"\bMB-\d+\b")
_SPRINT_NAME_PATTERN = re.compile(r"sprint\s*(\d+)", re.IGNORECASE)

JIRA_FIELDS_TO_FETCH = "summary,status,description"


class UatBugTestCasesError(Exception):
    """Raised when the target Azure DevOps suite can't be resolved."""


def _adf_to_text(node: Any) -> str:
    """Flatten a Jira ADF description into readable structured plain text
    (headings marked, list items as "- " bullets, paragraph breaks preserved).
    Real bug descriptions are too inconsistent in ADF shape for a strict
    section parser - this just makes reasonable, LLM-friendly input text.
    """
    out: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            t = n.get("type")
            if t == "heading":
                out.append("\n### ")
            elif t == "text":
                out.append(n.get("text", ""))
                return
            elif t == "hardBreak":
                out.append("\n")
            elif t == "listItem":
                out.append("\n- ")
            elif t == "paragraph":
                out.append("\n")
            for c in n.get("content") or []:
                walk(c)
        elif isinstance(n, list):
            for c in n:
                walk(c)

    walk(node or {})
    return "".join(out).strip()


def _is_passed_status(status_name: str) -> bool:
    return (status_name or "").strip().lower() == _REQUIRED_STATUS


def _find_sprint_suites(suites: list[dict[str, Any]], root_suite_id: str) -> list[tuple[int, dict[str, Any]]]:
    sprint_suites: list[tuple[int, dict[str, Any]]] = []
    for suite in suites:
        parent_id = str((suite.get("parent") or {}).get("id") or "")
        if parent_id != str(root_suite_id):
            continue
        match = _SPRINT_NAME_PATTERN.search(str(suite.get("name") or ""))
        if match:
            sprint_suites.append((int(match.group(1)), suite))
    return sprint_suites


def _find_uat_bugs_suite(suites: list[dict[str, Any]], sprint_suite_id: str) -> Optional[dict[str, Any]]:
    return next(
        (
            s for s in suites
            if str((s.get("parent") or {}).get("id") or "") == sprint_suite_id
            and str(s.get("name") or "").strip().lower() == UAT_BUGS_SUITE_NAME.lower()
        ),
        None,
    )


async def list_available_sprints(azure_client, plan_id: str, root_suite_id: str) -> list[dict[str, Any]]:
    """List "Sprint N" folders under `root_suite_id`, newest first, each flagged
    with whether it already has a "UAT Bugs" child suite ready to receive test
    cases. Powers the sprint dropdown in the UI - the user picks a sprint
    instead of the workflow always auto-targeting the latest one.
    """
    suites = await azure_client.fetch_suites(plan_id)
    sprint_suites = _find_sprint_suites(suites, root_suite_id)
    result = []
    for sprint_number, sprint_suite in sorted(sprint_suites, key=lambda pair: pair[0], reverse=True):
        sprint_suite_id = str(sprint_suite["id"])
        uat_suite = _find_uat_bugs_suite(suites, sprint_suite_id)
        result.append({
            "sprint_number": sprint_number,
            "sprint_name": str(sprint_suite.get("name") or ""),
            "sprint_suite_id": sprint_suite_id,
            "has_uat_bugs_suite": uat_suite is not None,
            "uat_bugs_suite_id": str(uat_suite["id"]) if uat_suite else None,
        })
    return result


async def _resolve_target_suite(
    azure_client,
    plan_id: str,
    root_suite_id: str,
    sprint_number: Optional[int] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
) -> dict[str, Any]:
    """Find the "UAT Bugs" suite under a "Sprint N" folder directly under
    `root_suite_id` - the one named `sprint_number` if given, otherwise the
    highest-numbered one. Raises UatBugTestCasesError if no matching sprint
    folder is found, or it has no "UAT Bugs" child yet (the user creates that
    by cloning the previous sprint's folder structure by hand).
    """
    def _log(msg: str) -> None:
        if log_fn:
            log_fn("DEBUG", msg)

    suites = await azure_client.fetch_suites(plan_id)
    sprint_suites = _find_sprint_suites(suites, root_suite_id)

    if not sprint_suites:
        raise UatBugTestCasesError(
            f"Под корневым suite {root_suite_id} не найдено ни одной папки 'Sprint N'."
        )

    if sprint_number is not None:
        matching = [pair for pair in sprint_suites if pair[0] == sprint_number]
        if not matching:
            available = ", ".join(str(n) for n, _ in sorted(sprint_suites, reverse=True))
            raise UatBugTestCasesError(
                f"Папка 'Sprint {sprint_number}' не найдена под suite {root_suite_id}. "
                f"Доступные спринты: {available}."
            )
        resolved_number, sprint_suite = matching[0]
    else:
        resolved_number, sprint_suite = max(sprint_suites, key=lambda pair: pair[0])

    sprint_suite_id = str(sprint_suite["id"])
    _log(f"Целевой спринт: '{sprint_suite.get('name')}' (id={sprint_suite_id})")

    uat_suite = _find_uat_bugs_suite(suites, sprint_suite_id)
    if not uat_suite:
        raise UatBugTestCasesError(
            f"В папке '{sprint_suite.get('name')}' (id={sprint_suite_id}) ещё нет suite "
            f"'{UAT_BUGS_SUITE_NAME}' — создайте его (клонированием структуры предыдущего "
            f"спринта) перед запуском воркфлоу."
        )

    return {
        "sprint_number": resolved_number,
        "sprint_name": str(sprint_suite.get("name") or ""),
        "sprint_suite_id": sprint_suite_id,
        "suite_id": str(uat_suite["id"]),
        "suite_name": str(uat_suite.get("name") or ""),
    }


def _build_link_html(bug_key: str, bug_url: str, precondition: str) -> str:
    """Shared HTML content for both System.Description (under the "Summary"
    tab of the Test Case form - not shown by default) and a Discussion
    comment (shown regardless of which tab is active - see
    AzureDevOpsClient.add_work_item_comment) - posting the same clickable
    link+precondition to both means it's easy to find no matter where you
    look on the work item.
    """
    parts = [f'<p>Баг: <a href="{bug_url}">{bug_key}</a></p>']
    if precondition:
        escaped = precondition.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        parts.append(f"<p><strong>Предусловие:</strong> {escaped}</p>")
    return "".join(parts)


async def create_test_case_for_row(
    azure_client,
    *,
    plan_id: str,
    suite_id: str,
    priority: str,
    state: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Create a single Azure DevOps test case from a preview row - the same
    shape a dry-run's `results` entries have (key, url, title, precondition,
    steps, expected_result). Used both by the live (non-dry-run) workflow loop
    and by the "Apply" action on an already-previewed dry run, so applying
    from the UI reproduces exactly what a live run would have created without
    re-fetching Jira or re-running LLM extraction.
    """
    key = str(row.get("key") or "")
    bug_url = str(row.get("url") or "")
    precondition = str(row.get("precondition") or "")
    title = str(row.get("title") or key)
    steps = row.get("steps") or [title]
    expected_result = str(row.get("expected_result") or "")
    azure_steps = [
        {"action": step_text, "expected": expected_result if i == len(steps) else ""}
        for i, step_text in enumerate(steps, 1)
    ]
    link_html = _build_link_html(key, bug_url, precondition)
    return await azure_client.create_test_case_in_suite(
        test_plan_id=plan_id,
        suite_id=suite_id,
        title=title,
        priority=priority,
        steps=azure_steps,
        state=state,
        description=link_html,
        comment_html=link_html,
    )


async def run_uat_bug_test_cases_workflow(
    jira_client,
    azure_client,
    llm_client,
    *,
    jql: str = DEFAULT_JQL,
    plan_id: str = DEFAULT_PLAN_ID,
    root_suite_id: str = DEFAULT_ROOT_SUITE_ID,
    sprint_number: Optional[int] = None,
    max_results: int = 200,
    priority: str = DEFAULT_PRIORITY,
    state: str = DEFAULT_STATE,
    dry_run: bool = True,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Create Azure DevOps test cases for UAT bugs that don't have one yet.

    Always previews first (`dry_run=True`): resolves the target suite, fetches
    and classifies bugs, and runs the LLM extraction for every new bug so the
    preview is a genuine dry run of the real output - only the final
    create_test_case_in_suite call is skipped when dry_run=True.
    """

    def _log(level: str, msg: str) -> None:
        logger.info(msg) if level == "INFO" else logger.warning(msg) if level == "WARNING" else logger.debug(msg)
        if log_fn:
            log_fn(level, msg)

    _log("INFO", f"UAT bug test-case generation started: jql={jql!r}, dry_run={dry_run}")

    if should_cancel_fn and should_cancel_fn():
        _log("WARNING", "Остановлено пользователем")
        return {"status": "canceled", "error": "Остановлено пользователем"}

    try:
        issues = await jira_client.fetch_bugs(jql, max_results=max_results, fields=JIRA_FIELDS_TO_FETCH)
    except Exception as exc:
        _log("ERROR", f"Failed to fetch bugs from Jira: {exc}")
        return {"status": "failed", "error": str(exc)}

    _log("INFO", f"Получено багов из Jira: {len(issues)}")

    active_bugs = [
        issue for issue in issues
        if _is_passed_status(((issue.get("fields") or {}).get("status") or {}).get("name", ""))
    ]
    excluded_count = len(issues) - len(active_bugs)
    _log("INFO", f"В статусе PASSED: {len(active_bugs)}, отброшено (другой статус): {excluded_count}")

    if should_cancel_fn and should_cancel_fn():
        _log("WARNING", "Остановлено пользователем")
        return {"status": "canceled", "error": "Остановлено пользователем"}

    try:
        target = await _resolve_target_suite(
            azure_client, plan_id, root_suite_id, sprint_number=sprint_number, log_fn=_log,
        )
    except UatBugTestCasesError as exc:
        _log("ERROR", str(exc))
        return {"status": "failed", "error": str(exc)}

    _log(
        "INFO",
        f"Целевой suite: '{target['sprint_name']}' → '{target['suite_name']}' (id={target['suite_id']})",
    )

    existing_test_cases = await azure_client.fetch_test_cases_for_suite(plan_id, target["suite_id"])
    existing_keys: set[str] = set()
    for tc in existing_test_cases:
        name = str((tc.get("workItem") or {}).get("name") or "")
        match = _BUG_KEY_PATTERN.search(name)
        if match:
            existing_keys.add(match.group(0))
    _log(
        "INFO",
        f"В целевом suite уже есть тест-кейсов: {len(existing_test_cases)} "
        f"(распознано багов: {len(existing_keys)})",
    )

    new_bugs = [issue for issue in active_bugs if str(issue.get("key") or "") not in existing_keys]
    _log("INFO", f"Новых багов без тест-кейса: {len(new_bugs)}")

    jira_base_url = str(getattr(jira_client, "base_url", "") or "")
    results: list[dict[str, Any]] = []
    created_count = 0
    failed_count = 0
    canceled = False

    for idx, issue in enumerate(new_bugs, 1):
        if should_cancel_fn and should_cancel_fn():
            _log("WARNING", f"Остановлено пользователем после {idx - 1}/{len(new_bugs)} багов")
            canceled = True
            break

        key = str(issue.get("key") or "")
        fields = issue.get("fields") or {}
        summary = str(fields.get("summary") or "")
        status_name = str((fields.get("status") or {}).get("name") or "")
        bug_url = f"{jira_base_url.rstrip('/')}/browse/{key}" if key else ""

        _log("INFO", f"[{idx}/{len(new_bugs)}] Обрабатываю {key}: {summary[:70]}")

        description_text = _adf_to_text(fields.get("description"))
        try:
            extracted = await llm_client.extract_bug_repro_steps(key, summary, description_text)
        except Exception as exc:
            _log("WARNING", f"  Не удалось извлечь шаги для {key}: {exc}")
            extracted = {"precondition": "", "steps": [summary or key], "expected_result": ""}

        title = f"{key}: {summary}"
        steps = extracted.get("steps") or [summary or key]
        expected_result = extracted.get("expected_result", "")

        row: dict[str, Any] = {
            "key": key,
            "url": bug_url,
            "title": title,
            "status": status_name,
            "precondition": extracted.get("precondition", ""),
            "steps": steps,
            "expected_result": expected_result,
        }

        if dry_run:
            row["result"] = {"would_create": True}
        else:
            create_result = await create_test_case_for_row(
                azure_client, plan_id=plan_id, suite_id=target["suite_id"],
                priority=priority, state=state, row=row,
            )
            row["result"] = create_result
            if create_result.get("success"):
                created_count += 1
                _log("INFO", f"  Создан ТК {create_result.get('case_id')} для {key}")
            else:
                failed_count += 1
                _log("ERROR", f"  Ошибка создания ТК для {key}: {create_result.get('error')}")

        results.append(row)

    _log(
        "INFO",
        f"Готово{'  (остановлено)' if canceled else ''}: "
        f"{'создано' if not dry_run else 'в предпросмотре'}={len(results)}"
        + (f", ошибок={failed_count}" if not dry_run else ""),
    )

    summary_data = {
        "jql": jql,
        "bugs_fetched": len(issues),
        "excluded_not_passed": excluded_count,
        "passed_bugs": len(active_bugs),
        "existing_in_suite": len(existing_test_cases),
        "new_bugs": len(new_bugs),
        "processed": len(results),
        "created_count": created_count,
        "failed_count": failed_count,
        "dry_run": dry_run,
        "target_suite": target,
    }

    return {
        "status": "canceled" if canceled else "succeeded",
        "error": "Остановлено пользователем" if canceled else None,
        "summary": summary_data,
        "results": results,
        "correlation_id": correlation_id,
    }


def render_text_report(result: dict[str, Any]) -> str:
    """Render the run result as a human-readable text report."""
    if result.get("status") != "succeeded":
        return f"Воркфлоу не выполнен: {result.get('error', 'неизвестная ошибка')}"

    summary = result.get("summary", {})
    target = summary.get("target_suite", {})
    lines: list[str] = []
    lines.append("ТЕСТ-КЕЙСЫ ИЗ UAT-БАГОВ")
    lines.append(f"JQL: {summary.get('jql', '')}")
    lines.append(f"Целевой suite: {target.get('sprint_name', '')} → {target.get('suite_name', '')} (id={target.get('suite_id', '')})")
    lines.append(
        f"Багов получено: {summary.get('bugs_fetched', 0)} | В статусе PASSED: {summary.get('passed_bugs', 0)} | "
        f"Отброшено (другой статус): {summary.get('excluded_not_passed', 0)} | Уже в suite: {summary.get('existing_in_suite', 0)} | "
        f"Новых: {summary.get('new_bugs', 0)}"
    )
    mode = "ПРЕДПРОСМОТР (ничего не записано)" if summary.get("dry_run") else (
        f"СОЗДАНО: {summary.get('created_count', 0)} | ОШИБОК: {summary.get('failed_count', 0)}"
    )
    lines.append(f"Режим: {mode}")
    lines.append("")

    for row in result.get("results", []):
        lines.append(f"— {row['key']} — {row['title']}")
        if row.get("precondition"):
            lines.append(f"  Предусловие: {row['precondition']}")
        for i, step in enumerate(row.get("steps", []), 1):
            lines.append(f"  {i}. {step}")
        if row.get("expected_result"):
            lines.append(f"  Ожидаемый результат: {row['expected_result']}")
        res = row.get("result") or {}
        if res.get("success") is False:
            lines.append(f"  ОШИБКА: {res.get('error')}")
        lines.append("")

    return "\n".join(lines)


__all__ = [
    "DEFAULT_JQL",
    "DEFAULT_PLAN_ID",
    "DEFAULT_ROOT_SUITE_ID",
    "UAT_BUGS_SUITE_NAME",
    "DEFAULT_PRIORITY",
    "DEFAULT_STATE",
    "UatBugTestCasesError",
    "list_available_sprints",
    "create_test_case_for_row",
    "run_uat_bug_test_cases_workflow",
    "render_text_report",
]
