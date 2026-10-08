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

from bs4 import BeautifulSoup

from packages.common import get_logger

logger = get_logger(__name__)

# Business US pages are titled "US-<n>"; admin-panel US pages use "AUS-<n>".
_US_NUMBER_PATTERN_TEMPLATE = r"\bA?US-{num}\b"

# US suite names are set to the Confluence page's own title at creation time
# (see resolve_suite_chain) - if that page later gets renamed, the two drift
# apart. This extracts just the "US-<n>"/"AUS-<n>" leading token, which is
# stable even when the rest of the title changes, so a suite can still be
# found by number after a rename (see resolve_suite_chain's fallback).
_SUITE_NUMBER_PATTERN = re.compile(r"^(A?US-\d+(?:\.\d+)*)\b", re.IGNORECASE)


def _suite_number_token(name: str) -> Optional[str]:
    match = _SUITE_NUMBER_PATTERN.match((name or "").strip())
    return match.group(1).upper() if match else None


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

# Business-criticality tier (from classify_business_priority) -> Azure DevOps
# test case Priority. Replaces whatever the CSV's own Priority column said -
# that column reflects per-case ordering picked by whoever wrote the CSV, not
# the team's P0/P1/P2 release-risk rubric, so the two aren't meant to agree.
_TIER_TO_PRIORITY = {"P0": "High", "P1": "Medium", "P2": "Low"}


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


async def _cql_candidates_under_folder(
    confluence_client, folder_id: str, us_number: str, prefix_hint: Optional[str], log_fn: Optional[Callable] = None,
) -> list[dict[str, Any]]:
    """Fast path: ask Confluence's own search index for pages under `folder_id`
    whose title plausibly matches the US number, instead of paginating every
    descendant page one HTTP request at a time (see `get_all_child_pages_recursive`
    fallback below - on a specs folder with hundreds of pages that full crawl can
    take several minutes). Returns raw candidates for `match_us_pages` to filter
    precisely; may return false positives (broader match) but never false negatives
    for a well-formed CQL query, since strategy 2 is a broad substring match.
    """
    space_filter = f' AND space = "{confluence_client.space}"' if getattr(confluence_client, "space", None) else ""
    token = f"{prefix_hint}-{us_number}" if prefix_hint else us_number

    async def _search(cql: str) -> list[dict[str, Any]]:
        try:
            return await confluence_client.search_pages(cql, limit=100)
        except Exception as exc:
            if log_fn:
                log_fn("DEBUG", f"  CQL search failed ({cql!r}): {exc}")
            return []

    # Strategy 1: quoted-phrase CQL - exact phrase, immune to '.'/'-' tokenization.
    quoted = token.replace('"', '\\"')
    cql1 = f'type=page AND ancestor={folder_id} AND title ~ "\\"{quoted}\\""' + space_filter
    results = await _search(cql1)
    if results:
        return results

    # Strategy 2: broad substring match on the number's major segment.
    major = us_number.split(".")[0]
    cql2 = f'type=page AND ancestor={folder_id} AND title ~ "{major}"' + space_filter
    return await _search(cql2)


