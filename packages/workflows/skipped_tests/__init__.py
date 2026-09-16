"""Skipped-tests audit workflow.

Scans a Playwright/TS test repository for tests that are disabled (`.skip`) or
flagged with a `todo` / `жду` comment or a bug reference (`MB-1234` / `МВ-1234`),
asks the LLM to work out *why* each one is actually skipped/flagged (comments
near a test don't always describe that test's own reason — see
`_match_test_start`), then cross-checks any referenced bug against Jira so that
tests whose blocking bug has since been resolved surface in their own
category, ready to be un-skipped.

Read-only: never touches the test repo or Jira. Both are only read.
"""

import re
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_TESTS_ROOT = "/Users/oadmin/PROJECTS/FINCA/qa-api-tests/tests"
DEFAULT_FILE_GLOB = "**/*.spec.ts"

_SNIPPET_MAX_CHARS = 3000
_LLM_BATCH_SIZE = 10

_MARKER_RE = re.compile(r"todo|жду|\b(?:MB|МВ)-\d+\b", re.IGNORECASE)
_BUG_KEY_RE = re.compile(r"\b(MB|МВ)-(\d+)\b")
_SKIP_CALL_RE = re.compile(r"\.skip\(")
_TEST_START_RE = re.compile(r"^\s*test(\.skip|\.only)?\(\s*(['\"`])?(.*)$")
_DESCRIBE_START_RE = re.compile(r"^\s*test\.describe(?:\.skip|\.only)?\(\s*(['\"`])(.*?)\1")
_QUOTED_STRING_RE = re.compile(r"^\s*(['\"`])(.*?)\1")
_CONST_START_RE = re.compile(r"^\s*const\s+([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$")
_SKIP_CONST_REF_RE = re.compile(r"test\.skip\(\s*true\s*,\s*([A-Z_][A-Z0-9_]*)\s*\)")

CATEGORY_ORDER = [
    "bug_resolved",
    "bug_open",
    "bug_not_found",
    "waiting_for_answer",
    "todo_backlog",
    "unclear",
]

CATEGORY_LABELS = {
    "bug_resolved": "Баг уже закрыт в Jira — тест пора вернуть",
    "bug_open": "Баг ещё открыт в Jira",
    "bug_not_found": "Упомянутый баг не найден в Jira",
    "waiting_for_answer": "Ожидание ответа/уточнения",
    "todo_backlog": "TODO без привязки к багу",
    "unclear": "Причина skip не ясна",
}


def _normalize_bug_key(raw: str) -> str:
    """Normalize a bug reference to 'MB-1234', mapping the Cyrillic МВ- prefix to MB-."""
    m = _BUG_KEY_RE.search(raw)
    if not m:
        return raw.strip().upper()
    return f"MB-{m.group(2)}"


def _brace_delta(line: str) -> int:
    return line.count("{") - line.count("}")


def _match_test_start(lines: list[str], i: int) -> Optional[tuple[str, str, int]]:
    """Check whether line `i` opens a real test declaration (`test(...)` /
    `test.skip(...)` / `test.only(...)` whose first argument is the test's name
    string), as opposed to a conditional runtime call like `test.skip(true, REASON)`
    used *inside* an already-open test body — which must NOT be treated as a new
    test start, or its enclosing test gets mis-parsed as two separate tests.

    Returns (variant, name, name_line_offset) or None. `name_line_offset` is the
    offset from `i` of the line containing the name string — the caller excludes
    that line when scanning for todo/жду/bug markers, since a test's own title
    often documents which bug it guards against (e.g. '21464: Баг MB-6120: ...')
    without the test being skipped or otherwise flagged.
    """
    match = _TEST_START_RE.match(lines[i])
    if not match:
        return None
    if re.search(r"\.(describe|beforeEach|afterEach|beforeAll|afterAll)\(", lines[i]):
        return None
    variant = "skip" if match.group(1) == ".skip" else ("only" if match.group(1) == ".only" else "plain")
    quote_char, rest = match.group(2), match.group(3)

    if quote_char:
        return variant, rest.split(quote_char)[0], 0

    if rest.strip() != "":
        # First argument isn't a string literal (e.g. `test.skip(true, REASON)`) —
        # this is a conditional skip call inside a test body, not a new test.
        return None

    # Multi-line call form: `test.skip(\n  "name",\n  async () => {`.
    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j < len(lines):
        quoted = _QUOTED_STRING_RE.match(lines[j])
        if quoted:
            return variant, quoted.group(2), j - i
    return None


