"""Backlog bug field/link completeness audit.

Checks bugs matched by a JQL query (Backlog bugs by default) for:
- Required fields filled: Фаза, Метки, Компоненты, ENV (Полигон), Team.
- At least one "is Bug for" (or "blocks" - either counts) link to a User Story
  ("История") issue.
- At least one "is Bug for" (or "blocks") link to a QA task issue (any issue
  type whose name contains "QA" - this Jira instance has several per
  team/platform, e.g. "QA MB task", "QA WEB auto task", "QA API task").

Reference for a bug that passes every check: MB-6419.

Read-only: only reads from Jira, never writes anything back.
"""

from typing import Any, Callable, Optional

from packages.common import get_logger

logger = get_logger(__name__)

DEFAULT_JQL = 'issuetype in ("BE BUG", "Mobile bug", Bug, "FE bug") AND status = Backlog'

# Custom field IDs on this Jira instance (see packages/integrations/jira - same
# pattern as triage's DEFAULT_SEVERITY_FIELD_ID/DEFAULT_IMPACT_FIELD_ID).
PHASE_FIELD_ID = "customfield_10562"  # Фаза
ENV_FIELD_ID = "customfield_11111"  # ENV (полигон)
TEAM_FIELD_ID = "customfield_10001"  # Team

FIELDS_TO_FETCH = f"summary,issuetype,status,labels,components,{PHASE_FIELD_ID},{ENV_FIELD_ID},{TEAM_FIELD_ID},issuelinks"

REQUIRED_FIELD_CHECKS: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
    ("Фаза", lambda f: bool(f.get(PHASE_FIELD_ID))),
    ("Метки", lambda f: bool(f.get("labels"))),
    ("Компоненты", lambda f: bool(f.get("components"))),
    ("ENV (Полигон)", lambda f: bool(f.get(ENV_FIELD_ID))),
    ("Team", lambda f: bool(f.get(TEAM_FIELD_ID))),
]

_STORY_TYPE_MARKER = "история"
_QA_TYPE_MARKER = "qa"

# Either link type's outward phrase counts - a bug can be tied to its Story/QA
# task via the dedicated "Bugs" link type ("is Bug for") or via a "Blocks"
# link ("blocks"), interchangeably (per user confirmation - either/or).
_QUALIFYING_OUTWARD_PHRASES = {"is bug for", "blocks"}


