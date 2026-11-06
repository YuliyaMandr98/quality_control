"""Confluence lookups for ac_checklist_sync: find a User Story's spec page by
number, then parse its "Критерии Приемки" (Acceptance Criteria) table.

The page-lookup helpers below are intentionally duplicated from
`packages.workflows.upload_test_cases` rather than imported from it - workflow
packages in this project are self-contained with no cross-imports between
`packages/workflows/*` packages (see e.g. `_text_from_html`, duplicated the
same way in `upload_test_cases`, `review_test_cases` and `triage`).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from bs4 import BeautifulSoup

DEFAULT_SPECS_FOLDER_TITLE = "Фаза 1: спецификации"
DEFAULT_ADMIN_SPECS_FOLDER_ID = "10321934"

_US_NUMBER_PATTERN_TEMPLATE = r"\bA?US-{num}\b"

# Accepts a leading "US"/"AUS" with any typographic dash variant (plain
# hyphen, non-breaking hyphen, en/em dash) before the dotted number, same as
# triage's `_extract_story_ref`.
_STORY_REF_RE = re.compile(r"(?:^|[^\w])(AUS|US)[:\s\-‑–—]*([\d]+(?:\.[\d]+)*)")

# "Критерии Приемки" - this Jira/Confluence org's spec template always uses
# this exact Russian heading for the acceptance-criteria table (verified by
# inspecting a real spec page and the real checklist item links that point at
# `#Критерии-Приемки`). A couple of close spelling variants are accepted too
# in case a spec deviates slightly.
_AC_HEADING_CANDIDATES = ("Критерии Приемки", "Критерии приёмки", "Критерии приемки")

# AC ids in real specs are written inconsistently with either the Latin "AC"
# or Cyrillic "АС" prefix (same spec, same table, mixed - e.g. US-9.2.2 has
# both "AC-01" and "АС-02" in the same column) - match either.
_AC_ID_CELL_RE = re.compile(r"^[AА][CС]-(\d+)$", re.IGNORECASE)


class ChecklistResolutionError(Exception):
    """Raised when the User Story page or its acceptance-criteria table can't
    be unambiguously resolved."""


def extract_story_ref(text: str) -> Optional[tuple[str, str]]:
    """Extract ('US', '9.2.2') / ('AUS', '7.2') from issue summary/title text."""
    match = _STORY_REF_RE.search(text or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def _is_admin_titled(title: str, us_number: str) -> bool:
    return bool(re.search(rf"\bAUS-{re.escape(us_number)}\b", title, re.IGNORECASE))


def _match_us_pages(
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
            raise ChecklistResolutionError(
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
    """Fast path: Confluence's own search index, scoped to `folder_id` - see
    `upload_test_cases._cql_candidates_under_folder` for why this must be the
    primary strategy (a full recursive tree crawl over hundreds of pages can
    take several minutes)."""
    space_filter = f' AND space = "{confluence_client.space}"' if getattr(confluence_client, "space", None) else ""
    token = f"{prefix_hint}-{us_number}" if prefix_hint else us_number

    async def _search(cql: str) -> list[dict[str, Any]]:
        try:
            return await confluence_client.search_pages(cql, limit=100)
        except Exception as exc:
            if log_fn:
                log_fn("DEBUG", f"  CQL search failed ({cql!r}): {exc}")
            return []

    quoted = token.replace('"', '\\"')
    cql1 = f'type=page AND ancestor={folder_id} AND title ~ "\\"{quoted}\\""' + space_filter
    results = await _search(cql1)
    if results:
        return results

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
    matches = _match_us_pages(descendants, us_number, prefix_hint=prefix_hint)

    if not matches:
        _log(f"CQL-поиск не дал точного совпадения под '{label}', сканирую дерево страниц целиком (может занять время)…")
        descendants = await confluence_client.get_all_child_pages_recursive(
            root["id"], log_fn=log_fn, should_cancel_fn=should_cancel_fn
        )
        _log(f"Найдено {len(descendants)} страниц под '{label}' полным обходом.")
        matches = _match_us_pages(descendants, us_number, prefix_hint=prefix_hint)

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
    """Find the Confluence page for US-{us_number} (or AUS-{us_number}),
    falling back to the admin-panel specs folder. Raises
    ChecklistResolutionError if not found."""
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

    raise ChecklistResolutionError(
        f"Страница для US-{us_number}/AUS-{us_number} не найдена ни в '{folder_title}', ни в fallback-папке."
    )


def parse_ac_table(storage_html: str) -> tuple[str, list[int]]:
    """Find the "Критерии Приемки" heading in Confluence storage-format HTML
    and parse its table's ID column. Returns (anchor, sorted_unique_ac_numbers)
    where `anchor` is the Confluence in-page anchor text for that heading
    (heading text with spaces replaced by hyphens - matches the real anchors
    already used in existing checklist items, e.g. "Критерии-Приемки").
    Raises ChecklistResolutionError if the heading or table can't be found, or
    the table has no recognizable AC-NN rows.
    """
    soup = BeautifulSoup(storage_html or "", "html.parser")
    candidates_lower = {c.lower() for c in _AC_HEADING_CANDIDATES}

    heading = None
    heading_text = None
    for tag in soup.find_all(re.compile(r"^h[1-6]$")):
        text = tag.get_text(strip=True)
        if text.lower() in candidates_lower:
            heading = tag
            heading_text = text
            break

    if heading is None:
        raise ChecklistResolutionError(
            f"Заголовок раздела «Критерии Приемки» не найден на странице спецификации "
            f"(ожидался один из вариантов: {', '.join(_AC_HEADING_CANDIDATES)})."
        )

    table = heading.find_next("table")
    if table is None:
        raise ChecklistResolutionError("После заголовка «Критерии Приемки» не найдена таблица с критериями.")

    ac_numbers: set[int] = set()
    for row in table.find_all("tr"):
        cell = row.find(["td", "th"])
        if cell is None:
            continue
        match = _AC_ID_CELL_RE.match(cell.get_text(strip=True))
        if match:
            ac_numbers.add(int(match.group(1)))

    if not ac_numbers:
        raise ChecklistResolutionError("В таблице «Критерии Приемки» не найдено ни одной строки вида AC-NN/АС-NN.")

    anchor = heading_text.replace(" ", "-")
    return anchor, sorted(ac_numbers)