async def _search_folder(
    confluence_client, folder_title: Optional[str], folder_id: Optional[str], us_number: str,
    prefix_hint: Optional[str] = None, log_fn: Optional[Callable] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> Optional[dict[str, Any]]:
    def _log(msg: str) -> None:
        if log_fn:
            log_fn("DEBUG", msg)

    root = await _resolve_folder_root(confluence_client, folder_title, folder_id)
    if not root:
        _log(f"Confluence-папка '{folder_title or f'id={folder_id}'}' не найдена")
        return None

    label = root.get("title", folder_title or f"id={folder_id}")
    _log(f"Найдена папка '{label}' (page id={root['id']}), ищу через Confluence-поиск (CQL)…")
    descendants = await _cql_candidates_under_folder(confluence_client, root["id"], us_number, prefix_hint, log_fn=log_fn)
    matches = match_us_pages(descendants, us_number, prefix_hint=prefix_hint)

    # CQL's search index can be flaky/inconsistent (occasionally misses a page
    # that a re-run finds instantly) - fall back to the exhaustive tree walk
    # whenever the fast path didn't yield a confirmed match, not merely when it
    # returned zero raw candidates (a broad strategy-2 query can return unrelated
    # candidates that all fail the exact-match filter, which looks "non-empty"
    # but still means the real match wasn't found).
    if not matches:
        _log(f"CQL-поиск не дал точного совпадения под '{label}', сканирую дерево страниц целиком (может занять время)…")
        descendants = await confluence_client.get_all_child_pages_recursive(
            root["id"], log_fn=log_fn, should_cancel_fn=should_cancel_fn
        )
        _log(f"Найдено {len(descendants)} страниц под '{label}' полным обходом.")
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
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Find the page for `US-{us_number}` (or `AUS-{us_number}`), falling back
    to the admin-panel specs folder. Raises UploadResolutionError if not found."""
    found = await _search_folder(
        confluence_client, folder_title, None, us_number,
        prefix_hint=prefix_hint, log_fn=log_fn, should_cancel_fn=should_cancel_fn,
    )
    if found:
        return found

    if admin_folder_id:
        if log_fn:
            log_fn("DEBUG", f"Пробую fallback-папку админ-панели (id={admin_folder_id})…")
        found = await _search_folder(
            confluence_client, None, admin_folder_id, us_number,
            prefix_hint=prefix_hint, log_fn=log_fn, should_cancel_fn=should_cancel_fn,
        )
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


def _text_from_html(storage_html: str) -> str:
    """Convert Confluence storage-format HTML to plain text."""
    soup = BeautifulSoup(storage_html or "", "html.parser")
    return soup.get_text("\n", strip=True)


def _steps_to_text(steps: list[dict[str, Any]]) -> str:
    """Join a test case's parsed steps into a compact "action -> expected"
    string, for feeding into an LLM prompt (see classify_test_case_priorities)."""
    parts = []
    for step in steps:
        action = (step.get("action") or "").strip()
        expected = (step.get("expected") or "").strip()
        if not action and not expected:
            continue
        parts.append(f"{action} -> {expected}" if expected else action)
    return "; ".join(parts)


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


async def _find_scoped_or_anywhere(
    azure_client, plan_id: str, name: str, parent_id: str
) -> tuple[Optional[str], bool]:
    """Find a suite by exact name under `parent_id`; if not found there, fall
    back to an unambiguous plan-wide match.

    The chain-resolution levels below assume a fixed depth (root -> [admin
    group] -> Epic -> US), derived from Confluence's immediate ancestor
    titles. Some branches nest an extra "feature group" page between Epic
    and User Story (e.g. Epic "E-1 | ..." -> group "US-1.1 | ..." -> leaf
    "US-1.1.1 | ..."), which pushes what looks like "the Epic"
    one level deeper in Azure DevOps than expected - `find_suite` scoped to
    the assumed parent then finds nothing, even though a suite with that
    exact name exists (just nested differently). Returns (suite_id, True) if
    the fallback lookup is what matched, so the caller can log it - a plan
    lookup would otherwise have to be trusted blindly, which is fine here
    only because `find_suite_anywhere` refuses to guess between duplicates.
    """
    found = await azure_client.find_suite(plan_id, name, parent_id)
    if found:
        return found, False
    found = await azure_client.find_suite_anywhere(plan_id, name)
    return found, bool(found)


async def resolve_suite_chain(
    azure_client, plan_id: str, epic_title: str, us_suite_name: str,
    needs_admin_group: bool, admin_group_title: str = DEFAULT_ADMIN_GROUP_SUITE_TITLE, dry_run: bool = False,
) -> dict[str, Any]:
    """Resolve (or create) the root -> [admin group] -> Epic -> US suite chain.

    Returns {"us_suite_id": str|None, "levels": [{"title","id","status"}]},
    where status is "found", "created", "would_create" (dry_run),
    "found_elsewhere" (the admin-group/Epic suite wasn't directly under its
    assumed parent - typically an extra Confluence nesting level pushing it
    deeper - but was unambiguously found by exact name elsewhere in the
    plan), or "found_renamed" (the US suite wasn't found by exact name -
    typically because the Confluence page was renamed after the suite was
    first created - but was unambiguously matched by its stable
    US-<n>/AUS-<n> number token instead; that level also carries
    "matched_name" with the suite's actual current name in Azure DevOps).
    """
    levels: list[dict[str, Any]] = []
    root_id = await _root_suite_id(azure_client, plan_id)
    levels.append({"title": "<root>", "id": root_id, "status": "found"})

    parent_id = root_id
    if needs_admin_group:
        found, elsewhere = await _find_scoped_or_anywhere(azure_client, plan_id, admin_group_title, parent_id)
        if found:
            levels.append({"title": admin_group_title, "id": found, "status": "found_elsewhere" if elsewhere else "found"})
            parent_id = found
        elif dry_run:
            levels.append({"title": admin_group_title, "id": None, "status": "would_create"})
            parent_id = None
        else:
            created = await azure_client.get_or_create_suite(plan_id, admin_group_title, parent_id)
            levels.append({"title": admin_group_title, "id": created, "status": "created"})
            parent_id = created

    found, elsewhere = await _find_scoped_or_anywhere(azure_client, plan_id, epic_title, parent_id) if parent_id else (None, False)
    if found:
        levels.append({"title": epic_title, "id": found, "status": "found_elsewhere" if elsewhere else "found"})
        parent_id = found
    elif dry_run:
        levels.append({"title": epic_title, "id": None, "status": "would_create"})
        parent_id = None
    else:
        created = await azure_client.get_or_create_suite(plan_id, epic_title, parent_id)
        levels.append({"title": epic_title, "id": created, "status": "created"})
        parent_id = created

    found = await azure_client.find_suite(plan_id, us_suite_name, parent_id) if parent_id else None
    found_status = "found"
    matched_name = None
    if not found and parent_id:
        # Exact-name lookup failed - the Confluence page (whose title set the
        # suite's name at creation time) may have been renamed since. Fall
        # back to matching by the stable "US-<n>"/"AUS-<n>" leading token,
        # but only trust it when exactly one sibling suite carries that
        # number - an ambiguous match would risk silently picking the wrong
        # suite, which is worse than just not finding one.
        number_token = _suite_number_token(us_suite_name)
        if number_token:
            siblings = await azure_client.fetch_child_suites(plan_id, parent_id)
            candidates = [s for s in siblings if _suite_number_token(s.get("name", "")) == number_token]
            if len(candidates) == 1:
                found = str(candidates[0]["id"])
                found_status = "found_renamed"
                matched_name = candidates[0].get("name", "")

    if found:
        level = {"title": us_suite_name, "id": found, "status": found_status}
        if matched_name:
            level["matched_name"] = matched_name
        levels.append(level)
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
    llm_client,
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
    should_cancel_fn: Optional[Callable[[], bool]] = None,
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

    if should_cancel_fn and should_cancel_fn():
        _log("WARNING", "Остановлено пользователем")
        return {"status": "canceled", "error": "Остановлено пользователем"}

    try:
        us_number = normalize_us_number(us)
        prefix_hint = detect_prefix_hint(us)

        test_cases = parse_test_cases_csv_text(csv_text)
        _log("INFO", f"Разобрано {len(test_cases)} тест-кейс(ов) из CSV")

        us_page = await find_us_page_under_folder(
            confluence_client, specs_folder, us_number,
            admin_folder_id=admin_specs_folder_id or None, prefix_hint=prefix_hint, log_fn=_log,
            should_cancel_fn=should_cancel_fn,
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

    _log("INFO", "Определяю бизнес-приоритет (P0/P1/P2) по спецификации…")
    try:
        us_full_page = await confluence_client.get_page(us_page["id"])
        storage_html = (us_full_page or {}).get("body", {}).get("storage", {}).get("value", "")
        spec_text = _text_from_html(storage_html)
        priority_classification = await llm_client.classify_business_priority(
            us_title=us_page["title"], us_text=spec_text,
        )
    except Exception as exc:
        _log("WARNING", f"Не удалось классифицировать бизнес-приоритет, использую P1 по умолчанию: {exc}")
        priority_classification = {"tier": "P1", "reasoning": f"Classification error: {exc}"}

    business_tier = priority_classification.get("tier", "P1")
    business_priority = _TIER_TO_PRIORITY.get(business_tier, "Medium")
    _log(
        "INFO",
        f"Бизнес-приоритет фичи: {business_tier} -> Azure Priority={business_priority}. "
        f"{priority_classification.get('reasoning', '')}",
    )

    _log("INFO", f"Определяю индивидуальный приоритет для {len(test_cases)} тест-кейс(ов) внутри фичи…")
    try:
        tc_classifications = await llm_client.classify_test_case_priorities(
            us_title=us_page["title"], business_tier=business_tier, business_priority=business_priority,
            test_cases=[
                {"id": str(i), "title": tc["title"], "steps_text": _steps_to_text(tc["steps"])}
                for i, tc in enumerate(test_cases)
            ],
        )
    except Exception as exc:
        _log("WARNING", f"Не удалось классифицировать приоритет тест-кейсов индивидуально, использую {business_priority} для всех: {exc}")
        tc_classifications = [
            {"id": str(i), "priority": business_priority, "reasoning": f"Classification error: {exc}"}
            for i in range(len(test_cases))
        ]
    tc_priority_by_id = {c["id"]: c for c in tc_classifications}

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
        marker = {
            "found": "найден", "created": "создан", "would_create": "будет создан",
            "found_renamed": "найден по номеру US (название отличается)",
            "found_elsewhere": "найден не под ожидаемым родителем",
        }[level["status"]]
        _log("INFO", f"Suite '{level['title']}' (id={level['id']}): {marker}")
        if level["status"] == "found_renamed":
            _log(
                "WARNING",
                f"Suite не найден по точному названию '{level['title']}' - использован suite с тем же "
                f"номером US, но текущим названием в Azure DevOps: '{level.get('matched_name')}' (id={level['id']}). "
                f"Похоже, страница в Confluence была переименована после создания suite - стоит переименовать "
                f"suite в Azure DevOps, чтобы название снова совпадало.",
            )
        elif level["status"] == "found_elsewhere":
            _log(
                "WARNING",
                f"Suite '{level['title']}' (id={level['id']}) не найден под ожидаемым родителем - использован "
                f"suite с точно таким же названием, найденный в другом месте плана. Похоже, в Confluence между "
                f"Epic и User Story есть дополнительный уровень вложенности, не учтённый при первом создании "
                f"suite - структура в Azure DevOps сейчас соответствует Confluence, но стоит перепроверить.",
            )

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
            "priority": tc_priority_by_id.get(str(i), {}).get("priority", business_priority),
            "priority_reasoning": tc_priority_by_id.get(str(i), {}).get("reasoning", ""),
            "csv_priority": normalize_priority(tc.get("priority", "")),
            "steps_count": len(tc["steps"]),
            "duplicate": tc["title"].lower() in existing_titles_lower,
        }
        for i, tc in enumerate(test_cases)
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
        "business_priority_tier": business_tier,
        "business_priority": business_priority,
        "business_priority_reasoning": priority_classification.get("reasoning", ""),
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
    canceled = False
    for i, tc in enumerate(test_cases, 1):
        if should_cancel_fn and should_cancel_fn():
            _log("WARNING", f"Остановлено пользователем после {i - 1}/{len(test_cases)} тест-кейсов")
            canceled = True
            break
        if not force and tc["title"].lower() in existing_titles_lower:
            existing_id = existing_titles_lower[tc["title"].lower()]
            _log("INFO", f"[{i}/{len(test_cases)}] Пропущен (уже существует, id={existing_id}): {tc['title']}")
            results.append({"title": tc["title"], "result": {"success": True, "skipped": True, "case_id": existing_id}})
            skipped_count += 1
            continue

        tc_priority = tc_priority_by_id.get(str(i - 1), {}).get("priority", business_priority)
        result = await _create_with_retry(
            azure_client, plan_id, us_suite_id, tc["title"], tc_priority, tc["steps"], state,
        )
        results.append({"title": tc["title"], "priority": tc_priority, "result": result})
        if result.get("success"):
            created_count += 1
            _log("INFO", f"[{i}/{len(test_cases)}] Создан ТК {result['case_id']}: {tc['title']}")
        else:
            _log("ERROR", f"[{i}/{len(test_cases)}] ОШИБКА: {tc['title']} -> {str(result.get('error'))[:200]}")

    failed_count = len(results) - created_count - skipped_count
    _log(
        "INFO",
        f"Загрузка {'остановлена' if canceled else 'завершена'}: "
        f"создано={created_count}, пропущено={skipped_count}, ошибок={failed_count}",
    )

    return {
        "status": "canceled" if canceled else "succeeded",
        "error": "Остановлено пользователем" if canceled else None,
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
