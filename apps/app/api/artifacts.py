"""Artifact management API endpoints"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
import os

from apps.app.database import ArtifactModel, get_session_factory
from apps.app.config import get_settings
from packages.common import get_logger

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


@router.get("/run/{run_id}")
async def list_run_artifacts(
    run_id: str, request: Request, db: Session = Depends(get_db)
) -> list[dict]:
    """List artifacts for a run"""
    correlation_id = getattr(request.state, "correlation_id", None)

    artifacts = db.query(ArtifactModel).filter(ArtifactModel.run_id == run_id).all()

    result = []
    for artifact in artifacts:
        result.append({
            "id": artifact.id,
            "run_id": artifact.run_id,
            "filename": artifact.filename,
            "content_type": artifact.content_type,
            "size_bytes": int(artifact.size_bytes),
            "created_at": artifact.created_at.isoformat(),
            "download_url": f"/api/artifacts/{artifact.id}/download",
        })

    logger.info(
        f"Listed {len(result)} artifacts",
        correlation_id=correlation_id,
        extra={"run_id": run_id},
    )

    return result


@router.get("/{artifact_id}")
async def get_artifact(artifact_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """Get artifact metadata"""
    correlation_id = getattr(request.state, "correlation_id", None)

    artifact = db.query(ArtifactModel).filter(ArtifactModel.id == artifact_id).first()

    if not artifact:
        logger.warning(f"Artifact not found: {artifact_id}", correlation_id=correlation_id)
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}")

    return {
        "id": artifact.id,
        "run_id": artifact.run_id,
        "filename": artifact.filename,
        "content_type": artifact.content_type,
        "size_bytes": int(artifact.size_bytes),
        "created_at": artifact.created_at.isoformat(),
        "download_url": f"/api/artifacts/{artifact.id}/download",
    }


@router.get("/{artifact_id}/download")
async def download_artifact(
    artifact_id: str, request: Request, db: Session = Depends(get_db)
) -> FileResponse:
    """Download artifact file"""
    correlation_id = getattr(request.state, "correlation_id", None)

    artifact = db.query(ArtifactModel).filter(ArtifactModel.id == artifact_id).first()

    if not artifact:
        logger.warning(f"Artifact not found: {artifact_id}", correlation_id=correlation_id)
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}")

    settings = get_settings()
    file_path = os.path.join(settings.artifact_storage_path, artifact.storage_path)

    if not os.path.exists(file_path):
        logger.error(
            f"Artifact file not found on disk: {file_path}",
            correlation_id=correlation_id,
        )
        raise HTTPException(status_code=404, detail="Artifact file not found")

    logger.info(
        f"Downloaded artifact: {artifact.filename}",
        correlation_id=correlation_id,
        extra={"artifact_id": artifact_id},
    )

    return FileResponse(
        path=file_path,
        filename=artifact.filename,
        media_type=artifact.content_type,
    )
