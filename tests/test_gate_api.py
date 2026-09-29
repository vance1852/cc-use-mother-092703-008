from __future__ import annotations

import json
import sqlite3
import unittest

from project_gate.api import JsonApplication
from project_gate.service import GateService


def sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class GateApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(GateService(self.connection))
        for uid, role in (("plan", "planner"), ("pm", "director"), ("risk", "risk"),
                          ("fin", "finance"), ("dept", "department")):
            self.call("POST", "/users", {"user_id": uid, "display_name": uid, "role": role})
        self.call("POST", "/projects", {
            "project_id": "p1", "name": "项目", "owner_org": "项目办",
            "director_id": "pm", "tags": ["dc"],
        }, actor="pm")
        self.call("POST", "/gates", {
            "gate_id": "g1", "gate_order": 1, "name": "开工门", "decision_role": "director",
            "stage_budget_cny": "1000",
            "conditions": [
                {"condition_id": "land", "condition_type": "evidence", "title": "用地",
                 "responsible_dept": "自然资源部门", "authority": "土地法", "rule_version": "r1",
                 "evidence": {"doc_kind": "用地证", "accepted_sources": ["自然资源部门"]}},
                {"condition_id": "funds", "condition_type": "budget", "title": "资金",
                 "responsible_dept": "财政部门", "authority": "协议", "rule_version": "r1",
                 "threshold_cny": "1000"},
            ],
        }, actor="plan")

    def tearDown(self) -> None:
        self.connection.close()

    def call(self, method: str, path: str, payload: dict | None = None, actor: str = "pm"):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
        headers = {"X-Actor-Id": actor}
        return self.app.handle(method, path, headers, body)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)

    def test_missing_actor(self) -> None:
        response = self.app.handle("GET", "/projects/p1/dashboard")
        self.assertEqual(response.status, 422)

    def test_blocked_then_full_flow_and_dashboard(self) -> None:
        blocked = self.call("POST", "/projects/p1/gates/1/issue", {"note": "先试试"})
        self.assertEqual(blocked.status, 409)
        self.assertEqual(blocked.body["error"]["code"], "gating_blocked")
        self.assertTrue(blocked.body["error"]["details"]["blockers"])

        ev = self.call("POST", "/projects/p1/evidence", {
            "evidence_id": "e1", "condition_id": "land", "doc_number": "12",
            "doc_kind": "用地证", "source": "自然资源部门", "content_sha256": sha("x"),
            "issued_at": "2026-09-01T00:00:00Z", "submitted_by_dept": "自然资源部门",
        }, actor="dept")
        self.assertEqual(ev.status, 201)
        review = self.call("POST", "/projects/p1/evidence/e1/review",
                           {"accepted": True, "note": "受理"}, actor="risk")
        self.assertEqual(review.status, 200)
        fund = self.call("POST", "/projects/p1/commitments", {
            "commitment_id": "c1", "gate_order": 1, "fund_source": "省专项",
            "amount_cny": "1000", "state": "committed", "doc_number": "财1",
        }, actor="fin")
        self.assertEqual(fund.status, 201)

        status = self.call("GET", "/projects/p1/gates/1/status")
        self.assertEqual(status.body["decision"], "approved")
        issued = self.call("POST", "/projects/p1/gates/1/issue", {"note": "签发"})
        self.assertEqual(issued.status, 201)
        self.assertEqual(issued.body["gate_version"], 1)

        dashboard = self.call("GET", "/projects/p1/dashboard")
        self.assertEqual(dashboard.status, 200)
        self.assertEqual(dashboard.body["gates"][0]["issued"]["gate_id"], "g1")
        self.assertEqual(dashboard.body["commitments"][0]["amount_cny"], "1000")

    def test_override_flow_over_http(self) -> None:
        granted = self.call("POST", "/projects/p1/overrides", {
            "override_id": "ov1", "scope": "condition", "target_id": "land",
            "reason": "系统迁移", "expires_at": "2026-10-01T00:00:00Z",
            "boundary": {"work_allowance": "不扩面", "max_amount_cny": "0", "conditions": []},
        }, actor="risk")
        self.assertEqual(granted.status, 201)
        status = self.call("GET", "/projects/p1/gates/1/status")
        # 资金仍缺，land 被例外覆盖
        land = next(c for c in status.body["conditions"] if c["condition_id"] == "land")
        self.assertTrue(land["waived"])
        blocker_ids = {b["condition_id"] for b in status.body["blockers"]}
        self.assertEqual(blocker_ids, {"funds"})

    def test_unknown_route(self) -> None:
        response = self.call("GET", "/nope")
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
