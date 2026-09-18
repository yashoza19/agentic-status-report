"""Route skill JSON invocation to Anthropic or Gemini hosted skills."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from status.config import Settings, get_settings
from status.skills.client import SkillClient, SkillError, SkillRef
from status.skills.gemini_skills import GeminiSkillError, GeminiSkillsClient

SkillProvider = Literal["anthropic", "gemini"]


def skill_prompt_version(skill_id: str, skill_version: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if skill_provider(settings) == "gemini":
        return f"{skill_id}@{skill_version}"
    return SkillRef(skill_id=skill_id, version=skill_version).prompt_version


def skill_provider(settings: Settings | None = None) -> SkillProvider:
    settings = settings or get_settings()
    raw = (settings.skill_provider or "anthropic").strip().lower()
    if raw == "gemini":
        return "gemini"
    if raw in {"", "anthropic"}:
        return "anthropic"
    raise SkillError(
        f"Unsupported SKILL_PROVIDER={settings.skill_provider!r}. "
        "Use anthropic or gemini."
    )


def gemini_client_from_settings(settings: Settings) -> GeminiSkillsClient:
    project = settings.effective_gcp_project
    if not project:
        raise GeminiSkillError(
            "GCP_PROJECT or GOOGLE_CLOUD_PROJECT is required when SKILL_PROVIDER=gemini"
        )
    return GeminiSkillsClient(
        project=project,
        location=settings.gcp_location,
        agents_location=settings.gcp_agents_location,
        interactions_location=settings.gcp_interactions_location,
        base_agent=settings.gemini_base_agent,
    )


def invoke_skill_json(
    *,
    skill_id: str,
    skill_version: str,
    payload: dict[str, Any],
    instruction: str,
    schema: type[BaseModel],
    settings: Settings | None = None,
    max_tokens: int = 8000,
    agent_id: str | None = None,
) -> BaseModel:
    settings = settings or get_settings()
    provider = skill_provider(settings)

    if provider == "gemini":
        resolved_agent = (agent_id or "").strip()
        if not resolved_agent:
            raise GeminiSkillError(
                "DRAFTER_AGENT_ID or SYNTHESIZER_AGENT_ID is required when SKILL_PROVIDER=gemini"
            )
        client = gemini_client_from_settings(settings)
        return client.invoke_json(resolved_agent, payload, instruction, schema)

    if not settings.anthropic_api_key:
        raise SkillError("ANTHROPIC_API_KEY not configured")
    client = SkillClient(
        api_key=settings.anthropic_api_key,
        model=settings.claude_model,
        max_tokens=max_tokens,
    )
    skill = SkillRef(skill_id=skill_id, version=skill_version)
    result = client.invoke_json(skill, payload, instruction, schema)
    assert isinstance(result, BaseModel)
    return result
