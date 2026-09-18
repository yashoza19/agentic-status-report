"""Unit tests for Gemini Skill Registry helpers and provider routing."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from status.config import Settings
from status.skills.client import SkillError
from status.skills.gemini_skills import (
    GeminiSkillError,
    GeminiSkillsClient,
    extract_interaction_text,
    skill_dir_to_zip_bytes,
    skill_metadata_from_dir,
    skill_resource_name,
    validate_skill_id,
)
from status.skills.schemas import DraftOutput
from status.skills.skill_invoke import invoke_skill_json, skill_provider


class _TinySchema(BaseModel):
    ok: bool


def test_validate_skill_id_accepts_kebab() -> None:
    assert validate_skill_id("weekly-status-drafter") == "weekly-status-drafter"


def test_validate_skill_id_rejects_invalid() -> None:
    with pytest.raises(GeminiSkillError):
        validate_skill_id("Weekly_Status")
    with pytest.raises(GeminiSkillError):
        validate_skill_id("gcp-builtin")


def test_skill_dir_to_zip_bytes_puts_skill_md_at_root(tmp_path: Path) -> None:
    skill_dir = tmp_path / "weekly-status-drafter"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: weekly-status-drafter\n---\n# Hi\n")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "helper.py").write_text("print('hi')\n")

    raw = skill_dir_to_zip_bytes(skill_dir)
    with zipfile.ZipFile(__import__("io").BytesIO(raw)) as archive:
        names = set(archive.namelist())
    assert "SKILL.md" in names
    assert "scripts/helper.py" in names
    assert not any(name.startswith("weekly-status-drafter/") for name in names)


def test_skill_metadata_from_dir(tmp_path: Path) -> None:
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Does a thing\n---\n\n# Demo\n"
    )
    assert skill_metadata_from_dir(skill_dir) == ("demo-skill", "Does a thing")


def test_skill_resource_name_pins_version() -> None:
    assert (
        skill_resource_name("proj", "us-central1", "weekly-status-drafter", "v2")
        == "projects/proj/locations/us-central1/skills/weekly-status-drafter/skill_versions/v2"
    )
    assert (
        skill_resource_name("proj", "us-central1", "weekly-status-drafter", "latest")
        == "projects/proj/locations/us-central1/skills/weekly-status-drafter"
    )


def test_extract_interaction_text_from_steps() -> None:
    payload = {
        "id": "abc",
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "text", "text": '{"ok": true}'}],
            }
        ],
    }
    assert extract_interaction_text(payload) == '{"ok": true}'


def test_extract_interaction_text_from_sse() -> None:
    good = (
        "event: content.delta\n"
        'data: {"delta": {"text": "{\\"ok\\":"}}\n\n'
        "event: content.delta\n"
        'data: {"delta": {"text": " true}"}}\n\n'
        "event: interaction.complete\n"
        'data: {"interaction": {"id": "x", "status": "completed"}}\n\n'
    )
    assert extract_interaction_text(good) == '{"ok":true}'


def test_extract_interaction_text_from_model_output_steps() -> None:
    payload = {
        "status": "completed",
        "steps": [
            {"type": "user_input", "content": [{"type": "text", "text": "ignore me"}]},
            {"type": "model_output", "content": [{"type": "text", "text": '{"ok":'}]},
            {"type": "model_output", "content": [{"type": "text", "text": " true}"}]},
        ],
    }
    assert extract_interaction_text(payload) == '{"ok": true}'


def test_extract_interaction_text_from_output_text() -> None:
    assert extract_interaction_text({"output_text": " hello "}) == "hello"


def test_skill_provider_defaults_anthropic() -> None:
    settings = Settings(_env_file=None)
    assert skill_provider(settings) == "anthropic"


def test_skill_provider_gemini() -> None:
    settings = Settings(_env_file=None, SKILL_PROVIDER="gemini")
    assert skill_provider(settings) == "gemini"


def test_skill_provider_rejects_unknown() -> None:
    settings = Settings(_env_file=None, SKILL_PROVIDER="openai")
    with pytest.raises(SkillError, match="Unsupported SKILL_PROVIDER"):
        skill_provider(settings)


def test_invoke_skill_json_routes_to_gemini() -> None:
    settings = Settings(
        _env_file=None,
        SKILL_PROVIDER="gemini",
        GCP_PROJECT="demo-project",
        DRAFTER_AGENT_ID="weekly-status-drafter-agent",
    )
    fake = MagicMock()
    fake.invoke_json.return_value = _TinySchema(ok=True)
    with patch("status.skills.skill_invoke.gemini_client_from_settings", return_value=fake):
        result = invoke_skill_json(
            skill_id="weekly-status-drafter",
            skill_version="latest",
            payload={"x": 1},
            instruction="do it",
            schema=_TinySchema,
            settings=settings,
            agent_id="weekly-status-drafter-agent",
        )
    assert result.ok is True
    fake.invoke_json.assert_called_once()


def test_invoke_skill_json_gemini_requires_agent_id() -> None:
    settings = Settings(_env_file=None, SKILL_PROVIDER="gemini", GCP_PROJECT="demo")
    with pytest.raises(GeminiSkillError, match="AGENT_ID"):
        invoke_skill_json(
            skill_id="weekly-status-drafter",
            skill_version="latest",
            payload={},
            instruction="do it",
            schema=_TinySchema,
            settings=settings,
            agent_id=None,
        )


def test_gemini_client_upload_or_update_creates_when_missing(tmp_path: Path) -> None:
    skill_dir = tmp_path / "weekly-status-drafter"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: weekly-status-drafter\ndescription: d\n---\n")

    client = GeminiSkillsClient(project="demo", location="us-central1")
    with (
        patch.object(client, "get_skill", return_value=None),
        patch.object(client, "upload", return_value="weekly-status-drafter") as upload,
    ):
        skill_id, version = client.upload_or_update(skill_dir, "weekly-status-drafter")
    assert skill_id == "weekly-status-drafter"
    assert version == "latest"
    upload.assert_called_once()


def test_gemini_client_ensure_agent_creates_when_missing() -> None:
    client = GeminiSkillsClient(project="demo", location="us-central1")
    with (
        patch.object(client, "get_agent", return_value=None),
        patch.object(
            client,
            "_request",
            return_value={"name": "projects/demo/locations/global/operations/1", "done": True},
        ) as request,
        patch.object(client, "_poll_operation", return_value={}),
    ):
        agent_id = client.ensure_agent(
            agent_id="weekly-status-drafter-agent",
            skill_id="weekly-status-drafter",
            system_instruction="Follow the skill.",
        )
    assert agent_id == "weekly-status-drafter-agent"
    assert request.call_args.args[0] == "POST"


def test_gemini_invoke_json_parses_and_follow_up() -> None:
    client = GeminiSkillsClient(project="demo")
    draft_json = (
        '{"person":"yoza","week_ending":"2026-09-11","entries":[],'
        '"flags":[],"unticketed_prompt":""}'
    )
    with patch.object(
        client,
        "_interactions_create",
        side_effect=[
            {"output_text": "not json"},
            {"output_text": draft_json},
        ],
    ):
        result = client.invoke_json(
            "weekly-status-drafter-agent",
            {"person": "yoza"},
            "draft please",
            DraftOutput,
        )
    assert isinstance(result, DraftOutput)
    assert result.person == "yoza"


@pytest.mark.integration
def test_gemini_skills_live_smoke() -> None:
    """Gated live smoke; skipped unless RUN_GEMINI_SKILLS_LIVE=1 and GCP is configured."""
    import os

    if os.environ.get("RUN_GEMINI_SKILLS_LIVE") != "1":
        pytest.skip("set RUN_GEMINI_SKILLS_LIVE=1 to run live Gemini skills tests")
    settings = Settings()
    if not settings.effective_gcp_project:
        pytest.skip("GCP_PROJECT not set")
    from status.skills.skill_invoke import gemini_client_from_settings

    client = gemini_client_from_settings(settings)
    skills = client.list_skills()
    assert isinstance(skills, list)
