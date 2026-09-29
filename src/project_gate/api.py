"""无第三方依赖的重大项目阶段门控 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import GateError, ValidationFailed
from .service import GateService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: GateService) -> None:
        self.service = service
        # 单个 SQLite 连接在 ThreadingHTTPServer 的工作线程间共享；用请求级锁
        # 串行化，配合 BEGIN IMMEDIATE 与 busy_timeout 保证事务安全。
        self._lock = threading.RLock()

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

    @staticmethod
    def _text(payload: Mapping[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailed(f"{key} 不能为空")
        return value.strip()

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        with self._lock:
            return self._handle(method, target, headers, body)

    def _handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
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
                    payload["user_id"], payload["display_name"], payload["role"], payload.get("dept")
                ))

            if method == "POST" and path == "/policies":
                return Response(201, service.publish_policy(actor, payload))

            if method == "POST" and path == "/projects":
                return Response(201, service.create_project(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "projects":
                return Response(200, service.project_status(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "policy":
                return Response(200, service.apply_policy_version(actor, parts[1], int(payload["version"])))

            if method == "POST" and path == "/budgets":
                return Response(201, service.set_stage_budget(actor, payload))
            if method == "POST" and path == "/commitments":
                return Response(201, service.record_commitment(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "commitments" and parts[2] == "withdraw":
                return Response(200, service.withdraw_commitment(actor, parts[1], self._text(payload, "reason")))

            if method == "POST" and path == "/packages":
                return Response(201, service.add_work_package(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "packages" and parts[2] == "start":
                return Response(200, service.start_package(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "packages" and parts[2] == "complete":
                return Response(200, service.complete_package(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "packages" and parts[2] == "resume":
                return Response(200, service.resume_package(actor, parts[1]))

            if method == "POST" and path == "/evidences":
                return Response(201, service.submit_evidence(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "evidences" and parts[2] == "invalidate":
                return Response(200, service.invalidate_evidence(actor, parts[1], self._text(payload, "reason")))
            if method == "POST" and len(parts) == 4 and parts[0] == "projects" and parts[2] == "gates" and parts[3] == "waive":
                # POST /projects/{id}/gates/waive，body 指定 stage、gate_code、note
                return Response(200, service.waive_gate(
                    actor, parts[1], self._text(payload, "stage"), self._text(payload, "gate_code"),
                    self._text(payload, "note"),
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "sweep-evidence":
                return Response(200, service.sweep_evidence_expiry(actor, parts[1]))

            if method == "POST" and path == "/exceptions":
                return Response(201, service.grant_exception(
                    actor,
                    self._text(payload, "project_id"),
                    self._text(payload, "stage"),
                    payload["scope_gates"],
                    self._text(payload, "reason"),
                    self._text(payload, "expires_at"),
                    payload.get("scope_packages") or [],
                    payload.get("exception_id"),
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "exceptions" and parts[2] == "revoke":
                return Response(200, service.revoke_exception(actor, parts[1], self._text(payload, "note")))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "sweep-exceptions":
                return Response(200, service.sweep_exception_expiry(actor, parts[1]))

            if method == "POST" and len(parts) == 3 and parts[0] == "impacts" and parts[2] == "resolve":
                return Response(200, service.resolve_impact(actor, int(parts[1]), self._text(payload, "note")))

            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "orders":
                return Response(201, service.issue_stage_order(
                    actor, parts[1], self._text(payload, "idempotency_key"), payload.get("note", "")
                ))

            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except GateError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ProjectGate/1"

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
    parser = argparse.ArgumentParser(description="启动重大项目阶段门控服务")
    parser.add_argument("--database", type=Path, default=Path("project_gate.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database, check_same_thread=False)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(GateService(connection))))
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
