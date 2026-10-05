"""Jira activity collector."""

from __future__ import annotations

import base64
import logging
import re
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any

from status.collectors.http import HttpError, get_json, post_json, with_query
from status.collectors.person import roster_jira_email_addresses
from status.config import Settings, get_settings

log = logging.getLogger(__name__)

ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
MAX_PERSON_COMMENTS_PER_ISSUE = 20


class JiraCollectorError(RuntimeError):
    pass


def _probe_jira_auth(base_url: str, email: str, api_token: str) -> bool:
    auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/rest/api/3/myself",
        headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.status) == 200
    except urllib.error.HTTPError:
        return False


@lru_cache
def _discover_jira_auth_email(base_url: str, api_token: str) -> str | None:
    """Find which roster email pairs with this API token (token owner discovery)."""
    for email in roster_jira_email_addresses():
        if _probe_jira_auth(base_url, email, api_token):
            log.info("discovered Jira API auth email %s from roster", email)
            return email
    return None


def jira_auth_email(settings: Settings) -> str | None:
    if settings.jira_auth_email:
        return settings.jira_auth_email
    if settings.jira_base_url and settings.jira_api_token:
        return _discover_jira_auth_email(settings.jira_base_url, settings.jira_api_token)
    return None


def _auth_header(settings: Settings) -> dict[str, str]:
    auth_email = jira_auth_email(settings)
    if not settings.jira_base_url or not auth_email or not settings.jira_api_token:
        raise JiraCollectorError(
            "Jira API credentials not configured. Set JIRA_API_TOKEN and either "
            "JIRA_API_EMAIL (your Atlassian account email) or ensure your email is "
            "listed in fixtures/eet-persons.json for automatic discovery."
        )
    token = base64.b64encode(f"{auth_email}:{settings.jira_api_token}".encode()).decode()
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
        dt = dt.replace(tzinfo=UTC)
    return start <= dt.date() <= end


def _person_matches(
    actor: dict[str, Any] | None,
    *,
    email: str | None,
    account_id: str | None,
) -> bool:
    if not actor:
        return False
    if account_id and str(actor.get("accountId") or "") == account_id:
        return True
    actor_email = str(actor.get("emailAddress") or "").lower()
    return bool(email and actor_email and actor_email == email.lower())


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
    *,
    author_email: str | None = None,
    author_account_id: str | None = None,
) -> list[dict[str, str]]:
    transitions: list[dict[str, str]] = []
    if not changelog:
        return transitions

    for history in changelog.get("histories", []):
        created = _parse_jira_dt(history.get("created"))
        if not _in_window(created, week_start, week_end):
            continue
        if (author_email or author_account_id) and not _person_matches(
            history.get("author"),
            email=author_email,
            account_id=author_account_id,
        ):
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
    author_email: str | None = None,
    author_account_id: str | None = None,
) -> list[dict[str, str]]:
    comments: list[dict[str, str]] = []
    comment_block = fields.get("comment", {})
    for comment in comment_block.get("comments", []):
        created = _parse_jira_dt(comment.get("created"))
        if not _in_window(created, week_start, week_end):
            continue
        author = comment.get("author", {})
        if (author_email or author_account_id) and not _person_matches(
            author,
            email=author_email,
            account_id=author_account_id,
        ):
            continue
        comments.append(
            {
                "author": author.get("displayName", author.get("accountId", "unknown")),
                "body": _bounded_jira_text(comment.get("body")),
                "at": created.isoformat() if created else comment.get("created", ""),
            }
        )
        if len(comments) >= MAX_PERSON_COMMENTS_PER_ISSUE:
            break
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


def _bounded_jira_text(body: Any, *, maximum: int = 2_000) -> str:
    """Flatten Jira text fields while keeping skill payloads bounded."""
    text = _comment_body(body).strip()
    if len(text) <= maximum:
        return text
    return text[: maximum - 1].rstrip() + "…"


