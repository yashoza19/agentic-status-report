"""GitHub pull request and commit collectors."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any

from status.collectors.http import get_json, with_query
from status.config import Settings, get_settings

log = logging.getLogger(__name__)

ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
COMMITS_SEARCH_URL = "https://api.github.com/search/commits"
ISSUES_SEARCH_URL = "https://api.github.com/search/issues"
MAX_BODY_LENGTH = 2_000


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


def _bounded_body(value: object, maximum: int = MAX_BODY_LENGTH) -> str:
    body = str(value or "").strip()
    if len(body) <= maximum:
        return body
    return body[: maximum - 1].rstrip() + "…"


def _repo_from_item(item: dict[str, Any]) -> str:
    repo_url = str(item.get("repository_url") or "")
    if "/repos/" in repo_url:
        return repo_url.rsplit("/repos/", 1)[-1]
    return str(item.get("repo") or "")


def _normalize_pr(item: dict[str, Any], week_start: date, week_end: date) -> dict[str, Any] | None:
    updated = _parse_github_dt(item.get("updated_at"))
    created = _parse_github_dt(item.get("created_at"))
    if not (_in_window(updated, week_start, week_end) or _in_window(created, week_start, week_end)):
        return None

    pull_request = item.get("pull_request") or {}
    merged_at = pull_request.get("merged_at")
    state = "merged" if merged_at else ("draft" if item.get("draft") else "open")

    repo = _repo_from_item(item)

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


def _normalize_created_issue(
    item: dict[str, Any], week_start: date, week_end: date
) -> dict[str, Any] | None:
    if item.get("pull_request"):
        return None
    created = _parse_github_dt(item.get("created_at"))
    if not _in_window(created, week_start, week_end):
        return None
    title = str(item.get("title") or "")
    body = _bounded_body(item.get("body"))
    return {
        "type": "issue_created",
        "url": item.get("html_url", ""),
        "subject_url": item.get("html_url", ""),
        "title": title,
        "repo": _repo_from_item(item),
        "action": "created",
        "body": body,
        "occurred_at": item.get("created_at"),
        "linked_issue_keys": extract_issue_keys(f"{title}\n{body}"),
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
    normalize: Callable[[dict[str, Any], date, date], dict[str, Any] | None],
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


def _search_items(
    query: str,
    headers: dict[str, str],
    *,
    max_results: int,
    sort: str = "updated",
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    """Return bounded raw GitHub search results for follow-up API lookups."""
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        search_url = with_query(
            ISSUES_SEARCH_URL,
            {
                "q": query,
                "per_page": "100",
                "page": str(page),
                "sort": sort,
                "order": "desc",
            },
        )
        data = get_json(search_url, headers=headers)
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            break
        for item in items:
            key = str(item.get("html_url") or item.get("id") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(item)
            if len(results) >= max_results:
                return results
        if len(items) < 100:
            break
    return results


def _get_list_paginated(
    url: str,
    headers: dict[str, str],
    *,
    max_results: int,
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        page_url = with_query(url, {"per_page": "100", "page": str(page)})
        data = get_json(page_url, headers=headers)
        batch = data if isinstance(data, list) else []
        if not batch:
            break
        results.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100 or len(results) >= max_results:
            break
    return results[:max_results]


def collect_github_issue_activity(
    github_login: str,
    week_start: date,
    week_end: date,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Collect issues created and issue/PR conversation comments by a person."""
    settings = settings or get_settings()
    headers = _auth_header(settings)
    date_range = f"{week_start.isoformat()}..{week_end.isoformat()}"
    created = _search_paginated(
        ISSUES_SEARCH_URL,
        f"is:issue author:{github_login} created:{date_range}",
        headers,
        week_start,
        week_end,
        normalize=_normalize_created_issue,
        max_results=settings.github_max_issues,
        sort="created",
    )

    candidates = _search_items(
        f"commenter:{github_login} updated:{date_range}",
        headers,
        max_results=settings.github_max_comments,
    )
    comments: list[dict[str, Any]] = []
    login = github_login.lower()
    for item in candidates:
        comments_url = str(item.get("comments_url") or "")
        if not comments_url:
            continue
        remaining = settings.github_max_comments - len(comments)
        if remaining <= 0:
            break
        for comment in _get_list_paginated(comments_url, headers, max_results=500):
            if str((comment.get("user") or {}).get("login") or "").lower() != login:
                continue
            occurred = _parse_github_dt(comment.get("created_at"))
            if not _in_window(occurred, week_start, week_end):
                continue
            title = str(item.get("title") or "")
            body = _bounded_body(comment.get("body"))
            is_pr = bool(item.get("pull_request"))
            comments.append(
                {
                    "type": "pull_request_comment" if is_pr else "issue_comment",
                    "url": comment.get("html_url") or item.get("html_url", ""),
                    "subject_url": item.get("html_url", ""),
                    "title": title,
                    "repo": _repo_from_item(item),
                    "action": "commented",
                    "body": body,
                    "occurred_at": comment.get("created_at"),
                    "linked_issue_keys": extract_issue_keys(
                        f"{title}\n{item.get('body') or ''}\n{body}"
                    ),
                }
            )
            if len(comments) >= settings.github_max_comments:
                break
    return [*created, *comments]


