"""Set Azure DevOps test case Priority for test cases that already exist,
from the same P0/P1/P2 business-criticality classification `upload_test_cases`
uses at creation time.

Companion to `upload_test_cases`, not a replacement: this workflow never
creates suites or test cases - it only reads the existing suite chain / test
cases in Azure DevOps and (in apply mode) PATCHes their Priority field.

Two scopes:
- "single_us": one User Story's existing suite (must already exist).
- "whole_plan": every suite in a Test Plan whose name resolves to a
  US-<n>/AUS-<n> Confluence spec, reprioritized in one run.

Always previews (`dry_run=True`) unless explicitly told to write, same
convention as every other workflow in this app.
"""

import asyncio
import re
from typing import Any, Callable, Optional

from bs4 import BeautifulSoup

from packages.common import get_logger
from packages.workflows.upload_test_cases import (
    DEFAULT_ADMIN_GROUP_SUITE_TITLE,
    DEFAULT_ADMIN_SPECS_FOLDER_ID,
    DEFAULT_SPECS_FOLDER_TITLE,
    UploadResolutionError,
    detect_prefix_hint,
    find_us_page_under_folder,
    normalize_us_number,
    resolve_epic_context,
    resolve_suite_chain,
)

logger = get_logger(__name__)

SCOPES = ("single_us", "whole_plan")

# Business-criticality tier (from classify_business_priority) -> Azure DevOps
# test case Priority. Same mapping as upload_test_cases.
_TIER_TO_PRIORITY = {"P0": "High", "P1": "Medium", "P2": "Low"}
_PRIORITY_INT_TO_STRING = {1: "High", 2: "Medium", 3: "Low"}

# Suite names are the Confluence US/AUS page's own title (set by
# upload_test_cases when it first created the suite), e.g.
# "US-11.1.2 | API Подача заявки на кредит" or "AUS-7.2 | ...".
_SUITE_US_PATTERN = re.compile(r"\b(AUS|US)-(\d+(?:\.\d+)*)\b", re.IGNORECASE)


def _text_from_html(storage_html: str) -> str:
    """Convert Confluence storage-format HTML to plain text."""
    soup = BeautifulSoup(storage_html or "", "html.parser")
    return soup.get_text("\n", strip=True)


def _parse_steps_field(steps_xml: str) -> str:
    """Extract a compact "action -> expected" text from a Test Case work
    item's Microsoft.VSTS.TCM.Steps XML field, for feeding into an LLM
    prompt (see classify_test_case_priorities). Each step's action/expected
    is itself HTML (possibly escaped) - parsed with BeautifulSoup twice
    (once for the outer <step> structure, once for any nested HTML) to get
    plain text either way."""
    if not steps_xml:
        return ""
    soup = BeautifulSoup(steps_xml, "html.parser")
    parts = []
    for step in soup.find_all("step"):
        strings = step.find_all("parameterizedstring")
        action = BeautifulSoup(strings[0].get_text(), "html.parser").get_text(" ", strip=True) if len(strings) > 0 else ""
        expected = BeautifulSoup(strings[1].get_text(), "html.parser").get_text(" ", strip=True) if len(strings) > 1 else ""
        if not action and not expected:
            continue
        parts.append(f"{action} -> {expected}" if expected else action)
    return "; ".join(parts)


def _match_suite_to_us(suite_name: str) -> Optional[tuple[str, str]]:
    """Extract (prefix_hint, us_number) from a suite name, or None if it isn't a US/AUS suite."""
    match = _SUITE_US_PATTERN.search(suite_name or "")
    if not match:
        return None
    return match.group(1).upper(), match.group(2)


async def _classify_us_priority(confluence_client, llm_client, us_page: dict[str, Any]) -> dict[str, Any]:
    """Fetch a User Story page's full spec text and classify its business tier."""
    full_page = await confluence_client.get_page(us_page["id"])
    storage_html = (full_page or {}).get("body", {}).get("storage", {}).get("value", "")
    spec_text = _text_from_html(storage_html)
    return await llm_client.classify_business_priority(us_title=us_page["title"], us_text=spec_text)