def _scan_file(path: Path, root: Path) -> list[dict[str, Any]]:
    """Extract skip/todo/bug-flagged test candidates from a single .spec.ts file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    rel_path = str(path.relative_to(root))
    candidates: list[dict[str, Any]] = []
    const_map: dict[str, str] = {}
    describe_stack: list[tuple[str, int]] = []  # (title, depth at which it was opened)
    depth = 0

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # Track const declarations so `test.skip(true, SOME_CONST)` can be resolved
        # to the actual reason text later on.
        const_match = _CONST_START_RE.match(line)
        if const_match:
            name, rest = const_match.group(1), const_match.group(2)
            value_lines = [rest]
            j = i
            while j < n and not value_lines[-1].rstrip().rstrip(";").endswith(('"', "'", "`", ")")) and ";" not in value_lines[-1]:
                j += 1
                if j >= n:
                    break
                value_lines.append(lines[j])
            const_map[name] = " ".join(v.strip() for v in value_lines).rstrip(";").strip()

        describe_match = _DESCRIBE_START_RE.match(line)
        if describe_match:
            describe_stack.append((describe_match.group(2), depth))

        test_start = _match_test_start(lines, i)
        if test_start:
            variant, name, name_line_offset = test_start

            # Leading comment: contiguous non-blank '//' lines directly above this test.
            leading: list[str] = []
            k = i - 1
            while k >= 0 and lines[k].strip().startswith("//"):
                leading.append(lines[k].strip())
                k -= 1
            leading.reverse()

            # Find the block's end by tracking brace depth locally from this line.
            local_depth = 0
            opened = False
            end_i = i
            for j in range(i, n):
                d = _brace_delta(lines[j])
                local_depth += d
                if local_depth > 0:
                    opened = True
                if opened and local_depth <= 0:
                    end_i = j
                    break
            else:
                end_i = n - 1

            block_lines = lines[i : end_i + 1]
            block_text = "\n".join(block_lines)

            # Resolve `test.skip(true, SOME_CONST)` to its actual string value.
            const_note = ""
            for const_ref in _SKIP_CONST_REF_RE.finditer(block_text):
                const_name = const_ref.group(1)
                if const_name in const_map:
                    const_note += f"\n// {const_name} = {const_map[const_name]}"

            full_text = "\n".join(leading) + "\n" + block_text + const_note
            has_skip = variant == "skip" or bool(_SKIP_CALL_RE.search(block_text))

            # The test's own title line is excluded from marker detection: a name
            # like '21464: Баг MB-6120: ...' documents which bug the test guards
            # against without the test itself being skipped/flagged.
            marker_lines = [l for idx, l in enumerate(block_lines) if idx != name_line_offset]
            marker_text = "\n".join(leading) + "\n" + "\n".join(marker_lines) + const_note
            has_marker = bool(_MARKER_RE.search(marker_text))

            if has_skip or has_marker:
                suite_path = " › ".join(t for t, _ in describe_stack)
                snippet = full_text.strip()
                truncated = len(snippet) > _SNIPPET_MAX_CHARS
                if truncated:
                    snippet = snippet[:_SNIPPET_MAX_CHARS] + "\n… (обрезано)"
                candidates.append(
                    {
                        "file": rel_path,
                        "line": i + 1,
                        "test_name": name or "(без названия)",
                        "suite_path": suite_path,
                        "is_skip_call": has_skip,
                        "truncated": truncated,
                        "snippet": snippet,
                    }
                )

            # Advance past the whole block, feeding its brace deltas into the
            # file-level depth counter so describe-stack popping stays correct.
            for j in range(i, end_i + 1):
                depth += _brace_delta(lines[j])
            i = end_i + 1

            while describe_stack and depth <= describe_stack[-1][1]:
                describe_stack.pop()
            continue

        depth += _brace_delta(line)
        while describe_stack and depth <= describe_stack[-1][1] and i > 0:
            # Only pop when we've moved past the describe's own opening line.
            describe_stack.pop()
        i += 1

    return candidates


def _find_candidates(tests_root: str, file_glob: str = DEFAULT_FILE_GLOB) -> tuple[list[dict[str, Any]], int]:
    root = Path(tests_root)
    files = sorted(root.glob(file_glob))
    all_candidates: list[dict[str, Any]] = []
    for path in files:
        all_candidates.extend(_scan_file(path, root))
    for idx, cand in enumerate(all_candidates):
        cand["id"] = idx
    return all_candidates, len(files)


def _chunk(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def _classify_candidates(
    llm_client,
    candidates: list[dict[str, Any]],
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[int, dict[str, Any]]:
    """Run LLM classification in batches, returning {candidate_id: classification}.

    Candidates whose batch never ran (because `should_cancel_fn` fired) are simply
    absent from the returned dict - the caller already treats a missing id as
    "unclear" via `.get(id, {})`, so no explicit fallback-fill is needed here.
    """
    results: dict[int, dict[str, Any]] = {}
    batches = _chunk(candidates, _LLM_BATCH_SIZE)
    for batch_num, batch in enumerate(batches, 1):
        if should_cancel_fn and should_cancel_fn():
            if log_fn:
                log_fn("WARNING", f"Остановлено пользователем после {batch_num - 1}/{len(batches)} батчей классификации")
            break
        if log_fn:
            log_fn("INFO", f"Классификация батча {batch_num}/{len(batches)} ({len(batch)} тестов)")
        try:
            batch_payload = [
                {"id": c["id"], "file": c["file"], "test_name": c["test_name"], "snippet": c["snippet"]}
                for c in batch
            ]
            classified = await llm_client.classify_skipped_tests(batch_payload)
            for item in classified or []:
                cid = item.get("id")
                if isinstance(cid, int):
                    results[cid] = item
        except Exception as exc:
            if log_fn:
                log_fn("WARNING", f"  Батч {batch_num} не классифицирован: {exc}")
        for c in batch:
            if c["id"] not in results:
                results[c["id"]] = {
                    "is_skipped": c["is_skip_call"],
                    "category": "unclear",
                    "bug_keys": [],
                    "reason_summary": "Не удалось классифицировать (ошибка LLM)",
                }
    return results


async def run_skipped_tests_audit_workflow(
    jira_client,
    llm_client,
    *,
    tests_root: str = DEFAULT_TESTS_ROOT,
    file_glob: str = DEFAULT_FILE_GLOB,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Scan `tests_root` for skip/todo/bug-flagged tests, classify each with the LLM,
    cross-check referenced bugs against Jira, and return a categorized report.
    """

    def _log(level: str, message: str) -> None:
        if log_fn:
            log_fn(level, message)

    root = Path(tests_root)
    if not root.is_dir():
        return {
            "status": "failed",
            "error": f"tests_root not found or not a directory: {tests_root}",
        }

    # ── 1. Static scan ───────────────────────────────────────────────────────────
    candidates, files_scanned = _find_candidates(tests_root, file_glob)
    _log("INFO", f"Найдено файлов: {files_scanned}, кандидатов на skip/todo/баг: {len(candidates)}")

    if not candidates:
        return {
            "status": "succeeded",
            "summary": {"files_scanned": files_scanned, "total_candidates": 0},
            "categories": {key: [] for key in CATEGORY_ORDER},
            "category_labels": CATEGORY_LABELS,
        }

    if should_cancel_fn and should_cancel_fn():
        _log("WARNING", "Остановлено пользователем")
        return {"status": "canceled", "error": "Остановлено пользователем"}

    # ── 2. LLM classification ────────────────────────────────────────────────────
    classifications = await _classify_candidates(llm_client, candidates, log_fn=_log, should_cancel_fn=should_cancel_fn)
    canceled = bool(should_cancel_fn and should_cancel_fn())

    # ── 3. Collect bug keys and check them against Jira ─────────────────────────
    all_bug_keys: set[str] = set()
    for cand in candidates:
        cls = classifications.get(cand["id"], {})
        normalized = [_normalize_bug_key(k) for k in cls.get("bug_keys", []) if k]
        cls["bug_keys"] = normalized
        all_bug_keys.update(normalized)

    _log("INFO", f"Уникальных упомянутых багов: {len(all_bug_keys)}")
    jira_issues = await jira_client.fetch_issues_by_keys(sorted(all_bug_keys)) if all_bug_keys else []
    jira_by_key: dict[str, dict[str, Any]] = {}
    for issue in jira_issues:
        fields = issue.get("fields") or {}
        status = fields.get("status") or {}
        resolution = fields.get("resolution") or {}
        jira_by_key[str(issue.get("key") or "")] = {
            "key": issue.get("key"),
            "summary": fields.get("summary"),
            "status": status.get("name"),
            "status_category": (status.get("statusCategory") or {}).get("key"),
            "resolution": resolution.get("name") if resolution else None,
        }

    # ── 4. Build final categorized report ────────────────────────────────────────
    categories: dict[str, list[dict[str, Any]]] = {key: [] for key in CATEGORY_ORDER}
    for cand in candidates:
        cls = classifications.get(cand["id"], {})
        bug_keys = cls.get("bug_keys", [])
        bug_infos = [jira_by_key.get(k) for k in bug_keys]
        found_infos = [b for b in bug_infos if b]

        if any(b.get("status_category") == "done" for b in found_infos):
            category = "bug_resolved"
        elif found_infos:
            category = "bug_open"
        elif bug_keys:
            category = "bug_not_found"
        elif cls.get("category") == "waiting_for_answer":
            category = "waiting_for_answer"
        elif cls.get("category") == "todo_backlog":
            category = "todo_backlog"
        else:
            category = "unclear"

        row = {
            "file": cand["file"],
            "line": cand["line"],
            "test_name": cand["test_name"],
            "suite_path": cand["suite_path"],
            "is_skipped": bool(cls.get("is_skipped", cand["is_skip_call"])),
            "bug_keys": bug_keys,
            "jira": [b for b in bug_infos if b] + [{"key": k, "not_found": True} for k, b in zip(bug_keys, bug_infos) if not b],
            "reason_summary": cls.get("reason_summary", ""),
        }
        categories[category].append(row)

    summary = {
        "files_scanned": files_scanned,
        "total_candidates": len(candidates),
        **{f"count_{key}": len(rows) for key, rows in categories.items()},
    }
    _log(
        "INFO",
        ("Остановлено пользователем. " if canceled else "Готово: ")
        + ", ".join(f"{CATEGORY_LABELS[k]}={len(v)}" for k, v in categories.items()),
    )

    return {
        "status": "canceled" if canceled else "succeeded",
        "error": "Остановлено пользователем" if canceled else None,
        "summary": summary,
        "categories": categories,
        "category_labels": CATEGORY_LABELS,
    }


