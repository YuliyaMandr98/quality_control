"""Upload reviewed test cases into Azure DevOps Test Plan suites.

Adapted from trace2quality's scripts/upload_reviewed_test_cases.py +
scripts/spec_common.py for in-app execution. Takes an already-reviewed test
case CSV for a single User Story and uploads it into the matching Azure
DevOps Test Plan suite, resolving (or creating) the full
root -> [Админ Панель] -> Epic -> User Story suite chain automatically from
the User Story's Confluence ancestors.

Differences from the standalone script:
- The CSV is uploaded through the browser (its text content is passed in
  directly) instead of being auto-discovered on local disk.
- `sys.exit()` calls become a returned `{"status": "failed", "error": ...}`
  dict, since a web workflow can't terminate the process.
- Always resolves the suite chain and previews the parsed CSV first
  (`dry_run=True` by default); actually creating/uploading test cases is an
  explicit opt-in, same convention as the other workflows in this app.
"""

import csv
import io
import re
from typing import Any, Callable, Optional

from packages.common import get_logger

logger = get_logger(__name__)

# Business US pages are titled "US-<n>"; admin-panel US pages use "AUS-<n>".
_US_NUMBER_PATTERN_TEMPLATE = r"\bA?US-{num}\b"

DEFAULT_SPECS_FOLDER_TITLE = "Фаза 1: спецификации"
DEFAULT_ADMIN_SPECS_FOLDER_ID = "10321934"
DEFAULT_ADMIN_GROUP_SUITE_TITLE = "Админ Панель"
DEFAULT_STATE = "Ready"

# Test Plans available in the UI dropdown.
TEST_PLANS = {
    "web": {"plan_id": "15751", "label": "Web Test Cases (plan 15751)"},
    "mobile": {"plan_id": "438", "label": "Mobile Test Cases (plan 438)"},
    "api": {"plan_id": "2015", "label": "API Test Cases (plan 2015)"},
}

_PRIORITY_MAP = {"1": "High", "2": "Medium", "3": "Low", "4": "Low"}


class UploadResolutionError(Exception):
    """Raised when the User Story page or suite chain can't be unambiguously resolved."""


def normalize_us_number(raw: str) -> str:
    """Accept '11.2.1', 'US-11.2.1', 'us 11.2.1' and return the bare dotted number."""
    match = re.search(r"([\d]+(?:\.[\d]+)*)", raw)
    if not match:
        raise UploadResolutionError(f"Не удалось извлечь номер User Story из '{raw}'")
    return match.group(1)


def detect_prefix_hint(raw: str) -> Optional[str]:
    """Detect whether the user's input explicitly says "AUS" (admin-panel) or
    "US" (regular), to disambiguate colliding numbers. None if bare number."""
    upper = raw.upper()
    if "AUS" in upper:
        return "AUS"
    if "US" in upper:
        return "US"
    return None


def _is_admin_titled(title: str, us_number: str) -> bool:
    return bool(re.search(rf"\bAUS-{re.escape(us_number)}\b", title, re.IGNORECASE))


def match_us_pages(
    descendants: list[dict[str, Any]], us_number: str, prefix_hint: Optional[str] = None
) -> list[dict[str, Any]]:
    if prefix_hint == "AUS":
        pattern = re.compile(rf"\bAUS-{re.escape(us_number)}\b", re.IGNORECASE)
    elif prefix_hint == "US":
        pattern = re.compile(rf"\bUS-{re.escape(us_number)}\b", re.IGNORECASE)
    else:
        pattern = re.compile(_US_NUMBER_PATTERN_TEMPLATE.format(num=re.escape(us_number)), re.IGNORECASE)
    matches = [p for p in descendants if pattern.search(p.get("title", ""))]

    if prefix_hint is None and matches:
        admin_flags = {_is_admin_titled(m.get("title", ""), us_number) for m in matches}
        if len(admin_flags) > 1:
            candidates = "; ".join(f"{m['title']} (id={m['id']})" for m in matches)
            raise UploadResolutionError(
                f"Номер {us_number} совпадает у обычной User Story и у админ-панельной (AUS) страницы - "
                f"это разные пространства нумерации: {candidates}. "
                f"Укажите явно \"US-{us_number}\" или \"AUS-{us_number}\"."
            )

    return matches


