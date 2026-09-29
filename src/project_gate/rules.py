"""阶段门控的纯领域规则：预算、门槛状态、适用性与影响分析。

本模块不访问数据库，所有输入都是普通字典，便于单独测试。
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Sequence


def money(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("金额必须是十进制数值")
    try:
        result = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise ValueError("金额必须是十进制数值") from exc
    if not result.is_finite():
        raise ValueError("金额必须是有限数值")
    return result


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money_text(value: Decimal) -> str:
    return format(quantize_money(value), "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def applicability_matches(applicability: Mapping[str, Any], tags: Sequence[str]) -> bool:
    """门槛是否适用于给定项目标签。

    tags 为空表示项目无标签；只包含受标签约束的门槛视为不适用。
    """
    required = applicability.get("required_tags", [])
    if required:
        tag_set = set(tags)
        if not all(tag in tag_set for tag in required):
            return False
    excluded = applicability.get("excluded_tags", [])
    if excluded and any(tag in set(tags) for tag in excluded):
        return False
    return True


def evaluate_condition(
    condition: Mapping[str, Any],
    *,
    evidence: Sequence[Mapping[str, Any]],
    budget_allocated: Decimal,
    now: datetime,
) -> tuple[str, str]:
    """评估单条门槛，返回 (state, reason)。

    state 取值：satisfied / pending / expired / not_applicable
    - 不适用门槛（applicability）在调用前过滤，本函数只处理适用门槛。
    - condition_type=evidence：需有通过核验的有效证据；
      证据存在但超过证据有效期 -> expired。
    - condition_type=budget：阶段预算承诺金额达到要求阈值。
    """
    condition_type = condition["condition_type"]
    if condition_type == "budget":
        threshold = money(condition.get("threshold_cny", "0"))
        if budget_allocated + Decimal("0.001") >= threshold:
            return "satisfied", "资金承诺已落实"
        return "pending", f"资金承诺 {money_text(budget_allocated)} 未达门槛 {money_text(threshold)}"
    if condition_type != "evidence":
        return "pending", f"未知条件类型 {condition_type}"

    evidence_spec = condition.get("evidence", {}) or {}
    accepted_sources = set(evidence_spec.get("accepted_sources", []))
    max_age_days = evidence_spec.get("max_age_days")
    valid_documents: list[Mapping[str, Any]] = []
    saw_expired = False
    for document in evidence:
        if accepted_sources and document.get("source") not in accepted_sources:
            continue
        if not str(document.get("doc_number", "")).strip() or not str(document.get("content_sha256", "")).strip():
            continue
        issued_raw = document.get("issued_at")
        if max_age_days is not None and issued_raw:
            try:
                issued_at = datetime.fromisoformat(str(issued_raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if issued_at.tzinfo is None:
                continue
            age_days = (now - issued_at.astimezone(now.tzinfo)).total_seconds() / 86400
            if age_days > int(max_age_days):
                saw_expired = True
                continue
        valid_documents.append(document)
    if valid_documents:
        return "satisfied", "有效证据齐备"
    if saw_expired:
        return "expired", "证据存在但已超过有效期，需重新提交"
    return "pending", "缺少有效证据"


def evaluate_gate(
    conditions: Sequence[Mapping[str, Any]],
    project_tags: Sequence[str],
    evidence_by_condition: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    budget_allocated: Decimal,
    now: datetime,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """评估一个阶段门的全部门槛，输出可追溯的逐项结果。"""
    overrides = overrides or {}
    results: list[dict[str, Any]] = []
    for condition in conditions:
        condition_id = condition["condition_id"]
        if not applicability_matches(condition.get("applicability", {}) or {}, project_tags):
            results.append(
                {
                    "condition_id": condition_id,
                    "state": "not_applicable",
                    "reason": "门槛不适用于本项目标签",
                    "waived": False,
                }
            )
            continue
        state, reason = evaluate_condition(
            condition,
            evidence=evidence_by_condition.get(condition_id, []),
            budget_allocated=budget_allocated,
            now=now,
        )
        waived = False
        override = overrides.get(condition_id)
        if override is not None and state != "satisfied":
            if _override_active(override, now):
                waived = True
                reason = f"紧急例外覆盖（{override['reason']}），到期 {override['expires_at']}"
            else:
                reason = f"紧急例外已到期：{reason}"
        results.append(
            {
                "condition_id": condition_id,
                "state": state,
                "reason": reason,
                "waived": waived,
                "responsible_dept": condition.get("responsible_dept"),
                "authority": condition.get("authority"),
            }
        )
    return results


def _override_active(override: Mapping[str, Any], now: datetime) -> bool:
    try:
        expires_at = datetime.fromisoformat(str(override["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False
    if expires_at.tzinfo is None:
        return False
    return now < expires_at.astimezone(now.tzinfo) and override.get("state") == "active"


def gate_decision(results: Iterable[Mapping[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """所有适用门槛满足（或被有效例外覆盖）才放行；返回 (decision, blockers)。"""
    blockers = [
        dict(item)
        for item in results
        if item["state"] != "satisfied" and not item.get("waived") and item["state"] != "not_applicable"
    ]
    return ("approved" if not blockers else "blocked", blockers)


def budget_ledger(commitments: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """汇总阶段预算承诺：已承诺、已支出（拨付）、剩余可用。"""
    committed = sum((money(item.get("amount_cny", 0)) for item in commitments), Decimal("0"))
    disbursed = sum(
        (money(item.get("amount_cny", 0)) for item in commitments if item.get("state") == "disbursed"),
        Decimal("0"),
    )
    committed = quantize_money(committed)
    disbursed = quantize_money(disbursed)
    return {
        "committed_cny": money_text(committed),
        "disbursed_cny": money_text(disbursed),
        "available_cny": money_text(committed - disbursed),
    }


def impacted_packages(
    packages: Sequence[Mapping[str, Any]],
    *,
    changed_condition_ids: Sequence[str],
    gate_order: int,
) -> list[dict[str, Any]]:
    """规则换版/条件失效时，找出尚未执行且受影响的工作包。

    只影响「尚未执行」的工作包（状态不属于 completed/cancelled）。命中条件：
    - 工作包显式引用了变更门槛（requires_conditions 与变更集合相交）；或
    - 工作包位于发生变更的阶段门（整门门槛需重新核验）。
    已经发生的阶段批准不在此函数内改动；是否被已签发阶段令保护由服务层标注。
    """
    changed = set(changed_condition_ids)
    impacted: list[dict[str, Any]] = []
    finished = {"completed", "cancelled"}
    for package in packages:
        if package.get("state") in finished:
            continue
        requires = set(package.get("requires_conditions", []))
        matched = sorted(requires & changed)
        same_gate = int(package.get("gate_order", -1)) == gate_order
        if matched or same_gate:
            impacted.append(
                {
                    "package_id": package["package_id"],
                    "state": package.get("state"),
                    "matched_conditions": matched,
                    "gate_order": package.get("gate_order"),
                }
            )
    return impacted
