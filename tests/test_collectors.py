from datetime import date

from status.collectors.github import extract_issue_keys, _normalize_pr
from status.collectors.jira import build_jql, normalize_jira_issue


def test_build_jql_email_skips_commented_by() -> None:
    jql = build_jql("yoza@redhat.com", date(2026, 8, 8), date(2026, 8, 14))
    assert "commentedBy" not in jql
    assert 'assignee = "yoza@redhat.com"' in jql
    assert 'reporter = "yoza@redhat.com"' in jql


def test_build_jql_account_id_includes_contributor_fields() -> None:
    jql = build_jql("712020:abc-def", date(2026, 8, 8), date(2026, 8, 14), projects=["EET"])
    assert "Developer" in jql
    assert "Contributors" in jql
    assert "project in (EET)" in jql


def test_normalize_jira_issue_transitions_and_comments() -> None:
    issue = {
        "key": "EET-5000",
        "fields": {
            "summary": "Fix parsing",
            "issuetype": {"name": "Bug"},
            "status": {"name": "Done"},
            "project": {"key": "EET"},
            "updated": "2026-08-11T16:00:00.000+0000",
            "comment": {
                "comments": [
                    {
                        "author": {"accountId": "user-1", "displayName": "Alice"},
                        "body": "Shipped the fix.",
                        "created": "2026-08-11T15:00:00.000+0000",
                    }
                ]
            },
            "parent": {
                "key": "EET-4900",
                "fields": {"summary": "Chart-Verifier", "issuetype": {"name": "Epic"}},
            },
        },
    }
    changelog = {
        "histories": [
            {
                "created": "2026-08-11T16:00:00.000+0000",
                "items": [{"field": "status", "toString": "Done"}],
            }
        ]
    }
    normalized = normalize_jira_issue(
        issue,
        changelog,
        date(2026, 8, 8),
        date(2026, 8, 14),
        jira_account_id="user-1",
    )
    assert normalized["key"] == "EET-5000"
    assert normalized["epic_key"] == "EET-4900"
    assert len(normalized["transitions"]) == 1
    assert len(normalized["comments"]) == 1


def test_extract_issue_keys() -> None:
    keys = extract_issue_keys("EET-5000: fix bug and OCPBUGS-81187 follow-up")
    assert keys == ["EET-5000", "OCPBUGS-81187"]


def test_normalize_pr_filters_outside_window() -> None:
    item = {
        "html_url": "https://github.com/org/repo/pull/1",
        "title": "EET-1: test",
        "body": "",
        "updated_at": "2026-07-01T00:00:00Z",
        "created_at": "2026-07-01T00:00:00Z",
        "repository_url": "https://api.github.com/repos/org/repo",
        "pull_request": {"merged_at": None},
        "draft": False,
    }
    assert _normalize_pr(item, date(2026, 8, 8), date(2026, 8, 14)) is None
