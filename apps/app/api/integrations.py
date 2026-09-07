"""Integration settings API endpoints"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from apps.app.database import IntegrationConfigModel, get_session_factory
from apps.app.config import get_settings
from apps.app.workflows import _resolve_integration_config
from packages.common import (
    IntegrationConfig,
    IntegrationConnectionStatus,
    IntegrationType,
    SecretEncryption,
    get_logger,
)
from packages.integrations import integration_registry

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
async def list_integrations(
    request: Request, db: Session = Depends(get_db)
) -> list[IntegrationConfig]:
    """List all configured integrations"""
    correlation_id = getattr(request.state, "correlation_id", None)

    configs = db.query(IntegrationConfigModel).all()
    result = []

    for config in configs:
        result.append(
            IntegrationConfig(
                type=config.type,
                is_configured=config.status != "unconfigured",
                status=IntegrationConnectionStatus(config.status),
                last_tested=config.last_tested,
                error_message=config.error_message,
            )
        )

    logger.info(f"Listed {len(result)} integration configs", correlation_id=correlation_id)
    return result


@router.post("/{integration_type}/test")
async def test_connection(
    integration_type: str, request: Request, db: Session = Depends(get_db)
) -> dict:
    """Test connection to an integration provider"""
    correlation_id = getattr(request.state, "correlation_id", None)

    try:
        provider_type = IntegrationType(integration_type)
    except ValueError:
        logger.warning(f"Invalid integration type: {integration_type}", correlation_id=correlation_id)
        raise HTTPException(status_code=400, detail=f"Invalid integration type: {integration_type}")

    # Resolve config the same way the real workflow run does: DB-saved config
    # takes precedence, falling back to .env — so this button reflects
    # whichever credentials a run would actually use.
    config_data = _resolve_integration_config(db, provider_type)
    if not any(config_data.values()):
        logger.warning(
            f"Integration config not found: {integration_type}", correlation_id=correlation_id
        )
        return {
            "success": False,
            "error": "Integration not configured — set values here or in .env",
        }

    config_record = db.query(IntegrationConfigModel).filter(
        IntegrationConfigModel.type == provider_type
    ).first()
    if not config_record:
        from uuid import uuid4
        # No DB override exists — config_data came from .env. Store an empty
        # encrypted config (not the resolved .env values) so this row only
        # tracks test status and doesn't start shadowing future .env edits.
        enc = SecretEncryption(get_settings().app_encryption_key)
        config_record = IntegrationConfigModel(
            id=str(uuid4()),
            type=provider_type,
            config_encrypted=enc.encrypt("{}"),
            status="unconfigured",
        )
        db.add(config_record)

    try:
        client = integration_registry.get_client(provider_type, config_data)
        success, error = await client.test_connection()

        # Update status in database
        config_record.status = "healthy" if success else "unhealthy"
        config_record.error_message = error
        config_record.last_tested = datetime.utcnow()
        db.commit()

        logger.info(
            f"Tested connection for {integration_type}: success={success}",
            correlation_id=correlation_id,
        )
        return {"success": success, "error": error}
    except Exception as e:
        logger.error(
            f"Failed to test connection for {integration_type}: {str(e)}",
            correlation_id=correlation_id,
        )
        raise HTTPException(status_code=500, detail=f"Failed to test connection: {str(e)}")


@router.put("/{integration_type}")
async def update_integration(
    integration_type: str, config: dict, request: Request, db: Session = Depends(get_db)
) -> dict:
    """Update integration configuration"""
    correlation_id = getattr(request.state, "correlation_id", None)

    try:
        provider_type = IntegrationType(integration_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid integration type: {integration_type}")

    enc = SecretEncryption(get_settings().app_encryption_key)

    config_record = db.query(IntegrationConfigModel).filter(
        IntegrationConfigModel.type == provider_type
    ).first()

    if not config_record:
        from uuid import uuid4
        config_record = IntegrationConfigModel(
            id=str(uuid4()),
            type=provider_type,
            status="unconfigured",
        )
        db.add(config_record)

    config_record.config_encrypted = enc.encrypt(str(config))
    config_record.status = "unconfigured"
    config_record.error_message = None

    db.commit()

    logger.info(f"Updated integration config: {integration_type}", correlation_id=correlation_id)
    return {"success": True, "message": f"Configuration updated for {integration_type}"}


@router.get("/{integration_type}")
async def get_integration(
    integration_type: str, request: Request, db: Session = Depends(get_db)
) -> dict:
    """Get integration configuration status (no secrets)"""
    correlation_id = getattr(request.state, "correlation_id", None)

    try:
        provider_type = IntegrationType(integration_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid integration type: {integration_type}")

    config_record = db.query(IntegrationConfigModel).filter(
        IntegrationConfigModel.type == provider_type
    ).first()

    if not config_record:
        return {
            "type": integration_type,
            "status": "unconfigured",
            "is_configured": False,
        }

    return {
        "type": integration_type,
        "status": config_record.status,
        "is_configured": config_record.status != "unconfigured",
        "last_tested": config_record.last_tested,
        "error_message": config_record.error_message,
    }
