"""Draft generation from collector payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from status.skills.schemas import DraftOutput


def run_drafter(payload: dict[str, Any], *, dry_run: bool = False) -> DraftOutput:
    _ = dry_run
    return DraftOutput.model_validate(
        {
            "person": payload.get("person", "unknown"),
            "week_ending": payload.get("week_end", ""),
            "entries": [],
            "flags": ["dry-run: no skill invocation"] if dry_run else ["no DRAFTER_SKILL_ID configured"],
            "unticketed_prompt": "",
        }
    )


def load_fixture(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())
