"""Shared Pydantic models and enums."""

import re
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
    GOOGLE_SHEETS = "google_sheets"


class WorkflowType(str, Enum):
    """Workflow identifiers."""
    TRIAGE_BUGS = "triage_bugs"
    REVIEW_PULL_REQUEST = "review_pull_request"
    REVIEW_COMMENT_FIXES = "review_comment_fixes"
    UPLOAD_TEST_CASES = "upload_test_cases"
    SKIPPED_TESTS_AUDIT = "skipped_tests_audit"
    REVIEW_TEST_CASES = "review_test_cases"
    BUG_BACKLOG_AUDIT = "bug_backlog_audit"
    UAT_BUG_TEST_CASES = "uat_bug_test_cases"
    PRIORITIZE_TEST_CASES = "prioritize_test_cases"
    TEST_CASE_STATUS_SYNC = "test_case_status_sync"
    TESTING_TASKS_GENERATOR = "testing_tasks_generator"
    UAT_BUGS_REPORT = "uat_bugs_report"
    CANCELLED_BUG_SEVERITY_CLEANUP = "cancelled_bug_severity_cleanup"
    FAILED_BLOCKED_TEST_CASES_REPORT = "failed_blocked_test_cases_report"
    BUGS_PER_EPICS_REPORT = "bugs_per_epics_report"
    AC_CHECKLIST_SYNC = "ac_checklist_sync"


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


class CancelledBugSeverityCleanupRunRequest(BaseModel):
    """Request payload for cancelled_bug_severity_cleanup workflow.

    Finds bugs matched by `jql` (cancelled bugs in the MB project by default)
    that still have the Severity custom field set and clears it. `apply=False`
    (default) only previews which bugs would be affected.
    """

    jql: str = Field(
        default='project = MB AND issuetype in ("BE BUG", "Mobile bug", "FE bug") AND status = Cancelled',
        description="JQL query selecting cancelled bug issues to clean up",
    )
    max_results: int = Field(default=200, ge=1, le=1000, description="Maximum bugs to check")
    apply: bool = Field(
        default=False,
        description="If true, actually clear the Severity field in Jira; otherwise only preview",
    )


class FailedBlockedTestCasesReportRunRequest(BaseModel):
    """Request payload for failed_blocked_test_cases_report workflow.

    Read-only report: finds all Failed/Blocked test points in the Mobile
    and/or Web UI test suites and reports each test case's id/title/
    configuration plus its parent folder (Epic suite) and subfolder (User
    Story suite). Never writes to Azure DevOps.
    """

    include_mobile: bool = Field(default=True, description="Scan the Mobile UI test suite")
    include_web: bool = Field(default=True, description="Scan the Web UI test suite")
    mobile_plan_id: str = Field(default="438", description="Azure DevOps Test Plan ID for Mobile")
    mobile_root_suite_id: str = Field(default="439", description="Root suite id to scan for Mobile")
    web_plan_id: str = Field(default="15751", description="Azure DevOps Test Plan ID for Web")
    web_root_suite_id: str = Field(default="17762", description="Root suite id to scan for Web")

    @model_validator(mode="after")
    def _require_a_platform(self) -> "FailedBlockedTestCasesReportRunRequest":
        if not (self.include_mobile or self.include_web):
            raise ValueError("Нужно выбрать хотя бы одну платформу: Mobile или Web")
        return self


class UatBugsReportRunRequest(BaseModel):
    """Request payload for uat_bugs_report workflow. Read-only on Jira, writes
    (for real, no dry-run) to a Google Sheets tab - there's no apply step."""

    jql: str = Field(
        default='project = MB AND issuetype = Bug ORDER BY parent ASC',
        description="JQL query selecting bugs to report on",
    )
    max_results: int = Field(default=500, ge=1, le=2000, description="Maximum bugs to fetch")
    sheet_title: str = Field(default="UAT BUGs", description="Target Google Sheets tab name")
    run_date: Optional[str] = Field(
        default=None, description="Override the snapshot date (YYYY-MM-DD); defaults to today"
    )
    spreadsheet: Optional[str] = Field(
        default=None,
        description=(
            "Google Sheets URL or spreadsheet ID to write to for this run only - "
            "leave empty to use the spreadsheet configured on the Google Sheets "
            "integration"
        ),
    )


