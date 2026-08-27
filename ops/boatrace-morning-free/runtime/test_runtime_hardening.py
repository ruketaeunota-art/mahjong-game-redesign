from __future__ import annotations

import unittest
from datetime import date

import delivery_gate
import stage_guard
import stage_lock


class RuntimeHardeningTest(unittest.TestCase):
    def test_stage_lock_identity_is_stable_and_stage_specific(self) -> None:
        target = date(2026, 8, 26)
        early = stage_lock.lock_identity(target, "EARLY")
        late = stage_lock.lock_identity(target, "LATE")
        self.assertEqual(
            early,
            "boatrace-morning-stage-lock-v1:2026-08-26:EARLY",
        )
        self.assertNotEqual(early, late)

    def test_delivery_gate_requires_v2_when_skip_details_exist(self) -> None:
        with self.assertRaises(RuntimeError):
            delivery_gate._validate_accepted(
                {
                    "outbox_id": 1,
                    "payload_sha256": "a" * 64,
                    "presentation": "MORNING_CARD_V1",
                    "ui_version": "MORNING_USER_CARD_V2",
                },
                expected_skip_count=1,
            )

        delivery_gate._validate_accepted(
            {
                "outbox_id": 1,
                "payload_sha256": "a" * 64,
                "presentation": "MORNING_CARD_V2",
                "ui_version": "MORNING_USER_CARD_V3",
            },
            expected_skip_count=1,
        )

    def test_stage_guard_allows_retry_after_failure(self) -> None:
        self.assertIn("latest.lifecycle_status = 'SUCCEEDED'", stage_guard._STAGE_STARTED_SQL)
        self.assertIn("latest.lifecycle_status = 'RUNNING'", stage_guard._STAGE_STARTED_SQL)
        self.assertNotIn("latest.lifecycle_status = 'FAILED'", stage_guard._STAGE_STARTED_SQL)

    def test_stage_guard_has_no_credential_schema_probe(self) -> None:
        self.assertFalse(hasattr(stage_guard, "_CREDENTIAL_SCHEMA_SQL"))
        self.assertFalse(hasattr(stage_guard, "credential_schema_matches"))

    def test_delivery_gate_accepts_only_sealed_nullable_suppression(self) -> None:
        suppression = {
            "schema_version": "boatrace-morning-digest-outbox-result-v4",
            "run_execution_id": 11,
            "decision_id": 30,
            "outbox_id": None,
            "status": "SUPPRESSED",
            "inserted": True,
            "deduplicated": True,
            "replayed": False,
            "suppressed": True,
            "delivery_required": False,
            "canonical_delivery_pending": False,
            "decision_reason_code": "LATE_SUPPRESSED_NO_MATERIAL_CHANGE",
            "semantic_policy_id": "morning-late-diff-v1",
            "canonical_semantic_sha256": "b" * 64,
            "baseline_run_execution_id": 10,
            "baseline_outbox_id": 20,
            "change_kinds": [],
        }
        delivery_gate._validate_suppressed(
            suppression,
            expected_run_execution_id=11,
        )
        suppression["outbox_id"] = 99
        with self.assertRaises(RuntimeError):
            delivery_gate._validate_suppressed(
                suppression,
                expected_run_execution_id=11,
            )

    def test_delivery_gate_v2_status_keeps_canonical_outbox_identity(self) -> None:
        status = {
            "schema_version": "boatrace-morning-digest-outbox-result-v4",
            "run_execution_id": 11,
            "decision_id": 30,
            "outbox_id": 40,
            "suppressed": False,
            "semantic_policy_id": "morning-late-diff-v1",
            "canonical_semantic_sha256": "c" * 64,
        }
        delivery_gate._validate_v2_status_identity(
            status,
            expected_run_execution_id=11,
        )
        status["run_execution_id"] = 12
        with self.assertRaises(RuntimeError):
            delivery_gate._validate_v2_status_identity(
                status,
                expected_run_execution_id=11,
            )

    def test_finalized_stage_reuses_outbox_without_new_delivery(self) -> None:
        status = {
            "decision_reason_code": "STAGE_ALREADY_FINALIZED",
            "outbox_id": 40,
            "status": "ACCEPTED",
            "deduplicated": True,
            "replayed": True,
            "delivery_required": False,
        }
        self.assertEqual(
            delivery_gate._accepted_gate_status(status, legacy_run=False),
            "PASS_DEDUPLICATED",
        )
        status["delivery_required"] = True
        with self.assertRaises(RuntimeError):
            delivery_gate._accepted_gate_status(status, legacy_run=False)


if __name__ == "__main__":
    unittest.main()
