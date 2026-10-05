from datetime import date
from unittest.mock import patch

from status.collectors.github import (
    _normalize_commit,
    _normalize_created_issue,
    _normalize_pr,
    collect_github_activity,
    collect_github_issue_activity,
    collect_github_pull_request_reviews,
    commit_summary,
    extract_issue_keys,
)
from status.collectors.jira import (
    build_collaboration_jql,
    build_jql,
    collect_jira_activity,
    filter_person_jira_issues,
    normalize_jira_issue,
)
from status.collectors.payload import build_payload
from status.config import Settings


def test_build_jql_email_uses_assignee_and_reporter() -> None:
    jql = build_jql("user@example.com", date(2026, 8, 8), date(2026, 8, 14))
    assert "commentedBy" not in jql
    assert 'assignee = "user@example.com"' in jql
    assert 'reporter = "user@example.com"' in jql
    assert "Developer" not in jql


def test_build_jql_scopes_projects() -> None:
    jql = build_jql("user@example.com", date(2026, 8, 8), date(2026, 8, 14), projects=["EET"])
    assert "project in (EET)" in jql


def test_build_collaboration_jql_scopes_recent_project_activity() -> None:
    jql = build_collaboration_jql(
        date(2026, 8, 8),
        date(2026, 8, 14),
        projects=["EET", "CNFCERT"],
    )
    assert "project in (EET, CNFCERT)" in jql
    assert 'updated >= "2026-08-08"' in jql
    assert 'updated <= "2026-08-14"' in jql
    assert "assignee" not in jql


def test_normalize_jira_issue_transitions_and_comments() -> None:
    issue = {
        "key": "EET-5000",
        "fields": {
            "summary": "Fix parsing",
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Handle nested values."}],
                    }
                ],
            },
            "issuetype": {"name": "Bug"},
            "status": {"name": "Done"},
            "project": {"key": "EET"},
            "updated": "2026-08-11T16:00:00.000+0000",
            "comment": {
                "comments": [
                    {
                        "author": {
                            "accountId": "user-1",
                            "displayName": "Alice",
                            "emailAddress": "alice@example.com",
                        },
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
                "author": {
                    "accountId": "user-1",
                    "emailAddress": "alice@example.com",
                },
                "items": [{"field": "status", "toString": "Done"}],
            }
        ]
    }
    normalized = normalize_jira_issue(
        issue,
        changelog,
        date(2026, 8, 8),
        date(2026, 8, 14),
        jira_email="alice@example.com",
    )
    assert normalized["key"] == "EET-5000"
    assert normalized["description"] == "Handle nested values."
    assert normalized["epic_key"] == "EET-4900"
    assert normalized["is_assignee"] is False
    assert len(normalized["transitions"]) == 1
    assert len(normalized["comments"]) == 1
    assert normalized["activity_role"] == "collaborator"


def test_normalize_jira_issue_ignores_other_peoples_activity() -> None:
    issue = {
        "key": "EET-5001",
        "fields": {
            "summary": "Partner follow-up",
            "description": "Coordinate the certification follow-up.",
            "issuetype": {"name": "Task"},
            "status": {"name": "In Progress"},
            "project": {"key": "EET"},
            "updated": "2026-08-11T16:00:00.000+0000",
            "assignee": {"emailAddress": "owner@example.com"},
            "reporter": {"emailAddress": "reporter@example.com"},
            "comment": {
                "comments": [
                    {
                        "author": {"emailAddress": "someone@example.com"},
                        "body": "Unrelated comment.",
                        "created": "2026-08-11T15:00:00.000+0000",
                    }
                ]
            },
        },
    }
    changelog = {
        "histories": [
            {
                "created": "2026-08-11T16:00:00.000+0000",
                "author": {"emailAddress": "someone@example.com"},
                "items": [{"field": "status", "toString": "Done"}],
            }
        ]
    }
    normalized = normalize_jira_issue(
        issue,
        changelog,
        date(2026, 8, 8),
        date(2026, 8, 14),
        jira_email="alice@example.com",
    )
    assert normalized["activity_role"] == "collaborator"
    assert normalized["comments"] == []
    assert normalized["transitions"] == []