class BugsPerEpicsReportRunRequest(BaseModel):
    """Request payload for bugs_per_epics_report workflow. Read-only on Jira,
    writes (for real, no dry-run) to a Google Sheets tab - there's no apply step."""

    jql: str = Field(
        default='project = MB AND issuetype in ("BE BUG", Bug, "Mobile bug", "FE bug") ORDER BY parent ASC',
        description="JQL query selecting bugs to report on",
    )
    max_results: int = Field(default=2000, ge=1, le=5000, description="Maximum bugs to fetch")
    sheet_title: str = Field(default="Bugs per Epics", description="Target Google Sheets tab name")
    run_date: Optional[str] = Field(
        default=None, description="Override the snapshot date (YYYY-MM-DD); defaults to today"
    )
    spreadsheet: Optional[str] = Field(
        default=None,
        description=(
            "Google Sheets URL or spreadsheet ID to write to for this run only - "
            "leave empty to use the spreadsheet configured on the Google Sheets "
            "integration"
        ),
    )


class AcChecklistSyncRunRequest(BaseModel):
    """Request payload for ac_checklist_sync workflow."""

    issue_key: str = Field(
        ...,
        min_length=3,
        description="Jira issue key (e.g. MB-1666) whose checklist should be synced",
    )
    specs_folder_title: str = Field(
        default="Фаза 1: спецификации",
        description="Confluence folder (page title) containing the User Story spec pages",
    )
    dry_run: bool = Field(
        default=True,
        description="If true, only report what would change - never writes to Jira",
    )
    feature_scope: Literal["both", "mb_only", "web_only"] = Field(
        default="both",
        description="Which platform(s) this feature ships on; the other platform's section is cleared entirely if not 'both'",
    )
    platforms: Optional[list[str]] = Field(
        default=None,
        description="Restrict which section(s) get written this call (e.g. ['MB']) - for applying one platform at a time; omit for both",
    )
    mark_dor_dod: bool = Field(
        default=False,
        description="Also mark Definition of Ready / Definition of Done items as done, excluding platform-specific items not covered by feature_scope",
    )

    @model_validator(mode="after")
    def _normalize_issue_key(self) -> "AcChecklistSyncRunRequest":
        self.issue_key = self.issue_key.strip().upper()
        if not re.match(r"^[A-Z][A-Z0-9]*-\d+$", self.issue_key):
            raise ValueError(f"'{self.issue_key}' не похож на ключ задачи Jira (ожидается, например, MB-1666)")
        return self

    @model_validator(mode="after")
    def _normalize_platforms(self) -> "AcChecklistSyncRunRequest":
        if self.platforms is not None:
            normalized = [p.strip().upper() for p in self.platforms if p and p.strip()]
            invalid = [p for p in normalized if p not in ("MB", "WEB")]
            if invalid:
                raise ValueError(f"Недопустимые значения platforms: {invalid} (ожидается 'MB' и/или 'WEB')")
            self.platforms = normalized or None
        return self


class UatBugTestCasesRunRequest(BaseModel):
    """Request payload for uat_bug_test_cases workflow."""

    jql: str = Field(
        default='project = MB and issuetype in (Bug) and Phasa in (1) ORDER BY parent ASC',
        description="JQL query selecting UAT bugs to convert into test cases",
    )
    plan_id: str = Field(default="12296", description="Azure DevOps test plan id")
    root_suite_id: str = Field(default="12297", description="Root 'Regression' suite id containing the Sprint N folders")
    sprint_number: Optional[int] = Field(
        default=None, description="Target Sprint N folder; if omitted, the highest-numbered sprint is used"
    )
    max_results: int = Field(default=200, ge=1, le=500, description="Maximum bugs to fetch from Jira")
    priority: Literal["High", "Medium", "Low"] = Field(default="Medium", description="Priority for created test cases")
    dry_run: bool = Field(default=True, description="Preview only — set false to actually create test cases")


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


