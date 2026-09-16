"""Pull Request review workflows: Claude-based code review and comment-fix verification.

Adapted from trace2quality's scripts/review_pull_request.py and
scripts/review_comment_fixes.py for in-app execution. Unlike the standalone
scripts (which cache Claude's non-deterministic output per PR iteration in a
JSON file so a dry-run followed by --apply reuses the same findings), each
workflow run here always does a single dry-run analysis and persists its
result as a run artifact; a separate "apply" API call reads that same
artifact to post comments/replies, so there is never a second Claude call
for the same run — no file-based cache needed.
"""

import difflib
import hashlib
import textwrap
from typing import Any, Callable, Optional

import httpx

from packages.common import get_logger

logger = get_logger(__name__)

_MAX_FILE_CHARS = 20_000  # skip a single file whose "after" content exceeds this
_MAX_TOTAL_DIFF_CHARS = 60_000  # cap the combined diff text sent to Claude
_CONTEXT_LINES = 3  # unchanged lines shown around each change, for windowed files
_WINDOW_THRESHOLD_LINES = 200  # files longer than this get windowed (elided) context

_THREAD_CONTEXT_LINES = 15  # lines of code shown above/below a commented line
_MAX_TOTAL_PROMPT_CHARS = 60_000  # cap the combined text sent to Claude for comment-fix checks

_STATUS_BY_ID = {
    0: "unknown", 1: "active", 2: "fixed", 3: "wontFix",
    4: "closed", 5: "byDesign", 6: "pending",
}

_REVIEW_PROMPT = textwrap.dedent("""\
    Выступай как Senior Software Engineer, проводящий код-ревью Pull Request.

    Тебе дан:
    1. Заголовок и описание PR.
    2. Список изменённых файлов с построчным diff. Формат каждой строки:
       "+  42 | код"  - добавленная/изменённая строка, число - её номер в ИТОГОВОЙ версии файла.
       "-     | код"  - удалённая строка (существовала только в старой версии, номера нет).
       "   42 | код"  - неизменённая контекстная строка, число - её номер в итоговой версии.

    Твоя задача - найти РЕАЛЬНЫЕ проблемы ИМЕННО В ИЗМЕНЁННОМ КОДЕ (строки,
    начинающиеся с "+"), учитывая следующие аспекты:
    - Логические ошибки, потенциальные баги, проблемы безопасности,
      несоответствия/несостыковки в коде.
    - Нарушения SOLID и DRY (дублирование логики, которое можно вынести в
      общее место) и KISS (излишне сложное решение там, где есть явно более
      простой вариант).
    - Если изменённый код - автотесты, дополнительно проверяй:
      * признаки нестабильных (flaky) тестов - жёсткие sleep/wait вместо
        ожидания условия, зависимость от порядка выполнения или внешнего
        состояния, недетерминированные данные/время;
      * качество локаторов - хрупкие селекторы (по числовому индексу,
        абсолютный XPath, привязка к тексту, который может измениться)
        вместо стабильных id/data-атрибутов/семантических локаторов;
      * качество и полноту assertions - слишком общие/слабые проверки
        (например, только код ответа без проверки данных), отсутствие
        проверки реального результата операции;
      * хардкод данных прямо в тесте (значения, id, строки, ожидаемые
        результаты и т.п. записаны буквально в теле теста) вместо констант/
        конфигурационных файлов/тест-данных - при изменении значения его
        придётся искать и менять по всему коду, а не в одном месте;
      * try/catch (или аналог в языке файла) непосредственно в самом
        тестовом файле - вся обработка ошибок/ожиданий при взаимодействии с
        элементами должна быть реализована в общих методах Page Object
        Model (POM), а не разбросана по тестам.
    - Naming (неясные/неточные имена переменных, функций, тест-кейсов) и
      best-practices языка программирования, на котором написан файл
      (определи язык по расширению файла в заголовке секции ниже).

    Правила
    -------
    - Комментируй ТОЛЬКО строки, начинающиеся с "+". Контекстные строки
      ("   ") помогают понять код, но сами по себе не предмет ревью, если
      они не были изменены в этом PR.
    - Не комментируй чистое форматирование без последствий для качества
      кода (пробелы, порядок импортов и т.п.) - но нейминг, дублирование,
      сложность решения и надёжность тестов ВСЕГДА в рамках ревью.
    - Для каждой проблемы укажи "line" - число перед "|" у строки с "+".
    - Если реальных проблем нет - верни пустой список findings. Не
      придумывай проблемы, если не уверен в них, и не занижай список
      найденных проблем ради краткости - лучше найти больше, чем пропустить.
    - "comment" должен подробно и технически точно включать три вещи:
      а) в чём конкретно проблема; б) конкретное последствие - что реально
      сломается и в каком сценарии; в) конкретное правильное исправление.
      Последствие описывай в терминах РЕАЛЬНОЙ роли изменённого файла: если
      это тестовый код (test/, integration_test/, *_test.*, тестовые хелперы,
      моки, page objects для тестов) - последствия только в терминах
      надёжности/стабильности тестов, отладки, CI, НИКОГДА не упоминай
      "пользователя" или UX, так как реальные пользователи не видят
      автотесты и их код; если это код приложения - последствия в терминах
      поведения приложения и его пользователей.
    - Пиши "comment" на "ты", в женском роде ("ты забыла", а не "забыл").
      БЕЗ смайлов/эмодзи, БЕЗ префиксов вроде "Ошибка:", "Рекомендация:",
      БЕЗ списков и маркированных пунктов.

    Формат ответа
    -------------
    Верни ТОЛЬКО валидный JSON без markdown-блоков:
    {{
        "summary": "краткое резюме ревью на русском (1-2 предложения)",
        "findings": [
            {{
                "file": "путь к файлу, точно как в заголовке файла ниже",
                "line": <int>,
                "severity": "critical" | "major" | "minor",
                "comment": "подробное описание проблемы, последствий и исправления"
            }}
        ]
    }}

    ── PR #{pr_id}: {title} ──────────────────────────────────────────────────
    {description}

    ── Изменённые файлы ────────────────────────────────────────────────────
    {files_diff_text}
""")

