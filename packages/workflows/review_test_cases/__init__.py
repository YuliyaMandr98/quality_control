"""Test case coverage review workflow.

Reviews test cases pasted by the user for a single User Story against its
Confluence specification (and, for API test cases, the technical
implementation doc) for completeness of coverage: missing requirements,
missing validations, missing alternative scenarios and missing edge cases.

Unlike upload_test_cases / triage, this workflow does not search Confluence
by User Story number — the caller always pastes the exact spec (and, for
API, technical-implementation) link or page ID directly, since those pages
aren't reliably discoverable by number alone for every test type.

Read-only: only fetches Confluence pages. The review verdict is returned for
display / artifact storage and never written back anywhere.
"""

import re
from typing import Any, Callable, Optional

from bs4 import BeautifulSoup

from packages.common import get_logger

logger = get_logger(__name__)

TEST_TYPES = ("web", "mobile", "api")

CATEGORY_ORDER = [
    "missing_requirements",
    "missing_validations",
    "missing_alternative_flows",
    "missing_edge_cases",
    "ambiguities",
    "well_covered",
]

CATEGORY_LABELS = {
    "missing_requirements": "Непокрытые требования / критерии приёмки",
    "missing_validations": "Недостающие проверки валидации",
    "missing_alternative_flows": "Недостающие альтернативные сценарии",
    "missing_edge_cases": "Недостающие edge-кейсы",
    "ambiguities": "Неоднозначности и риски",
    "well_covered": "Что уже хорошо покрыто",
}

# Must match (or be <=) the LLM client's own per-field prompt truncation cap
# (see _MAX_REVIEW_FIELD_CHARS in packages/integrations/anthropic) - used here
# only to surface a visible warning in the run log when a field is about to be
# silently cut, instead of the review just reporting real coverage as missing.
_MAX_FIELD_CHARS_WARNING = 100_000

_PAGE_ID_PATTERNS = [
    re.compile(r"/pages/(\d+)"),
    re.compile(r"[?&]pageId=(\d+)"),
]


class ReviewTestCasesError(Exception):
    """Raised when a pasted Confluence link/ID can't be resolved to page content."""


def extract_confluence_page_id(url_or_id: str) -> str:
    """Extract a numeric Confluence page ID from a pasted link, or pass a bare ID through.

    Accepts a bare numeric ID, a modern Cloud link (`.../pages/123456789/Title`),
    or a legacy link (`.../pages/viewpage.action?pageId=123456789`).
    """
    value = (url_or_id or "").strip()
    if not value:
        raise ReviewTestCasesError("Confluence-ссылка не указана")
    if value.isdigit():
        return value
    for pattern in _PAGE_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    raise ReviewTestCasesError(
        f"Не удалось извлечь ID страницы Confluence из '{value}'. "
        f"Ожидается ссылка вида .../pages/123456789/... или числовой ID страницы."
    )


def _text_from_html(storage_html: str) -> str:
    """Convert Confluence storage-format HTML to plain text."""
    soup = BeautifulSoup(storage_html or "", "html.parser")
    return soup.get_text("\n", strip=True)


async def _fetch_confluence_text(
    confluence_client, url_or_id: str, label: str, log_fn: Optional[Callable] = None
) -> tuple[str, str, str]:
    """Resolve a pasted Confluence link/ID to (page_id, title, plain_text)."""
    page_id = extract_confluence_page_id(url_or_id)
    if log_fn:
        log_fn("INFO", f"Загружаю «{label}» из Confluence (page id={page_id})…")
    page = await confluence_client.get_page(page_id)
    if not page:
        raise ReviewTestCasesError(f"Страница Confluence id={page_id} ({label}) не найдена")
    title = str(page.get("title") or "")
    storage_html = (page.get("body") or {}).get("storage", {}).get("value", "")
    text = _text_from_html(storage_html)
    if not text.strip():
        raise ReviewTestCasesError(f"Страница Confluence «{title}» (id={page_id}, {label}) пустая")
    return page_id, title, text