def collect_github_pull_request_reviews(
    github_login: str,
    week_start: date,
    week_end: date,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Collect submitted pull-request reviews, excluding passive review requests."""
    settings = settings or get_settings()
    headers = _auth_header(settings)
    date_range = f"{week_start.isoformat()}..{week_end.isoformat()}"
    candidates = _search_items(
        f"is:pr reviewed-by:{github_login} updated:{date_range}",
        headers,
        max_results=settings.github_max_reviews,
    )
    login = github_login.lower()
    reviews: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        pull_api_url = str((item.get("pull_request") or {}).get("url") or "")
        if not pull_api_url:
            continue
        remaining = settings.github_max_reviews - len(reviews)
        if remaining <= 0:
            break
        review_rows = _get_list_paginated(
            f"{pull_api_url}/reviews",
            headers,
            max_results=500,
        )
        inline_rows = _get_list_paginated(
            f"{pull_api_url}/comments",
            headers,
            max_results=500,
        )
        inline_by_review: dict[str, list[str]] = {}
        for comment in inline_rows:
            if str((comment.get("user") or {}).get("login") or "").lower() != login:
                continue
            occurred = _parse_github_dt(comment.get("created_at"))
            if not _in_window(occurred, week_start, week_end):
                continue
            review_id = str(comment.get("pull_request_review_id") or "")
            body = _bounded_body(comment.get("body"))
            if review_id and body:
                inline_by_review.setdefault(review_id, []).append(body)
        for review in review_rows:
            if str((review.get("user") or {}).get("login") or "").lower() != login:
                continue
            submitted = _parse_github_dt(review.get("submitted_at"))
            if not _in_window(submitted, week_start, week_end):
                continue
            state = str(review.get("state") or "").lower()
            if not state or state == "pending":
                continue
            url = str(review.get("html_url") or item.get("html_url") or "")
            dedupe_key = str(review.get("id") or url)
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            title = str(item.get("title") or "")
            body = _bounded_body(review.get("body"))
            review_comments = inline_by_review.get(str(review.get("id") or ""), [])
            reviews.append(
                {
                    "type": "pull_request_review",
                    "url": url,
                    "subject_url": item.get("html_url", ""),
                    "title": title,
                    "repo": _repo_from_item(item),
                    "action": state,
                    "body": body,
                    "review_comments": review_comments,
                    "occurred_at": review.get("submitted_at"),
                    "linked_issue_keys": extract_issue_keys(
                        f"{title}\n{item.get('body') or ''}\n{body}\n"
                        + "\n".join(review_comments)
                    ),
                }
            )
            if len(reviews) >= settings.github_max_reviews:
                break
    return reviews


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
    """Collect authored and collaborative activity across repos visible to the token."""
    settings = settings or get_settings()
    pull_requests = collect_github_pull_requests(
        github_login,
        week_start,
        week_end,
        settings=settings,
    )
    commits = collect_github_commits(
        github_login,
        week_start,
        week_end,
        settings=settings,
    )
    collaboration = collect_github_issue_activity(
        github_login,
        week_start,
        week_end,
        settings=settings,
    )
    collaboration.extend(
        collect_github_pull_request_reviews(
            github_login,
            week_start,
            week_end,
            settings=settings,
        )
    )
    return {
        "pull_requests": pull_requests,
        "commits": commits,
        "github_activity": sorted(
            collaboration,
            key=lambda item: str(item.get("occurred_at") or ""),
            reverse=True,
        ),
    }
