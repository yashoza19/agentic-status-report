from __future__ import annotations

import json

import pytest

from status.collectors import person as person_module
from status.collectors.person import resolve_person


@pytest.fixture(autouse=True)
def roster_fixture(tmp_path, monkeypatch):
    roster_path = tmp_path / "eet-persons.json"
    roster_path.write_text(
        json.dumps(
            [
                {
                    "person_id": "makon57",
                    "github_login": "makon57",
                    "jira_email": "mkong@redhat.com",
                },
                {
                    "person_id": "caxu-rh",
                    "github_login": "caxu-rh",
                    "jira_email": "caxu@redhat.com",
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(person_module, "ROSTER_PATH", roster_path)
    person_module._roster_jira_emails.cache_clear()
    yield
    person_module._roster_jira_emails.cache_clear()


def test_resolve_person_uses_roster_jira_email_for_teammate() -> None:
    person = resolve_person("makon57", None, github_login="makon57")
    assert person.github_login == "makon57"
    assert person.jira_email == "mkong@redhat.com"


def test_resolve_person_uses_roster_for_caxu() -> None:
    person = resolve_person("caxu-rh", None)
    assert person.github_login == "caxu-rh"
    assert person.jira_email == "caxu@redhat.com"


def test_resolve_person_cli_override_wins() -> None:
    person = resolve_person(
        "caxu-rh",
        None,
        jira_email="override@redhat.com",
        github_login="other-login",
    )
    assert person.jira_email == "override@redhat.com"
    assert person.github_login == "other-login"


def test_resolve_person_prefers_db_jira_email(monkeypatch) -> None:
    class FakeRow:
        person_id = "caxu-rh"
        display_name = "Caleb Xu"
        github_login = "caxu-rh"
        jira_email = "db@redhat.com"

    class FakeSession:
        def get(self, model, person_id):  # noqa: ANN001
            return FakeRow()

    person = resolve_person("caxu-rh", FakeSession())
    assert person.jira_email == "db@redhat.com"
