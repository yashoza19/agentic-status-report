"""Gemini Skill Registry + Managed Agents + Interactions API client (Option A)."""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pydantic import BaseModel

from status.skills.json_output import JsonOutputError, parse_json_model

log = logging.getLogger(__name__)

SKILL_ID_RE = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
DEFAULT_BASE_AGENT = "antigravity-preview-05-2026"
DEFAULT_SKILL_LOCATION = "us-central1"
DEFAULT_AGENTS_LOCATION = "global"
DEFAULT_INTERACTIONS_LOCATION = "global"
API_REVISION = "2026-05-20"
LRO_POLL_INTERVAL_S = 2.0
LRO_TIMEOUT_S = 300.0
INTERACTION_TIMEOUT_S = 600.0

JSON_FOLLOW_UP = (
    "Reply with ONLY the complete final JSON object as plain text. "
    "Do not use markdown code fences, shell, or commentary. "
    "If your prior reply was truncated, return the full JSON object from the start."
)

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class GeminiSkillError(RuntimeError):
    """Raised for Gemini Skill Registry / Managed Agents / Interactions failures."""


def validate_skill_id(skill_id: str) -> str:
    cleaned = skill_id.strip()
    if not SKILL_ID_RE.match(cleaned):
        raise GeminiSkillError(
            f"Invalid Gemini skill id {skill_id!r}. "
            "Must be 1-63 chars, lowercase letters/numbers/hyphens, "
            "start with a letter, end with a letter or number."
        )
    if cleaned.startswith("gcp-"):
        raise GeminiSkillError(f"Skill id {skill_id!r} cannot use reserved gcp- prefix")
    return cleaned


