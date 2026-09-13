# Triage Bugs Tool (Claude edition)

**Claude-версия** этого инструмента — идентична оригинальной [Triage Bugs Tool](../triage-bugs-tool),
но вместо Google Gemini использует Anthropic Claude (`ANTHROPIC_API_KEY`) для всех LLM-вызовов.
Работает отдельно от оригинала: свой порт (**8001**), своя БД (`triage_bugs_claude.db`), свой git —
можно держать оба проекта рядом в `~/PROJECTS` и сравнивать результаты Gemini vs Claude.

Автономный инструмент для триажа багов из Jira: берёт баг-тикеты, находит их спецификацию
(User Story) в Confluence и просит Claude классифицировать severity/impact/priority и решить,
реальный ли это баг. Работает в режиме dry-run (только предпросмотр) или apply (реально
пишет в Jira).

Плюс три workflow для работы с Azure DevOps через Claude:
**Review Pull Request** (ревью diff'а на баги/несостыковки, с возможностью запостить найденное
как комментарии к строкам), **Review Comment Fixes** (проверка, действительно ли исправлены
комментарии, помеченные автором PR как «исправлено», а не просто закрыты без реального фикса)
и **Upload Test Cases** (загрузка провалидированного CSV с тест-кейсами в нужный Test Plan —
Web/Mobile/API — с автоматическим резолвом suite-цепочки по Confluence-спецификациям User Story).

Это выделенная часть проекта **Trace2Quality** — функционал Triage Bugs, PR-ревью и загрузки
тест-кейсов, плюс всё, от чего они зависят (без coverage-анализа, автогенерации тест-кейсов
из спецификаций и прочего).

## Требования

- **Python 3.11+** (код использует синтаксис `X | None`, на Python 3.9/3.10 не запустится).
  На macOS системный `python3` часто указывает на старую версию (3.9) — `make setup`
  сам найдёт `python3.11`/`python3.12`/`python3.13`, если он есть в PATH (например,
  после `brew install python@3.11`); если такого интерпретатора нет, `make setup`
  остановится с понятной ошибкой вместо непонятного краха.
- Учётки с доступом к Jira Cloud, Confluence Cloud и Anthropic API (`ANTHROPIC_API_KEY`) —
  обязательно для Triage Bugs; для Review Pull Request / Review Comment Fixes нужны
  Azure DevOps + Anthropic, для Upload Test Cases — Azure DevOps + Confluence
- Никаких внешних сервисов (Redis, Postgres, Docker) не требуется — всё работает на
  локальном venv + SQLite

## Быстрый старт

```bash
git clone <URL этого репозитория>   # или распакуйте архив
cd triage-bugs-tool-claude

make setup      # создаст venv, поставит зависимости, скопирует .env.example -> .env
make dev        # запустит приложение на http://localhost:8001
```

Откройте **http://localhost:8001** — попадёте на дашборд. Swagger-документация всех
JSON API эндпоинтов — на **http://localhost:8001/docs**. Порт **8001** (а не 8000)
выбран специально, чтобы этот проект мог работать одновременно с оригинальной
Gemini-версией на 8000.

Если `make` недоступен (например, Windows без WSL), эквивалентные команды:

```bash
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -e .
cp .env.example .env
mkdir -p data/artifacts
PYTHONPATH=$(pwd) venv/bin/uvicorn apps.app.main:app --reload --port 8001
```

## Настройка учётных данных

Есть два равнозначных способа — используйте любой (второй удобнее для тестирования
разными пользователями на одной инсталляции, так как хранится в БД, а не в файле):

### Вариант A — через `.env`

Откройте `.env` и заполните:

