from datetime import date

from status.collectors.payload import build_payload, week_bounds


def test_week_bounds_friday() -> None:
    week_ending = date(2026, 8, 14)  # Friday
    start, end = week_bounds(week_ending)
    assert start == date(2026, 8, 8)
    assert end == week_ending


def test_build_payload_shape() -> None:
    week_ending = date(2026, 8, 14)
    payload = build_payload("yash", week_ending, [], [])
    assert payload["person"] == "yash"
    assert payload["week_start"] == "2026-08-08"
    assert payload["week_end"] == "2026-08-14"
    assert payload["jira_issues"] == []
    assert payload["pull_requests"] == []
    assert payload["commits"] == []
