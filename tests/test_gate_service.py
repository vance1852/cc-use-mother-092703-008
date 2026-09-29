from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from project_gate.clock import FrozenClock
from project_gate.errors import Conflict, Forbidden, GatingBlocked, InvalidState, ValidationFailed
from project_gate.service import GateService


def sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class GateServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        self.service = GateService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"), ("pm", "director"), ("risk", "risk"),
            ("fin", "finance"), ("audit", "auditor"), ("dept", "department"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.pid = "p1"
        self.service.create_project("pm", {
            "project_id": self.pid, "name": "测试项目", "owner_org": "项目办",
            "director_id": "pm", "tags": ["dc"],
        })
        self.service.publish_gate("plan", {
            "gate_id": "g1v1", "gate_order": 1, "name": "开工门",
            "decision_role": "director", "stage_budget_cny": "1000",
            "conditions": [
                {"condition_id": "land", "condition_type": "evidence", "title": "用地",
                 "responsible_dept": "自然资源部门", "authority": "土地法", "rule_version": "r1",
                 "evidence": {"doc_kind": "用地证", "accepted_sources": ["自然资源部门"]}},
                {"condition_id": "rail-only", "condition_type": "evidence", "title": "铁路专用线",
                 "responsible_dept": "交通部门", "authority": "铁路法", "rule_version": "r1",
                 "evidence": {"doc_kind": "专用线意见", "accepted_sources": ["交通部门"]},
                 "applicability": {"required_tags": ["rail"]}},
                {"condition_id": "funds", "condition_type": "budget", "title": "资金",
                 "responsible_dept": "财政部门", "authority": "资金协议", "rule_version": "r1",
                 "threshold_cny": "1000"},
            ],
        })
        self.service.add_package("pm", self.pid, {
            "package_id": "wp1", "name": "基础", "gate_order": 1,
            "requires_conditions": ["land", "funds"], "responsible_dept": "总包",
        })

    def tearDown(self) -> None:
        self.connection.close()

    def _submit_land(self, accepted: bool = True) -> None:
        self.service.submit_evidence("dept", self.pid, {
            "evidence_id": "ev-land", "condition_id": "land",
            "doc_number": "12 号", "doc_kind": "用地证", "source": "自然资源部门",
            "content_sha256": sha("land"), "issued_at": "2026-09-01T00:00:00Z",
            "submitted_by_dept": "自然资源部门",
        })
        if accepted:
            self.service.review_evidence("risk", self.pid, "ev-land", True, "受理")

    def test_not_applicable_condition_ignored(self) -> None:
        status = self.service.gate_status("pm", self.pid, 1)
        states = {c["condition_id"]: c["state"] for c in status["conditions"]}
        self.assertEqual(states["rail-only"], "not_applicable")
        self.assertEqual(states["land"], "pending")

    def test_submitted_but_not_reviewed_evidence_does_not_count(self) -> None:
        self._submit_land(accepted=False)
        status = self.service.gate_status("pm", self.pid, 1)
        land = next(c for c in status["conditions"] if c["condition_id"] == "land")
        self.assertEqual(land["state"], "pending")

    def test_blocked_until_all_then_issue_once(self) -> None:
        with self.assertRaises(GatingBlocked):
            self.service.issue_order("pm", self.pid, 1)
        self._submit_land()
        self.service.record_commitment("fin", self.pid, {
            "commitment_id": "c1", "gate_order": 1, "fund_source": "省专项",
            "amount_cny": "1000", "state": "committed", "doc_number": "财 1 号",
        })
        order = self.service.issue_order("pm", self.pid, 1, note="齐备")
        self.assertEqual(order["decision"], "issued")
        self.assertEqual(len(order["basis_sha256"]), 64)
        with self.assertRaises(InvalidState):
            self.service.issue_order("pm", self.pid, 1)

    def test_frozen_and_withdrawn_commitments_do_not_count(self) -> None:
        self._submit_land()
        self.service.record_commitment("fin", self.pid, {
            "commitment_id": "c1", "gate_order": 1, "fund_source": "省专项",
            "amount_cny": "1000", "state": "committed", "doc_number": "财 1 号",
        })
        self.service.update_commitment_state("fin", self.pid, "c1", "frozen")
        with self.assertRaises(GatingBlocked):
            self.service.issue_order("pm", self.pid, 1)

    def test_package_requires_order_or_active_package_override(self) -> None:
        with self.assertRaises(GatingBlocked):
            self.service.advance_package("pm", self.pid, "wp1", "ready")
        expires = (self.clock.now() + timedelta(days=3)).isoformat().replace("+00:00", "Z")
        self.service.grant_override("risk", self.pid, {
            "override_id": "ov1", "scope": "package", "target_id": "wp1",
            "reason": "抢险", "expires_at": expires,
            "boundary": {"work_allowance": "仅基坑", "max_amount_cny": "100", "conditions": []},
        })
        advanced = self.service.advance_package("pm", self.pid, "wp1", "ready", "先行")
        self.assertIsNone(advanced["order_id"])
        self.clock.advance(days=4)
        with self.assertRaises(GatingBlocked):
            self.service.advance_package("pm", self.pid, "wp1", "in_progress")

    def test_override_requires_scope_reason_expiry(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.grant_override("risk", self.pid, {
                "override_id": "ov", "scope": "package", "target_id": "wp1",
                "reason": " ", "expires_at": "2026-09-25T00:00:00Z",
                "boundary": {"work_allowance": "x", "max_amount_cny": "1"},
            })
        with self.assertRaises(ValidationFailed):
            self.service.grant_override("risk", self.pid, {
                "override_id": "ov", "scope": "package", "target_id": "wp1",
                "reason": "过去的例外", "expires_at": "2026-09-01T00:00:00Z",
                "boundary": {"work_allowance": "x", "max_amount_cny": "1"},
            })

    def test_role_permissions(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.issue_order("fin", self.pid, 1)
        with self.assertRaises(Forbidden):
            self.service.publish_gate("pm", {
                "gate_id": "g9", "gate_order": 9, "name": "x", "decision_role": "director",
                "stage_budget_cny": "0", "conditions": [{
                    "condition_id": "c", "condition_type": "budget", "title": "t",
                    "responsible_dept": "d", "authority": "a", "threshold_cny": "1"},
                ],
            })
        with self.assertRaises(Forbidden):
            self.service.grant_override("fin", self.pid, {
                "override_id": "ov", "scope": "package", "target_id": "wp1",
                "reason": "r", "expires_at": "2026-09-25T00:00:00Z",
                "boundary": {"work_allowance": "x", "max_amount_cny": "1"},
            })

    def test_revise_gate_preserves_history_and_marks_impact(self) -> None:
        self._submit_land()
        self.service.record_commitment("fin", self.pid, {
            "commitment_id": "c1", "gate_order": 1, "fund_source": "省专项",
            "amount_cny": "1000", "state": "committed", "doc_number": "财 1 号",
        })
        order = self.service.issue_order("pm", self.pid, 1)
        self.service.advance_package("pm", self.pid, "wp1", "ready")
        self.service.revise_gate("plan", 1, {
            "gate_id": "g1v2", "gate_order": 1, "name": "开工门",
            "decision_role": "director", "stage_budget_cny": "1000",
            "conditions": [
                {"condition_id": "land", "condition_type": "evidence", "title": "用地",
                 "responsible_dept": "自然资源部门", "authority": "土地法", "rule_version": "r1",
                 "evidence": {"doc_kind": "用地证", "accepted_sources": ["自然资源部门"]}},
                {"condition_id": "energy", "condition_type": "evidence", "title": "节能",
                 "responsible_dept": "发改部门", "authority": "节能法", "rule_version": "r2",
                 "evidence": {"doc_kind": "节能意见", "accepted_sources": ["发改部门"]}},
                {"condition_id": "funds", "condition_type": "budget", "title": "资金",
                 "responsible_dept": "财政部门", "authority": "资金协议", "rule_version": "r1",
                 "threshold_cny": "1000"},
            ],
        })
        impact = self.service.assess_rule_change(
            "risk", self.pid, "rule_revised", ["energy"], 1, note="新增节能审查")
        hit = {p["package_id"]: p for p in impact["impacted_packages"]}
        self.assertIn("wp1", hit)
        self.assertTrue(hit["wp1"]["protected_by_order"])
        row = self.connection.execute(
            "SELECT gate_id,gate_version FROM stage_orders WHERE order_id=?", (order["order_id"],)
        ).fetchone()
        self.assertEqual((row["gate_id"], row["gate_version"]), ("g1v1", 1))
        # 新版本下节能成为新卡点
        status = self.service.gate_status("pm", self.pid, 1)
        self.assertIn("energy", {b["condition_id"] for b in status["blockers"]})

    def test_revoke_evidence_records_impact_without_deleting_history(self) -> None:
        self._submit_land()
        self.service.revoke_evidence("dept", self.pid, "ev-land", "文号撤销")
        row = self.connection.execute(
            "SELECT state FROM evidence_documents WHERE evidence_id='ev-land'"
        ).fetchone()
        self.assertEqual(row["state"], "revoked")
        impact = self.connection.execute(
            "SELECT change_kind,impacted_packages_json FROM rule_change_impacts ORDER BY impact_id"
        ).fetchone()
        self.assertEqual(impact["change_kind"], "condition_expired")
        self.assertIn("wp1", impact["impacted_packages_json"])

    def test_unknown_condition_in_package_rejected(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.add_package("pm", self.pid, {
                "package_id": "wpX", "name": "x", "gate_order": 1,
                "requires_conditions": ["ghost"], "responsible_dept": "d",
            })

    def test_audit_chain_is_valid(self) -> None:
        self._submit_land()
        chain = self.service.audit_chain("audit", self.pid)
        self.assertTrue(chain["valid"])
        self.assertGreater(chain["events"], 0)

    def test_revised_earlier_gate_blocks_next_order(self) -> None:
        # 第 1 阶段门签发
        self._submit_land()
        self.service.record_commitment("fin", self.pid, {
            "commitment_id": "c1", "gate_order": 1, "fund_source": "省专项",
            "amount_cny": "1000", "state": "committed", "doc_number": "财 1 号",
        })
        self.service.issue_order("pm", self.pid, 1)
        # 发布第 2 阶段门并满足其自身门槛
        self.service.publish_gate("plan", {
            "gate_id": "g2", "gate_order": 2, "name": "设备门", "decision_role": "director",
            "stage_budget_cny": "0", "conditions": [
                {"condition_id": "funds2", "condition_type": "budget", "title": "资金",
                 "responsible_dept": "财政部门", "authority": "协议", "rule_version": "r1",
                 "threshold_cny": "1"},
            ],
        })
        self.service.record_commitment("fin", self.pid, {
            "commitment_id": "c2", "gate_order": 2, "fund_source": "省专项",
            "amount_cny": "1", "state": "committed", "doc_number": "财 2 号",
        })
        # 第 1 门换版新增 energy 门槛且未满足
        self.service.revise_gate("plan", 1, {
            "gate_id": "g1v2", "gate_order": 1, "name": "开工门", "decision_role": "director",
            "stage_budget_cny": "1000",
            "conditions": [
                {"condition_id": "land", "condition_type": "evidence", "title": "用地",
                 "responsible_dept": "自然资源部门", "authority": "土地法", "rule_version": "r1",
                 "evidence": {"doc_kind": "用地证", "accepted_sources": ["自然资源部门"]}},
                {"condition_id": "energy", "condition_type": "evidence", "title": "节能",
                 "responsible_dept": "发改部门", "authority": "节能法", "rule_version": "r2",
                 "evidence": {"doc_kind": "节能意见", "accepted_sources": ["发改部门"]}},
                {"condition_id": "funds", "condition_type": "budget", "title": "资金",
                 "responsible_dept": "财政部门", "authority": "资金协议", "rule_version": "r1",
                 "threshold_cny": "1000"},
            ],
        })
        with self.assertRaises(GatingBlocked) as caught:
            self.service.issue_order("pm", self.pid, 2)
        self.assertEqual(caught.exception.details["gate_order"], 1)
        dashboard = self.service.dashboard("pm", self.pid)
        reopened = {(b["gate_order"], b["condition_id"]) for b in dashboard["reopened_alerts"]}
        self.assertIn((1, "energy"), reopened)


if __name__ == "__main__":
    unittest.main()
