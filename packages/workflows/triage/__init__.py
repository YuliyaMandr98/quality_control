"""Bug triage workflow: validate Jira bug tickets against user story specs via Claude."""

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from bs4 import BeautifulSoup

from packages.common import get_logger

logger = get_logger(__name__)

# Default JQL that selects untriaged bugs (can be overridden via parameters)
DEFAULT_BUG_JQL = 'issuetype in ("BE BUG", "Mobile bug", Bug, "FE bug") AND status = Backlog'

# Default JIRA custom field IDs (discoverable via /rest/api/3/issue/createmeta)
DEFAULT_SEVERITY_FIELD_ID = "customfield_10865"
DEFAULT_IMPACT_FIELD_ID = "customfield_10004"

# Status to transition confirmed bugs into
DEFAULT_TARGET_STATUS = "Triage"

# Admin-panel specs (AUS-<n>) sometimes live under a dedicated Confluence
# folder rather than being reachable via the regular space-wide title search.
DEFAULT_ADMIN_SPECS_FOLDER_ID = "10321934"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_story_ref(issue_title: str) -> Optional[tuple[str, str]]:
    """Extract a user-story reference like ('US', '11.1.2') or ('AUS', '7.2') from an issue title.

    Matches 'US-X.Y.Z', 'US: X.Y.Z', 'US X.Y.Z' (regular business/mobile specs)
    and 'AUS-X.Y.Z' (admin-panel specs). AUS and US are independent numbering
    spaces that can collide on the same number (e.g. both 'US-20.1.1' and
    'AUS-20.1.1' exist), so the prefix is captured and matched exactly rather
    than being normalized away.
    The separator also accepts typographic dash variants (non-breaking hyphen,
    en dash, em dash) that sometimes end up in titles via copy-paste, in
    addition to the plain ASCII hyphen.
    Returns (prefix, dotted_number) or None if no reference is found.
    """
    match = re.search(r"(?:^|[^\w])(AUS|US)[:\s\-‑–—]*([\d]+(?:\.[\d]+)*)", issue_title)
    if not match:
        return None
    return match.group(1), match.group(2)


def _text_from_html(storage_html: str) -> str:
    """Convert Confluence storage-format HTML to plain text."""
    soup = BeautifulSoup(storage_html or "", "html.parser")
    return soup.get_text("\n", strip=True)


async def _search_admin_specs_folder(
    confluence_client,
    pattern: re.Pattern,
    log_fn: Optional[Callable] = None,
) -> list[dict]:
    """Fallback lookup for AUS pages living under the dedicated admin-panel specs folder."""
    try:
        root = await confluence_client.get_page(DEFAULT_ADMIN_SPECS_FOLDER_ID)
    except Exception as exc:
        if log_fn:
            log_fn("WARNING", f"Failed to fetch admin specs folder id={DEFAULT_ADMIN_SPECS_FOLDER_ID}: {exc}")
        return []
    if not root:
        if log_fn:
            log_fn("DEBUG", f"Admin specs folder id={DEFAULT_ADMIN_SPECS_FOLDER_ID} not found")
        return []
    descendants = await confluence_client.get_all_child_pages_recursive(root["id"])
    return [p for p in descendants if pattern.search(str(p.get("title") or ""))]


