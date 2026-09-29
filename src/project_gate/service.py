"""重大项目阶段门控的事务用例。

职责：项目与阶段门模板、门槛适用性、有效证据核验、资金承诺、
紧急例外、阶段令签发（门控）、规则换版影响分析、项目负责人看板。
历史批准一旦签发永不修改；规则变化只作用于尚未执行的工作包。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, parse_utc, utc_text
from .contracts import (
    validate_commitment,
    validate_condition,
    validate_evidence,
    validate_gate,
    validate_override,
    validate_package,
    validate_project,
)
from .errors import Conflict, Forbidden, GatingBlocked, InvalidState, NotFound, ValidationFailed
from .rules import (
    budget_ledger,
    canonical_json,
    evaluate_gate,
    gate_decision,
    impacted_packages,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"gate.write", "gate.revise", "package.write", "project.write", "dashboard.read"},
    "director": {
        "project.write",
        "package.write",
        "evidence.review",
        "order.issue",
        "override.grant",
        "override.revoke",
        "impact.recognize",
        "dashboard.read",
        "evidence.submit",
        "commitment.write",
    },
    "risk": {
        "evidence.review", "override.grant", "override.revoke", "impact.recognize",
        "dashboard.read", "audit.read",
    },
    "finance": {"commitment.write", "dashboard.read", "audit.read"},
    "auditor": {"audit.read", "dashboard.read"},
    "department": {"evidence.submit", "dashboard.read"},
}

# 门槛满足后才允许进入的工作包状态流转
PACKAGE_ADVANCE = {
    "planned": {"ready"},
    "ready": {"in_progress", "blocked"},
    "blocked": {"ready", "in_progress"},
    "in_progress": {"completed", "blocked"},
    "completed": set(),
    "cancelled": set(),
}

FUNDS_ACTIVE_STATES = ("committed", "disbursed")


class GateService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _now_dt(self) -> datetime:
        return self.clock.now()

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM gate_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
        project_id: str | None = None,
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM gate_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "project_id": project_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO gate_audit_events(entity_type,entity_id,project_id,event_type,actor_id,"
            "payload_json,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                project_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str, dept: str | None = None) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO gate_users(user_id,display_name,role,dept,created_at) VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, dept, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role, "dept": dept}

    # ------------------------------------------------------------------ 项目

    def create_project(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        project = validate_project(raw)
        self._user(project["director_id"])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO projects(project_id,name,owner_org,director_id,tags_json,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        project["project_id"],
                        project["name"],
                        project["owner_org"],
                        project["director_id"],
                        canonical_json(project["tags"]),
                        self._now(),
                    ),
                )
                self._audit(
                    "project", project["project_id"], "project.created", actor_id, project,
                    project_id=project["project_id"],
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("项目编号已经存在或负责人不存在") from exc
        return {"project_id": project["project_id"], "state": "created"}

    def _project_row(self, project_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise NotFound("项目不存在")
        return row

    def project(self, project_id: str) -> dict[str, Any]:
        row = self._project_row(project_id)
        return {
            "project_id": row["project_id"],
            "name": row["name"],
            "owner_org": row["owner_org"],
            "director_id": row["director_id"],
            "tags": json.loads(row["tags_json"]),
            "revision": row["revision"],
        }

    # ------------------------------------------------------------------ 阶段门模板

    def publish_gate(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """发布阶段门模板（门槛库）。门槛编号是跨版本稳定的逻辑编号。"""
        self._require(actor_id, "gate.write")
        gate = validate_gate(raw)
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO policy_gates(gate_id,gate_order,name,decision_role,stage_budget_cny,"
                    "version,state,created_by,created_at,published_at) VALUES(?,?,?,?,?,1,'published',?,?,?)",
                    (
                        gate["gate_id"],
                        gate["gate_order"],
                        gate["name"],
                        gate["decision_role"],
                        gate["stage_budget_cny"],
                        actor_id,
                        now,
                        now,
                    ),
                )
                gate_id = gate["gate_id"]
                for condition in gate["conditions"]:
                    self._insert_condition(gate_id, condition, now)
                self._audit("gate", gate_id, "gate.published", actor_id, {
                    "gate_id": gate_id, "gate_order": gate["gate_order"],
                    "conditions": [c["condition_id"] for c in gate["conditions"]],
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("阶段门编号或阶段序号冲突（同序号只能有一个发布版本）") from exc
        return {
            "gate_id": gate_id,
            "gate_order": gate["gate_order"],
            "version": 1,
            "state": "published",
            "conditions": [c["condition_id"] for c in gate["conditions"]],
        }

    def _insert_condition(self, gate_id: str, condition: Mapping[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO gate_conditions(condition_key,gate_id,version,condition_type,title,"
            "responsible_dept,authority,rule_version,threshold_cny,evidence_spec_json,applicability_json,"
            "active,created_at) VALUES(?,?,1,?,?,?,?,?,?,?,?,1,?)",
            (
                condition["condition_id"],
                gate_id,
                condition["condition_type"],
                condition["title"],
                condition["responsible_dept"],
                condition["authority"],
                condition["rule_version"],
                condition["threshold_cny"],
                None if condition["evidence_spec_json"] is None else canonical_json(condition["evidence_spec_json"]),
                canonical_json(condition["applicability_json"] or {}),
                now,
            ),
        )

    def revise_gate(self, actor_id: str, gate_order: int, raw: Mapping[str, Any]) -> dict[str, Any]:
        """政策规则换版：旧版本整版停用、保留历史；新版本以新 gate_id 发布。

        已经签发的阶段令记录了当时的 gate_id 与版本，不被换版抹去。
        """
        self._require(actor_id, "gate.revise")
        gate = validate_gate(raw)
        if gate["gate_order"] != gate_order:
            raise ValidationFailed("换版必须保持 gate_order 不变")
        old = self.connection.execute(
            "SELECT * FROM policy_gates WHERE gate_order=? AND state='published'", (gate_order,)
        ).fetchone()
        if old is None:
            raise NotFound("该阶段序号没有已发布版本")
        if gate["gate_id"] == old["gate_id"]:
            raise ValidationFailed("新版本必须使用新的 gate_id")
        now = self._now()
        version = int(old["version"]) + 1
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE policy_gates SET state='retired' WHERE gate_id=?", (old["gate_id"],)
            )
            self.connection.execute(
                "INSERT INTO policy_gates(gate_id,gate_order,name,decision_role,stage_budget_cny,"
                "version,state,supersedes_gate_id,created_by,created_at,published_at) "
                "VALUES(?,?,?,?,?,?, 'published',?,?,?,?)",
                (
                    gate["gate_id"], gate_order, gate["name"], gate["decision_role"],
                    gate["stage_budget_cny"], version, old["gate_id"], actor_id, now, now,
                ),
            )
            for condition in gate["conditions"]:
                self._insert_condition(gate["gate_id"], condition, now)
            old_keys = {
                row["condition_key"]
                for row in self.connection.execute(
                    "SELECT condition_key FROM gate_conditions WHERE gate_id=?", (old["gate_id"],)
                ).fetchall()
            }
            new_keys = {c["condition_id"] for c in gate["conditions"]}
            removed = sorted(old_keys - new_keys)
            self._audit("gate", gate["gate_id"], "gate.revised", actor_id, {
                "gate_order": gate_order,
                "old_gate_id": old["gate_id"],
                "new_gate_id": gate["gate_id"],
                "version": version,
                "removed_conditions": removed,
            })
        return {
            "gate_id": gate["gate_id"],
            "gate_order": gate_order,
            "version": version,
            "state": "published",
            "supersedes": old["gate_id"],
            "conditions": [c["condition_id"] for c in gate["conditions"]],
        }

    def gate_definition(self, gate_order: int) -> dict[str, Any]:
        gate = self.connection.execute(
            "SELECT * FROM policy_gates WHERE gate_order=? AND state='published'", (gate_order,)
        ).fetchone()
        if gate is None:
            raise NotFound("该阶段没有已发布阶段门")
        return self._gate_payload(gate)

    def _gate_payload(self, gate: sqlite3.Row) -> dict[str, Any]:
        conditions = []
        for row in self.connection.execute(
            "SELECT * FROM gate_conditions WHERE gate_id=? AND active=1 ORDER BY condition_uid",
            (gate["gate_id"],),
        ).fetchall():
            conditions.append(
                {
                    "condition_id": row["condition_key"],
                    "condition_type": row["condition_type"],
                    "title": row["title"],
                    "responsible_dept": row["responsible_dept"],
                    "authority": row["authority"],
                    "rule_version": row["rule_version"],
                    "threshold_cny": row["threshold_cny"],
                    "evidence_spec": json.loads(row["evidence_spec_json"]) if row["evidence_spec_json"] else None,
                    "applicability": json.loads(row["applicability_json"]),
                }
            )
        return {
            "gate_id": gate["gate_id"],
            "gate_order": gate["gate_order"],
            "name": gate["name"],
            "decision_role": gate["decision_role"],
            "stage_budget_cny": gate["stage_budget_cny"],
            "version": gate["version"],
            "conditions": conditions,
        }

    # ------------------------------------------------------------------ 工作包

    def add_package(self, actor_id: str, project_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "package.write")
        self._project_row(project_id)
        package = validate_package(raw)
        self._assert_gate_exists(package["gate_order"])
        missing = self._unknown_condition_keys(package["gate_order"], package["requires_conditions"])
        if missing:
            raise ValidationFailed(f"门槛编号在当前阶段门中不存在: {', '.join(missing)}")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO work_packages(package_id,project_id,name,gate_order,requires_json,"
                    "responsible_dept,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        package["package_id"], project_id, package["name"], package["gate_order"],
                        canonical_json(package["requires_conditions"]), package["responsible_dept"], self._now(),
                    ),
                )
                self._audit("work_package", package["package_id"], "package.added", actor_id, package,
                            project_id=project_id)
        except sqlite3.IntegrityError as exc:
            raise Conflict("工作包编号冲突") from exc
        return {"package_id": package["package_id"], "project_id": project_id, "state": "planned"}

    def _assert_gate_exists(self, gate_order: int) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM policy_gates WHERE gate_order=? AND state='published'", (gate_order,)
        ).fetchone()
        if row is None:
            raise ValidationFailed(f"阶段 {gate_order} 尚未发布阶段门")

    def _unknown_condition_keys(self, gate_order: int, keys: Sequence[str]) -> list[str]:
        known = {
            row["condition_key"]
            for row in self.connection.execute(
                "SELECT c.condition_key FROM gate_conditions c JOIN policy_gates g ON g.gate_id=c.gate_id "
                "WHERE g.gate_order=? AND g.state='published' AND c.active=1",
                (gate_order,),
            ).fetchall()
        }
        return sorted(set(keys) - known)

    def _package_row(self, project_id: str, package_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM work_packages WHERE project_id=? AND package_id=?", (project_id, package_id)
        ).fetchone()
        if row is None:
            raise NotFound("工作包不存在")
        return row

    def advance_package(
        self, actor_id: str, project_id: str, package_id: str, to_state: str, note: str = ""
    ) -> dict[str, Any]:
        """推进工作包状态；进入 ready/in_progress 前由阶段令把关（见 issue_order）。"""
        self._require(actor_id, "package.write")
        package = self._package_row(project_id, package_id)
        current = package["state"]
        if to_state not in PACKAGE_ADVANCE.get(current, set()):
            raise InvalidState(f"工作包不能从 {current} 流转到 {to_state}")
        if to_state in {"ready", "in_progress"}:
            order = self.connection.execute(
                "SELECT order_id FROM stage_orders WHERE project_id=? AND gate_order=? AND decision='issued'",
                (project_id, package["gate_order"]),
            ).fetchone()
            override_row = None if order is not None else self.connection.execute(
                "SELECT * FROM emergency_overrides WHERE project_id=? AND scope='package' AND target_id=? "
                "AND state='active' AND expires_at>?",
                (project_id, package_id, self._now()),
            ).fetchone()
            if order is None and override_row is None:
                raise GatingBlocked("阶段令未签发，工作包不得越过阶段门")
            order_id = None if order is None else order["order_id"]
            if override_row is not None:
                boundary = json.loads(override_row["boundary_json"])
                note = (note + " | " if note else "") + (
                    f"依紧急例外 {override_row['override_id']} 先行（范围：{boundary['work_allowance']}，"
                    f"到期 {override_row['expires_at']}）"
                )
        else:
            order_id = None
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE work_packages SET state=?,revision=revision+1 WHERE package_id=? AND project_id=?",
                (to_state, package_id, project_id),
            )
            self.connection.execute(
                "INSERT INTO package_progress(project_id,package_id,from_state,to_state,order_id,note,actor_id,"
                "created_at) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, package_id, current, to_state, order_id, note, actor_id, self._now()),
            )
            self._audit("work_package", package_id, f"package.{to_state}", actor_id,
                        {"from": current, "order_id": order_id, "note": note}, project_id=project_id)
        return {"package_id": package_id, "state": to_state, "order_id": order_id}

    # ------------------------------------------------------------------ 证据

    def submit_evidence(self, actor_id: str, project_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "evidence.submit")
        self._project_row(project_id)
        evidence = validate_evidence(raw)
        self._assert_condition_known(evidence["condition_id"])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO evidence_documents(evidence_id,project_id,condition_id,doc_number,doc_kind,"
                    "source,content_sha256,issued_at,submitted_by_dept,state,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?, 'submitted',?)",
                    (
                        evidence["evidence_id"], project_id, evidence["condition_id"], evidence["doc_number"],
                        evidence["doc_kind"], evidence["source"], evidence["content_sha256"],
                        evidence["issued_at"], evidence["submitted_by_dept"], self._now(),
                    ),
                )
                self._audit("evidence", evidence["evidence_id"], "evidence.submitted", actor_id, {
                    "condition_id": evidence["condition_id"], "doc_number": evidence["doc_number"],
                    "content_sha256": evidence["content_sha256"],
                }, project_id=project_id)
        except sqlite3.IntegrityError as exc:
            raise Conflict("证据编号冲突") from exc
        return {"evidence_id": evidence["evidence_id"], "state": "submitted",
                "condition_id": evidence["condition_id"]}

    def _assert_condition_known(self, condition_key: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM gate_conditions WHERE condition_key=? AND active=1", (condition_key,)
        ).fetchone()
        if row is None:
            raise ValidationFailed(f"门槛 {condition_key} 不存在或已失效")

    def revoke_evidence(self, actor_id: str, project_id: str, evidence_id: str, reason: str) -> dict[str, Any]:
        """证据失效：登记失效，不删除记录；同事务落定对未执行工作包的影响判断。"""
        self._require(actor_id, "evidence.submit")
        row = self.connection.execute(
            "SELECT * FROM evidence_documents WHERE project_id=? AND evidence_id=?",
            (project_id, evidence_id),
        ).fetchone()
        if row is None:
            raise NotFound("证据不存在")
        if row["state"] in {"revoked", "rejected"}:
            raise InvalidState("证据已经失效")
        gate_order = self._gate_order_for_condition(row["condition_id"])
        impact_payload = self._compute_impact(
            project_id, "condition_expired", [row["condition_id"]], gate_order,
            note=f"证据 {evidence_id} 失效：{reason}",
        )
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE evidence_documents SET state='revoked' WHERE project_id=? AND evidence_id=?",
                (project_id, evidence_id),
            )
            impact_id = self._insert_impact(actor_id, impact_payload)
            self._audit("evidence", evidence_id, "evidence.revoked", actor_id,
                        {"condition_id": row["condition_id"], "reason": reason,
                         "impact_id": impact_id}, project_id=project_id)
        impact_payload["impact_id"] = impact_id
        return {"evidence_id": evidence_id, "state": "revoked", "impact": impact_payload}

    def _gate_order_for_condition(self, condition_key: str) -> int:
        row = self.connection.execute(
            "SELECT g.gate_order FROM gate_conditions c JOIN policy_gates g ON g.gate_id=c.gate_id "
            "WHERE c.condition_key=? AND g.state='published' ORDER BY g.version DESC LIMIT 1",
            (condition_key,),
        ).fetchone()
        if row is None:
            return -1
        return int(row["gate_order"])

    def _evidence_for_project(self, project_id: str) -> dict[str, list[dict[str, Any]]]:
        rows = self.connection.execute(
            "SELECT * FROM evidence_documents WHERE project_id=? AND state='accepted'",
            (project_id,),
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["condition_id"], []).append(dict(row))
        return grouped

    def review_evidence(self, actor_id: str, project_id: str, evidence_id: str, accepted: bool, note: str) -> dict[str, Any]:
        """部门提交的证据需经核验受理后才作为有效证据（防止会议纪要式口头开工）。"""
        self._require(actor_id, "evidence.review")
        row = self.connection.execute(
            "SELECT * FROM evidence_documents WHERE project_id=? AND evidence_id=?",
            (project_id, evidence_id),
        ).fetchone()
        if row is None:
            raise NotFound("证据不存在")
        new_state = "accepted" if accepted else "rejected"
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE evidence_documents SET state=? WHERE project_id=? AND evidence_id=?",
                (new_state, project_id, evidence_id),
            )
            self._audit("evidence", evidence_id, f"evidence.{new_state}", actor_id,
                        {"note": note}, project_id=project_id)
        return {"evidence_id": evidence_id, "state": new_state}

    # ------------------------------------------------------------------ 资金承诺

    def record_commitment(self, actor_id: str, project_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "commitment.write")
        self._project_row(project_id)
        commitment = validate_commitment(raw)
        self._assert_gate_exists(commitment["gate_order"])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO budget_commitments(commitment_id,project_id,gate_order,fund_source,"
                    "amount_cny,doc_number,state,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        commitment["commitment_id"], project_id, commitment["gate_order"],
                        commitment["fund_source"], commitment["amount_cny"], commitment["doc_number"],
                        commitment["state"], actor_id, self._now(),
                    ),
                )
                self._audit("commitment", commitment["commitment_id"], "commitment.recorded", actor_id, {
                    "gate_order": commitment["gate_order"], "amount_cny": commitment["amount_cny"],
                    "state": commitment["state"],
                }, project_id=project_id)
        except sqlite3.IntegrityError as exc:
            raise Conflict("资金承诺编号冲突") from exc
        return {"commitment_id": commitment["commitment_id"], "state": commitment["state"]}

    def update_commitment_state(
        self, actor_id: str, project_id: str, commitment_id: str, state: str
    ) -> dict[str, Any]:
        self._require(actor_id, "commitment.write")
        if state not in {"committed", "disbursed", "frozen", "withdrawn"}:
            raise ValidationFailed("资金承诺状态不合法")
        row = self.connection.execute(
            "SELECT * FROM budget_commitments WHERE project_id=? AND commitment_id=?",
            (project_id, commitment_id),
        ).fetchone()
        if row is None:
            raise NotFound("资金承诺不存在")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE budget_commitments SET state=?,revision=revision+1 WHERE project_id=? AND commitment_id=?",
                (state, project_id, commitment_id),
            )
            self._audit("commitment", commitment_id, f"commitment.{state}", actor_id,
                        {"previous": row["state"]}, project_id=project_id)
        return {"commitment_id": commitment_id, "state": state}

    def _budget_summary(self, project_id: str, gate_order: int) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT amount_cny,state FROM budget_commitments WHERE project_id=? AND gate_order=? AND state IN (?,?)",
            (project_id, gate_order, *FUNDS_ACTIVE_STATES),
        ).fetchall()
        ledger = budget_ledger([dict(r) for r in rows])
        return ledger

    # ------------------------------------------------------------------ 紧急例外

    def grant_override(self, actor_id: str, project_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "override.grant")
        self._project_row(project_id)
        override = validate_override(raw)
        expires_at = parse_utc(override["expires_at"], "expires_at")
        if expires_at <= self._now_dt():
            raise ValidationFailed("紧急例外到期时间必须晚于当前时间")
        if override["scope"] == "condition":
            self._assert_condition_known(override["target_id"])
        else:
            self._package_row(project_id, override["target_id"])
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO emergency_overrides(override_id,project_id,scope,target_id,reason,boundary_json,"
                "expires_at,state,granted_by,granted_at) VALUES(?,?,?,?,?,?,?, 'active',?,?)",
                (
                    override["override_id"], project_id, override["scope"], override["target_id"],
                    override["reason"], canonical_json(override["boundary"]), override["expires_at"],
                    actor_id, self._now(),
                ),
            )
            self._audit("override", override["override_id"], "override.granted", actor_id, {
                "scope": override["scope"], "target_id": override["target_id"],
                "reason": override["reason"], "expires_at": override["expires_at"],
                "boundary": override["boundary"],
            }, project_id=project_id)
        return {"override_id": override["override_id"], "state": "active",
                "expires_at": override["expires_at"]}

    def close_override(self, actor_id: str, project_id: str, override_id: str) -> dict[str, Any]:
        self._require(actor_id, "override.revoke")
        row = self.connection.execute(
            "SELECT * FROM emergency_overrides WHERE project_id=? AND override_id=?",
            (project_id, override_id),
        ).fetchone()
        if row is None:
            raise NotFound("紧急例外不存在")
        if row["state"] != "active":
            raise InvalidState("紧急例外已不在有效期")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE emergency_overrides SET state='closed',closed_at=? WHERE project_id=? AND override_id=?",
                (self._now(), project_id, override_id),
            )
            self._audit("override", override_id, "override.closed", actor_id, {}, project_id=project_id)
        return {"override_id": override_id, "state": "closed"}

    def _active_overrides(self, project_id: str) -> dict[str, dict[str, Any]]:
        """条件级例外映射（含已到期但未关闭的），是否在有效期由规则层判定。"""
        rows = self.connection.execute(
            "SELECT * FROM emergency_overrides WHERE project_id=? AND scope='condition' AND state='active'",
            (project_id,),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["scope"] == "condition":
                result[row["target_id"]] = {
                    "state": "active",
                    "reason": row["reason"],
                    "expires_at": row["expires_at"],
                    "boundary": json.loads(row["boundary_json"]),
                    "override_id": row["override_id"],
                }
        return result

    # ------------------------------------------------------------------ 门控评估与阶段令

    def gate_status(self, actor_id: str, project_id: str, gate_order: int) -> dict[str, Any]:
        self._require(actor_id, "dashboard.read")
        return self._gate_status(project_id, gate_order)

    def _gate_status(self, project_id: str, gate_order: int) -> dict[str, Any]:
        project = self._project_row(project_id)
        gate = self.connection.execute(
            "SELECT * FROM policy_gates WHERE gate_order=? AND state='published'", (gate_order,)
        ).fetchone()
        if gate is None:
            raise NotFound("该阶段没有已发布阶段门")
        definition = self._gate_payload(gate)
        evidence = self._evidence_for_project(project_id)
        ledger = self._budget_summary(project_id, gate_order)
        committed = Decimal(ledger["committed_cny"])
        overrides = self._active_overrides(project_id)
        results = evaluate_gate(
            definition["conditions"],
            json.loads(project["tags_json"]),
            evidence,
            budget_allocated=committed,
            now=self._now_dt(),
            overrides=overrides,
        )
        decision, blockers = gate_decision(results)
        previous_order = self.connection.execute(
            "SELECT order_id FROM stage_orders WHERE project_id=? AND gate_order=? AND decision='issued'",
            (project_id, gate_order),
        ).fetchone()
        return {
            "project_id": project_id,
            "gate": {
                "gate_id": definition["gate_id"],
                "gate_order": gate_order,
                "name": definition["name"],
                "version": definition["version"],
                "stage_budget_cny": definition["stage_budget_cny"],
            },
            "budget": {
                **ledger,
                "stage_budget_cny": definition["stage_budget_cny"],
                "sufficient": committed >= Decimal(definition["stage_budget_cny"]),
            },
            "conditions": results,
            "decision": decision,
            "blockers": blockers,
            "already_issued": previous_order is not None,
        }

    def issue_order(self, actor_id: str, project_id: str, gate_order: int, note: str = "") -> dict[str, Any]:
        """签发下一阶段令：只有所有适用门槛满足（或在有效例外覆盖下）才允许。"""
        user = self._require(actor_id, "order.issue")
        project = self._project_row(project_id)
        gate = self.connection.execute(
            "SELECT * FROM policy_gates WHERE gate_order=? AND state='published'", (gate_order,)
        ).fetchone()
        if gate is None:
            raise NotFound("该阶段没有已发布阶段门")
        if gate["decision_role"] != user["role"]:
            raise Forbidden(f"阶段令须由 {gate['decision_role']} 角色签发")
        prior = self.connection.execute(
            "SELECT order_id FROM stage_orders WHERE project_id=? AND gate_order=? AND decision='issued'",
            (project_id, gate_order),
        ).fetchone()
        if prior is not None:
            raise InvalidState("该阶段令已经签发，历史批准不可重复签发")
        if gate_order > 1:
            prev = self.connection.execute(
                "SELECT order_id FROM stage_orders WHERE project_id=? AND gate_order=? AND decision='issued'",
                (project_id, gate_order - 1),
            ).fetchone()
            if prev is None:
                raise InvalidState(f"必须先完成第 {gate_order - 1} 阶段门")

        # 按当前发布版本评估：历史阶段令不回滚，但任一适用门槛当前不满足时，
        # 新阶段令不得签发（恢复条件在看板中给出）
        status = self._gate_status(project_id, gate_order)
        if status["decision"] != "approved":
            raise GatingBlocked(
                "存在未满足的适用门槛，阶段令不得签发",
                details={"blockers": status["blockers"]},
            )
        if gate_order > 1:
            for earlier in range(1, gate_order):
                earlier_status = self._gate_status(project_id, earlier)
                if earlier_status["decision"] != "approved":
                    raise GatingBlocked(
                        f"第 {earlier} 阶段门在当前规则版本下存在未闭合卡点，不得签发下一阶段令",
                        details={"gate_order": earlier, "blockers": earlier_status["blockers"]},
                    )

        definition = self._gate_payload(gate)
        snapshot = {
            "gate": {k: v for k, v in definition.items()},
            "project_tags": json.loads(project["tags_json"]),
            "evaluated_at": self._now(),
            "conditions": status["conditions"],
            "budget": status["budget"],
        }
        basis = {
            "gate_id": gate["gate_id"],
            "gate_version": gate["version"],
            "conditions": status["conditions"],
            "budget": status["budget"],
            "note": note,
        }
        basis_sha256 = hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO stage_orders(project_id,gate_id,gate_version,gate_order,decision,"
                "snapshot_json,blocker_summary_json,basis_sha256,issued_by,issued_at) "
                "VALUES(?,?,?,?, 'issued',?,?,?,?,?)",
                (
                    project_id, gate["gate_id"], gate["version"], gate_order,
                    canonical_json(snapshot), canonical_json(status["blockers"]),
                    basis_sha256, actor_id, self._now(),
                ),
            )
            order_id = int(cursor.lastrowid)
            self._audit("stage_order", str(order_id), "order.issued", actor_id, {
                "project_id": project_id, "gate_order": gate_order,
                "gate_id": gate["gate_id"], "gate_version": gate["version"],
                "basis_sha256": basis_sha256, "note": note,
            }, project_id=project_id)
        return {
            "order_id": order_id,
            "project_id": project_id,
            "gate_order": gate_order,
            "decision": "issued",
            "gate_id": gate["gate_id"],
            "gate_version": gate["version"],
            "basis_sha256": basis_sha256,
            "issued_at": self._now(),
        }

    # ------------------------------------------------------------------ 规则换版/条件失效影响

    def assess_rule_change(
        self,
        actor_id: str,
        project_id: str,
        change_kind: str,
        condition_ids: Sequence[str],
        gate_order: int,
        note: str = "",
    ) -> dict[str, Any]:
        """登记条件失效或规则换版的影响：判断哪些尚未执行的工作包受影响。

        历史阶段批准保留不动；已签发阶段令覆盖的工作包标注 protected_by_order，
        只提示复工后复核，不回滚。
        """
        self._require(actor_id, "impact.recognize")
        impact_payload = self._compute_impact(project_id, change_kind, condition_ids, gate_order, note)
        with transaction(self.connection, immediate=True):
            impact_id = self._insert_impact(actor_id, impact_payload)
            self._audit("rule_change", str(impact_id), "rule_change.recognized", actor_id, impact_payload,
                        project_id=project_id)
        impact_payload["impact_id"] = impact_id
        return impact_payload

    def _compute_impact(
        self,
        project_id: str,
        change_kind: str,
        condition_ids: Sequence[str],
        gate_order: int,
        note: str,
    ) -> dict[str, Any]:
        if change_kind not in {"condition_expired", "rule_revised", "condition_deactivated"}:
            raise ValidationFailed("change_kind 不合法")
        if not condition_ids:
            raise ValidationFailed("至少给出一个受影响门槛")
        self._project_row(project_id)
        package_rows = self.connection.execute(
            "SELECT package_id,state,requires_json,gate_order FROM work_packages WHERE project_id=?",
            (project_id,),
        ).fetchall()
        packages = [
            {
                "package_id": r["package_id"],
                "state": r["state"],
                "requires_conditions": json.loads(r["requires_json"]),
                "gate_order": r["gate_order"],
            }
            for r in package_rows
        ]
        impacted = impacted_packages(
            packages, changed_condition_ids=condition_ids, gate_order=gate_order
        )
        issued_gates = {
            r["gate_order"]
            for r in self.connection.execute(
                "SELECT DISTINCT gate_order FROM stage_orders WHERE project_id=? AND decision='issued'",
                (project_id,),
            ).fetchall()
        }
        for item in impacted:
            protected = item["gate_order"] in issued_gates
            item["protected_by_order"] = protected
            if protected:
                item["protection_note"] = "阶段令已签发，历史批准不回滚；复工或进入下一阶段前需重新核验"
        return {
            "project_id": project_id,
            "change_kind": change_kind,
            "condition_ids": list(condition_ids),
            "gate_order": gate_order,
            "impacted_packages": impacted,
            "note": note,
        }

    def _insert_impact(self, actor_id: str, payload: Mapping[str, Any]) -> int:
        cursor = self.connection.execute(
            "INSERT INTO rule_change_impacts(project_id,change_kind,condition_ids_json,from_gate_order,"
            "impacted_packages_json,note,recognized_at,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                payload["project_id"], payload["change_kind"], canonical_json(payload["condition_ids"]),
                payload["gate_order"], canonical_json(payload["impacted_packages"]), payload["note"],
                self._now(), actor_id, self._now(),
            ),
        )
        return int(cursor.lastrowid)

    # ------------------------------------------------------------------ 项目负责人看板

    def dashboard(self, actor_id: str, project_id: str) -> dict[str, Any]:
        """项目负责人视角：卡点、授权依据、资金承诺、后续恢复条件。"""
        self._require(actor_id, "dashboard.read")
        project = self._project_row(project_id)
        gate_orders = [
            r["gate_order"]
            for r in self.connection.execute(
                "SELECT DISTINCT gate_order FROM policy_gates WHERE state='published' ORDER BY gate_order"
            ).fetchall()
        ]
        gates = []
        current_blockers: list[dict[str, Any]] = []
        reopened: list[dict[str, Any]] = []
        for order in gate_orders:
            status = self._gate_status(project_id, order)
            order_row = self.connection.execute(
                "SELECT order_id,gate_id,gate_version,basis_sha256,issued_by,issued_at FROM stage_orders "
                "WHERE project_id=? AND gate_order=? AND decision='issued'",
                (project_id, order),
            ).fetchone()
            gate_view = {
                "gate_order": order,
                "gate_id": status["gate"]["gate_id"],
                "name": status["gate"]["name"],
                "version": status["gate"]["version"],
                "decision": status["decision"],
                "issued": None if order_row is None else dict(order_row),
                "conditions": status["conditions"],
                "budget": status["budget"],
            }
            gates.append(gate_view)
            bucket = reopened if order_row is not None else current_blockers
            for blocker in status["blockers"]:
                bucket.append({"gate_order": order, **blocker})

        packages = [
            dict(r)
            for r in self.connection.execute(
                "SELECT package_id,name,gate_order,state,responsible_dept,requires_json FROM work_packages "
                "WHERE project_id=? ORDER BY gate_order,package_id",
                (project_id,),
            ).fetchall()
        ]
        for package in packages:
            package["requires_conditions"] = json.loads(package.pop("requires_json"))

        overrides = [
            {
                "override_id": r["override_id"],
                "scope": r["scope"],
                "target_id": r["target_id"],
                "reason": r["reason"],
                "boundary": json.loads(r["boundary_json"]),
                "expires_at": r["expires_at"],
                "state": self._override_liveness(r),
                "granted_by": r["granted_by"],
                "granted_at": r["granted_at"],
            }
            for r in self.connection.execute(
                "SELECT * FROM emergency_overrides WHERE project_id=? ORDER BY granted_at", (project_id,)
            ).fetchall()
        ]
        commitments = [
            dict(r)
            for r in self.connection.execute(
                "SELECT commitment_id,gate_order,fund_source,amount_cny,doc_number,state FROM "
                "budget_commitments WHERE project_id=? ORDER BY gate_order,commitment_id",
                (project_id,),
            ).fetchall()
        ]
        recovery = self._recovery_conditions(project_id, current_blockers + reopened, overrides)
        impacts = [
            {
                "impact_id": r["impact_id"],
                "change_kind": r["change_kind"],
                "condition_ids": json.loads(r["condition_ids_json"]),
                "from_gate_order": r["from_gate_order"],
                "impacted_packages": json.loads(r["impacted_packages_json"]),
                "note": r["note"],
                "recognized_at": r["recognized_at"],
            }
            for r in self.connection.execute(
                "SELECT * FROM rule_change_impacts WHERE project_id=? ORDER BY impact_id DESC", (project_id,)
            ).fetchall()
        ]
        return {
            "project": {
                "project_id": project["project_id"],
                "name": project["name"],
                "director_id": project["director_id"],
                "tags": json.loads(project["tags_json"]),
            },
            "generated_at": self._now(),
            "current_blockers": current_blockers,
            "reopened_alerts": reopened,
            "gates": gates,
            "work_packages": packages,
            "overrides": overrides,
            "commitments": commitments,
            "recovery_conditions": recovery,
            "rule_change_impacts": impacts,
        }

    def _override_liveness(self, row: sqlite3.Row) -> str:
        if row["state"] != "active":
            return row["state"]
        try:
            expired = parse_utc(row["expires_at"]) <= self._now_dt()
        except ValueError:
            expired = True
        return "expired" if expired else "active"

    def _recovery_conditions(
        self,
        project_id: str,
        blockers: Sequence[Mapping[str, Any]],
        overrides: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """每个卡点给出恢复条件：补什么证据/资金、是否有可援引例外、到期后如何恢复。"""
        active_by_target = {o["target_id"]: o for o in overrides if o["scope"] == "condition" and o["state"] == "active"}
        recovery: list[dict[str, Any]] = []
        for blocker in blockers:
            condition_id = blocker["condition_id"]
            condition = self.connection.execute(
                "SELECT c.*,g.gate_order FROM gate_conditions c JOIN policy_gates g ON g.gate_id=c.gate_id "
                "WHERE c.condition_key=? AND c.active=1 AND g.state='published'",
                (condition_id,),
            ).fetchone()
            if condition is None:
                continue
            actions: list[str] = []
            if condition["condition_type"] == "evidence":
                spec = json.loads(condition["evidence_spec_json"])
                actions.append(
                    f"由 {condition['responsible_dept']} 补交符合要求的《{spec['doc_kind']}》"
                    f"（认可来源：{('、'.join(spec['accepted_sources']) or '不限')}"
                    + (f"，签发 {spec['max_age_days']} 天内" if spec["max_age_days"] else "")
                    + "），经核验受理后重新评估"
                )
            elif condition["condition_type"] == "budget":
                actions.append(
                    f"由资金部门补足承诺至 {condition['threshold_cny']} 元（依据 {condition['authority']}）"
                )
            override = active_by_target.get(condition_id)
            recovery.append(
                {
                    "gate_order": blocker["gate_order"],
                    "condition_id": condition_id,
                    "title": condition["title"],
                    "responsible_dept": condition["responsible_dept"],
                    "authority": condition["authority"],
                    "rule_version": condition["rule_version"],
                    "blocker_reason": blocker["reason"],
                    "actions": actions,
                    "active_override": None
                    if override is None
                    else {
                        "override_id": override["override_id"],
                        "reason": override["reason"],
                        "expires_at": override["expires_at"],
                        "boundary": override["boundary"],
                        "after_expiry": "例外到期后必须完成上述恢复动作并重新评估，未完成不得继续越过阶段门",
                    },
                }
            )
        return recovery

    # ------------------------------------------------------------------ 审计

    def audit_chain(self, actor_id: str, project_id: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        if project_id is not None:
            self._project_row(project_id)
        # 哈希链是全局链，项目过滤只影响计数，不影响逐环校验
        rows = self.connection.execute("SELECT * FROM gate_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        matched = 0
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "project_id": row["project_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            if project_id is None or row["project_id"] == project_id:
                matched += 1
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": matched, "head_hash": previous_hash,
                "project_id": project_id}
