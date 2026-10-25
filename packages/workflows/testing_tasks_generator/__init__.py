"""Generate the standard set of QA testing tasks for a "История" (User Story)
issue, mirroring a reference story like MB-76.

MB-76 shows the pattern this closes a gap on: a User Story gets linked (via
the "Testing" issue link type, inward phrase "is Tested by") to one QA task
per testing activity, each a copy of the story's own summary with a prefix
marking which activity it is:

- API: one task covering test cases + manual execution together
  ("QA API task", prefix "[API tc+ex]"), plus one automation task
  ("QA API auto task", prefix "[auto API]").
- Mobile: a test-cases task ("QA MB task", prefix "[MB tc]"), a manual
  execution task (same issue type, prefix "[MB ex]"), and an automation task
  ("QA MB auto task", prefix "[auto MB]").
- Web: the same three-way split as Mobile, with "QA WEB task"/"QA WEB auto
  task" and "[WEB tc]"/"[WEB ex]"/"[auto WEB]" prefixes.

A story sometimes already has one of these created by hand without its
prefix (just a plain copy of the story's summary) - that always means the
earliest task in the priority order (test cases for Mobile/Web, the combined
tc+ex task for API), so it fills that slot rather than being treated as
missing.

Read-only on Jira unless apply=True; always computes the same "missing
tasks" list which callers can preview and then re-run with apply=True.
"""

import re
from typing import Any, Callable, Optional

TESTING_LINK_TYPE_NAME = "Testing"
_STORY_TYPE_MARKERS = ("истор", "story")

# One entry per task Jira expects to exist, per platform, in priority order
# (earliest activity first - matters for classifying an unprefixed existing
# task, see `_classify_mb_web_slot`).
SLOT_DEFS: dict[str, list[dict[str, str]]] = {
    "api": [
        {"slot": "tc_ex", "issuetype_id": "10462", "issuetype_name": "QA API task", "prefix": "API tc+ex"},
        {"slot": "auto", "issuetype_id": "10463", "issuetype_name": "QA API auto task", "prefix": "auto API"},
    ],
    "mobile": [
        {"slot": "tc", "issuetype_id": "10183", "issuetype_name": "QA MB task", "prefix": "MB tc"},
        {"slot": "ex", "issuetype_id": "10183", "issuetype_name": "QA MB task", "prefix": "MB ex"},
        {"slot": "auto", "issuetype_id": "10460", "issuetype_name": "QA MB auto task", "prefix": "auto MB"},
    ],
    "web": [
        {"slot": "tc", "issuetype_id": "10459", "issuetype_name": "QA WEB task", "prefix": "WEB tc"},
        {"slot": "ex", "issuetype_id": "10459", "issuetype_name": "QA WEB task", "prefix": "WEB ex"},
        {"slot": "auto", "issuetype_id": "10461", "issuetype_name": "QA WEB auto task", "prefix": "auto WEB"},
    ],
}

PLATFORM_LABELS = {"api": "API", "mobile": "Mobile", "web": "Web"}

_LEAD_TAG_RE = re.compile(r"^(\[[^\]]+\]):\s*(.*)$")
_BRACKET_RE = re.compile(r"\[([^\]]+)\]")


def build_summary(parent_summary: str, prefix: str) -> str:
    """Copy the parent story's summary, inserting `[prefix]:` right after
    its leading epic tag (e.g. "[E10]:"), matching how these tasks are named
    by hand on this Jira instance. Falls back to prepending the prefix if
    the parent summary has no such leading tag."""
    match = _LEAD_TAG_RE.match(parent_summary.strip())
    if match:
        tag, rest = match.group(1), match.group(2)
        return f"{tag}: [{prefix}]: {rest}".strip()
    return f"[{prefix}]: {parent_summary.strip()}".strip()


def _classify_mb_web_slot(summary: str) -> Optional[str]:
    """Return "tc" or "ex" if the summary's bracket tags contain one of
    those tokens (e.g. "[MB tc]", "[WEB ex]"), else None (unprefixed)."""
    for bracket in _BRACKET_RE.findall(summary):
        tokens = bracket.lower().replace("+", " ").split()
        if "tc" in tokens:
            return "tc"
        if "ex" in tokens:
            return "ex"
    return None