def render_markdown_report(result: dict[str, Any]) -> str:
    """Render the categorized result as a human-readable Markdown report."""
    lines = ["# Отчёт по skip/todo тестам", ""]
    summary = result.get("summary", {})
    lines.append(f"Файлов просканировано: {summary.get('files_scanned', 0)}")
    lines.append(f"Кандидатов найдено: {summary.get('total_candidates', 0)}")
    lines.append("")

    categories = result.get("categories", {})
    labels = result.get("category_labels", CATEGORY_LABELS)
    for key in CATEGORY_ORDER:
        rows = categories.get(key, [])
        lines.append(f"## {labels.get(key, key)} ({len(rows)})")
        lines.append("")
        if not rows:
            lines.append("_Нет тестов в этой категории._")
            lines.append("")
            continue
        for row in rows:
            bug_str = ""
            if row.get("bug_keys"):
                parts = []
                for j in row.get("jira", []):
                    if j.get("not_found"):
                        parts.append(f"{j['key']} (не найден в Jira)")
                    else:
                        parts.append(f"{j['key']} [{j.get('status')}" + (f" / {j.get('resolution')}" if j.get("resolution") else "") + "]")
                bug_str = " — " + ", ".join(parts)
            suite = f"{row['suite_path']} › " if row.get("suite_path") else ""
            lines.append(f"- **{row['file']}:{row['line']}** — {suite}{row['test_name']}{bug_str}")
            if row.get("reason_summary"):
                lines.append(f"  {row['reason_summary']}")
        lines.append("")

    return "\n".join(lines)


__all__ = [
    "DEFAULT_TESTS_ROOT",
    "DEFAULT_FILE_GLOB",
    "CATEGORY_ORDER",
    "CATEGORY_LABELS",
    "run_skipped_tests_audit_workflow",
    "render_markdown_report",
]