```
CONFLUENCE_BASE_URL=https://yourdomain.atlassian.net/wiki
CONFLUENCE_SPACE=YOURSPACE
CONFLUENCE_EMAIL=your.email@domain.com
CONFLUENCE_API_TOKEN=...

JIRA_BASE_URL=https://yourdomain.atlassian.net
JIRA_EMAIL=your.email@domain.com
JIRA_API_TOKEN=...

ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-5

# Только для Review Pull Request / Review Comment Fixes / Upload Test Cases:
AZURE_DEVOPS_ORG_URL=https://dev.azure.com/yourorg
AZURE_DEVOPS_PROJECT=YourProject
AZURE_DEVOPS_PAT=...
```

Для Jira Cloud и Confluence Cloud на одном Atlassian-домене **email и API-токен обычно
одинаковые** — можно один раз создать токен и вставить его в оба поля.

- **Jira/Confluence API token**: https://id.atlassian.com/manage-profile/security/api-tokens
  → «Create API token».
- **Anthropic API key**: `console.anthropic.com` → Settings → API Keys. Это отдельный продукт
  от подписки на Claude Code — оплата по факту использования (за токены), нужна отдельная
  организация/биллинг в Anthropic Console. Подписка на Claude Code (интерактивный доступ,
  как у CLI) сама по себе ключ не выдаёт.
  `ANTHROPIC_MODEL` по умолчанию — `claude-sonnet-5`; если модель недоступна на вашем
  тарифе, `Test Connection` вернёт понятную ошибку (`not_found_error` или `429` с описанием
  лимита) — смотрите логи прогона.
