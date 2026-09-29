"""重大项目阶段门控的事务用例。

门控规则：

* 政策规则（策略）按版本留存，阶段令永久快照签发时的规则、证据、资金与例外；
* 只有下一阶段所有适用门槛满足（或被有效紧急例外覆盖）、阶段预算已核定且资金
  承诺足额时，才允许签发下一阶段令；
* 证据失效、政策换版、例外到期只作用于尚未执行的工作包，历史阶段令不被修改；
* 紧急例外必须限定门槛/工作包范围、写明理由并设置到期时间。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import Evidence, FundCommitment, Policy, Project, StageBudget, WorkPackage
from .storage import initialize, transaction


# coordinator：项目办协调人；department：责任部门经办人；approver：决策批准人；auditor：审计。
ROLE_PERMISSIONS = {
    "coordinator": {
        "project.write", "budget.write", "package.write", "policy.apply",
        "status.read", "exception.read",
    },
    "department": {
        "evidence.write", "commitment.write", "package.execute", "status.read",
    },
    "approver": {
        "policy.write", "gate.waive", "evidence.invalidate", "exception.grant",
        "exception.revoke", "exception.sweep", "order.issue", "impact.resolve",
        "status.read", "audit.read",
    },
    "auditor": {"audit.read", "status.read"},
}

def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def money(value: Decimal) -> str:
    return format(value, "f")


class GateService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> datetime:
        return self.clock.now()

    def _now_text(self) -> str:
        return utc_text(self._now())

    def _today(self) -> date:
        return self._now().date()

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
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM gate_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now_text(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO gate_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(
        self, user_id: str, display_name: str, role: str, dept: str | None = None
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        if role == "department" and not dept:
            raise ValidationFailed("责任部门经办人必须指定 dept")
        if role == "approver" and not dept:
            raise ValidationFailed("批准人必须指定决策权限层级 dept")
        dept = dept.strip().upper() if dept else None
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO gate_users(user_id,display_name,role,dept,created_at) VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, dept, self._now_text()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role, "dept": dept}

    # ------------------------------------------------------------------ 策略

    def publish_policy(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "policy.write")
        policy = Policy.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        with transaction(self.connection, immediate=True):
            previous = self.connection.execute(
                "SELECT version FROM policies WHERE policy_id=? ORDER BY version DESC LIMIT 1",
                (policy.policy_id,),
            ).fetchone()
            if previous is not None and policy.version != previous["version"] + 1:
                raise Conflict(f"新版本号必须是 {previous['version'] + 1}")
            if previous is None and policy.version != 1:
                raise ValidationFailed("首个策略版本必须是 1")
            try:
                self.connection.execute(
                    "INSERT INTO policies(policy_id,version,name,effective_date,definition_json,"
                    "content_sha256,state,created_by,created_at) VALUES(?,?,?,?,?,?, 'active',?,?)",
                    (
                        policy.policy_id,
                        policy.version,
                        policy.name,
                        policy.effective_date,
                        definition,
                        content_sha256,
                        actor_id,
                        self._now_text(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("策略版本或内容已经存在") from exc
            if previous is not None:
                self.connection.execute(
                    "UPDATE policies SET state='superseded' WHERE policy_id=? AND version=?",
                    (policy.policy_id, previous["version"]),
                )
            self._audit(
                "policy",
                f"{policy.policy_id}:v{policy.version}",
                "policy.published",
                actor_id,
                {"policy_id": policy.policy_id, "version": policy.version, "sha256": content_sha256},
            )
        return {
            "policy_id": policy.policy_id,
            "version": policy.version,
            "state": "active",
            "sha256": content_sha256,
        }

    def _load_policy(self, policy_id: str, version: int) -> tuple[sqlite3.Row, Policy]:
        row = self.connection.execute(
            "SELECT * FROM policies WHERE policy_id=? AND version=?", (policy_id, version)
        ).fetchone()
        if row is None:
            raise NotFound("策略版本不存在")
        return row, Policy.from_dict(json.loads(row["definition_json"]))

    def apply_policy_version(
        self, actor_id: str, project_id: str, new_version: int
    ) -> dict[str, Any]:
        """把项目迁移到新政策版本；只影响尚未进入的阶段和尚未执行的工作包。"""
        self._require(actor_id, "policy.apply")
        with transaction(self.connection, immediate=True):
            project = self._project_row(project_id)
            old_version = int(project["policy_version"])
            if new_version <= old_version:
                raise InvalidState("只能迁移到更新的政策版本")
            new_row, new_policy = self._load_policy(project["policy_id"], new_version)
            if new_row["state"] != "active":
                raise InvalidState("目标政策版本不是现行版本")
            if project["current_stage"] not in new_policy.stages:
                raise ValidationFailed("现行阶段在新政策中不存在，无法迁移")
            _, old_policy = self._load_policy(project["policy_id"], old_version)
            # 立项阶段虽无阶段令，但同样属于已经进入、不追溯调整的阶段。
            entered_stages = self._entered_stages(project_id) | {old_policy.stages[0]}
            self.connection.execute(
                "UPDATE projects SET policy_version=? WHERE project_id=?",
                (new_version, project_id),
            )
            changes = self._instantiate_gates(
                project, new_policy, new_version, entered_stages, old_policy
            )
            for change in changes:
                affected = self._unfinished_packages(project_id, change["stage"])
                # 政策换版只拦截尚未执行（pending）的工作包；已在执行的工作包按旧授权
                # 继续，但其未开工的后续工作包必须满足新版门槛。证据/例外失效则不同，
                # 会立即暂停在执行工作包（见 invalidate_evidence 与例外到期处理）。
                blocked_pending = [p["package_id"] for p in affected if p["state"] == "pending"]
                if not affected and change["stage"] in entered_stages:
                    continue
                self._record_impact(
                    impact_type="POLICY_VERSION",
                    project_id=project_id,
                    policy_version=new_version,
                    stage=change["stage"],
                    gate_code=None,
                    package_id=None,
                    detail={
                        "stage": change["stage"],
                        "old_version": old_version,
                        "new_version": new_version,
                        "gates": change["gates"],
                        "affected_packages": [p["package_id"] for p in affected],
                        "blocked_pending_packages": blocked_pending,
                    },
                    recovery={
                        "instruction": "按新版政策补齐门槛证据或由批准人认定不适用",
                        "required_gates": [item["gate_code"] for item in change["gates"]],
                        "decision_role": new_policy.decision_authority.get(change["stage"]),
                    },
                )
            self._reconcile_impacts(project_id)
            self._audit(
                "project",
                project_id,
                "policy.version_applied",
                actor_id,
                {"old_version": old_version, "new_version": new_version, "changes": changes},
            )
        return {"project_id": project_id, "policy_version": new_version}

    # ------------------------------------------------------------------ 项目

    def create_project(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "project.write")
        data = Project.from_dict(raw)
        self._user(data.manager_id)
        with transaction(self.connection, immediate=True):
            policy_row = self.connection.execute(
                "SELECT * FROM policies WHERE policy_id=? ORDER BY version DESC LIMIT 1",
                (data.policy_id,),
            ).fetchone()
            if policy_row is None:
                raise NotFound("政策不存在")
            if policy_row["state"] != "active":
                raise InvalidState("政策没有现行版本")
            _, policy = self._load_policy(data.policy_id, policy_row["version"])
            if data.initial_stage != policy.stages[0]:
                raise ValidationFailed(f"initial_stage 必须是政策首个阶段 {policy.stages[0]}")
            try:
                self.connection.execute(
                    "INSERT INTO projects(project_id,name,owner_org,manager_id,policy_id,policy_version,"
                    "current_stage,budget_cny,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        data.project_id,
                        data.name,
                        data.owner_org,
                        data.manager_id,
                        data.policy_id,
                        policy_row["version"],
                        data.initial_stage,
                        money(data.budget_cny),
                        self._now_text(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("项目编号已经存在") from exc
            self._instantiate_gates(
                self._project_row(data.project_id), policy, policy_row["version"], set(), None
            )
            self._audit("project", data.project_id, "project.created", actor_id, dict(raw))
        return {
            "project_id": data.project_id,
            "current_stage": data.initial_stage,
            "policy_id": data.policy_id,
            "policy_version": policy_row["version"],
        }

    def _project_row(self, project_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFound("项目不存在")
        return row

    def _entered_stages(self, project_id: str) -> set[str]:
        """已经通过阶段令进入过的阶段（含当前阶段）。"""
        rows = self.connection.execute(
            "SELECT to_stage FROM stage_orders WHERE project_id=?", (project_id,)
        ).fetchall()
        return {row["to_stage"] for row in rows}

    def _instantiate_gates(
        self,
        project: sqlite3.Row,
        policy: Policy,
        version: int,
        entered_stages: set[str],
        previous_policy: Policy | None,
    ) -> list[dict[str, Any]]:
        """按政策版本实例化门槛；返回新增、要求变化或证据已失效的门槛变化。

        规格（类别、责任部门、适用方式、证据类型、证据要求、有效期）等价的门槛
        直接沿用旧版本状态，不产生影响；历史阶段令的快照不受影响。
        """
        changes: list[dict[str, Any]] = []
        today = self._today()
        for stage in policy.stages:
            stage_changes: list[dict[str, Any]] = []
            for gate_code in policy.stage_gates[stage]:
                spec = next(gate for gate in policy.gates if gate.gate_code == gate_code)
                prior = None
                prior_spec = None
                if previous_policy is not None:
                    prior = self.connection.execute(
                        "SELECT * FROM project_gates WHERE project_id=? AND stage=? AND gate_code=? "
                        "ORDER BY policy_version DESC LIMIT 1",
                        (project["project_id"], stage, gate_code),
                    ).fetchone()
                    prior_spec = next(
                        (g for g in previous_policy.gates if g.gate_code == gate_code), None
                    )
                equivalent = prior is not None and prior_spec is not None and (
                    prior_spec.kind == spec.kind
                    and prior_spec.owner_dept == spec.owner_dept
                    and prior_spec.applies == spec.applies
                    and prior_spec.evidence_kind == spec.evidence_kind
                    and prior_spec.evidence_requirement == spec.evidence_requirement
                    and prior_spec.valid_days == spec.valid_days
                )
                if equivalent and self._carry_gate_row(
                    project["project_id"], prior, version, spec, today
                ):
                    continue
                # 新增门槛、要求变化，或旧证据已过有效期：按待满足重新实例化。
                self.connection.execute(
                    "INSERT INTO project_gates(project_id,policy_id,policy_version,stage,gate_code,"
                    "name,kind,owner_dept,applies,evidence_kind,evidence_requirement,valid_days) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        project["project_id"],
                        policy.policy_id,
                        version,
                        stage,
                        spec.gate_code,
                        spec.name,
                        spec.kind,
                        spec.owner_dept,
                        spec.applies,
                        spec.evidence_kind,
                        spec.evidence_requirement,
                        spec.valid_days,
                    ),
                )
                if prior is None:
                    reason = "新版政策新增门槛"
                elif prior_spec is None:
                    reason = "新版政策新增门槛"
                elif not equivalent:
                    reason = "新版政策门槛要求发生变化"
                else:
                    reason = "旧证据在新版政策下已过有效期"
                stage_changes.append(
                    {"gate_code": spec.gate_code, "name": spec.name, "reason": reason}
                )
            if stage_changes:
                changes.append({"stage": stage, "gates": stage_changes})
        return changes

    def _carry_gate_row(
        self,
        project_id: str,
        prior: sqlite3.Row,
        version: int,
        spec: Any,
        today: date,
    ) -> bool:
        """把旧版本门槛行原样沿用到新版本。证据已过期则不沿用（返回 False）。"""
        if prior["state"] == "satisfied" and prior["expires_at"] is not None:
            if date.fromisoformat(prior["expires_at"]) < today:
                return False
        self.connection.execute(
            "INSERT INTO project_gates(project_id,policy_id,policy_version,stage,gate_code,name,kind,"
            "owner_dept,applies,evidence_kind,evidence_requirement,valid_days,state,evidence_id,"
            "satisfied_at,expires_at,waive_note,satisfied_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                project_id,
                spec.policy_version.split(":")[0],
                version,
                prior["stage"],
                prior["gate_code"],
                spec.name,
                spec.kind,
                spec.owner_dept,
                spec.applies,
                spec.evidence_kind,
                spec.evidence_requirement,
                spec.valid_days,
                prior["state"],
                prior["evidence_id"],
                prior["satisfied_at"],
                prior["expires_at"],
                prior["waive_note"],
                prior["satisfied_by"],
            ),
        )
        return True

    # -------------------------------------------------------------- 预算/资金

    def set_stage_budget(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "budget.write")
        budget = StageBudget.from_dict(raw)
        project = self._project_row(budget.project_id)
        _, policy = self._load_policy(project["policy_id"], project["policy_version"])
        if budget.stage not in policy.stages:
            raise ValidationFailed("阶段不在项目政策中")
        with transaction(self.connection, immediate=True):
            total = self.connection.execute(
                "SELECT COALESCE(sum(CAST(allocated_cny AS REAL)),0) total FROM stage_budgets WHERE project_id=?",
                (budget.project_id,),
            ).fetchone()["total"]
            remaining = Decimal(project["budget_cny"]) - Decimal(str(total))
            existing = self.connection.execute(
                "SELECT allocated_cny FROM stage_budgets WHERE project_id=? AND stage=?",
                (budget.project_id, budget.stage),
            ).fetchone()
            if existing is not None:
                remaining += Decimal(existing["allocated_cny"])
            if budget.allocated_cny > remaining:
                raise InvalidState(
                    f"阶段预算超出项目总预算：可核定额度 {money(remaining)}"
                )
            self.connection.execute(
                "INSERT INTO stage_budgets(project_id,stage,allocated_cny,updated_by,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(project_id,stage) DO UPDATE SET "
                "allocated_cny=excluded.allocated_cny,revision=stage_budgets.revision+1,"
                "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (budget.project_id, budget.stage, money(budget.allocated_cny), actor_id, self._now_text()),
            )
            self._audit(
                "stage_budget",
                f"{budget.project_id}:{budget.stage}",
                "budget.set",
                actor_id,
                {"allocated_cny": money(budget.allocated_cny)},
            )
        return {"project_id": budget.project_id, "stage": budget.stage,
                "allocated_cny": money(budget.allocated_cny)}

    def record_commitment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "commitment.write")
        commitment = FundCommitment.from_dict(raw)
        project = self._project_row(commitment.project_id)
        _, policy = self._load_policy(project["policy_id"], project["policy_version"])
        if commitment.stage not in policy.stages:
            raise ValidationFailed("阶段不在项目政策中")
        if (user["dept"] or "") != "FINANCE":
            raise Forbidden("只有财政部门可以登记资金承诺")
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO fund_commitments(commitment_id,project_id,stage,source,amount_cny,"
                    "document_ref,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        commitment.commitment_id,
                        commitment.project_id,
                        commitment.stage,
                        commitment.source,
                        money(commitment.amount_cny),
                        commitment.document_ref,
                        actor_id,
                        self._now_text(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("资金承诺编号或凭据已经存在") from exc
            self._audit(
                "fund_commitment",
                commitment.commitment_id,
                "commitment.recorded",
                actor_id,
                {"project_id": commitment.project_id, "stage": commitment.stage,
                 "amount_cny": money(commitment.amount_cny)},
            )
        return {"commitment_id": commitment.commitment_id, "state": "committed"}

    def withdraw_commitment(self, actor_id: str, commitment_id: str, reason: str) -> dict[str, Any]:
        user = self._require(actor_id, "commitment.write")
        if (user["dept"] or "") != "FINANCE":
            raise Forbidden("只有财政部门可以撤回资金承诺")
        row = self.connection.execute(
            "SELECT * FROM fund_commitments WHERE commitment_id=?", (commitment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("资金承诺不存在")
        if row["state"] != "committed":
            raise InvalidState("资金承诺已撤回")
        if not reason.strip():
            raise ValidationFailed("撤回理由不能为空")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE fund_commitments SET state='withdrawn',revision=revision+1 WHERE commitment_id=?",
                (commitment_id,),
            )
            self._audit(
                "fund_commitment",
                commitment_id,
                "commitment.withdrawn",
                actor_id,
                {"reason": reason, "stage": row["stage"]},
            )
        return {"commitment_id": commitment_id, "state": "withdrawn"}

    # ---------------------------------------------------------------- 工作包

    def add_work_package(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "package.write")
        package = WorkPackage.from_dict(raw)
        project = self._project_row(package.project_id)
        _, policy = self._load_policy(project["policy_id"], project["policy_version"])
        if package.stage not in policy.stages:
            raise ValidationFailed("工作包阶段不在项目政策中")
        with transaction(self.connection, immediate=True):
            existing_packages = self.connection.execute(
                "SELECT package_id,depends_on_json FROM work_packages WHERE project_id=?",
                (package.project_id,),
            ).fetchall()
            graph = {row["package_id"]: json.loads(row["depends_on_json"]) for row in existing_packages}
            graph[package.package_id] = list(package.depends_on)
            _ensure_acyclic(graph, package.package_id)
            for dependency in package.depends_on:
                dep = self.connection.execute(
                    "SELECT project_id FROM work_packages WHERE package_id=?", (dependency,)
                ).fetchone()
                if dep is None:
                    raise ValidationFailed(f"依赖工作包不存在: {dependency}")
                if dep["project_id"] != package.project_id:
                    raise ValidationFailed("不能跨项目依赖工作包")
            try:
                self.connection.execute(
                    "INSERT INTO work_packages(package_id,project_id,stage,title,owner_dept,budget_cny,"
                    "depends_on_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        package.package_id,
                        package.project_id,
                        package.stage,
                        package.title,
                        package.owner_dept,
                        money(package.budget_cny),
                        canonical_json(list(package.depends_on)),
                        actor_id,
                        self._now_text(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("工作包编号已经存在") from exc
            self._audit(
                "work_package",
                package.package_id,
                "package.created",
                actor_id,
                {"stage": package.stage, "depends_on": list(package.depends_on)},
            )
        return {"package_id": package.package_id, "state": "pending", "stage": package.stage}

    def _package_row(self, package_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM work_packages WHERE package_id=?", (package_id,)
        ).fetchone()
        if row is None:
            raise NotFound("工作包不存在")
        return row

    def _unfinished_packages(self, project_id: str, stage: str) -> list[sqlite3.Row]:
        """该阶段尚未完工的工作包（含已暂停，恢复条件同样适用于它们）。"""
        return list(self.connection.execute(
            "SELECT * FROM work_packages WHERE project_id=? AND stage=? "
            "AND state IN ('pending','executing','suspended') ORDER BY package_id",
            (project_id, stage),
        ).fetchall())

    def _suspend_executing(self, package_ids: list[str]) -> None:
        for package_id in package_ids:
            self.connection.execute(
                "UPDATE work_packages SET state='suspended',revision=revision+1 "
                "WHERE package_id=? AND state='executing'",
                (package_id,),
            )

    def _covered_gate_codes(self, project_id: str, stage: str, package_id: str) -> set[str]:
        """当前生效且范围覆盖该工作包的紧急例外所覆盖的门槛代码。"""
        covered: set[str] = set()
        for exc in self._active_exceptions(project_id, stage):
            scope_packages = json.loads(exc["scope_packages_json"])
            if scope_packages and package_id not in scope_packages:
                continue
            covered.update(json.loads(exc["scope_gates_json"]))
        return covered

    @staticmethod
    def _impact_gate_codes(row: sqlite3.Row) -> set[str]:
        detail = json.loads(row["detail_json"])
        if row["impact_type"] == "EVIDENCE_INVALID":
            return {row["gate_code"]}
        if row["impact_type"] == "EXCEPTION_EXPIRED":
            return set(detail["scope_gates"])
        return {item["gate_code"] for item in detail["gates"]}  # POLICY_VERSION

    def _blocking_impacts_for_package(self, package: sqlite3.Row) -> list[sqlite3.Row]:
        """该工作包当前仍被卡点的影响；被生效紧急例外覆盖门槛的影响不计入。"""
        rows = self.connection.execute(
            "SELECT * FROM gate_impacts WHERE project_id=? AND status='open' ORDER BY impact_id",
            (package["project_id"],),
        ).fetchall()
        covered = self._covered_gate_codes(package["project_id"], package["stage"],
                                           package["package_id"])
        blockers = []
        for row in rows:
            stage_matches = row["package_id"] == package["package_id"] or (
                row["package_id"] is None and row["stage"] == package["stage"]
            )
            if not stage_matches:
                continue
            if self._impact_gate_codes(row) <= covered:
                continue  # 门槛全部处于有效紧急例外范围内
            blockers.append(row)
        return blockers

    def start_package(self, actor_id: str, package_id: str) -> dict[str, Any]:
        user = self._require(actor_id, "package.execute")
        package = self._package_row(package_id)
        if package["state"] != "pending":
            raise InvalidState("只有待执行工作包可以开工")
        project = self._project_row(package["project_id"])
        if project["current_stage"] != package["stage"]:
            raise InvalidState("阶段令尚未签发到该工作包所在阶段，不得开工")
        if user["role"] == "department" and (user["dept"] or "") != package["owner_dept"]:
            raise Forbidden("工作包不属于该责任部门")
        dependencies = json.loads(package["depends_on_json"])
        for dependency in dependencies:
            dep = self._package_row(dependency)
            if dep["state"] != "done":
                raise InvalidState(f"前置工作包尚未完成: {dependency}")
        # 到期失效是独立于本次决策的硬事实：先在单独事务中落地，避免随后门控
        # 拒绝时把暂停/影响记录一并回滚。
        self._apply_time_effects(project["project_id"])
        with transaction(self.connection, immediate=True):
            impacts = self._blocking_impacts_for_package(package)
            if impacts:
                raise InvalidState(
                    "工作包存在未消除的条件影响: "
                    + ", ".join(f"#{row['impact_id']}({row['impact_type']})" for row in impacts)
                )
            self.connection.execute(
                "UPDATE work_packages SET state='executing',revision=revision+1 WHERE package_id=?",
                (package_id,),
            )
            self._audit("work_package", package_id, "package.started", actor_id, {})
        return {"package_id": package_id, "state": "executing"}

    def complete_package(self, actor_id: str, package_id: str) -> dict[str, Any]:
        user = self._require(actor_id, "package.execute")
        package = self._package_row(package_id)
        if package["state"] != "executing":
            raise InvalidState("只有执行中的工作包可以完工")
        if user["role"] == "department" and (user["dept"] or "") != package["owner_dept"]:
            raise Forbidden("工作包不属于该责任部门")
        # 完工前同样先落地到期事实；门槛刚失效时工作包应转入暂停而非正常完工。
        self._apply_time_effects(package["project_id"])
        package = self._package_row(package_id)
        if package["state"] != "executing":
            raise InvalidState("工作包因前置条件失效已被暂停，不能按正常流程完工")
        with transaction(self.connection, immediate=True):
            # 政策换版不追溯在执行中的工作包；但证据/例外失效必须阻止正常完工。
            impacts = [
                row for row in self._blocking_impacts_for_package(package)
                if row["impact_type"] in ("EVIDENCE_INVALID", "EXCEPTION_EXPIRED")
            ]
            if impacts:
                raise InvalidState(
                    "工作包前置条件已失效: "
                    + ", ".join(f"#{row['impact_id']}({row['impact_type']})" for row in impacts)
                )
            self.connection.execute(
                "UPDATE work_packages SET state='done',revision=revision+1 WHERE package_id=?",
                (package_id,),
            )
            self._audit("work_package", package_id, "package.completed", actor_id, {})
        return {"package_id": package_id, "state": "done"}

    def resume_package(self, actor_id: str, package_id: str) -> dict[str, Any]:
        self._require(actor_id, "package.write")
        package = self._package_row(package_id)
        if package["state"] != "suspended":
            raise InvalidState("只有暂停的工作包可以恢复")
        self._apply_time_effects(package["project_id"])
        with transaction(self.connection, immediate=True):
            impacts = self._blocking_impacts_for_package(package)
            if impacts:
                raise InvalidState("恢复条件尚未满足，仍有未消除影响")
            self.connection.execute(
                "UPDATE work_packages SET state='executing',revision=revision+1 WHERE package_id=?",
                (package_id,),
            )
            self._audit("work_package", package_id, "package.resumed", actor_id, {})
        return {"package_id": package_id, "state": "executing"}

    # ------------------------------------------------------------------ 证据

    def _current_gate(self, project_id: str, stage: str, gate_code: str) -> sqlite3.Row:
        project = self._project_row(project_id)
        row = self.connection.execute(
            "SELECT * FROM project_gates WHERE project_id=? AND policy_version=? AND stage=? AND gate_code=?",
            (project_id, project["policy_version"], stage, gate_code),
        ).fetchone()
        if row is None:
            raise NotFound("当前政策版本下没有该门槛")
        return row

    def _stage_position(self, project: sqlite3.Row, stage: str) -> int | None:
        """阶段在项目现行政策中的序号；不在现行政策中返回 None。"""
        _, policy = self._load_policy(project["policy_id"], project["policy_version"])
        if stage not in policy.stages:
            return None
        return policy.stages.index(stage)

    def submit_evidence(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "evidence.write")
        evidence = Evidence.from_dict(raw)
        gate = self._current_gate(evidence.project_id, evidence.stage, evidence.gate_code)
        if (user["dept"] or "") != gate["owner_dept"]:
            raise Forbidden("证据必须由门槛责任部门提交")
        if evidence.kind != gate["evidence_kind"]:
            raise ValidationFailed(
                f"证据类型必须是 {gate['evidence_kind']}：{gate['evidence_requirement']}"
            )
        if gate["state"] not in ("pending", "invalid", "expired"):
            raise InvalidState("门槛已有有效证据，如需更换须先由批准人宣布失效")
        issued = date.fromisoformat(evidence.issued_at)
        expires_at = None
        if gate["valid_days"] is not None:
            expires_at = (issued + timedelta(days=int(gate["valid_days"]))).isoformat()
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO evidences(evidence_id,project_id,gate_code,kind,title,document_ref,"
                    "issued_by,issued_at,submitted_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        evidence.evidence_id,
                        evidence.project_id,
                        evidence.gate_code,
                        evidence.kind,
                        evidence.title,
                        evidence.document_ref,
                        evidence.issued_by,
                        evidence.issued_at,
                        actor_id,
                        self._now_text(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("证据编号或凭据编号已经存在") from exc
            self.connection.execute(
                "UPDATE project_gates SET state='satisfied',evidence_id=?,satisfied_at=?,"
                "expires_at=?,waive_note=NULL,satisfied_by=? WHERE gate_uid=?",
                (evidence.evidence_id, self._now_text(), expires_at, actor_id, gate["gate_uid"]),
            )
            self._audit(
                "evidence",
                evidence.evidence_id,
                "evidence.submitted",
                actor_id,
                {"project_id": evidence.project_id, "gate_code": evidence.gate_code,
                 "document_ref": evidence.document_ref, "expires_at": expires_at},
            )
            self._reconcile_impacts(evidence.project_id)
        return {
            "evidence_id": evidence.evidence_id,
            "gate_code": evidence.gate_code,
            "state": "satisfied",
            "expires_at": expires_at,
        }

    def invalidate_evidence(self, actor_id: str, evidence_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "evidence.invalidate")
        evidence = self.connection.execute(
            "SELECT * FROM evidences WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        if evidence is None:
            raise NotFound("证据不存在")
        if evidence["state"] != "active":
            raise InvalidState("证据已经失效")
        if not reason.strip():
            raise ValidationFailed("失效理由不能为空")
        project_id = evidence["project_id"]
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE evidences SET state='invalidated',invalidate_reason=?,revision=revision+1 "
                "WHERE evidence_id=?",
                (reason, evidence_id),
            )
            project = self._project_row(project_id)
            current_index = self._stage_position(project, project["current_stage"])
            # 只处理当前政策版本上仍以该证据为有效依据的门槛；已过期/失效门槛不重复登记。
            gate_rows = self.connection.execute(
                "SELECT * FROM project_gates WHERE project_id=? AND policy_version=? "
                "AND gate_code=? AND evidence_id=? AND state='satisfied'",
                (project_id, project["policy_version"], evidence["gate_code"], evidence_id),
            ).fetchall()
            for gate in gate_rows:
                self.connection.execute(
                    "UPDATE project_gates SET state='invalid',evidence_id=NULL,satisfied_at=NULL,"
                    "expires_at=NULL,satisfied_by=NULL WHERE gate_uid=?",
                    (gate["gate_uid"],),
                )
                stage_index = self._stage_position(project, gate["stage"])
                # 只影响当前及尚未进入的阶段；已经完成的阶段不追溯。
                if stage_index is None or stage_index < current_index:
                    continue
                affected = self._unfinished_packages(project_id, gate["stage"])
                executing = [p["package_id"] for p in affected if p["state"] == "executing"]
                self._suspend_executing(executing)
                self._record_impact(
                    impact_type="EVIDENCE_INVALID",
                    project_id=project_id,
                    policy_version=gate["policy_version"],
                    stage=gate["stage"],
                    gate_code=gate["gate_code"],
                    package_id=None,
                    detail={
                        "evidence_id": evidence_id,
                        "reason": reason,
                        "suspended_packages": executing,
                        "affected_packages": [p["package_id"] for p in affected],
                    },
                    recovery={
                        "instruction": "由责任部门重新提交满足门槛要求的有效证据",
                        "gate_code": gate["gate_code"],
                        "evidence_requirement": gate["evidence_requirement"],
                        "owner_dept": gate["owner_dept"],
                    },
                )
            self._audit(
                "evidence",
                evidence_id,
                "evidence.invalidated",
                actor_id,
                {"reason": reason, "project_id": project_id, "gate_code": evidence["gate_code"]},
            )
            self._reconcile_impacts(project_id)
        return {"evidence_id": evidence_id, "state": "invalidated"}

    def waive_gate(self, actor_id: str, project_id: str, stage: str, gate_code: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "gate.waive")
        if not note.strip():
            raise ValidationFailed("豁免/不适用认定必须写明依据")
        gate = self._current_gate(project_id, stage, gate_code)
        if gate["applies"] != "EXEMPTIBLE":
            raise InvalidState("该门槛为必须满足项，不得豁免")
        if gate["state"] == "waived":
            raise InvalidState("门槛已认定不适用")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE project_gates SET state='waived',evidence_id=NULL,satisfied_at=NULL,"
                "expires_at=NULL,waive_note=?,satisfied_by=? WHERE gate_uid=?",
                (note, actor_id, gate["gate_uid"]),
            )
            self._audit(
                "project_gate",
                f"{project_id}:{stage}:{gate_code}",
                "gate.waived",
                actor_id,
                {"note": note},
            )
            self._reconcile_impacts(project_id)
        return {"project_id": project_id, "stage": stage, "gate_code": gate_code, "state": "waived"}

    # ------------------------------------------------------------------ 例外

    def grant_exception(
        self,
        actor_id: str,
        project_id: str,
        stage: str,
        scope_gates: list[str],
        reason: str,
        expires_at: str,
        scope_packages: list[str] | None = None,
        exception_id: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "exception.grant")
        project = self._project_row(project_id)
        _, policy = self._load_policy(project["policy_id"], project["policy_version"])
        if stage not in policy.stage_gates:
            raise ValidationFailed("阶段不在项目政策中")
        if not scope_gates:
            raise ValidationFailed("紧急例外必须明确覆盖的门槛范围")
        if len(set(scope_gates)) != len(scope_gates):
            raise ValidationFailed("门槛范围不能重复")
        for code in scope_gates:
            if code not in policy.stage_gates[stage]:
                raise ValidationFailed(f"例外门槛 {code} 不属于该阶段")
        scope_packages = scope_packages or []
        for package_id in scope_packages:
            package = self.connection.execute(
                "SELECT stage FROM work_packages WHERE package_id=? AND project_id=?",
                (package_id, project_id),
            ).fetchone()
            if package is None:
                raise ValidationFailed(f"工作包不存在: {package_id}")
            if package["stage"] != stage:
                raise ValidationFailed(f"工作包 {package_id} 不属于该阶段")
        if not reason.strip():
            raise ValidationFailed("紧急例外必须写明理由")
        expiry = parse_utc(expires_at, "expires_at")
        if expiry <= self._now():
            raise ValidationFailed("到期时间必须晚于当前时间")
        exception_id = exception_id or f"exc-{len(scope_gates)}-{expiry.strftime('%Y%m%d%H%M%S')}"
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO exceptions(exception_id,project_id,stage,scope_gates_json,scope_packages_json,"
                    "reason,expires_at,issued_by,issued_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        exception_id,
                        project_id,
                        stage,
                        canonical_json(scope_gates),
                        canonical_json(scope_packages),
                        reason,
                        utc_text(expiry),
                        actor_id,
                        self._now_text(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("例外编号已经存在") from exc
            self._audit(
                "exception",
                exception_id,
                "exception.granted",
                actor_id,
                {"stage": stage, "scope_gates": scope_gates, "scope_packages": scope_packages,
                 "reason": reason, "expires_at": utc_text(expiry)},
            )
        return {
            "exception_id": exception_id,
            "state": "active",
            "scope_gates": scope_gates,
            "expires_at": utc_text(expiry),
        }

    def revoke_exception(self, actor_id: str, exception_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "exception.revoke")
        row = self._exception_row(exception_id)
        if row["state"] != "active":
            raise InvalidState("例外不在生效中")
        if not note.strip():
            raise ValidationFailed("撤销说明不能为空")
        with transaction(self.connection, immediate=True):
            self._expire_or_revoke_exception(row, "revoked", note)
            self._audit("exception", exception_id, "exception.revoked", actor_id, {"note": note})
        return {"exception_id": exception_id, "state": "revoked"}

    def sweep_exception_expiry(self, actor_id: str, project_id: str) -> dict[str, Any]:
        self._require(actor_id, "exception.sweep")
        with transaction(self.connection, immediate=True):
            expired = self._sweep_exceptions(project_id)
        return {"project_id": project_id, "expired": expired}

    def _exception_row(self, exception_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM exceptions WHERE exception_id=?", (exception_id,)
        ).fetchone()
        if row is None:
            raise NotFound("紧急例外不存在")
        return row

    def _sweep_exceptions(self, project_id: str) -> list[str]:
        """把已到期但仍标记 active 的例外转为 expired，并暂停范围内未完成工作包。"""
        now_text = self._now_text()
        rows = self.connection.execute(
            "SELECT * FROM exceptions WHERE project_id=? AND state='active' AND expires_at<=?",
            (project_id, now_text),
        ).fetchall()
        expired: list[str] = []
        for row in rows:
            self._expire_or_revoke_exception(row, "expired", "例外到期自动失效")
            expired.append(row["exception_id"])
        return expired

    def _expire_or_revoke_exception(self, row: sqlite3.Row, state: str, note: str) -> None:
        self.connection.execute(
            "UPDATE exceptions SET state=?,closure_note=?,closed_at=? WHERE exception_id=?",
            (state, note, self._now_text(), row["exception_id"]),
        )
        scope_gates = json.loads(row["scope_gates_json"])
        scope_packages = json.loads(row["scope_packages_json"])
        project = self._project_row(row["project_id"])
        all_unfinished = self._unfinished_packages(row["project_id"], row["stage"])
        if scope_packages:
            # 显式范围：只挂到指定工作包（按包登记影响）。
            targets = {
                package["package_id"]: package
                for package in all_unfinished
                if package["package_id"] in set(scope_packages)
            }
        else:
            # 未显式列工作包：范围推定覆盖该阶段所有未完成工作包，记一条阶段级影响。
            targets = {package["package_id"]: package for package in all_unfinished}
        executing = [pid for pid, package in targets.items() if package["state"] == "executing"]
        self._suspend_executing(executing)
        base_detail = {
            "exception_id": row["exception_id"],
            "scope_gates": scope_gates,
            "note": note,
            "suspended_packages": executing,
        }
        if scope_packages:
            for package_id in targets:
                self._record_impact(
                    impact_type="EXCEPTION_EXPIRED",
                    project_id=row["project_id"],
                    policy_version=project["policy_version"],
                    stage=row["stage"],
                    gate_code=None,
                    package_id=package_id,
                    detail=base_detail,
                    recovery={
                        "instruction": "补齐例外所覆盖的全部门槛后恢复工作包",
                        "gate_codes": scope_gates,
                    },
                )
        else:
            self._record_impact(
                impact_type="EXCEPTION_EXPIRED",
                project_id=row["project_id"],
                policy_version=project["policy_version"],
                stage=row["stage"],
                gate_code=None,
                package_id=None,
                detail={**base_detail,
                        "affected_packages": [package["package_id"] for package in all_unfinished]},
                recovery={"instruction": "补齐例外所覆盖的全部门槛", "gate_codes": scope_gates},
            )
        self._reconcile_impacts(row["project_id"])

    def sweep_evidence_expiry(self, actor_id: str, project_id: str) -> dict[str, Any]:
        """显式扫描超过有效期限的证据（开工/签发阶段令时也会自动执行）。"""
        self._require(actor_id, "evidence.invalidate")
        with transaction(self.connection, immediate=True):
            expired = self._sweep_evidence_expiry(project_id)
        return {"project_id": project_id, "expired_gates": expired}

    def _apply_time_effects(self, project_id: str) -> None:
        """在独立事务中完成证据到期、例外到期的状态转换并提交。

        这样即使随后的门控决策因仍有卡点而失败，条件失效导致的暂停和影响
        记录也不会被回滚——条件失效本身是客观事实，与本次决策成败无关。
        """
        with transaction(self.connection, immediate=True):
            self._sweep_evidence_expiry(project_id)
            self._sweep_exceptions(project_id)

    def _sweep_evidence_expiry(self, project_id: str) -> list[str]:
        today = self._today().isoformat()
        project = self._project_row(project_id)
        current_index = self._stage_position(project, project["current_stage"])
        rows = self.connection.execute(
            "SELECT * FROM project_gates WHERE project_id=? AND policy_version=? "
            "AND state='satisfied' AND expires_at IS NOT NULL AND expires_at<?",
            (project_id, project["policy_version"], today),
        ).fetchall()
        expired: list[str] = []
        for gate in rows:
            stage_index = self._stage_position(project, gate["stage"])
            if stage_index is None or stage_index < current_index:
                continue
            self.connection.execute(
                "UPDATE project_gates SET state='expired' WHERE gate_uid=?", (gate["gate_uid"],)
            )
            expired.append(f"{gate['stage']}:{gate['gate_code']}")
            affected = self._unfinished_packages(project_id, gate["stage"])
            self._suspend_executing([p["package_id"] for p in affected if p["state"] == "executing"])
            self._record_impact(
                impact_type="EVIDENCE_INVALID",
                project_id=project_id,
                policy_version=gate["policy_version"],
                stage=gate["stage"],
                gate_code=gate["gate_code"],
                package_id=None,
                detail={
                    "evidence_id": gate["evidence_id"],
                    "reason": "证据超过有效期限",
                    "suspended_packages": [p["package_id"] for p in affected if p["state"] == "executing"],
                    "affected_packages": [p["package_id"] for p in affected],
                },
                recovery={
                    "instruction": "由责任部门重新提交在有效期内的证据",
                    "gate_code": gate["gate_code"],
                    "evidence_requirement": gate["evidence_requirement"],
                    "owner_dept": gate["owner_dept"],
                },
            )
        if expired:
            self._reconcile_impacts(project_id)
        return expired

    def _active_exceptions(self, project_id: str, stage: str) -> list[sqlite3.Row]:
        now_text = self._now_text()
        return list(self.connection.execute(
            "SELECT * FROM exceptions WHERE project_id=? AND stage=? AND state='active' AND expires_at>?",
            (project_id, stage, now_text),
        ).fetchall())

    # ------------------------------------------------------------------ 影响

    def _record_impact(
        self,
        *,
        impact_type: str,
        project_id: str,
        policy_version: int | None,
        stage: str,
        gate_code: str | None,
        package_id: str | None,
        detail: Mapping[str, Any],
        recovery: Mapping[str, Any],
    ) -> None:
        self.connection.execute(
            "INSERT INTO gate_impacts(impact_type,project_id,policy_version,stage,gate_code,package_id,"
            "detail_json,recovery_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                impact_type,
                project_id,
                policy_version,
                stage,
                gate_code,
                package_id,
                canonical_json(detail),
                canonical_json(recovery),
                self._now_text(),
            ),
        )

    def _reconcile_impacts(self, project_id: str) -> None:
        """条件恢复后自动关闭影响记录；暂停的工作包仍需显式恢复。

        恢复条件一律按项目当前政策版本上的门槛状态判定——换版后旧版本门槛不再
        被评估，工作包能否恢复只取决于现行门槛。
        """
        today = self._today()
        project = self._project_row(project_id)
        current_version = int(project["policy_version"])
        rows = self.connection.execute(
            "SELECT * FROM gate_impacts WHERE project_id=? AND status='open' ORDER BY impact_id",
            (project_id,),
        ).fetchall()

        def gate_open(stage: str, code: str) -> bool:
            gate = self.connection.execute(
                "SELECT * FROM project_gates WHERE project_id=? AND policy_version=? AND stage=? AND gate_code=?",
                (project_id, current_version, stage, code),
            ).fetchone()
            if gate is None:
                return False
            if gate["state"] == "waived":
                return True
            if gate["state"] != "satisfied":
                return False
            if gate["expires_at"] is not None and date.fromisoformat(gate["expires_at"]) < today:
                return False
            return True

        for row in rows:
            detail = json.loads(row["detail_json"])
            if row["impact_type"] == "EVIDENCE_INVALID":
                recovered = gate_open(row["stage"], row["gate_code"])
            elif row["impact_type"] == "EXCEPTION_EXPIRED":
                recovered = all(
                    gate_open(row["stage"], code) for code in detail["scope_gates"]
                )
            else:  # POLICY_VERSION
                recovered = all(
                    gate_open(row["stage"], item["gate_code"]) for item in detail["gates"]
                )
            if recovered:
                self.connection.execute(
                    "UPDATE gate_impacts SET status='resolved',resolved_at=? WHERE impact_id=?",
                    (self._now_text(), row["impact_id"]),
                )

    def resolve_impact(self, actor_id: str, impact_id: int, note: str) -> dict[str, Any]:
        self._require(actor_id, "impact.resolve")
        row = self.connection.execute(
            "SELECT * FROM gate_impacts WHERE impact_id=?", (impact_id,)
        ).fetchone()
        if row is None:
            raise NotFound("影响记录不存在")
        if row["status"] == "resolved":
            raise InvalidState("影响记录已关闭")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE gate_impacts SET status='resolved',resolved_at=? WHERE impact_id=?",
                (self._now_text(), impact_id),
            )
            self._audit(
                "impact",
                str(impact_id),
                "impact.resolved",
                actor_id,
                {"note": note, "impact_type": row["impact_type"]},
            )
        return {"impact_id": impact_id, "status": "resolved"}

    # ---------------------------------------------------------------- 阶段令

    def _evaluate_stage(
        self, project: sqlite3.Row, stage: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        """返回 (门槛判定, 卡点, 生效例外编号)。"""
        gates = self.connection.execute(
            "SELECT * FROM project_gates WHERE project_id=? AND policy_version=? AND stage=? ORDER BY gate_uid",
            (project["project_id"], project["policy_version"], stage),
        ).fetchall()
        exceptions = self._active_exceptions(project["project_id"], stage)
        today = self._today()
        results: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        used_exceptions: list[str] = []
        for gate in gates:
            covered_by = next(
                (exc for exc in exceptions if gate["gate_code"] in json.loads(exc["scope_gates_json"])),
                None,
            )
            open_state = False
            reason = ""
            if gate["state"] == "waived":
                open_state = True
                reason = "经批准认定不适用"
            elif gate["state"] == "satisfied":
                if gate["expires_at"] is not None and date.fromisoformat(gate["expires_at"]) < today:
                    reason = f"证据已过有效期（{gate['expires_at']}），需重新提交"
                else:
                    open_state = True
            elif gate["state"] == "expired":
                reason = "证据已超过有效期限，需重新提交"
            elif gate["state"] == "invalid":
                reason = "证据已被宣布失效，需重新提交有效证据"
            else:
                reason = "尚未提交有效证据"
            result = {
                "gate_code": gate["gate_code"],
                "name": gate["name"],
                "kind": gate["kind"],
                "owner_dept": gate["owner_dept"],
                "state": gate["state"],
                "open": open_state,
                "reason": reason,
                "evidence_requirement": gate["evidence_requirement"],
                "evidence_id": gate["evidence_id"],
                "expires_at": gate["expires_at"],
                "waive_note": gate["waive_note"],
                "covered_by_exception": None,
            }
            evidence = None
            if gate["evidence_id"]:
                evidence = self.connection.execute(
                    "SELECT evidence_id,document_ref,title,issued_by,issued_at,state FROM evidences "
                    "WHERE evidence_id=?",
                    (gate["evidence_id"],),
                ).fetchone()
            if evidence is not None:
                result["evidence"] = dict(evidence)
            if not open_state and covered_by is not None:
                result["covered_by_exception"] = covered_by["exception_id"]
                used_exceptions.append(covered_by["exception_id"])
            elif not open_state:
                blockers.append(result)
            results.append(result)
        return results, blockers, sorted(set(used_exceptions))

    def _fund_position(self, project_id: str, stage: str) -> dict[str, Any]:
        commitments = list(self.connection.execute(
            "SELECT * FROM fund_commitments WHERE project_id=? AND stage=? ORDER BY commitment_id",
            (project_id, stage),
        ).fetchall())
        committed = sum(
            (Decimal(row["amount_cny"]) for row in commitments if row["state"] == "committed"),
            Decimal("0"),
        )
        budget_row = self.connection.execute(
            "SELECT * FROM stage_budgets WHERE project_id=? AND stage=?", (project_id, stage)
        ).fetchone()
        allocated = Decimal("0") if budget_row is None else Decimal(budget_row["allocated_cny"])
        return {
            "stage": stage,
            "budget_allocated_cny": money(allocated),
            "committed_total_cny": money(committed),
            "gap_cny": money(max(allocated - committed, Decimal("0"))),
            "commitments": [dict(row) for row in commitments],
        }

    def issue_stage_order(
        self,
        actor_id: str,
        project_id: str,
        idempotency_key: str,
        note: str = "",
    ) -> dict[str, Any]:
        self._require(actor_id, "order.issue")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValidationFailed("idempotency_key 不能为空")
        idempotency_key = idempotency_key.strip()
        request_digest = digest(
            {"project_id": project_id, "action": "issue_stage_order"}
        )
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM gate_idempotency "
            "WHERE scope='stage_order' AND idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同项目的阶段令")
            return json.loads(stored["response_json"])

        project = self._project_row(project_id)
        _, policy = self._load_policy(project["policy_id"], project["policy_version"])
        current_index = policy.stages.index(project["current_stage"])
        if current_index == len(policy.stages) - 1:
            raise InvalidState("项目已处于最终阶段，没有下一阶段令可签发")
        to_stage = policy.stages[current_index + 1]
        duplicate = self.connection.execute(
            "SELECT order_id FROM stage_orders WHERE project_id=? AND to_stage=?",
            (project_id, to_stage),
        ).fetchone()
        if duplicate is not None:
            raise InvalidState("该阶段令已经签发")

        # 到期失效先在独立事务落地；即便本次因卡点拒绝签发，暂停与影响也已留存。
        self._apply_time_effects(project_id)

        with transaction(self.connection, immediate=True):
            project = self._project_row(project_id)
            _, policy = self._load_policy(project["policy_id"], project["policy_version"])
            gate_results, blockers, exception_ids = self._evaluate_stage(project, to_stage)
            if blockers:
                raise InvalidState(
                    "存在未满足的适用门槛: "
                    + ", ".join(f"{item['gate_code']}（{item['reason']}）" for item in blockers)
                )

            fund = self._fund_position(project_id, to_stage)
            if Decimal(fund["budget_allocated_cny"]) <= 0:
                raise InvalidState(f"{to_stage} 阶段预算尚未核定")
            if Decimal(fund["committed_total_cny"]) < Decimal(fund["budget_allocated_cny"]):
                raise InvalidState(
                    f"{to_stage} 资金承诺不足：缺口 {fund['gap_cny']} 元"
                )

            package_budget = self.connection.execute(
                "SELECT COALESCE(sum(CAST(budget_cny AS REAL)),0) total FROM work_packages "
                "WHERE project_id=? AND stage=?",
                (project_id, to_stage),
            ).fetchone()["total"]
            if Decimal(str(package_budget)) > Decimal(fund["budget_allocated_cny"]):
                raise InvalidState(
                    f"{to_stage} 工作包预算合计 {package_budget} 元超过阶段预算 "
                    f"{fund['budget_allocated_cny']} 元"
                )

            # 决策权限：策略为每个阶段规定批准角色（可带 :DEPT 层级限定）。
            required_authority = policy.decision_authority[to_stage]
            actor = self._user(actor_id)
            role, _, level = required_authority.partition(":")
            if actor["role"] != role or (level and (actor["dept"] or "") != level):
                raise Forbidden(
                    f"签发 {to_stage} 阶段令需要 {required_authority} 决策权限"
                )

            policy_row = self.connection.execute(
                "SELECT content_sha256 FROM policies WHERE policy_id=? AND version=?",
                (project["policy_id"], project["policy_version"]),
            ).fetchone()
            order_id = f"order-{project_id}-{to_stage}-{self._now().strftime('%Y%m%d%H%M%S')}"
            gate_snapshot = gate_results
            fund_snapshot = fund
            decided_at = self._now_text()
            self.connection.execute(
                "INSERT INTO stage_orders(order_id,project_id,from_stage,to_stage,policy_id,policy_version,"
                "policy_sha256,decision_role,decided_by,decided_at,gate_snapshot_json,fund_snapshot_json,"
                "exception_ids_json,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    order_id,
                    project_id,
                    project["current_stage"],
                    to_stage,
                    project["policy_id"],
                    project["policy_version"],
                    policy_row["content_sha256"],
                    required_authority,
                    actor_id,
                    decided_at,
                    canonical_json(gate_snapshot),
                    canonical_json(fund_snapshot),
                    canonical_json(exception_ids),
                    idempotency_key,
                ),
            )
            self.connection.execute(
                "UPDATE projects SET current_stage=? WHERE project_id=?",
                (to_stage, project_id),
            )
            response = {
                "order_id": order_id,
                "project_id": project_id,
                "from_stage": project["current_stage"],
                "to_stage": to_stage,
                "decided_at": decided_at,
                "decision_role": required_authority,
                "policy": {"policy_id": project["policy_id"], "version": project["policy_version"],
                           "sha256": policy_row["content_sha256"]},
                "gates": gate_snapshot,
                "fund": fund_snapshot,
                "exception_ids": exception_ids,
                "state": "issued",
            }
            self.connection.execute(
                "INSERT INTO gate_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('stage_order',?,?,?,?)",
                (idempotency_key, request_digest, canonical_json(response), decided_at),
            )
            self._audit(
                "stage_order",
                order_id,
                "order.issued",
                actor_id,
                {"to_stage": to_stage, "exceptions": exception_ids, "note": note},
            )
        return response

    # -------------------------------------------------------------- 负责人视图

    def project_status(self, actor_id: str, project_id: str) -> dict[str, Any]:
        self._require(actor_id, "status.read")
        project = self._project_row(project_id)
        policy_row, policy = self._load_policy(project["policy_id"], project["policy_version"])
        current_index = policy.stages.index(project["current_stage"])
        next_stage = policy.stages[current_index + 1] if current_index + 1 < len(policy.stages) else None

        orders = [
            {
                "order_id": row["order_id"],
                "from_stage": row["from_stage"],
                "to_stage": row["to_stage"],
                "decided_by": row["decided_by"],
                "decided_at": row["decided_at"],
                "decision_role": row["decision_role"],
                "policy_id": row["policy_id"],
                "policy_version": row["policy_version"],
                "policy_sha256": row["policy_sha256"],
                "exception_ids": json.loads(row["exception_ids_json"]),
                "gate_snapshot": json.loads(row["gate_snapshot_json"]),
            }
            for row in self.connection.execute(
                "SELECT * FROM stage_orders WHERE project_id=? ORDER BY decided_at", (project_id,)
            ).fetchall()
        ]

        stage_views = []
        blockers: list[dict[str, Any]] = []
        authority = None
        for index, stage in enumerate(policy.stages):
            gate_results, stage_blockers, _used_exceptions = self._evaluate_stage(project, stage)
            fund = self._fund_position(project_id, stage)
            view = {
                "stage": stage,
                "sequence": index,
                "entered": any(order["to_stage"] == stage for order in orders),
                "is_current": stage == project["current_stage"],
                "gates": gate_results,
                "fund": fund,
                "exceptions": [
                    {
                        "exception_id": row["exception_id"],
                        "scope_gates": json.loads(row["scope_gates_json"]),
                        "scope_packages": json.loads(row["scope_packages_json"]),
                        "reason": row["reason"],
                        "expires_at": row["expires_at"],
                        "state": row["state"],
                    }
                    for row in self.connection.execute(
                        "SELECT * FROM exceptions WHERE project_id=? AND stage=? ORDER BY issued_at",
                        (project_id, stage),
                    ).fetchall()
                ],
            }
            stage_views.append(view)
            if next_stage == stage:
                blockers = stage_blockers
                required = policy.decision_authority[stage]
                role, _, level = required.partition(":")
                authority = {
                    "stage": stage,
                    "required_decision_role": required,
                    "role": role,
                    "level": level or None,
                    "policy_id": project["policy_id"],
                    "policy_version": project["policy_version"],
                    "policy_sha256": policy_row["content_sha256"],
                    "policy_name": policy.name,
                }

        impacts = [
            {
                "impact_id": row["impact_id"],
                "type": row["impact_type"],
                "stage": row["stage"],
                "gate_code": row["gate_code"],
                "package_id": row["package_id"],
                "detail": json.loads(row["detail_json"]),
                "recovery": json.loads(row["recovery_json"]),
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in self.connection.execute(
                "SELECT * FROM gate_impacts WHERE project_id=? ORDER BY impact_id", (project_id,)
            ).fetchall()
        ]
        packages = [
            {
                "package_id": row["package_id"],
                "stage": row["stage"],
                "title": row["title"],
                "owner_dept": row["owner_dept"],
                "budget_cny": row["budget_cny"],
                "depends_on": json.loads(row["depends_on_json"]),
                "state": row["state"],
                "open_impacts": [
                    item["impact_id"]
                    for item in impacts
                    if item["status"] == "open"
                    and (
                        item["package_id"] == row["package_id"]
                        or (item["package_id"] is None and item["stage"] == row["stage"])
                    )
                ],
            }
            for row in self.connection.execute(
                "SELECT * FROM work_packages WHERE project_id=? ORDER BY stage,package_id", (project_id,)
            ).fetchall()
        ]
        return {
            "project_id": project_id,
            "name": project["name"],
            "owner_org": project["owner_org"],
            "manager_id": project["manager_id"],
            "total_budget_cny": project["budget_cny"],
            "current_stage": project["current_stage"],
            "next_stage": next_stage,
            "policy": {
                "policy_id": project["policy_id"],
                "version": project["policy_version"],
                "name": policy.name,
                "sha256": policy_row["content_sha256"],
            },
            "decision_authority": authority,
            "blockers": blockers,
            "stages": stage_views,
            "open_impacts": [item for item in impacts if item["status"] == "open"],
            "impacts": impacts,
            "work_packages": packages,
            "stage_orders": orders,
        }

    # ------------------------------------------------------------------ 审计

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM gate_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
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
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}


def _ensure_acyclic(graph: Mapping[str, list[str]], start: str) -> None:
    """新增依赖后做一次可达性环检测；成环则拒绝该工作包计划。"""
    visiting: set[str] = set()
    visited: set[str] = set()

    def dfs(node: str) -> None:
        if node in visiting:
            raise ValidationFailed("工作包依赖成环")
        if node in visited:
            return
        visiting.add(node)
        for neighbor in graph.get(node, []):
            dfs(neighbor)
        visiting.discard(node)
        visited.add(node)

    dfs(start)
