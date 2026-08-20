"""Jira activity collector."""

from __future__ import annotations

import base64
import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from status.collectors.http import get_json, post_json
from status.config import Settings, get_settings

log = logging.getLogger(__name__)

ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
ACCOUNT_ID_RE = re.compile(r"^\d+:[0-9a-f-]+$|^[0-9a-f]{24}$", re.IGNORECASE)


class JiraCollectorError(RuntimeError):
    pass


def _auth_header(settings: Settings) -> dict[str, str]:
    if not settings.jira_base_url or not settings.jira_email or not settings.jira_api_token:
        raise JiraCollectorError(
            "Jira credentials not configured. Set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN."
        )
    token = base64.b64encode(f"{settings.jira_email}:{settings.jira_api_token}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
    }


def _parse_jira_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    # Jira returns 2026-08-14T12:34:56.789+0000
    normalized = value.replace("+0000", "+00:00")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _in_window(dt: datetime | None, start: date, end: date) -> bool:
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return start <= dt.date() <= end


def _epic_from_fields(fields: dict[str, Any], epic_field: str | None) -> tuple[str | None, str | None]:
    parent = fields.get("parent")
    if isinstance(parent, dict):
        parent_type = parent.get("fields", {}).get("issuetype", {}).get("name", "")
        if parent_type == "Epic" or ISSUE_KEY_RE.match(parent.get("key", "")):
            return parent.get("key"), parent.get("fields", {}).get("summary")

    if epic_field:
        epic = fields.get(epic_field)
        if isinstance(epic, dict):
            return epic.get("key"), epic.get("fields", {}).get("summary")
        if isinstance(epic, str) and ISSUE_KEY_RE.match(epic):
            return epic, None

    for key, value in fields.items():
        if not key.startswith("customfield_"):
            continue
        if isinstance(value, dict) and ISSUE_KEY_RE.match(value.get("key", "")):
            return value.get("key"), value.get("fields", {}).get("summary")

    return None, None


def _transitions_from_changelog(
    changelog: dict[str, Any] | None,
    week_start: date,
    week_end: date,
) -> list[dict[str, str]]:
    transitions: list[dict[str, str]] = []
    if not changelog:
        return transitions

    for history in changelog.get("histories", []):
        created = _parse_jira_dt(history.get("created"))
        if not _in_window(created, week_start, week_end):
            continue
        for item in history.get("items", []):
            if item.get("field") != "status":
                continue
            transitions.append(
                {
                    "to": str(item.get("toString", "")),
                    "at": created.isoformat() if created else history.get("created", ""),
                }
            )
    return transitions


def _comments_in_window(
    fields: dict[str, Any],
    week_start: date,
    week_end: date,
    *,
    author_account_id: str | None = None,
) -> list[dict[str, str]]:
    comments: list[dict[str, str]] = []
    comment_block = fields.get("comment", {})
    for comment in comment_block.get("comments", []):
        created = _parse_jira_dt(comment.get("created"))
        if not _in_window(created, week_start, week_end):
            continue
        author = comment.get("author", {})
        if author_account_id and author.get("accountId") != author_account_id:
            continue
        comments.append(
            {
                "author": author.get("displayName", author.get("accountId", "unknown")),
                "body": _comment_body(comment.get("body")),
                "at": created.isoformat() if created else comment.get("created", ""),
            }
        )
    return comments


def _comment_body(body: Any) -> str:
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        # Atlassian Document Format
        texts: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "text":
                    texts.append(str(node.get("text", "")))
                for child in node.get("content", []):
                    walk(child)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(body)
        return "".join(texts).strip()
    return str(body or "")


def _in_progress_since(fields: dict[str, Any], changelog: dict[str, Any] | None) -> str | None:
    status = fields.get("status", {}).get("name", "")
    if status.lower() != "in progress":
        return None

    latest: datetime | None = None
    if changelog:
        for history in changelog.get("histories", []):
            created = _parse_jira_dt(history.get("created"))
            for item in history.get("items", []):
                if item.get("field") == "status" and item.get("toString", "").lower() == "in progress":
                    if created and (latest is None or created > latest):
                        latest = created
    return latest.isoformat() if latest else None


def normalize_jira_issue(
    issue: dict[str, Any],
    changelog: dict[str, Any] | None,
    week_start: date,
    week_end: date,
    *,
    jira_account_id: str | None = None,
    epic_field: str | None = None,
) -> dict[str, Any]:
    fields = issue.get("fields", {})
    epic_key, epic_name = _epic_from_fields(fields, epic_field)
    updated = _parse_jira_dt(fields.get("updated"))

    return {
        "key": issue.get("key", ""),
        "summary": fields.get("summary", ""),
        "issue_type": fields.get("issuetype", {}).get("name", ""),
        "status": fields.get("status", {}).get("name", ""),
        "epic_key": epic_key,
        "epic_name": epic_name,
        "project": fields.get("project", {}).get("key", ""),
        "transitions": _transitions_from_changelog(changelog, week_start, week_end),
        "comments": _comments_in_window(
            fields,
            week_start,
            week_end,
            author_account_id=jira_account_id,
        ),
        "last_updated": updated.isoformat() if updated else fields.get("updated", ""),
        "in_progress_since": _in_progress_since(fields, changelog),
    }