_VERIFY_PROMPT = textwrap.dedent("""\
    Выступай как Senior Software Engineer, который проверяет, действительно
    ли разработчик исправил замечания код-ревью, оставленные коллегой в Pull
    Request, а не просто пометил их как решённые.

    Тебе дан список тредов комментариев. Для каждого треда показаны:
    1. Файл и номер строки, к которой был оставлен комментарий.
    2. Текущий статус треда в Azure DevOps ("active" - открыт, "fixed"/
       "closed" - помечен автором кода как решённый, и т.д.). Этому статусу
       НЕЛЬЗЯ слепо доверять - именно его и нужно перепроверить по коду.
    3. Вся переписка в треде по порядку (первый комментарий - исходное
       замечание, остальные - ответы).
    4. "Код ДО" - фрагмент файла на момент, когда комментарий был оставлен.
    5. "Код СЕЙЧАС" - тот же фрагмент файла в актуальной версии PR.

    Твоя задача - для каждого треда определить статус исправления:
    - "fixed" - код действительно изменился и проблема, описанная в
      комментарии, устранена.
    - "not_fixed" - код не изменился по существу (или изменился, но
      проблема осталась), при этом тред помечен как решённый/закрытый, либо
      автор в переписке заявил, что исправил, а по факту нет.
    - "unclear" - невозможно уверенно определить (например, комментарий был
      не про конкретный код, а вопрос/обсуждение без конкретного действия,
      либо фрагмент кода недостаточен для вывода).

    Правила
    -------
    - Сравнивай "Код ДО" и "Код СЕЙЧАС" построчно - если единственное
      отличие не относится к сути замечания (пробелы, соседние правки),
      это НЕ считается исправлением.
    - Если тред уже "active" и по коду видно, что проблема устранена - всё
      равно верни "fixed" (значит автор забыл поменять статус).
    - "reasoning" - коротко и технически точно объясни вывод: что именно
      изменилось (или не изменилось) в коде относительно замечания.
    - Пиши "reasoning" на "ты", в женском роде. БЕЗ смайлов/эмодзи.
    - Если для not_fixed нужен текст, который стоит написать коллеге в
      ответ в треде - положи его в "reply_comment" (кратко, по-русски, на
      "ты", без смайлов); иначе оставь "reply_comment" пустой строкой.

    Формат ответа
    -------------
    Верни ТОЛЬКО валидный JSON без markdown-блоков:
    {{
        "results": [
            {{
                "thread_id": <int>,
                "verdict": "fixed" | "not_fixed" | "unclear",
                "reasoning": "...",
                "reply_comment": "..."
            }}
        ]
    }}

    ── PR #{pr_id}: {title} ──────────────────────────────────────────────────
    {description}

    ── Треды для проверки ──────────────────────────────────────────────────
    {threads_text}
""")


