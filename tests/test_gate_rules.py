from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from project_gate.rules import (
    applicability_matches,
    evaluate_condition,
    evaluate_gate,
    gate_decision,
    impacted_packages,
)


NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


class RulesTests(unittest.TestCase):
    def test_applicability_required_and_excluded_tags(self) -> None:
        self.assertTrue(applicability_matches({"required_tags": ["dc"]}, ["dc", "major"]))
        self.assertFalse(applicability_matches({"required_tags": ["rail"]}, ["dc"]))
        self.assertTrue(applicability_matches({}, ["dc"]))
        self.assertFalse(applicability_matches({"excluded_tags": ["dc"]}, ["dc"]))

    def test_evidence_requires_accepted_source_fresh_and_fingerprint(self) -> None:
        condition = {
            "condition_id": "eia",
            "condition_type": "evidence",
            "evidence": {"accepted_sources": ["生态环境部门"], "max_age_days": 30},
        }
        fresh = [{
            "doc_number": "环审 1 号", "source": "生态环境部门",
            "content_sha256": "a" * 64, "issued_at": "2026-09-01T00:00:00Z",
        }]
        state, _ = evaluate_condition(condition, evidence=fresh, budget_allocated=Decimal("0"), now=NOW)
        self.assertEqual(state, "satisfied")

        stale = [{**fresh[0], "issued_at": "2025-01-01T00:00:00Z"}]
        state, reason = evaluate_condition(condition, evidence=stale, budget_allocated=Decimal("0"), now=NOW)
        self.assertEqual(state, "expired")
        self.assertIn("有效期", reason)

        wrong_source = [{**fresh[0], "source": "会议纪要"}]
        state, _ = evaluate_condition(condition, evidence=wrong_source, budget_allocated=Decimal("0"), now=NOW)
        self.assertEqual(state, "pending")

        no_fingerprint = [{**fresh[0], "content_sha256": ""}]
        state, _ = evaluate_condition(condition, evidence=no_fingerprint, budget_allocated=Decimal("0"), now=NOW)
        self.assertEqual(state, "pending")

    def test_budget_threshold(self) -> None:
        condition = {"condition_id": "funds", "condition_type": "budget", "threshold_cny": "500"}
        state, _ = evaluate_condition(condition, evidence=[], budget_allocated=Decimal("499.99"), now=NOW)
        self.assertEqual(state, "pending")
        state, _ = evaluate_condition(condition, evidence=[], budget_allocated=Decimal("500"), now=NOW)
        self.assertEqual(state, "satisfied")

    def test_gate_decision_requires_all_applicable(self) -> None:
        conditions = [
            {"condition_id": "a", "condition_type": "evidence",
             "evidence": {"accepted_sources": ["s"]}},
            {"condition_id": "b", "condition_type": "budget", "threshold_cny": "10"},
            {"condition_id": "c", "condition_type": "evidence",
             "evidence": {"accepted_sources": ["s"]},
             "applicability": {"required_tags": ["rail"]}},
        ]
        evidence = {"a": [{"doc_number": "d", "source": "s", "content_sha256": "x" * 64,
                           "issued_at": "2026-09-01T00:00:00Z"}]}
        results = evaluate_gate(
            conditions, ["dc"], evidence, budget_allocated=Decimal("10"), now=NOW,
        )
        states = {r["condition_id"]: r["state"] for r in results}
        self.assertEqual(states, {"a": "satisfied", "b": "satisfied", "c": "not_applicable"})
        decision, blockers = gate_decision(results)
        self.assertEqual(decision, "approved")
        self.assertEqual(blockers, [])

    def test_active_override_waives_blocker_expired_override_does_not(self) -> None:
        conditions = [{"condition_id": "a", "condition_type": "evidence",
                       "evidence": {"accepted_sources": ["s"]}}]
        results = evaluate_gate(
            conditions, [], {}, budget_allocated=Decimal("0"), now=NOW,
            overrides={"a": {"state": "active", "reason": "紧急",
                             "expires_at": "2026-09-25T00:00:00Z"}},
        )
        self.assertTrue(results[0]["waived"])
        self.assertEqual(gate_decision(results)[0], "approved")

        results = evaluate_gate(
            conditions, [], {}, budget_allocated=Decimal("0"), now=NOW,
            overrides={"a": {"state": "active", "reason": "紧急",
                             "expires_at": "2026-09-01T00:00:00Z"}},
        )
        self.assertFalse(results[0]["waived"])
        self.assertEqual(gate_decision(results)[0], "blocked")

    def test_impact_only_hits_unfinished_and_matched(self) -> None:
        packages = [
            {"package_id": "p1", "state": "planned", "gate_order": 1, "requires_conditions": ["a"]},
            {"package_id": "p2", "state": "in_progress", "gate_order": 1, "requires_conditions": []},
            {"package_id": "p3", "state": "completed", "gate_order": 1, "requires_conditions": ["a"]},
            {"package_id": "p4", "state": "planned", "gate_order": 2, "requires_conditions": ["z"]},
        ]
        hit = impacted_packages(packages, changed_condition_ids=["a"], gate_order=1)
        ids = {item["package_id"] for item in hit}
        self.assertEqual(ids, {"p1", "p2"})
        matched = next(item for item in hit if item["package_id"] == "p2")
        self.assertEqual(matched["matched_conditions"], [])  # 同门整门重核


if __name__ == "__main__":
    unittest.main()
