"""Fill in the "QA: AccCriterias are passed" checklist (Checklist for Jira
Cloud by Appbox, add-on key `com.appbox.ai.checklist`) on a Jira User Story,
based on the real number of Acceptance Criteria found in its Confluence
specification.

For a given Jira issue (a User Story, or any issue whose summary contains a
"US-X.Y.Z"/"AUS-X.Y.Z" reference): resolves the matching Confluence spec page,
parses its "Критерии Приемки" table for the real list of AC-NN ids, then adds
one checklist item per AC that isn't already present - in BOTH the "MB:
precheck ACs..." and "WEB (IB): precheck ACs..." sections (same full AC list
in both, since both platforms need to verify the same acceptance criteria;
see [[sibling-claude-project]] memory for background) - never removing or
altering items a QA engineer may have already started working on.

Read-modify-write against the app's own (undocumented) entity-property
format - see `_codec.py` for how that format was reverse-engineered and why
(no documented REST API exists for Jira issues using "multiple checklists").
Every write is preceded by an in-process round-trip self-check
(`_codec.encode_shard_payload`); on any mismatch the workflow fails closed
without touching Jira.
"""

import json
import re
import traceback
import uuid
from typing import Any, Callable, Optional

from packages.common import get_logger

from . import _codec
from ._confluence_lookup import (
    ChecklistResolutionError,
    DEFAULT_SPECS_FOLDER_TITLE,
    extract_story_ref,
    find_us_page_under_folder,
    parse_ac_table,
)

logger = get_logger(__name__)

CHECKLIST_META_PROPERTY = "com.appbox.ai.checklist-ticket-checklist"
CHECKLIST_SHARD_PROPERTY = "com.appbox.ai.checklist-ticket-checklist_0"

# (platform label, section-name matcher) - both are filled with the identical
# full AC list; a ticket missing one of these sections just skips it (e.g. a
# checklist that was never set up with the WEB section yet).
_SECTION_MATCHERS: list[tuple[str, Callable[[str], bool]]] = [
    ("MB", lambda name: name.strip().upper().startswith("MB:")),
    ("WEB", lambda name: "WEB" in name.upper()),
]

_QA_TAB_NAME_MARKER = "acccriteria"  # matched against the tab name, case-insensitive, spaces/colons stripped
_AC_ITEM_PREFIX_RE = re.compile(r"^[AА][CС]-(\d+)\s*:", re.IGNORECASE)

# Which platform(s) a feature actually applies to. "both" (default) touches
# neither platform's section specially; "mb_only"/"web_only" mean the OTHER
# platform's section is cleared out entirely (see `feature_excludes`), since
# a feature that doesn't exist on that platform has nothing to verify there.
FEATURE_SCOPE_CHOICES = ("both", "mb_only", "web_only")


def feature_excludes(feature_scope: str, platform: str) -> bool:
    return (feature_scope == "mb_only" and platform == "WEB") or (
        feature_scope == "web_only" and platform == "MB"
    )


def _norm_ws(text: str) -> str:
    """Collapse runs of whitespace and strip - real checklist text on this
    instance has stray double-spaces/trailing spaces (e.g. 'There are build
    WEB (ИБ)  in UAT branch ', 'ready for QA WEB testing '), so exact
    byte-for-byte matching is too brittle for tab/section/item names."""
    return re.sub(r"\s+", " ", text or "").strip()


# Definition of Ready: which SECTION to leave untouched (not marked done) when
# the feature doesn't ship on that platform - verified against real data on
# MB-1666 (section names there have the exact whitespace quirks below).
_DOR_EXCLUDE_SECTION_BY_SCOPE = {
    "mb_only": _norm_ws("ready for QA WEB testing"),
    "web_only": _norm_ws("ready for QA MB testing"),
}

