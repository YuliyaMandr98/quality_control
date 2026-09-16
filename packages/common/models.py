"""Shared Pydantic models and enums."""

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class IntegrationType(str, Enum):
    """Supported integration providers."""
    CONFLUENCE = "confluence"
    JIRA = "jira"
    ANTHROPIC = "anthropic"
    AZURE_DEVOPS = "azure_devops"


class WorkflowType(str, Enum):
    """Workflow identifiers."""
    TRIAGE_BUGS = "triage_bugs"
    REVIEW_PULL_REQUEST = "review_pull_request"
    REVIEW_COMMENT_FIXES = "review_comment_fixes"
    UPLOAD_TEST_CASES = "upload_test_cases"
    SKIPPED_TESTS_AUDIT = "skipped_tests_audit"
    REVIEW_TEST_CASES = "review_test_cases"
    BUG_BACKLOG_AUDIT = "bug_backlog_audit"


class RunStatus(str, Enum):
    """Workflow run status lifecycle."""
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class IntegrationConnectionStatus(str, Enum):
    """Connection health status."""
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNCONFIGURED = "unconfigured"


# === Schemas for API ===


class IntegrationConfig(BaseModel):
    """Integration provider configuration."""
    type: IntegrationType
    space: Optional[str] = None
    email: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    org_url: Optional[str] = None
    is_configured: bool = False
    status: IntegrationConnectionStatus = IntegrationConnectionStatus.UNCONFIGURED
    last_tested: Optional[datetime] = None
    error_message: Optional[str] = None

    class Config:
        use_enum_values = True


class WorkflowRunCreate(BaseModel):
    """Request to create a workflow run."""
    workflow_key: Optional[WorkflowType] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    force: bool = False


class TriageBugTicketsRunRequest(BaseModel):
    """Request payload for triage_bugs workflow."""

    jql: str = Field(
        default='issuetype in ("BE BUG", "Mobile bug", Bug, "FE bug") AND status = Backlog',
        description="JQL query selecting bug issues to triage",
    )
    max_results: int = Field(default=50, ge=1, le=500, description="Maximum bugs to process")
    apply: bool = Field(default=False, description="When False, runs in dry-run mode without modifying Jira")
    add_comment: bool = Field(
        default=False,
        description="When True and apply=True, add triage reasoning comments to Jira issues",
    )
    severity_field_id: str = Field(
        default="customfield_10865", description="Jira custom field ID for Severity"
    )
    impact_field_id: str = Field(
        default="customfield_10004", description="Jira custom field ID for Impact"
    )
    target_status: str = Field(
        default="Triage", description="Jira status name to transition confirmed bugs into"
    )
    batch_delay_seconds: int = Field(
        default=10, ge=0, le=60, description="Seconds to wait between Claude API calls"
    )


class ReviewPullRequestRunRequest(BaseModel):
    """Request payload for review_pull_request workflow."""

    repo: str = Field(description="Azure DevOps repository name (or ID)")
    pr_id: int = Field(description="Pull Request ID")
    project: Optional[str] = Field(
        default=None, description="Azure DevOps project (defaults to AZURE_DEVOPS_PROJECT)"
    )
    no_anonymize: bool = Field(
        default=False, description="Skip anonymization of file content before sending it to Claude"
    )


class ReviewCommentFixesRunRequest(BaseModel):
    """Request payload for review_comment_fixes workflow."""

    repo: str = Field(description="Azure DevOps repository name (or ID)")
    pr_id: int = Field(description="Pull Request ID")
    project: Optional[str] = Field(
        default=None, description="Azure DevOps project (defaults to AZURE_DEVOPS_PROJECT)"
    )
    no_anonymize: bool = Field(
        default=False, description="Skip anonymization of code/comments before sending them to Claude"
    )


class SkippedTestsAuditRunRequest(BaseModel):
    """Request payload for skipped_tests_audit workflow."""

    tests_root: str = Field(
        default="/Users/oadmin/PROJECTS/FINCA/qa-api-tests/tests",
        description="Root directory to scan for *.spec.ts test files",
    )


class BugBacklogAuditRunRequest(BaseModel):
    """Request payload for bug_backlog_audit workflow."""

    jql: str = Field(
        default='issuetype in ("BE BUG", "Mobile bug", Bug, "FE bug") AND status = Backlog',
        description="JQL query selecting bug issues to audit",
    )
    max_results: int = Field(default=100, ge=1, le=500, description="Maximum bugs to check")


class ReviewTestCasesRunRequest(BaseModel):
    """Request payload for review_test_cases workflow."""

    us: str = Field(description="User Story number/code (used to label the report)")
    test_type: Literal["web", "mobile", "api"] = Field(
        description="Test case type — determines what the review focuses on"
    )
    spec_url: str = Field(description="Confluence link (or bare page ID) to the specification")
    tech_impl_url: Optional[str] = Field(
        default=None,
        description="Confluence link (or bare page ID) to the technical implementation — required when test_type='api'",
    )
    test_cases_text: str = Field(description="Pasted test cases to review (plain text or CSV)")

    @model_validator(mode="after")
    def _require_tech_impl_for_api(self) -> "ReviewTestCasesRunRequest":
        if self.test_type == "api" and not (self.tech_impl_url or "").strip():
            raise ValueError("tech_impl_url обязателен, когда test_type='api'")
        return self


class WorkflowRunResponse(BaseModel):
    """Workflow run response."""
    id: str
    workflow_key: WorkflowType
    status: RunStatus
    parameters: dict[str, Any]
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    log_lines: int = 0
    error_message: Optional[str] = None

    class Config:
        use_enum_values = True


class ArtifactResponse(BaseModel):
    """Artifact metadata."""
    id: str
    run_id: str
    filename: str
    content_type: str
    size_bytes: int
    created_at: datetime
    download_url: str


class HealthCheckResponse(BaseModel):
    """System health status."""
    status: str  # "ok" or "degraded"
    database: str  # "ok" or "error"
    integrations: dict[str, str]  # integration_name -> status


class ErrorResponse(BaseModel):
    """Standard error response."""
    error: str
    detail: Optional[str] = None
    correlation_id: Optional[str] = None
