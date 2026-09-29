from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from project_gate.clock import FrozenClock
from project_gate.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from project_gate.service import GateService


POLICY = {
    "policy_id": "pol",
    "version": 1,
    "name": "规则",
    "effective_date": "2026-01-01",
    "stages": ["INIT", "PREP", "BUILD"],
    "gates": [
        {"gate_code": "LAND", "name": "用地", "kind": "LAND", "owner_dept": "NATRES",
         "applies": "REQUIRED", "evidence_kind": "DOCUMENT",
         "evidence_requirement": "用地批复", "valid_days": 365},
        {"gate_code": "EIA", "name": "环评", "kind": "EIA", "owner_dept": "ECOENV",
         "applies": "REQUIRED", "evidence_kind": "DOCUMENT",
         "evidence_requirement": "环评批复", "valid_days": None},
        {"gate_code": "WAIVER", "name": "可选事项", "kind": "PERMIT", "owner_dept": "INDUSTRY",
         "applies": "EXEMPTIBLE", "evidence_kind": "DOCUMENT",
         "evidence_requirement": "备案文件", "valid_days": None},
    ],
    "stage_gates": {"PREP": ["LAND"], "BUILD": ["EIA", "WAIVER"]},
    "decision_authority": {"INIT": "approver", "PREP": "approver", "BUILD": "approver:JOINT"},
}

POLICY_V2 = {
    **POLICY,
    "version": 2,
    "effective_date": "2026-06-01",
    "gates": [
        *POLICY["gates"],
        {"gate_code": "ENERGY", "name": "能耗", "kind": "POLICY", "owner_dept": "DRC",
         "applies": "REQUIRED", "evidence_kind": "SYSTEM_RECORD",
         "evidence_requirement": "能耗指标记录", "valid_days": None},
    ],
    "stage_gates": {"PREP": ["LAND"], "BUILD": ["EIA", "WAIVER", "ENERGY"]},
}


class GateTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc))
        self.service = GateService(self.connection, self.clock)
        users = (
            ("coord", "协调人", "coordinator", None),
            ("natres", "用地", "department", "NATRES"),
            ("ecoenv", "环评", "department", "ECOENV"),
            ("finance", "财政", "department", "FINANCE"),
            ("industry", "行业", "department", "INDUSTRY"),
            ("drc", "发改", "department", "DRC"),
            ("chief", "批准人", "approver", "JOINT"),
            ("audit", "审计", "auditor", None),
        )
        for args in users:
            self.service.create_user(*args)
        self.service.publish_policy("chief", POLICY)
        self.service.create_project("coord", {
            "project_id": "p1", "name": "项目", "owner_org": "org", "manager_id": "coord",
            "policy_id": "pol", "initial_stage": "INIT", "budget_cny": "1000",
        })

    def tearDown(self) -> None:
        self.connection.close()

    def prep_budget_and_fund(self, budget: str = "400", amount: str = "400") -> None:
        self.service.set_stage_budget("coord", {"project_id": "p1", "stage": "PREP",
                                                "allocated_cny": budget})
        self.service.record_commitment("finance", {"commitment_id": "cm-1", "project_id": "p1",
            "stage": "PREP", "source": "财政", "amount_cny": amount, "document_ref": "DOC-1"})

    def satisfy_land(self) -> None:
        self.service.submit_evidence("natres", {
            "evidence_id": "ev-land", "project_id": "p1", "stage": "PREP",
            "gate_code": "LAND", "kind": "DOCUMENT", "title": "用地批复",
            "document_ref": "LAND-DOC", "issued_by": "自然资源局", "issued_at": "2026-08-25",
        })


