from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from project_gate.api import JsonApplication
from project_gate.clock import FrozenClock
from project_gate.service import GateService


POLICY = {
    "policy_id": "pol",
    "version": 1,
    "name": "规则",
    "effective_date": "2026-01-01",
    "stages": ["INIT", "PREP"],
    "gates": [
        {"gate_code": "LAND", "name": "用地", "kind": "LAND", "owner_dept": "NATRES",
         "applies": "REQUIRED", "evidence_kind": "DOCUMENT",
         "evidence_requirement": "用地批复", "valid_days": 365},
    ],
    "stage_gates": {"PREP": ["LAND"]},
    "decision_authority": {"INIT": "approver", "PREP": "approver"},
}


def call(app: JsonApplication, method: str, path: str, payload=None, actor: str = "coord"):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    headers = {"X-Actor-Id": actor} if actor else {}
    return app.handle(method, path, headers, body)


class GateApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        clock = FrozenClock(datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc))
        self.service = GateService(self.connection, clock)
        self.app = JsonApplication(self.service)
        self.service.create_user("coord", "协调人", "coordinator", None)
        self.service.create_user("natres", "用地", "department", "NATRES")
        self.service.create_user("finance", "财政", "department", "FINANCE")
        self.service.create_user("chief", "批准人", "approver", "JOINT")

    def tearDown(self) -> None:
        self.connection.close()

    def _bootstrap(self) -> None:
        call(self.app, "POST", "/policies", POLICY, actor="chief")
        call(self.app, "POST", "/projects", {
            "project_id": "p1", "name": "项目", "owner_org": "org", "manager_id": "coord",
            "policy_id": "pol", "initial_stage": "INIT", "budget_cny": "1000",
        })

    def test_health(self) -> None:
        response = call(self.app, "GET", "/health", actor="")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_requires_actor_header(self) -> None:
        response = call(self.app, "GET", "/projects/p1", actor="")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_bad_json_rejected(self) -> None:
        response = self.app.handle("POST", "/policies", {"X-Actor-Id": "chief"}, b"not-json")
        self.assertEqual(response.status, 422)

    def test_order_blocked_then_issued_over_http(self) -> None:
        self._bootstrap()
        call(self.app, "POST", "/budgets", {"project_id": "p1", "stage": "PREP",
                                            "allocated_cny": "400"})
        call(self.app, "POST", "/commitments", {"commitment_id": "cm1", "project_id": "p1",
             "stage": "PREP", "source": "财政", "amount_cny": "400", "document_ref": "D1"},
             actor="finance")
        blocked = call(self.app, "POST", "/projects/p1/orders", {"idempotency_key": "k1"},
                       actor="chief")
        self.assertEqual(blocked.status, 409)
        self.assertEqual(blocked.body["error"]["code"], "invalid_state")

        status = call(self.app, "GET", "/projects/p1")
        self.assertEqual(status.status, 200)
        self.assertEqual([b["gate_code"] for b in status.body["blockers"]], ["LAND"])
        self.assertEqual(status.body["decision_authority"]["required_decision_role"], "approver")
        self.assertEqual(status.body["stages"][1]["fund"]["gap_cny"], "0")

        evidence = call(self.app, "POST", "/evidences", {
            "evidence_id": "ev1", "project_id": "p1", "stage": "PREP", "gate_code": "LAND",
            "kind": "DOCUMENT", "title": "用地批复", "document_ref": "LAND-1",
            "issued_by": "自然资源局", "issued_at": "2026-08-25",
        }, actor="natres")
        self.assertEqual(evidence.status, 201)

        issued = call(self.app, "POST", "/projects/p1/orders", {"idempotency_key": "k1"},
                      actor="chief")
        self.assertEqual(issued.status, 201)
        self.assertEqual(issued.body["to_stage"], "PREP")
        self.assertEqual(issued.body["state"], "issued")

        replay = call(self.app, "POST", "/projects/p1/orders", {"idempotency_key": "k1"},
                      actor="chief")
        self.assertEqual(replay.body["order_id"], issued.body["order_id"])

    def test_exception_lifecycle_over_http(self) -> None:
        self._bootstrap()
        call(self.app, "POST", "/budgets", {"project_id": "p1", "stage": "PREP",
                                            "allocated_cny": "400"})
        call(self.app, "POST", "/commitments", {"commitment_id": "cm1", "project_id": "p1",
             "stage": "PREP", "source": "财政", "amount_cny": "400", "document_ref": "D1"},
             actor="finance")
        granted = call(self.app, "POST", "/exceptions", {
            "project_id": "p1", "stage": "PREP", "scope_gates": ["LAND"],
            "reason": "紧急先行", "expires_at": "2026-09-10T08:00:00Z",
            "exception_id": "exc-1",
        }, actor="chief")
        self.assertEqual(granted.status, 201)
        self.assertEqual(granted.body["scope_gates"], ["LAND"])
        issued = call(self.app, "POST", "/projects/p1/orders", {"idempotency_key": "k-exc"},
                      actor="chief")
        self.assertEqual(issued.status, 201)
        self.assertEqual(issued.body["exception_ids"], ["exc-1"])

        revoked = call(self.app, "POST", "/exceptions/exc-1/revoke", {"note": "撤销"},
                       actor="chief")
        self.assertEqual(revoked.status, 200)
        self.assertEqual(revoked.body["state"], "revoked")

    def test_unknown_route_and_missing_field(self) -> None:
        self.assertEqual(call(self.app, "GET", "/nope").status, 404)
        response = call(self.app, "POST", "/projects", {"project_id": "p1"}, actor="coord")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")


if __name__ == "__main__":
    unittest.main()
