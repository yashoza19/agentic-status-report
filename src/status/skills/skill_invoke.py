"""Route skill JSON invocation to Anthropic or OpenAI hosted skills."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from status.config import Settings, get_settings
from status.skills.client import SkillClient, SkillError, SkillRef
from status.skills.openai_skills import OpenAISkillError, OpenAISkillRef, OpenAISkillsClient

SkillProvider = Literal["anthropic", "openai"]


def skill_model(settings: Settings | None = None) -> str:
    """Return the model used by the active hosted-skill provider."""
    settings = settings or get_settings()
    if skill_provider(settings) == "openai":
        return settings.openai_skills_model
    return settings.claude_model


def skill_prompt_version(skill_id: str, skill_version: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if skill_provider(settings) == "openai":
        return OpenAISkillRef(skill_id=skill_id, version=skill_version).prompt_version
    return SkillRef(skill_id=skill_id, version=skill_version).prompt_version


def skill_provider(settings: Settings | None = None) -> SkillProvider:
    settings = settings or get_settings()
    raw = (settings.skill_provider or "anthropic").strip().lower()
    if raw == "openai":
        return "openai"
    if raw == "anthropic":
        return "anthropic"
    raise ValueError(f"Unsupported SKILL_PROVIDER={raw!r}; use 'anthropic' or 'openai'")


def invoke_skill_json(
    *,
    skill_id: str,
    skill_version: str,
    payload: dict[str, Any],
    instruction: str,
    schema: type[BaseModel],
    settings: Settings | None = None,
    max_tokens: int = 8000,
) -> BaseModel:
    settings = settings or get_settings()
    provider = skill_provider(settings)

    if provider == "openai":
        if not settings.openai_api_key:
            raise OpenAISkillError("OPENAI_API_KEY not configured")
        openai_client = OpenAISkillsClient(
            settings.openai_api_key,
            base_url=settings.effective_openai_skills_base_url,
            model=settings.openai_skills_model,
        )
        openai_skill = OpenAISkillRef(skill_id=skill_id, version=skill_version)
        return openai_client.invoke_json(
            openai_skill,
            payload,
            instruction,
            schema,
            max_output_tokens=max_tokens,
        )

    if not settings.anthropic_api_key:
        raise SkillError("ANTHROPIC_API_KEY not configured")
    anthropic_client = SkillClient(
        api_key=settings.anthropic_api_key,
        model=settings.claude_model,
        max_tokens=max_tokens,
    )
    anthropic_skill = SkillRef(skill_id=skill_id, version=skill_version)
    result = anthropic_client.invoke_json(anthropic_skill, payload, instruction, schema)
    assert isinstance(result, BaseModel)
    return result
