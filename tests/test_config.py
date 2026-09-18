from __future__ import annotations

from status.config import Settings


def test_jira_auth_email_prefers_api_email() -> None:
    settings = Settings(
        _env_file=None,
        JIRA_API_EMAIL="api@redhat.com",
        JIRA_EMAIL="legacy@redhat.com",
    )
    assert settings.jira_auth_email == "api@redhat.com"


def test_effective_gcp_project_prefers_gcp_project() -> None:
    settings = Settings(
        _env_file=None,
        GCP_PROJECT="from-gcp",
        GOOGLE_CLOUD_PROJECT="from-google",
    )
    assert settings.effective_gcp_project == "from-gcp"


def test_effective_gcp_project_falls_back_to_google_cloud_project() -> None:
    settings = Settings(_env_file=None, GOOGLE_CLOUD_PROJECT="from-google")
    assert settings.effective_gcp_project == "from-google"
