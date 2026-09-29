"""重大项目阶段门控服务的离线验收。

演示省市合作数据中心项目从立项到开工的门控全过程：
* 用地、环评、资金、设备交付门槛未满足时，阶段令拒绝签发（即使会议纪要称已开工）；
* 紧急例外限定范围、理由和到期时间，到期后未执行工作包自动暂停；
* 政策换版只影响尚未执行的工作包，历史阶段令快照保持不变；
* 证据失效暂停在建工作包，重新提交有效证据并满足恢复条件后方可复工。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import InvalidState
from .service import GateService


POLICY_V1 = {
    "policy_id": "province-city-dc",
    "version": 1,
    "name": "省市合作重大项目阶段门控规则",
    "effective_date": "2026-01-01",
    "stages": ["INIT", "PREPARATION", "CONSTRUCTION", "ACCEPTANCE"],
    "gates": [
        {"gate_code": "LAND_PRE", "name": "用地预审与选址意见书", "kind": "LAND",
         "owner_dept": "NATRES", "applies": "REQUIRED", "evidence_kind": "DOCUMENT",
         "evidence_requirement": "自然资源部门核发的用地预审与选址意见书扫描件", "valid_days": 730},
        {"gate_code": "FUND_PREP", "name": "筹备阶段资金承诺", "kind": "FUND",
         "owner_dept": "FINANCE", "applies": "REQUIRED", "evidence_kind": "DOCUMENT",
         "evidence_requirement": "财政资金承诺函", "valid_days": None},
        {"gate_code": "EIA_CONS", "name": "环境影响评价批复", "kind": "EIA",
         "owner_dept": "ECOENV", "applies": "REQUIRED", "evidence_kind": "DOCUMENT",
         "evidence_requirement": "生态环境部门环评批复文件", "valid_days": 365},
        {"gate_code": "FUND_CONS", "name": "建设阶段资金承诺", "kind": "FUND",
         "owner_dept": "FINANCE", "applies": "REQUIRED", "evidence_kind": "DOCUMENT",
         "evidence_requirement": "预算下达文件与配套资金承诺函", "valid_days": None},
        {"gate_code": "EQUIP_DELIVERY", "name": "关键设备交付计划确认", "kind": "EQUIPMENT",
         "owner_dept": "EQUIPMENT", "applies": "REQUIRED", "evidence_kind": "THIRD_PARTY",
         "evidence_requirement": "设备厂商到货承诺函与第三方履约担保", "valid_days": 180},
        {"gate_code": "FIRE_ACCEPT", "name": "消防验收意见", "kind": "PERMIT",
         "owner_dept": "INDUSTRY", "applies": "EXEMPTIBLE", "evidence_kind": "DOCUMENT",
         "evidence_requirement": "行业主管部门消防验收意见书", "valid_days": None},
    ],
    "stage_gates": {
        "PREPARATION": ["LAND_PRE", "FUND_PREP"],
        "CONSTRUCTION": ["EIA_CONS", "FUND_CONS", "EQUIP_DELIVERY"],
        "ACCEPTANCE": ["FIRE_ACCEPT"],
    },
    "decision_authority": {
        "INIT": "approver",
        "PREPARATION": "approver",
        "CONSTRUCTION": "approver:JOINT",
        "ACCEPTANCE": "approver:JOINT",
    },
}

POLICY_V2 = {
    **POLICY_V1,
    "version": 2,
    "effective_date": "2026-09-01",
    "gates": [
        *POLICY_V1["gates"],
        {"gate_code": "ENERGY_QUOTA", "name": "能耗指标确认", "kind": "POLICY",
         "owner_dept": "DRC", "applies": "REQUIRED", "evidence_kind": "SYSTEM_RECORD",
         "evidence_requirement": "发改部门能耗指标平台确认记录", "valid_days": None},
    ],
    "stage_gates": {
        **POLICY_V1["stage_gates"],
        "CONSTRUCTION": ["EIA_CONS", "FUND_CONS", "EQUIP_DELIVERY", "ENERGY_QUOTA"],
    },
}


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    start = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    clock = FrozenClock(start)
    service = GateService(connection, clock)

    users = (
        ("coord", "项目办协调人", "coordinator", None),
        ("natres", "自然资源经办人", "department", "NATRES"),
        ("ecoenv", "生态环境经办人", "department", "ECOENV"),
        ("finance", "财政经办人", "department", "FINANCE"),
        ("equipment", "设备业主经办人", "department", "EQUIPMENT"),
        ("drc", "发改经办人", "department", "DRC"),
        ("industry", "行业主管经办人", "department", "INDUSTRY"),
        ("joint", "省市联席批准人", "approver", "JOINT"),
        ("audit", "审计", "auditor", None),
    )
    for user_id, name, role, dept in users:
        service.create_user(user_id, name, role, dept)

    service.publish_policy("joint", POLICY_V1)
    project = service.create_project("coord", {
        "project_id": "dc-east-1", "name": "省市合作东部算力枢纽", "owner_org": "省发改委/市政府",
        "manager_id": "coord", "policy_id": "province-city-dc", "initial_stage": "INIT",
        "budget_cny": "1200000000",
    })

    for stage, amount in (("PREPARATION", "80000000"), ("CONSTRUCTION", "900000000"),
                          ("ACCEPTANCE", "220000000")):
        service.set_stage_budget("coord", {"project_id": "dc-east-1", "stage": stage,
                                           "allocated_cny": amount})
    service.record_commitment("finance", {"commitment_id": "c-prep", "project_id": "dc-east-1",
        "stage": "PREPARATION", "source": "市财政", "amount_cny": "80000000",
        "document_ref": "CZ-2026-PREP"})
    service.record_commitment("finance", {"commitment_id": "c-cons", "project_id": "dc-east-1",
        "stage": "CONSTRUCTION", "source": "省财政+专项债", "amount_cny": "900000000",
        "document_ref": "CZ-2026-CONS"})
    service.record_commitment("finance", {"commitment_id": "c-acc", "project_id": "dc-east-1",
        "stage": "ACCEPTANCE", "source": "市财政", "amount_cny": "220000000",
        "document_ref": "CZ-2026-ACC"})

    service.add_work_package("coord", {"package_id": "wp-survey", "project_id": "dc-east-1",
        "stage": "PREPARATION", "title": "场地勘察与临建", "owner_dept": "NATRES",
        "budget_cny": "20000000", "depends_on": []})
    service.add_work_package("coord", {"package_id": "wp-civil", "project_id": "dc-east-1",
        "stage": "CONSTRUCTION", "title": "土建施工", "owner_dept": "INDUSTRY",
        "budget_cny": "500000000", "depends_on": ["wp-survey"]})
    service.add_work_package("coord", {"package_id": "wp-power", "project_id": "dc-east-1",
        "stage": "CONSTRUCTION", "title": "供配电安装", "owner_dept": "EQUIPMENT",
        "budget_cny": "300000000", "depends_on": ["wp-civil"]})

    # 场景一：会议纪要称已开工，但用地、资金证据缺失——阶段令必须被拒绝。
    blocked = None
    try:
        service.issue_stage_order("joint", "dc-east-1", "key-prep-1")
    except InvalidState as exc:
        blocked = str(exc)
    status_before = service.project_status("coord", "dc-east-1")
    blocker_codes = [item["gate_code"] for item in status_before["blockers"]]
    # 用地、资金未满足时，工作包也不得开工。
    start_blocked = None
    try:
        service.start_package("natres", "wp-survey")
    except InvalidState as exc:
        start_blocked = str(exc)

    service.submit_evidence("natres", {"evidence_id": "ev-land", "project_id": "dc-east-1",
        "stage": "PREPARATION", "gate_code": "LAND_PRE", "kind": "DOCUMENT",
        "title": "用地预审与选址意见书", "document_ref": "ZR-2026-0091",
        "issued_by": "市自然资源和规划局", "issued_at": "2026-09-15"})
    service.submit_evidence("finance", {"evidence_id": "ev-fund-prep", "project_id": "dc-east-1",
        "stage": "PREPARATION", "gate_code": "FUND_PREP", "kind": "DOCUMENT",
        "title": "筹备资金承诺函", "document_ref": "CZ-2026-PREP",
        "issued_by": "市财政局", "issued_at": "2026-09-16"})
    prep_order = service.issue_stage_order("joint", "dc-east-1", "key-prep-1")
    # 幂等重放：同一键返回同一阶段令。
    replay = service.issue_stage_order("joint", "dc-east-1", "key-prep-1")
    service.start_package("natres", "wp-survey")
    service.complete_package("natres", "wp-survey")

    # 场景二：建设阶段——环评和建设资金齐备，设备交付临时无法满足，走紧急例外。
    service.submit_evidence("ecoenv", {"evidence_id": "ev-eia", "project_id": "dc-east-1",
        "stage": "CONSTRUCTION", "gate_code": "EIA_CONS", "kind": "DOCUMENT",
        "title": "环评批复", "document_ref": "HB-2026-0312",
        "issued_by": "省生态环境厅", "issued_at": "2026-09-18"})
    service.submit_evidence("finance", {"evidence_id": "ev-fund-cons", "project_id": "dc-east-1",
        "stage": "CONSTRUCTION", "gate_code": "FUND_CONS", "kind": "DOCUMENT",
        "title": "预算下达与配套资金承诺", "document_ref": "CZ-2026-CONS",
        "issued_by": "省财政厅", "issued_at": "2026-09-17"})
    missing_equipment = None
    try:
        service.issue_stage_order("joint", "dc-east-1", "key-cons-early")
    except InvalidState as exc:
        missing_equipment = str(exc)

    exception = service.grant_exception(
        "joint", "dc-east-1", "CONSTRUCTION", ["EQUIP_DELIVERY"],
        "首批机柜延期到货，土建与供配电须先行；设备承诺函预计 10 日内补交",
        (start + timedelta(days=10)).isoformat(),
        scope_packages=["wp-civil"], exception_id="exc-equip-1",
    )
    cons_order = service.issue_stage_order("joint", "dc-east-1", "key-cons-1")
    service.start_package("industry", "wp-civil")

    # 场景三：政策换版新增能耗指标门槛——历史阶段令不被改写，未来工作包出现新卡点。
    service.publish_policy("joint", POLICY_V2)
    service.apply_policy_version("coord", "dc-east-1", 2)
    status_v2 = service.project_status("coord", "dc-east-1")
    policy_impacts = [item for item in status_v2["open_impacts"] if item["type"] == "POLICY_VERSION"]
    # 历史阶段令仍保留 v1 快照。
    historical = [order for order in status_v2["stage_orders"] if order["to_stage"] == "PREPARATION"][0]

    # 场景四：例外到期——土建工作包暂停，登记恢复条件；补齐设备证据后恢复。
    clock.advance(days=11)
    swept = service.sweep_exception_expiry("joint", "dc-east-1")
    status_suspended = service.project_status("coord", "dc-east-1")
    civil = next(item for item in status_suspended["work_packages"] if item["package_id"] == "wp-civil")
    resume_before = None
    try:
        service.resume_package("coord", "wp-civil")
    except InvalidState as exc:
        resume_before = str(exc)
    service.submit_evidence("equipment", {"evidence_id": "ev-equip", "project_id": "dc-east-1",
        "stage": "CONSTRUCTION", "gate_code": "EQUIP_DELIVERY", "kind": "THIRD_PARTY",
        "title": "设备到货承诺与履约担保", "document_ref": "EQ-2026-1001",
        "issued_by": "设备厂商及担保银行", "issued_at": "2026-10-01"})
    # 能耗指标门槛仍按新版政策待补；由发改部门补系统记录后影响全部消除。
    service.submit_evidence("drc", {"evidence_id": "ev-energy", "project_id": "dc-east-1",
        "stage": "CONSTRUCTION", "gate_code": "ENERGY_QUOTA", "kind": "SYSTEM_RECORD",
        "title": "能耗指标平台确认", "document_ref": "FG-ENERGY-2026-77",
        "issued_by": "省发改委", "issued_at": "2026-10-01"})

    # 场景五：环评证据被宣布失效（批复被撤销）——在建土建立即暂停，历史阶段令仍可追溯。
    service.invalidate_evidence("joint", "ev-eia", "环评批复因公示异议被上级撤销，需补充评价后重报")
    status_invalid = service.project_status("coord", "dc-east-1")
    civil_after_invalidation = next(
        item for item in status_invalid["work_packages"] if item["package_id"] == "wp-civil"
    )
    # 设备门槛满足后例外影响已闭环；复工仍被环评失效影响拦截。
    resume_blocked_eia = None
    try:
        service.resume_package("coord", "wp-civil")
    except InvalidState as exc:
        resume_blocked_eia = str(exc)
    clock.advance(days=5)
    service.submit_evidence("ecoenv", {"evidence_id": "ev-eia-2", "project_id": "dc-east-1",
        "stage": "CONSTRUCTION", "gate_code": "EIA_CONS", "kind": "DOCUMENT",
        "title": "重新报批后的环评批复", "document_ref": "HB-2026-0418",
        "issued_by": "省生态环境厅", "issued_at": "2026-10-06"})
    resumed = service.resume_package("coord", "wp-civil")
    service.complete_package("industry", "wp-civil")

    # 场景六：竣工验收阶段的可豁免门槛，由批准人书面认定不适用。
    service.waive_gate("joint", "dc-east-1", "ACCEPTANCE", "FIRE_ACCEPT",
                       "该数据中心一期不含人员密集场所，按新规消防验收与竣工验收合并办理")
    acceptance_order = service.issue_stage_order("joint", "dc-east-1", "key-acc-1")

    final_status = service.project_status("coord", "dc-east-1")
    audit = service.audit_chain("audit")

    result = {
        "status": "ok",
        "workspace": workspace.resolve().name,
        "policy_version": final_status["policy"]["version"],
        "blocked_without_gates": blocked is not None,
        "initial_blocker_codes": blocker_codes,
        "package_start_blocked_before_order": start_blocked is not None,
        "preparation_order": prep_order["order_id"],
        "idempotent_replay": replay["order_id"] == prep_order["order_id"],
        "blocked_without_equipment_commitment": missing_equipment is not None,
        "exception": exception,
        "construction_order": {"order_id": cons_order["order_id"],
                               "exception_ids": cons_order["exception_ids"]},
        "policy_v2_open_impacts": len(policy_impacts),
        "historical_order_policy": {"policy_version": historical["policy_version"],
                                    "policy_sha256": historical["policy_sha256"]},
        "exception_swept": swept["expired"],
        "civil_suspended_on_expiry": civil["state"] == "suspended",
        "resume_blocked_until_recovered": resume_before is not None,
        "civil_suspended_on_evidence_invalidation": civil_after_invalidation["state"] == "suspended",
        "resume_blocked_by_eia_invalidation": resume_blocked_eia is not None,
        "resumed_after_new_eia": resumed["state"] == "executing",
        "acceptance_order": acceptance_order["order_id"],
        "current_stage": final_status["current_stage"],
        "open_impact_count": len(final_status["open_impacts"]),
        "stage_order_count": len(final_status["stage_orders"]),
        "audit": audit,
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