def _qualifying_links(fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the issues this bug is linked to via a qualifying outward link
    (see `_QUALIFYING_OUTWARD_PHRASES`), each as {"key": ..., "type_name": ...}.
    Matches on the link type's outward phrase, not a specific link type id/name,
    so it still works if the instance ever adds a differently named link type
    with the same phrasing.
    """
    result: list[dict[str, Any]] = []
    for link in fields.get("issuelinks") or []:
        link_type = link.get("type") or {}
        outward_issue = link.get("outwardIssue")
        if outward_issue and str(link_type.get("outward", "")).strip().lower() in _QUALIFYING_OUTWARD_PHRASES:
            issue_type = ((outward_issue.get("fields") or {}).get("issuetype") or {}).get("name", "")
            result.append({"key": outward_issue.get("key"), "type_name": issue_type})
    return result


def _check_bug(issue: dict[str, Any], jira_base_url: str) -> dict[str, Any]:
    fields = issue.get("fields") or {}
    key = issue.get("key", "")

    missing_fields = [label for label, check in REQUIRED_FIELD_CHECKS if not check(fields)]

    linked = _qualifying_links(fields)
    has_story = any(_STORY_TYPE_MARKER in (l["type_name"] or "").lower() for l in linked)
    has_qa = any(_QA_TYPE_MARKER in (l["type_name"] or "").lower() for l in linked)

    missing_links: list[str] = []
    if not has_story:
        missing_links.append("История")
    if not has_qa:
        missing_links.append("QA")

    return {
        "key": key,
        "url": f"{jira_base_url.rstrip('/')}/browse/{key}" if key else "",
        "summary": fields.get("summary", ""),
        "issuetype": (fields.get("issuetype") or {}).get("name", ""),
        "status": (fields.get("status") or {}).get("name", ""),
        "missing_fields": missing_fields,
        "linked_types": [l["type_name"] for l in linked if l["type_name"]],
        "missing_links": missing_links,
        "is_valid": not missing_fields and not missing_links,
    }


async def run_bug_backlog_audit_workflow(
    jira_client,
    *,
    jql: str = DEFAULT_JQL,
    max_results: int = 100,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Fetch bugs matched by `jql` and check each for required-field and
    required-link completeness. Never writes to Jira.
    """

    def _log(level: str, msg: str) -> None:
        logger.info(msg) if level == "INFO" else logger.warning(msg) if level == "WARNING" else logger.debug(msg)
        if log_fn:
            log_fn(level, msg)

    _log("INFO", f"Bug backlog audit started: jql={jql!r}, max_results={max_results}")

    if should_cancel_fn and should_cancel_fn():
        _log("WARNING", "Остановлено пользователем")
        return {"status": "canceled", "error": "Остановлено пользователем"}

    try:
        issues = await jira_client.fetch_bugs(jql, max_results=max_results, fields=FIELDS_TO_FETCH)
    except Exception as exc:
        _log("ERROR", f"Failed to fetch bugs from Jira: {exc}")
        return {"status": "failed", "error": str(exc)}

    _log("INFO", f"Fetched {len(issues)} bug(s)")

    jira_base_url = str(getattr(jira_client, "base_url", "") or "")
    results: list[dict[str, Any]] = []
    canceled = False
    for idx, issue in enumerate(issues, 1):
        if should_cancel_fn and should_cancel_fn():
            _log("WARNING", f"Остановлено пользователем после {idx - 1}/{len(issues)} багов")
            canceled = True
            break
        results.append(_check_bug(issue, jira_base_url))

    valid_count = sum(1 for r in results if r["is_valid"])
    invalid_count = len(results) - valid_count

    summary = {
        "bugs_fetched": len(issues),
        "bugs_checked": len(results),
        "valid_count": valid_count,
        "invalid_count": invalid_count,
        "jql": jql,
    }

    _log(
        "INFO",
        f"Audit {'canceled' if canceled else 'complete'}: "
        f"valid={valid_count}, invalid={invalid_count}, checked={len(results)}/{len(issues)}",
    )

    return {
        "status": "canceled" if canceled else "succeeded",
        "error": "Остановлено пользователем" if canceled else None,
        "summary": summary,
        "results": results,
        "correlation_id": correlation_id,
    }


def render_text_report(result: dict[str, Any]) -> str:
    """Render the audit result as a human-readable text report."""
    if result.get("status") != "succeeded":
        return f"Аудит не выполнен: {result.get('error', 'неизвестная ошибка')}"

    summary = result.get("summary", {})
    lines: list[str] = []
    lines.append("АУДИТ БЭКЛОГ-БАГОВ (обязательные поля + связи)")
    lines.append(f"JQL: {summary.get('jql', '')}")
    lines.append(
        f"Проверено: {summary.get('bugs_checked', 0)}/{summary.get('bugs_fetched', 0)} | "
        f"Корректных: {summary.get('valid_count', 0)} | С замечаниями: {summary.get('invalid_count', 0)}"
    )
    lines.append("")

    results = result.get("results", [])
    invalid = [r for r in results if not r["is_valid"]]
    valid = [r for r in results if r["is_valid"]]

    lines.append(f"С ЗАМЕЧАНИЯМИ ({len(invalid)})")
    if not invalid:
        lines.append("  Нет.")
    for row in invalid:
        parts = []
        if row["missing_fields"]:
            parts.append(f"поля: {', '.join(row['missing_fields'])}")
        if row["missing_links"]:
            parts.append(f"связи: {', '.join(row['missing_links'])}")
        lines.append(f"  - {row['key']} — {row['summary']} [{'; '.join(parts)}]")
    lines.append("")

    lines.append(f"БЕЗ ЗАМЕЧАНИЙ ({len(valid)})")
    if not valid:
        lines.append("  Нет.")
    for row in valid:
        lines.append(f"  - {row['key']} — {row['summary']}")

    return "\n".join(lines)


__all__ = [
    "DEFAULT_JQL",
    "PHASE_FIELD_ID",
    "ENV_FIELD_ID",
    "TEAM_FIELD_ID",
    "run_bug_backlog_audit_workflow",
    "render_text_report",
]
