"""重大项目阶段门控领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
DEPARTMENT_CODE = re.compile(r"^[A-Z]{2,12}$")

# 门槛责任类别：发改/项目办、自然资源（用地）、生态环境（环评）、财政（资金）、
# 设备业主、行业主管、项目负责人。
DEPARTMENTS = {"DRC", "NATRES", "ECOENV", "FINANCE", "EQUIPMENT", "INDUSTRY", "PMO"}

# 门槛（前置条件）类别，覆盖用地、环评、资金、设备交付以及通用行政许可。
GATE_KINDS = {
    "LAND",          # 用地：用地预审与选址意见书、建设用地批准等
    "EIA",           # 环评：环境影响评价批复
    "FUND",          # 资金：资金承诺函、预算下达、配套资金到位
    "EQUIPMENT",     # 设备交付：关键设备到货节点/交付承诺
    "PERMIT",        # 其他行政许可（规划许可、施工许可等）
    "POLICY",        # 政策/规划符合性
}

# 门槛对某一阶段的适用方式：必须满足 / 可按规则豁免（不适用须留痕）。
APPLIES_VALUES = {"REQUIRED", "EXEMPTIBLE"}

# 有效证据的核验方式。
EVIDENCE_KINDS = {"DOCUMENT", "SYSTEM_RECORD", "THIRD_PARTY", "SITE_PHOTO", "MEETING_MINUTE"}


def required_text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def department(value: object, field: str) -> str:
    result = required_text(value, field, 12).upper()
    if not DEPARTMENT_CODE.fullmatch(result) or result not in DEPARTMENTS:
        raise ValidationFailed(f"{field} 必须是受支持的责任部门代码")
    return result


def _string_list(value: object, field: str, *, minimum: int = 0) -> list[str]:
    if not isinstance(value, list):
        raise ValidationFailed(f"{field} 必须是数组")
    items = [required_text(item, f"{field}[]", 128) for item in value]
    if len(items) < minimum:
        raise ValidationFailed(f"{field} 至少需要 {minimum} 项")
    return items


@dataclass(frozen=True, slots=True)
class GateSpec:
    """策略模板里的一条门槛定义。"""

    gate_code: str
    name: str
    kind: str
    owner_dept: str
    applies: str
    evidence_kind: str
    evidence_requirement: str
    valid_days: int | None  # 证据/批准的有效期限（天），None 表示随政策版本管理
    policy_version: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], policy_version: str) -> "GateSpec":
        kind = required_text(raw.get("kind"), "kind", 16).upper()
        if kind not in GATE_KINDS:
            raise ValidationFailed("kind 不是受支持的门槛类别")
        applies = required_text(raw.get("applies", "REQUIRED"), "applies", 16).upper()
        if applies not in APPLIES_VALUES:
            raise ValidationFailed("applies 必须是 REQUIRED 或 EXEMPTIBLE")
        evidence_kind = required_text(raw.get("evidence_kind"), "evidence_kind", 16).upper()
        if evidence_kind not in EVIDENCE_KINDS:
            raise ValidationFailed("evidence_kind 不是受支持的证据类型")
        valid_days_raw = raw.get("valid_days")
        if valid_days_raw is None:
            valid_days = None
        else:
            valid_days = positive_integer(valid_days_raw, "valid_days")
        return cls(
            gate_code=identifier(raw.get("gate_code"), "gate_code"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            owner_dept=department(raw.get("owner_dept"), "owner_dept"),
            applies=applies,
            evidence_kind=evidence_kind,
            evidence_requirement=required_text(raw.get("evidence_requirement"), "evidence_requirement"),
            valid_days=valid_days,
            policy_version=policy_version,
        )


@dataclass(frozen=True, slots=True)
class Policy:
    """门控策略（政策规则）版本，定义各阶段的适用门槛与决策权限。"""

    policy_id: str
    version: int
    name: str
    effective_date: str
    stages: tuple[str, ...]
    gates: tuple[GateSpec, ...]
    stage_gates: Mapping[str, tuple[str, ...]]  # stage -> gate_code 列表
    decision_authority: Mapping[str, str]       # stage -> 批准角色

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Policy":
        policy_id = identifier(raw.get("policy_id"), "policy_id")
        version = positive_integer(raw.get("version"), "version")
        name = required_text(raw.get("name"), "name")
        effective_date = required_text(raw.get("effective_date"), "effective_date", 10)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", effective_date):
            raise ValidationFailed("effective_date 必须是 YYYY-MM-DD 日期")
        stages = tuple(_string_list(raw.get("stages"), "stages", minimum=2))
        if len(set(stages)) != len(stages):
            raise ValidationFailed("stages 不能重复")
        raw_gates = raw.get("gates")
        if not isinstance(raw_gates, list) or not raw_gates:
            raise ValidationFailed("gates 至少需要一条门槛定义")
        gates = tuple(GateSpec.from_dict(item, f"{policy_id}:v{version}") for item in raw_gates)
        codes = [gate.gate_code for gate in gates]
        if len(set(codes)) != len(codes):
            raise ValidationFailed("gate_code 不能重复")
        gate_codes = set(codes)
        raw_stage_gates = raw.get("stage_gates")
        if not isinstance(raw_stage_gates, Mapping):
            raise ValidationFailed("stage_gates 必须是对象")
        stage_gates: dict[str, tuple[str, ...]] = {}
        for stage in stages:
            codes_for_stage = raw_stage_gates.get(stage)
            if codes_for_stage is None:
                # 缺省表示该阶段没有前置门槛（如立项阶段）。
                stage_gates[stage] = ()
                continue
            if not isinstance(codes_for_stage, list) or not codes_for_stage:
                raise ValidationFailed(f"stage_gates.{stage} 必须是非空数组或省略")
            parsed = tuple(identifier(code, f"stage_gates.{stage}[]") for code in codes_for_stage)
            unknown = set(parsed) - gate_codes
            if unknown:
                raise ValidationFailed(f"stage_gates.{stage} 引用了未定义门槛 {sorted(unknown)}")
            if len(set(parsed)) != len(parsed):
                raise ValidationFailed(f"stage_gates.{stage} 不能重复引用门槛")
            stage_gates[stage] = parsed
        unbound = [
            code for code in codes
            if not any(code in bound for bound in stage_gates.values())
        ]
        if unbound:
            raise ValidationFailed(f"门槛未绑定到任何阶段: {unbound}")
        raw_authority = raw.get("decision_authority")
        if not isinstance(raw_authority, Mapping):
            raise ValidationFailed("decision_authority 必须是对象")
        decision_authority: dict[str, str] = {}
        for stage in stages:
            role = raw_authority.get(stage)
            if not isinstance(role, str) or not role.strip():
                raise ValidationFailed(f"decision_authority.{stage} 不能为空")
            decision_authority[stage] = role.strip()
        return cls(
            policy_id=policy_id,
            version=version,
            name=name,
            effective_date=effective_date,
            stages=stages,
            gates=gates,
            stage_gates=stage_gates,
            decision_authority=decision_authority,
        )


@dataclass(frozen=True, slots=True)
class Project:
    project_id: str
    name: str
    owner_org: str
    manager_id: str
    policy_id: str
    initial_stage: str
    budget_cny: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Project":
        return cls(
            project_id=identifier(raw.get("project_id"), "project_id"),
            name=required_text(raw.get("name"), "name"),
            owner_org=required_text(raw.get("owner_org"), "owner_org"),
            manager_id=identifier(raw.get("manager_id"), "manager_id"),
            policy_id=identifier(raw.get("policy_id"), "policy_id"),
            initial_stage=required_text(raw.get("initial_stage"), "initial_stage", 64),
            budget_cny=decimal_value(raw.get("budget_cny"), "budget_cny", minimum=Decimal("0")),
        )


@dataclass(frozen=True, slots=True)
class WorkPackage:
    package_id: str
    project_id: str
    stage: str
    title: str
    owner_dept: str
    budget_cny: Decimal
    depends_on: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WorkPackage":
        depends = raw.get("depends_on", [])
        if not isinstance(depends, list):
            raise ValidationFailed("depends_on 必须是数组")
        return cls(
            package_id=identifier(raw.get("package_id"), "package_id"),
            project_id=identifier(raw.get("project_id"), "project_id"),
            stage=required_text(raw.get("stage"), "stage", 64),
            title=required_text(raw.get("title"), "title"),
            owner_dept=department(raw.get("owner_dept"), "owner_dept"),
            budget_cny=decimal_value(raw.get("budget_cny"), "budget_cny", minimum=Decimal("0")),
            depends_on=tuple(identifier(code, "depends_on[]") for code in depends),
        )


@dataclass(frozen=True, slots=True)
class Evidence:
    """责任部门为满足门槛提交的有效证据。"""

    evidence_id: str
    project_id: str
    stage: str
    gate_code: str
    kind: str
    title: str
    document_ref: str
    issued_by: str
    issued_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Evidence":
        kind = required_text(raw.get("kind"), "kind", 16).upper()
        if kind not in EVIDENCE_KINDS:
            raise ValidationFailed("kind 不是受支持的证据类型")
        issued_at = required_text(raw.get("issued_at"), "issued_at", 40)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", issued_at):
            raise ValidationFailed("issued_at 必须是 YYYY-MM-DD 日期")
        return cls(
            evidence_id=identifier(raw.get("evidence_id"), "evidence_id"),
            project_id=identifier(raw.get("project_id"), "project_id"),
            stage=required_text(raw.get("stage"), "stage", 64),
            gate_code=identifier(raw.get("gate_code"), "gate_code"),
            kind=kind,
            title=required_text(raw.get("title"), "title"),
            document_ref=required_text(raw.get("document_ref"), "document_ref"),
            issued_by=required_text(raw.get("issued_by"), "issued_by"),
            issued_at=issued_at,
        )


@dataclass(frozen=True, slots=True)
class FundCommitment:
    """资金承诺：记录承诺主体、金额及其所支撑的阶段。"""

    commitment_id: str
    project_id: str
    stage: str
    source: str
    amount_cny: Decimal
    document_ref: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FundCommitment":
        return cls(
            commitment_id=identifier(raw.get("commitment_id"), "commitment_id"),
            project_id=identifier(raw.get("project_id"), "project_id"),
            stage=required_text(raw.get("stage"), "stage", 64),
            source=required_text(raw.get("source"), "source"),
            amount_cny=decimal_value(raw.get("amount_cny"), "amount_cny", minimum=Decimal("0.01")),
            document_ref=required_text(raw.get("document_ref"), "document_ref"),
        )


@dataclass(frozen=True, slots=True)
class StageBudget:
    """阶段预算：项目办为某阶段核定的预算上限。"""

    project_id: str
    stage: str
    allocated_cny: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StageBudget":
        return cls(
            project_id=identifier(raw.get("project_id"), "project_id"),
            stage=required_text(raw.get("stage"), "stage", 64),
            allocated_cny=decimal_value(raw.get("allocated_cny"), "allocated_cny", minimum=Decimal("0")),
        )
