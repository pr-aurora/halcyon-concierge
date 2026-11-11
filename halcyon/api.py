"""HTTP contract: POST a member message (answer 202), GET the conversation transcript.

Every request must carry `Authorization: Bearer <AGENT_TOKEN>`. The POST stores the
message and returns before any work is done. The reply is produced in the background.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from .concierge import Concierge
from .config import Settings

log = logging.getLogger("halcyon.api")

MAX_BODY_BYTES = 64 * 1024
MAX_TEXT_CHARS = 8000
MAX_ID_CHARS = 128
MESSAGES_PATH = re.compile(r"^/conversations/([^/]{1,200})/messages/?$")


class ConciergeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Members poll every two seconds and several conversations run at once. The default backlog of 5 is too small.
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], concierge: Concierge, token: str) -> None:
        super().__init__(address, ConciergeHandler)
        self.concierge = concierge
        self.token = token


class ConciergeHandler(BaseHTTPRequestHandler):
    server: ConciergeServer

    def do_POST(self) -> None:
        conversation_id = self._conversation_id()
        if conversation_id is None:
            return self._send_json(404, {"error": "not_found"})
        if not self._authorised():
            return self._send_json(401, {"error": "unauthorised"})

        payload = self._read_json()
        if payload is None:
            return self._send_json(400, {"error": "invalid_json"})
        problem = _validate_member_message(payload)
        if problem:
            return self._send_json(400, {"error": "invalid_message", "detail": problem})

        member = payload["member"]
        accepted = self.server.concierge.receive(
            conversation_id=conversation_id,
            message_id=payload["message_id"],
            member_id=member["id"],
            member_name=member.get("name") or "Member",
            text=payload["text"],
        )
        self._send_json(202, {"status": "accepted" if accepted else "duplicate"})

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            return self._send_json(200, {"status": "ok"})
        conversation_id = self._conversation_id()
        if conversation_id is None:
            return self._send_json(404, {"error": "not_found"})
        if not self._authorised():
            return self._send_json(401, {"error": "unauthorised"})

        messages = self.server.concierge.conversation_messages(conversation_id)
        body = {
            "messages": [
                {
                    "id": message.id,
                    "role": message.role,
                    "text": message.text,
                    "created_at": message.created_at,
                }
                for message in messages
            ]
        }
        self._send_json(200, body)

    # Helpers -------------------------------------------------------------

    def _conversation_id(self) -> str | None:
        match = MESSAGES_PATH.match(urlsplit(self.path).path)
        return unquote(match.group(1)) if match else None

    def _authorised(self) -> bool:
        expected = f"Bearer {self.server.token}"
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - matches base signature
        # Members' clients poll GET every 2 seconds. Log only writes and errors to keep the log readable.
        if self.command == "GET" and args and str(args[1]).startswith("2"):
            return
        log.info(format, *args)


def _validate_member_message(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "body must be an object"
    message_id = payload.get("message_id")
    if not _is_id(message_id):
        return "message_id must be a non-empty string"
    member = payload.get("member")
    if not isinstance(member, dict) or not _is_id(member.get("id")):
        return "member.id must be a non-empty string"
    if "name" in member and not isinstance(member["name"], str):
        return "member.name must be a string"
    text = payload.get("text")
    if not isinstance(text, str):
        return "text must be a string"
    if len(text) > MAX_TEXT_CHARS:
        return f"text must be at most {MAX_TEXT_CHARS} characters"
    payload["text"] = text.strip()
    return None


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_ID_CHARS


def build_server(settings: Settings, concierge: Concierge) -> ConciergeServer:
    return ConciergeServer((settings.host, settings.port), concierge, settings.token)