async def _reprioritize_suite_test_cases(
    azure_client, llm_client, plan_id: str, suite_id: str, us_title: str,
    business_tier: str, business_priority: str,
    dry_run: bool, log_fn: Callable[[str, str], None], should_cancel_fn: Optional[Callable[[], bool]],
) -> dict[str, Any]:
    """Fetch existing test cases in a suite, classify each one's OWN priority
    within the feature (not every test case in a critical feature is equally
    critical), and preview/apply the result."""
    test_cases = await azure_client.fetch_test_cases_for_suite(plan_id, suite_id)

    items: list[dict[str, Any]] = []
    for tc in test_cases:
        if should_cancel_fn and should_cancel_fn():
            break
        wi = tc.get("workItem", {})
        wi_id = str(wi.get("id", ""))
        title = wi.get("name", "")
        if not wi_id:
            continue
        fields = await azure_client.get_work_item_fields(
            wi_id, ["Microsoft.VSTS.Common.Priority", "Microsoft.VSTS.TCM.Steps"]
        )
        old_priority_int = fields.get("Microsoft.VSTS.Common.Priority")
        old_priority = _PRIORITY_INT_TO_STRING.get(old_priority_int, str(old_priority_int or "?"))
        steps_text = _parse_steps_field(fields.get("Microsoft.VSTS.TCM.Steps", ""))
        items.append({"id": wi_id, "title": title, "steps_text": steps_text, "old_priority": old_priority})

    if not items:
        return {"test_cases": [], "updated_count": 0, "failed_count": 0}

    try:
        classifications = await llm_client.classify_test_case_priorities(
            us_title=us_title, business_tier=business_tier, business_priority=business_priority,
            test_cases=[{"id": it["id"], "title": it["title"], "steps_text": it["steps_text"]} for it in items],
        )
    except Exception as exc:
        log_fn(
            "WARNING",
            f"  Не удалось классифицировать приоритет тест-кейсов индивидуально, использую "
            f"{business_priority} для всех: {exc}",
        )
        classifications = [
            {"id": it["id"], "priority": business_priority, "reasoning": f"Classification error: {exc}"}
            for it in items
        ]
    by_id = {c["id"]: c for c in classifications}

    rows: list[dict[str, Any]] = []
    updated = 0
    failed = 0
    for it in items:
        if should_cancel_fn and should_cancel_fn():
            break
        wi_id = it["id"]
        title = it["title"]
        old_priority = it["old_priority"]
        classified = by_id.get(wi_id, {})
        new_priority = classified.get("priority", business_priority)

        row: dict[str, Any] = {
            "id": wi_id,
            "title": title,
            "old_priority": old_priority,
            "new_priority": new_priority,
            "priority_reasoning": classified.get("reasoning", ""),
            "changed": old_priority != new_priority,
            "updated": False,
            "error": None,
        }

        if not dry_run:
            result = await azure_client.update_test_case_priority(wi_id, new_priority)
            if result.get("success"):
                row["updated"] = True
                updated += 1
                log_fn("INFO", f"  ТК {wi_id} '{title}': {old_priority} -> {new_priority}")
            else:
                row["error"] = result.get("error")
                failed += 1
                log_fn("ERROR", f"  ТК {wi_id} '{title}': ошибка обновления - {result.get('error')}")

        rows.append(row)

    return {"test_cases": rows, "updated_count": updated, "failed_count": failed}


