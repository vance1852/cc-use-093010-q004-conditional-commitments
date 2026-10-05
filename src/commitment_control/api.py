"""无第三方依赖的条件化承诺管理 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import CommitmentError, ValidationFailed
from .service import CommitmentControlService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Any


class JsonApplication:
    def __init__(self, service: CommitmentControlService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            service = self.service

            if method == "POST" and path == "/users":
                return Response(201, service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/programs":
                return Response(201, service.register_program(
                    actor, payload["program_id"], payload["name"], payload["lead_office"]))
            if method == "POST" and path == "/milestones":
                return Response(201, service.register_milestone(
                    actor, payload["milestone_id"], payload["program_id"], payload["title"]))
            if method == "POST" and path == "/pools":
                return Response(201, service.create_pool(
                    actor, payload["pool_id"], payload["resource_type"], payload["total_quota"]))
            if method == "GET" and len(parts) == 3 and parts[:2] == ["pools", "status"]:
                return Response(200, service.pool_status(parts[2]))
            if method == "POST" and path == "/commitments":
                return Response(201, service.freeze_commitment(
                    actor, payload, payload.get("idempotency_key", "")))
            if method == "GET" and len(parts) == 2 and parts[0] == "commitments":
                return Response(200, service.get_commitment(parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "commitments":
                return Response(200, service.get_commitment(parts[1], int(parts[2])))
            if method == "POST" and path == "/evidence/evaluate":
                return Response(200, service.evaluate_evidence(
                    actor, payload["condition_id"], payload["evidence_ref"],
                    payload["outcome"], payload.get("note", "")))
            if method == "POST" and path == "/conditions/decide":
                return Response(200, service.decide_condition(
                    actor, payload["condition_id"], payload["outcome"], payload.get("note", "")))
            if method == "GET" and len(parts) == 3 and parts[:2] == ["conditions", "status"]:
                return Response(200, service.condition_status(parts[2]))
            if method == "POST" and path == "/deliveries":
                return Response(201, service.record_delivery(
                    actor, payload["resource_version_id"], payload["amount"],
                    payload["evidence_ref"], payload["content_sha256"], payload["idempotency_key"]))
            if method == "POST" and path == "/sweep":
                return Response(200, service.sweep_due(actor))
            if method == "POST" and len(parts) == 3 and parts[:2] == ["pools", "promote"]:
                return Response(200, service.promote_standby(actor, parts[2]))
            if method == "GET" and len(parts) == 3 and parts[:2] == ["milestones", "explain"]:
                return Response(200, service.explain_milestone(parts[2]))
            if method == "GET" and len(parts) == 3 and parts[:2] == ["resources", "trace"]:
                return Response(200, service.resource_trace(actor, parts[2]))
            if method == "GET" and path == "/notifications":
                return Response(200, service.list_notifications(
                    actor, query.get("scope_type", [None])[0], query.get("scope_id", [None])[0],
                    int(query.get("limit", ["100"])[0])))
            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except CommitmentError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CommitmentControl/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动条件化承诺管理服务")
    parser.add_argument("--database", type=Path, default=Path("commitment-control.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(CommitmentControlService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