async def _find_story_confluence_page(
    confluence_client,
    prefix: str,
    number: str,
    log_fn: Optional[Callable] = None,
) -> Optional[str]:
    """Search Confluence for a page whose title contains '{prefix}-{number}' exactly.

    `prefix` is 'US' (regular business/mobile specs) or 'AUS' (admin-panel specs) —
    it must be matched exactly since AUS/US numbers are independent and can collide.
    Returns the page ID if found, else None.

    Strategy (most to least precise):
    1. Quoted-phrase CQL: title ~ '"{prefix}-{number}"' — avoids Lucene tokenisation
       splitting dots/hyphens, so '11.1.2' is matched as a single phrase.
    2. Broad CQL on the major part (e.g. 'US-11') with Python-side regex filtering —
       handles cases where the Confluence analyser still can't match the dotted phrase.
    3. For AUS only: same title match, scoped to the dedicated admin-panel specs
       folder — AUS pages aren't always reachable via the space-wide search above.
    """
    token = f"{prefix}-{number}"
    pattern = re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)
    space_filter = (
        f' AND space = "{confluence_client.space}"' if getattr(confluence_client, "space", None) else ""
    )

    async def _search_and_collect(cql: str) -> list[dict]:
        """Run CQL and return all pages whose title matches the pattern."""
        if log_fn:
            log_fn("DEBUG", f"Searching Confluence for {token}: CQL={cql}")
        try:
            pages = await confluence_client.search_pages(cql, limit=50)
        except Exception as exc:
            if log_fn:
                log_fn("WARNING", f"Confluence search failed ({cql!r}): {exc}")
            return []
        return [p for p in pages if pattern.search(str(p.get("title") or ""))]

    def _best_match(pages: list[dict]) -> Optional[str]:
        """From multiple matching pages prefer the main spec (not API sub-pages)."""
        if not pages:
            return None
        # Prefer pages that don't have ' API' immediately after the story number
        api_pat = re.compile(rf"{re.escape(token)}\s+API", re.IGNORECASE)
        preferred = [p for p in pages if not api_pat.search(str(p.get("title") or ""))]
        candidates = preferred if preferred else pages
        # Among candidates pick shortest title — most likely the canonical spec page
        best = min(candidates, key=lambda p: len(str(p.get("title") or "")))
        page_id = str(best.get("id") or "")
        if log_fn:
            log_fn("DEBUG", f"Selected Confluence page id={page_id} title={best.get('title')!r} (from {len(pages)} match(es))")
        return page_id or None

    # Strategy 1: quoted-phrase CQL (exact phrase, immune to tokenisation)
    quoted_token = token.replace('"', '\\"')
    cql1 = f'type=page AND title ~ "\\"{quoted_token}\\""' + space_filter
    matches = await _search_and_collect(cql1)
    result = _best_match(matches)
    if result:
        return result

    # Strategy 2: broad search on the major segment + Python-side exact filter
    major = number.split(".")[0]
    cql2 = f'type=page AND title ~ "{prefix}-{major}"' + space_filter
    matches = await _search_and_collect(cql2)
    result = _best_match(matches)
    if result:
        return result

    # Strategy 3: AUS pages sometimes sit under the dedicated admin specs folder
    if prefix == "AUS":
        folder_matches = await _search_admin_specs_folder(confluence_client, pattern, log_fn=log_fn)
        result = _best_match(folder_matches)
        if result:
            return result

    if log_fn:
        log_fn("WARNING", f"No Confluence page found for {token}")
    return None


def _extract_description_text(issue: dict[str, Any]) -> str:
    """Extract plain-text description from a Jira issue (ADF or plain string)."""
    description = (issue.get("fields") or {}).get("description") or ""
    if isinstance(description, dict):
        # Atlassian Document Format — walk the content tree
        parts: list[str] = []

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "text":
                    parts.append(str(node.get("text") or ""))
                for child in node.get("content") or []:
                    _walk(child)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(description)
        return " ".join(parts).strip()
    return str(description).strip()


