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


def build_azure_client(project_override: str | None = None) -> AzureDevOpsClient:
    return AzureDevOpsClient({
        "org_url": os.getenv("AZURE_DEVOPS_ORG_URL"),
        "project": project_override or os.getenv("AZURE_DEVOPS_PROJECT"),
        "pat": os.getenv("AZURE_DEVOPS_PAT"),
    })
