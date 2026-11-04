"""Remove the Severity custom field from cancelled bugs in a Jira project.

A cancelled bug (status "Отменено") no longer needs a severity classification
- once it's closed with no work done, a leftover Severity value is just noise
that can skew severity-based reports (e.g. bugs_per_epics_report). This
workflow finds cancelled bugs that still have Severity set and clears it.

Read-only by default (`apply=False`): only reports which bugs would be
affected. `apply=True` actually PATCHes the Severity field to null in Jira.
"""

from typing import Any, Callable, Optional

from packages.common import get_logger

logger = get_logger(__name__)

# Same custom field id as packages/workflows/triage's DEFAULT_SEVERITY_FIELD_ID
# (this Jira instance's own field id, not derivable generically) - verified
# live against real MB bugs, e.g. MB-7075: {"value": "Critical", "id": "10998"}.
SEVERITY_FIELD_ID = "customfield_10865"

# Non-obvious JQL gotcha, verified live before settling on this default:
# on this instance, the issue type's and status's actual *names* are the
# Cyrillic "Баг"/"Отменено" (confirmed via `GET /rest/api/3/issue/{key}`),
# and quoting those Cyrillic names directly in JQL (`issuetype = "Баг"`,
# `status = "Отменено"`) returns ZERO results even on issues that plainly
# have them. Jira resolves each standard issue type/status's English i18n
# key as a JQL alias though - plain `Bug` and `Cancelled` (no quotes) match
# correctly and return the identical result set as matching by numeric id
# (verified: both forms returned the same 185 issues for this JQL). Using
# the English alias here since it mirrors the issuetype names already used
# by this app's other bug-scanning workflows (bug_backlog_audit,
# bugs_per_epics_report), which also just use "BE BUG"/"Mobile bug"/
# "FE bug"/Bug - the 4 bug issue types that exist on this instance
# (a 5th, "RQs bug", exists too but isn't one of the 4 tracked here).
DEFAULT_JQL = 'project = MB AND issuetype in ("BE BUG", "Mobile bug", "FE bug", Bug) AND status = Cancelled'

FIELDS_TO_FETCH = f"summary,issuetype,status,{SEVERITY_FIELD_ID}"