def build_jql(
    jira_account_id: str,
    week_start: date,
    week_end: date,
    *,
    projects: list[str] | None = None,
) -> str:
    start = week_start.isoformat()
    end = week_end.isoformat()

    if jira_account_id in {"currentUser", "me"}:
        person_clauses = [
            "assignee = currentUser()",
            "reporter = currentUser()",
            "watcher = currentUser()",
            "worklogAuthor = currentUser()",
        ]
    elif ACCOUNT_ID_RE.match(jira_account_id):
        # Red Hat EET tickets often list involvement via Developer / Contributors fields.
        person_clauses = [
            f'assignee = "{jira_account_id}"',
            f'reporter = "{jira_account_id}"',
            f'watcher = "{jira_account_id}"',
            f'worklogAuthor = "{jira_account_id}"',
            f'"Developer" = "{jira_account_id}"',
            f'"Contributors" = "{jira_account_id}"',
        ]
    else:
        # Email works for assignee/reporter on Red Hat Jira; commentedBy() does not.
        person_clauses = [
            f'assignee = "{jira_account_id}"',
            f'reporter = "{jira_account_id}"',
        ]

    activity = "(" + " OR ".join(person_clauses) + ")"
    date_clause = f'updated >= "{start}" AND updated <= "{end}"'

    clauses = [activity, date_clause]
    if projects:
        quoted = ", ".join(projects)
        clauses.insert(0, f"project in ({quoted})")

    return " AND ".join(clauses) + " ORDER BY updated DESC"


def check_project_access(
    projects: list[str],
    *,
    settings: Settings | None = None,
) -> list[str]:
    """Return project keys the API token cannot read."""
    if not projects:
        return []

    settings = settings or get_settings()
    headers = _auth_header(settings)
    base = settings.jira_base_url.rstrip("/")
    blocked: list[str] = []

    for project in projects:
        jql = f'project = {project} ORDER BY updated DESC'
        try:
            data = post_json(
                f"{base}/rest/api/3/search/jql",
                {"jql": jql, "maxResults": 1, "fields": ["summary"]},
                headers=headers,
            )
            if not data.get("issues") and data.get("isLast", True):
                # Could be empty project or no permission — probe project metadata.
                try:
                    get_json(f"{base}/rest/api/3/project/{project}", headers=headers)
                except Exception:
                    blocked.append(project)
        except Exception:
            blocked.append(project)

    return blocked


def collect_jira_activity(
    jira_account_id: str,
    week_start: date,
    week_end: date,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    headers = _auth_header(settings)
    base = settings.jira_base_url.rstrip("/")
    jql = build_jql(
        jira_account_id,
        week_start,
        week_end,
        projects=settings.jira_project_list or None,
    )

    blocked = check_project_access(settings.jira_project_list, settings=settings)
    if blocked:
        raise JiraCollectorError(
            f"No API access to Jira project(s): {', '.join(blocked)}. "
            f"Your token ({settings.jira_email}) needs Browse permission on those projects. "
            "Ask a Jira admin to grant access, then retry."
        )

    fields = [
        "summary",
        "status",
        "issuetype",
        "project",
        "comment",
        "parent",
        "updated",
    ]
    if settings.jira_epic_field:
        fields.append(settings.jira_epic_field)

    issues: list[dict[str, Any]] = []
    page_size = 50
    next_page_token: str | None = None

    while True:
        payload: dict[str, Any] = {
            "jql": jql,
            "maxResults": page_size,
            "fields": fields,
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        data = post_json(f"{base}/rest/api/3/search/jql", payload, headers=headers)
        batch = data.get("issues", [])
        issues.extend(batch)

        if data.get("isLast", True) or not batch:
            break
        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break
        if len(issues) >= settings.jira_max_issues:
            break

    normalized: list[dict[str, Any]] = []
    for issue in issues[: settings.jira_max_issues]:
        key = issue.get("key")
        if not key:
            continue
        try:
            changelog_data = get_json(
                f"{base}/rest/api/3/issue/{key}/changelog",
                headers=headers,
            )
            changelog = changelog_data if changelog_data else None
        except Exception as exc:
            log.warning("failed to fetch changelog for %s: %s", key, exc)
            changelog = None

        normalized.append(
            normalize_jira_issue(
                issue,
                changelog,
                week_start,
                week_end,
                jira_account_id=jira_account_id,
                epic_field=settings.jira_epic_field,
            )
        )

    return normalized