async def run_review_test_cases_workflow(
    confluence_client,
    llm_client,
    *,
    us: str,
    test_type: str,
    spec_url: str,
    test_cases_text: str,
    tech_impl_url: Optional[str] = None,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Review pasted test cases for `us` against their Confluence spec (and, for
    API cases, the pasted technical-implementation doc) for coverage completeness.
    """

    def _log(level: str, msg: str) -> None:
        logger.info(msg) if level == "INFO" else logger.warning(msg) if level == "WARNING" else logger.debug(msg)
        if log_fn:
            log_fn(level, msg)

    test_type = (test_type or "").strip().lower()
    if test_type not in TEST_TYPES:
        return {"status": "failed", "error": f"Неизвестный тип тест-кейсов: {test_type!r}. Ожидается одно из {TEST_TYPES}."}

    if not (test_cases_text or "").strip():
        return {"status": "failed", "error": "Тест-кейсы не переданы (пустое поле)."}

    if test_type == "api" and not (tech_impl_url or "").strip():
        return {"status": "failed", "error": "Для API тест-кейсов нужна ссылка на техническую реализацию."}

    _log("INFO", f"Ревью тест-кейсов начато: US={us}, тип={test_type}")

    if should_cancel_fn and should_cancel_fn():
        _log("WARNING", "Остановлено пользователем")
        return {"status": "canceled", "error": "Остановлено пользователем"}

    try:
        spec_page_id, spec_title, spec_text = await _fetch_confluence_text(
            confluence_client, spec_url, "спецификация", log_fn=_log
        )
    except ReviewTestCasesError as exc:
        _log("ERROR", str(exc))
        return {"status": "failed", "error": str(exc)}

    if should_cancel_fn and should_cancel_fn():
        _log("WARNING", "Остановлено пользователем")
        return {"status": "canceled", "error": "Остановлено пользователем"}

    tech_impl_page_id: Optional[str] = None
    tech_impl_title = ""
    tech_impl_text = ""
    if test_type == "api" and tech_impl_url:
        try:
            tech_impl_page_id, tech_impl_title, tech_impl_text = await _fetch_confluence_text(
                confluence_client, tech_impl_url, "техническая реализация", log_fn=_log
            )
        except ReviewTestCasesError as exc:
            _log("ERROR", str(exc))
            return {"status": "failed", "error": str(exc)}

    _log(
        "INFO",
        f"Спецификация: «{spec_title}» (id={spec_page_id})"
        + (f", техническая реализация: «{tech_impl_title}» (id={tech_impl_page_id})" if tech_impl_page_id else ""),
    )

    for label, text in (
        ("тест-кейсы", test_cases_text),
        ("спецификация", spec_text),
        ("техническая реализация", tech_impl_text),
    ):
        if len(text) > _MAX_FIELD_CHARS_WARNING:
            _log(
                "WARNING",
                f"«{label}» ({len(text)} симв.) превышает {_MAX_FIELD_CHARS_WARNING} симв. — "
                f"хвост будет обрезан перед отправкой в LLM, часть содержимого может быть "
                f"не учтена в ревью.",
            )

    if should_cancel_fn and should_cancel_fn():
        _log("WARNING", "Остановлено пользователем")
        return {"status": "canceled", "error": "Остановлено пользователем"}

    _log("INFO", "Отправляю на анализ в Claude…")

    try:
        review = await llm_client.review_test_case_coverage(
            test_type=test_type,
            us=us,
            spec_text=spec_text,
            tech_impl_text=tech_impl_text,
            test_cases_text=test_cases_text,
        )
    except Exception as exc:
        _log("ERROR", f"Claude review failed: {exc}")
        return {"status": "failed", "error": str(exc)}

    _log("INFO", "Ревью завершено.")

    return {
        "status": "succeeded",
        "us": us,
        "test_type": test_type,
        "spec_page": {"id": spec_page_id, "title": spec_title},
        "tech_impl_page": {"id": tech_impl_page_id, "title": tech_impl_title} if tech_impl_page_id else None,
        "review": review,
        "category_order": CATEGORY_ORDER,
        "category_labels": CATEGORY_LABELS,
        "correlation_id": correlation_id,
    }


def render_text_report(result: dict[str, Any]) -> str:
    """Render the review result as a human-readable text report, split into labeled blocks."""
    if result.get("status") != "succeeded":
        return f"Ревью не выполнено: {result.get('error', 'неизвестная ошибка')}"

    lines: list[str] = []
    lines.append(f"РЕВЬЮ ТЕСТ-КЕЙСОВ — US {result.get('us', '')} ({result.get('test_type', '')})")
    spec_page = result.get("spec_page") or {}
    lines.append(f"Спецификация: {spec_page.get('title', '')} (id={spec_page.get('id', '')})")
    tech_impl_page = result.get("tech_impl_page")
    if tech_impl_page:
        lines.append(f"Техническая реализация: {tech_impl_page.get('title', '')} (id={tech_impl_page.get('id', '')})")
    lines.append("")

    review = result.get("review") or {}
    overall = review.get("overall_assessment")
    if overall:
        lines.append("ИТОГОВАЯ ОЦЕНКА")
        lines.append(overall)
        lines.append("")

    for key in CATEGORY_ORDER:
        items = review.get(key) or []
        label = CATEGORY_LABELS.get(key, key)
        lines.append(f"{label.upper()} ({len(items)})")
        if not items:
            lines.append("  Пунктов нет.")
        elif key == "well_covered":
            for item in items:
                lines.append(f"  - {item}")
        else:
            for item in items:
                title = str(item.get("title") or "").strip()
                description = str(item.get("description") or "").strip()
                if title and description:
                    lines.append(f"  - {title}: {description}")
                else:
                    lines.append(f"  - {title or description}")
        lines.append("")

    return "\n".join(lines)


__all__ = [
    "TEST_TYPES",
    "CATEGORY_ORDER",
    "CATEGORY_LABELS",
    "ReviewTestCasesError",
    "extract_confluence_page_id",
    "run_review_test_cases_workflow",
    "render_text_report",
]