def _existing_testing_links(story: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the testing tasks already linked to the story via the
    "Testing" link type. Verified live against MB-76/MB-1065's raw
    `issueLink` record: the story is always the link's `outwardIssue` and
    the testing task is always its `inwardIssue`, so on the story's own
    payload the linked task appears under `inwardIssue`."""
    links = (story.get("fields") or {}).get("issuelinks") or []
    result = []
    for link in links:
        if (link.get("type") or {}).get("name") != TESTING_LINK_TYPE_NAME:
            continue
        other = link.get("inwardIssue")
        if not other:
            continue
        fields = other.get("fields") or {}
        result.append({
            "key": other.get("key"),
            "summary": fields.get("summary", ""),
            "issuetype_id": str((fields.get("issuetype") or {}).get("id") or ""),
            "issuetype_name": (fields.get("issuetype") or {}).get("name", ""),
        })
    return result


def compute_plan(story: dict[str, Any], platforms: list[str]) -> dict[str, Any]:
    """Pure function: given the fetched story issue and the list of selected
    platform keys ("api"/"mobile"/"web"), return which testing tasks already
    satisfy a slot and which are missing and need to be created."""
    existing = _existing_testing_links(story)

    # issuetype_id -> list of existing links of that type, in the order Jira
    # returned them (stable enough for the greedy tc/ex assignment below).
    by_issuetype: dict[str, list[dict[str, Any]]] = {}
    for item in existing:
        by_issuetype.setdefault(item["issuetype_id"], []).append(item)

    already_exists: list[dict[str, Any]] = []
    to_create: list[dict[str, Any]] = []

    for platform in platforms:
        filled_slots: dict[str, dict[str, Any]] = {}
        slot_defs = SLOT_DEFS[platform]

        if platform == "api":
            for slot_def in slot_defs:
                candidates = by_issuetype.get(slot_def["issuetype_id"], [])
                if candidates:
                    filled_slots[slot_def["slot"]] = candidates[0]
        else:
            # The two "tc"/"ex" slots share one issuetype id; classify each
            # existing task by its prefix, defaulting an unprefixed one to
            # the earliest unfilled slot (tc before ex).
            tc_ex_def = {d["slot"]: d for d in slot_defs if d["slot"] in ("tc", "ex")}
            tc_ex_issuetype_id = next(iter(tc_ex_def.values()))["issuetype_id"]
            for candidate in by_issuetype.get(tc_ex_issuetype_id, []):
                slot = _classify_mb_web_slot(candidate["summary"])
                if slot is None:
                    slot = "tc" if "tc" not in filled_slots else ("ex" if "ex" not in filled_slots else None)
                if slot and slot not in filled_slots:
                    filled_slots[slot] = candidate
            auto_def = next(d for d in slot_defs if d["slot"] == "auto")
            auto_candidates = by_issuetype.get(auto_def["issuetype_id"], [])
            if auto_candidates:
                filled_slots["auto"] = auto_candidates[0]

        for slot_def in slot_defs:
            found = filled_slots.get(slot_def["slot"])
            if found:
                already_exists.append({
                    "platform": platform,
                    "slot": slot_def["slot"],
                    "prefix": slot_def["prefix"],
                    "key": found["key"],
                    "summary": found["summary"],
                })
            else:
                to_create.append({
                    "platform": platform,
                    "slot": slot_def["slot"],
                    "prefix": slot_def["prefix"],
                    "issuetype_id": slot_def["issuetype_id"],
                    "issuetype_name": slot_def["issuetype_name"],
                    "summary": build_summary((story.get("fields") or {}).get("summary", ""), slot_def["prefix"]),
                })

    return {"already_exists": already_exists, "to_create": to_create}


async def run_testing_tasks_generator_workflow(
    jira_client,
    *,
    story_key: str,
    include_api: bool = True,
    include_mobile: bool = True,
    include_web: bool = True,
    apply: bool = False,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    def _log(level: str, message: str) -> None:
        if log_fn:
            log_fn(level, message)

    platforms = [
        p for p, included in (("api", include_api), ("mobile", include_mobile), ("web", include_web)) if included
    ]
    if not platforms:
        return {"status": "failed", "error": "Не выбрана ни одна платформа (API/Mobile/Web)"}

    story = await jira_client.get_issue(story_key)
    if not story:
        return {"status": "failed", "error": f"Задача {story_key} не найдена в Jira"}

    issuetype_name = ((story.get("fields") or {}).get("issuetype") or {}).get("name", "")
    if not any(marker in issuetype_name.lower() for marker in _STORY_TYPE_MARKERS):
        return {
            "status": "failed",
            "error": f"{story_key} имеет тип '{issuetype_name}', а не 'История' (User Story) - отменено",
        }

    project_key = ((story.get("fields") or {}).get("project") or {}).get("key", "")
    if not project_key:
        return {"status": "failed", "error": f"Не удалось определить проект для {story_key}"}

    _log("INFO", f"{story_key}: тип '{issuetype_name}', проект {project_key}, платформы: {', '.join(platforms)}")

    plan = compute_plan(story, platforms)
    to_create = plan["to_create"]
    already_exists = plan["already_exists"]
    _log(
        "INFO",
        f"Уже существует: {len(already_exists)}, нужно создать: {len(to_create)}"
        + (" (ПРЕДПРОСМОТР)" if not apply else ""),
    )

    canceled = False
    if apply:
        for item in to_create:
            if should_cancel_fn and should_cancel_fn():
                canceled = True
                break
            create_result = await jira_client.create_issue({
                "fields": {
                    "project": {"key": project_key},
                    "issuetype": {"id": item["issuetype_id"]},
                    "summary": item["summary"],
                }
            })
            item["created"] = bool(create_result.get("success"))
            if not item["created"]:
                item["error"] = create_result.get("error")
                _log("ERROR", f"Не удалось создать задачу [{item['prefix']}]: {item['error']}")
                continue

            new_key = create_result.get("issue_key")
            item["key"] = new_key
            _log("INFO", f"Создано {new_key}: {item['summary']}")

            link_result = await jira_client.create_issue_link(
                outward_key=story_key, inward_key=new_key, link_type_name=TESTING_LINK_TYPE_NAME
            )
            item["linked"] = bool(link_result.get("success"))
            if not item["linked"]:
                item["link_error"] = link_result.get("error")
                _log("ERROR", f"Задача {new_key} создана, но не удалось привязать её к {story_key}: {item['link_error']}")

    summary = {
        "story_key": story_key,
        "project_key": project_key,
        "platforms": platforms,
        "already_exists_count": len(already_exists),
        "to_create_count": len(to_create),
        "created_count": sum(1 for i in to_create if i.get("created")) if apply else 0,
        "apply": apply,
    }
    _log(
        "INFO",
        ("Остановлено пользователем. " if canceled else "Готово: ")
        + f"уже есть={len(already_exists)}, "
        + (f"создано={summary['created_count']}/{len(to_create)}" if apply else f"к созданию={len(to_create)}"),
    )

    return {
        "status": "canceled" if canceled else "succeeded",
        "error": "Остановлено пользователем" if canceled else None,
        "summary": summary,
        "already_exists": already_exists,
        "to_create": to_create,
        "correlation_id": correlation_id,
    }


def render_text_report(result: dict[str, Any]) -> str:
    """Render the generator result as a human-readable text report."""
    if result.get("status") not in ("succeeded", "canceled"):
        return f"Генерация задач тестирования не выполнена: {result.get('error', 'неизвестная ошибка')}"

    summary = result.get("summary", {})
    lines: list[str] = []
    lines.append("ГЕНЕРАЦИЯ ЗАДАЧ ТЕСТИРОВАНИЯ")
    lines.append(f"История: {summary.get('story_key')} (проект {summary.get('project_key')})")
    lines.append(f"Платформы: {', '.join(PLATFORM_LABELS.get(p, p) for p in summary.get('platforms', []))}")
    mode = "ПРИМЕНЕНО" if summary.get("apply") else "ПРЕДПРОСМОТР (ничего не создано)"
    lines.append(f"Режим: {mode}")
    lines.append("")

    to_create = result.get("to_create", [])
    header = f"СОЗДАНО ({summary.get('created_count', 0)}/{len(to_create)})" if summary.get("apply") else f"БУДЕТ СОЗДАНО ({len(to_create)})"
    lines.append(header)
    if not to_create:
        lines.append("  Нет - все необходимые задачи уже есть.")
    for item in to_create:
        status = ""
        if summary.get("apply"):
            if item.get("created"):
                status = f" ✅ {item.get('key')}" + ("" if item.get("linked") else f" (⚠ не привязано: {item.get('link_error', '')})")
            else:
                status = f" ❌ {item.get('error', '')}"
        lines.append(f"  - [{item['prefix']}] ({item['issuetype_name']}): {item['summary']}{status}")
    lines.append("")

    already_exists = result.get("already_exists", [])
    lines.append(f"УЖЕ СУЩЕСТВУЕТ ({len(already_exists)})")
    if not already_exists:
        lines.append("  Нет.")
    for item in already_exists:
        lines.append(f"  - [{item['prefix']}] {item['key']}: {item['summary']}")

    return "\n".join(lines)


__all__ = [
    "TESTING_LINK_TYPE_NAME",
    "SLOT_DEFS",
    "PLATFORM_LABELS",
    "build_summary",
    "compute_plan",
    "run_testing_tasks_generator_workflow",
    "render_text_report",
]
