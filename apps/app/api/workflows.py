"""Workflow API endpoints (triage_bugs, review_pull_request, review_comment_fixes, upload_test_cases)"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from apps.app.config import get_settings
from apps.app.database import ArtifactModel, WorkflowRunModel, get_session_factory
from apps.app.workflows import _resolve_integration_config, enqueue_workflow
from packages.common import (
    BugBacklogAuditRunRequest,
    IntegrationType,
    ReviewCommentFixesRunRequest,
    ReviewPullRequestRunRequest,
    ReviewTestCasesRunRequest,
    SkippedTestsAuditRunRequest,
    TriageBugTicketsRunRequest,
    UatBugTestCasesRunRequest,
    WorkflowType,
    get_logger,
)
from packages.integrations import integration_registry
from packages.workflows import review as review_workflow
from packages.workflows import triage as triage_workflow
from packages.workflows import uat_bug_test_cases as uat_bug_test_cases_workflow
from packages.workflows import upload_test_cases as upload_workflow

logger = get_logger(__name__)

router = APIRouter()


def get_db(request: Request) -> Session:
    """Get database session"""
    settings = get_settings()
    SessionLocal = get_session_factory(settings.database_url)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@router.get("")
async def list_workflows() -> dict:
    """List available workflows"""
    workflows = []
    for workflow in WorkflowType:
        workflows.append({
            "key": workflow.value,
            "name": workflow.value.replace("_", " ").title(),
            "description": f"Workflow: {workflow.value}",
        })
    return {"workflows": workflows}


@router.post("/triage-bugs/runs")
async def create_triage_bugs_run(
    payload: TriageBugTicketsRunRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Create a triage_bugs run via stable JSON API contract.

    When `apply` is False (default) the workflow runs in dry-run mode: it
    evaluates every bug and reports what it would do but makes no changes to Jira.
    Set `apply: true` to actually update severity/impact fields and transition issues.
    Triage comments remain disabled unless `add_comment: true` is explicitly set.
    """
    correlation_id = getattr(request.state, "correlation_id", None)

    run_id = str(uuid4())
    accepted_params = payload.model_dump()

    run = WorkflowRunModel(
        id=run_id,
        workflow_key="triage_bugs",
        parameters=accepted_params,
        dry_run="0" if payload.apply else "1",
        status="queued",
    )
    db.add(run)
    db.commit()

    queue_info = enqueue_workflow(run_id, "triage_bugs")

    logger.info(
        "Created triage_bugs run",
        correlation_id=correlation_id,
        extra={"run_id": run_id, "apply": payload.apply, "max_results": payload.max_results},
    )

    return {
        "run_id": run_id,
        "workflow_key": "triage_bugs",
        "status": "queued",
        "accepted_parameters": accepted_params,
        "queue": queue_info.get("queue"),
        "task_id": queue_info.get("task_id"),
        "created_at": datetime.utcnow().isoformat(),
    }


# ── Selective apply for dry-run triage results ────────────────────────────────

class _ApplyIssuesRequest(BaseModel):
    run_id: str
    issue_keys: list[str] | None = None  # None / empty = apply all real-bug rows
    add_comment: bool | None = None