def _in_progress_since(fields: dict[str, Any], changelog: dict[str, Any] | None) -> str | None:
    status = fields.get("status", {}).get("name", "")
    if status.lower() != "in progress":
        return None

    latest: datetime | None = None
    if changelog:
        for history in changelog.get("histories", []):
            created = _parse_jira_dt(history.get("created"))
            for item in history.get("items", []):
                if (
                    item.get("field") == "status"
                    and item.get("toString", "").lower() == "in progress"
                    and created
                    and (latest is None or created > latest)
                ):
                    latest = created
    return latest.isoformat() if latest else None


def normalize_jira_issue(
    issue: dict[str, Any],
    changelog: dict[str, Any] | None,
    week_start: date,
    week_end: date,
    *,
    jira_email: str | None = None,
    jira_account_id: str | None = None,
    epic_field: str | None = None,
) -> dict[str, Any]:
    fields = issue.get("fields", {})
    epic_key, epic_name = _epic_from_fields(fields, epic_field)
    updated = _parse_jira_dt(fields.get("updated"))
    assignee = fields.get("assignee") or {}
    reporter = fields.get("reporter") or {}
    is_assignee = _person_matches(
        assignee,
        email=jira_email,
        account_id=jira_account_id,
    )
    is_reporter = _person_matches(
        reporter,
        email=jira_email,
        account_id=jira_account_id,
    )
    transitions = _transitions_from_changelog(
        changelog,
        week_start,
        week_end,
        author_email=jira_email,
        author_account_id=jira_account_id,
    )
    comments = _comments_in_window(
        fields,
        week_start,
        week_end,
        author_email=jira_email,
        author_account_id=jira_account_id,
    )
    activity_role = "owner" if is_assignee or is_reporter else "collaborator"

    return {
        "key": issue.get("key", ""),
        "summary": fields.get("summary", ""),
        "description": _bounded_jira_text(fields.get("description")),
        "issue_type": fields.get("issuetype", {}).get("name", ""),
        "status": fields.get("status", {}).get("name", ""),
        "epic_key": epic_key,
        "epic_name": epic_name,
        "project": fields.get("project", {}).get("key", ""),
        "assignee_display_name": assignee.get("displayName"),
        "reporter_display_name": reporter.get("displayName"),
        "is_assignee": is_assignee,
        "is_reporter": is_reporter,
        "activity_role": activity_role,
        "transitions": transitions,
        "comments": comments,
        "last_updated": updated.isoformat() if updated else fields.get("updated", ""),
        "in_progress_since": _in_progress_since(fields, changelog),
    }


def build_jql(
    jira_email: str,
    week_start: date,
    week_end: date,
    *,
    projects: list[str] | None = None,
) -> str:
    start = week_start.isoformat()
    end = week_end.isoformat()

    if jira_email in {"currentUser", "me"}:
        person_clauses = [
            "assignee = currentUser()",
            "reporter = currentUser()",
            "watcher = currentUser()",
            "worklogAuthor = currentUser()",
        ]
    else:
        # Email works for assignee/reporter on Red Hat Jira; commentedBy() does not.
        person_clauses = [
            f'assignee = "{jira_email}"',
            f'reporter = "{jira_email}"',
        ]

    activity = "(" + " OR ".join(person_clauses) + ")"
    date_clause = f'updated >= "{start}" AND updated <= "{end}"'

    clauses = [activity, date_clause]
    if projects:
        quoted = ", ".join(projects)
        clauses.insert(0, f"project in ({quoted})")

    return " AND ".join(clauses) + " ORDER BY updated DESC"


def build_collaboration_jql(
    week_start: date,
    week_end: date,
    *,
    projects: list[str],
) -> str:
    """Find recently active project issues that may contain a person's comments."""
    if not projects:
        raise ValueError("projects are required for collaboration discovery")
    quoted = ", ".join(projects)
    return (
        f"project in ({quoted}) AND updated >= \"{week_start.isoformat()}\" "
        f"AND updated <= \"{week_end.isoformat()}\" ORDER BY updated DESC"
    )


