"""阶段门控领域输入契约与校验。"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed
from .rules import money


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

CONDITION_TYPES = {"evidence", "budget"}
OVERRIDE_SCOPES = {"condition", "package"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
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


def money_value(value: object, field: str, *, minimum: Decimal | None = Decimal("0")) -> Decimal:
    try:
        result = money(value)
    except ValueError as exc:
        raise ValidationFailed(str(exc).replace("金额", field)) from exc
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def tag_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValidationFailed(f"{field} 必须是字符串数组")
    result = [item.strip() for item in value]
    if len(result) != len(set(result)):
        raise ValidationFailed(f"{field} 不能重复")
    return result


def iso_text(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        parsed = parse_utc(text, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return text if text.endswith("Z") else parsed.isoformat().replace("+00:00", "Z")


def validate_project(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "project_id": identifier(raw.get("project_id"), "project_id"),
        "name": required_text(raw.get("name"), "name"),
        "owner_org": required_text(raw.get("owner_org"), "owner_org"),
        "director_id": identifier(raw.get("director_id"), "director_id"),
        "tags": tag_list(raw.get("tags"), "tags"),
    }


def validate_condition(raw: Mapping[str, Any]) -> dict[str, Any]:
    condition_type = required_text(raw.get("condition_type"), "condition_type", 16)
    if condition_type not in CONDITION_TYPES:
        raise ValidationFailed("condition_type 必须是 evidence 或 budget")
    condition = {
        "condition_id": identifier(raw.get("condition_id"), "condition_id"),
        "condition_type": condition_type,
        "title": required_text(raw.get("title"), "title"),
        "responsible_dept": required_text(raw.get("responsible_dept"), "responsible_dept", 64),
        "authority": required_text(raw.get("authority"), "authority"),
        "rule_version": required_text(raw.get("rule_version", "v1"), "rule_version", 32),
        "threshold_cny": None,
        "evidence_spec_json": None,
        "applicability_json": None,
    }
    applicability = raw.get("applicability")
    if applicability is not None:
        if not isinstance(applicability, Mapping):
            raise ValidationFailed("applicability 必须是对象")
        required_tags = tag_list(applicability.get("required_tags"), "applicability.required_tags")
        excluded_tags = tag_list(applicability.get("excluded_tags"), "applicability.excluded_tags")
        if set(required_tags) & set(excluded_tags):
            raise ValidationFailed("同一标签不能同时出现在 required_tags 和 excluded_tags")
        condition["applicability_json"] = {"required_tags": required_tags, "excluded_tags": excluded_tags}
    if condition_type == "budget":
        condition["threshold_cny"] = str(
            money_value(raw.get("threshold_cny"), "threshold_cny", minimum=Decimal("0.01"))
        )
    else:
        spec = raw.get("evidence")
        if not isinstance(spec, Mapping):
            raise ValidationFailed("evidence 类型门槛必须提供 evidence 规格")
        accepted_sources = tag_list(spec.get("accepted_sources"), "evidence.accepted_sources")
        max_age_days = spec.get("max_age_days")
        if max_age_days is not None:
            max_age_days = positive_integer(max_age_days, "evidence.max_age_days")
        condition["evidence_spec_json"] = {
            "accepted_sources": accepted_sources,
            "max_age_days": max_age_days,
            "doc_kind": required_text(spec.get("doc_kind"), "evidence.doc_kind", 64),
        }
    return condition


def validate_gate(raw: Mapping[str, Any]) -> dict[str, Any]:
    order = positive_integer(raw.get("gate_order"), "gate_order")
    name = required_text(raw.get("name"), "name")
    decision_role = required_text(raw.get("decision_role"), "decision_role", 48)
    raw_conditions = raw.get("conditions", [])
    if not isinstance(raw_conditions, list) or not raw_conditions:
        raise ValidationFailed("阶段门至少包含一条门槛")
    conditions = [validate_condition(item) for item in raw_conditions]
    ids = [item["condition_id"] for item in conditions]
    if len(ids) != len(set(ids)):
        raise ValidationFailed("同一阶段门内 condition_id 不能重复")
    return {
        "gate_id": identifier(raw.get("gate_id"), "gate_id"),
        "gate_order": order,
        "name": name,
        "decision_role": decision_role,
        "stage_budget_cny": str(
            money_value(raw.get("stage_budget_cny"), "stage_budget_cny", minimum=Decimal("0"))
        ),
        "conditions": conditions,
    }


def validate_package(raw: Mapping[str, Any]) -> dict[str, Any]:
    requires = tag_list(raw.get("requires_conditions"), "requires_conditions")
    return {
        "package_id": identifier(raw.get("package_id"), "package_id"),
        "name": required_text(raw.get("name"), "name"),
        "gate_order": positive_integer(raw.get("gate_order"), "gate_order"),
        "requires_conditions": requires,
        "responsible_dept": required_text(raw.get("responsible_dept"), "responsible_dept", 64),
    }


def validate_evidence(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": identifier(raw.get("evidence_id"), "evidence_id"),
        "condition_id": identifier(raw.get("condition_id"), "condition_id"),
        "doc_number": required_text(raw.get("doc_number"), "doc_number", 96),
        "doc_kind": required_text(raw.get("doc_kind"), "doc_kind", 64),
        "source": required_text(raw.get("source"), "source", 96),
        "content_sha256": _sha256(raw.get("content_sha256")),
        "issued_at": iso_text(raw.get("issued_at"), "issued_at"),
        "submitted_by_dept": required_text(raw.get("submitted_by_dept"), "submitted_by_dept", 64),
    }


def _sha256(value: object) -> str:
    text = required_text(value, "content_sha256", 64)
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValidationFailed("content_sha256 必须是 64 位十六进制摘要")
    return text


def validate_commitment(raw: Mapping[str, Any]) -> dict[str, Any]:
    state = required_text(raw.get("state", "committed"), "state", 16)
    if state not in {"committed", "disbursed", "frozen", "withdrawn"}:
        raise ValidationFailed("资金承诺状态必须是 committed、disbursed、frozen 或 withdrawn")
    return {
        "commitment_id": identifier(raw.get("commitment_id"), "commitment_id"),
        "gate_order": positive_integer(raw.get("gate_order"), "gate_order"),
        "fund_source": required_text(raw.get("fund_source"), "fund_source", 96),
        "amount_cny": str(money_value(raw.get("amount_cny"), "amount_cny", minimum=Decimal("0.01"))),
        "state": state,
        "doc_number": required_text(raw.get("doc_number"), "doc_number", 96),
    }


def validate_override(raw: Mapping[str, Any]) -> dict[str, Any]:
    scope = required_text(raw.get("scope"), "scope", 16)
    if scope not in OVERRIDE_SCOPES:
        raise ValidationFailed("scope 必须是 condition 或 package")
    target = identifier(raw.get("target_id"), "target_id")
    reason = required_text(raw.get("reason"), "reason", 512)
    expires_at = iso_text(raw.get("expires_at"), "expires_at")
    bound = raw.get("boundary", {})
    if not isinstance(bound, Mapping):
        raise ValidationFailed("boundary 必须是对象")
    boundary = {
        "work_allowance": required_text(bound.get("work_allowance"), "boundary.work_allowance", 256),
        "max_amount_cny": str(
            money_value(bound.get("max_amount_cny", "0"), "boundary.max_amount_cny", minimum=Decimal("0"))
        ),
        "conditions": tag_list(bound.get("conditions"), "boundary.conditions"),
    }
    return {
        "override_id": identifier(raw.get("override_id"), "override_id"),
        "scope": scope,
        "target_id": target,
        "reason": reason,
        "expires_at": expires_at,
        "boundary": boundary,
    }