class StageOrderTests(GateTestBase):
    def test_order_blocked_until_gates_budget_and_fund_ready(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.issue_stage_order("chief", "p1", "key-1")
        self.prep_budget_and_fund()
        with self.assertRaises(InvalidState):
            self.service.issue_stage_order("chief", "p1", "key-1")
        self.satisfy_land()
        order = self.service.issue_stage_order("chief", "p1", "key-1")
        self.assertEqual(order["to_stage"], "PREP")
        self.assertEqual(order["policy"]["version"], 1)
        self.assertEqual(self.service.project_status("coord", "p1")["current_stage"], "PREP")

    def test_missing_stage_budget_blocks_order(self) -> None:
        self.satisfy_land()
        self.service.record_commitment("finance", {"commitment_id": "cm-1", "project_id": "p1",
            "stage": "PREP", "source": "财政", "amount_cny": "400", "document_ref": "DOC-1"})
        with self.assertRaises(InvalidState):
            self.service.issue_stage_order("chief", "p1", "key-1")

    def test_fund_gap_blocks_order(self) -> None:
        self.prep_budget_and_fund(budget="400", amount="300")
        self.satisfy_land()
        with self.assertRaises(InvalidState) as ctx:
            self.service.issue_stage_order("chief", "p1", "key-1")
        self.assertIn("缺口 100", str(ctx.exception))

    def test_withdrawn_commitment_counts_as_unfunded(self) -> None:
        self.prep_budget_and_fund()
        self.service.withdraw_commitment("finance", "cm-1", "资金调整")
        self.satisfy_land()
        with self.assertRaises(InvalidState):
            self.service.issue_stage_order("chief", "p1", "key-1")

    def test_stage_budget_cannot_exceed_project_budget(self) -> None:
        self.service.set_stage_budget("coord", {"project_id": "p1", "stage": "PREP",
                                                "allocated_cny": "600"})
        with self.assertRaises(InvalidState):
            self.service.set_stage_budget("coord", {"project_id": "p1", "stage": "BUILD",
                                                    "allocated_cny": "500"})

    def test_idempotent_replay_and_cross_project_conflict(self) -> None:
        self.prep_budget_and_fund()
        self.satisfy_land()
        first = self.service.issue_stage_order("chief", "p1", "key-1")
        second = self.service.issue_stage_order("chief", "p1", "key-1")
        self.assertEqual(first["order_id"], second["order_id"])
        self.service.create_project("coord", {
            "project_id": "p2", "name": "项目2", "owner_org": "org", "manager_id": "coord",
            "policy_id": "pol", "initial_stage": "INIT", "budget_cny": "1000",
        })
        with self.assertRaises(Conflict):
            self.service.issue_stage_order("chief", "p2", "key-1")

    def test_decision_authority_level_enforced(self) -> None:
        self.prep_budget_and_fund()
        self.satisfy_land()
        # PREP 仅要求 approver 角色；进入 BUILD 还需要 JOINT 层级。
        order = self.service.issue_stage_order("chief", "p1", "key-prep")
        self.assertEqual(order["decision_role"], "approver")

    def test_approver_without_level_rejected(self) -> None:
        self.service.create_user("chief2", "普通批准人", "approver", "CITY")
        self.prep_budget_and_fund()
        self.satisfy_land()
        self.service.issue_stage_order("chief", "p1", "key-prep")
        self.service.set_stage_budget("coord", {"project_id": "p1", "stage": "BUILD",
                                                "allocated_cny": "400"})
        self.service.record_commitment("finance", {"commitment_id": "cm-2", "project_id": "p1",
            "stage": "BUILD", "source": "财政", "amount_cny": "400", "document_ref": "DOC-2"})
        self.service.submit_evidence("ecoenv", {"evidence_id": "ev-eia", "project_id": "p1",
            "stage": "BUILD", "gate_code": "EIA", "kind": "DOCUMENT", "title": "环评",
            "document_ref": "EIA-DOC", "issued_by": "生态环境局", "issued_at": "2026-08-25"})
        self.service.waive_gate("chief", "p1", "BUILD", "WAIVER", "合并办理")
        with self.assertRaises(Forbidden):
            self.service.issue_stage_order("chief2", "p1", "key-build")


class EvidenceAndWaiveTests(GateTestBase):
    def test_evidence_must_come_from_owner_department(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.submit_evidence("ecoenv", {
                "evidence_id": "ev-land", "project_id": "p1", "stage": "PREP",
                "gate_code": "LAND", "kind": "DOCUMENT", "title": "用地",
                "document_ref": "D1", "issued_by": "x", "issued_at": "2026-08-25"})

    def test_evidence_kind_must_match_gate_requirement(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.submit_evidence("natres", {
                "evidence_id": "ev-land", "project_id": "p1", "stage": "PREP",
                "gate_code": "LAND", "kind": "MEETING_MINUTE", "title": "会议纪要",
                "document_ref": "D1", "issued_by": "x", "issued_at": "2026-08-25"})

    def test_meeting_minute_cannot_substitute_for_land_permit(self) -> None:
        # “会议纪要写着已开工”不构成 LAND 门槛的有效证据。
        with self.assertRaises(ValidationFailed):
            self.service.submit_evidence("natres", {
                "evidence_id": "ev-min", "project_id": "p1", "stage": "PREP",
                "gate_code": "LAND", "kind": "MEETING_MINUTE", "title": "开工会议纪要",
                "document_ref": "MIN-1", "issued_by": "项目办", "issued_at": "2026-08-25"})

    def test_only_exemptible_gate_can_be_waived_with_note(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.waive_gate("chief", "p1", "PREP", "LAND", "不适用")
        with self.assertRaises(ValidationFailed):
            self.service.waive_gate("chief", "p1", "BUILD", "WAIVER", "  ")
        waived = self.service.waive_gate("chief", "p1", "BUILD", "WAIVER", "合并办理")
        self.assertEqual(waived["state"], "waived")

    def test_duplicate_document_ref_rejected(self) -> None:
        self.satisfy_land()
        # 凭据编号全局唯一：另一门槛的证据也不能复用同一文号。
        with self.assertRaises(Conflict):
            self.service.submit_evidence("ecoenv", {
                "evidence_id": "ev-eia-x", "project_id": "p1", "stage": "BUILD",
                "gate_code": "EIA", "kind": "DOCUMENT", "title": "环评",
                "document_ref": "LAND-DOC", "issued_by": "生态环境局", "issued_at": "2026-08-25"})


class ExceptionTests(GateTestBase):
    def _prepare_build(self) -> None:
        self.prep_budget_and_fund()
        self.satisfy_land()
        self.service.issue_stage_order("chief", "p1", "key-prep")
        self.service.set_stage_budget("coord", {"project_id": "p1", "stage": "BUILD",
                                                "allocated_cny": "400"})
        self.service.record_commitment("finance", {"commitment_id": "cm-2", "project_id": "p1",
            "stage": "BUILD", "source": "财政", "amount_cny": "400", "document_ref": "DOC-2"})
        # EIA 故意留空：紧急例外测试的正是未满足 EIA 时的临时放行。
        self.service.waive_gate("chief", "p1", "BUILD", "WAIVER", "合并办理")

    def test_exception_requires_scope_reason_and_expiry(self) -> None:
        self._prepare_build()
        with self.assertRaises(ValidationFailed):
            self.service.grant_exception("chief", "p1", "BUILD", [], "理由",
                                         self.clock.now().replace(microsecond=0).isoformat())
        with self.assertRaises(ValidationFailed):
            self.service.grant_exception("chief", "p1", "BUILD", ["ENERGY"], "理由",
                                         (self.clock.now() + timedelta(days=1)).isoformat())
        with self.assertRaises(ValidationFailed):
            self.service.grant_exception("chief", "p1", "BUILD", ["EIA"], "   ",
                                         (self.clock.now() + timedelta(days=1)).isoformat())
        with self.assertRaises(ValidationFailed):
            self.service.grant_exception("chief", "p1", "BUILD", ["EIA"], "理由",
                                         (self.clock.now() - timedelta(days=1)).isoformat())

    def test_active_exception_covers_gate_and_expiry_blocks_again(self) -> None:
        self._prepare_build()
        with self.assertRaises(InvalidState):
            self.service.issue_stage_order("chief", "p1", "key-build-early")
        self.service.grant_exception(
            "chief", "p1", "BUILD", ["EIA"], "紧急先行",
            (self.clock.now() + timedelta(days=2)).isoformat(), exception_id="exc-1")
        order = self.service.issue_stage_order("chief", "p1", "key-build")
        self.assertEqual(order["exception_ids"], ["exc-1"])
        self.clock.advance(days=3)
        # 例外到期后，历史阶段令保留，但状态视图重新暴露卡点。
        status = self.service.project_status("coord", "p1")
        codes = {gate["gate_code"] for stage in status["stages"] if stage["stage"] == "BUILD"
                 for gate in stage["gates"] if not gate["open"]}
        self.assertIn("EIA", codes)

    def test_exception_expiry_suspends_executing_package(self) -> None:
        self._prepare_build()
        self.service.add_work_package("coord", {"package_id": "wp1", "project_id": "p1",
            "stage": "BUILD", "title": "施工", "owner_dept": "INDUSTRY",
            "budget_cny": "100", "depends_on": []})
        self.service.grant_exception(
            "chief", "p1", "BUILD", ["EIA"], "紧急先行",
            (self.clock.now() + timedelta(days=2)).isoformat(),
            scope_packages=["wp1"], exception_id="exc-1")
        self.service.issue_stage_order("chief", "p1", "key-build")
        self.service.start_package("industry", "wp1")
        self.clock.advance(days=3)
        self.service.sweep_exception_expiry("chief", "p1")
        state = self.connection.execute(
            "SELECT state FROM work_packages WHERE package_id='wp1'").fetchone()["state"]
        self.assertEqual(state, "suspended")
        # 恢复条件：补齐 EIA 证据后才能复工。
        with self.assertRaises(InvalidState):
            self.service.resume_package("coord", "wp1")
        self.service.submit_evidence("ecoenv", {"evidence_id": "ev-eia-2", "project_id": "p1",
            "stage": "BUILD", "gate_code": "EIA", "kind": "DOCUMENT", "title": "环评补办",
            "document_ref": "EIA-DOC-2", "issued_by": "生态环境局", "issued_at": "2026-09-04"})
        self.assertEqual(self.service.resume_package("coord", "wp1")["state"], "executing")

    def test_revoke_exception_takes_effect_immediately(self) -> None:
        self._prepare_build()
        self.service.grant_exception(
            "chief", "p1", "BUILD", ["EIA"], "紧急先行",
            (self.clock.now() + timedelta(days=5)).isoformat(), exception_id="exc-1")
        self.service.revoke_exception("chief", "exc-1", "条件恢复，撤销例外")
        with self.assertRaises(InvalidState):
            self.service.issue_stage_order("chief", "p1", "key-build")


class PolicyVersionTests(GateTestBase):
    def test_new_policy_version_adds_gate_and_keeps_history(self) -> None:
        self.prep_budget_and_fund()
        self.satisfy_land()
        order = self.service.issue_stage_order("chief", "p1", "key-prep")
        self.assertEqual(order["policy"]["version"], 1)

        self.service.publish_policy("chief", POLICY_V2)
        self.service.apply_policy_version("coord", "p1", 2)
        status = self.service.project_status("coord", "p1")
        self.assertEqual(status["policy"]["version"], 2)
        impact_types = [item["type"] for item in status["open_impacts"]]
        self.assertIn("POLICY_VERSION", impact_types)
        # 历史阶段令快照仍然引用 v1 及其 sha256。
        prep = next(o for o in status["stage_orders"] if o["to_stage"] == "PREP")
        self.assertEqual(prep["policy_version"], 1)
        self.assertEqual(prep["policy_sha256"], order["policy"]["sha256"])

    def test_cannot_skip_or_downgrade_version(self) -> None:
        self.service.publish_policy("chief", POLICY_V2)
        # v3 尚不存在：无法迁移到不存在的版本。
        with self.assertRaises(NotFound):
            self.service.apply_policy_version("coord", "p1", 3)
        # 不能降级回旧版本。
        self.service.apply_policy_version("coord", "p1", 2)
        with self.assertRaises(InvalidState):
            self.service.apply_policy_version("coord", "p1", 1)

    def test_policy_version_must_be_sequential(self) -> None:
        v3 = {**POLICY_V2, "version": 3}
        with self.assertRaises(Conflict):
            self.service.publish_policy("chief", v3)

    def test_equivalent_gate_carries_over_without_new_impact(self) -> None:
        self.prep_budget_and_fund()
        self.satisfy_land()
        self.service.issue_stage_order("chief", "p1", "key-prep")
        # v2 只改名称等显示字段，LAND 规格等价时沿用，不产生针对 LAND 的新影响。
        v2 = {
            **POLICY, "version": 2, "effective_date": "2026-06-01",
            "gates": [
                {**POLICY["gates"][0], "name": "用地审批（新名称）"},
                *POLICY["gates"][1:],
            ],
        }
        self.service.publish_policy("chief", v2)
        self.service.apply_policy_version("coord", "p1", 2)
        status = self.service.project_status("coord", "p1")
        self.assertEqual(status["open_impacts"], [])
        gate = self.connection.execute(
            "SELECT state FROM project_gates WHERE project_id='p1' AND policy_version=2 "
            "AND stage='PREP' AND gate_code='LAND'").fetchone()
        self.assertEqual(gate["state"], "satisfied")


class PackageAndInvalidationTests(GateTestBase):
    def _enter_prep_with_package(self) -> None:
        self.prep_budget_and_fund()
        self.satisfy_land()
        self.service.issue_stage_order("chief", "p1", "key-prep")
        self.service.add_work_package("coord", {"package_id": "wp1", "project_id": "p1",
            "stage": "PREP", "title": "勘察", "owner_dept": "NATRES",
            "budget_cny": "100", "depends_on": []})

    def test_active_exception_allows_resume_while_gate_impact_stays_open(self) -> None:
        self._enter_prep_with_package()
        self.service.start_package("natres", "wp1")
        self.service.invalidate_evidence("chief", "ev-land", "用地批复被撤销")
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM work_packages WHERE package_id='wp1'").fetchone()["state"],
            "suspended")
        # 在证据补齐前，批准人签发限定范围、理由和到期时间的紧急例外，允许先行复工。
        self.service.grant_exception(
            "chief", "p1", "PREP", ["LAND"],
            "行政复议期间批复暂继续有效，限勘察工作包",
            (self.clock.now() + timedelta(days=7)).isoformat(),
            scope_packages=["wp1"], exception_id="exc-land")
        self.assertEqual(self.service.resume_package("coord", "wp1")["state"], "executing")
        # 例外到期后若证据仍未补齐，工作包再次被暂停。
        self.clock.advance(days=8)
        self.service.sweep_exception_expiry("chief", "p1")
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM work_packages WHERE package_id='wp1'").fetchone()["state"],
            "suspended")

    def test_package_cannot_start_before_stage_order(self) -> None:
        self.service.add_work_package("coord", {"package_id": "wp1", "project_id": "p1",
            "stage": "PREP", "title": "勘察", "owner_dept": "NATRES",
            "budget_cny": "100", "depends_on": []})
        with self.assertRaises(InvalidState):
            self.service.start_package("natres", "wp1")

    def test_dependency_must_finish_first(self) -> None:
        self._enter_prep_with_package()
        self.service.add_work_package("coord", {"package_id": "wp2", "project_id": "p1",
            "stage": "PREP", "title": "设计", "owner_dept": "NATRES",
            "budget_cny": "100", "depends_on": ["wp1"]})
        with self.assertRaises(InvalidState):
            self.service.start_package("natres", "wp2")
        self.service.start_package("natres", "wp1")
        self.service.complete_package("natres", "wp1")
        self.assertEqual(self.service.start_package("natres", "wp2")["state"], "executing")

    def test_invalidating_evidence_suspends_executing_package(self) -> None:
        self._enter_prep_with_package()
        self.service.start_package("natres", "wp1")
        self.service.invalidate_evidence("chief", "ev-land", "用地批复被撤销")
        state = self.connection.execute(
            "SELECT state FROM work_packages WHERE package_id='wp1'").fetchone()["state"]
        self.assertEqual(state, "suspended")
        status = self.service.project_status("coord", "p1")
        impact = next(i for i in status["open_impacts"] if i["type"] == "EVIDENCE_INVALID")
        self.assertIn("owner_dept", impact["recovery"])
        self.assertEqual(impact["recovery"]["owner_dept"], "NATRES")
        # 历史没有阶段令被删除或改写。
        self.assertEqual(len(status["stage_orders"]), 1)
        with self.assertRaises(InvalidState):
            self.service.resume_package("coord", "wp1")
        # 重新提交有效证据后影响自动消除，可以复工。
        self.service.submit_evidence("natres", {
            "evidence_id": "ev-land-2", "project_id": "p1", "stage": "PREP",
            "gate_code": "LAND", "kind": "DOCUMENT", "title": "新用地批复",
            "document_ref": "LAND-DOC-2", "issued_by": "自然资源局", "issued_at": "2026-09-02"})
        self.assertEqual(self.service.resume_package("coord", "wp1")["state"], "executing")

    def test_expired_evidence_is_detected(self) -> None:
        # 用地证据有效期 365 天；签发日期为 2025 年，当前已过期。
        self.service.submit_evidence("natres", {
            "evidence_id": "ev-land", "project_id": "p1", "stage": "PREP",
            "gate_code": "LAND", "kind": "DOCUMENT", "title": "用地批复",
            "document_ref": "LAND-DOC", "issued_by": "自然资源局", "issued_at": "2025-08-01"})
        # 即使不主动扫描，状态视图也按日期暴露过期卡点。
        status = self.service.project_status("coord", "p1")
        self.assertTrue(any(not g["open"] and g["gate_code"] == "LAND"
                            for g in status["blockers"]))
        # 显式扫描会把门槛状态持久化为 expired，并登记恢复条件。
        self.service.sweep_evidence_expiry("chief", "p1")
        gate = self.connection.execute(
            "SELECT state FROM project_gates WHERE project_id='p1' AND stage='PREP' "
            "AND gate_code='LAND'").fetchone()
        self.assertEqual(gate["state"], "expired")
        self.prep_budget_and_fund()
        with self.assertRaises(InvalidState):
            self.service.issue_stage_order("chief", "p1", "key-prep")


class RoleTests(GateTestBase):
    def test_coordinator_cannot_publish_policy(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.publish_policy("coord", POLICY_V2)

    def test_department_cannot_issue_order(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.issue_stage_order("finance", "p1", "k")

    def test_finance_only_registers_commitments(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.record_commitment("natres", {
                "commitment_id": "cm-x", "project_id": "p1", "stage": "PREP",
                "source": "x", "amount_cny": "1", "document_ref": "D"})

    def test_auditor_reads_chain_only(self) -> None:
        chain = self.service.audit_chain("audit")
        self.assertTrue(chain["valid"])
        with self.assertRaises(Forbidden):
            self.service.set_stage_budget("audit", {"project_id": "p1", "stage": "PREP",
                                                    "allocated_cny": "1"})


class StatusViewTests(GateTestBase):
    def test_status_reports_blockers_authority_and_commitments(self) -> None:
        self.prep_budget_and_fund()
        status = self.service.project_status("coord", "p1")
        self.assertEqual(status["next_stage"], "PREP")
        self.assertEqual(status["decision_authority"]["required_decision_role"], "approver")
        self.assertEqual([b["gate_code"] for b in status["blockers"]], ["LAND"])
        blocker = status["blockers"][0]
        self.assertEqual(blocker["owner_dept"], "NATRES")
        self.assertIn("用地批复", blocker["evidence_requirement"])
        prep_view = next(s for s in status["stages"] if s["stage"] == "PREP")
        self.assertEqual(prep_view["fund"]["committed_total_cny"], "400")
        self.assertEqual(prep_view["fund"]["commitments"][0]["document_ref"], "DOC-1")

    def test_unknown_project_raises_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.project_status("coord", "missing")


if __name__ == "__main__":
    unittest.main()