async def run_triage_bugs_workflow(
    jira_client,
    confluence_client,
    llm_client,
    *,
    jql: str = DEFAULT_BUG_JQL,
    max_results: int = 50,
    apply: bool = False,
    add_comment: bool = False,
    severity_field_id: str = DEFAULT_SEVERITY_FIELD_ID,
    impact_field_id: str = DEFAULT_IMPACT_FIELD_ID,
    target_status: str = DEFAULT_TARGET_STATUS,
    batch_delay_seconds: int = 10,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Triage Jira bug tickets using Confluence user-story specs and Claude LLM assessment.

    For each bug matched by `jql`:
      1. Extract the US number from the issue summary.
      2. Fetch the corresponding user-story page from Confluence.
      3. Send the spec + bug details to Claude for severity/impact classification.
      4. If `apply=True` and the LLM confirms it is a real bug:
         - Update the Jira issue with severity and impact custom fields.
         - Transition the issue to `target_status`.
         - Optionally add a triage comment when `add_comment=True`.

    Returns a structured summary dict suitable for artifact persistence.
    """

    def _log(level: str, msg: str) -> None:
        logger.info(msg) if level == "INFO" else logger.warning(msg) if level == "WARNING" else logger.debug(msg)
        if log_fn:
            log_fn(level, msg)

    _log(
        "INFO",
        f"Triage workflow started: jql={jql!r}, max_results={max_results}, apply={apply}, add_comment={add_comment}",
    )

    # ── 1. Fetch bug issues from Jira ──────────────────────────────────────────
    try:
        issues = await jira_client.fetch_bugs(jql, max_results=max_results)
    except Exception as exc:
        _log("ERROR", f"Failed to fetch bugs from Jira: {exc}")
        return {
            "status": "failed",
            "error": str(exc),
            "bugs_fetched": 0,
            "bugs_triaged": 0,
            "per_issue_results": [],
            "timestamp": _utc_now_iso(),
        }

    _log("INFO", f"Fetched {len(issues)} bug issues from Jira")

    per_issue_results: list[dict[str, Any]] = []
    triaged_count = 0
    skipped_no_us = 0
    skipped_no_confluence = 0
    skipped_not_real = 0
    error_count = 0

    # ── 2. Process each issue ──────────────────────────────────────────────────
    canceled = False
    for idx, issue in enumerate(issues, 1):
        if should_cancel_fn and should_cancel_fn():
            _log("WARNING", f"Остановлено пользователем после {idx - 1}/{len(issues)} тикетов")
            canceled = True
            break
        issue_key = str(issue.get("key") or "")
        fields = issue.get("fields") or {}
        summary = str(fields.get("summary") or "")
        status_name = str((fields.get("status") or {}).get("name") or "")

        _log("INFO", f"[{idx}/{len(issues)}] Processing {issue_key}: {summary[:70]}")

        result: dict[str, Any] = {
            "key": issue_key,
            "summary": summary,
            "status_before": status_name,
            "outcome": "skipped",
            "reason": "",
            "severity": None,
            "impact": None,
            "priority": None,
            "is_real_bug": None,
            "reasoning": "",
            "updated": False,
        }

        # 2a. Extract US/AUS story reference
        story_ref = _extract_story_ref(summary)
        if not story_ref:
            _log("WARNING", f"  No US/AUS number found in title, skipping")
            result["reason"] = "no_us_number_in_title"
            per_issue_results.append(result)
            skipped_no_us += 1
            continue
        story_prefix, story_number = story_ref
        story_token = f"{story_prefix}-{story_number}"

        # 2b. Find Confluence page for this US/AUS
        page_id = await _find_story_confluence_page(confluence_client, story_prefix, story_number, log_fn=_log)
        if not page_id:
            _log("WARNING", f"  Confluence page not found for {story_token}, skipping")
            result["reason"] = f"confluence_page_not_found_for_{story_token}"
            per_issue_results.append(result)
            skipped_no_confluence += 1
            continue

        # 2c. Fetch page content
        try:
            raw_html = await confluence_client.fetch_page_content(page_id)
            us_text = _text_from_html(raw_html)
        except Exception as exc:
            _log("WARNING", f"  Failed to fetch Confluence page {page_id}: {exc}")
            result["reason"] = f"confluence_fetch_error: {exc}"
            result["outcome"] = "error"
            per_issue_results.append(result)
            error_count += 1
            continue

        if not us_text.strip():
            _log("WARNING", f"  Confluence page {page_id} returned empty content, skipping")
            result["reason"] = "confluence_page_empty"
            per_issue_results.append(result)
            skipped_no_confluence += 1
            continue

        # 2d. Rate-limit delay before Claude call (skip for the first issue)
        if idx > 1 and batch_delay_seconds > 0:
            _log("DEBUG", f"  Waiting {batch_delay_seconds}s before Claude call")
            await asyncio.sleep(batch_delay_seconds)

        # 2e. Claude assessment
        bug_description = _extract_description_text(issue)
        _log("INFO", f"  Assessing bug with Claude ({story_token}, page_id={page_id})")
        try:
            assessment = await llm_client.assess_bug(
                us_content=us_text,
                bug_title=summary,
                bug_description=bug_description,
            )
        except Exception as exc:
            _log("WARNING", f"  Claude assessment failed: {exc}")
            result["reason"] = f"llm_error: {exc}"
            result["outcome"] = "error"
            per_issue_results.append(result)
            error_count += 1
            continue

        result["is_real_bug"] = assessment.get("is_real_bug")
        result["severity"] = assessment.get("severity")
        result["impact"] = assessment.get("impact")
        result["priority"] = assessment.get("priority")
        result["reasoning"] = assessment.get("reasoning", "")

        if not assessment.get("is_real_bug"):
            _log("INFO", f"  Not a real bug — no Jira changes. Reason: {assessment.get('reasoning')}")
            result["outcome"] = "not_real_bug"
            result["reason"] = assessment.get("reasoning", "")
            per_issue_results.append(result)
            skipped_not_real += 1
            continue

        severity = str(assessment.get("severity") or "Major")
        impact = str(assessment.get("impact") or "Moderate / Limited")
        priority = str(assessment.get("priority") or "Medium")
        _log("INFO", f"  Real bug confirmed — severity={severity}, impact={impact}, priority={priority}")
        _log("INFO", f"  Reasoning: {assessment.get('reasoning')}")

        # 2f. Apply updates (or dry-run)
        if not apply:
            _log("INFO", f"  [DRY-RUN] Would update {issue_key}: severity={severity}, impact={impact}, priority={priority}, status={target_status}")
            result["outcome"] = "dry_run"
            result["updated"] = False
            per_issue_results.append(result)
            triaged_count += 1
            continue

        update_ok = False
        try:
            update_resp = await jira_client.update_issue(
                issue_key,
                {
                    "fields": {
                        severity_field_id: {"value": severity},
                        impact_field_id: {"value": impact},
                        "priority": {"name": priority},
                    }
                },
            )
            update_ok = bool(update_resp.get("success"))
            if not update_ok:
                _log("WARNING", f"  Field update failed: {update_resp.get('error')}")
        except Exception as exc:
            _log("WARNING", f"  Field update raised: {exc}")

        transition_ok = False
        try:
            transition_ok = await jira_client.transition_issue(issue_key, target_status)
            if not transition_ok:
                _log("WARNING", f"  Transition to '{target_status}' failed or not available")
        except Exception as exc:
            _log("WARNING", f"  Transition raised: {exc}")

        if add_comment:
            comment_text = (
                f"Triaged by automated system:\n"
                f"- Severity: {severity}\n"
                f"- Impact: {impact}\n"
                f"- Priority: {priority}\n"
                f"- Reason: {assessment.get('reasoning', '')}"
            )
            try:
                await jira_client.add_comment(issue_key, comment_text)
            except Exception as exc:
                _log("WARNING", f"  Comment failed: {exc}")

        result["updated"] = update_ok or transition_ok
        result["outcome"] = "triaged" if result["updated"] else "partial_update"
        per_issue_results.append(result)
        triaged_count += 1

    # ── 3. Build summary ───────────────────────────────────────────────────────
    summary_data = {
        "bugs_fetched": len(issues),
        "bugs_triaged": triaged_count,
        "skipped_no_us_number": skipped_no_us,
        "skipped_no_confluence_page": skipped_no_confluence,
        "skipped_not_real_bug": skipped_not_real,
        "errors": error_count,
        "apply": apply,
        "add_comment": add_comment,
        "jql": jql,
        "target_status": target_status,
    }

    _log(
        "INFO",
        f"Triage {'canceled' if canceled else 'complete'}: triaged={triaged_count}/{len(issues)}, "
        f"not_real={skipped_not_real}, no_us={skipped_no_us}, "
        f"no_confluence={skipped_no_confluence}, errors={error_count}",
    )

    return {
        "status": "canceled" if canceled else "succeeded",
        "error": "Остановлено пользователем" if canceled else None,
        "summary": summary_data,
        "per_issue_results": per_issue_results,
        "timestamp": _utc_now_iso(),
        "correlation_id": correlation_id,
    }


__all__ = ["run_triage_bugs_workflow"]
