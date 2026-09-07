"""Run history and logs API endpoints"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from apps.app.database import WorkflowRunModel, WorkflowRunLogModel, get_session_factory
from apps.app.config import get_settings
from packages.common import get_logger

logger = get_logger(__name__)

router = APIRouter()


def _status_to_str(status_value) -> str:
    """Return normalized status string for Enum or plain string values."""
    if hasattr(status_value, "value"):
        return str(status_value.value)
    return str(status_value or "unknown")


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
async def list_runs(
    request: Request,
    status: str = None,
    workflow_key: str = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    """List workflow runs with optional filtering"""
    correlation_id = getattr(request.state, "correlation_id", None)

    query = db.query(WorkflowRunModel)

    if status:
        query = query.filter(WorkflowRunModel.status == status)
    if workflow_key:
        query = query.filter(WorkflowRunModel.workflow_key == workflow_key)

    total = query.count()
    runs = query.order_by(WorkflowRunModel.created_at.desc()).offset(offset).limit(limit).all()

    result = []
    for run in runs:
        result.append({
            "id": run.id,
            "workflow_key": run.workflow_key,
            "status": _status_to_str(run.status),
            "created_at": run.created_at.isoformat(),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        })

    logger.info(
        f"Listed {len(result)} workflow runs",
        correlation_id=correlation_id,
        extra={"total": total, "limit": limit, "offset": offset},
    )

    return {
        "runs": result,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{run_id}")
async def get_run(run_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """Get run details"""
    correlation_id = getattr(request.state, "correlation_id", None)

    run = db.query(WorkflowRunModel).filter(WorkflowRunModel.id == run_id).first()

    if not run:
        logger.warning(f"Run not found: {run_id}", correlation_id=correlation_id)
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    # Calculate duration
    duration = None
    if run.started_at and run.completed_at:
        duration = (run.completed_at - run.started_at).total_seconds()

    return {
        "id": run.id,
        "workflow_key": run.workflow_key,
        "status": _status_to_str(run.status),
        "parameters": run.parameters,
        "dry_run": run.dry_run == "1",
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "duration_seconds": duration,
        "error_message": run.error_message,
    }


@router.get("/{run_id}/logs")
async def get_run_logs(
    run_id: str,
    request: Request,
    limit: int = 1000,
    db: Session = Depends(get_db),
) -> dict:
    """Get run logs"""
    correlation_id = getattr(request.state, "correlation_id", None)

    run = db.query(WorkflowRunModel).filter(WorkflowRunModel.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    logs = (
        db.query(WorkflowRunLogModel)
        .filter(WorkflowRunLogModel.run_id == run_id)
        .order_by(WorkflowRunLogModel.timestamp.asc())
        .limit(limit)
        .all()
    )

    log_lines = []
    for log in logs:
        log_lines.append({
            "timestamp": log.timestamp.isoformat(),
            "level": log.level,
            "message": log.message,
        })

    logger.info(
        f"Retrieved {len(log_lines)} log lines",
        correlation_id=correlation_id,
        extra={"run_id": run_id},
    )

    return {
        "run_id": run_id,
        "logs": log_lines,
        "count": len(log_lines),
    }
