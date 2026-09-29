from __future__ import annotations

import unittest
from pathlib import Path

from project_gate.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class GateAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        # 无证据时阶段令与工作包开工都必须被拦截。
        self.assertTrue(result["blocked_without_gates"])
        self.assertEqual(set(result["initial_blocker_codes"]), {"LAND_PRE", "FUND_PREP"})
        self.assertTrue(result["package_start_blocked_before_order"])
        # 设备交付承诺缺失时不得签发建设阶段令。
        self.assertTrue(result["blocked_without_equipment_commitment"])
        # 紧急例外有明确范围与到期时间，阶段令记录了授权依据。
        self.assertEqual(result["construction_order"]["exception_ids"], ["exc-equip-1"])
        # 政策换版产生未来工作包影响，但历史阶段令保留 v1 快照。
        self.assertEqual(result["policy_version"], 2)
        self.assertEqual(result["historical_order_policy"]["policy_version"], 1)
        self.assertEqual(len(result["historical_order_policy"]["policy_sha256"]), 64)
        # 例外到期暂停在建工作包，补齐恢复条件后才能复工。
        self.assertEqual(result["exception_swept"], ["exc-equip-1"])
        self.assertTrue(result["civil_suspended_on_expiry"])
        self.assertTrue(result["resume_blocked_until_recovered"])
        # 环评失效立即暂停；新批复满足恢复条件后复工。
        self.assertTrue(result["civil_suspended_on_evidence_invalidation"])
        self.assertTrue(result["resume_blocked_by_eia_invalidation"])
        self.assertTrue(result["resumed_after_new_eia"])
        # 最终所有影响闭环，阶段令与审计哈希链完整。
        self.assertEqual(result["current_stage"], "ACCEPTANCE")
        self.assertEqual(result["open_impact_count"], 0)
        self.assertEqual(result["stage_order_count"], 3)
        self.assertTrue(result["idempotent_replay"])
        self.assertTrue(result["audit"]["valid"])
        self.assertGreater(result["audit"]["events"], 0)


if __name__ == "__main__":
    unittest.main()
