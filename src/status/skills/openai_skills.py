"""OpenAI hosted Skills (upload + Responses API invocation)."""

from __future__ import annotations

import io
import json
import logging
import re
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ValidationError

from status.collectors.http import request_json

log = logging.getLogger(__name__)
FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class OpenAISkillError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAISkillRef:
    skill_id: str
    version: str = "latest"

    @property
    def prompt_version(self) -> str:
        return f"{self.skill_id}@{self.version}"

    def as_skill_reference(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "type": "skill_reference",
            "skill_id": self.skill_id,
        }
        if self.version and self.version != "latest":
            if self.version.isdigit():
                entry["version"] = int(self.version)
            else:
                entry["version"] = self.version
        return entry


def skill_dir_to_zip_bytes(skill_dir: Path) -> bytes:
    """Zip a skill folder with a single top-level directory (OpenAI upload format)."""
    if not (skill_dir / "SKILL.md").is_file():
        raise OpenAISkillError(f"SKILL.md not found in {skill_dir}")
    top = skill_dir.name
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in skill_dir.rglob("*"):
            if not path.is_file():
                continue
            arcname = f"{top}/{path.relative_to(skill_dir).as_posix()}"
            archive.write(path, arcname)
    return buffer.getvalue()


def _api_base(base_url: str | None) -> str:
    return (base_url or "https://api.openai.com/v1").rstrip("/")


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _multipart_zip_upload(url: str, api_key: str, zip_bytes: bytes) -> Any:
    boundary = f"----status-{uuid.uuid4().hex}"
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        b'Content-Disposition: form-data; name="files"; filename="skill.zip"\r\n'
    )
    body.write(b"Content-Type: application/zip\r\n\r\n")
    body.write(zip_bytes)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    data = body.getvalue()
    request = Request(
        url,
        data=data,
        headers={
            **_auth_headers(api_key),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise OpenAISkillError(f"HTTP {exc.code} for {url}: {detail[:500]}") from exc


def _skill_id_from_response(data: Any) -> str:
    if not isinstance(data, dict):
        raise OpenAISkillError(f"unexpected skills API response: {data!r}")
    skill_id = data.get("id") or data.get("skill_id")
    if not skill_id:
        raise OpenAISkillError(f"skills API response missing id: {data!r}")
    return str(skill_id)


def _version_label_from_response(data: Any) -> str:
    if not isinstance(data, dict):
        return "latest"
    version = data.get("version") or data.get("id")
    return str(version) if version is not None else "latest"


def _extract_output_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"].strip()
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"output_text", "text"} and block.get("text"):
                chunks.append(str(block["text"]))
    return "\n".join(chunks).strip()


class OpenAISkillsClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        model: str = "gpt-4.1",
        timeout_s: float = 600.0,
    ) -> None:
        self._api_key = api_key
        self._base = _api_base(base_url)
        self._model = model
        self._timeout_s = timeout_s

    def upload(self, skill_dir: Path) -> str:
        zip_bytes = skill_dir_to_zip_bytes(skill_dir)
        data = _multipart_zip_upload(
            f"{self._base}/skills",
            self._api_key,
            zip_bytes,
        )
        skill_id = _skill_id_from_response(data)
        log.info("created OpenAI skill %s from %s", skill_id, skill_dir)
        return skill_id

    def publish_version(self, skill_id: str, skill_dir: Path) -> str:
        zip_bytes = skill_dir_to_zip_bytes(skill_dir)
        data = _multipart_zip_upload(
            f"{self._base}/skills/{skill_id}/versions",
            self._api_key,
            zip_bytes,
        )
        label = _version_label_from_response(data)
        log.info("published OpenAI skill %s version %s", skill_id, label)
        return label

    def list_skills(self) -> list[dict[str, Any]]:
        data = request_json(
            "GET",
            f"{self._base}/skills",
            headers=_auth_headers(self._api_key),
            timeout_s=60.0,
        )
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return [row for row in data["data"] if isinstance(row, dict)]
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []

    def invoke_json(
        self,
        skill: OpenAISkillRef,
        payload: dict[str, Any],
        instruction: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        user_input = (
            f"{instruction}\n\n<payload>\n{json.dumps(payload)}\n</payload>"
        )
        body: dict[str, Any] = {
            "model": self._model,
            "input": user_input,
            "tools": [
                {
                    "type": "shell",
                    "environment": {
                        "type": "container_auto",
                        "skills": [skill.as_skill_reference()],
                    },
                }
            ],
        }
        response = request_json(
            "POST",
            f"{self._base}/responses",
            headers={**_auth_headers(self._api_key), "Content-Type": "application/json"},
            body=body,
            timeout_s=self._timeout_s,
        )
        if not isinstance(response, dict):
            raise OpenAISkillError(f"unexpected responses payload: {response!r}")
        text = _extract_output_text(response)
        if not text:
            raise OpenAISkillError(f"{skill.skill_id} returned no text output")
        cleaned = FENCE_RE.sub("", text).strip()
        try:
            raw = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise OpenAISkillError(f"{skill.skill_id} returned non-JSON output") from exc
        try:
            return schema.model_validate(raw)
        except ValidationError as exc:
            raise OpenAISkillError(f"{skill.skill_id} output failed validation") from exc