async def run_cancelled_bug_severity_cleanup_workflow(
    jira_client,
    *,
    jql: str = DEFAULT_JQL,
    max_results: int = 200,
    apply: bool = False,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Fetch cancelled bugs matched by `jql` and clear their Severity field
    (`SEVERITY_FIELD_ID`) wherever it's set. Only writes to Jira when
    `apply=True`; otherwise just reports what would be cleared.
    """

    def _log(level: str, msg: str) -> None:
        logger.info(msg) if level == "INFO" else logger.warning(msg) if level == "WARNING" else logger.debug(msg)
        if log_fn:
            log_fn(level, msg)

    _log(
        "INFO",
        f"Cancelled bug severity cleanup started: jql={jql!r}, max_results={max_results}, apply={apply}",
    )

    if should_cancel_fn and should_cancel_fn():
        _log("WARNING", "Остановлено пользователем")
        return {"status": "canceled", "error": "Остановлено пользователем"}

    try:
        issues = await jira_client.fetch_bugs(jql, max_results=max_results, fields=FIELDS_TO_FETCH)
    except Exception as exc:
        _log("ERROR", f"Failed to fetch bugs from Jira: {exc}")
        return {"status": "failed", "error": str(exc)}

    _log("INFO", f"Fetched {len(issues)} cancelled bug(s)")

    jira_base_url = str(getattr(jira_client, "base_url", "") or "")
    results: list[dict[str, Any]] = []
    canceled = False
    for idx, issue in enumerate(issues, 1):
        if should_cancel_fn and should_cancel_fn():
            _log("WARNING", f"Остановлено пользователем после {idx - 1}/{len(issues)} багов")
            canceled = True
            break

        key = issue.get("key", "")
        fields = issue.get("fields") or {}
        severity_raw = fields.get(SEVERITY_FIELD_ID)
        severity_value = severity_raw.get("value") if isinstance(severity_raw, dict) else severity_raw

        row: dict[str, Any] = {
            "key": key,
            "url": f"{jira_base_url.rstrip('/')}/browse/{key}" if key else "",
            "summary": fields.get("summary", ""),
            "issuetype": (fields.get("issuetype") or {}).get("name", ""),
            "status": (fields.get("status") or {}).get("name", ""),
            "severity": severity_value,
            "has_severity": bool(severity_raw),
        }

        if row["has_severity"] and apply:
            update_result = await jira_client.update_issue(key, {"fields": {SEVERITY_FIELD_ID: None}})
            row["cleared"] = bool(update_result.get("success"))
            if row["cleared"]:
                _log("INFO", f"У {key} очищен Severity (был: {severity_value})")
            else:
                row["clear_error"] = update_result.get("error")
                _log("ERROR", f"Не удалось очистить Severity у {key}: {update_result.get('error')}")

        results.append(row)

    with_severity = [r for r in results if r["has_severity"]]
    cleared_count = sum(1 for r in with_severity if r.get("cleared")) if apply else 0

    summary = {
        "jql": jql,
        "bugs_fetched": len(issues),
        "bugs_checked": len(results),
        "with_severity_count": len(with_severity),
        "cleared_count": cleared_count,
        "apply": apply,
    }

    _log(
        "INFO",
        f"Cleanup {'canceled' if canceled else 'complete'}: "
        f"проверено={len(results)}/{len(issues)}, с Severity={len(with_severity)}"
        + (f" (очищено={cleared_count})" if apply else ""),
    )

    return {
        "status": "canceled" if canceled else "succeeded",
        "error": "Остановлено пользователем" if canceled else None,
        "summary": summary,
        "results": results,
        "correlation_id": correlation_id,
    }


def render_text_report(result: dict[str, Any]) -> str:
    """Render the cleanup result as a human-readable text report."""
    if result.get("status") != "succeeded":
        return f"Очистка Severity не выполнена: {result.get('error', 'неизвестная ошибка')}"

    summary = result.get("summary", {})
    mode = "ПРИМЕНЕНО" if summary.get("apply") else "ПРЕДПРОСМОТР"
    lines: list[str] = []
    lines.append("ОЧИСТКА SEVERITY У ОТМЕНЁННЫХ БАГОВ")
    lines.append(f"JQL: {summary.get('jql', '')}")
    lines.append(
        f"Режим: {mode} | Проверено: {summary.get('bugs_checked', 0)}/{summary.get('bugs_fetched', 0)} | "
        f"С Severity: {summary.get('with_severity_count', 0)}"
        + (f" (очищено: {summary.get('cleared_count', 0)})" if summary.get("apply") else "")
    )
    lines.append("")

    results = result.get("results", [])
    with_severity = [r for r in results if r["has_severity"]]
    without_severity = [r for r in results if not r["has_severity"]]

    lines.append(f"С SEVERITY ({len(with_severity)})")
    if not with_severity:
        lines.append("  Нет.")
    for row in with_severity:
        if summary.get("apply"):
            status = "✅ очищено" if row.get("cleared") else f"❌ не очищено: {row.get('clear_error', '')}"
        else:
            status = "⚠ будет очищено при применении"
        lines.append(f"  - {row['key']} ({row['severity']}) — {row['summary']} [{status}]")
    lines.append("")

    lines.append(f"УЖЕ БЕЗ SEVERITY ({len(without_severity)})")
    if not without_severity:
        lines.append("  Нет.")
    for row in without_severity:
        lines.append(f"  - {row['key']} — {row['summary']}")

    return "\n".join(lines)


__all__ = [
    "DEFAULT_JQL",
    "SEVERITY_FIELD_ID",
    "run_cancelled_bug_severity_cleanup_workflow",
    "render_text_report",
]
