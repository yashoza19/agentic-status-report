"""Skill upload, versioning, and invocation.

Two custom Skills back this pipeline:

    skills/weekly-status-drafter/     payload  -> draft entries
    skills/weekly-status-synthesizer/ entries  -> management report

Both are uploaded to the workspace via the Skills API and invoked through the
Messages API `container` parameter. Skills require the code execution tool.

IMPORTANT: the skill container has no network access and cannot install
packages. Skills receive an already-assembled payload; all Jira and GitHub
fetching happens in `status.collectors` before we get here.

Skills are GA on the Claude API and no longer require a beta header. Depending
on your installed SDK version the management surface may be `client.skills` or
`client.beta.skills`; `_skills_api()` below resolves whichever is present.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import anthropic
from anthropic.lib import files_from_dir
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

CODE_EXECUTION_TOOL = {"type": "code_execution_20250825", "name": "code_execution"}

# Skills may pause long operations; resume by replaying the container id.
MAX_PAUSE_RESUMES = 5

FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass(frozen=True)
class SkillRef:
    """A pinned skill. `version` should be an explicit version id in
    production — 'latest' means anyone publishing to the workspace changes
    what this job runs, mid-week, with no deploy."""

    skill_id: str
    version: str = "latest"
    type: Literal["custom", "anthropic"] = "custom"

    def as_container_entry(self) -> dict[str, str]:
        return {"type": self.type, "skill_id": self.skill_id, "version": self.version}

    @property
    def prompt_version(self) -> str:
        """Stamped onto every generated row so we can segment quality metrics
        by skill version later. Without this, comparing prompt revisions is
        guesswork."""
        return f"{self.skill_id}@{self.version}"


class SkillClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-5",
        max_tokens: int = 8000,
    ) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    # -- management ---------------------------------------------------------

    def _skills_api(self):
        return getattr(self._client, "skills", None) or self._client.beta.skills

    def upload(self, skill_dir: Path, display_name: str | None = None) -> str:
        """Create a new skill from a directory containing SKILL.md at its root.

        Returns the skill_01... id. Record it in config; it is stable across
        versions.
        """
        kwargs: dict[str, Any] = {"files": files_from_dir(str(skill_dir))}
        if display_name:
            kwargs["display_name"] = display_name
        skill = self._skills_api().create(**kwargs)
        log.info("created skill %s from %s", skill.id, skill_dir)
        return skill.id

    def publish_version(self, skill_id: str, skill_dir: Path) -> str:
        """Publish a new version and return its version id.

        A version is a complete snapshot, not a delta — the full file set is
        re-uploaded every time. Files omitted here are NOT carried over from
        the previous version, and the `name` in SKILL.md must still match the
        skill's existing name.
        """
        version = self._skills_api().versions.create(
            skill_id=skill_id,
            files=files_from_dir(str(skill_dir)),
        )
        log.info("published %s version %s", skill_id, version.version)
        return str(version.version)

    def list_custom(self) -> list[Any]:
        return list(self._skills_api().list(source="custom"))

    # -- invocation ---------------------------------------------------------

    def invoke_json(
        self,
        skill: SkillRef,
        payload: dict[str, Any],
        instruction: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        """Run a skill over a payload and parse its JSON response.

        The skill returns JSON as text rather than writing a file. Writing to
        the container and downloading via the Files API is more robust for
        large outputs, but adds a round trip we don't need at this size — a
        week of entries for one person is a few KB.
        """
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": f"{instruction}\n\n<payload>\n{json.dumps(payload)}\n</payload>",
            }
        ]

        container: dict[str, Any] = {"skills": [skill.as_container_entry()]}
        response = self._create(messages, container)

        # Long-running skill operations pause the turn; replay to continue.
        for _ in range(MAX_PAUSE_RESUMES):
            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})
            response = self._create(
                messages,
                {"id": response.container.id, "skills": [skill.as_container_entry()]},
            )
        else:
            raise SkillError(f"{skill.skill_id} did not settle after {MAX_PAUSE_RESUMES} resumes")

        return self._parse(response, schema, skill)

    def _create(self, messages: list[dict[str, Any]], container: dict[str, Any]):
        try:
            return self._client.beta.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                container=container,
                messages=messages,
                tools=[CODE_EXECUTION_TOOL],
            )
        except anthropic.BadRequestError as exc:
            if "skill" in str(exc).lower():
                raise SkillError(f"skill rejected: {exc}") from exc
            raise

    def _parse(self, response, schema: type[BaseModel], skill: SkillRef) -> BaseModel:
        text = "\n".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()

        if not text:
            raise SkillError(f"{skill.skill_id} returned no text output")

        cleaned = FENCE_RE.sub("", text).strip()

        try:
            raw = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            log.error("unparseable output from %s: %.500s", skill.skill_id, cleaned)
            raise SkillError(f"{skill.skill_id} returned non-JSON output") from exc

        try:
            return schema.model_validate(raw)
        except ValidationError as exc:
            log.error("schema violation from %s: %s", skill.skill_id, exc)
            raise SkillError(f"{skill.skill_id} output failed validation") from exc


class SkillError(RuntimeError):
    """Raised when a skill returns something we refuse to persist.

    Callers must NOT fall back to persisting partial or repaired output. An
    empty draft with a flag is recoverable — a human writes their own entry.
    A silently mangled draft is not: it reaches management looking correct.
    """
