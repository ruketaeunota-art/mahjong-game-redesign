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


if __name__ == "__main__":
    unittest.main()
