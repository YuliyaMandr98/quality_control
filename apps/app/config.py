"""Configuration for the Triage Bugs Tool."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Application settings from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        case_sensitive=False,
        extra="ignore",
    )

    # === Application ===
    app_env: str = "development"
    app_debug: bool = True
    app_port: int = 8001

    # === Security ===
    app_encryption_key: str = "dev-encryption-key-change-in-production"

    # === Database ===
    database_url: str = "sqlite:///./data/triage_bugs_claude.db"

    # === Confluence ===
    confluence_base_url: str = "https://yourdomain.atlassian.net/wiki"
    confluence_space: str = "YOURSPACE"
    confluence_email: str = ""
    confluence_api_token: str = ""

    # === Jira ===
    jira_base_url: str = "https://yourdomain.atlassian.net"
    jira_email: str = ""
    jira_api_token: str = ""

    # === Anthropic (Claude) ===
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # === Azure DevOps (used by the PR review workflows only) ===
    azure_devops_org_url: str = "https://dev.azure.com/yourorg"
    azure_devops_project: str = "YourProject"
    azure_devops_pat: str = ""

    # === Storage ===
    artifact_storage_path: str = "./data/artifacts"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Prefer values from the repo-root .env over inherited shell env vars.
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
