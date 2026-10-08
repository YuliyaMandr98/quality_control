"""Anthropic (Claude) integration client - Claude edition of this tool.

Drop-in replacement for the Gemini client used in the original
Triage Bugs Tool: implements exactly the surface every workflow actually
calls on its LLM client (`assess_bug`, `_generate_with_retry`,
`_parse_json`, `test_connection`, `get_health_status`), so
packages/workflows/triage and packages/workflows/review work completely
unmodified.
"""

import asyncio
import json
import platform
import time
import textwrap
from typing import Any, Optional

# Workaround for a bug in `truststore` (a transitive dependency of the
# anthropic SDK's bundled HTTP stack): on some macOS setups
# `platform.mac_ver()` returns an empty version string, which crashes
# truststore's SSL context setup at import time with
# `ValueError: invalid literal for int() with base 10: ''`. Only patch it
# when it's actually broken - leave normal platform detection alone
# everywhere else.
if not platform.mac_ver()[0]:
    _real_mac_ver = platform.mac_ver

    def _safe_mac_ver():
        version, versioninfo, machine = _real_mac_ver()
        return (version or "14.0.0", versioninfo, machine)

    platform.mac_ver = _safe_mac_ver

import anthropic

from packages.common import IntegrationConnectionStatus, IntegrationType, get_logger
from packages.integrations import IntegrationClient

logger = get_logger(__name__)

# Rate limiting: mirrors the original Gemini client's spacing between calls
_RATE_LIMIT_SECONDS = 10
_last_request_time: float = 0.0
_rate_limit_lock = asyncio.Lock()

# Per-field character cap for review_test_case_coverage's prompt (spec / technical
# implementation / test cases). Generous on purpose - a hard 12000-char cutoff
# used to silently drop the back half of any real Test Plan CSV export (dozens of
# test cases easily exceed that), making the review report cases as "missing"
# that were simply never sent to the model. 100k chars stays well within Claude's
# context window even with all three fields plus the prompt scaffolding.
_MAX_REVIEW_FIELD_CHARS = 100_000

# Output token budget for review_test_case_coverage specifically (overrides the
# 4096 default). This model spends part of its max_tokens budget on extended
# thinking before emitting the actual answer - for this prompt (large spec + large
# test case list + a 5-category JSON schema) thinking alone has been observed to
# consume ~4000 tokens, so the 4096 default leaves ~0 tokens for the real JSON
# response (stop_reason="max_tokens", empty/truncated text). 32k leaves generous
# headroom for both.
_REVIEW_MAX_OUTPUT_TOKENS = 32_000

# Output token budget for assess_bug specifically (overrides the 4096 default).
# The expected JSON answer is tiny, but extended thinking alone has been observed
# to exhaust the 4096 default before any answer text is emitted (stop_reason=
# "max_tokens", empty text, no exception raised - just a silent generic
# "Assessment failed" with no real reasoning). 8k leaves generous headroom for
# thinking while still being far below the review budget.
_ASSESS_BUG_MAX_OUTPUT_TOKENS = 8_000

# Same rationale as _ASSESS_BUG_MAX_OUTPUT_TOKENS, for classify_business_priority.
_CLASSIFY_PRIORITY_MAX_OUTPUT_TOKENS = 8_000

# For classify_test_case_priorities - a batch call returning one JSON object
# per test case, so the budget needs to scale with suite size. 16k comfortably
# covers suites well beyond anything seen in practice (largest so far: ~30
# test cases) while leaving headroom for extended thinking.
_CLASSIFY_TC_PRIORITY_MAX_OUTPUT_TOKENS = 16_000