class PrioritizeTestCasesRunRequest(BaseModel):
    """Request payload for prioritize_test_cases workflow.

    Sets Azure DevOps test case Priority for test cases that already exist,
    from the same P0/P1/P2 business-criticality classification
    upload_test_cases uses at creation time - never creates suites or test
    cases, only updates the Priority field on what's already there.
    """

    scope: Literal["single_us", "whole_plan"] = Field(
        description=(
            "single_us: reprioritize one User Story's existing suite. "
            "whole_plan: reprioritize every US/AUS suite found in the Test Plan."
        )
    )
    plan_id: str = Field(description="Azure DevOps Test Plan ID")
    us: Optional[str] = Field(
        default=None, description="User Story number/code - required when scope='single_us'"
    )
    specs_folder: str = Field(
        default="Фаза 1: спецификации", description="Confluence page title of the specifications folder"
    )
    admin_specs_folder_id: str = Field(
        default="10321934", description="Fallback Confluence page id for admin-panel specs (AUS-<n> pages)"
    )
    admin_group_title: str = Field(
        default="Админ Панель", description="Azure suite name for the admin-panel grouping level"
    )
    batch_delay_seconds: int = Field(
        default=10, ge=0, le=60, description="Seconds to wait between LLM calls (relevant for scope='whole_plan')"
    )
    dry_run: bool = Field(
        default=True, description="Preview only — set false to actually update priorities in Azure DevOps"
    )

    @model_validator(mode="after")
    def _require_us_for_single_scope(self) -> "PrioritizeTestCasesRunRequest":
        if self.scope == "single_us" and not (self.us or "").strip():
            raise ValueError("us обязателен, когда scope='single_us'")
        return self


class TestCaseStatusSyncRunRequest(BaseModel):
    """Request payload for test_case_status_sync workflow.

    Finds Azure DevOps test points marked Failed for a given configuration
    (e.g. "macOS Apidog"), checks whether the matching automated test (by test
    case id in its title) is enabled (not `.skip`) in the local qa-api-tests
    repo, and - if so - sets that test point's outcome back to Passed. Test
    cases with no matching automated test are never touched, only reported.
    """

    plan_id: str = Field(default="2015", description="Azure DevOps Test Plan ID")
    root_suite_id: str = Field(default="2016", description="Root suite id to scan (including all nested child suites)")
    configuration_name: str = Field(default="macOS Apidog", description="Test configuration name to check")
    tests_root: str = Field(
        default="/Users/oadmin/PROJECTS/FINCA/qa-api-tests/tests",
        description="Root directory of the qa-api-tests repo to scan for *.spec.ts automated tests",
    )
    apply: bool = Field(
        default=False,
        description="If true, actually set matching test points' outcome to Passed in Azure DevOps; otherwise only preview",
    )


class TestingTasksGeneratorRunRequest(BaseModel):
    """Request payload for testing_tasks_generator workflow.

    Given a "История" (User Story) issue key, looks at its "is Tested by"
    linked testing tasks and creates whichever of the standard QA testing
    tasks (API / Mobile / Web - test cases, manual testing, automation) are
    still missing, mirroring the parent story's summary with the matching
    prefix (e.g. "[auto API]"). `include_api`/`include_mobile`/`include_web`
    pick which platforms to generate tasks for.
    """

    story_key: str = Field(description="Jira key of the 'История' (User Story) issue, e.g. MB-3454")
    include_api: bool = Field(default=True, description="Generate API testing tasks (test cases+execution, automation)")
    include_mobile: bool = Field(default=True, description="Generate Mobile testing tasks (test cases, manual testing, automation)")
    include_web: bool = Field(default=True, description="Generate Web testing tasks (test cases, manual testing, automation)")
    apply: bool = Field(
        default=False,
        description="If true, actually create the missing issues (and link them) in Jira; otherwise only preview",
    )

    @model_validator(mode="after")
    def _require_a_platform(self) -> "TestingTasksGeneratorRunRequest":
        if not (self.include_api or self.include_mobile or self.include_web):
            raise ValueError("Нужно выбрать хотя бы одну платформу: API, Mobile или Web")
        if not self.story_key.strip():
            raise ValueError("story_key обязателен")
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
