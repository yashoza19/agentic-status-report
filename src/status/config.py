from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "skills"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(
        default="postgresql+psycopg://localhost/weekly_status",
        alias="DATABASE_URL",
    )
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    claude_model: str = Field(default="claude-sonnet-5", alias="CLAUDE_MODEL")

    skill_provider: str = Field(
        default="anthropic",
        alias="SKILL_PROVIDER",
        description="anthropic (Claude hosted skills) or openai (OpenAI hosted skills)",
    )
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(
        default="https://api.openai.com/v1",
        alias="OPENAI_BASE_URL",
        description="Optional OpenAI-compatible chat base URL (not used for hosted skills)",
    )
    openai_skills_base_url: str | None = Field(
        default=None,
        alias="OPENAI_SKILLS_BASE_URL",
        description="Skills upload + Responses API; defaults to https://api.openai.com/v1",
    )
    openai_model: str | None = Field(
        default=None,
        alias="OPENAI_MODEL",
        description="Optional OpenAI-compatible model id (e.g. Granite); not used for hosted skills",
    )
    openai_skills_model: str = Field(
        default="gpt-5.2",
        alias="OPENAI_SKILLS_MODEL",
        description="OpenAI Responses model that supports shell + skill_reference (e.g. gpt-5.2)",
    )

    slack_bot_token: str | None = Field(default=None, alias="SLACK_BOT_TOKEN")
    slack_app_token: str | None = Field(default=None, alias="SLACK_APP_TOKEN")
    report_channel_id: str | None = Field(default=None, alias="REPORT_CHANNEL_ID")

    jira_base_url: str | None = Field(
        default="https://redhat.atlassian.net",
        alias="JIRA_BASE_URL",
    )
    jira_api_email: str | None = Field(
        default=None,
        alias="JIRA_API_EMAIL",
        description="Email for Jira REST API authentication (operator token owner)",
    )
    jira_email: str | None = Field(
        default=None,
        alias="JIRA_EMAIL",
        description="Deprecated alias for JIRA_API_EMAIL",
    )
    jira_api_token: str | None = Field(default=None, alias="JIRA_API_TOKEN")
    jira_projects: str = Field(
        default="EET",
        alias="JIRA_PROJECTS",
        description="Comma-separated project keys to scope search, e.g. EET",
    )

    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    github_login: str | None = Field(
        default=None,
        alias="GITHUB_LOGIN",
        description="Deprecated; collect uses github_login from the person table",
    )
    github_max_prs: int = Field(default=50, alias="GITHUB_MAX_PRS")
    github_max_commits: int = Field(default=100, alias="GITHUB_MAX_COMMITS")

    jira_epic_field: str | None = Field(
        default=None,
        alias="JIRA_EPIC_FIELD",
        description="Custom field id for Epic Link, e.g. customfield_10014",
    )
    jira_max_issues: int = Field(default=100, alias="JIRA_MAX_ISSUES")

    pilot_person_ids: str = Field(default="", alias="PILOT_PERSON_IDS")

    drafter_skill_id: str | None = Field(default=None, alias="DRAFTER_SKILL_ID")
    drafter_skill_version: str = Field(default="latest", alias="DRAFTER_SKILL_VERSION")
    synthesizer_skill_id: str | None = Field(default=None, alias="SYNTHESIZER_SKILL_ID")
    synthesizer_skill_version: str = Field(default="latest", alias="SYNTHESIZER_SKILL_VERSION")

    @property
    def jira_auth_email(self) -> str | None:
        """Email paired with JIRA_API_TOKEN for REST authentication."""
        return self.jira_api_email or self.jira_email

    @property
    def pilot_person_id_list(self) -> list[str]:
        return [p.strip() for p in self.pilot_person_ids.split(",") if p.strip()]

    @property
    def jira_project_list(self) -> list[str]:
        return [p.strip() for p in self.jira_projects.split(",") if p.strip()]

    @property
    def effective_openai_skills_base_url(self) -> str:
        base = self.openai_skills_base_url or "https://api.openai.com/v1"
        return base.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