class AnthropicClient(IntegrationClient):
    """Claude API client for bug triage and PR review assessment."""

    provider_type = IntegrationType.ANTHROPIC

    _TEST_TYPE_GUIDANCE = {
        "web": textwrap.dedent(
            """\
            Тип тестирования: WEB (браузерный UI).
            Обрати особое внимание на:
            - Валидацию полей форм (обязательность, форматы, границы длины/значений, сообщения об ошибках).
            - Альтернативные UI-сценарии: отмена действия, кнопка «назад» браузера, обновление страницы,
              повторная отправка формы, параллельные вкладки/сессии, таймаут сессии.
            - Edge-кейсы: пустые/очень длинные значения, спецсимволы, копипаст, медленное соединение,
              недоступность стороннего сервиса, разные разрешения экрана и масштабирование браузера.
            - Доступность (клавиатурная навигация, сообщения об ошибках), если это прослеживается
              в требованиях.

            ВАЖНО: наличие тест-кейсов на локализацию и на тёмную тему проверяется отдельно,
            автоматической проверкой вне этого анализа — НЕ указывай их отсутствие сам ни в
            missing_requirements, ни в других категориях, даже если не видишь таких тест-кейсов
            среди присланных.
            """
        ),
        "mobile": textwrap.dedent(
            """\
            Тип тестирования: MOBILE (нативное/мобильное приложение).
            Обрати особое внимание на:
            - Валидацию полей форм так же, как в web, но с учётом мобильного ввода (клавиатура,
              автозаполнение, разные типы клавиатур).
            - Альтернативные сценарии: сворачивание/разворачивание приложения, уход в фон и возврат,
              входящий звонок/уведомление во время сценария, переключение Wi-Fi/мобильная сеть, разрыв
              связи и восстановление, повторный запуск приложения посреди сценария.
            - Разрешения устройства (камера, геолокация, уведомления, биометрия) — сценарии отказа и
              повторного запроса разрешения.
            - Edge-кейсы: разные размеры экрана и ОС (iOS/Android, версии), офлайн-режим, низкий заряд
              батареи/память, push-уведомления, deep links.

            ВАЖНО: наличие тест-кейсов на локализацию и на тёмную тему проверяется отдельно,
            автоматической проверкой вне этого анализа — НЕ указывай их отсутствие сам ни в
            missing_requirements, ни в других категориях, даже если не видишь таких тест-кейсов
            среди присланных.
            """
        ),
        "api": textwrap.dedent(
            """\
            Тип тестирования: API.
            Обрати особое внимание на:
            - Валидацию запроса/ответа: обязательные/необязательные поля, типы данных, граничные
              значения, некорректные/отсутствующие параметры, некорректный Content-Type/формат тела.
            - Коды ответа и структуру ошибок для каждого сценария (успех, 4xx, 5xx), включая точные
              тексты/коды ошибок, если они описаны в технической реализации.
            - Аутентификацию/авторизацию: отсутствующий/просроченный/невалидный токен, доступ без
              нужной роли.
            - Альтернативные сценарии: повторный вызов (идемпотентность), конкурентные запросы,
              частичные сбои, пагинация/сортировка/фильтрация, если применимо.
            - Edge-кейсы: пустые массивы/коллекции, максимальные размеры payload, unicode/спецсимволы,
              rate limiting.
            Сверяй тест-кейсы не только со спецификацией, но и с приложенной технической реализацией —
            если в реализации есть логика/ветвления/ошибки, не упомянутые в спецификации явно, но не
            покрытые тест-кейсами, обязательно укажи это как пробел.
            """
        ),
    }

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "claude-sonnet-5")
        self.max_tokens = int(config.get("max_tokens", 4096))
        self._client: Optional[anthropic.AsyncAnthropic] = (
            anthropic.AsyncAnthropic(api_key=self.api_key) if self.api_key else None
        )

    async def _generate(self, prompt: str, *, max_tokens: Optional[int] = None) -> Optional[str]:
        """Call the Claude Messages API asynchronously with rate limiting.

        `max_tokens` overrides `self.max_tokens` for this call only. This model can
        spend a large chunk of that budget on extended-thinking tokens before it
        ever emits the actual answer - for long/complex prompts (e.g. a full test
        case coverage review) the default 4096 budget can be exhausted entirely by
        thinking, leaving `stop_reason="max_tokens"` and an empty/truncated text
        block. Callers with a large expected output should pass a generous override.
        """
        global _last_request_time

        if not self._client:
            return None

        async with _rate_limit_lock:
            elapsed = time.time() - _last_request_time
            if elapsed < _RATE_LIMIT_SECONDS:
                sleep_time = _RATE_LIMIT_SECONDS - elapsed
                logger.debug(f"Rate limiting: sleeping {sleep_time:.2f}s before next Claude request")
                await asyncio.sleep(sleep_time)
            _last_request_time = time.time()

        # Streaming (not .create()) is required by the SDK once max_tokens is large
        # enough that a non-streamed call could plausibly run past its 10-minute
        # client-side timeout ("Streaming is required for operations that may take
        # longer than 10 minutes"). Using it unconditionally keeps every call
        # (small or large max_tokens) on one code path.
        async with self._client.messages.stream(
            model=self.model,
            max_tokens=max_tokens or self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = await stream.get_final_message()
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        if response.stop_reason == "max_tokens":
            logger.warning(
                f"Claude hit max_tokens (budget={max_tokens or self.max_tokens}) before finishing; "
                f"got {len(text)} chars of text. Response may be truncated or empty - consider raising max_tokens."
            )
        return text

    async def _generate_with_retry(
        self, prompt: str, *, max_attempts: int = 5, base_backoff_seconds: int = 10,
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        """Generate content with bounded retries for transient provider failures."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._generate(prompt, max_tokens=max_tokens)
            except Exception as e:
                last_exc = e
                msg = str(e)
                retryable = any(token in msg for token in ["429", "overloaded", "500", "502", "503", "529"])
                if not retryable or attempt == max_attempts:
                    break
                wait_s = max(_RATE_LIMIT_SECONDS, base_backoff_seconds * attempt)
                logger.warning(
                    f"Claude transient error on attempt {attempt}/{max_attempts}; retrying in {wait_s}s: {msg}"
                )
                await asyncio.sleep(wait_s)

        if last_exc:
            raise last_exc
        return None

    async def test_connection(self) -> tuple[bool, Optional[str]]:
        """Test connection to Claude."""
        if not self.api_key:
            return False, "API key not configured"
        try:
            text = await self._generate("Say 'OK' if you can read this.")
            if text:
                return True, None
            return False, "Empty response from Claude"
        except Exception as e:
            return False, str(e)

    async def get_health_status(self) -> IntegrationConnectionStatus:
        """Get current health status."""
        if not self.api_key:
            return IntegrationConnectionStatus.UNCONFIGURED
        success, _ = await self.test_connection()
        return (
            IntegrationConnectionStatus.HEALTHY
            if success
            else IntegrationConnectionStatus.UNHEALTHY
        )

    def _parse_json(self, text: str) -> Any:
        """Extract and parse JSON that may be wrapped in markdown code fences."""
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        return json.loads(text.strip())

    async def assess_bug(
        self,
        us_content: str,
        bug_title: str,
        bug_description: str,
    ) -> dict[str, Any]:
        """Assess whether a bug ticket describes a real defect and determine severity/impact.

        Returns a dict with:
        - is_real_bug: bool
        - severity: "Critical" | "Major" | "Minor"
        - impact: one of the four JIRA impact values
        - priority: one of the five JIRA priority values
        - reasoning: short explanation
        """
        if not self.api_key:
            return {
                "is_real_bug": False,
                "severity": "Major",
                "impact": "Moderate / Limited",
                "priority": "Medium",
                "reasoning": "Anthropic API key not configured",
            }

        prompt = textwrap.dedent(f"""\
            You are a QA expert reviewing a bug ticket for triage.

            Given a user story specification and a bug report, determine:
            1. Is this a REAL bug (not a feature request, documentation issue, or user misunderstanding)?
            2. If real: What is the severity level?
            3. If real: What is the business impact?

            User Story (Specification):
            {us_content[:8000]}

            Bug Title:
            {bug_title}

            Bug Description:
            {bug_description[:4000]}

            Respond with valid JSON only (no markdown). Write the "reasoning"
            value in RUSSIAN, regardless of the language of the
            specification/bug text above - this field is read directly by a
            Russian-speaking QA team:
            {{
                "is_real_bug": true or false,
                "severity": "Critical" or "Major" or "Minor",
                "impact": "Extensive / Widespread" or "Significant / Large" or "Moderate / Limited" or "Minor / Localized",
                "priority": "Highest" or "High" or "Medium" or "Low" or "Lowest",
                "reasoning": "Краткое объяснение на русском языке"
            }}

            SEVERITY GUIDE:
            - Critical: system crash, data loss, security breach, or complete feature unavailability
            - Major: core feature broken but workaround exists; significant user impact
            - Minor: cosmetic issues, edge cases, minor UX problems

            IMPACT GUIDE:
            - Extensive / Widespread: affects all or most users / all environments
            - Significant / Large: affects many users or multiple key workflows
            - Moderate / Limited: affects some users or a non-critical workflow
            - Minor / Localized: affects very few users or a rarely used feature

            PRIORITY GUIDE:
            - Highest: Critical severity + Extensive impact; blocks a release or causes data loss
            - High: Critical/Major severity + Significant impact; core feature broken
            - Medium: Major severity + Moderate impact; workaround available
            - Low: Minor severity or Minor/Localized impact
            - Lowest: Cosmetic or very edge-case issues

            If NOT a real bug, set priority to "Low" and explain why in reasoning (in Russian).""")

        try:
            text = await self._generate_with_retry(
                prompt,
                max_attempts=3,
                base_backoff_seconds=_RATE_LIMIT_SECONDS,
                max_tokens=_ASSESS_BUG_MAX_OUTPUT_TOKENS,
            )
            if text:
                result = self._parse_json(text)
                severity = result.get("severity", "Major")
                impact = result.get("impact", "Moderate / Limited")
                priority = result.get("priority", "Medium")

                if severity not in ("Critical", "Major", "Minor"):
                    severity = "Major"
                if impact not in (
                    "Extensive / Widespread",
                    "Significant / Large",
                    "Moderate / Limited",
                    "Minor / Localized",
                ):
                    impact = "Moderate / Limited"
                if priority not in ("Highest", "High", "Medium", "Low", "Lowest"):
                    priority = "Medium"

                return {
                    "is_real_bug": bool(result.get("is_real_bug", False)),
                    "severity": severity,
                    "impact": impact,
                    "priority": priority,
                    "reasoning": str(result.get("reasoning", "")),
                }

            logger.error(
                "Claude bug assessment returned empty text (likely exhausted "
                f"max_tokens={_ASSESS_BUG_MAX_OUTPUT_TOKENS} on extended thinking)"
            )
            return {
                "is_real_bug": False,
                "severity": "Major",
                "impact": "Moderate / Limited",
                "priority": "Medium",
                "reasoning": (
                    "Оценка не выполнена: Claude вернул пустой ответ (вероятно, "
                    "исчерпан бюджет токенов на «размышления» модели)"
                ),
            }
        except Exception as e:
            logger.error(f"Claude bug assessment failed: {str(e)}")
            return {
                "is_real_bug": False,
                "severity": "Major",
                "impact": "Moderate / Limited",
                "priority": "Medium",
                "reasoning": f"Оценка не выполнена: {e}",
            }

    async def classify_business_priority(self, us_title: str, us_text: str) -> dict[str, Any]:
        """Classify a User Story's Azure DevOps test-case priority tier (P0/P1/P2)
        from its Confluence spec, per the team's business-criticality rubric.

        Returns a dict with `tier` ("P0" | "P1" | "P2") and `reasoning` (Russian).
        Falls back to "P1" (a neutral middle tier, avoids silently deprioritizing
        an unclassifiable but potentially critical feature) when the API key is
        missing or the call fails.
        """
        if not self.api_key:
            return {"tier": "P1", "reasoning": "Anthropic API key not configured"}

        prompt = textwrap.dedent(f"""\
            Ты - QA-лид, определяющий бизнес-приоритет тестирования (P0/P1/P2) для
            функциональности, описанной в спецификации ниже, строго по следующему
            правилу классификации:

            P0 - Critical. Фичи с прямым движением денег клиента, аутентификацией,
            необратимыми операциями (платежи, переводы, погашение кредита, открытие
            вклада). Получают Layer 1 в первую очередь, Layer 2 - по возможности.

            P1 - High. Фичи, без которых приложение неюзабельно (логин, главный
            экран, просмотр продуктов, профиль). Layer 1 обязателен, Layer 2/3 - по
            остатку времени.

            P2 - Medium/Low. Второстепенные/косметические/settings-фичи, а также
            фичи, явно ИСКЛЮЧЁННЫЕ из текущего релиза/цикла тестирования (например,
            статус говорит "перенесено на следующий релиз", "отменено", "не входит
            в этот релиз"). Тестируются в последнюю очередь либо переносятся на
            пост-релизный цикл.

            ВАЖНО про статус разработки: тест-кейсы пишутся ЗАРАНЕЕ, пока разработка
            ещё идёт, а само тестирование начнётся только после того, как функционал
            будет полностью готов. Поэтому текущий статус реализации - "Design",
            "Design in progress", "BE Development", "FE is blocked", "In Progress" и
            т.п. - НЕ является основанием для P2 сам по себе: оценивай тир по тому,
            каким будет функционал, когда он будет полностью готов к тестированию, а
            не по тому, на каком этапе разработки он находится сейчас. В P2 из-за
            статуса понижай только если спецификация явно говорит, что фича не войдёт
            в текущий релиз/цикл тестирования вообще (перенесена на следующий релиз,
            отменена, вне скоупа) - это другое, чем "ещё не реализовано".

            Название User Story: {us_title}

            Текст спецификации (Confluence):
            {us_text[:8000]}

            Определи, к какому тиру относится эта функциональность, строго следуя
            правилу выше.

            Ответь ТОЛЬКО валидным JSON (без markdown):
            {{
                "tier": "P0" or "P1" or "P2",
                "reasoning": "Краткое объяснение на русском языке, 1-2 предложения"
            }}""")

        try:
            text = await self._generate_with_retry(
                prompt,
                max_attempts=6,
                base_backoff_seconds=20,
                max_tokens=_CLASSIFY_PRIORITY_MAX_OUTPUT_TOKENS,
            )
            if text:
                result = self._parse_json(text)
                tier = result.get("tier", "P1")
                if tier not in ("P0", "P1", "P2"):
                    tier = "P1"
                return {"tier": tier, "reasoning": str(result.get("reasoning", ""))}
            logger.error(
                "Claude business-priority classification returned empty text (likely "
                f"exhausted max_tokens={_CLASSIFY_PRIORITY_MAX_OUTPUT_TOKENS} on extended thinking)"
            )
        except Exception as e:
            logger.error(f"Claude business-priority classification failed: {str(e)}")
            return {"tier": "P1", "reasoning": f"Оценка не выполнена: {e}"}

        return {"tier": "P1", "reasoning": "Classification failed"}

    async def classify_test_case_priorities(
        self, us_title: str, business_tier: str, business_priority: str, test_cases: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Classify each test case's own Azure DevOps Priority within a feature
        already tiered P0/P1/P2 (via classify_business_priority) - not every
        test case in a critical feature is equally critical: a P0 payments
        feature's happy-path test is High, but a cosmetic label-wording check
        in the same feature isn't.

        `test_cases` is a list of {"id": str, "title": str, "steps_text": str}.
        Returns a list of {"id": str, "priority": "High"|"Medium"|"Low",
        "reasoning": str (Russian)}, one per input test case (matched back to
        callers by "id", not position). Falls back to `business_priority` for
        every test case (the old blanket behavior) when the API key is
        missing, the call fails, or a given id doesn't come back at all.
        """
        if not self.api_key or not test_cases:
            return [
                {"id": tc["id"], "priority": business_priority, "reasoning": "Anthropic API key not configured"}
                for tc in test_cases
            ]

        items_text = "\n\n".join(
            f"id={tc['id']}\nНазвание: {tc['title']}\nШаги: {(tc.get('steps_text') or '')[:600] or '(нет шагов)'}"
            for tc in test_cases
        )

        prompt = textwrap.dedent(f"""\
            Ты - QA-лид, определяющий индивидуальный приоритет (Azure DevOps
            Priority: High/Medium/Low) для каждого тест-кейса внутри одной
            фичи.

            Фича: "{us_title}"
            Общий бизнес-тир фичи: {business_tier} (соответствует Azure DevOps
            Priority = {business_priority} для её ОСНОВНОГО/критического
            сценария).

            Не все тест-кейсы внутри одной фичи одинаково важны:
            - Позитивные (happy path) сценарии, проверяющие ИМЕННО то основное
              поведение, из-за которого фича получила тир {business_tier}, -
              получают приоритет {business_priority} (как у самой фичи).
            - Негативные/валидационные/граничные/edge-сценарии, как правило,
              на один уровень НИЖЕ (High -> Medium, Medium -> Low), кроме
              случаев, когда сам такой сценарий защищает что-то критическое
              (например, проверка безопасности/авторизации в P0-фиче остаётся
              High).
            - Чисто второстепенные/косметические проверки (тексты сообщений,
              форматирование, мелкие UI-детали) - Low, независимо от тира
              фичи.

            Приоритет тест-кейса НЕ должен превышать {business_priority} - тир
            фичи это потолок, а не пол: внутри фичи можно и нужно понижать
            приоритет менее важных сценариев.

            Тест-кейсы:
            {items_text}

            Ответь ТОЛЬКО валидным JSON (без markdown) - массив, один объект
            на каждый тест-кейс:
            [
              {{
                "id": "<тот же id>",
                "priority": "High" or "Medium" or "Low",
                "reasoning": "Краткое объяснение на русском языке, 1 предложение"
              }},
              ...
            ]

            Верни ровно {len(test_cases)} объектов, по одному на каждый id выше.""")

        try:
            text = await self._generate_with_retry(
                prompt,
                max_attempts=6,
                base_backoff_seconds=20,
                max_tokens=_CLASSIFY_TC_PRIORITY_MAX_OUTPUT_TOKENS,
            )
            if text:
                parsed = self._parse_json(text)
                if isinstance(parsed, list):
                    by_id: dict[str, dict[str, Any]] = {}
                    for item in parsed:
                        tc_id = str(item.get("id", ""))
                        priority = item.get("priority", business_priority)
                        if priority not in ("High", "Medium", "Low"):
                            priority = business_priority
                        by_id[tc_id] = {"id": tc_id, "priority": priority, "reasoning": str(item.get("reasoning", ""))}
                    return [
                        by_id.get(tc["id"]) or {"id": tc["id"], "priority": business_priority, "reasoning": "Не вернулось от LLM"}
                        for tc in test_cases
                    ]
            logger.error(
                "Claude test-case priority classification returned empty text (likely "
                f"exhausted max_tokens={_CLASSIFY_TC_PRIORITY_MAX_OUTPUT_TOKENS} on extended thinking)"
            )
        except Exception as e:
            logger.error(f"Claude test-case priority classification failed: {str(e)}")
            return [
                {"id": tc["id"], "priority": business_priority, "reasoning": f"Оценка не выполнена: {e}"}
                for tc in test_cases
            ]

        return [
            {"id": tc["id"], "priority": business_priority, "reasoning": "Classification failed"}
            for tc in test_cases
        ]

    async def review_test_case_coverage(
        self,
        *,
        test_type: str,
        us: str,
        spec_text: str,
        test_cases_text: str,
        tech_impl_text: str = "",
    ) -> dict[str, Any]:
        """Review pasted test cases against a spec (+ technical implementation for API)
        for completeness: missing requirements, validations, alternative scenarios,
        edge cases. `test_type` selects the review focus (web/mobile/api).

        Returns a dict with `missing_requirements`, `missing_validations`,
        `missing_alternative_flows`, `missing_edge_cases`, `ambiguities` (lists of
        `{title, description}`), `well_covered` (list of strings) and
        `overall_assessment` (string).
        """
        fallback: dict[str, Any] = {
            "missing_requirements": [],
            "missing_validations": [],
            "missing_alternative_flows": [],
            "missing_edge_cases": [],
            "ambiguities": [],
            "well_covered": [],
            "overall_assessment": "",
        }
        if not self.api_key:
            return {**fallback, "overall_assessment": "Anthropic API key not configured"}

        guidance = self._TEST_TYPE_GUIDANCE.get(test_type, "")
        tech_impl_section = (
            f"\n─── Техническая реализация ──────────────────────────────────\n{tech_impl_text[:_MAX_REVIEW_FIELD_CHARS]}\n"
            if tech_impl_text
            else ""
        )

        prompt = textwrap.dedent(f"""\
            Ты — опытный QA-лид. Проверь тест-кейсы для User Story {us} на полноту покрытия:
            все требования, валидации, альтернативные сценарии и edge-кейсы.

            {guidance}
            ─── Спецификация (User Story) ──────────────────────────────────
            {spec_text[:_MAX_REVIEW_FIELD_CHARS]}
            {tech_impl_section}
            ─── Тест-кейсы для проверки ──────────────────────────────────────
            {test_cases_text[:_MAX_REVIEW_FIELD_CHARS]}

            ─── Задача ──────────────────────────────────────────────────────
            Проанализируй тест-кейсы относительно спецификации{" и технической реализации" if tech_impl_text else ""}
            и верни JSON со следующими полями (каждый список из объектов
            {{"title": ..., "description": ...}}, кратких и конкретных, на русском языке;
            если пунктов нет — пустой список):

            - missing_requirements: требования/критерии приёмки из спецификации, не покрытые
              ни одним тест-кейсом
            - missing_validations: недостающие проверки валидации (входные данные, поля,
              форматы, границы)
            - missing_alternative_flows: недостающие альтернативные/негативные сценарии
              (не только happy path)
            - missing_edge_cases: недостающие граничные/edge-кейсы
            - ambiguities: неоднозначности, противоречия или риски, которые ты заметил
              (в спецификации или в самих тест-кейсах), не относящиеся напрямую к пробелам
              в покрытии
            - well_covered: краткий список (строки, не объекты) того, что уже хорошо
              покрыто — не более 5 пунктов
            - overall_assessment: 2-4 предложения — итоговая оценка полноты покрытия

            Каждый пункт в missing_* должен явно объяснять, ЧТО именно не покрыто и ПОЧЕМУ
            (со ссылкой на конкретное требование/раздел спецификации), а не быть общей
            рекомендацией.

            Верни ТОЛЬКО валидный JSON, без markdown-обрамления.
        """)

        try:
            text = await self._generate_with_retry(
                prompt, max_attempts=5, base_backoff_seconds=_RATE_LIMIT_SECONDS,
                max_tokens=_REVIEW_MAX_OUTPUT_TOKENS,
            )
            if text:
                parsed = self._parse_json(text)
                result = dict(fallback)
                for key in (
                    "missing_requirements",
                    "missing_validations",
                    "missing_alternative_flows",
                    "missing_edge_cases",
                    "ambiguities",
                ):
                    items = parsed.get(key) or []
                    result[key] = [
                        {
                            "title": str(item.get("title") or "").strip(),
                            "description": str(item.get("description") or "").strip(),
                        }
                        for item in items
                        if isinstance(item, dict) and (item.get("title") or item.get("description"))
                    ]
                result["well_covered"] = [
                    str(x).strip() for x in (parsed.get("well_covered") or []) if str(x).strip()
                ]
                result["overall_assessment"] = str(parsed.get("overall_assessment") or "").strip()
                return result
        except Exception as e:
            logger.error(f"Claude test case coverage review failed: {str(e)}")

        return {**fallback, "overall_assessment": "Не удалось выполнить анализ (ошибка LLM)"}

    async def extract_bug_repro_steps(self, bug_key: str, summary: str, description_text: str) -> dict[str, Any]:
        """Extract a structured precondition/steps/expected-result breakdown from a
        Jira bug's freeform description, for converting it into an Azure DevOps
        Test Case. Real bug reports vary wildly in formatting (proper headings,
        inline bold labels inside one paragraph, plain "1. 2. 3." text, or no
        structure at all) - too inconsistent for a reliable regex/heading parser,
        so an LLM does the extraction instead.

        Returns {"precondition": str, "steps": list[str], "expected_result": str}.
        `steps` always has at least one entry (falls back to the bug summary if no
        concrete steps could be identified).
        """
        fallback = {"precondition": "", "steps": [summary or bug_key], "expected_result": ""}
        if not self.api_key:
            return fallback

        prompt = textwrap.dedent(f"""\
            Ты — опытный QA-инженер. Дан баг-репорт из Jira ({bug_key}): {summary}

            Текст бага:
            {description_text[:8000]}

            Извлеки из текста:
            - precondition: предусловия для воспроизведения (если явно описаны), одной
              короткой строкой; если предусловий нет — пустая строка.
            - steps: шаги воспроизведения бага как список отдельных конкретных действий
              (каждый пункт — одно действие, без нумерации в самом тексте). Если шаги
              описаны не списком, а сплошным текстом или неструктурированно — сам
              логично раздели их на отдельные шаги. Если вообще невозможно выделить
              шаги — верни список из одного элемента с кратким описанием сути бага.
            - expected_result: ожидаемый (корректный) результат, который должен
              происходить согласно требованиям — одной строкой (можно с переносами).
              Если явно не указан — оставь пустую строку.

            Верни ТОЛЬКО валидный JSON:
            {{"precondition": "...", "steps": ["...", "..."], "expected_result": "..."}}
        """)

        try:
            text = await self._generate_with_retry(prompt, max_attempts=5, base_backoff_seconds=_RATE_LIMIT_SECONDS)
            if text:
                parsed = self._parse_json(text)
                steps = [str(s).strip() for s in (parsed.get("steps") or []) if str(s).strip()]
                return {
                    "precondition": str(parsed.get("precondition") or "").strip(),
                    "steps": steps or fallback["steps"],
                    "expected_result": str(parsed.get("expected_result") or "").strip(),
                }
        except Exception as e:
            logger.error(f"Claude bug repro-steps extraction failed for {bug_key}: {str(e)}")

        return fallback

    async def classify_skipped_tests(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Work out why each candidate test is actually skipped/flagged.

        Each item in `batch` is `{id, file, test_name, snippet}`, where `snippet` is the
        test's source (plus any leading comment and resolved constant text). A comment
        mentioning a bug number does NOT always describe *this* test's own reason for
        being skipped — it may be incidental historical context about a different test
        or a past decision. Only report a bug_key when it is genuinely why THIS test is
        currently skipped/flagged.

        Returns a list of `{id, is_skipped, category, bug_keys, reason_summary}`.
        """
        if not self.api_key or not batch:
            return [
                {
                    "id": item["id"],
                    "is_skipped": True,
                    "category": "unclear",
                    "bug_keys": [],
                    "reason_summary": "Anthropic API key not configured",
                }
                for item in batch
            ]

        items_text = "\n\n".join(
            f"### id={item['id']} | file={item['file']} | test_name={item['test_name']!r}\n"
            f"```\n{item['snippet']}\n```"
            for item in batch
        )

        prompt = textwrap.dedent(f"""\
            You are a QA lead auditing skipped/flagged Playwright tests in a TypeScript
            test suite. For EACH test below, work out why it is disabled or flagged, using
            ONLY evidence from its own snippet.

            IMPORTANT: a `//` comment or bug number appearing near a test does not always
            describe THAT test's own reason for being skipped — it can be a leftover
            historical note about a different test or a past decision (e.g. "MB-6024
            отменён (won't fix)." right above a test that is actually skipped for an
            unrelated reason like a named constant `BLOCKED_BY_...`). Only extract a bug
            key when the snippet clearly ties it to the CURRENT reason this specific test
            doesn't run. If a resolved constant's text (`// CONST_NAME = "..."`) is present,
            treat that as the authoritative reason.

            Tests to classify:

            {items_text}

            Respond with valid JSON only (no markdown), an array with one object per test:
            [
              {{
                "id": <the same id>,
                "is_skipped": true or false,   // true if this test does not run at all
                                                 // (whole-test .skip, or a runtime
                                                 // test.skip(true, ...) inside the body);
                                                 // false if it runs normally and the
                                                 // comment is just a known-issue caveat
                "category": "bug_reference" or "waiting_for_answer" or "todo_backlog" or "unclear",
                "bug_keys": ["MB-1234", ...],   // bug keys that are the ACTUAL reason for
                                                 // this test's current state; normalize a
                                                 // Cyrillic "МВ-" prefix to "MB-"; empty
                                                 // array if none apply
                "reason_summary": "краткое объяснение на русском, 1 предложение"
              }},
              ...
            ]

            CATEGORY GUIDE:
            - bug_reference: skip/caveat is because of a specific tracked bug (bug_keys non-empty)
            - waiting_for_answer: blocked on someone's answer/decision, no bug ticket ("жду ответа...")
            - todo_backlog: generic TODO — needs test data, env control, admin panel work, etc.
            - unclear: cannot determine a concrete reason from the snippet

            Return exactly {len(batch)} objects, one per listed id.""")

        try:
            text = await self._generate_with_retry(
                prompt, max_attempts=3, base_backoff_seconds=_RATE_LIMIT_SECONDS
            )
            if text:
                parsed = self._parse_json(text)
                if isinstance(parsed, list):
                    return parsed
        except Exception as e:
            logger.error(f"Claude skipped-test classification failed: {str(e)}")

        return [
            {
                "id": item["id"],
                "is_skipped": True,
                "category": "unclear",
                "bug_keys": [],
                "reason_summary": "Не удалось классифицировать (ошибка LLM)",
            }
            for item in batch
        ]


__all__ = ["AnthropicClient"]
