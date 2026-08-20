"""GitHub pull request and commit collectors."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from status.collectors.http import get_json, with_query
from status.config import Settings, get_settings

log = logging.getLogger(__name__)

ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
COMMITS_SEARCH_URL = "https://api.github.com/search/commits"
ISSUES_SEARCH_URL = "https://api.github.com/search/issues"


class GitHubCollectorError(RuntimeError):
    pass


def _auth_header(settings: Settings) -> dict[str, str]:
    if not settings.github_token:
        raise GitHubCollectorError("GITHUB_TOKEN not configured.")
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_github_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _in_window(dt: datetime | None, start: date, end: date) -> bool:
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return start <= dt.date() <= end


def extract_issue_keys(text: str) -> list[str]:
    return sorted(set(ISSUE_KEY_RE.findall(text)))


def commit_summary(message: str) -> str:
    return message.split("\n", 1)[0].strip()


def _normalize_pr(item: dict[str, Any], week_start: date, week_end: date) -> dict[str, Any] | None:
    updated = _parse_github_dt(item.get("updated_at"))
    created = _parse_github_dt(item.get("created_at"))
    if not (_in_window(updated, week_start, week_end) or _in_window(created, week_start, week_end)):
        return None

    pull_request = item.get("pull_request") or {}
    merged_at = pull_request.get("merged_at")
    state = "merged" if merged_at else ("draft" if item.get("draft") else "open")

    repo_url = item.get("repository_url", "")
    repo = repo_url.rsplit("/repos/", 1)[-1] if "/repos/" in repo_url else item.get("repo", "")

    body = item.get("body") or ""
    title = item.get("title") or ""
    linked = extract_issue_keys(f"{title}\n{body}")

    return {
        "url": item.get("html_url", ""),
        "title": title,
        "repo": repo,
        "state": state,
        "merged_at": merged_at,
        "linked_issue_keys": linked,
    }


def _normalize_commit(item: dict[str, Any], week_start: date, week_end: date) -> dict[str, Any] | None:
    commit = item.get("commit") or {}
    committer = commit.get("committer") or {}
    committed_at = _parse_github_dt(committer.get("date"))
    if not _in_window(committed_at, week_start, week_end):
        return None

    message = commit.get("message") or ""
    summary = commit_summary(message)
    if not summary:
        return None

    repository = item.get("repository") or {}
    repo = repository.get("full_name") or ""

    return {
        "sha": item.get("sha", ""),
        "url": item.get("html_url", ""),
        "summary": summary,
        "message": message,
        "repo": repo,
        "committed_at": committer.get("date"),
        "linked_issue_keys": extract_issue_keys(message),
    }


def _search_paginated(
    url: str,
    query: str,
    headers: dict[str, str],
    week_start: date,
    week_end: date,
    *,
    normalize,
    max_results: int,
    max_pages: int = 5,
    sort: str = "updated",
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1

    while len(results) < max_results and page <= max_pages:
        search_url = with_query(
            url,
            {
                "q": query,
                "per_page": "100",
                "page": str(page),
                "sort": sort,
                "order": "desc",
            },
        )
        data = get_json(search_url, headers=headers)
        items = data.get("items", [])
        if not items:
            break

        for item in items:
            normalized = normalize(item, week_start, week_end)
            if normalized is None:
                continue
            key = normalized.get("url") or normalized.get("sha")
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(normalized)
            if len(results) >= max_results:
                break
        page += 1

    return results


def collect_github_pull_requests(
    github_login: str,
    week_start: date,
    week_end: date,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    headers = _auth_header(settings)
    date_range = f"{week_start.isoformat()}..{week_end.isoformat()}"
    query = f"is:pr author:{github_login} updated:{date_range}"
    return _search_paginated(
        ISSUES_SEARCH_URL,
        query,
        headers,
        week_start,
        week_end,
        normalize=_normalize_pr,
        max_results=settings.github_max_prs,
    )


def collect_github_commits(
    github_login: str,
    week_start: date,
    week_end: date,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    headers = _auth_header(settings)
    date_range = f"{week_start.isoformat()}..{week_end.isoformat()}"
    query = f"author:{github_login} committer-date:{date_range}"
    return _search_paginated(
        COMMITS_SEARCH_URL,
        query,
        headers,
        week_start,
        week_end,
        normalize=_normalize_commit,
        max_results=settings.github_max_commits,
        sort="committer-date",
    )


def collect_github_activity(
    github_login: str,
    week_start: date,
    week_end: date,
    *,
    settings: Settings | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Collect PRs and commits for a user across all repos visible to the token."""
    settings = settings or get_settings()
    return {
        "pull_requests": collect_github_pull_requests(
            github_login,
            week_start,
            week_end,
            settings=settings,
        ),
        "commits": collect_github_commits(
            github_login,
            week_start,
            week_end,
            settings=settings,
        ),
    }
