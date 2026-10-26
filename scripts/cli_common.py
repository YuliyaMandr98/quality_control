"""
Shared helpers for the standalone CLI workflow scripts in this directory.

Not a standalone entry point - import from the other scripts here only.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from packages.integrations.anthropic import AnthropicClient
from packages.integrations.azure_devops import AzureDevOpsClient
from packages.integrations.confluence import ConfluenceClient
from packages.integrations.google_sheets import GoogleSheetsClient
from packages.integrations.jira import JiraClient

PROJECT_ROOT = Path(__file__).parent.parent


def load_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


def build_jira_client() -> JiraClient:
    return JiraClient({
        "base_url": os.getenv("JIRA_BASE_URL"),
        "email": os.getenv("JIRA_EMAIL"),
        "api_token": os.getenv("JIRA_API_TOKEN"),
    })


def build_confluence_client() -> ConfluenceClient:
    return ConfluenceClient({
        "base_url": os.getenv("CONFLUENCE_BASE_URL"),
        "space": os.getenv("CONFLUENCE_SPACE"),
        "email": os.getenv("CONFLUENCE_EMAIL"),
        "api_token": os.getenv("CONFLUENCE_API_TOKEN"),
    })


def build_anthropic_client() -> AnthropicClient:
    return AnthropicClient({
        "api_key": os.getenv("ANTHROPIC_API_KEY"),
        "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5"),
    })


def build_google_sheets_client(spreadsheet_override: str | None = None) -> GoogleSheetsClient:
    """`spreadsheet_override` accepts either a bare spreadsheet ID or a full
    Google Sheets URL (as pasted from the browser address bar) - lets a CLI
    invocation target a specific spreadsheet for one run without touching
    GOOGLE_SHEETS_SPREADSHEET_ID in .env."""
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
    if spreadsheet_override:
        from packages.integrations.google_sheets import extract_spreadsheet_id

        spreadsheet_id = extract_spreadsheet_id(spreadsheet_override)
    return GoogleSheetsClient({
        "spreadsheet_id": spreadsheet_id,
        "credentials_file": os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE"),
    })


def build_azure_client(project_override: str | None = None) -> AzureDevOpsClient:
    return AzureDevOpsClient({
        "org_url": os.getenv("AZURE_DEVOPS_ORG_URL"),
        "project": project_override or os.getenv("AZURE_DEVOPS_PROJECT"),
        "pat": os.getenv("AZURE_DEVOPS_PAT"),
    })
