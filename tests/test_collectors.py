from datetime import date
from unittest.mock import patch

from status.collectors.github import (
    _normalize_commit,
    _normalize_pr,
    collect_github_activity,
    commit_summary,
    extract_issue_keys,
)
from status.config import Settings
from status.collectors.payload import build_payload
from status.collectors.jira import build_jql, normalize_jira_issue


def test_build_jql_email_skips_commented_by() -> None:
    jql = build_jql("user@example.com", date(2026, 8, 8), date(2026, 8, 14))
    assert "commentedBy" not in jql
    assert 'assignee = "user@example.com"' in jql
    assert 'reporter = "user@example.com"' in jql


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


def test_commit_summary_uses_subject_line() -> None:
    message = "EET-5001: add destroy command\n\nAlso fix tests."
    assert commit_summary(message) == "EET-5001: add destroy command"


def test_normalize_commit_includes_summary_and_repo() -> None:
    item = {
        "sha": "abc123",
        "html_url": "https://github.com/example-org/example-repo/commit/abc123",
        "repository": {"full_name": "example-org/example-repo"},
        "commit": {
            "message": "EET-5001: add destroy command\n\nAlso fix tests.",
            "committer": {"date": "2026-08-11T12:00:00Z"},
        },
    }
    normalized = _normalize_commit(item, date(2026, 8, 8), date(2026, 8, 14))
    assert normalized is not None
    assert normalized["summary"] == "EET-5001: add destroy command"
    assert normalized["repo"] == "example-org/example-repo"
    assert normalized["linked_issue_keys"] == ["EET-5001"]


def test_normalize_commit_filters_outside_window() -> None:
    item = {
        "sha": "abc123",
        "html_url": "https://github.com/org/repo/commit/abc123",
        "repository": {"full_name": "org/repo"},
        "commit": {
            "message": "old work",
            "committer": {"date": "2026-07-01T00:00:00Z"},
        },
    }
    assert _normalize_commit(item, date(2026, 8, 8), date(2026, 8, 14)) is None


def test_build_payload_includes_commits() -> None:
    payload = build_payload(
        "pilot",
        date(2026, 8, 14),
        [],
        [],
        commits=[{"summary": "EET-1: ship it", "repo": "org/repo"}],
    )
    assert payload["commits"][0]["summary"] == "EET-1: ship it"


def test_collect_github_activity_searches_all_repos() -> None:
    settings = Settings(
        GITHUB_TOKEN="ghp_test",
        GITHUB_MAX_PRS=1,
        GITHUB_MAX_COMMITS=1,
    )
    pr_response = {
        "items": [
            {
                "html_url": "https://github.com/org/a/pull/1",
                "title": "EET-1: pr",
                "body": "",
                "updated_at": "2026-08-11T00:00:00Z",
                "created_at": "2026-08-11T00:00:00Z",
                "repository_url": "https://api.github.com/repos/org/a",
                "pull_request": {"merged_at": None},
                "draft": False,
            }
        ]
    }
    commit_response = {
        "items": [
            {
                "sha": "deadbeef",
                "html_url": "https://github.com/org/b/commit/deadbeef",
                "repository": {"full_name": "org/b"},
                "commit": {
                    "message": "EET-2: commit",
                    "committer": {"date": "2026-08-11T00:00:00Z"},
                },
            }
        ]
    }

    with patch("status.collectors.github.get_json", side_effect=[pr_response, commit_response]) as mock_get:
        activity = collect_github_activity(
            "pilot-user",
            date(2026, 8, 8),
            date(2026, 8, 14),
            settings=settings,
        )

    assert len(activity["pull_requests"]) == 1
    assert len(activity["commits"]) == 1
    assert activity["commits"][0]["summary"] == "EET-2: commit"

    pr_query = mock_get.call_args_list[0][0][0]
    commit_query = mock_get.call_args_list[1][0][0]
    assert "repo%3A" not in pr_query
    assert "repo%3A" not in commit_query
    assert "author%3Apilot-user" in pr_query
    assert "committer-date%3A2026-08-08..2026-08-14" in commit_query
    assert "author%3Apilot-user" in commit_query