def test_filter_person_jira_issues_drops_watcher_only_rows() -> None:
    issues = [
        {
            "key": "EET-5527",
            "is_assignee": False,
            "is_reporter": False,
            "transitions": [],
            "comments": [],
            "assignee_display_name": "Manna",
        },
        {
            "key": "EET-5528",
            "is_assignee": True,
            "is_reporter": False,
            "transitions": [],
            "comments": [],
        },
    ]
    kept = filter_person_jira_issues(issues, "alice@example.com")
    assert [row["key"] for row in kept] == ["EET-5528"]


def test_collect_jira_activity_keeps_comment_on_someone_elses_ticket() -> None:
    candidate = {
        "key": "EET-5599",
        "fields": {
            "summary": "Cover partner request during leave",
            "description": "Investigate the partner's reported failure.",
            "issuetype": {"name": "Task"},
            "status": {"name": "In Progress"},
            "project": {"key": "EET"},
            "updated": "2026-08-12T00:00:00.000+0000",
            "assignee": {
                "accountId": "owner-1",
                "emailAddress": "owner@example.com",
                "displayName": "Owner",
            },
            "reporter": {
                "accountId": "reporter-1",
                "emailAddress": "reporter@example.com",
                "displayName": "Reporter",
            },
            "comment": {"comments": []},
        },
    }
    own_comment = {
        "author": {
            "accountId": "alice-1",
            "emailAddress": "alice@example.com",
            "displayName": "Alice",
        },
        "body": "Reproduced the failure and sent the partner a workaround.",
        "created": "2026-08-12T12:00:00.000+0000",
    }
    settings = Settings(
        _env_file=None,
        JIRA_BASE_URL="https://jira.example.com",
        JIRA_API_EMAIL="service@example.com",
        JIRA_API_TOKEN="token",
        JIRA_PROJECTS="EET",
    )
    with (
        patch("status.collectors.jira._auth_header", return_value={}),
        patch("status.collectors.jira.check_project_access", return_value=[]),
        patch(
            "status.collectors.jira._search_jira_issues",
            side_effect=[[], [candidate]],
        ),
        patch("status.collectors.jira._resolve_jira_account_id", return_value="alice-1"),
        patch("status.collectors.jira._fetch_jira_comments", return_value=[own_comment]),
        patch("status.collectors.jira.get_json", return_value={"histories": []}),
    ):
        issues = collect_jira_activity(
            "alice@example.com",
            date(2026, 8, 8),
            date(2026, 8, 14),
            settings=settings,
        )
    assert len(issues) == 1
    assert issues[0]["key"] == "EET-5599"
    assert issues[0]["activity_role"] == "collaborator"
    assert issues[0]["comments"][0]["body"].startswith("Reproduced")


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


def test_normalize_created_issue() -> None:
    item = {
        "html_url": "https://github.com/org/repo/issues/7",
        "title": "EET-5002: document upgrade path",
        "body": "Capture the supported migration steps.",
        "created_at": "2026-08-11T00:00:00Z",
        "repository_url": "https://api.github.com/repos/org/repo",
    }
    normalized = _normalize_created_issue(item, date(2026, 8, 8), date(2026, 8, 14))
    assert normalized is not None
    assert normalized["type"] == "issue_created"
    assert normalized["repo"] == "org/repo"
    assert normalized["linked_issue_keys"] == ["EET-5002"]


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


