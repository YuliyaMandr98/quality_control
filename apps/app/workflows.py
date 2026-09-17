"""Workflow execution engine (triage_bugs only)."""

import asyncio
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from apps.app.config import get_settings
from apps.app.database import (
    ArtifactModel,
    IntegrationConfigModel,
    WorkflowRunLogModel,
    WorkflowRunModel,
    get_session_factory,
)
from packages.common import IntegrationType, SecretEncryption, get_logger
from packages.integrations import integration_registry
from packages.workflows import (
    bug_backlog_audit,
    review,
    review_test_cases,
    skipped_tests,
    triage,
    uat_bug_test_cases,
    upload_test_cases,
)

logger = get_logger(__name__)

# In-memory cooperative-cancellation registry: run_id -> Event. Workflows are
# plain background threads (no Celery/Redis), so a run can't be killed
# outright - each workflow function checks `should_cancel_fn()` at safe
# checkpoints (between items in a loop, between major sequential steps) and
# stops itself, returning {"status": "canceled", ...}. Lost on server restart,
# which is fine: the background thread that owned it is gone too by then.
_cancel_events: dict[str, threading.Event] = {}


def request_cancel(run_id: str) -> bool:
    """Signal a run to stop at its next safe checkpoint. Returns False if this
    process has no tracked event for run_id (already finished, or the server
    restarted after the run started - the thread is gone either way)."""
    event = _cancel_events.get(run_id)
    if event is None:
        return False
    event.set()
    return True


def _make_should_cancel(run_id: str):
    def _should_cancel() -> bool:
        event = _cancel_events.get(run_id)
        return bool(event and event.is_set())

    return _should_cancel


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_config_from_record(record: IntegrationConfigModel) -> dict:
    enc = SecretEncryption(get_settings().app_encryption_key)
    try:
        return json.loads(enc.decrypt(record.config_encrypted).replace("'", '"'))
    except Exception:
        try:
            import ast

            return ast.literal_eval(enc.decrypt(record.config_encrypted))
        except Exception:
            return {}


def _resolve_integration_config(session, provider_type: IntegrationType) -> dict:
    """Resolve integration config: DB-stored config takes precedence, falling back to .env."""
    record = (
        session.query(IntegrationConfigModel)
        .filter(IntegrationConfigModel.type == provider_type)
        .first()
    )
    loaded: dict = {}
    if record:
        loaded = _load_config_from_record(record) or {}

    settings = get_settings()
    if provider_type == IntegrationType.CONFLUENCE:
        if loaded:
            return {
                "base_url": loaded.get("base_url") or settings.confluence_base_url,
                "space": loaded.get("space") or settings.confluence_space,
                "email": loaded.get("email") or settings.confluence_email,
                "api_token": loaded.get("api_token") or settings.confluence_api_token,
            }
        return {
            "base_url": settings.confluence_base_url,
            "space": settings.confluence_space,
            "email": settings.confluence_email,
            "api_token": settings.confluence_api_token,
        }
    if provider_type == IntegrationType.JIRA:
        if loaded:
            return {
                "base_url": loaded.get("base_url") or settings.jira_base_url,
                "email": loaded.get("email") or settings.jira_email,
                "api_token": loaded.get("api_token") or settings.jira_api_token,
            }
        return {
            "base_url": settings.jira_base_url,
            "email": settings.jira_email,
            "api_token": settings.jira_api_token,
        }
    if provider_type == IntegrationType.ANTHROPIC:
        if loaded:
            return {
                "api_key": loaded.get("api_key") or settings.anthropic_api_key,
                "model": loaded.get("model") or settings.anthropic_model,
            }
        return {
            "api_key": settings.anthropic_api_key,
            "model": settings.anthropic_model,
        }
    if provider_type == IntegrationType.AZURE_DEVOPS:
        if loaded:
            return {
                "org_url": loaded.get("org_url") or settings.azure_devops_org_url,
                "project": loaded.get("project") or settings.azure_devops_project,
                "pat": loaded.get("pat") or settings.azure_devops_pat,
            }
        return {
            "org_url": settings.azure_devops_org_url,
            "project": settings.azure_devops_project,
            "pat": settings.azure_devops_pat,
        }
    return {}


