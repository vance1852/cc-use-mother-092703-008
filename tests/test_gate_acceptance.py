from __future__ import annotations

import unittest
from pathlib import Path

from project_gate.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class GateAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        # 缺关键许可时阶段令被拦截
        blocked = {b["condition_id"] for b in result["blocked_first_attempt"]["details"]["blockers"]}
        self.assertEqual(blocked, {"eia-approval", "start-funds"})
        # 历史阶段令保留为 v1，换版产生 v2
        self.assertEqual(result["order_1"]["gate_version"], 1)
        self.assertEqual(result["revised_gate"]["version"], 2)
        # 已完成工作包不受影响，在执行的包受阶段令保护、不回滚
        impacted = {p["package_id"]: p for p in result["rule_change_impact"]["impacted_packages"]}
        self.assertNotIn("wp-finished", impacted)
        self.assertTrue(impacted["wp-foundation"]["protected_by_order"])
        # 紧急例外到期自动失效
        self.assertFalse(result["expired_waiver_condition"]["waived"])
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
