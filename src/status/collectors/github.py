"""GitHub pull request collector."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from status.collectors.http import get_json, with_query
from status.config import Settings, get_settings

log = logging.getLogger(__name__)

ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


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


def _search_prs(
    query: str,
    headers: dict[str, str],
    week_start: date,
    week_end: date,
    *,
    max_results: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    page = 1

    while len(results) < max_results:
        url = with_query(
            "https://api.github.com/search/issues",
            {
                "q": query,
                "per_page": "100",
                "page": str(page),
                "sort": "updated",
                "order": "desc",
            },
        )
        data = get_json(url, headers=headers)
        items = data.get("items", [])
        if not items:
            break

        for item in items:
            normalized = _normalize_pr(item, week_start, week_end)
            if normalized:
                results.append(normalized)
            if len(results) >= max_results:
                break
        page += 1
        if page > 5:
            break

    return results


def collect_github_activity(
    github_login: str,
    week_start: date,
    week_end: date,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    headers = _auth_header(settings)
    date_range = f"{week_start.isoformat()}..{week_end.isoformat()}"

    repos = settings.github_repo_list
    all_prs: dict[str, dict[str, Any]] = {}

    if repos:
        for repo in repos:
            query = f"is:pr author:{github_login} repo:{repo} updated:{date_range}"
            for pr in _search_prs(
                query,
                headers,
                week_start,
                week_end,
                max_results=settings.github_max_prs,
            ):
                all_prs[pr["url"]] = pr
    else:
        query = f"is:pr author:{github_login} updated:{date_range}"
        for pr in _search_prs(
            query,
            headers,
            week_start,
            week_end,
            max_results=settings.github_max_prs,
        ):
            all_prs[pr["url"]] = pr

    return list(all_prs.values())