def _write_artifact(run_id: str, filename: str, payload, content_type: str) -> tuple[str, int]:
    settings = get_settings()
    run_dir = Path(settings.artifact_storage_path) / "results" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    file_path = run_dir / filename

    if content_type == "application/json":
        data = json.dumps(payload, indent=2, ensure_ascii=True)
    else:
        data = str(payload)

    file_path.write_text(data, encoding="utf-8")
    rel_storage_path = str(Path("results") / run_id / filename)
    return rel_storage_path, len(data.encode("utf-8"))


def _persist_artifact(session, run_id: str, filename: str, payload, content_type: str = "application/json"):
    storage_path, size_bytes = _write_artifact(run_id, filename, payload, content_type)
    session.add(
        ArtifactModel(
            id=str(uuid4()),
            run_id=run_id,
            filename=filename,
            content_type=content_type,
            size_bytes=str(size_bytes),
            storage_path=storage_path,
        )
    )
    session.commit()


def enqueue_workflow(run_id: str, workflow_key: str) -> dict[str, str]:
    """Run the workflow in a local background thread (no Celery/Redis required)."""
    _cancel_events[run_id] = threading.Event()
    thread = threading.Thread(target=run_workflow, args=(run_id, workflow_key), daemon=True)
    thread.start()
    return {"queue": "local-thread", "task_id": thread.name}