def _resolve_jira_account_id(
    base: str,
    jira_email: str,
    headers: dict[str, str],
) -> str | None:
    url = with_query(
        f"{base}/rest/api/3/user/search",
        {"query": jira_email, "maxResults": "20"},
    )
    try:
        data = get_json(url, headers=headers)
    except Exception as exc:  # noqa: BLE001 - email matching remains a safe fallback
        log.warning("failed to resolve Jira account id for %s: %s", jira_email, exc)
        return None
    users = data if isinstance(data, list) else []
    for user in users:
        if str(user.get("emailAddress") or "").lower() == jira_email.lower():
            return str(user.get("accountId") or "") or None
    if len(users) == 1:
        return str(users[0].get("accountId") or "") or None
    return None


def _search_jira_issues(
    base: str,
    jql: str,
    fields: list[str],
    headers: dict[str, str],
    *,
    max_results: int,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    page_size = min(50, max_results)
    next_page_token: str | None = None
    while len(issues) < max_results:
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
    return issues[:max_results]


def _fetch_jira_comments(
    base: str,
    issue_key: str,
    headers: dict[str, str],
    *,
    max_results: int = 500,
) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    start_at = 0
    page_size = 100
    while len(comments) < max_results:
        url = with_query(
            f"{base}/rest/api/3/issue/{issue_key}/comment",
            {"startAt": str(start_at), "maxResults": str(page_size)},
        )
        data = get_json(url, headers=headers)
        batch = data.get("comments", []) if isinstance(data, dict) else []
        comments.extend(batch)
        total = int(data.get("total", len(comments))) if isinstance(data, dict) else len(comments)
        if not batch or len(comments) >= total:
            break
        start_at += len(batch)
    return comments[:max_results]


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
    base = (settings.jira_base_url or "").rstrip("/")
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
                try:
                    get_json(f"{base}/rest/api/3/project/{project}", headers=headers)
                except HttpError as exc:
                    if exc.status in {401, 403, 404}:
                        blocked.append(project)
                    else:
                        raise JiraCollectorError(
                            f"Jira API error probing project {project}: {exc}"
                        ) from exc
        except HttpError as exc:
            if exc.status in {401, 403, 404}:
                blocked.append(project)
            else:
                raise JiraCollectorError(
                    f"Jira API error probing project {project}: {exc}"
                ) from exc
        except Exception as exc:
            raise JiraCollectorError(
                f"Jira API probe failed for project {project}: {exc}"
            ) from exc

    return blocked


def collect_jira_activity(
    jira_email: str,
    week_start: date,
    week_end: date,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    headers = _auth_header(settings)
    base = (settings.jira_base_url or "").rstrip("/")
    owned_jql = build_jql(
        jira_email,
        week_start,
        week_end,
        projects=settings.jira_project_list or None,
    )

    blocked = check_project_access(settings.jira_project_list, settings=settings)
    if blocked:
        raise JiraCollectorError(
            f"No API access to Jira project(s): {', '.join(blocked)}. "
            f"Your token ({jira_auth_email(settings)}) needs Browse permission on those projects. "
            "Ask a Jira admin to grant access, then retry."
        )

    fields = [
        "summary",
        "description",
        "status",
        "issuetype",
        "project",
        "comment",
        "parent",
        "updated",
        "assignee",
        "reporter",
    ]
    if settings.jira_epic_field:
        fields.append(settings.jira_epic_field)

    owned_issues = _search_jira_issues(
        base,
        owned_jql,
        fields,
        headers,
        max_results=settings.jira_max_issues,
    )
    issues_by_key = {
        str(issue.get("key")): issue for issue in owned_issues if issue.get("key")
    }

    # Jira Cloud JQL cannot reliably search comments by another user's email.
    # Search only configured projects for recently updated candidates, then
    # verify authorship against fully paginated comments before retaining them.
    if settings.jira_project_list:
        collaboration_jql = build_collaboration_jql(
            week_start,
            week_end,
            projects=settings.jira_project_list,
        )
        candidates = _search_jira_issues(
            base,
            collaboration_jql,
            fields,
            headers,
            max_results=settings.jira_max_collaboration_issues,
        )
        for issue in candidates:
            key = str(issue.get("key") or "")
            if key:
                issues_by_key.setdefault(key, issue)

    jira_account_id = _resolve_jira_account_id(base, jira_email, headers)

    normalized: list[dict[str, Any]] = []
    for issue in issues_by_key.values():
        issue_key = issue.get("key")
        if not issue_key:
            continue

        try:
            all_comments = _fetch_jira_comments(base, str(issue_key), headers)
        except Exception as exc:  # noqa: BLE001 - embedded comments remain a fallback
            log.warning("failed to fetch comments for %s: %s", issue_key, exc)
            all_comments = list(
                (issue.get("fields", {}).get("comment") or {}).get("comments", [])
            )
        issue_fields = issue.setdefault("fields", {})
        issue_fields["comment"] = {"comments": all_comments}

        assignee = issue_fields.get("assignee") or {}
        reporter = issue_fields.get("reporter") or {}
        is_owner = _person_matches(
            assignee,
            email=jira_email,
            account_id=jira_account_id,
        ) or _person_matches(
            reporter,
            email=jira_email,
            account_id=jira_account_id,
        )
        own_comments = _comments_in_window(
            issue_fields,
            week_start,
            week_end,
            author_email=jira_email,
            author_account_id=jira_account_id,
        )
        if not is_owner and not own_comments:
            continue

        try:
            changelog_data = get_json(
                f"{base}/rest/api/3/issue/{issue_key}/changelog",
                headers=headers,
            )
            changelog = changelog_data if changelog_data else None
        except Exception as exc:  # noqa: BLE001 - preserve issue collection without changelog
            log.warning("failed to fetch changelog for %s: %s", issue_key, exc)
            changelog = None

        normalized.append(
            normalize_jira_issue(
                issue,
                changelog,
                week_start,
                week_end,
                jira_email=jira_email,
                jira_account_id=jira_account_id,
                epic_field=settings.jira_epic_field,
            )
        )

    return normalized


def filter_person_jira_issues(
    issues: list[dict[str, Any]],
    jira_email: str | None,
) -> list[dict[str, Any]]:
    """Keep owned issues plus issues with activity attributable to the person."""
    if not jira_email:
        return issues

    kept: list[dict[str, Any]] = []
    for issue in issues:
        if (
            issue.get("is_assignee")
            or issue.get("is_reporter")
            or issue.get("transitions")
            or issue.get("comments")
        ):
            kept.append(issue)
        else:
            log.debug(
                "dropping %s — assignee is %s, not %s",
                issue.get("key"),
                issue.get("assignee_display_name"),
                jira_email,
            )
    return kept


def fetch_jira_summaries(
    keys: list[str],
    *,
    settings: Settings | None = None,
) -> dict[str, str]:
    """Fetch issue summaries for explicit keys (used to label evidence at synthesize time)."""
    unique = sorted({key for key in keys if ISSUE_KEY_RE.match(key)})
    if not unique:
        return {}

    settings = settings or get_settings()
    headers = _auth_header(settings)
    base = (settings.jira_base_url or "").rstrip("/")
    jql = "key in (" + ", ".join(unique) + ")"
    data = post_json(
        f"{base}/rest/api/3/search/jql",
        {"jql": jql, "maxResults": len(unique), "fields": ["summary"]},
        headers=headers,
    )

    summaries: dict[str, str] = {}
    for issue in data.get("issues", []):
        key = issue.get("key")
        summary = issue.get("fields", {}).get("summary")
        if key and summary:
            summaries[str(key)] = str(summary).strip()
    return summaries