def _auth(pat: str) -> tuple[str, str]:
    return ("", pat)


async def _get_pr(azure_client, repo: str, pr_id: int) -> dict[str, Any]:
    url = f"{azure_client.project_base_url}/git/repositories/{repo}/pullrequests/{pr_id}"
    async with httpx.AsyncClient() as http:
        resp = await http.get(url, auth=_auth(azure_client.pat), params={"api-version": "7.1"}, timeout=30)
        resp.raise_for_status()
        return resp.json()


async def _get_iterations(azure_client, repo: str, pr_id: int) -> list[dict[str, Any]]:
    url = f"{azure_client.project_base_url}/git/repositories/{repo}/pullrequests/{pr_id}/iterations"
    async with httpx.AsyncClient() as http:
        resp = await http.get(url, auth=_auth(azure_client.pat), params={"api-version": "7.1"}, timeout=30)
        resp.raise_for_status()
        iterations = resp.json().get("value", [])
        if not iterations:
            raise ValueError(f"PR #{pr_id} has no iterations")
        return iterations


async def _get_iteration_changes(
    azure_client, repo: str, pr_id: int, iteration_id: int
) -> list[dict[str, Any]]:
    url = (
        f"{azure_client.project_base_url}/git/repositories/{repo}"
        f"/pullrequests/{pr_id}/iterations/{iteration_id}/changes"
    )
    collected: list[dict[str, Any]] = []
    skip, top = 0, 100
    async with httpx.AsyncClient() as http:
        while True:
            resp = await http.get(
                url, auth=_auth(azure_client.pat),
                params={"api-version": "7.1", "$top": top, "$skip": skip}, timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json().get("changeEntries", [])
            collected.extend(batch)
            if len(batch) < top:
                break
            skip += top
    return collected


async def _get_threads(azure_client, repo: str, pr_id: int) -> list[dict[str, Any]]:
    url = f"{azure_client.project_base_url}/git/repositories/{repo}/pullrequests/{pr_id}/threads"
    async with httpx.AsyncClient() as http:
        resp = await http.get(url, auth=_auth(azure_client.pat), params={"api-version": "7.1"}, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])


async def _fetch_item_content(azure_client, repo: str, path: str, commit_id: str) -> Optional[str]:
    """Fetch raw text content of `path` at `commit_id`. Returns None for binary/missing files."""
    url = f"{azure_client.project_base_url}/git/repositories/{repo}/items"
    params = {
        "path": path,
        "versionDescriptor.version": commit_id,
        "versionDescriptor.versionType": "commit",
        "download": "true",
        "api-version": "7.1",
    }
    async with httpx.AsyncClient() as http:
        try:
            resp = await http.get(url, auth=_auth(azure_client.pat), params=params, timeout=30)
        except Exception as exc:
            logger.warning(f"Error fetching {path}@{commit_id}: {exc}")
            return None
        if resp.status_code != 200:
            return None
        try:
            return resp.content.decode("utf-8")
        except UnicodeDecodeError:
            return None  # binary file


def _numbered_diff(before_text: str, after_text: str) -> str:
    """Render a line-numbered diff; '+' lines are numbered by their position in `after_text`."""
    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    sm = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    windowed = len(after_lines) > _WINDOW_THRESHOLD_LINES

    out: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            length = i2 - i1
            for k in range(length):
                if windowed and _CONTEXT_LINES <= k < length - _CONTEXT_LINES:
                    if k == _CONTEXT_LINES:
                        out.append("   ...  |")
                    continue
                ln = j1 + k + 1
                out.append(f"   {ln:5d} | {after_lines[j1 + k]}")
        else:
            for k in range(i1, i2):
                out.append(f"-       | {before_lines[k]}")
            for k in range(j1, j2):
                out.append(f"+  {k + 1:5d} | {after_lines[k]}")
    return "\n".join(out)