def run_workflow(run_id: str, workflow_key: str):
    """Execute a workflow synchronously."""
    settings = get_settings()
    SessionLocal = get_session_factory(settings.database_url)
    session = SessionLocal()
    run = None

    def log_step(level: str, message: str, correlation_id: str | None = None):
        """Log to database and console."""
        log_entry = WorkflowRunLogModel(
            id=str(uuid4()),
            run_id=run_id,
            level=level,
            message=message,
            timestamp=_utc_now(),
            correlation_id=correlation_id,
        )
        session.add(log_entry)
        session.commit()

        # Also print to stderr so it appears in terminal
        timestamp = _utc_now().isoformat()
        prefix = f"[{timestamp}] [{workflow_key}] [{level}]"
        print(f"{prefix} {message}", file=sys.stderr, flush=True)

    try:
        run = session.query(WorkflowRunModel).filter(WorkflowRunModel.id == run_id).first()
        if not run:
            logger.error(f"Run not found: {run_id}")
            return

        correlation_id = str(run_id)
        should_cancel = _make_should_cancel(run_id)

        run.status = "running"
        run.started_at = _utc_now()
        session.commit()

        log_step("INFO", f"Workflow {workflow_key} started", correlation_id=correlation_id)
        logger.info(f"Starting workflow: {workflow_key} (run_id={run_id})")

        # Parse parameters
        params = (
            run.parameters
            if isinstance(run.parameters, dict)
            else json.loads(run.parameters or "{}")
        )
        workflow_result = {}
        _persist_artifact(session, run_id, "parameters.json", params)

        # Execute workflow based on key
        if workflow_key == "triage_bugs":
            log_step("INFO", "Triaging bug tickets", correlation_id=correlation_id)

            jira_client = integration_registry.get_client(
                IntegrationType.JIRA,
                _resolve_integration_config(session, IntegrationType.JIRA),
            )
            confluence_client = integration_registry.get_client(
                IntegrationType.CONFLUENCE,
                _resolve_integration_config(session, IntegrationType.CONFLUENCE),
            )
            anthropic_client = integration_registry.get_client(
                IntegrationType.ANTHROPIC,
                _resolve_integration_config(session, IntegrationType.ANTHROPIC),
            )

            result = asyncio.run(
                triage.run_triage_bugs_workflow(
                    jira_client=jira_client,
                    confluence_client=confluence_client,
                    llm_client=anthropic_client,
                    jql=params.get("jql", triage.DEFAULT_BUG_JQL),
                    max_results=int(params.get("max_results", 50)),
                    apply=bool(params.get("apply", False)),
                    add_comment=bool(params.get("add_comment", False)),
                    severity_field_id=str(params.get("severity_field_id", triage.DEFAULT_SEVERITY_FIELD_ID)),
                    impact_field_id=str(params.get("impact_field_id", triage.DEFAULT_IMPACT_FIELD_ID)),
                    target_status=str(params.get("target_status", triage.DEFAULT_TARGET_STATUS)),
                    batch_delay_seconds=int(params.get("batch_delay_seconds", 10)),
                    correlation_id=correlation_id,
                    log_fn=lambda level, message: log_step(level, message, correlation_id=correlation_id),
                    should_cancel_fn=should_cancel,
                )
            )

            if result.get("status") == "canceled":
                run.status = "canceled"
                run.error_message = result.get("error", "Остановлено пользователем")
                run.completed_at = _utc_now()
                session.commit()
                log_step("WARNING", f"Workflow {workflow_key} canceled by user", correlation_id=correlation_id)
                return

            workflow_result = result
            triage_summary = result.get("summary", {})
            log_step(
                "INFO",
                f"Triage complete: triaged={triage_summary.get('bugs_triaged', 0)}"
                f"/{triage_summary.get('bugs_fetched', 0)}",
                correlation_id=correlation_id,
            )

        elif workflow_key in ("review_pull_request", "review_comment_fixes"):
            azure_config = _resolve_integration_config(session, IntegrationType.AZURE_DEVOPS)
            if params.get("project"):
                azure_config["project"] = params["project"]
            azure_client = integration_registry.get_client(IntegrationType.AZURE_DEVOPS, azure_config)
            anthropic_client = integration_registry.get_client(
                IntegrationType.ANTHROPIC,
                _resolve_integration_config(session, IntegrationType.ANTHROPIC),
            )
            repo = str(params.get("repo", ""))
            pr_id = int(params.get("pr_id"))
            no_anonymize = bool(params.get("no_anonymize", False))

            if workflow_key == "review_pull_request":
                log_step("INFO", f"Reviewing PR #{pr_id} in {repo}", correlation_id=correlation_id)
                result = asyncio.run(
                    review.run_review_pull_request_workflow(
                        azure_client=azure_client,
                        llm_client=anthropic_client,
                        repo=repo,
                        pr_id=pr_id,
                        no_anonymize=no_anonymize,
                        correlation_id=correlation_id,
                        log_fn=lambda level, message: log_step(level, message, correlation_id=correlation_id),
                        should_cancel_fn=should_cancel,
                    )
                )
            else:
                log_step("INFO", f"Verifying comment fixes for PR #{pr_id} in {repo}", correlation_id=correlation_id)
                result = asyncio.run(
                    review.run_review_comment_fixes_workflow(
                        azure_client=azure_client,
                        llm_client=anthropic_client,
                        repo=repo,
                        pr_id=pr_id,
                        no_anonymize=no_anonymize,
                        correlation_id=correlation_id,
                        log_fn=lambda level, message: log_step(level, message, correlation_id=correlation_id),
                        should_cancel_fn=should_cancel,
                    )
                )

            if result.get("status") == "canceled":
                run.status = "canceled"
                run.error_message = result.get("error", "Остановлено пользователем")
                run.completed_at = _utc_now()
                session.commit()
                log_step("WARNING", f"Workflow {workflow_key} canceled by user", correlation_id=correlation_id)
                return

            if result.get("status") == "failed":
                run.status = "failed"
                run.error_message = result.get("error", "Workflow failed")
                run.completed_at = _utc_now()
                session.commit()
                log_step("ERROR", f"Workflow {workflow_key} failed: {result.get('error')}", correlation_id=correlation_id)
                return

            workflow_result = result
            log_step("INFO", f"Workflow {workflow_key} finished", correlation_id=correlation_id)

        elif workflow_key == "upload_test_cases":
            azure_client = integration_registry.get_client(
                IntegrationType.AZURE_DEVOPS,
                _resolve_integration_config(session, IntegrationType.AZURE_DEVOPS),
            )
            confluence_client = integration_registry.get_client(
                IntegrationType.CONFLUENCE,
                _resolve_integration_config(session, IntegrationType.CONFLUENCE),
            )
            log_step(
                "INFO",
                f"Uploading test cases: us={params.get('us')}, plan_id={params.get('plan_id')}, "
                f"dry_run={params.get('dry_run', True)}, force={params.get('force', False)}",
                correlation_id=correlation_id,
            )
            result = asyncio.run(
                upload_test_cases.run_upload_test_cases_workflow(
                    azure_client=azure_client,
                    confluence_client=confluence_client,
                    us=str(params.get("us", "")),
                    plan_id=str(params.get("plan_id", "")),
                    csv_text=str(params.get("csv_text", "")),
                    specs_folder=str(params.get("specs_folder") or upload_test_cases.DEFAULT_SPECS_FOLDER_TITLE),
                    admin_specs_folder_id=str(params.get("admin_specs_folder_id") or upload_test_cases.DEFAULT_ADMIN_SPECS_FOLDER_ID),
                    admin_group_title=str(params.get("admin_group_title") or upload_test_cases.DEFAULT_ADMIN_GROUP_SUITE_TITLE),
                    epic_suite_name=params.get("epic_suite_name") or None,
                    us_suite_name=params.get("us_suite_name") or None,
                    state=str(params.get("state") or upload_test_cases.DEFAULT_STATE),
                    force=bool(params.get("force", False)),
                    dry_run=bool(params.get("dry_run", True)),
                    correlation_id=correlation_id,
                    log_fn=lambda level, message: log_step(level, message, correlation_id=correlation_id),
                    should_cancel_fn=should_cancel,
                )
            )

            if result.get("status") == "canceled":
                run.status = "canceled"
                run.error_message = result.get("error", "Остановлено пользователем")
                run.completed_at = _utc_now()
                session.commit()
                log_step("WARNING", f"Workflow {workflow_key} canceled by user", correlation_id=correlation_id)
                return

            if result.get("status") == "failed":
                run.status = "failed"
                run.error_message = result.get("error", "Workflow failed")
                run.completed_at = _utc_now()
                session.commit()
                log_step("ERROR", f"Workflow {workflow_key} failed: {result.get('error')}", correlation_id=correlation_id)
                return

            workflow_result = result
            log_step("INFO", f"Workflow {workflow_key} finished", correlation_id=correlation_id)

        elif workflow_key == "skipped_tests_audit":
            jira_client = integration_registry.get_client(
                IntegrationType.JIRA,
                _resolve_integration_config(session, IntegrationType.JIRA),
            )
            anthropic_client = integration_registry.get_client(
                IntegrationType.ANTHROPIC,
                _resolve_integration_config(session, IntegrationType.ANTHROPIC),
            )
            tests_root = str(params.get("tests_root") or skipped_tests.DEFAULT_TESTS_ROOT)
            log_step("INFO", f"Auditing skipped tests under {tests_root}", correlation_id=correlation_id)

            result = asyncio.run(
                skipped_tests.run_skipped_tests_audit_workflow(
                    jira_client=jira_client,
                    llm_client=anthropic_client,
                    tests_root=tests_root,
                    correlation_id=correlation_id,
                    log_fn=lambda level, message: log_step(level, message, correlation_id=correlation_id),
                    should_cancel_fn=should_cancel,
                )
            )

            if result.get("status") == "canceled":
                run.status = "canceled"
                run.error_message = result.get("error", "Остановлено пользователем")
                run.completed_at = _utc_now()
                session.commit()
                log_step("WARNING", f"Workflow {workflow_key} canceled by user", correlation_id=correlation_id)
                return

            if result.get("status") == "failed":
                run.status = "failed"
                run.error_message = result.get("error", "Workflow failed")
                run.completed_at = _utc_now()
                session.commit()
                log_step("ERROR", f"Workflow {workflow_key} failed: {result.get('error')}", correlation_id=correlation_id)
                return

            workflow_result = result
            log_step("INFO", f"Workflow {workflow_key} finished", correlation_id=correlation_id)

        elif workflow_key == "review_test_cases":
            confluence_client = integration_registry.get_client(
                IntegrationType.CONFLUENCE,
                _resolve_integration_config(session, IntegrationType.CONFLUENCE),
            )
            anthropic_client = integration_registry.get_client(
                IntegrationType.ANTHROPIC,
                _resolve_integration_config(session, IntegrationType.ANTHROPIC),
            )
            log_step(
                "INFO",
                f"Reviewing test case coverage: us={params.get('us')}, test_type={params.get('test_type')}",
                correlation_id=correlation_id,
            )

            result = asyncio.run(
                review_test_cases.run_review_test_cases_workflow(
                    confluence_client=confluence_client,
                    llm_client=anthropic_client,
                    us=str(params.get("us", "")),
                    test_type=str(params.get("test_type", "")),
                    spec_url=str(params.get("spec_url", "")),
                    tech_impl_url=params.get("tech_impl_url") or None,
                    test_cases_text=str(params.get("test_cases_text", "")),
                    correlation_id=correlation_id,
                    log_fn=lambda level, message: log_step(level, message, correlation_id=correlation_id),
                    should_cancel_fn=should_cancel,
                )
            )

            if result.get("status") == "canceled":
                run.status = "canceled"
                run.error_message = result.get("error", "Остановлено пользователем")
                run.completed_at = _utc_now()
                session.commit()
                log_step("WARNING", f"Workflow {workflow_key} canceled by user", correlation_id=correlation_id)
                return

            if result.get("status") == "failed":
                run.status = "failed"
                run.error_message = result.get("error", "Workflow failed")
                run.completed_at = _utc_now()
                session.commit()
                log_step("ERROR", f"Workflow {workflow_key} failed: {result.get('error')}", correlation_id=correlation_id)
                return

            workflow_result = result
            log_step("INFO", f"Workflow {workflow_key} finished", correlation_id=correlation_id)

        elif workflow_key == "bug_backlog_audit":
            jira_client = integration_registry.get_client(
                IntegrationType.JIRA,
                _resolve_integration_config(session, IntegrationType.JIRA),
            )
            log_step(
                "INFO",
                f"Auditing backlog bugs: jql={params.get('jql')}, max_results={params.get('max_results')}",
                correlation_id=correlation_id,
            )

            result = asyncio.run(
                bug_backlog_audit.run_bug_backlog_audit_workflow(
                    jira_client=jira_client,
                    jql=str(params.get("jql", bug_backlog_audit.DEFAULT_JQL)),
                    max_results=int(params.get("max_results", 100)),
                    correlation_id=correlation_id,
                    log_fn=lambda level, message: log_step(level, message, correlation_id=correlation_id),
                    should_cancel_fn=should_cancel,
                )
            )

            if result.get("status") == "canceled":
                run.status = "canceled"
                run.error_message = result.get("error", "Остановлено пользователем")
                run.completed_at = _utc_now()
                session.commit()
                log_step("WARNING", f"Workflow {workflow_key} canceled by user", correlation_id=correlation_id)
                return

            if result.get("status") == "failed":
                run.status = "failed"
                run.error_message = result.get("error", "Workflow failed")
                run.completed_at = _utc_now()
                session.commit()
                log_step("ERROR", f"Workflow {workflow_key} failed: {result.get('error')}", correlation_id=correlation_id)
                return

            workflow_result = result
            log_step("INFO", f"Workflow {workflow_key} finished", correlation_id=correlation_id)

        elif workflow_key == "uat_bug_test_cases":
            jira_client = integration_registry.get_client(
                IntegrationType.JIRA,
                _resolve_integration_config(session, IntegrationType.JIRA),
            )
            azure_client = integration_registry.get_client(
                IntegrationType.AZURE_DEVOPS,
                _resolve_integration_config(session, IntegrationType.AZURE_DEVOPS),
            )
            anthropic_client = integration_registry.get_client(
                IntegrationType.ANTHROPIC,
                _resolve_integration_config(session, IntegrationType.ANTHROPIC),
            )
            dry_run = bool(params.get("dry_run", True))
            log_step(
                "INFO",
                f"Генерация тест-кейсов из UAT-багов: jql={params.get('jql')}, dry_run={dry_run}",
                correlation_id=correlation_id,
            )

            result = asyncio.run(
                uat_bug_test_cases.run_uat_bug_test_cases_workflow(
                    jira_client=jira_client,
                    azure_client=azure_client,
                    llm_client=anthropic_client,
                    jql=str(params.get("jql", uat_bug_test_cases.DEFAULT_JQL)),
                    plan_id=str(params.get("plan_id", uat_bug_test_cases.DEFAULT_PLAN_ID)),
                    root_suite_id=str(params.get("root_suite_id", uat_bug_test_cases.DEFAULT_ROOT_SUITE_ID)),
                    sprint_number=int(params["sprint_number"]) if params.get("sprint_number") not in (None, "") else None,
                    max_results=int(params.get("max_results", 200)),
                    priority=str(params.get("priority", uat_bug_test_cases.DEFAULT_PRIORITY)),
                    dry_run=dry_run,
                    correlation_id=correlation_id,
                    log_fn=lambda level, message: log_step(level, message, correlation_id=correlation_id),
                    should_cancel_fn=should_cancel,
                )
            )

            if result.get("status") == "canceled":
                run.status = "canceled"
                run.error_message = result.get("error", "Остановлено пользователем")
                run.completed_at = _utc_now()
                session.commit()
                log_step("WARNING", f"Workflow {workflow_key} canceled by user", correlation_id=correlation_id)
                return

            if result.get("status") == "failed":
                run.status = "failed"
                run.error_message = result.get("error", "Workflow failed")
                run.completed_at = _utc_now()
                session.commit()
                log_step("ERROR", f"Workflow {workflow_key} failed: {result.get('error')}", correlation_id=correlation_id)
                return

            workflow_result = result
            log_step("INFO", f"Workflow {workflow_key} finished", correlation_id=correlation_id)

        else:
            log_step("WARNING", f"Unknown workflow: {workflow_key}", correlation_id=correlation_id)
            run.status = "failed"
            run.error_message = f"Unknown workflow: {workflow_key}"
            run.completed_at = _utc_now()
            session.commit()
            return

        if workflow_result:
            _persist_artifact(session, run_id, "workflow_result.json", workflow_result)
            if workflow_key == "triage_bugs":
                _persist_artifact(session, run_id, "triage_summary.json", workflow_result.get("summary", {}))
                _persist_artifact(session, run_id, "per_issue_results.json", workflow_result.get("per_issue_results", []))
            elif workflow_key == "review_pull_request":
                _persist_artifact(session, run_id, "findings.json", workflow_result.get("findings", []))
            elif workflow_key == "review_comment_fixes":
                _persist_artifact(session, run_id, "comment_fix_results.json", workflow_result.get("results", []))
            elif workflow_key == "upload_test_cases":
                _persist_artifact(session, run_id, "upload_results.json", workflow_result.get("results", []))
            elif workflow_key == "skipped_tests_audit":
                _persist_artifact(
                    session, run_id, "skipped_tests_report.md",
                    skipped_tests.render_markdown_report(workflow_result), content_type="text/markdown",
                )
            elif workflow_key == "review_test_cases":
                _persist_artifact(
                    session, run_id, "review_report.txt",
                    review_test_cases.render_text_report(workflow_result), content_type="text/plain",
                )
            elif workflow_key == "bug_backlog_audit":
                _persist_artifact(
                    session, run_id, "bug_backlog_audit_report.txt",
                    bug_backlog_audit.render_text_report(workflow_result), content_type="text/plain",
                )
            elif workflow_key == "uat_bug_test_cases":
                _persist_artifact(
                    session, run_id, "uat_bug_test_cases_report.txt",
                    uat_bug_test_cases.render_text_report(workflow_result), content_type="text/plain",
                )

        run.status = "succeeded"
        run.completed_at = _utc_now()
        session.commit()

        log_step("INFO", f"Workflow {workflow_key} completed successfully", correlation_id=correlation_id)
        logger.info(f"Workflow completed: {workflow_key} (run_id={run_id})")

    except Exception as e:
        logger.error(
            f"Workflow execution failed: {str(e)}", extra={"run_id": run_id}
        )
        if run:
            log_step("ERROR", f"Workflow failed: {str(e)}")
            run.status = "failed"
            run.error_message = str(e)
            run.completed_at = _utc_now()
            session.commit()
    finally:
        _cancel_events.pop(run_id, None)
        session.close()
