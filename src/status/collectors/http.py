"""Lightweight JSON HTTP helpers for collectors."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")
        self.status = status
        self.url = url
        self.body = body


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    retries: int = 3,
    backoff_s: float = 1.0,
) -> Any:
    data = None
    req_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        req_headers.setdefault("Content-Type", "application/json")

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode(errors="replace")
            if exc.code in {429, 500, 502, 503, 504} and attempt < retries - 1:
                log.warning("retryable HTTP %s for %s (attempt %s)", exc.code, url, attempt + 1)
                time.sleep(backoff_s * (attempt + 1))
                last_error = HttpError(exc.code, url, err_body)
                continue
            raise HttpError(exc.code, url, err_body) from exc
        except urllib.error.URLError as exc:
            if attempt < retries - 1:
                time.sleep(backoff_s * (attempt + 1))
                last_error = exc
                continue
            raise

    if last_error:
        raise last_error
    raise RuntimeError(f"request failed for {url}")


def get_json(url: str, *, headers: dict[str, str] | None = None) -> Any:
    return request_json("GET", url, headers=headers)


def post_json(url: str, body: dict[str, Any], *, headers: dict[str, str] | None = None) -> Any:
    return request_json("POST", url, headers=headers, body=body)


def with_query(url: str, params: dict[str, str]) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    for key, value in params.items():
        query[key] = [value]
    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunparse(parsed._replace(query=new_query))