# Definition of Done: which individual ITEMS (by normalized text) to leave
# untouched when the feature doesn't ship on that platform - also verified
# against real data (exact wording/whitespace, incl. "there aren't" typo-
# like phrasing, is copied as-is from the real checklist item text).
_DOD_EXCLUDE_ITEMS_BY_SCOPE = {
    "mb_only": {
        _norm_ws("QA WEB has been done (all ACs are passed). there aren't critical and majors bugs"),
        _norm_ws("There are build WEB (ИБ) in UAT branch"),
    },
    "web_only": {
        _norm_ws("MB dev has been done"),
        _norm_ws("QA MB has been done (all ACs are passed). there aren't critical and majors bugs"),
        _norm_ws("There are builds (Android, iOS) in UAT branch for downloading"),
    },
}


def _mark_dor_dod(root: dict[str, Any], feature_scope: str, dry_run: bool) -> list[dict[str, Any]]:
    """Mark every Definition of Ready / Definition of Done checklist item as
    done - except items that don't apply to the platform(s) this feature
    doesn't ship on (per `feature_scope`), which are force-UNchecked instead
    (set done=False) since a feature that doesn't ship somewhere has nothing
    to verify there - any checkmark on those items is stale/misleading, not
    something to silently leave alone."""
    exclude_section = _DOR_EXCLUDE_SECTION_BY_SCOPE.get(feature_scope)
    exclude_items = _DOD_EXCLUDE_ITEMS_BY_SCOPE.get(feature_scope, set())
    # Looked up once per call (not per item) so every item we flip to the same
    # state shares the identical status object reference, same as the app's
    # own data does - keeps the re-encoded payload compact and consistent.
    done_status = _find_status_template(root, is_done=True)
    not_done_status = _find_status_template(root, is_done=False)

    reports: list[dict[str, Any]] = []
    for tab in root.get("tabs", []):
        tab_norm = _norm_ws(tab.get("name", ""))
        if tab_norm == "Definition of Ready":
            kind = "DoR"
        elif tab_norm == "Definition of Done":
            kind = "DoD"
        else:
            continue

        for section in tab.get("sections", []):
            section_norm = _norm_ws(section.get("name", ""))
            if kind == "DoR" and exclude_section and section_norm == exclude_section:
                unmarked = 0
                for item in section.get("list", []):
                    if not _is_fully_not_done(item):
                        unmarked += 1
                        if not dry_run:
                            item["done"] = False
                            item["status"] = not_done_status
                reports.append({
                    "tab": kind, "section": section.get("name"), "excluded": True,
                    "marked_count": 0, "unmarked_count": unmarked, "total": len(section.get("list", [])),
                })
                continue

            marked = 0
            unmarked = 0
            for item in section.get("list", []):
                item_norm = _norm_ws(item.get("summary") or "")
                if kind == "DoD" and item_norm in exclude_items:
                    if not _is_fully_not_done(item):
                        unmarked += 1
                        if not dry_run:
                            item["done"] = False
                            item["status"] = not_done_status
                    continue
                if not _is_fully_done(item):
                    marked += 1
                    if not dry_run:
                        item["done"] = True
                        item["status"] = done_status

            reports.append({
                "tab": kind, "section": section.get("name"), "excluded": False,
                "marked_count": marked, "unmarked_count": unmarked, "total": len(section.get("list", [])),
            })

    return reports


class AcChecklistSyncError(Exception):
    """Raised for any failure specific to this workflow (resolution, missing
    checklist/tab/section, or a failed write)."""


def _normalize_tab_name(name: str) -> str:
    return re.sub(r"[^a-zA-Zа-яА-Я]", "", name or "").lower()


def _find_qa_tab(root: dict[str, Any]) -> dict[str, Any]:
    for tab in root.get("tabs", []):
        if _QA_TAB_NAME_MARKER in _normalize_tab_name(tab.get("name", "")):
            return tab
    raise AcChecklistSyncError(
        'Вкладка чек-листа "QA: AccCriterias are passed" не найдена на этой задаче.'
    )