async def _resolve_folder_root(confluence_client, folder_title: Optional[str], folder_id: Optional[str]):
    if folder_id:
        return await confluence_client.get_page(folder_id)
    return await confluence_client.find_page_by_title(folder_title) if folder_title else None


async def _search_folder(
    confluence_client, folder_title: Optional[str], folder_id: Optional[str], us_number: str,
    prefix_hint: Optional[str] = None, log_fn: Optional[Callable] = None,
) -> Optional[dict[str, Any]]:
    def _log(msg: str) -> None:
        if log_fn:
            log_fn("DEBUG", msg)

    root = await _resolve_folder_root(confluence_client, folder_title, folder_id)
    if not root:
        _log(f"Confluence-папка '{folder_title or f'id={folder_id}'}' не найдена")
        return None

    label = root.get("title", folder_title or f"id={folder_id}")
    _log(f"Найдена папка '{label}' (page id={root['id']}), сканирую дочерние страницы…")
    descendants = await confluence_client.get_all_child_pages_recursive(root["id"])
    _log(f"Найдено {len(descendants)} страниц под '{label}'.")

    matches = match_us_pages(descendants, us_number, prefix_hint=prefix_hint)
    if not matches:
        _log(f"Страница для US-{us_number}/AUS-{us_number} не найдена под '{label}'.")
        return None

    best = min(matches, key=lambda p: len(p.get("title", "")))
    if len(matches) > 1:
        _log(f"Найдено {len(matches)} совпадений, выбрана самая короткая: '{best['title']}' (id={best['id']})")
    return best


async def find_us_page_under_folder(
    confluence_client, folder_title: str, us_number: str,
    admin_folder_id: Optional[str] = DEFAULT_ADMIN_SPECS_FOLDER_ID,
    prefix_hint: Optional[str] = None, log_fn: Optional[Callable] = None,
) -> dict[str, Any]:
    """Find the page for `US-{us_number}` (or `AUS-{us_number}`), falling back
    to the admin-panel specs folder. Raises UploadResolutionError if not found."""
    found = await _search_folder(confluence_client, folder_title, None, us_number, prefix_hint=prefix_hint, log_fn=log_fn)
    if found:
        return found

    if admin_folder_id:
        if log_fn:
            log_fn("DEBUG", f"Пробую fallback-папку админ-панели (id={admin_folder_id})…")
        found = await _search_folder(confluence_client, None, admin_folder_id, us_number, prefix_hint=prefix_hint, log_fn=log_fn)
        if found:
            return found

    raise UploadResolutionError(
        f"Страница для US-{us_number}/AUS-{us_number} не найдена ни в '{folder_title}', ни в fallback-папке."
    )


async def resolve_epic_context(
    confluence_client, us_page_id: str, admin_group_title: str = DEFAULT_ADMIN_GROUP_SUITE_TITLE,
) -> dict[str, Any]:
    """Resolve the Epic title (and whether it sits under an admin-panel grouping)
    for a User Story page, from its Confluence ancestor chain."""
    page = await confluence_client.get_page(us_page_id)
    ancestors = (page or {}).get("ancestors", [])
    if not ancestors:
        raise UploadResolutionError(f"У страницы {us_page_id} нет предков в Confluence - не могу определить Epic.")

    epic_title = ancestors[-1]["title"]
    needs_admin_group = len(ancestors) >= 2 and ancestors[-2].get("title") == admin_group_title
    return {"epic_title": epic_title, "needs_admin_group": needs_admin_group, "admin_group_title": admin_group_title}


