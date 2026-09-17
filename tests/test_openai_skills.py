from __future__ import annotations

import io
import zipfile
from pathlib import Path

from status.skills.openai_skills import (
    _extract_output_text,
    skill_dir_to_zip_bytes,
)


def test_skill_dir_to_zip_has_single_top_level_folder(tmp_path: Path) -> None:
    skill_dir = tmp_path / "weekly-status-drafter"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: test\n---\n", encoding="utf-8")

    data = skill_dir_to_zip_bytes(skill_dir)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
    assert names == ["weekly-status-drafter/SKILL.md"]


def test_extract_output_text_prefers_output_text_field() -> None:
    payload = {"output_text": '{"person": "yoza"}'}
    assert _extract_output_text(payload) == '{"person": "yoza"}'


def test_extract_output_text_reads_message_blocks() -> None:
    payload = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "hello"}],
            }
        ]
    }
    assert _extract_output_text(payload) == "hello"
