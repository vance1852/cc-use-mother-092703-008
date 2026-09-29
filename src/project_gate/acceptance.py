"""重大项目阶段门控服务的离线验收。

场景：省市合作的数据中心项目进入建设高峰。开工门需要用地预审、环评批复、
资金承诺；设备进场门需要建筑工程施工许可、设备交付确认。验收覆盖：
- 会议纪要宣称已开工但关键许可缺失时，阶段令被拦截；
- 证据核验与资金承诺齐备后才签发阶段令（含授权依据快照与哈希）；
- 规则换版（新增节能审查）只影响尚未执行的工作包，历史阶段令保留；
- 紧急例外必须有范围、理由、到期时间，到期自动失效；
- 项目负责人看板给出卡点、授权依据、资金承诺和后续恢复条件。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import GatingBlocked
from .service import GateService


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    start = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    clock = FrozenClock(start)
    service = GateService(connection, clock)

    # 用户与角色
    for user_id, role, dept in (
        ("plan", "planner", "发改部门"),
        ("pm", "director", "项目办"),
        ("risk", "risk", "风险合规"),
        ("fin", "finance", "财政部门"),
        ("audit", "auditor", "审计"),
        ("land", "department", "自然资源部门"),
        ("env", "department", "生态环境部门"),
        ("build", "department", "住建部门"),
    ):
        service.create_user(user_id, user_id, role, dept)

    project_id = "prov-city-dc"
    service.create_project("pm", {
        "project_id": project_id,
        "name": "省市合作智算中心项目",
        "owner_org": "省发改委与市大数据局",
        "director_id": "pm",
        "tags": ["major", "data-center"],
    })

    # 第 1 阶段门：开工
    service.publish_gate("plan", {
        "gate_id": "gate-start-v1",
        "gate_order": 1,
        "name": "开工门",
        "decision_role": "director",
        "stage_budget_cny": "500000000",
        "conditions": [
            {
                "condition_id": "land-preapproval",
                "condition_type": "evidence",
                "title": "建设项目用地预审与选址意见书",
                "responsible_dept": "自然资源部门",
                "authority": "《土地管理法实施条例》及自然资源部用地预审办法",
                "rule_version": "NR-2024-1",
                "evidence": {
                    "doc_kind": "用地预审与选址意见书",
                    "accepted_sources": ["自然资源部门"],
                    "max_age_days": 365,
                },
            },
            {
                "condition_id": "eia-approval",
                "condition_type": "evidence",
                "title": "环境影响评价批复",
                "responsible_dept": "生态环境部门",
                "authority": "《环境影响评价法》第 25 条",
                "rule_version": "EIA-2024-2",
                "evidence": {"doc_kind": "环评批复文件", "accepted_sources": ["生态环境部门"]},
                "applicability": {"required_tags": ["data-center"]},
            },
            {
                "condition_id": "start-funds",
                "condition_type": "budget",
                "title": "开工阶段资金承诺",
                "responsible_dept": "财政部门",
                "authority": "省市合作资金框架协议第 4 条",
                "rule_version": "FIN-2025-1",
                "threshold_cny": "500000000",
            },
        ],
    })

    # 第 2 阶段门：设备进场与安装
    service.publish_gate("plan", {
        "gate_id": "gate-equip-v1",
        "gate_order": 2,
        "name": "设备进场门",
        "decision_role": "director",
        "stage_budget_cny": "800000000",
        "conditions": [
            {
                "condition_id": "construction-permit",
                "condition_type": "evidence",
                "title": "建筑工程施工许可证",
                "responsible_dept": "住建部门",
                "authority": "《建筑法》第 8 条",
                "rule_version": "MOHURD-2023-1",
                "evidence": {"doc_kind": "施工许可证", "accepted_sources": ["住建部门"]},
            },
            {
                "condition_id": "equipment-delivery",
                "condition_type": "evidence",
                "title": "关键设备到货交付确认",
                "responsible_dept": "设备供应商",
                "authority": "设备采购合同 DC-EQ-2026-07 第 9 条",
                "rule_version": "PO-2026-1",
                "evidence": {"doc_kind": "设备交付确认单", "accepted_sources": ["设备供应商", "监理单位"]},
            },
            {
                "condition_id": "equip-funds",
                "condition_type": "budget",
                "title": "设备阶段资金承诺",
                "responsible_dept": "财政部门",
                "authority": "省市合作资金框架协议第 5 条",
                "rule_version": "FIN-2025-1",
                "threshold_cny": "800000000",
            },
        ],
    })

    # 工作包
    for package in (
        {"package_id": "wp-site", "name": "场地平整", "gate_order": 1,
         "requires_conditions": ["land-preapproval"], "responsible_dept": "施工总包"},
        {"package_id": "wp-foundation", "name": "主体基础施工", "gate_order": 1,
         "requires_conditions": ["land-preapproval", "eia-approval", "start-funds"],
         "responsible_dept": "施工总包"},
        {"package_id": "wp-equipment", "name": "变配电与制冷设备安装", "gate_order": 2,
         "requires_conditions": ["construction-permit", "equipment-delivery"],
         "responsible_dept": "设备安装单位"},
        {"package_id": "wp-finished", "name": "围挡搭设（已完成）", "gate_order": 1,
         "requires_conditions": ["land-preapproval"], "responsible_dept": "施工总包"},
    ):
        service.add_package("pm", project_id, package)

    # 场景一：会议纪要宣称已开工，但环评批复仍在补办 → 阶段令必须被拦截
    service.submit_evidence("land", project_id, {
        "evidence_id": "ev-land-1", "condition_id": "land-preapproval",
        "doc_number": "自然资预〔2026〕12 号", "doc_kind": "用地预审与选址意见书",
        "source": "自然资源部门", "content_sha256": _hash("land-doc"),
        "issued_at": "2026-08-01T00:00:00Z", "submitted_by_dept": "自然资源部门",
    })
    service.review_evidence("risk", project_id, "ev-land-1", True, "文号可查，受理")
    service.record_commitment("fin", project_id, {
        "commitment_id": "fund-1", "gate_order": 1, "fund_source": "省级专项",
        "amount_cny": "300000000", "state": "committed", "doc_number": "财预〔2026〕8 号",
    })
    blocked_attempt = None
    try:
        service.issue_order("pm", project_id, 1, note="会议纪要称已开工，尝试补签")
    except GatingBlocked as exc:
        blocked_attempt = {"code": exc.code, "details": exc.details}
    assert blocked_attempt is not None, "缺门槛时阶段令不应签发"
    blocker_ids = {b["condition_id"] for b in blocked_attempt["details"]["blockers"]}
    assert blocker_ids == {"eia-approval", "start-funds"}, blocker_ids

    # 无阶段令时工作包不得推进（即便现场已经动土）
    try:
        service.advance_package("pm", project_id, "wp-foundation", "ready", "现场已开挖，补办许可中")
        raised = False
    except GatingBlocked:
        raised = True
    assert raised

    # 场景二：补齐环评与资金 → 签发
    service.submit_evidence("env", project_id, {
        "evidence_id": "ev-eia-1", "condition_id": "eia-approval",
        "doc_number": "环审〔2026〕33 号", "doc_kind": "环评批复文件",
        "source": "生态环境部门", "content_sha256": _hash("eia-doc"),
        "issued_at": "2026-09-10T00:00:00Z", "submitted_by_dept": "生态环境部门",
    })
    service.review_evidence("risk", project_id, "ev-eia-1", True, "批复在有效期内")
    service.record_commitment("fin", project_id, {
        "commitment_id": "fund-2", "gate_order": 1, "fund_source": "市级配套",
        "amount_cny": "200000000", "state": "committed", "doc_number": "财城〔2026〕15 号",
    })
    status_before = service.gate_status("pm", project_id, 1)
    assert status_before["decision"] == "approved"
    order1 = service.issue_order("pm", project_id, 1, note="开工门槛全部满足")
    assert order1["gate_version"] == 1 and len(order1["basis_sha256"]) == 64
    # 重复签发必须被拒绝（历史批准不可变）
    try:
        service.issue_order("pm", project_id, 1)
        duplicated = False
    except Exception:
        duplicated = True
    assert duplicated

    service.advance_package("pm", project_id, "wp-site", "ready", "用地手续齐备")
    service.advance_package("pm", project_id, "wp-foundation", "ready", "基础施工具备条件")
    service.advance_package("pm", project_id, "wp-foundation", "in_progress", "基础施工开始")
    service.advance_package("pm", project_id, "wp-finished", "ready")
    service.advance_package("pm", project_id, "wp-finished", "in_progress")
    service.advance_package("pm", project_id, "wp-finished", "completed")

    # 场景三：政策规则换版——开工门新增节能审查门槛（v2）
    revised = service.revise_gate("plan", 1, {
        "gate_id": "gate-start-v2",
        "gate_order": 1,
        "name": "开工门",
        "decision_role": "director",
        "stage_budget_cny": "500000000",
        "conditions": [
            {
                "condition_id": "land-preapproval",
                "condition_type": "evidence",
                "title": "建设项目用地预审与选址意见书",
                "responsible_dept": "自然资源部门",
                "authority": "《土地管理法实施条例》",
                "rule_version": "NR-2024-1",
                "evidence": {"doc_kind": "用地预审与选址意见书", "accepted_sources": ["自然资源部门"]},
            },
            {
                "condition_id": "eia-approval",
                "condition_type": "evidence",
                "title": "环境影响评价批复",
                "responsible_dept": "生态环境部门",
                "authority": "《环境影响评价法》第 25 条",
                "rule_version": "EIA-2024-2",
                "evidence": {"doc_kind": "环评批复文件", "accepted_sources": ["生态环境部门"]},
                "applicability": {"required_tags": ["data-center"]},
            },
            {
                "condition_id": "energy-review",
                "condition_type": "evidence",
                "title": "固定资产投资项目节能审查意见",
                "responsible_dept": "发改部门",
                "authority": "《节约能源法》及节能审查办法（2026 修订）",
                "rule_version": "ENERGY-2026-1",
                "evidence": {"doc_kind": "节能审查意见", "accepted_sources": ["发改部门"]},
            },
            {
                "condition_id": "start-funds",
                "condition_type": "budget",
                "title": "开工阶段资金承诺",
                "responsible_dept": "财政部门",
                "authority": "省市合作资金框架协议第 4 条",
                "rule_version": "FIN-2025-1",
                "threshold_cny": "500000000",
            },
        ],
    })
    impact = service.assess_rule_change(
        "risk", project_id, "rule_revised", ["energy-review"], 1,
        note="节能审查办法换版，新增开工门门槛",
    )
    impacted_ids = {p["package_id"] for p in impact["impacted_packages"]}
    # 已完成的围挡包不再受影响；wp-foundation 在阶段令保护下标记不回滚
    assert "wp-finished" not in impacted_ids
    foundation = next(p for p in impact["impacted_packages"] if p["package_id"] == "wp-foundation")
    assert foundation["protected_by_order"] is True
    # 历史阶段令原封不动，仍是 v1 依据
    historical = connection.execute(
        "SELECT gate_id,gate_version FROM stage_orders WHERE project_id=? AND gate_order=1",
        (project_id,),
    ).fetchone()
    assert historical["gate_id"] == "gate-start-v1" and historical["gate_version"] == 1

    # 场景四：紧急例外（范围、理由、到期时间明确）
    clock.advance(days=20)
    expires = (clock.now() + timedelta(days=7)).isoformat().replace("+00:00", "Z")
    service.grant_override("risk", project_id, {
        "override_id": "ov-eq-1",
        "scope": "package",
        "target_id": "wp-equipment",
        "reason": "省政府要求的算力节点联调节点不可推迟，施工许可承诺 7 日内补齐",
        "expires_at": expires,
        "boundary": {
            "work_allowance": "仅限 2 号机房变配电柜就位，不得通电调试",
            "max_amount_cny": "3000000",
            "conditions": ["construction-permit"],
        },
    })
    service.advance_package("pm", project_id, "wp-equipment", "ready", "执行省级联调节点（例外边界内）")
    early = service.advance_package("pm", project_id, "wp-equipment", "in_progress", "执行省级联调节点")
    assert early["state"] == "in_progress"  # 例外边界内先行
    # 条件级例外：环评文件到期补办期间，明确豁免并到期
    waiver_expires = (clock.now() + timedelta(days=3)).isoformat().replace("+00:00", "Z")
    service.grant_override("risk", project_id, {
        "override_id": "ov-eia-1",
        "scope": "condition",
        "target_id": "eia-approval",
        "reason": "审批系统迁移导致换证延迟，已取得受理回执",
        "expires_at": waiver_expires,
        "boundary": {"work_allowance": "不新增土建作业面", "max_amount_cny": "0", "conditions": []},
    })
    service.revoke_evidence("env", project_id, "ev-eia-1", "审批系统迁移，旧证收回换发")
    waived_status = service.gate_status("pm", project_id, 1)
    waived_eia = next(c for c in waived_status["conditions"] if c["condition_id"] == "eia-approval")
    assert waived_eia["waived"] is True and "紧急例外覆盖" in waived_eia["reason"]
    clock.advance(days=4)  # 条件级例外到期
    expired_status = service.gate_status("pm", project_id, 1)
    eia = next(c for c in expired_status["conditions"] if c["condition_id"] == "eia-approval")
    assert "紧急例外已到期" in eia["reason"] and eia["waived"] is False

    # 场景五：项目负责人看板
    dashboard = service.dashboard("pm", project_id)
    blocker_keys = {(b["gate_order"], b["condition_id"]) for b in dashboard["current_blockers"]}
    reopened_keys = {(b["gate_order"], b["condition_id"]) for b in dashboard["reopened_alerts"]}
    assert (1, "energy-review") in reopened_keys  # 已签发门换版后的新卡点作为预警可见
    assert (2, "construction-permit") in blocker_keys  # 未签发门的卡点在当前卡点
    recovery = {(r["gate_order"], r["condition_id"]): r for r in dashboard["recovery_conditions"]}
    assert recovery[(1, "energy-review")]["responsible_dept"] == "发改部门"
    assert recovery[(1, "energy-review")]["authority"].startswith("《节约能源法》")
    assert recovery[(1, "energy-review")]["actions"], "恢复条件必须给出补交动作"
    committed_total = sum(
        int(c["amount_cny"]) for c in dashboard["commitments"] if c["gate_order"] == 1 and c["state"] != "withdrawn"
    )
    assert committed_total == 500_000_000

    audit = service.audit_chain("audit")
    assert audit["valid"] and audit["events"] >= 15

    result = {
        "status": "ok",
        "project": project_id,
        "blocked_first_attempt": blocked_attempt,
        "order_1": order1,
        "revised_gate": revised,
        "rule_change_impact": impact,
        "emergency_advance": early,
        "expired_waiver_condition": eia,
        "dashboard_summary": {
            "current_blockers": dashboard["current_blockers"],
            "reopened_alerts": dashboard["reopened_alerts"],
            "recovery_count": len(dashboard["recovery_conditions"]),
            "overrides": [{"id": o["override_id"], "state": o["state"]} for o in dashboard["overrides"]],
            "commitment_count": len(dashboard["commitments"]),
        },
        "audit": audit,
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行重大项目阶段门控服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
