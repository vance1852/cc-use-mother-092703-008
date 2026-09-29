from __future__ import annotations

import unittest

from project_gate.errors import ValidationFailed
from project_gate.models import Evidence, Policy, Project, WorkPackage


def policy_payload(**overrides):
    payload = {
        "policy_id": "p1",
        "version": 1,
        "name": "门控规则",
        "effective_date": "2026-01-01",
        "stages": ["A", "B", "C"],
        "gates": [
            {"gate_code": "g1", "name": "用地", "kind": "LAND", "owner_dept": "NATRES",
             "applies": "REQUIRED", "evidence_kind": "DOCUMENT",
             "evidence_requirement": "用地批复", "valid_days": 365},
            {"gate_code": "g2", "name": "环评", "kind": "EIA", "owner_dept": "ECOENV",
             "applies": "EXEMPTIBLE", "evidence_kind": "DOCUMENT",
             "evidence_requirement": "环评批复", "valid_days": None},
        ],
        "stage_gates": {"B": ["g1"], "C": ["g1", "g2"]},
        "decision_authority": {"A": "approver", "B": "approver", "C": "approver:JOINT"},
    }
    payload.update(overrides)
    return payload


class PolicyContractTests(unittest.TestCase):
    def test_valid_policy(self) -> None:
        policy = Policy.from_dict(policy_payload())
        self.assertEqual(policy.stages, ("A", "B", "C"))
        self.assertEqual(policy.stage_gates["B"], ("g1",))

    def test_version_must_be_positive(self) -> None:
        with self.assertRaises(ValidationFailed):
            Policy.from_dict(policy_payload(version=0))

    def test_unknown_gate_kind_rejected(self) -> None:
        payload = policy_payload()
        payload["gates"][0]["kind"] = "WEATHER"
        with self.assertRaises(ValidationFailed):
            Policy.from_dict(payload)

    def test_stage_gate_reference_unknown(self) -> None:
        payload = policy_payload()
        payload["stage_gates"]["B"] = ["g9"]
        with self.assertRaises(ValidationFailed):
            Policy.from_dict(payload)

    def test_every_gate_must_bind_to_stage(self) -> None:
        payload = policy_payload()
        payload["stage_gates"] = {"B": ["g1"]}
        with self.assertRaises(ValidationFailed):
            Policy.from_dict(payload)

    def test_duplicate_stage_rejected(self) -> None:
        with self.assertRaises(ValidationFailed):
            Policy.from_dict(policy_payload(stages=["A", "A", "C"]))

    def test_decision_authority_required_for_every_stage(self) -> None:
        payload = policy_payload()
        del payload["decision_authority"]["C"]
        with self.assertRaises(ValidationFailed):
            Policy.from_dict(payload)

    def test_department_code_normalized(self) -> None:
        payload = policy_payload()
        payload["gates"][0]["owner_dept"] = "natres"
        self.assertEqual(Policy.from_dict(payload).gates[0].owner_dept, "NATRES")


class SimpleContractTests(unittest.TestCase):
    def test_project_requires_decimal_budget(self) -> None:
        with self.assertRaises(ValidationFailed):
            Project.from_dict({
                "project_id": "prj1", "name": "项目", "owner_org": "org",
                "manager_id": "m1", "policy_id": "p1", "initial_stage": "A",
                "budget_cny": "not-a-number",
            })

    def test_evidence_requires_iso_date(self) -> None:
        with self.assertRaises(ValidationFailed):
            Evidence.from_dict({
                "evidence_id": "e1", "project_id": "prj1", "stage": "B",
                "gate_code": "g1", "kind": "DOCUMENT", "title": "t",
                "document_ref": "d1", "issued_by": "x", "issued_at": "2026/09/01",
            })

    def test_work_package_rejects_unknown_department(self) -> None:
        with self.assertRaises(ValidationFailed):
            WorkPackage.from_dict({
                "package_id": "wp1", "project_id": "prj1", "stage": "B",
                "title": "t", "owner_dept": "UNKNOWN", "budget_cny": "1", "depends_on": [],
            })


if __name__ == "__main__":
    unittest.main()