def skill_dir_to_zip_bytes(skill_dir: Path) -> bytes:
    """Zip a skill folder with SKILL.md at the archive root (Gemini Skill Registry format)."""
    if not (skill_dir / "SKILL.md").is_file():
        raise GeminiSkillError(f"SKILL.md not found in {skill_dir}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in skill_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.name.startswith(".") or "__pycache__" in path.parts:
                continue
            arcname = path.relative_to(skill_dir).as_posix()
            archive.write(path, arcname)
    return buffer.getvalue()


def skill_metadata_from_dir(skill_dir: Path) -> tuple[str, str]:
    """Return (display_name, description) from SKILL.md frontmatter when present."""
    skill_md = skill_dir / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    display_name = skill_dir.name
    description = f"Hosted skill package for {skill_dir.name}"
    match = FRONTMATTER_RE.match(text)
    if not match:
        return display_name, description
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if key == "name" and value:
            display_name = value
        elif key == "description" and value:
            description = value
    return display_name, description


def skill_resource_name(project: str, location: str, skill_id: str, version: str | None = None) -> str:
    base = f"projects/{project}/locations/{location}/skills/{skill_id}"
    if version and version != "latest":
        return f"{base}/skill_versions/{version}"
    return base


def extract_interaction_text(payload: dict[str, Any] | list[Any] | str) -> str:
    """Pull assistant text from Interactions API JSON or SSE event payloads."""
    if isinstance(payload, str):
        return _extract_from_sse(payload)
    if isinstance(payload, list):
        chunks = [extract_interaction_text(item) for item in payload if isinstance(item, (dict, str))]
        # Deltas are token fragments — concatenate without separators.
        return "".join(chunk for chunk in chunks if chunk).strip()
    if not isinstance(payload, dict):
        return ""

    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"].strip()

    if isinstance(payload.get("text"), str) and payload["text"].strip():
        return payload["text"].strip()

    delta = payload.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str) and delta["text"].strip():
        return delta["text"].strip()
    if isinstance(delta, str) and delta.strip():
        return delta.strip()

    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        content_chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("text"):
                content_chunks.append(str(block["text"]))
            elif isinstance(block, str) and block.strip():
                content_chunks.append(block)
        if content_chunks:
            return "".join(content_chunks)

    interaction = payload.get("interaction")
    if isinstance(interaction, dict):
        nested = extract_interaction_text(interaction)
        if nested:
            return nested

    chunks: list[str] = []
    steps = payload.get("steps")
    if isinstance(steps, list):
        step_chunks: list[str] = []
        for item in steps:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "model_output":
                continue
            nested = extract_interaction_text(item)
            if nested:
                step_chunks.append(nested)
        if step_chunks:
            # model_output steps are streamed fragments of one answer.
            return "".join(step_chunks).strip()

    for key in ("outputs", "output"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            nested = extract_interaction_text(item)
            if nested:
                chunks.append(nested)

    if chunks:
        return "".join(chunks).strip()

    data = payload.get("data")
    if isinstance(data, (dict, list, str)):
        return extract_interaction_text(data)
    return ""


def _extract_from_sse(raw: str) -> str:
    chunks: list[str] = []
    for block in raw.split("\n\n"):
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        data_text = "\n".join(data_lines).strip()
        if not data_text or data_text == "[DONE]":
            continue
        try:
            parsed = json.loads(data_text)
        except json.JSONDecodeError:
            continue
        text = extract_interaction_text(parsed)
        if text:
            chunks.append(text)
    # SSE content deltas are fragments — join without separators.
    return "".join(chunks).strip()


def _import_google_auth():
    try:
        import google.auth
        from google.auth.transport.requests import Request as GoogleAuthRequest
    except ImportError as exc:
        raise GeminiSkillError(
            "google-auth is required for Gemini Skill Registry / Managed Agents. "
            "Run: pip install -e '.[gemini]'"
        ) from exc
    return google.auth, GoogleAuthRequest


class GeminiSkillsClient:
    def __init__(
        self,
        *,
        project: str,
        location: str = DEFAULT_SKILL_LOCATION,
        agents_location: str = DEFAULT_AGENTS_LOCATION,
        interactions_location: str = DEFAULT_INTERACTIONS_LOCATION,
        base_agent: str = DEFAULT_BASE_AGENT,
        timeout_s: float = INTERACTION_TIMEOUT_S,
    ) -> None:
        if not project.strip():
            raise GeminiSkillError("GCP_PROJECT / GOOGLE_CLOUD_PROJECT is required")
        self.project = project.strip()
        self.location = location.strip() or DEFAULT_SKILL_LOCATION
        self.agents_location = agents_location.strip() or DEFAULT_AGENTS_LOCATION
        self.interactions_location = interactions_location.strip() or DEFAULT_INTERACTIONS_LOCATION
        self.base_agent = base_agent.strip() or DEFAULT_BASE_AGENT
        self._timeout_s = timeout_s
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def _skills_root(self) -> str:
        return (
            f"https://{self.location}-aiplatform.googleapis.com/v1beta1/"
            f"projects/{self.project}/locations/{self.location}/skills"
        )

    def _agents_root(self) -> str:
        return (
            f"https://aiplatform.googleapis.com/v1beta1/"
            f"projects/{self.project}/locations/{self.agents_location}/agents"
        )

    def _interactions_url(self) -> str:
        return (
            f"https://aiplatform.googleapis.com/v1beta1/"
            f"projects/{self.project}/locations/{self.interactions_location}/interactions"
        )

    def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expiry - 60:
            return self._token
        google_auth, GoogleAuthRequest = _import_google_auth()
        credentials, _ = google_auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(GoogleAuthRequest())
        if not credentials.token:
            raise GeminiSkillError(
                "Failed to obtain GCP access token via Application Default Credentials. "
                "Run `gcloud auth application-default login` or set GOOGLE_APPLICATION_CREDENTIALS."
            )
        self._token = str(credentials.token)
        expiry = getattr(credentials, "expiry", None)
        if expiry is not None:
            self._token_expiry = expiry.timestamp()
        else:
            self._token_expiry = now + 3500
        return self._token

    def _headers(self, *, interactions: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }
        if interactions:
            headers["Api-Revision"] = API_REVISION
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        interactions: bool = False,
        timeout_s: float | None = None,
        accept_sse: bool = False,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = self._headers(interactions=interactions)
        if accept_sse:
            headers["Accept"] = "text/event-stream"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout_s or self._timeout_s) as response:
                raw = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "")
                if accept_sse or "text/event-stream" in content_type:
                    return raw
                return json.loads(raw) if raw.strip() else None
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GeminiSkillError(f"HTTP {exc.code} for {method} {url}: {detail[:800]}") from exc
        except URLError as exc:
            raise GeminiSkillError(f"Network error for {method} {url}: {exc}") from exc

    def _poll_operation(self, operation: dict[str, Any], *, label: str) -> dict[str, Any]:
        name = operation.get("name")
        if not name:
            # Some endpoints may return the resource directly.
            if operation.get("done") is True or "error" not in operation:
                return operation
            raise GeminiSkillError(f"{label} response missing operation name: {operation!r}")

        if operation.get("done"):
            if operation.get("error"):
                raise GeminiSkillError(f"{label} failed: {operation['error']}")
            return operation.get("response") or operation

        # Operation name is a full resource path; poll via aiplatform global ops endpoint.
        if name.startswith("projects/"):
            op_url = f"https://aiplatform.googleapis.com/v1beta1/{name}"
            # Regional skill LROs often live on the regional host.
            if f"/locations/{self.location}/" in name and self.location != "global":
                op_url = f"https://{self.location}-aiplatform.googleapis.com/v1beta1/{name}"
        else:
            op_url = name

        deadline = time.time() + LRO_TIMEOUT_S
        current = operation
        while time.time() < deadline:
            if current.get("done"):
                if current.get("error"):
                    raise GeminiSkillError(f"{label} failed: {current['error']}")
                return current.get("response") or current
            time.sleep(LRO_POLL_INTERVAL_S)
            current = self._request("GET", op_url, timeout_s=60.0)
            if not isinstance(current, dict):
                raise GeminiSkillError(f"{label} poll returned unexpected payload: {current!r}")
        raise GeminiSkillError(f"{label} timed out after {LRO_TIMEOUT_S:.0f}s ({name})")

    def upload(self, skill_dir: Path, skill_id: str) -> str:
        skill_id = validate_skill_id(skill_id)
        display_name, description = skill_metadata_from_dir(skill_dir)
        zip_b64 = base64.b64encode(skill_dir_to_zip_bytes(skill_dir)).decode("ascii")
        url = f"{self._skills_root()}?skillId={quote(skill_id)}"
        operation = self._request(
            "POST",
            url,
            body={
                "displayName": display_name,
                "description": description,
                "zippedFilesystem": zip_b64,
            },
            timeout_s=LRO_TIMEOUT_S,
        )
        if not isinstance(operation, dict):
            raise GeminiSkillError(f"unexpected create skill response: {operation!r}")
        result = self._poll_operation(operation, label=f"create skill {skill_id}")
        name = result.get("name") if isinstance(result, dict) else None
        log.info("created Gemini skill %s (%s)", skill_id, name or skill_id)
        return skill_id

    def publish_version(self, skill_id: str, skill_dir: Path) -> str:
        skill_id = validate_skill_id(skill_id)
        display_name, description = skill_metadata_from_dir(skill_dir)
        zip_b64 = base64.b64encode(skill_dir_to_zip_bytes(skill_dir)).decode("ascii")
        url = (
            f"{self._skills_root()}/{quote(skill_id)}"
            f"?updateMask=displayName,description,zippedFilesystem"
        )
        operation = self._request(
            "PATCH",
            url,
            body={
                "displayName": display_name,
                "description": description,
                "zippedFilesystem": zip_b64,
            },
            timeout_s=LRO_TIMEOUT_S,
        )
        if not isinstance(operation, dict):
            raise GeminiSkillError(f"unexpected update skill response: {operation!r}")
        result = self._poll_operation(operation, label=f"update skill {skill_id}")
        version = "latest"
        if isinstance(result, dict):
            version = str(
                result.get("defaultRevision")
                or result.get("revisionId")
                or result.get("updateTime")
                or "latest"
            )
        log.info("updated Gemini skill %s revision %s", skill_id, version)
        return version

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        skill_id = validate_skill_id(skill_id)
        try:
            data = self._request("GET", f"{self._skills_root()}/{quote(skill_id)}", timeout_s=60.0)
        except GeminiSkillError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return data if isinstance(data, dict) else None

    def list_skills(self) -> list[dict[str, Any]]:
        data = self._request("GET", self._skills_root(), timeout_s=60.0)
        if isinstance(data, dict):
            skills = data.get("skills") or data.get("items") or []
            if isinstance(skills, list):
                return [row for row in skills if isinstance(row, dict)]
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []

    def upload_or_update(self, skill_dir: Path, skill_id: str) -> tuple[str, str]:
        """Create skill if missing, otherwise update. Returns (skill_id, version_label)."""
        skill_id = validate_skill_id(skill_id)
        existing = self.get_skill(skill_id)
        if existing is None:
            created = self.upload(skill_dir, skill_id)
            return created, "latest"
        version = self.publish_version(skill_id, skill_dir)
        return skill_id, version

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        try:
            data = self._request("GET", f"{self._agents_root()}/{quote(agent_id)}", timeout_s=60.0)
        except GeminiSkillError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return data if isinstance(data, dict) else None

    def _skill_source(
        self,
        skill_id: str,
        skill_version: str | None,
    ) -> dict[str, str]:
        return {
            "type": "skill_registry",
            "source": skill_resource_name(self.project, self.location, skill_id, skill_version),
            "target": "/.agent/skills",
        }

    def _agent_body(
        self,
        *,
        agent_id: str,
        skill_id: str,
        skill_version: str | None,
        system_instruction: str,
        description: str,
    ) -> dict[str, Any]:
        return {
            "id": agent_id,
            "base_agent": self.base_agent,
            "description": description,
            "system_instruction": system_instruction,
            "tools": [
                {"type": "code_execution"},
                {"type": "filesystem"},
            ],
            "base_environment": {
                "type": "remote",
                "sources": [self._skill_source(skill_id, skill_version)],
                "network": {"allowlist": [{"domain": "*"}]},
            },
        }

    def ensure_agent(
        self,
        *,
        agent_id: str,
        skill_id: str,
        skill_version: str = "latest",
        system_instruction: str,
        description: str | None = None,
    ) -> str:
        """Create agent if missing; otherwise refresh skill mount + instruction."""
        skill_id = validate_skill_id(skill_id)
        agent_id = validate_skill_id(agent_id)  # same id constraints
        version = None if skill_version in {"", "latest"} else skill_version
        desc = description or f"Managed agent for skill {skill_id}"
        existing = self.get_agent(agent_id)
        if existing is None:
            body = self._agent_body(
                agent_id=agent_id,
                skill_id=skill_id,
                skill_version=version,
                system_instruction=system_instruction,
                description=desc,
            )
            try:
                operation = self._request(
                    "POST",
                    self._agents_root(),
                    body=body,
                    timeout_s=LRO_TIMEOUT_S,
                )
            except GeminiSkillError as exc:
                # API may return 409 even when GET returns 404 (reserved / soft-deleted id).
                if "HTTP 409" in str(exc) or "ALREADY_EXISTS" in str(exc):
                    log.warning(
                        "agent %s create conflicted; attempting update (GET may lag)",
                        agent_id,
                    )
                    existing = self.get_agent(agent_id) or {"id": agent_id}
                else:
                    raise
            else:
                if not isinstance(operation, dict):
                    raise GeminiSkillError(f"unexpected create agent response: {operation!r}")
                self._poll_operation(operation, label=f"create agent {agent_id}")
                log.info("created Gemini agent %s mounting %s", agent_id, skill_id)
                return agent_id

        # Update mount + instruction for idempotent republish.
        env_body = {
            "name": agent_id,
            "base_environment": {
                "type": "remote",
                "sources": [self._skill_source(skill_id, version)],
                "network": {"allowlist": [{"domain": "*"}]},
            },
        }
        try:
            operation = self._request(
                "PATCH",
                f"{self._agents_root()}/{quote(agent_id)}?update_mask=base_environment",
                body=env_body,
                timeout_s=LRO_TIMEOUT_S,
            )
        except GeminiSkillError as exc:
            if "HTTP 404" in str(exc):
                raise GeminiSkillError(
                    f"Agent id {agent_id!r} is reserved or soft-deleted (create=409, get/patch=404). "
                    f"Choose a new DRAFTER_AGENT_ID / SYNTHESIZER_AGENT_ID and republish. "
                    f"Original error: {exc}"
                ) from exc
            raise
        if isinstance(operation, dict):
            self._poll_operation(operation, label=f"update agent env {agent_id}")

        instr_body = {"name": agent_id, "system_instruction": system_instruction}
        operation = self._request(
            "PATCH",
            f"{self._agents_root()}/{quote(agent_id)}?update_mask=system_instruction",
            body=instr_body,
            timeout_s=LRO_TIMEOUT_S,
        )
        if isinstance(operation, dict):
            self._poll_operation(operation, label=f"update agent instruction {agent_id}")

        log.info("updated Gemini agent %s to mount %s", agent_id, skill_id)
        return agent_id

    def _parse_sse_events(self, raw: str) -> tuple[str, str | None, str | None]:
        """Return (concatenated_text, interaction_id, status) from an SSE body."""
        chunks: list[str] = []
        interaction_id: str | None = None
        status: str | None = None
        for block in raw.split("\n\n"):
            data_lines: list[str] = []
            event_name = ""
            for line in block.splitlines():
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            data_text = "\n".join(data_lines).strip()
            if not data_text or data_text == "[DONE]":
                continue
            try:
                event = json.loads(data_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            interaction = event.get("interaction")
            if isinstance(interaction, dict):
                if interaction.get("id"):
                    interaction_id = str(interaction["id"])
                if interaction.get("status"):
                    status = str(interaction["status"])
            text = extract_interaction_text(event)
            # interaction.complete payloads often omit outputs; keep prior deltas.
            if text and event_name != "interaction.complete":
                chunks.append(text)
            elif text and not chunks:
                chunks.append(text)
        # Content deltas are token fragments — concatenate without separators.
        return "".join(chunks).strip(), interaction_id, status

    def _get_interaction(self, interaction_id: str) -> dict[str, Any]:
        url = f"{self._interactions_url()}/{quote(interaction_id, safe='')}"
        data = self._request("GET", url, interactions=True, timeout_s=60.0)
        if not isinstance(data, dict):
            raise GeminiSkillError(f"unexpected get interaction payload: {data!r}")
        return data

    def _wait_for_interaction(
        self,
        interaction_id: str,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        deadline = time.time() + (timeout_s or self._timeout_s)
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self._get_interaction(interaction_id)
            status = str(last.get("status") or "").lower()
            if status in {"completed", "failed", "cancelled", "incomplete"}:
                return last
            time.sleep(LRO_POLL_INTERVAL_S)
        raise GeminiSkillError(
            f"interaction {interaction_id} did not complete within "
            f"{timeout_s or self._timeout_s:.0f}s (last status={last.get('status')!r})"
        )

    def _interactions_create(self, body: dict[str, Any]) -> dict[str, Any]:
        # Agent interactions require stream+background+store (Agent Platform preview).
        attempt_body = {
            **body,
            "stream": True,
            "background": True,
            "store": True,
        }
        raw = self._request(
            "POST",
            self._interactions_url(),
            body=attempt_body,
            interactions=True,
            timeout_s=self._timeout_s,
            accept_sse=True,
        )
        if not isinstance(raw, str):
            raise GeminiSkillError(f"unexpected streamed interactions payload: {raw!r}")

        text, interaction_id, status = self._parse_sse_events(raw)
        if interaction_id and (not status or status.lower() in {"in_progress", "requires_action"}):
            log.info("waiting for interaction %s to complete (status=%s)", interaction_id, status)
            completed = self._wait_for_interaction(interaction_id)
            status = str(completed.get("status") or status or "")
            completed_text = extract_interaction_text(completed)
            if completed_text:
                text = completed_text
            if status.lower() == "failed":
                raise GeminiSkillError(f"interaction {interaction_id} failed: {completed!r}")
        elif interaction_id:
            # Even when SSE reports completed, prefer GET — full model_output steps live there.
            try:
                completed = self._get_interaction(interaction_id)
                completed_text = extract_interaction_text(completed)
                if completed_text:
                    text = completed_text
                status = str(completed.get("status") or status or "")
            except GeminiSkillError as exc:
                log.warning("could not refresh interaction %s: %s", interaction_id, exc)
        return {"output_text": text, "id": interaction_id, "status": status, "_raw_sse": raw}

    def _parse_skill_text(self, text: str, schema: type[BaseModel], source: str) -> BaseModel:
        try:
            return parse_json_model(text, schema, source=source)
        except JsonOutputError as exc:
            raise GeminiSkillError(str(exc)) from exc

    def invoke_json(
        self,
        agent_id: str,
        payload: dict[str, Any],
        instruction: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        user_text = f"{instruction}\n\nPAYLOAD:\n{json.dumps(payload)}"
        body: dict[str, Any] = {
            "agent": agent_id,
            "input": [
                {
                    "type": "user_input",
                    "content": [{"type": "text", "text": user_text}],
                }
            ],
        }
        response = self._interactions_create(body)
        text = extract_interaction_text(response)
        if not text:
            raise GeminiSkillError(f"agent {agent_id} returned no text output")

        try:
            return self._parse_skill_text(text, schema, agent_id)
        except GeminiSkillError:
            log.warning("%s returned non-JSON output; requesting JSON-only follow-up", agent_id)

        follow_body: dict[str, Any] = {
            "agent": agent_id,
            "input": [
                {
                    "type": "user_input",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"{user_text}\n\n---\nPrior model output (may be incomplete):\n"
                                f"{text}\n\n{JSON_FOLLOW_UP}"
                            ),
                        }
                    ],
                }
            ],
        }
        # Do not chain previous_interaction_id — background agent turns often remain
        # IN_PROGRESS briefly and reject follow-ups. Fresh turn with prior text is safer.
        follow_up = self._interactions_create(follow_body)
        follow_text = extract_interaction_text(follow_up)
        if not follow_text:
            raise GeminiSkillError(f"agent {agent_id} returned no text output after JSON follow-up")
        return self._parse_skill_text(follow_text, schema, agent_id)