def test_build_payload_includes_github_activity() -> None:
    payload = build_payload(
        "pilot",
        date(2026, 8, 14),
        [],
        [],
        commits=[{"summary": "EET-1: ship it", "repo": "org/repo"}],
        github_activity=[
            {
                "type": "pull_request_review",
                "url": "https://github.com/org/repo/pull/1#pullrequestreview-1",
            }
        ],
    )
    assert payload["commits"][0]["summary"] == "EET-1: ship it"
    assert payload["github_activity"][0]["type"] == "pull_request_review"


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

    with patch(
        "status.collectors.github.get_json",
        side_effect=[pr_response, commit_response, {"items": []}, {"items": []}, {"items": []}],
    ) as mock_get:
        activity = collect_github_activity(
            "pilot-user",
            date(2026, 8, 8),
            date(2026, 8, 14),
            settings=settings,
        )

    assert len(activity["pull_requests"]) == 1
    assert len(activity["commits"]) == 1
    assert activity["github_activity"] == []
    assert activity["commits"][0]["summary"] == "EET-2: commit"

    pr_query = mock_get.call_args_list[0][0][0]
    commit_query = mock_get.call_args_list[1][0][0]
    assert "repo%3A" not in pr_query
    assert "repo%3A" not in commit_query
    assert "author%3Apilot-user" in pr_query
    assert "committer-date%3A2026-08-08..2026-08-14" in commit_query
    assert "author%3Apilot-user" in commit_query


def test_collect_github_issue_comments_and_reviews_are_attributed() -> None:
    comment_candidate = {
        "html_url": "https://github.com/org/repo/issues/9",
        "title": "EET-5003: diagnose upgrade failure",
        "body": "Partner reports a failed upgrade.",
        "updated_at": "2026-08-11T00:00:00Z",
        "created_at": "2026-08-01T00:00:00Z",
        "comments_url": "https://api.github.com/repos/org/repo/issues/9/comments",
        "repository_url": "https://api.github.com/repos/org/repo",
    }
    comments = [
        {
            "id": 1,
            "html_url": "https://github.com/org/repo/issues/9#issuecomment-1",
            "user": {"login": "pilot-user"},
            "body": "Reproduced the failure and proposed a rollback check.",
            "created_at": "2026-08-12T00:00:00Z",
        },
        {
            "id": 2,
            "user": {"login": "someone-else"},
            "body": "Not the pilot's activity.",
            "created_at": "2026-08-12T00:00:00Z",
        },
    ]
    with patch(
        "status.collectors.github.get_json",
        side_effect=[{"items": []}, {"items": [comment_candidate]}, comments],
    ):
        activity = collect_github_issue_activity(
            "pilot-user",
            date(2026, 8, 8),
            date(2026, 8, 14),
            settings=Settings(GITHUB_TOKEN="ghp_test", GITHUB_MAX_COMMENTS=10),
        )
    assert len(activity) == 1
    assert activity[0]["type"] == "issue_comment"
    assert activity[0]["action"] == "commented"

    review_candidate = {
        "html_url": "https://github.com/org/repo/pull/10",
        "title": "EET-5004: update operator image",
        "body": "Refresh the image.",
        "updated_at": "2026-08-12T00:00:00Z",
        "repository_url": "https://api.github.com/repos/org/repo",
        "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/10"},
    }
    reviews = [
        {
            "id": 10,
            "html_url": "https://github.com/org/repo/pull/10#pullrequestreview-10",
            "user": {"login": "pilot-user"},
            "state": "CHANGES_REQUESTED",
            "body": "Please preserve the supported upgrade path.",
            "submitted_at": "2026-08-13T00:00:00Z",
        }
    ]
    inline_comments = [
        {
            "pull_request_review_id": 10,
            "user": {"login": "pilot-user"},
            "body": "EET-5005: the fallback must remain compatible with existing clusters.",
            "created_at": "2026-08-13T00:01:00Z",
        }
    ]
    with patch(
        "status.collectors.github.get_json",
        side_effect=[{"items": [review_candidate]}, reviews, inline_comments],
    ):
        review_activity = collect_github_pull_request_reviews(
            "pilot-user",
            date(2026, 8, 8),
            date(2026, 8, 14),
            settings=Settings(GITHUB_TOKEN="ghp_test", GITHUB_MAX_REVIEWS=10),
        )
    assert len(review_activity) == 1
    assert review_activity[0]["type"] == "pull_request_review"
    assert review_activity[0]["action"] == "changes_requested"
    assert review_activity[0]["review_comments"] == [
        "EET-5005: the fallback must remain compatible with existing clusters."
    ]
    assert review_activity[0]["linked_issue_keys"] == ["EET-5004", "EET-5005"]