def _ac_number_of(item: dict[str, Any]) -> Optional[int]:
    match = _AC_ITEM_PREFIX_RE.match(item.get("summary") or "")
    return int(match.group(1)) if match else None


def _find_template_status(root: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Reuse an existing mandatory, not-done "To Do" status object (same
    object reference, structurally shared - mirrors how the app's own data
    shares one status instance across many items) so newly added items render
    identically to the ones already on this checklist."""
    for tab in root.get("tabs", []):
        for section in tab.get("sections", []):
            for item in section.get("list", []):
                if item.get("mandatory") and not item.get("done"):
                    return item["status"]
    return None


# Fallback status objects, used only if a checklist has no existing item in
# the target done-state to copy a real template from (verified shape against
# real data: {"code": "Completed"/"To Do", "isDone": True/False, "color": ...}).
_FALLBACK_DONE_STATUS = {"color": "#01875a", "code": "Completed", "isDone": True}
_FALLBACK_NOT_DONE_STATUS = {"color": "#30b1d1", "code": "To Do", "isDone": False}


def _find_status_template(root: dict[str, Any], is_done: bool) -> dict[str, Any]:
    """Find any existing status object on this checklist matching `is_done`,
    to reuse (same object reference) when flipping an item's done/not-done
    state - the `status` object (its "Completed"/"To Do" code and color) is a
    SEPARATE field from the `done` boolean and must be kept in sync with it,
    or the item's visible status badge goes stale even though its checkbox
    changed. Never mutates a found status object in place, since it may be
    shared by reference with other items - always returns a reusable template
    to ASSIGN, not edit."""
    for tab in root.get("tabs", []):
        for section in tab.get("sections", []):
            for item in section.get("list", []):
                status = item.get("status")
                if isinstance(status, dict) and status.get("isDone") == is_done:
                    return status
    return dict(_FALLBACK_DONE_STATUS if is_done else _FALLBACK_NOT_DONE_STATUS)


def _is_fully_done(item: dict[str, Any]) -> bool:
    """True only if BOTH the `done` flag and the `status.isDone` field agree
    the item is done - a mismatch (e.g. done=False but status still says
    "Completed", left over from code that only touched one of the two
    fields) is treated as "not done" so it gets corrected, not skipped."""
    return bool(item.get("done")) and bool((item.get("status") or {}).get("isDone"))


def _is_fully_not_done(item: dict[str, Any]) -> bool:
    return not item.get("done") and not bool((item.get("status") or {}).get("isDone"))


def _recompute_section_counters(section: dict[str, Any]) -> None:
    total = len(section.get("list", []))
    done = sum(1 for item in section["list"] if item.get("done"))
    section["itemsDone"] = done
    section["percentDone"] = round(done / total * 100) if total else 0
    section["allItemsDone"] = bool(total) and done == total


def _recompute_global_stats(root: dict[str, Any]) -> None:
    all_sections = [s for tab in root.get("tabs", []) for s in tab.get("sections", [])]
    for section in all_sections:
        _recompute_section_counters(section)

    total_items = sum(len(s["list"]) for s in all_sections)
    items_done = sum(s["itemsDone"] for s in all_sections)
    mandatory_items = [item for s in all_sections for item in s["list"] if item.get("mandatory")]
    total_mandatory = len(mandatory_items)
    mandatory_done = sum(1 for item in mandatory_items if item.get("done"))

    stats = root.setdefault("stats", {})
    stats["itemsDone"] = items_done
    stats["totalItems"] = total_items
    stats["itemsRemaining"] = total_items - items_done
    stats["totalPercentageDone"] = round(items_done / total_items * 100) if total_items else 0
    stats["totalMandatoryItems"] = total_mandatory
    stats["mandatoryItemsDone"] = mandatory_done
    stats["mandatoryItemsRemaining"] = total_mandatory - mandatory_done
    stats["mandatoryPercentageDone"] = round(mandatory_done / total_mandatory * 100) if total_mandatory else 0
    stats["totalSections"] = len(all_sections)
    stats["totalTabs"] = len(root.get("tabs", []))
    stats["checklistProgress"] = f"Checklist Progress: {items_done}/{total_items}"


async def run_ac_checklist_sync_workflow(
    jira_client,
    confluence_client,
    *,
    issue_key: str,
    specs_folder_title: str = DEFAULT_SPECS_FOLDER_TITLE,
    dry_run: bool = True,
    feature_scope: str = "both",
    platforms: Optional[list[str]] = None,
    mark_dor_dod: bool = False,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """`feature_scope` ("both"/"mb_only"/"web_only") declares which platform(s)
    this User Story's feature actually ships on - chosen once, up front. When
    it's "mb_only"/"web_only", the OTHER platform's section has nothing to
    verify (the feature doesn't exist there) and gets fully cleared of AC
    items, not just left stale.

    `platforms`, if given, separately restricts which checklist sections are
    WRITTEN in this call - e.g. `["MB"]` to apply only the mobile section right
    now, leaving WEB (IB) completely untouched (not even read/modified) so one
    platform's QA lead can accept their part independently of the other, on
    their own schedule. None/omitted means both (whichever `feature_scope`
    leaves active).

    `mark_dor_dod`, if true, additionally marks every item in the "Definition
    of Ready"/"Definition of Done " tabs as done - except items that don't
    apply to the platform(s) this feature doesn't ship on per `feature_scope`
    (the "ready for QA WEB testing"/"ready for QA MB testing" DoR section, and
    specific MB-/WEB-only DoD items - see `_DOR_EXCLUDE_SECTION_BY_SCOPE`/
    `_DOD_EXCLUDE_ITEMS_BY_SCOPE`), which are force-UNCHECKED instead (any
    pre-existing checkmark there is stale once the feature is scoped away from
    that platform)."""
    def _log(level: str, msg: str) -> None:
        (logger.info if level == "INFO" else logger.warning if level == "WARNING" else logger.debug)(msg)
        if log_fn:
            log_fn(level, msg)

    issue_key = (issue_key or "").strip().upper()
    feature_scope = (feature_scope or "both").strip().lower()
    if feature_scope not in FEATURE_SCOPE_CHOICES:
        return {
            "status": "failed",
            "error": f"Недопустимое значение feature_scope: '{feature_scope}' (ожидается одно из {FEATURE_SCOPE_CHOICES})",
            "correlation_id": correlation_id,
        }
    platform_filter = {p.strip().upper() for p in platforms} if platforms else None
    _log(
        "INFO",
        f"ac_checklist_sync started for {issue_key} "
        f"(dry_run={dry_run}, feature_scope={feature_scope}, platforms={platform_filter or 'all'})",
    )

    if should_cancel_fn and should_cancel_fn():
        return {"status": "canceled", "error": "Остановлено пользователем"}

    try:
        issues = await jira_client.fetch_issues_by_keys([issue_key])
        if not issues:
            raise AcChecklistSyncError(f"Задача {issue_key} не найдена в Jira.")
        summary = ((issues[0].get("fields") or {}).get("summary")) or ""

        story_ref = extract_story_ref(summary)
        if not story_ref:
            raise AcChecklistSyncError(
                f'Не удалось найти ссылку на User Story ("US-X.Y.Z") в теме задачи {issue_key}: "{summary}".'
            )
        prefix_hint, us_number = story_ref
        _log("INFO", f"Задача {issue_key}: найдена ссылка {prefix_hint}-{us_number}")

        if should_cancel_fn and should_cancel_fn():
            return {"status": "canceled", "error": "Остановлено пользователем"}

        page_ref = await find_us_page_under_folder(
            confluence_client, specs_folder_title, us_number,
            prefix_hint=prefix_hint, log_fn=_log, should_cancel_fn=should_cancel_fn,
        )
        page = await confluence_client.get_page(page_ref["id"])
        if not page:
            raise AcChecklistSyncError(f"Не удалось загрузить страницу спецификации (id={page_ref['id']}).")

        space_key = (page.get("space") or {}).get("key", "")
        page_url = f"{confluence_client.base_url.rstrip('/')}/spaces/{space_key}/pages/{page['id']}"
        storage_html = ((page.get("body") or {}).get("storage") or {}).get("value", "")

        anchor, ac_numbers = parse_ac_table(storage_html)
        _log("INFO", f"Спецификация '{page.get('title')}': найдено {len(ac_numbers)} AC (AC-{ac_numbers[0]:02d}..AC-{ac_numbers[-1]:02d})")

        if should_cancel_fn and should_cancel_fn():
            return {"status": "canceled", "error": "Остановлено пользователем"}

        meta_raw = await jira_client.get_issue_property(issue_key, CHECKLIST_META_PROPERTY)
        if meta_raw is None:
            raise AcChecklistSyncError(f"На задаче {issue_key} не найден чек-лист (Checklist for Jira).")
        meta = json.loads(meta_raw["data"])
        if int(meta.get("totalChunks", 1)) != 1:
            raise AcChecklistSyncError(
                f"Чек-лист задачи {issue_key} хранится в {meta.get('totalChunks')} частях - "
                f"эта версия синхронизации поддерживает только однокусковые чек-листы."
            )

        shard_raw = await jira_client.get_issue_property(issue_key, CHECKLIST_SHARD_PROPERTY)
        if shard_raw is None:
            raise AcChecklistSyncError(f"Не удалось прочитать данные чек-листа задачи {issue_key}.")

        flat, root = _codec.decode_shard_payload(shard_raw["data"])
        qa_tab = _find_qa_tab(root)

        template_status = _find_template_status(root)
        if template_status is None:
            template_status = {
                "id": str(uuid.uuid4()),
                "color": "#30b1d1",
                "code": "To Do",
                "isDone": False,
            }

        section_reports: list[dict[str, Any]] = []
        any_section_matched = False
        for platform, matcher in _SECTION_MATCHERS:
            section = next((s for s in qa_tab.get("sections", []) if matcher(s.get("name", ""))), None)
            if section is None:
                section_reports.append({"platform": platform, "found": False})
                continue
            any_section_matched = True

            # If this feature doesn't apply to `platform` at all (e.g. a
            # mobile-only feature has no WEB part to verify), that section's
            # ACs are never relevant - clear it out entirely rather than
            # leaving stale/irrelevant items sitting on the checklist. This
            # happens on every real write regardless of `platforms` staging -
            # "apply only MB/WEB now" is about staging the normal AC sync for
            # the *active* platform, not about holding back a scope decision
            # that already says the other platform's section doesn't apply.
            if feature_excludes(feature_scope, platform):
                existing_items = section["list"]
                removed_numbers = sorted(
                    n for n in (_ac_number_of(i) for i in existing_items) if n is not None
                )
                if not dry_run:
                    section["list"] = []
                section_reports.append({
                    "platform": platform,
                    "found": True,
                    "section_name": section.get("name"),
                    "excluded_by_feature_scope": True,
                    "existing_count": len(existing_items),
                    "added_count": 0,
                    "added_ac_numbers": [],
                    "removed_count": len(existing_items),
                    "removed_ac_numbers": removed_numbers,
                })
                continue

            # Platform staging only applies to the normal AC sync below, for
            # a platform the feature actually ships on - not to the exclusion
            # clearing above.
            if platform_filter is not None and platform not in platform_filter:
                section_reports.append({"platform": platform, "found": None, "skipped": True})
                continue

            # Drop lingering fully-empty placeholder rows (no summary, not
            # mandatory) in sections we're actually touching - they carry no
            # QA data and are just template leftovers.
            section["list"] = [
                item for item in section["list"]
                if (item.get("summary") or "").strip() or item.get("mandatory")
            ]

            ac_number_set = set(ac_numbers)
            existing_by_number: dict[int, dict[str, Any]] = {}
            for item in section["list"]:
                n = _ac_number_of(item)
                if n is not None:
                    existing_by_number[n] = item  # last one wins if a number is somehow duplicated

            missing = [n for n in ac_numbers if n not in existing_by_number]
            # ACs present on the checklist but no longer in the current spec
            # (e.g. the spec was revised and a criterion was removed/renumbered)
            # - these are stale and get removed, never silently kept around.
            stale = sorted(n for n in existing_by_number if n not in ac_number_set)

            if not dry_run:
                if stale:
                    stale_set = set(stale)
                    section["list"] = [
                        item for item in section["list"]
                        if _ac_number_of(item) not in stale_set
                    ]
                for n in missing:
                    section["list"].append({
                        "id": str(uuid.uuid4()),
                        "summary": f"AC-{n:02d}: {page_url}#{anchor}",
                        "done": False,
                        "status": template_status,
                        "mandatory": True,
                    })

            section_reports.append({
                "platform": platform,
                "found": True,
                "section_name": section.get("name"),
                "existing_count": len(existing_by_number),
                "added_count": len(missing),
                "added_ac_numbers": missing,
                "removed_count": len(stale),
                "removed_ac_numbers": stale,
            })

        if not any_section_matched:
            raise AcChecklistSyncError(
                'Ни секция "MB: precheck ACs...", ни секция "WEB (IB): precheck ACs..." '
                f'не найдены во вкладке "{qa_tab.get("name")}" задачи {issue_key}.'
            )

        dor_dod_reports: list[dict[str, Any]] = []
        if mark_dor_dod:
            dor_dod_reports = _mark_dor_dod(root, feature_scope, dry_run)

        written = False
        if not dry_run and (
            any(r.get("added_count") or r.get("removed_count") for r in section_reports)
            or any(r.get("marked_count") or r.get("unmarked_count") for r in dor_dod_reports)
        ):
            _recompute_global_stats(root)
            payload, payload_len = _codec.encode_shard_payload(root)
            meta["totalSize"] = payload_len

            if should_cancel_fn and should_cancel_fn():
                return {"status": "canceled", "error": "Остановлено пользователем"}

            if not await jira_client.set_issue_property(issue_key, CHECKLIST_SHARD_PROPERTY, {"data": payload}):
                raise AcChecklistSyncError("Не удалось записать данные чек-листа в Jira.")
            if not await jira_client.set_issue_property(issue_key, CHECKLIST_META_PROPERTY, {"data": json.dumps(meta)}):
                raise AcChecklistSyncError(
                    "Пункты чек-листа записаны, но не удалось обновить метаданные размера - "
                    "откройте задачу и пересохраните любой пункт чек-листа вручную, чтобы приложение пересчитало счётчики."
                )
            written = True

        total_added = sum(r.get("added_count", 0) for r in section_reports)
        total_removed = sum(r.get("removed_count", 0) for r in section_reports)
        total_marked = sum(r.get("marked_count", 0) for r in dor_dod_reports)
        _log(
            "INFO",
            f"ac_checklist_sync {'(просмотр, без записи) ' if dry_run else ''}завершён: "
            f"найдено {len(ac_numbers)} AC, добавлено {total_added}, удалено устаревших {total_removed} "
            f"пункт(ов) суммарно по секциям"
            + (f"; отмечено {total_marked} пункт(ов) DoR/DoD" if mark_dor_dod else ""),
        )

        return {
            "status": "succeeded",
            "error": None,
            "issue_key": issue_key,
            "story_ref": f"{prefix_hint}-{us_number}",
            "spec_page_title": page.get("title"),
            "spec_page_url": page_url,
            "ac_total_found": len(ac_numbers),
            "ac_numbers": ac_numbers,
            "sections": section_reports,
            "dry_run": dry_run,
            "feature_scope": feature_scope,
            "platforms": sorted(platform_filter) if platform_filter else None,
            "mark_dor_dod": mark_dor_dod,
            "dor_dod": dor_dod_reports,
            "written": written,
            "correlation_id": correlation_id,
        }
    except (AcChecklistSyncError, ChecklistResolutionError) as exc:
        _log("ERROR", str(exc))
        return {"status": "failed", "error": str(exc), "correlation_id": correlation_id}
    except Exception as exc:  # defensive: never let an unexpected error look like success
        tb = traceback.format_exc()
        logger.error(f"ac_checklist_sync failed unexpectedly for {issue_key}: {exc}\n{tb}")
        _log("ERROR", f"Неожиданная ошибка: {exc}\n{tb}")
        return {"status": "failed", "error": str(exc), "correlation_id": correlation_id}


def render_text_report(result: dict[str, Any]) -> str:
    if result.get("status") != "succeeded":
        return f"Синхронизация чек-листа не выполнена: {result.get('error', 'неизвестная ошибка')}"

    lines: list[str] = []
    mode = "ПРЕДПРОСМОТР (без записи)" if result.get("dry_run") else "ВЫПОЛНЕНО"
    lines.append(f"СИНХРОНИЗАЦИЯ ЧЕК-ЛИСТА AC — {result.get('issue_key')} [{mode}]")
    lines.append(f"User Story: {result.get('story_ref')}")
    lines.append(f"Спецификация: {result.get('spec_page_title')} ({result.get('spec_page_url')})")
    lines.append(f"Найдено критериев приёмки: {result.get('ac_total_found')}")
    lines.append("")

    for section in result.get("sections", []):
        if section.get("skipped"):
            lines.append(f"[{section['platform']}] не затронуто в этом запуске (отфильтровано параметром platforms).")
            continue
        if not section.get("found"):
            lines.append(f"[{section['platform']}] секция не найдена на чек-листе — пропущено.")
            continue
        added = section.get("added_ac_numbers") or []
        removed = section.get("removed_ac_numbers") or []
        added_str = ", ".join(f"AC-{n:02d}" for n in added) if added else "нет"
        removed_str = ", ".join(f"AC-{n:02d}" for n in removed) if removed else "нет"
        if section.get("excluded_by_feature_scope"):
            lines.append(
                f"[{section['platform']}] «{section.get('section_name')}»: "
                f"функция не применима к этой платформе — очищено {section.get('removed_count')} пункт(ов) ({removed_str})"
            )
            continue
        lines.append(
            f"[{section['platform']}] «{section.get('section_name')}»: "
            f"было {section.get('existing_count')}, добавлено {section.get('added_count')} ({added_str}), "
            f"удалено устаревших {section.get('removed_count')} ({removed_str})"
        )

    if result.get("mark_dor_dod"):
        lines.append("")
        lines.append("DEFINITION OF READY / DEFINITION OF DONE:")
        for row in result.get("dor_dod", []):
            if row.get("excluded"):
                lines.append(
                    f"  [{row['tab']}] «{row.get('section')}»: не применимо к выбранной области фичи — "
                    f"снято отметок: {row.get('unmarked_count', 0)}"
                )
            else:
                extra = f", снято неприменимых: {row['unmarked_count']}" if row.get("unmarked_count") else ""
                lines.append(f"  [{row['tab']}] «{row.get('section')}»: отмечено {row.get('marked_count')} из {row.get('total')}{extra}")

    return "\n".join(lines)


__all__ = [
    "AcChecklistSyncError",
    "CHECKLIST_META_PROPERTY",
    "CHECKLIST_SHARD_PROPERTY",
    "run_ac_checklist_sync_workflow",
    "render_text_report",
]