def parse_test_cases_csv_text(csv_text: str) -> list[dict[str, Any]]:
    """Parse an Azure DevOps Test Plan CSV export (9- or 10-column) into test cases.

    Any pre-existing "ID"/"State"/"Area Path" columns are template
    placeholders, not references to real Azure DevOps work items - they are
    ignored other than for title-based dedup.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    test_cases: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for row in reader:
        wtype = (row.get("Work Item Type") or "").strip()
        title = (row.get("Title") or "").strip()
        step_num = (row.get("Test Step") or "").strip()

        if wtype.lower() == "test case" and title:
            current = {
                "id": (row.get("ID") or "").strip(),
                "title": title,
                "priority": (row.get("Priority") or "").strip(),
                "steps": [],
            }
            test_cases.append(current)
            if step_num:
                current["steps"].append({
                    "num": step_num,
                    "action": (row.get("Step Action") or "").strip(),
                    "expected": (row.get("Step Expected") or "").strip(),
                })
            continue

        if step_num and current is not None:
            current["steps"].append({
                "num": step_num,
                "action": (row.get("Step Action") or "").strip(),
                "expected": (row.get("Step Expected") or "").strip(),
            })

    if not test_cases:
        raise UploadResolutionError("В CSV-файле не найдено ни одного тест-кейса (Work Item Type = 'Test Case').")
    return test_cases


def normalize_priority(value: str) -> str:
    value = (value or "").strip()
    if value in ("High", "Medium", "Low"):
        return value
    return _PRIORITY_MAP.get(value, "Medium")


async def _root_suite_id(azure_client, plan_id: str) -> str:
    suites = await azure_client.fetch_suites(plan_id)
    roots = [s for s in suites if not s.get("parent")]
    if not roots:
        raise UploadResolutionError(f"Не удалось найти корневой suite для плана {plan_id}.")
    return str(roots[0]["id"])


async def resolve_suite_chain(
    azure_client, plan_id: str, epic_title: str, us_suite_name: str,
    needs_admin_group: bool, admin_group_title: str = DEFAULT_ADMIN_GROUP_SUITE_TITLE, dry_run: bool = False,
) -> dict[str, Any]:
    """Resolve (or create) the root -> [admin group] -> Epic -> US suite chain.

    Returns {"us_suite_id": str|None, "levels": [{"title","id","status"}]},
    where status is "found", "created", or "would_create" (dry_run).
    """
    levels: list[dict[str, Any]] = []
    root_id = await _root_suite_id(azure_client, plan_id)
    levels.append({"title": "<root>", "id": root_id, "status": "found"})

    parent_id = root_id
    if needs_admin_group:
        found = await azure_client.find_suite(plan_id, admin_group_title, parent_id)
        if found:
            levels.append({"title": admin_group_title, "id": found, "status": "found"})
            parent_id = found
        elif dry_run:
            levels.append({"title": admin_group_title, "id": None, "status": "would_create"})
            parent_id = None
        else:
            created = await azure_client.get_or_create_suite(plan_id, admin_group_title, parent_id)
            levels.append({"title": admin_group_title, "id": created, "status": "created"})
            parent_id = created

    found = await azure_client.find_suite(plan_id, epic_title, parent_id) if parent_id else None
    if found:
        levels.append({"title": epic_title, "id": found, "status": "found"})
        parent_id = found
    elif dry_run:
        levels.append({"title": epic_title, "id": None, "status": "would_create"})
        parent_id = None
    else:
        created = await azure_client.get_or_create_suite(plan_id, epic_title, parent_id)
        levels.append({"title": epic_title, "id": created, "status": "created"})
        parent_id = created

    found = await azure_client.find_suite(plan_id, us_suite_name, parent_id) if parent_id else None
    if found:
        levels.append({"title": us_suite_name, "id": found, "status": "found"})
        us_suite_id = found
    elif dry_run:
        levels.append({"title": us_suite_name, "id": None, "status": "would_create"})
        us_suite_id = None
    else:
        created = await azure_client.get_or_create_suite(plan_id, us_suite_name, parent_id)
        levels.append({"title": us_suite_name, "id": created, "status": "created"})
        us_suite_id = created

    return {"us_suite_id": us_suite_id, "levels": levels}


async def _create_with_retry(
    azure_client, plan_id: str, suite_id: str, title: str, priority: str,
    steps: list[dict], state: str, max_attempts: int = 3,
) -> dict:
    import asyncio

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        result = await azure_client.create_test_case_in_suite(
            test_plan_id=plan_id, suite_id=suite_id, title=title, priority=priority, steps=steps, state=state,
        )
        if result.get("success"):
            return result
        last_error = str(result.get("error", ""))
        if attempt < max_attempts:
            await asyncio.sleep(5 * attempt)
    return {"success": False, "error": last_error}


async def run_upload_test_cases_workflow(
    azure_client,
    confluence_client,
    *,
    us: str,
    plan_id: str,
    csv_text: str,
    specs_folder: str = DEFAULT_SPECS_FOLDER_TITLE,
    admin_specs_folder_id: str = DEFAULT_ADMIN_SPECS_FOLDER_ID,
    admin_group_title: str = DEFAULT_ADMIN_GROUP_SUITE_TITLE,
    epic_suite_name: Optional[str] = None,
    us_suite_name: Optional[str] = None,
    state: str = DEFAULT_STATE,
    force: bool = False,
    dry_run: bool = True,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
) -> dict[str, Any]:
    """Resolve the suite chain for a User Story and upload its reviewed test case
    CSV into Azure DevOps. Always previews (`dry_run=True`) unless explicitly
    told to write; `force=True` additionally wipes the suite's existing test
    cases before re-creating everything from the CSV.
    """

    def _log(level: str, msg: str) -> None:
        logger.info(msg) if level == "INFO" else logger.warning(msg) if level == "WARNING" else logger.debug(msg)
        if log_fn:
            log_fn(level, msg)

    _log("INFO", f"Upload workflow started: us={us}, plan_id={plan_id}, dry_run={dry_run}, force={force}")

    try:
        us_number = normalize_us_number(us)
        prefix_hint = detect_prefix_hint(us)

        test_cases = parse_test_cases_csv_text(csv_text)
        _log("INFO", f"Разобрано {len(test_cases)} тест-кейс(ов) из CSV")

        us_page = await find_us_page_under_folder(
            confluence_client, specs_folder, us_number,
            admin_folder_id=admin_specs_folder_id or None, prefix_hint=prefix_hint, log_fn=_log,
        )
        _log("INFO", f"Найдена страница User Story: '{us_page['title']}' (id={us_page['id']})")

        epic_ctx = await resolve_epic_context(confluence_client, us_page["id"], admin_group_title=admin_group_title)
        epic_title = epic_suite_name or epic_ctx["epic_title"]
        final_us_suite_name = us_suite_name or us_page["title"]
        _log("INFO", f"Epic: '{epic_title}' | Admin-группа нужна: {epic_ctx['needs_admin_group']}")
    except UploadResolutionError as exc:
        _log("ERROR", str(exc))
        return {"status": "failed", "error": str(exc)}
    except Exception as exc:
        _log("ERROR", f"Не удалось разрешить контекст US/CSV: {exc}")
        return {"status": "failed", "error": str(exc)}

    try:
        chain = await resolve_suite_chain(
            azure_client, plan_id, epic_title, final_us_suite_name,
            needs_admin_group=epic_ctx["needs_admin_group"], admin_group_title=admin_group_title, dry_run=dry_run,
        )
    except Exception as exc:
        _log("ERROR", f"Не удалось разрешить/создать цепочку suite: {exc}")
        return {"status": "failed", "error": str(exc)}

    for level in chain["levels"]:
        if level["title"] == "<root>":
            continue
        marker = {"found": "найден", "created": "создан", "would_create": "будет создан"}[level["status"]]
        _log("INFO", f"Suite '{level['title']}' (id={level['id']}): {marker}")

    us_suite_id = chain["us_suite_id"]

    existing_titles_lower: dict[str, str] = {}
    existing_count = 0
    if us_suite_id:
        existing_tcs = await azure_client.fetch_test_cases_for_suite(plan_id, us_suite_id)
        existing_count = len(existing_tcs)
        _log("INFO", f"Существующих ТК в сьюте: {existing_count}")
        if force and not dry_run and existing_tcs:
            tc_ids = [str(tc["workItem"]["id"]) for tc in existing_tcs if tc.get("workItem", {}).get("id")]
            _log("WARNING", f"[Force] Убираю {len(tc_ids)} существующих ТК из suite перед загрузкой "
                            f"(тест-кейсы не удаляются навсегда - только отвязываются от этого suite)...")
            remove_result = await azure_client.remove_test_cases_from_suite(plan_id, us_suite_id, tc_ids)
            if not remove_result.get("success"):
                _log("WARNING", f"Не удалось убрать ТК из suite ({remove_result.get('error')}); они будут учтены для дедупликации.")
            else:
                existing_tcs = []
        existing_titles_lower = {
            tc.get("workItem", {}).get("name", "").lower(): str(tc.get("workItem", {}).get("id"))
            for tc in existing_tcs if tc.get("workItem", {}).get("name")
        }

    preview_rows = [
        {
            "title": tc["title"],
            "priority": normalize_priority(tc.get("priority", "")),
            "steps_count": len(tc["steps"]),
            "duplicate": tc["title"].lower() in existing_titles_lower,
        }
        for tc in test_cases
    ]

    base_result = {
        "us_number": us_number,
        "prefix_hint": prefix_hint,
        "plan_id": plan_id,
        "epic_title": epic_title,
        "us_page_title": us_page["title"],
        "us_suite_name": final_us_suite_name,
        "us_suite_id": us_suite_id,
        "chain_levels": chain["levels"],
        "existing_count": existing_count,
        "dry_run": dry_run,
        "force": force,
        "test_cases_total": len(test_cases),
        "preview": preview_rows,
        "correlation_id": correlation_id,
    }

    if dry_run:
        _log("INFO", f"[DRY-RUN] Готово к загрузке {len(test_cases)} тест-кейс(ов), запись в Azure DevOps не выполнялась")
        return {"status": "succeeded", "results": [], "created_count": 0, "skipped_count": 0, "failed_count": 0, **base_result}

    if not us_suite_id:
        _log("ERROR", "Suite chain не разрешён - невозможно загрузить тест-кейсы")
        return {"status": "failed", "error": "Suite chain could not be resolved", **base_result}

    results: list[dict[str, Any]] = []
    created_count = 0
    skipped_count = 0
    for i, tc in enumerate(test_cases, 1):
        if not force and tc["title"].lower() in existing_titles_lower:
            existing_id = existing_titles_lower[tc["title"].lower()]
            _log("INFO", f"[{i}/{len(test_cases)}] Пропущен (уже существует, id={existing_id}): {tc['title']}")
            results.append({"title": tc["title"], "result": {"success": True, "skipped": True, "case_id": existing_id}})
            skipped_count += 1
            continue

        result = await _create_with_retry(
            azure_client, plan_id, us_suite_id, tc["title"], normalize_priority(tc.get("priority", "")), tc["steps"], state,
        )
        results.append({"title": tc["title"], "result": result})
        if result.get("success"):
            created_count += 1
            _log("INFO", f"[{i}/{len(test_cases)}] Создан ТК {result['case_id']}: {tc['title']}")
        else:
            _log("ERROR", f"[{i}/{len(test_cases)}] ОШИБКА: {tc['title']} -> {str(result.get('error'))[:200]}")

    failed_count = len(results) - created_count - skipped_count
    _log("INFO", f"Загрузка завершена: создано={created_count}, пропущено={skipped_count}, ошибок={failed_count}")

    return {
        "status": "succeeded",
        "results": results,
        "created_count": created_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        **base_result,
    }


__all__ = [
    "TEST_PLANS",
    "UploadResolutionError",
    "run_upload_test_cases_workflow",
    "parse_test_cases_csv_text",
]
