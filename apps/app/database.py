"""Database models for the Triage Bugs Tool."""

from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Enum, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from packages.common.models import IntegrationType, RunStatus

Base = declarative_base()


class IntegrationConfigModel(Base):
    """Encrypted integration provider configuration."""
    __tablename__ = "integration_configs"

    id = Column(String(36), primary_key=True)
    type = Column(Enum(IntegrationType), nullable=False, unique=True)
    config_encrypted = Column(Text, nullable=False)  # Encrypted JSON
    last_tested = Column(DateTime, nullable=True)
    status = Column(String(50), default="unconfigured")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WorkflowRunModel(Base):
    """Persisted workflow execution."""
    __tablename__ = "workflow_runs"

    id = Column(String(36), primary_key=True)
    workflow_key = Column(String(100), nullable=False)
    status = Column(Enum(RunStatus), default=RunStatus.QUEUED)
    parameters = Column(JSON, default={})
    dry_run = Column(String(1), default="0")  # SQLite boolean workaround
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)


class WorkflowRunLogModel(Base):
    """Persisted workflow run logs."""
    __tablename__ = "workflow_run_logs"

    id = Column(String(36), primary_key=True)
    run_id = Column(String(36), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    level = Column(String(20), default="INFO")  # DEBUG, INFO, WARNING, ERROR
    message = Column(Text, nullable=False)
    correlation_id = Column(String(36), nullable=True)


class ArtifactModel(Base):
    """Workflow output artifacts."""
    __tablename__ = "artifacts"

    id = Column(String(36), primary_key=True)
    run_id = Column(String(36), nullable=False)
    filename = Column(String(255), nullable=False)
    content_type = Column(String(100), default="application/json")
    size_bytes = Column(String(20), default="0")
    storage_path = Column(Text, nullable=False)  # Relative path in data/artifacts/
    created_at = Column(DateTime, default=datetime.utcnow)


def get_engine(database_url: str):
    """Create SQLAlchemy engine."""
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(database_url, connect_args=connect_args, echo=False)


def get_session_factory(database_url: str):
    """Create session factory."""
    engine = get_engine(database_url)
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_all_tables(database_url: str):
    """Create all tables."""
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
