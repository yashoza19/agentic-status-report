"""Skill upload, versioning, and invocation."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    import anthropic

log = logging.getLogger(__name__)

CODE_EXECUTION_TOOL = {"type": "code_execution_20250825", "name": "code_execution"}
MAX_PAUSE_RESUMES = 5
FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _import_anthropic():
    import anthropic as anthropic_module

    try:
        from anthropic.lib import files_from_dir
    except ImportError as exc:
        raise SkillError(
            "anthropic>=0.49 is required for Skills API support. "
            "Run: pip install -U 'anthropic>=0.49'"
        ) from exc
    return anthropic_module, files_from_dir


@dataclass(frozen=True)
class SkillRef:
    skill_id: str
    version: str = "latest"
    type: Literal["custom", "anthropic"] = "custom"

    def as_container_entry(self) -> dict[str, str]:
        return {"type": self.type, "skill_id": self.skill_id, "version": self.version}

    @property
    def prompt_version(self) -> str:
        return f"{self.skill_id}@{self.version}"


class SkillClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-5",
        max_tokens: int = 8000,
    ) -> None:
        anthropic_module, _ = _import_anthropic()
        self._anthropic = anthropic_module
        self._client = anthropic_module.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def _skills_api(self):
        return getattr(self._client, "skills", None) or self._client.beta.skills

    def upload(self, skill_dir: Path, display_name: str | None = None) -> str:
        _, files_from_dir = _import_anthropic()
        kwargs: dict[str, Any] = {"files": files_from_dir(str(skill_dir))}
        if display_name:
            kwargs["display_name"] = display_name
        skill = self._skills_api().create(**kwargs)
        log.info("created skill %s from %s", skill.id, skill_dir)
        return skill.id

    def publish_version(self, skill_id: str, skill_dir: Path) -> str:
        _, files_from_dir = _import_anthropic()
        version = self._skills_api().versions.create(
            skill_id=skill_id,
            files=files_from_dir(str(skill_dir)),
        )
        log.info("published %s version %s", skill_id, version.version)
        return str(version.version)

    def list_custom(self) -> list[Any]:
        return list(self._skills_api().list(source="custom"))

    def invoke_json(
        self,
        skill: SkillRef,
        payload: dict[str, Any],
        instruction: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": f"{instruction}\n\n<payload>\n{json.dumps(payload)}\n</payload>",
            }
        ]

        container: dict[str, Any] = {"skills": [skill.as_container_entry()]}
        response = self._create(messages, container)
        response = self._resume_to_completion(messages, response, skill)

        try:
            return self._parse(response, schema, skill)
        except SkillError as exc:
            if "no text output" not in str(exc):
                raise
            log.warning("%s returned no text; requesting JSON-only follow-up", skill.skill_id)
            messages.append({"role": "assistant", "content": response.content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Reply with ONLY the final JSON object as plain text in a text block. "
                        "No code execution, markdown fences, or commentary."
                    ),
                }
            )
            response = self._create(
                messages,
                {"id": response.container.id, "skills": [skill.as_container_entry()]},
            )
            response = self._resume_to_completion(messages, response, skill)
            return self._parse(response, schema, skill)

    def _resume_to_completion(self, messages: list[dict[str, Any]], response, skill: SkillRef):
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
        return response

    def _create(self, messages: list[dict[str, Any]], container: dict[str, Any]):
        try:
            return self._client.beta.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                container=container,
                messages=messages,
                tools=[CODE_EXECUTION_TOOL],
            )
        except self._anthropic.BadRequestError as exc:
            if "skill" in str(exc).lower():
                raise SkillError(f"skill rejected: {exc}") from exc
            raise

    def _parse(self, response, schema: type[BaseModel], skill: SkillRef) -> BaseModel:
        text = "\n".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()

        if not text:
            block_types = [getattr(block, "type", None) for block in response.content]
            log.error("%s returned no text output; blocks=%s stop_reason=%s", skill.skill_id, block_types, response.stop_reason)
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
    """Raised when a skill returns something we refuse to persist."""