@router.post("/triage-bugs/apply-issues")
async def apply_triage_issues(
    payload: _ApplyIssuesRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Apply triage results (severity/impact/priority/transition) to specific Jira issues
    from an existing dry-run.  If ``issue_keys`` is omitted every row where
    ``is_real_bug=true`` is applied. Jira comments are only added when
    ``add_comment=true``.
    """
    # ── 1. Validate the source run ────────────────────────────────────────────
    run = db.query(WorkflowRunModel).filter(WorkflowRunModel.id == payload.run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {payload.run_id}")
    if run.workflow_key != "triage_bugs":
        raise HTTPException(status_code=400, detail="Run is not a triage_bugs run")

    # ── 2. Load per_issue_results artifact ────────────────────────────────────
    artifact = (
        db.query(ArtifactModel)
        .filter(ArtifactModel.run_id == payload.run_id, ArtifactModel.filename == "per_issue_results.json")
        .first()
    )
    if not artifact:
        raise HTTPException(status_code=404, detail="per_issue_results.json artifact not found for this run")

    settings = get_settings()
    artifact_path = Path(settings.artifact_storage_path) / artifact.storage_path
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact file missing from storage")

    per_issue_results: list[dict] = json.loads(artifact_path.read_text(encoding="utf-8"))

    # ── 3. Filter to requested issues ─────────────────────────────────────────
    requested_keys: set[str] = set(payload.issue_keys or [])
    rows_to_apply = [
        row for row in per_issue_results
        if row.get("is_real_bug")
        and (not requested_keys or str(row.get("key")) in requested_keys)
    ]

    if not rows_to_apply:
        return {"applied": [], "skipped": [], "errors": [], "message": "No matching real-bug rows to apply"}

    # ── 4. Recover workflow parameters from the source run ────────────────────
    params: dict = (
        run.parameters if isinstance(run.parameters, dict)
        else json.loads(run.parameters or "{}")
    )
    severity_field_id = str(params.get("severity_field_id") or triage_workflow.DEFAULT_SEVERITY_FIELD_ID)
    impact_field_id = str(params.get("impact_field_id") or triage_workflow.DEFAULT_IMPACT_FIELD_ID)
    target_status = str(params.get("target_status") or triage_workflow.DEFAULT_TARGET_STATUS)
    add_comment = (
        payload.add_comment
        if payload.add_comment is not None
        else bool(params.get("add_comment", False))
    )

    # ── 5. Build Jira client ───────────────────────────────────────────────────
    jira_client = integration_registry.get_client(
        IntegrationType.JIRA, _resolve_integration_config(db, IntegrationType.JIRA)
    )

    # ── 6. Apply each issue ───────────────────────────────────────────────────
    applied: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []

    async def _apply_one(row: dict) -> None:
        key = str(row.get("key") or "")
        severity = str(row.get("severity") or "Major")
        impact = str(row.get("impact") or "Moderate / Limited")
        priority = str(row.get("priority") or "Medium")
        reasoning = str(row.get("reasoning") or "")

        try:
            await jira_client.update_issue(
                key,
                {
                    "fields": {
                        severity_field_id: {"value": severity},
                        impact_field_id: {"value": impact},
                        "priority": {"name": priority},
                    }
                },
            )
            await jira_client.transition_issue(key, target_status)
            if add_comment:
                comment_text = (
                    f"Triaged by automated system:\n"
                    f"- Severity: {severity}\n"
                    f"- Impact: {impact}\n"
                    f"- Priority: {priority}\n"
                    f"- Reason: {reasoning}"
                )
                await jira_client.add_comment(key, comment_text)
            applied.append(key)
        except Exception as exc:
            errors.append({"key": key, "error": str(exc)})

    try:
        await asyncio.gather(*[_apply_one(row) for row in rows_to_apply])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Apply failed: {exc}")

    not_real = [
        str(row.get("key")) for row in per_issue_results
        if not row.get("is_real_bug") and (not requested_keys or str(row.get("key")) in requested_keys)
    ]
    skipped.extend(not_real)

    return {
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "applied_count": len(applied),
        "error_count": len(errors),
        "add_comment": bool(add_comment),
    }


# ── Pull Request code review (Claude) ─────────────────────────────────────────


@router.post("/review-pull-request/runs")
async def create_review_pull_request_run(
    payload: ReviewPullRequestRunRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Create a review_pull_request run. Always a dry-run: findings are analyzed and
    persisted as an artifact but never posted to Azure DevOps. Use
    /review-pull-request/apply-comments to publish a run's findings.
    """
    correlation_id = getattr(request.state, "correlation_id", None)

    run_id = str(uuid4())
    accepted_params = payload.model_dump()

    run = WorkflowRunModel(
        id=run_id,
        workflow_key="review_pull_request",
        parameters=accepted_params,
        dry_run="1",
        status="queued",
    )
    db.add(run)
    db.commit()

    queue_info = enqueue_workflow(run_id, "review_pull_request")

    logger.info(
        "Created review_pull_request run",
        correlation_id=correlation_id,
        extra={"run_id": run_id, "repo": payload.repo, "pr_id": payload.pr_id},
    )

    return {
        "run_id": run_id,
        "workflow_key": "review_pull_request",
        "status": "queued",
        "accepted_parameters": accepted_params,
        "queue": queue_info.get("queue"),
        "task_id": queue_info.get("task_id"),
        "created_at": datetime.utcnow().isoformat(),
    }


class _ApplyReviewCommentsRequest(BaseModel):
    run_id: str
    findings: list[dict] | None = None  # None = post every finding from the run


@router.post("/review-pull-request/apply-comments")
async def apply_review_comments(
    payload: _ApplyReviewCommentsRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Post findings from an existing review_pull_request dry-run as PR line comments."""
    run = db.query(WorkflowRunModel).filter(WorkflowRunModel.id == payload.run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {payload.run_id}")
    if run.workflow_key != "review_pull_request":
        raise HTTPException(status_code=400, detail="Run is not a review_pull_request run")

    result_artifact = (
        db.query(ArtifactModel)
        .filter(ArtifactModel.run_id == payload.run_id, ArtifactModel.filename == "workflow_result.json")
        .first()
    )
    if not result_artifact:
        raise HTTPException(status_code=404, detail="workflow_result.json artifact not found for this run")

    settings = get_settings()
    artifact_path = Path(settings.artifact_storage_path) / result_artifact.storage_path
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact file missing from storage")

    workflow_result: dict = json.loads(artifact_path.read_text(encoding="utf-8"))
    files = workflow_result.get("files", [])
    findings = payload.findings if payload.findings is not None else workflow_result.get("findings", [])
    if not findings:
        return {"posted": [], "errors": [], "posted_count": 0, "error_count": 0, "message": "No findings to post"}

    params: dict = run.parameters if isinstance(run.parameters, dict) else json.loads(run.parameters or "{}")
    azure_config = _resolve_integration_config(db, IntegrationType.AZURE_DEVOPS)
    if params.get("project"):
        azure_config["project"] = params["project"]
    azure_client = integration_registry.get_client(IntegrationType.AZURE_DEVOPS, azure_config)

    try:
        result = await review_workflow.post_review_comments(
            azure_client, str(params.get("repo")), int(params.get("pr_id")), findings, files
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Apply failed: {exc}")

    return result


# ── Comment-fix verification (Claude) ─────────────────────────────────────────


@router.post("/review-comment-fixes/runs")
async def create_review_comment_fixes_run(
    payload: ReviewCommentFixesRunRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Create a review_comment_fixes run. Always a dry-run: verdicts are analyzed and
    persisted as an artifact but no reply is posted and no thread is reopened. Use
    /review-comment-fixes/apply-replies to act on a run's "not_fixed" threads.
    """
    correlation_id = getattr(request.state, "correlation_id", None)

    run_id = str(uuid4())
    accepted_params = payload.model_dump()

    run = WorkflowRunModel(
        id=run_id,
        workflow_key="review_comment_fixes",
        parameters=accepted_params,
        dry_run="1",
        status="queued",
    )
    db.add(run)
    db.commit()

    queue_info = enqueue_workflow(run_id, "review_comment_fixes")

    logger.info(
        "Created review_comment_fixes run",
        correlation_id=correlation_id,
        extra={"run_id": run_id, "repo": payload.repo, "pr_id": payload.pr_id},
    )

    return {
        "run_id": run_id,
        "workflow_key": "review_comment_fixes",
        "status": "queued",
        "accepted_parameters": accepted_params,
        "queue": queue_info.get("queue"),
        "task_id": queue_info.get("task_id"),
        "created_at": datetime.utcnow().isoformat(),
    }


class _ApplyCommentFixRequest(BaseModel):
    run_id: str
    thread_ids: list[int] | None = None  # None = act on every not_fixed thread from the run


@router.post("/review-comment-fixes/apply-replies")
async def apply_review_comment_fixes(
    payload: _ApplyCommentFixRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Reply into (and reopen) threads from an existing review_comment_fixes dry-run
    that Claude flagged as not actually fixed.
    """
    run = db.query(WorkflowRunModel).filter(WorkflowRunModel.id == payload.run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {payload.run_id}")
    if run.workflow_key != "review_comment_fixes":
        raise HTTPException(status_code=400, detail="Run is not a review_comment_fixes run")

    result_artifact = (
        db.query(ArtifactModel)
        .filter(ArtifactModel.run_id == payload.run_id, ArtifactModel.filename == "workflow_result.json")
        .first()
    )
    if not result_artifact:
        raise HTTPException(status_code=404, detail="workflow_result.json artifact not found for this run")

    settings = get_settings()
    artifact_path = Path(settings.artifact_storage_path) / result_artifact.storage_path
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact file missing from storage")

    workflow_result: dict = json.loads(artifact_path.read_text(encoding="utf-8"))
    contexts = workflow_result.get("contexts", [])
    results = workflow_result.get("results", [])

    params: dict = run.parameters if isinstance(run.parameters, dict) else json.loads(run.parameters or "{}")
    azure_config = _resolve_integration_config(db, IntegrationType.AZURE_DEVOPS)
    if params.get("project"):
        azure_config["project"] = params["project"]
    azure_client = integration_registry.get_client(IntegrationType.AZURE_DEVOPS, azure_config)

    try:
        result = await review_workflow.apply_comment_fix_results(
            azure_client, str(params.get("repo")), int(params.get("pr_id")),
            contexts, results, thread_ids=payload.thread_ids,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Apply failed: {exc}")

    return result


# ── Upload reviewed test cases into Azure DevOps ──────────────────────────────


@router.post("/upload-test-cases/runs")
async def create_upload_test_cases_run(
    request: Request,
    db: Session = Depends(get_db),
    us: str = Form(...),
    plan_id: str = Form(...),
    csv_file: UploadFile = File(...),
    specs_folder: str = Form(upload_workflow.DEFAULT_SPECS_FOLDER_TITLE),
    admin_specs_folder_id: str = Form(upload_workflow.DEFAULT_ADMIN_SPECS_FOLDER_ID),
    admin_group_title: str = Form(upload_workflow.DEFAULT_ADMIN_GROUP_SUITE_TITLE),
    epic_suite_name: str = Form(""),
    us_suite_name: str = Form(""),
    state: str = Form(upload_workflow.DEFAULT_STATE),
    force: bool = Form(False),
    dry_run: bool = Form(True),
) -> dict:
    """Create an upload_test_cases run from a reviewed test case CSV.

    `plan_id` must be one of the configured Test Plan ids (see
    GET /api/workflows/upload-test-cases/plans). Defaults to a dry-run that
    resolves the suite chain and previews the parsed CSV without writing
    anything to Azure DevOps.
    """
    correlation_id = getattr(request.state, "correlation_id", None)

    valid_plan_ids = {p["plan_id"] for p in upload_workflow.TEST_PLANS.values()}
    if plan_id not in valid_plan_ids:
        raise HTTPException(status_code=400, detail=f"Unknown plan_id: {plan_id}. Valid: {sorted(valid_plan_ids)}")

    csv_bytes = await csv_file.read()
    try:
        csv_text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV file must be UTF-8 encoded")

    run_id = str(uuid4())
    accepted_params = {
        "us": us,
        "plan_id": plan_id,
        "csv_text": csv_text,
        "specs_folder": specs_folder,
        "admin_specs_folder_id": admin_specs_folder_id,
        "admin_group_title": admin_group_title,
        "epic_suite_name": epic_suite_name or None,
        "us_suite_name": us_suite_name or None,
        "state": state,
        "force": force,
        "dry_run": dry_run,
    }

    run = WorkflowRunModel(
        id=run_id,
        workflow_key="upload_test_cases",
        parameters=accepted_params,
        dry_run="1" if dry_run else "0",
        status="queued",
    )
    db.add(run)
    db.commit()

    queue_info = enqueue_workflow(run_id, "upload_test_cases")

    logger.info(
        "Created upload_test_cases run",
        correlation_id=correlation_id,
        extra={"run_id": run_id, "us": us, "plan_id": plan_id, "dry_run": dry_run},
    )

    return {
        "run_id": run_id,
        "workflow_key": "upload_test_cases",
        "status": "queued",
        "accepted_parameters": {k: v for k, v in accepted_params.items() if k != "csv_text"},
        "queue": queue_info.get("queue"),
        "task_id": queue_info.get("task_id"),
        "created_at": datetime.utcnow().isoformat(),
    }


@router.get("/upload-test-cases/plans")
async def list_upload_test_case_plans() -> dict:
    """List the Test Plans available for upload (key, plan_id, label)."""
    return {"plans": [{"key": k, **v} for k, v in upload_workflow.TEST_PLANS.items()]}


# ── Skipped-tests audit (read-only: Jira + Claude) ────────────────────────────


@router.post("/skipped-tests-audit/runs")
async def create_skipped_tests_audit_run(
    payload: SkippedTestsAuditRunRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Create a skipped_tests_audit run. Read-only: scans the test repo and checks Jira,
    never writes to Jira or the test files.
    """
    correlation_id = getattr(request.state, "correlation_id", None)

    run_id = str(uuid4())
    accepted_params = payload.model_dump()

    run = WorkflowRunModel(
        id=run_id,
        workflow_key="skipped_tests_audit",
        parameters=accepted_params,
        dry_run="1",
        status="queued",
    )
    db.add(run)
    db.commit()

    queue_info = enqueue_workflow(run_id, "skipped_tests_audit")

    logger.info(
        "Created skipped_tests_audit run",
        correlation_id=correlation_id,
        extra={"run_id": run_id, "tests_root": payload.tests_root},
    )

    return {
        "run_id": run_id,
        "workflow_key": "skipped_tests_audit",
        "status": "queued",
        "accepted_parameters": accepted_params,
        "queue": queue_info.get("queue"),
        "task_id": queue_info.get("task_id"),
        "created_at": datetime.utcnow().isoformat(),
    }


# ── Test case coverage review (Confluence + Claude) ───────────────────────────


@router.post("/review-test-cases/runs")
async def create_review_test_cases_run(
    request: Request,
    db: Session = Depends(get_db),
    us: str = Form(...),
    test_type: str = Form(...),
    spec_url: str = Form(...),
    tech_impl_url: str = Form(""),
    test_cases_file: UploadFile = File(...),
) -> dict:
    """Create a review_test_cases run. Read-only: fetches the pasted Confluence spec
    (and, for API test cases, the pasted technical-implementation doc) and asks Claude
    to review the attached test cases file for coverage completeness. Never writes anywhere.
    """
    correlation_id = getattr(request.state, "correlation_id", None)

    file_bytes = await test_cases_file.read()
    try:
        test_cases_text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Файл с тест-кейсами должен быть в кодировке UTF-8")

    try:
        validated = ReviewTestCasesRunRequest(
            us=us,
            test_type=test_type,
            spec_url=spec_url,
            tech_impl_url=tech_impl_url or None,
            test_cases_text=test_cases_text,
        )
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    run_id = str(uuid4())
    accepted_params = validated.model_dump()

    run = WorkflowRunModel(
        id=run_id,
        workflow_key="review_test_cases",
        parameters=accepted_params,
        dry_run="1",
        status="queued",
    )
    db.add(run)
    db.commit()

    queue_info = enqueue_workflow(run_id, "review_test_cases")

    logger.info(
        "Created review_test_cases run",
        correlation_id=correlation_id,
        extra={"run_id": run_id, "us": validated.us, "test_type": validated.test_type},
    )

    return {
        "run_id": run_id,
        "workflow_key": "review_test_cases",
        "status": "queued",
        "accepted_parameters": {k: v for k, v in accepted_params.items() if k != "test_cases_text"},
        "queue": queue_info.get("queue"),
        "task_id": queue_info.get("task_id"),
        "created_at": datetime.utcnow().isoformat(),
    }


# ── Backlog bug field/link completeness audit (Jira only, read-only) ──────────


@router.post("/bug-backlog-audit/runs")
async def create_bug_backlog_audit_run(
    payload: BugBacklogAuditRunRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Create a bug_backlog_audit run. Read-only: checks each matched bug for
    required fields (Фаза, Метки, Компоненты, ENV (Полигон), Team) and required
    "is Bug for" links (a User Story + a QA task). Never writes to Jira.
    """
    correlation_id = getattr(request.state, "correlation_id", None)

    run_id = str(uuid4())
    accepted_params = payload.model_dump()

    run = WorkflowRunModel(
        id=run_id,
        workflow_key="bug_backlog_audit",
        parameters=accepted_params,
        dry_run="1",
        status="queued",
    )
    db.add(run)
    db.commit()

    queue_info = enqueue_workflow(run_id, "bug_backlog_audit")

    logger.info(
        "Created bug_backlog_audit run",
        correlation_id=correlation_id,
        extra={"run_id": run_id, "jql": payload.jql, "max_results": payload.max_results},
    )

    return {
        "run_id": run_id,
        "workflow_key": "bug_backlog_audit",
        "status": "queued",
        "accepted_parameters": accepted_params,
        "queue": queue_info.get("queue"),
        "task_id": queue_info.get("task_id"),
        "created_at": datetime.utcnow().isoformat(),
    }


# ── UAT bugs (from customer) → Azure DevOps test cases ────────────────────


@router.get("/uat-bug-test-cases/sprints")
async def list_uat_bug_test_cases_sprints(
    request: Request,
    plan_id: str = uat_bug_test_cases_workflow.DEFAULT_PLAN_ID,
    root_suite_id: str = uat_bug_test_cases_workflow.DEFAULT_ROOT_SUITE_ID,
    db: Session = Depends(get_db),
) -> dict:
    """List 'Sprint N' folders (newest first) for the sprint dropdown in the
    UI, each flagged with whether it already has a 'UAT Bugs' suite ready to
    receive test cases.
    """
    azure_client = integration_registry.get_client(
        IntegrationType.AZURE_DEVOPS,
        _resolve_integration_config(db, IntegrationType.AZURE_DEVOPS),
    )
    sprints = await uat_bug_test_cases_workflow.list_available_sprints(azure_client, plan_id, root_suite_id)
    return {"sprints": sprints}


@router.post("/uat-bug-test-cases/runs")
async def create_uat_bug_test_cases_run(
    payload: UatBugTestCasesRunRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Create a uat_bug_test_cases run. Only bugs in status 'PASSED' are
    considered, skips bugs that already have a test case in the target suite
    (matched by 'MB-XXXX' in the title), and defaults to dry_run=True — a
    preview of what would be created, with no writes to Azure DevOps until
    dry_run=False. Use /uat-bug-test-cases/apply-cases to create test cases
    from an already-previewed dry run without re-running it.
    """
    correlation_id = getattr(request.state, "correlation_id", None)

    run_id = str(uuid4())
    accepted_params = payload.model_dump()

    run = WorkflowRunModel(
        id=run_id,
        workflow_key="uat_bug_test_cases",
        parameters=accepted_params,
        dry_run="1" if payload.dry_run else "0",
        status="queued",
    )
    db.add(run)
    db.commit()

    queue_info = enqueue_workflow(run_id, "uat_bug_test_cases")

    logger.info(
        "Created uat_bug_test_cases run",
        correlation_id=correlation_id,
        extra={"run_id": run_id, "jql": payload.jql, "dry_run": payload.dry_run},
    )

    return {
        "run_id": run_id,
        "workflow_key": "uat_bug_test_cases",
        "status": "queued",
        "accepted_parameters": accepted_params,
        "queue": queue_info.get("queue"),
        "task_id": queue_info.get("task_id"),
        "created_at": datetime.utcnow().isoformat(),
    }


class _UatApplyCasesRequest(BaseModel):
    run_id: str
    bug_keys: list[str] | None = None  # None / empty = apply all not-yet-created rows


@router.post("/uat-bug-test-cases/apply-cases")
async def apply_uat_bug_test_cases(
    payload: _UatApplyCasesRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Create Azure DevOps test cases for rows from an already-previewed
    uat_bug_test_cases dry run, without re-fetching Jira or re-running LLM
    extraction. If ``bug_keys`` is omitted every row not yet successfully
    created is applied. Safe to call repeatedly - rows with a successful
    ``result`` are skipped, so re-clicking "Apply All" never creates duplicates.
    """
    run = db.query(WorkflowRunModel).filter(WorkflowRunModel.id == payload.run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {payload.run_id}")
    if run.workflow_key != "uat_bug_test_cases":
        raise HTTPException(status_code=400, detail="Run is not a uat_bug_test_cases run")

    artifact = (
        db.query(ArtifactModel)
        .filter(ArtifactModel.run_id == payload.run_id, ArtifactModel.filename == "workflow_result.json")
        .first()
    )
    if not artifact:
        raise HTTPException(status_code=404, detail="workflow_result.json artifact not found for this run")

    settings = get_settings()
    artifact_path = Path(settings.artifact_storage_path) / artifact.storage_path
    if not artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact file missing from storage")

    workflow_result: dict = json.loads(artifact_path.read_text(encoding="utf-8"))
    summary: dict = workflow_result.get("summary") or {}
    target_suite: dict = summary.get("target_suite") or {}
    suite_id = str(target_suite.get("suite_id") or "")
    if not suite_id:
        raise HTTPException(status_code=400, detail="Целевой suite не найден в результате рана")

    params: dict = run.parameters if isinstance(run.parameters, dict) else json.loads(run.parameters or "{}")
    plan_id = str(params.get("plan_id") or uat_bug_test_cases_workflow.DEFAULT_PLAN_ID)
    priority = str(params.get("priority") or uat_bug_test_cases_workflow.DEFAULT_PRIORITY)

    requested_keys: set[str] = set(payload.bug_keys or [])
    rows: list[dict] = workflow_result.get("results") or []
    rows_to_apply = [
        row for row in rows
        if (not requested_keys or str(row.get("key")) in requested_keys)
        and not (isinstance(row.get("result"), dict) and row["result"].get("success"))
    ]

    if not rows_to_apply:
        return {"applied": [], "errors": [], "applied_count": 0, "error_count": 0, "message": "Нет строк для применения"}

    azure_client = integration_registry.get_client(
        IntegrationType.AZURE_DEVOPS, _resolve_integration_config(db, IntegrationType.AZURE_DEVOPS)
    )

    applied: list[str] = []
    errors: list[dict] = []

    # Capped concurrency: firing all creates at once means every one of them
    # POSTs to the SAME Suites/{id}/TestCase URL concurrently to link into the
    # suite, which Azure DevOps has been observed to silently drop some of
    # under full concurrency even with the client-side retry in
    # create_test_case_in_suite (real incident: 13/41 created but unlinked).
    apply_semaphore = asyncio.Semaphore(5)

    async def _apply_one(row: dict) -> None:
        key = str(row.get("key") or "")
        async with apply_semaphore:
            try:
                result = await uat_bug_test_cases_workflow.create_test_case_for_row(
                    azure_client,
                    plan_id=plan_id,
                    suite_id=suite_id,
                    priority=priority,
                    state=uat_bug_test_cases_workflow.DEFAULT_STATE,
                    row=row,
                )
                row["result"] = result
                if result.get("success"):
                    applied.append(key)
                else:
                    errors.append({"key": key, "error": result.get("error")})
            except Exception as exc:
                errors.append({"key": key, "error": str(exc)})

    await asyncio.gather(*[_apply_one(row) for row in rows_to_apply])

    created_count = sum(1 for row in rows if isinstance(row.get("result"), dict) and row["result"].get("success"))
    summary["created_count"] = created_count
    summary["failed_count"] = sum(
        1 for row in rows
        if isinstance(row.get("result"), dict) and row["result"].get("success") is False
    )
    workflow_result["summary"] = summary
    workflow_result["results"] = rows
    artifact_path.write_text(json.dumps(workflow_result, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "applied": applied,
        "errors": errors,
        "applied_count": len(applied),
        "error_count": len(errors),
        "created_count": created_count,
    }