- **Azure DevOps PAT**: User Settings → Personal Access Tokens → New Token. Права зависят
  от того, какими workflow вы пользуетесь:
  - Review Pull Request / Review Comment Fixes — **Code: Read & Write** (чтение diff'а PR,
    тредов комментариев, постинг комментариев/ответов).
  - Upload Test Cases — **Test Management: Read & Write** (suite'ы, тест-кейсы в Test Plan)
    и **Work Items: Read & Write** (тест-кейс — это work item).
  - Проще всего один раз выдать PAT с `Full access` на все нужные workflow сразу — но
    учтите, что даже `Full access` не выдаёт прав, которых нет у самого пользователя в
    проекте (например, права полного удаления work item'ов) — отсюда режим «Delete ALL
    existing test cases» в Upload Test Cases убирает тест-кейсы из suite, а не удаляет их
    навсегда (см. раздел про Upload Test Cases ниже).

После правки `.env` перезапустите `make dev`.

### Вариант B — через веб-интерфейс

Откройте **http://localhost:8001/ui/integrations** — там карточки для каждого провайдера
(Jira, Confluence, Claude, Azure DevOps). Заполните поля, нажмите **Save**, затем
**Test Connection**, чтобы убедиться, что всё настроено верно. Значения хранятся в SQLite
зашифрованными (Fernet, ключ — `APP_ENCRYPTION_KEY` в `.env`) и никогда не отображаются
обратно в браузере. Карточку Azure DevOps можно не заполнять, если вы используете только
Triage Bugs.

Конфиг из БД (вариант B) имеет приоритет над `.env` (вариант A) для каждого провайдера.

## Как пользоваться

1. Откройте **http://localhost:8001/ui/workflows/triage_bugs/run**.
2. Задайте JQL-фильтр (по умолчанию отбирает баги в статусе `Backlog`), лимит количества,
   ID кастомных полей Severity/Impact в вашей Jira и целевой статус после триажа.
3. Оставьте чекбокс **Apply** не отмеченным и нажмите **Run Triage** — запустится dry-run:
   ничего не пишется в Jira, только показывается вердикт Claude по каждому багу
   (реальный баг или нет, severity/impact/priority, обоснование).
4. Просмотрите таблицу результатов на странице мониторинга. Для каждого бага с
   вердиктом «реальный» доступна кнопка **Apply** — применить именно этот баг, либо
   **«Apply All Real Bugs to Jira»** — применить все разом. Кнопка добавления
   комментария в Jira — опциональна.
5. ID кастомных полей Severity/Impact в вашей Jira можно найти через
   `GET /rest/api/3/issue/createmeta` вашего Jira-инстанса, либо через администрирование
   полей в Jira.

### Как найти ID кастомных полей Jira

```
curl -u "email:api_token" \
  "https://yourdomain.atlassian.net/rest/api/3/field" | jq '.[] | select(.name | test("Severity|Impact"; "i"))'
```

### Review Pull Request (код-ревью PR через Claude)

1. Откройте **http://localhost:8001/ui/workflows/review_pull_request/run**.
2. Укажите репозиторий (имя или ID) и номер PR в Azure DevOps. Поле «Project» нужно
   заполнять только если он отличается от указанного в настройках интеграции.
3. Нажмите **Run Review** — запустится dry-run: строится построчный diff последней
   итерации PR, Claude ищет баги/несостыковки/проблемы качества тестов и т.д.
   По умолчанию diff и описание PR анонимизируются (JWT/email/телефоны/внутренние
   URL) перед отправкой в Claude — можно отключить чекбоксом.
4. На странице мониторинга появится таблица найденных проблем (файл, строка,
   severity, комментарий). Для каждой — кнопка **Post**, либо
   **«Post All Findings to PR»**, чтобы опубликовать сразу все как комментарии
   к строкам в самом PR.

### Review Comment Fixes (проверка, реально ли исправлены комментарии)

1. Откройте **http://localhost:8001/ui/workflows/review_comment_fixes/run**.
2. Укажите репозиторий и номер PR — так же, как для Review Pull Request.
3. Нажмите **Run Verification** — инструмент найдёт треды комментариев, помеченные
   как «fixed»/«closed» либо получившие ответ автора кода, сравнит код «до» и
   «после» вокруг закомментированной строки и попросит Claude вынести вердикт:
   `fixed` / `not_fixed` / `unclear`.
4. Для тредов с вердиктом `not_fixed` доступна кнопка **Reply**, либо
   **«Reply + Reopen All Not Fixed»** — оставит предложенный Claude ответ в
   треде и переоткроет его (если он был помечен fixed/closed), чтобы автор
   увидел несостыковку.

Оба PR-workflow **всегда** сначала выполняются в режиме анализа (без изменений в Azure
DevOps) — публикация комментариев/ответов это отдельное, явное действие на странице
мониторинга запуска.

### Upload Test Cases (загрузка провалидированных тест-кейсов в Azure DevOps)

1. Откройте **http://localhost:8001/ui/workflows/upload_test_cases/run**.
2. Выберите **Test Plan** из выпадающего списка: Web (plan 15751), Mobile (plan 438)
   или API (plan 2015).
3. Укажите номер User Story (`20.1.1`, `US-20.1.1` или `AUS-7.2` для админ-панели) и
   приложите CSV-файл с уже провалидированными тест-кейсами (экспорт Azure DevOps Test Plan,
   9-10 колонок).
4. По умолчанию чекбокс **Apply** не отмечен — запуск только резолвит цепочку suite
   (root → [Админ Панель] → Epic → User Story) по Confluence-предкам страницы User Story
   и показывает превью тест-кейсов из CSV (с пометкой дубликатов по названию), ничего не
   записывая в Azure DevOps.
5. Отметьте **Apply**, чтобы реально создать тест-кейсы в резолвленном suite.
6. Если в целевом suite уже есть тест-кейсы, выберите один из двух режимов:
   - **Add new ones alongside existing** (по умолчанию) — тест-кейсы с уже существующим
     (по точному совпадению) названием пропускаются, остальные добавляются к уже
     имеющимся. Безопасно перезапускать повторно.
   - **Delete ALL existing test cases first** — перед загрузкой убирает ВСЕ существующие
     тест-кейсы из целевого suite и создаёт всё заново из CSV. Тест-кейсы при этом не
     удаляются навсегда — они только отвязываются от suite (как при ручном действии
     «Remove» в Test Plans UI), сам work item остаётся. Это намеренно: полное удаление
     work item'а требует отдельного project-level права "Delete work items", которого
     часто нет даже при PAT с полным доступом (PAT scope не расширяет реальные права
     пользователя в проекте). Требует подтверждения в браузере.
7. Раздел «Advanced options» позволяет переопределить название папки спецификаций в
   Confluence, id fallback-папки админ-панели, название группирующего suite'а «Админ
   Панель», имя Epic/US suite и целевой Azure DevOps State (по умолчанию `Ready`).

### Skipped Tests Audit (аудит skip/todo/баг-тестов в автотестах)

Только чтение — ничего не пишет ни в Jira, ни в файлы тестов.

1. Откройте **http://localhost:8001/ui/workflows/skipped_tests_audit/run**.
2. Укажите абсолютный путь к корневой папке автотестов (по умолчанию —
   `/Users/oadmin/PROJECTS/FINCA/qa-api-tests/tests`) и запустите аудит.
3. Воркфлоу рекурсивно сканирует все `*.spec.ts`, находит тесты с `.skip`, комментарием
   `todo`/`жду` или ссылкой на баг (`MB-XXXX`/`МВ-ХХХХ`), просит Claude разобраться в
   настоящей причине каждого пропуска (комментарий рядом с тестом не всегда описывает
   причину именно этого теста — Claude отличает актуальную причину от случайных
   исторических заметок) и проверяет упомянутые баги в Jira.
4. Результат разбит на категории: **баг уже закрыт в Jira** (тест пора вернуть),
   **баг ещё открыт**, **баг не найден в Jira**, **ожидание ответа/уточнения**,
   **TODO без привязки к багу** и **причина не ясна**. Артефакты рана включают
   `workflow_result.json` и человекочитаемый `skipped_tests_report.md`.

## Запуск без UI (CLI)

Каждый workflow можно запустить напрямую из терминала, без веб-интерфейса и сервера —
скрипты читают тот же `.env`, что и `make dev`. Все они по умолчанию делают только
предпросмотр/анализ (ничего не пишут во внешние системы) — добавьте `--apply`, чтобы
реально применить изменения.

```bash
# Триаж багов
make triage-bugs ARGS="--jql 'status = Backlog' --max-results 20"
make triage-bugs ARGS="--apply --add-comment"

# Ревью Pull Request через Claude
make review-pr ARGS="--repo my-repo --pr 1234"
make review-pr ARGS="--repo my-repo --pr 1234 --apply"

# Проверка, действительно ли исправлены комментарии в PR
make review-comment-fixes ARGS="--repo my-repo --pr 1234"
make review-comment-fixes ARGS="--repo my-repo --pr 1234 --apply"

# Загрузка тест-кейсов в Azure DevOps (--plan: web / mobile / api)
make upload-test-cases ARGS="--us 20.1.1 --plan web --csv path/to/cases.csv"
make upload-test-cases ARGS="--us 20.1.1 --plan web --csv path/to/cases.csv --apply"
make upload-test-cases ARGS="--us 20.1.1 --plan web --csv path/to/cases.csv --apply --replace-existing"

# Аудит skip/todo/баг-тестов в автотестах (только чтение)
make audit-skipped-tests
make audit-skipped-tests ARGS="--tests-root /path/to/tests"
```

Полный список опций каждого скрипта — через `--help`, например:
`make triage-bugs ARGS="--help"`.

Без `make` — то же самое напрямую через venv:

```bash
PYTHONPATH=$(pwd) venv/bin/python scripts/triage_bugs.py --help
PYTHONPATH=$(pwd) venv/bin/python scripts/review_pull_request.py --repo my-repo --pr 1234
PYTHONPATH=$(pwd) venv/bin/python scripts/review_comment_fixes.py --repo my-repo --pr 1234
PYTHONPATH=$(pwd) venv/bin/python scripts/upload_test_cases.py --us 20.1.1 --plan web --csv path/to/cases.csv
PYTHONPATH=$(pwd) venv/bin/python scripts/audit_skipped_tests.py --tests-root /path/to/tests
```

Результат каждого запуска сохраняется в `scripts/data/*.json` (путь можно переопределить
через `--output`) — независимо от истории запусков в UI (`/ui/runs`), так как CLI-скрипты
не используют базу данных приложения.

## Структура проекта

```
apps/app/
  main.py          — точка входа FastAPI, регистрация роутеров и клиентов интеграций
  config.py        — настройки (.env)
  database.py      — модели SQLAlchemy (SQLite, таблицы создаются автоматически при старте)
  workflows.py     — исполнитель workflow (фоновый поток, без Celery/Redis)
  api/             — JSON REST: /api/integrations, /api/workflows, /api/runs, /api/artifacts
  ui/              — серверный HTML-интерфейс: dashboard, workflows, runs, integrations
packages/
  common/          — общие Pydantic-модели, логирование, шифрование секретов
  integrations/    — клиенты Jira, Confluence, Claude, Azure DevOps (Anthropic SDK, httpx)
  workflows/triage/ — логика триажа: поиск US в Confluence, оценка Claude, апдейт Jira
  workflows/review/ — логика PR-ревью и проверки фиксов через Claude + анонимизация (anonymize.py)
  workflows/upload_test_cases/ — резолв suite-цепочки по Confluence-предкам US и загрузка CSV в Azure DevOps
  workflows/skipped_tests/ — сканер *.spec.ts на skip/todo/баг-маркеры + классификация Claude + проверка Jira
scripts/           — CLI-обёртки над теми же workflow'ами для запуска без UI (см. "Запуск без UI (CLI)")
```

## Отличия от полного Trace2Quality

Это упрощённый, самодостаточный срез — вот что сознательно убрано:

- Coverage-анализ, автогенерация тест-кейсов из Confluence-спецификаций через Claude,
  CSV Fixer, поиск orphan test cases и переназначение test case work item'ов — Azure DevOps
  здесь используется только под три конкретных workflow (чтение diff'а/тредов PR и постинг
  комментариев/ответов; резолв suite-цепочки и создание/уборка тест-кейсов в Test Plan).
- Celery/Redis — фоновые задачи выполняются в обычном Python-потоке в рамках процесса
  приложения (для одного пользователя этого достаточно).
- Alembic-миграции — таблицы SQLite создаются автоматически при старте приложения
  (`Base.metadata.create_all`), отдельный шаг миграции не нужен.
- Composition/scheduling/webhooks — многошаговые цепочки workflow и cron-расписания.
- Файловый кэш результатов Claude между dry-run и apply (как в оригинальных
  `scripts/review_pull_request.py` / `scripts/review_comment_fixes.py`) — здесь вместо
  него результат каждого запуска один раз сохраняется как артефакт в БД, и «Apply»
  читает его оттуда же, так что повторного запроса к Claude между dry-run и apply
  никогда не происходит.

Если нужен полный функционал (coverage, генерация тест-кейсов из Confluence и т.д.) —
обращайтесь к полному репозиторию Trace2Quality.

## Безопасность

- Все секреты (API-токены, ключи) хранятся в БД зашифрованными через Fernet
  (`APP_ENCRYPTION_KEY` в `.env`). **Установите свой ключ** перед реальным использованием —
  сгенерировать можно так:
  ```bash
  venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- Не коммитьте `.env` и файл `data/triage_bugs_claude.db` (они уже в `.gitignore`) — там могут
  оказаться реальные токены и данные багов.
- Apply-действия (запись severity/impact/priority и переход статуса в Jira; постинг
  комментариев/ответов и переоткрытие тредов в Azure DevOps) выполняются под учёткой,
  чей API-токен/PAT указан в настройках соответствующей интеграции — Jira и Azure DevOps
  атрибутируют изменения именно этому аккаунту, отдельного «post as» механизма нет.