async def run_prioritize_test_cases_workflow(
    azure_client,
    confluence_client,
    llm_client,
    *,
    scope: str,
    plan_id: str,
    us: Optional[str] = None,
    specs_folder: str = DEFAULT_SPECS_FOLDER_TITLE,
    admin_specs_folder_id: str = DEFAULT_ADMIN_SPECS_FOLDER_ID,
    admin_group_title: str = DEFAULT_ADMIN_GROUP_SUITE_TITLE,
    batch_delay_seconds: int = 10,
    dry_run: bool = True,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Reprioritize test cases that already exist in Azure DevOps, per-suite,
    from an LLM classification of the suite's User Story spec. Never creates
    suites or test cases - suites that don't already exist are reported as
    unresolved/skipped rather than created.
    """

    def _log(level: str, msg: str) -> None:
        logger.info(msg) if level == "INFO" else logger.warning(msg) if level == "WARNING" else logger.debug(msg)
        if log_fn:
            log_fn(level, msg)

    if scope not in SCOPES:
        return {"status": "failed", "error": f"Unknown scope: {scope!r}, expected one of {SCOPES}"}

    _log("INFO", f"Prioritize workflow started: scope={scope}, plan_id={plan_id}, us={us}, dry_run={dry_run}")

    if should_cancel_fn and should_cancel_fn():
        _log("WARNING", "Остановлено пользователем")
        return {"status": "canceled", "error": "Остановлено пользователем"}

    # Each target is one Azure DevOps suite already known to hold test cases
    # for a specific User Story - {prefix_hint, us_number, us_page (or None,
    # resolved later), suite_id, suite_name}.
    suite_targets: list[dict[str, Any]] = []

    if scope == "single_us":
        if not (us or "").strip():
            return {"status": "failed", "error": "Укажите номер User Story."}
        try:
            us_number = normalize_us_number(us)
            prefix_hint = detect_prefix_hint(us)
            us_page = await find_us_page_under_folder(
                confluence_client, specs_folder, us_number,
                admin_folder_id=admin_specs_folder_id or None, prefix_hint=prefix_hint, log_fn=_log,
                should_cancel_fn=should_cancel_fn,
            )
            epic_ctx = await resolve_epic_context(confluence_client, us_page["id"], admin_group_title=admin_group_title)
            us_suite_name = us_page["title"]
        except UploadResolutionError as exc:
            _log("ERROR", str(exc))
            return {"status": "failed", "error": str(exc)}
        except Exception as exc:
            _log("ERROR", f"Не удалось разрешить контекст US: {exc}")
            return {"status": "failed", "error": str(exc)}

        # dry_run=True here is intentional and NOT the workflow's own dry_run
        # flag - this workflow must never create a suite, only find one that
        # already exists (resolve_suite_chain in dry_run mode is read-only).
        try:
            chain = await resolve_suite_chain(
                azure_client, plan_id, epic_ctx["epic_title"], us_suite_name,
                needs_admin_group=epic_ctx["needs_admin_group"], admin_group_title=admin_group_title, dry_run=True,
            )
        except Exception as exc:
            _log("ERROR", f"Не удалось разрешить цепочку suite: {exc}")
            return {"status": "failed", "error": str(exc)}

        if not chain["us_suite_id"]:
            msg = f"Suite для '{us_suite_name}' ещё не существует в Azure DevOps (plan {plan_id}) - нечего переприоритизировать."
            _log("ERROR", msg)
            return {"status": "failed", "error": msg}

        for level in chain["levels"]:
            if level.get("status") == "found_renamed":
                _log(
                    "WARNING",
                    f"Suite не найден по точному названию '{level['title']}' - использован suite с тем же "
                    f"номером US, но текущим названием в Azure DevOps: '{level.get('matched_name')}' "
                    f"(id={level.get('id')}). Похоже, страница в Confluence была переименована после создания "
                    f"suite - стоит переименовать suite в Azure DevOps, чтобы название снова совпадало.",
                )
            elif level.get("status") == "found_elsewhere":
                _log(
                    "WARNING",
                    f"Suite '{level['title']}' (id={level.get('id')}) не найден под ожидаемым родителем - "
                    f"использован suite с точно таким же названием, найденный в другом месте плана. Похоже, "
                    f"в Confluence между Epic и User Story есть дополнительный уровень вложенности.",
                )

        suite_targets.append({
            "prefix_hint": prefix_hint, "us_number": us_number, "us_page": us_page,
            "suite_id": chain["us_suite_id"], "suite_name": us_suite_name,
        })
    else:
        all_suites = await azure_client.fetch_suites(plan_id)
        _log("INFO", f"Найдено {len(all_suites)} suite(ов) в плане {plan_id}, ищу совпадения с US/AUS…")
        for suite in all_suites:
            suite_name = suite.get("name", "")
            matched = _match_suite_to_us(suite_name)
            if not matched:
                continue
            prefix_hint, us_number = matched
            suite_targets.append({
                "prefix_hint": prefix_hint, "us_number": us_number, "us_page": None,
                "suite_id": str(suite["id"]), "suite_name": suite_name,
            })
        _log("INFO", f"{len(suite_targets)} suite(ов) распознаны как US/AUS-сьюты.")

    if not suite_targets:
        _log("WARNING", "Не найдено ни одного suite для обработки.")
        return {
            "status": "succeeded", "scope": scope, "plan_id": plan_id, "dry_run": dry_run,
            "suites_total": 0, "suites_processed": 0, "results": [],
            "test_cases_total": 0, "updated_count": 0, "failed_count": 0,
            "correlation_id": correlation_id,
        }

    results: list[dict[str, Any]] = []
    test_cases_total = 0
    updated_count = 0
    failed_count = 0
    canceled = False

    for i, target in enumerate(suite_targets, 1):
        if should_cancel_fn and should_cancel_fn():
            _log("WARNING", f"Остановлено пользователем после {i - 1}/{len(suite_targets)} suite(ов)")
            canceled = True
            break

        us_number = target["us_number"]
        suite_name = target["suite_name"]
        _log("INFO", f"[{i}/{len(suite_targets)}] Suite '{suite_name}' (id={target['suite_id']})")

        entry: dict[str, Any] = {
            "us_number": us_number,
            "suite_id": target["suite_id"],
            "suite_name": suite_name,
            "business_priority_tier": None,
            "business_priority": None,
            "business_priority_reasoning": None,
            "test_cases": [],
            "updated_count": 0,
            "failed_count": 0,
            "error": None,
        }

        try:
            us_page = target["us_page"]
            if us_page is None:
                us_page = await find_us_page_under_folder(
                    confluence_client, specs_folder, us_number,
                    admin_folder_id=admin_specs_folder_id or None, prefix_hint=target["prefix_hint"], log_fn=_log,
                    should_cancel_fn=should_cancel_fn,
                )

            if i > 1 and batch_delay_seconds > 0:
                _log("DEBUG", f"  Waiting {batch_delay_seconds}s before LLM call")
                await asyncio.sleep(batch_delay_seconds)

            classification = await _classify_us_priority(confluence_client, llm_client, us_page)
        except UploadResolutionError as exc:
            entry["error"] = str(exc)
            _log("WARNING", f"  Пропущен: {exc}")
            results.append(entry)
            continue
        except Exception as exc:
            entry["error"] = str(exc)
            _log("WARNING", f"  Пропущен из-за ошибки: {exc}")
            results.append(entry)
            continue

        tier = classification.get("tier", "P1")
        business_priority = _TIER_TO_PRIORITY.get(tier, "Medium")
        entry["business_priority_tier"] = tier
        entry["business_priority"] = business_priority
        entry["business_priority_reasoning"] = classification.get("reasoning", "")
        _log("INFO", f"  Бизнес-приоритет: {tier} -> {business_priority}. {classification.get('reasoning', '')}")

        suite_result = await _reprioritize_suite_test_cases(
            azure_client, llm_client, plan_id, target["suite_id"], us_page["title"],
            tier, business_priority, dry_run, _log, should_cancel_fn,
        )
        entry["test_cases"] = suite_result["test_cases"]
        entry["updated_count"] = suite_result["updated_count"]
        entry["failed_count"] = suite_result["failed_count"]
        test_cases_total += len(suite_result["test_cases"])
        updated_count += suite_result["updated_count"]
        failed_count += suite_result["failed_count"]

        results.append(entry)

    _log(
        "INFO",
        f"{'Остановлено' if canceled else 'Готово'}: suite(ов) обработано={len(results)}/{len(suite_targets)}, "
        f"тест-кейсов={test_cases_total}, обновлено={updated_count}, ошибок={failed_count}",
    )

    return {
        "status": "canceled" if canceled else "succeeded",
        "error": "Остановлено пользователем" if canceled else None,
        "scope": scope,
        "plan_id": plan_id,
        "dry_run": dry_run,
        "suites_total": len(suite_targets),
        "suites_processed": len(results),
        "results": results,
        "test_cases_total": test_cases_total,
        "updated_count": updated_count,
        "failed_count": failed_count,
        "correlation_id": correlation_id,
    }


__all__ = ["SCOPES", "run_prioritize_test_cases_workflow"]
