"""Backlog bug field/link completeness audit.

Checks bugs matched by a JQL query (Backlog bugs by default) for two
DISTINCT kinds of problem, always reported separately (never merged into one
list) since "should be filled but isn't" and "should be empty but isn't" are
opposite failure modes and mixing them makes the report ambiguous:

- Missing (required but empty): Фаза, Метки, Компоненты, ENV (Полигон), Team -
  categorization fields that should be set as soon as a bug enters the
  backlog; at least one "is Bug for"/"blocks" link to a User Story
  ("История") issue; at least one such link to a QA task issue (any issue
  type whose name contains "QA" - this Jira instance has several per
  team/platform, e.g. "QA MB task", "QA WEB auto task", "QA API task").
- Extra (present but should be empty for a backlog bug not yet scheduled into
  work): Исходная оценка (estimation happens at planning time, not while
  still in the backlog - a zeroed-out value counts as empty, same as never
  set), Sprint, Available at Android/iOS/WEB app/AP WEB/AP BE/BE build; a
  "clones" ("клонирует задачу") outward link to another issue.

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
ORIGINAL_ESTIMATE_FIELD_ID = "timeoriginalestimate"  # Исходная оценка (system field, seconds)
SPRINT_FIELD_ID = "customfield_10020"  # Спринт (Sprint)
BUILD_FIELD_IDS = {
    "Available at Android build": "customfield_10964",
    "Available at iOS build": "customfield_11106",
    "Available at WEB app build": "customfield_11107",
    "Available at AP WEB build": "customfield_11108",
    "Available at AP BE build": "customfield_11109",
    "Available at BE build": "customfield_11110",
}

FIELDS_TO_FETCH = (
    f"summary,issuetype,status,reporter,labels,components,"
    f"{PHASE_FIELD_ID},{ENV_FIELD_ID},{TEAM_FIELD_ID},{ORIGINAL_ESTIMATE_FIELD_ID},"
    f"{SPRINT_FIELD_ID},{','.join(BUILD_FIELD_IDS.values())},issuelinks"
)

REQUIRED_FIELD_CHECKS: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
    ("Фаза", lambda f: bool(f.get(PHASE_FIELD_ID))),
    ("Метки", lambda f: bool(f.get("labels"))),
    ("Компоненты", lambda f: bool(f.get("components"))),
    ("ENV (Полигон)", lambda f: bool(f.get(ENV_FIELD_ID))),
    ("Team", lambda f: bool(f.get(TEAM_FIELD_ID))),
]

# Fields a backlog bug (not yet scheduled into a sprint/build) should NOT
# have filled - presence, not absence, is the problem here. Исходная оценка
# belongs here, not in REQUIRED_FIELD_CHECKS: estimation happens at planning
# time when a bug is pulled out of the backlog, same as Sprint/Available-at-
# build - a bug still sitting in the backlog shouldn't have one yet.
FORBIDDEN_FIELD_CHECKS: list[tuple[str, str]] = [
    ("Исходная оценка", ORIGINAL_ESTIMATE_FIELD_ID),
    ("Sprint", SPRINT_FIELD_ID),
    *BUILD_FIELD_IDS.items(),
]

_STORY_TYPE_MARKER = "история"
_QA_TYPE_MARKER = "qa"

# Either link type's outward phrase counts - a bug can be tied to its Story/QA
# task via the dedicated "Bugs" link type ("is Bug for") or via a "Blocks"
# link ("blocks"), interchangeably (per user confirmation - either/or).
_QUALIFYING_OUTWARD_PHRASES = {"is bug for", "blocks"}

# Standard Jira "Cloners" link type - its outward phrase is "clones" in the
# API regardless of UI locale (the Russian UI shows it as "клонирует
# задачу"). A bug carrying this link outward is flagged as a problem, not
# treated as satisfying any requirement.
_CLONE_OUTWARD_PHRASE = "clones"


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


def _clone_links(fields: dict[str, Any]) -> list[str]:
    """Return the keys of issues this bug "clones" (outward "Cloners" link) -
    should always be empty; any entry here is a problem to flag.
    """
    result: list[str] = []
    for link in fields.get("issuelinks") or []:
        link_type = link.get("type") or {}
        outward_issue = link.get("outwardIssue")
        if outward_issue and str(link_type.get("outward", "")).strip().lower() == _CLONE_OUTWARD_PHRASE:
            result.append(outward_issue.get("key") or "")
    return [k for k in result if k]


def _check_bug(issue: dict[str, Any], jira_base_url: str) -> dict[str, Any]:
    fields = issue.get("fields") or {}
    key = issue.get("key", "")

    missing_fields = [label for label, check in REQUIRED_FIELD_CHECKS if not check(fields)]
    extra_fields = [label for label, field_id in FORBIDDEN_FIELD_CHECKS if fields.get(field_id)]

    linked = _qualifying_links(fields)
    has_story = any(_STORY_TYPE_MARKER in (l["type_name"] or "").lower() for l in linked)
    has_qa = any(_QA_TYPE_MARKER in (l["type_name"] or "").lower() for l in linked)

    missing_links: list[str] = []
    if not has_story:
        missing_links.append("История")
    if not has_qa:
        missing_links.append("QA task")

    clone_links = _clone_links(fields)
    extra_links = [f"клонирует {cloned_key}" for cloned_key in clone_links]

    return {
        "key": key,
        "url": f"{jira_base_url.rstrip('/')}/browse/{key}" if key else "",
        "summary": fields.get("summary", ""),
        "issuetype": (fields.get("issuetype") or {}).get("name", ""),
        "status": (fields.get("status") or {}).get("name", ""),
        "reporter": (fields.get("reporter") or {}).get("displayName", ""),
        "missing_fields": missing_fields,
        "extra_fields": extra_fields,
        "linked_types": [l["type_name"] for l in linked if l["type_name"]],
        "missing_links": missing_links,
        "extra_links": extra_links,
        "clone_links": clone_links,
        "is_valid": not missing_fields and not extra_fields and not missing_links and not extra_links,
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
            parts.append(f"недостающие поля: {', '.join(row['missing_fields'])}")
        if row.get("extra_fields"):
            parts.append(f"лишние поля: {', '.join(row['extra_fields'])}")
        if row["missing_links"]:
            parts.append(f"недостающие связи: {', '.join(row['missing_links'])}")
        if row.get("extra_links"):
            parts.append(f"лишние связи: {', '.join(row['extra_links'])}")
        author = f" (автор: {row['reporter']})" if row.get("reporter") else ""
        lines.append(f"  - {row['key']} — {row['summary']}{author} [{'; '.join(parts)}]")
    lines.append("")

    lines.append(f"БЕЗ ЗАМЕЧАНИЙ ({len(valid)})")
    if not valid:
        lines.append("  Нет.")
    for row in valid:
        author = f" (автор: {row['reporter']})" if row.get("reporter") else ""
        lines.append(f"  - {row['key']} — {row['summary']}{author}")

    return "\n".join(lines)


__all__ = [
    "DEFAULT_JQL",
    "PHASE_FIELD_ID",
    "ENV_FIELD_ID",
    "TEAM_FIELD_ID",
    "ORIGINAL_ESTIMATE_FIELD_ID",
    "SPRINT_FIELD_ID",
    "BUILD_FIELD_IDS",
    "run_bug_backlog_audit_workflow",
    "render_text_report",
]
