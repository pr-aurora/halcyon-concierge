"""The language model behind each turn.

Every call asks for one structured decision (see DECISION_SCHEMA). Two backends
implement the same contract:

* ClaudeCliBrain runs `claude -p` with --json-schema. This is the default until an
  API key is provided.
* AnthropicApiBrain calls the Messages API directly, forcing a tool call whose input
  matches the same schema. It is used when ANTHROPIC_API_KEY is set.

Both are stateless. Each call gets the full context in its prompt.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import Settings
from .store import FACT_CATEGORIES
from .venues import TAGS

log = logging.getLogger("halcyon.brain")

ACTIONS = ("reply", "search_venues", "book_table", "cancel_booking")

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "reply": {
            "type": "string",
            "description": "Message to the member. Required when action is reply. Empty otherwise.",
        },
        "search": {
            "type": ["object", "null"],
            "description": "Arguments for search_venues. Null for other actions.",
            "properties": {
                "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                "time": {"type": ["string", "null"], "description": "HH:MM, 24-hour. The preferred time."},
                "flex_minutes": {
                    "type": ["integer", "null"],
                    "description": "How far either side of time the member will accept, 0 to 180. Null means any time.",
                },
                "party_size": {"type": ["integer", "null"]},
                "cuisine": {"type": ["string", "null"]},
                "neighbourhoods": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Areas the member is happy with. Any one of them matches. Empty means anywhere.",
                },
                "price_max": {"type": ["integer", "null"], "description": "1 to 4, where 1 is cheapest"},
                "requirements": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(TAGS)},
                    "description": "Must-haves such as dietary needs. Venues without all of them are excluded.",
                },
                "preferences": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(TAGS)},
                    "description": "Nice-to-haves such as romantic or quiet. Used only to rank results.",
                },
            },
            "required": [
                "date",
                "time",
                "flex_minutes",
                "party_size",
                "cuisine",
                "neighbourhoods",
                "price_max",
                "requirements",
                "preferences",
            ],
            "additionalProperties": False,
        },
        "booking": {
            "type": ["object", "null"],
            "description": "Arguments for book_table. Only after the member has confirmed. Null otherwise.",
            "properties": {
                "venue_id": {"type": ["string", "null"]},
                "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                "time": {"type": ["string", "null"], "description": "HH:MM, 24-hour"},
                "party_size": {"type": ["integer", "null"]},
                "special_requests": {"type": ["string", "null"]},
            },
            "required": ["venue_id", "date", "time", "party_size", "special_requests"],
            "additionalProperties": False,
        },
        "cancel": {
            "type": ["object", "null"],
            "description": "Arguments for cancel_booking. Null otherwise.",
            "properties": {"booking_ref": {"type": ["string", "null"]}},
            "required": ["booking_ref"],
            "additionalProperties": False,
        },
        "remember": {
            "type": "array",
            "description": "New facts about the member worth keeping for future conversations.",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": list(FACT_CATEGORIES)},
                    "value": {"type": "string"},
                },
                "required": ["category", "value"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["action", "reply", "search", "booking", "cancel", "remember"],
    "additionalProperties": False,
}


class BrainError(RuntimeError):
    pass


@dataclass
class Decision:
    action: str
    reply: str = ""
    search: dict[str, Any] | None = None
    booking: dict[str, Any] | None = None
    cancel: dict[str, Any] | None = None
    remember: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def parse(cls, data: Any) -> "Decision":
        if not isinstance(data, dict):
            raise BrainError("decision is not an object")
        action = data.get("action")
        if action not in ACTIONS:
            raise BrainError(f"unknown action {action!r}")
        remember = []
        for item in data.get("remember") or []:
            if isinstance(item, dict) and item.get("category") in FACT_CATEGORIES and isinstance(item.get("value"), str):
                remember.append({"category": item["category"], "value": item["value"]})
        return cls(
            action=action,
            reply=(data.get("reply") or "").strip(),
            search=data.get("search") if action == "search_venues" else None,
            booking=data.get("booking") if action == "book_table" else None,
            cancel=data.get("cancel") if action == "cancel_booking" else None,
            remember=remember,
        )


class Brain(Protocol):
    name: str

    def decide(self, *, system: str, prompt: str, timeout_s: float) -> Decision: ...


class ClaudeCliBrain:
    def __init__(self, *, binary: str, model: str, workdir: Path) -> None:
        self._binary = binary
        self._model = model
        self._workdir = workdir
        self.name = f"claude-cli:{model}"

    def decide(self, *, system: str, prompt: str, timeout_s: float) -> Decision:
        self._workdir.mkdir(parents=True, exist_ok=True)
        command = [
            self._binary,
            "-p",
            prompt,
            "--model",
            self._model,
            "--system-prompt",
            system,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(DECISION_SCHEMA),
            "--no-session-persistence",
            "--disable-slash-commands",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=self._workdir,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BrainError("claude timed out") from exc
        except OSError as exc:
            raise BrainError(f"could not run claude: {exc}") from exc

        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise BrainError(f"claude returned non-JSON (exit {completed.returncode}): {completed.stderr[:300]}") from exc
        if envelope.get("is_error") or envelope.get("subtype") != "success":
            raise BrainError(f"claude reported an error: {str(envelope.get('result'))[:300]}")
        structured = envelope.get("structured_output")
        if structured is None:
            structured = _loads_loose(envelope.get("result", ""))
        return Decision.parse(structured)


class AnthropicApiBrain:
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, *, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model
        self.name = f"anthropic-api:{model}"

    def decide(self, *, system: str, prompt: str, timeout_s: float) -> Decision:
        body = {
            "model": self._model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "name": "record_decision",
                    "description": "Record the next step for this member's conversation.",
                    "input_schema": DECISION_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": "record_decision"},
        }
        request = urllib.request.Request(
            self.URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise BrainError(f"Anthropic API returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BrainError(f"Anthropic API request failed: {exc}") from exc

        for block in payload.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "record_decision":
                return Decision.parse(block.get("input"))
        raise BrainError("Anthropic API response had no decision")


def build_brain(settings: Settings) -> Brain:
    if settings.anthropic_api_key:
        log.info("Using the Anthropic API")
        return AnthropicApiBrain(api_key=settings.anthropic_api_key, model=settings.model)
    workdir = Path(tempfile.gettempdir()) / "halcyon-claude"
    return ClaudeCliBrain(binary=settings.claude_bin, model=settings.model, workdir=workdir)


def _loads_loose(text: str) -> Any:
    """Parse a JSON object from free text, used only if structured output is missing."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise BrainError("no JSON object in response")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise BrainError("response JSON did not parse") from exc