async def _build_file_diffs(
    azure_client, repo: str, changes: list[dict[str, Any]], base_commit: str, target_commit: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (per-file diff info, list of human-readable skip reasons)."""
    files: list[dict[str, Any]] = []
    skipped: list[str] = []

    for change in changes:
        item = change.get("item", {})
        path = item.get("path", "")
        change_type = str(change.get("changeType", ""))
        if not path or item.get("isFolder"):
            continue
        if "delete" in change_type:
            skipped.append(f"{path} (удалён - не рецензируется)")
            continue

        after_content = await _fetch_item_content(azure_client, repo, path, target_commit)
        if after_content is None:
            skipped.append(f"{path} (бинарный файл или недоступен)")
            continue
        if len(after_content) > _MAX_FILE_CHARS:
            skipped.append(f"{path} (слишком большой: {len(after_content)} символов)")
            continue

        before_content = "" if "add" in change_type else (
            await _fetch_item_content(azure_client, repo, path, base_commit) or ""
        )

        diff_text = _numbered_diff(before_content, after_content)
        if not diff_text.strip():
            continue
        files.append({"path": path, "change_type": change_type, "diff": diff_text})

    return files, skipped


def _build_review_prompt(
    pr_id: int, title: str, description: str, files: list[dict[str, Any]]
) -> tuple[str, list[str]]:
    """Build the review prompt, capping total size. Returns (prompt, files dropped for size)."""
    sections: list[str] = []
    dropped: list[str] = []
    total = 0
    for f in files:
        section = f"### Файл: {f['path']} ({f['change_type']})\n{f['diff']}\n"
        if total + len(section) > _MAX_TOTAL_DIFF_CHARS:
            dropped.append(f["path"])
            continue
        sections.append(section)
        total += len(section)

    prompt = _REVIEW_PROMPT.format(
        pr_id=pr_id,
        title=title,
        description=description or "(без описания)",
        files_diff_text="\n".join(sections),
    )
    return prompt, dropped


async def _post_line_comment(
    azure_client, repo: str, pr_id: int, file_path: str, line: int, text: str
) -> tuple[bool, Optional[str]]:
    url = f"{azure_client.project_base_url}/git/repositories/{repo}/pullrequests/{pr_id}/threads"
    path = file_path if file_path.startswith("/") else f"/{file_path}"
    body = {
        "comments": [{"parentCommentId": 0, "content": text, "commentType": 1}],
        "status": 1,
        "threadContext": {
            "filePath": path,
            "rightFileStart": {"line": line, "offset": 1},
            "rightFileEnd": {"line": line, "offset": 1},
        },
    }
    async with httpx.AsyncClient() as http:
        resp = await http.post(url, auth=_auth(azure_client.pat), params={"api-version": "7.1"}, json=body, timeout=30)
        if resp.status_code not in (200, 201):
            return False, f"HTTP {resp.status_code}: {resp.text}"
        return True, None


async def _post_reply(
    azure_client, repo: str, pr_id: int, thread_id: int, parent_comment_id: int, text: str
) -> tuple[bool, Optional[str]]:
    url = f"{azure_client.project_base_url}/git/repositories/{repo}/pullrequests/{pr_id}/threads/{thread_id}/comments"
    body = {"parentCommentId": parent_comment_id, "content": text, "commentType": 1}
    async with httpx.AsyncClient() as http:
        resp = await http.post(url, auth=_auth(azure_client.pat), params={"api-version": "7.1"}, json=body, timeout=30)
        if resp.status_code not in (200, 201):
            return False, f"HTTP {resp.status_code}: {resp.text}"
        return True, None


async def _reopen_thread(azure_client, repo: str, pr_id: int, thread_id: int) -> tuple[bool, Optional[str]]:
    url = f"{azure_client.project_base_url}/git/repositories/{repo}/pullrequests/{pr_id}/threads/{thread_id}"
    async with httpx.AsyncClient() as http:
        resp = await http.patch(url, auth=_auth(azure_client.pat), params={"api-version": "7.1"}, json={"status": 1}, timeout=30)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text}"
        return True, None


def _filter_threads(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only file-anchored threads that a colleague has actually engaged with:
    marked fixed/closed, or carrying at least one reply beyond the original comment.
    A still-open thread with a single (unanswered) comment has nothing to verify yet.
    """
    kept = []
    for thread in threads:
        if thread.get("isDeleted"):
            continue
        ctx = thread.get("threadContext")
        if not ctx or not ctx.get("filePath"):
            continue  # general PR discussion / system thread, not tied to a code line
        comments = [c for c in thread.get("comments", []) if not c.get("isDeleted") and c.get("commentType") != "system"]
        if not comments:
            continue
        status = _STATUS_BY_ID.get(thread.get("status", 0), "unknown")
        if status in ("fixed", "closed") or len(comments) > 1:
            kept.append(thread)
    return kept


def _thread_line(thread: dict[str, Any]) -> Optional[int]:
    ctx = thread.get("threadContext", {})
    right = ctx.get("rightFileStart") or ctx.get("rightFileEnd")
    left = ctx.get("leftFileStart") or ctx.get("leftFileEnd")
    point = right or left
    return point.get("line") if point else None


def _created_iteration_id(thread: dict[str, Any]) -> Optional[int]:
    pr_ctx = thread.get("pullRequestThreadContext") or {}
    iter_ctx = pr_ctx.get("iterationContext") or {}
    return iter_ctx.get("secondComparingIteration") or iter_ctx.get("firstComparingIteration")


def _extract_window(text: Optional[str], line: Optional[int]) -> str:
    if text is None:
        return "(файл недоступен или бинарный)"
    if not line:
        return "(номер строки неизвестен)"
    lines = text.splitlines()
    start = max(0, line - 1 - _THREAD_CONTEXT_LINES)
    end = min(len(lines), line + _THREAD_CONTEXT_LINES)
    if start >= len(lines):
        return "(строка вне диапазона файла - возможно, файл был сильно изменён)"
    return "\n".join(f"{i + 1:5d} | {lines[i]}" for i in range(start, end))


async def _build_thread_contexts(
    azure_client, repo: str, threads: list[dict[str, Any]],
    iterations: list[dict[str, Any]], latest_target_commit: str,
) -> list[dict[str, Any]]:
    """Attach before/after code windows and a flattened conversation to each thread."""
    iterations_by_id = {it["id"]: it for it in iterations}
    file_cache: dict[tuple[str, str], Optional[str]] = {}

    async def get_content(path: str, commit: str) -> Optional[str]:
        key = (path, commit)
        if key not in file_cache:
            file_cache[key] = await _fetch_item_content(azure_client, repo, path, commit)
        return file_cache[key]

    contexts = []
    for thread in threads:
        path = thread["threadContext"]["filePath"]
        line = _thread_line(thread)
        created_iter_id = _created_iteration_id(thread)
        before_commit = None
        if created_iter_id and created_iter_id in iterations_by_id:
            before_commit = (iterations_by_id[created_iter_id].get("sourceRefCommit") or {}).get("commitId")

        after_text = await get_content(path, latest_target_commit)
        before_text = await get_content(path, before_commit) if before_commit else after_text

        comments = [c for c in thread.get("comments", []) if not c.get("isDeleted") and c.get("commentType") != "system"]
        conversation = []
        for c in comments:
            author = (c.get("author") or {}).get("displayName", "?")
            conversation.append(f"[{author}]: {c.get('content', '').strip()}")

        contexts.append({
            "thread_id": thread["id"],
            "path": path,
            "line": line,
            "status": _STATUS_BY_ID.get(thread.get("status", 0), "unknown"),
            "parent_comment_id": comments[0]["id"] if comments else 1,
            "conversation": conversation,
            "before": _extract_window(before_text, line),
            "after": _extract_window(after_text, line),
        })
    return contexts


def _build_verify_prompt(
    pr_id: int, title: str, description: str, contexts: list[dict[str, Any]]
) -> tuple[str, list[int]]:
    """Build the verification prompt, capping total size. Returns (prompt, thread_ids dropped for size)."""
    sections: list[str] = []
    dropped: list[int] = []
    total = 0
    for ctx in contexts:
        section = (
            f"### Тред #{ctx['thread_id']} - {ctx['path']}:{ctx['line']} (статус: {ctx['status']})\n"
            f"Переписка:\n" + "\n".join(ctx["conversation"]) + "\n\n"
            f"Код ДО:\n{ctx['before']}\n\n"
            f"Код СЕЙЧАС:\n{ctx['after']}\n"
        )
        if total + len(section) > _MAX_TOTAL_PROMPT_CHARS:
            dropped.append(ctx["thread_id"])
            continue
        sections.append(section)
        total += len(section)

    prompt = _VERIFY_PROMPT.format(
        pr_id=pr_id,
        title=title,
        description=description or "(без описания)",
        threads_text="\n".join(sections),
    )
    return prompt, dropped


def _threads_signature(threads: list[dict[str, Any]], latest_target_commit: str) -> str:
    parts = [latest_target_commit]
    for t in sorted(threads, key=lambda t: t["id"]):
        comment_ids = ",".join(str(c.get("id")) for c in t.get("comments", []) if not c.get("isDeleted"))
        parts.append(f"{t['id']}:{t.get('status')}:{comment_ids}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


async def run_review_pull_request_workflow(
    azure_client,
    llm_client,
    *,
    repo: str,
    pr_id: int,
    no_anonymize: bool = False,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Review a Pull Request's latest iteration diff with Claude.

    Always a dry-run: findings are returned/persisted but never posted here.
    Use `post_review_comments()` against the resulting artifact to publish them.
    """

    def _log(level: str, msg: str) -> None:
        logger.info(msg) if level == "INFO" else logger.warning(msg) if level == "WARNING" else logger.debug(msg)
        if log_fn:
            log_fn(level, msg)

    def _canceled() -> bool:
        if should_cancel_fn and should_cancel_fn():
            _log("WARNING", "Остановлено пользователем")
            return True
        return False

    _log("INFO", f"Review workflow started: repo={repo}, pr={pr_id}")

    try:
        pr = await _get_pr(azure_client, repo, pr_id)
    except Exception as exc:
        _log("ERROR", f"Failed to fetch PR #{pr_id}: {exc}")
        return {"status": "failed", "error": str(exc)}

    title = pr.get("title", "")
    description = pr.get("description", "")
    _log("INFO", f"PR #{pr_id}: '{title}' ({pr.get('sourceRefName')} → {pr.get('targetRefName')})")

    if _canceled():
        return {"status": "canceled", "error": "Остановлено пользователем"}

    try:
        iterations = await _get_iterations(azure_client, repo, pr_id)
    except Exception as exc:
        _log("ERROR", f"Failed to fetch iterations for PR #{pr_id}: {exc}")
        return {"status": "failed", "error": str(exc)}

    iteration = max(iterations, key=lambda it: it["id"])
    base_commit = (iteration.get("targetRefCommit") or {}).get("commitId")
    target_commit = (iteration.get("sourceRefCommit") or {}).get("commitId")
    if not base_commit or not target_commit:
        _log("ERROR", f"Could not determine commit IDs for iteration {iteration.get('id')}")
        return {"status": "failed", "error": "Could not determine iteration commit IDs"}
    _log("INFO", f"Iteration {iteration['id']}: comparing {base_commit[:8]}…{target_commit[:8]}")

    if _canceled():
        return {"status": "canceled", "error": "Остановлено пользователем"}

    changes = await _get_iteration_changes(azure_client, repo, pr_id, iteration["id"])
    _log("INFO", f"Changed items: {len(changes)}")

    if _canceled():
        return {"status": "canceled", "error": "Остановлено пользователем"}

    files, skipped = await _build_file_diffs(azure_client, repo, changes, base_commit, target_commit)
    if not files:
        _log("WARNING", "No files with a text diff to review (all skipped, or PR is empty)")
        return {
            "status": "succeeded",
            "pr": {"id": pr_id, "title": title, "description": description,
                   "source_ref": pr.get("sourceRefName"), "target_ref": pr.get("targetRefName")},
            "iteration_id": iteration["id"], "base_commit": base_commit, "target_commit": target_commit,
            "summary": "Нет файлов с текстовым diff для ревью.",
            "findings": [], "files": [], "skipped": skipped, "dropped": [],
            "correlation_id": correlation_id,
        }
    _log("INFO", f"Files to review: {len(files)}" + (f", skipped: {len(skipped)}" if skipped else ""))

    if not no_anonymize:
        from packages.workflows.review.anonymize import Redactor

        redactor = Redactor(auto_git=True, auto_names=False)
        for f in files:
            f["diff"] = redactor.redact_text(f["diff"])
        description = redactor.redact_text(description or "")
        _log("INFO", "Diff anonymized before sending to Claude")

    prompt, dropped = _build_review_prompt(pr_id, title, description, files)
    if dropped:
        _log("WARNING", f"Dropped from analysis due to size limit ({_MAX_TOTAL_DIFF_CHARS} chars): {', '.join(dropped)}")

    if _canceled():
        return {"status": "canceled", "error": "Остановлено пользователем"}

    _log("INFO", "Sending code review request to Claude …")
    try:
        raw = await llm_client._generate_with_retry(prompt, max_attempts=5)
        if not raw:
            raise ValueError("Claude returned an empty response")
        review = llm_client._parse_json(raw)
    except Exception as exc:
        _log("ERROR", f"Claude review failed: {exc}")
        return {"status": "failed", "error": str(exc)}

    findings = review.get("findings", []) or []
    _log("INFO", f"Review complete: {len(findings)} finding(s)")

    return {
        "status": "succeeded",
        "pr": {"id": pr_id, "title": title, "description": description,
               "source_ref": pr.get("sourceRefName"), "target_ref": pr.get("targetRefName")},
        "iteration_id": iteration["id"], "base_commit": base_commit, "target_commit": target_commit,
        "summary": str(review.get("summary", "")),
        "findings": findings,
        "files": [{"path": f["path"], "change_type": f["change_type"]} for f in files],
        "skipped": skipped,
        "dropped": dropped,
        "correlation_id": correlation_id,
    }


async def post_review_comments(
    azure_client, repo: str, pr_id: int, findings: list[dict[str, Any]], files: list[dict[str, Any]],
) -> dict[str, Any]:
    """Post a subset (or all) of a review's findings as PR line comments.

    `files` is the workflow result's `files` list, used to resolve Claude's
    (sometimes slash-stripped) file path back to the canonical repo path.
    """
    path_by_normalized = {f["path"].lstrip("/"): f["path"] for f in files}
    posted: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for finding in findings:
        raw_file_path = str(finding.get("file", ""))
        line = finding.get("line")
        comment_text = str(finding.get("comment", "")).strip()
        resolved_path = path_by_normalized.get(raw_file_path.lstrip("/"))
        if not resolved_path or not isinstance(line, int) or not comment_text:
            errors.append({"file": raw_file_path, "line": line, "error": "Cannot resolve finding to a file/line"})
            continue
        ok, err = await _post_line_comment(azure_client, repo, pr_id, resolved_path, line, comment_text)
        if ok:
            posted.append({"file": resolved_path, "line": line})
        else:
            errors.append({"file": resolved_path, "line": line, "error": err})

    return {"posted": posted, "errors": errors, "posted_count": len(posted), "error_count": len(errors)}


async def run_review_comment_fixes_workflow(
    azure_client,
    llm_client,
    *,
    repo: str,
    pr_id: int,
    no_anonymize: bool = False,
    correlation_id: Optional[str] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    should_cancel_fn: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Verify with Claude whether PR review comment threads were actually fixed.

    Always a dry-run: verdicts are returned/persisted but no reply is posted
    and no thread is reopened here. Use `apply_comment_fix_results()` against
    the resulting artifact to act on "not_fixed" threads.
    """

    def _log(level: str, msg: str) -> None:
        logger.info(msg) if level == "INFO" else logger.warning(msg) if level == "WARNING" else logger.debug(msg)
        if log_fn:
            log_fn(level, msg)

    def _canceled() -> bool:
        if should_cancel_fn and should_cancel_fn():
            _log("WARNING", "Остановлено пользователем")
            return True
        return False

    _log("INFO", f"Comment-fix verification started: repo={repo}, pr={pr_id}")

    try:
        pr = await _get_pr(azure_client, repo, pr_id)
    except Exception as exc:
        _log("ERROR", f"Failed to fetch PR #{pr_id}: {exc}")
        return {"status": "failed", "error": str(exc)}

    title = pr.get("title", "")
    description = pr.get("description", "")
    _log("INFO", f"PR #{pr_id}: '{title}' ({pr.get('sourceRefName')} → {pr.get('targetRefName')})")

    if _canceled():
        return {"status": "canceled", "error": "Остановлено пользователем"}

    try:
        iterations = await _get_iterations(azure_client, repo, pr_id)
    except Exception as exc:
        _log("ERROR", f"Failed to fetch iterations for PR #{pr_id}: {exc}")
        return {"status": "failed", "error": str(exc)}

    latest_iteration = max(iterations, key=lambda it: it["id"])
    latest_target_commit = (latest_iteration.get("sourceRefCommit") or {}).get("commitId")
    if not latest_target_commit:
        _log("ERROR", "Could not determine the latest iteration's commit ID")
        return {"status": "failed", "error": "Could not determine latest iteration commit ID"}

    all_threads = await _get_threads(azure_client, repo, pr_id)
    threads = _filter_threads(all_threads)
    skipped_count = len(
        [t for t in all_threads if not t.get("isDeleted") and (t.get("threadContext") or {}).get("filePath")]
    ) - len(threads)

    if not threads:
        no_threads_message = (
            f"Ни один из {skipped_count} комментариев к коду в этом PR ещё не помечен "
            "исправленным и не получил ответа от автора кода — проверять пока нечего."
            if skipped_count
            else "В этом PR нет комментариев, привязанных к коду, — проверять нечего."
        )
        _log("INFO", no_threads_message)
        return {
            "status": "succeeded",
            "pr": {"id": pr_id, "title": title, "description": description,
                   "source_ref": pr.get("sourceRefName"), "target_ref": pr.get("targetRefName")},
            "contexts": [], "results": [], "skipped_count": skipped_count, "dropped": [],
            "message": no_threads_message,
            "correlation_id": correlation_id,
        }
    _log("INFO", f"Threads to verify: {len(threads)}" + (f", skipped (no reply): {skipped_count}" if skipped_count else ""))

    if _canceled():
        return {"status": "canceled", "error": "Остановлено пользователем"}

    contexts = await _build_thread_contexts(azure_client, repo, threads, iterations, latest_target_commit)

    if not no_anonymize:
        from packages.workflows.review.anonymize import Redactor

        redactor = Redactor(auto_git=True, auto_names=False)
        for ctx in contexts:
            ctx["before"] = redactor.redact_text(ctx["before"])
            ctx["after"] = redactor.redact_text(ctx["after"])
            ctx["conversation"] = [redactor.redact_text(line) for line in ctx["conversation"]]
        description = redactor.redact_text(description or "")
        _log("INFO", "Data anonymized before sending to Claude")

    prompt, dropped = _build_verify_prompt(pr_id, title, description, contexts)
    if dropped:
        _log("WARNING", f"Dropped from analysis due to size limit: {len(dropped)} thread(s)")

    if _canceled():
        return {"status": "canceled", "error": "Остановлено пользователем"}

    _log("INFO", "Sending comment-fix verification request to Claude …")
    try:
        raw = await llm_client._generate_with_retry(prompt, max_attempts=5)
        if not raw:
            raise ValueError("Claude returned an empty response")
        review = llm_client._parse_json(raw)
    except Exception as exc:
        _log("ERROR", f"Claude verification failed: {exc}")
        return {"status": "failed", "error": str(exc)}

    results = review.get("results", []) or []
    fixed = sum(1 for r in results if r.get("verdict") == "fixed")
    not_fixed = sum(1 for r in results if r.get("verdict") == "not_fixed")
    _log("INFO", f"Verification complete: fixed={fixed}, not_fixed={not_fixed}, total={len(results)}")

    # Contexts are persisted without the (potentially large) before/after code
    # windows or full conversation — apply only needs thread_id/path/line/status/parent_comment_id.
    slim_contexts = [
        {k: ctx[k] for k in ("thread_id", "path", "line", "status", "parent_comment_id")}
        for ctx in contexts
    ]

    return {
        "status": "succeeded",
        "pr": {"id": pr_id, "title": title, "description": description,
               "source_ref": pr.get("sourceRefName"), "target_ref": pr.get("targetRefName")},
        "contexts": slim_contexts,
        "results": results,
        "skipped_count": skipped_count,
        "dropped": dropped,
        "correlation_id": correlation_id,
    }


async def apply_comment_fix_results(
    azure_client, repo: str, pr_id: int,
    contexts: list[dict[str, Any]], results: list[dict[str, Any]],
    thread_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    """Reply into (and reopen, if needed) threads Claude flagged as not actually fixed."""
    results_by_id = {int(r["thread_id"]): r for r in results if "thread_id" in r}
    contexts_by_id = {int(ctx["thread_id"]): ctx for ctx in contexts}
    requested: Optional[set[int]] = set(thread_ids) if thread_ids else None

    handled: list[int] = []
    skipped: list[int] = []
    errors: list[dict[str, Any]] = []

    for thread_id, result in results_by_id.items():
        if result.get("verdict") != "not_fixed":
            continue
        if requested is not None and thread_id not in requested:
            continue
        ctx = contexts_by_id.get(thread_id)
        if not ctx:
            errors.append({"thread_id": thread_id, "error": "Thread context not found in artifact"})
            continue

        reply_text = str(result.get("reply_comment") or result.get("reasoning") or "").strip()
        if not reply_text:
            skipped.append(thread_id)
            continue

        ok, err = await _post_reply(azure_client, repo, pr_id, thread_id, ctx["parent_comment_id"], reply_text)
        if ctx.get("status") in ("fixed", "closed"):
            reopen_ok, reopen_err = await _reopen_thread(azure_client, repo, pr_id, thread_id)
            ok = ok and reopen_ok
            err = err or reopen_err
        if ok:
            handled.append(thread_id)
        else:
            errors.append({"thread_id": thread_id, "error": err})

    return {
        "handled": handled, "skipped": skipped, "errors": errors,
        "handled_count": len(handled), "error_count": len(errors),
    }


__all__ = [
    "run_review_pull_request_workflow",
    "post_review_comments",
    "run_review_comment_fixes_workflow",
    "apply_comment_fix_results",
]
