from __future__ import annotations

import copy
import unittest
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import late_diff


JST = ZoneInfo("Asia/Tokyo")
TARGET = date(2026, 8, 27)
NOW = datetime(2026, 8, 27, 8, 0, tzinfo=JST)


def _signal(*, lane: int = 2, strength: float = 1.2, priority: str = "B") -> dict[str, Any]:
    return {
        "lane": lane,
        "direction": "value",
        "scope": "1",
        "priority": priority,
        "strength": strength,
        "ratio": 1.2,
    }


def _race(
    *,
    venue: int = 1,
    race_no: int = 1,
    deadline: str = "10:30:00",
    grade: str = "B",
    tickets: list[dict[str, Any]] | None = None,
    signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "venue_code": venue,
        "race_no": race_no,
        "event_day": 1,
        "event_phase": "REGULAR",
        "event_day_label": "初日",
        "deadline_time_jst": deadline,
        "race_grade": grade,
        "signals": [signal or _signal()],
        "tickets": tickets or [],
        "feature_degraded": True,
        "degradation_codes": ["MOTOR_RECENT5_UNAVAILABLE"],
    }


def _evaluations(
    races: list[dict[str, Any]] | None = None,
    *,
    stage: str = "LATE",
) -> list[dict[str, Any]]:
    return late_diff.build_full_evaluations(
        races or [_race()],
        target_date=TARGET,
        stage=stage,
        now=NOW,
    )


def _semantics(evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    return late_diff.build_legacy_semantics(
        evaluations,
        source_state="SOURCE_OK",
        history_state="DEGRADED_HISTORY",
        feature_state="FEATURE_DEGRADED",
    )


class _LegacyCursor:
    def __init__(
        self,
        *,
        baseline: tuple[Any, ...] | None,
        late_outbox: tuple[Any, ...] | None = None,
        current: tuple[Any, ...] | None = None,
        v2_available: bool = False,
    ) -> None:
        self.baseline = baseline
        self.late_outbox = late_outbox
        self.current = current
        self.v2_available = v2_available
        self.last_sql = ""
        self.calls: list[str] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.last_sql = sql
        self.calls.append(sql)

    def fetchone(self) -> Any:
        if "to_regprocedure" in self.last_sql:
            return (self.v2_available,)
        if "pg_advisory_xact_lock" in self.last_sql:
            return (None,)
        if "SELECT execution.target_date" in self.last_sql:
            return self.current
        if "owner_run.stage = 'LATE'" in self.last_sql:
            return self.late_outbox
        if "owner_run.stage = 'EARLY'" in self.last_sql:
            return self.baseline
        raise AssertionError(f"unexpected SQL: {self.last_sql}")


def _baseline(semantic: dict[str, Any]) -> tuple[Any, ...]:
    return (
        10,
        20,
        "ACCEPTED",
        "a" * 64,
        late_diff.legacy_semantic_detail_fields(semantic),
    )


class FullEvaluationBuilderTest(unittest.TestCase):
    def test_full_day_is_sorted_and_stage_actionability_is_explicit(self) -> None:
        evaluations = _evaluations([
            _race(venue=2, race_no=2, deadline="11:00:00"),
            _race(venue=1, race_no=1, deadline="09:00:00"),
        ])
        self.assertEqual(
            [(row["venue_code"], row["race_no"]) for row in evaluations],
            [(1, 1), (2, 2)],
        )
        self.assertFalse(evaluations[0]["actionable"])
        self.assertTrue(evaluations[1]["actionable"])
        for row in evaluations:
            self.assertEqual(row["event_day"], 1)
            self.assertEqual(row["event_phase"], "REGULAR")
            self.assertEqual(row["event_day_label"], "初日")
            self.assertFalse(row["actual_odds_evaluated"])
            self.assertEqual(row["ev_status"], "EV_UNASSESSED")
            self.assertEqual(row["purchase_decision"], "SKIP")

    def test_b_reason_and_strongest_signal_are_reconstructible(self) -> None:
        row = _evaluations()[0]
        self.assertEqual(row["morning_grade"], "B")
        self.assertEqual(row["max_signal_strength"], 1.2)
        self.assertIn("SA_THRESHOLD_NOT_MET", row["reason_codes"])
        self.assertEqual(row["strongest_signal"]["lane"], 2)
        self.assertEqual(row["ticket_combinations"], [])

    def test_ticketless_s_a_remains_morning_judgment_not_purchase_buy(self) -> None:
        row = _evaluations([
            _race(
                grade="A",
                signal=_signal(strength=2.0, priority="A"),
                tickets=[],
            )
        ])[0]
        self.assertEqual(row["morning_grade"], "A")
        self.assertEqual(row["ticket_combinations"], [])
        self.assertEqual(row["purchase_decision"], "SKIP")
        self.assertFalse(row["actual_odds_evaluated"])
        self.assertEqual(
            _semantics([row])["snapshot"]["late_rows"][0]["decision_state"],
            "MORNING_CANDIDATE",
        )

    def test_invalid_duplicates_nonfinite_and_ticket_shapes_fail_closed(self) -> None:
        cases = []
        duplicate = [_race(), _race()]
        cases.append(duplicate)
        nonfinite = [_race(signal=_signal(strength=float("nan")))]
        cases.append(nonfinite)
        invalid_ticket = [_race(
            grade="A",
            signal=_signal(strength=2.0, priority="A"),
            tickets=[{"combo": "1-1-2"}],
        )]
        cases.append(invalid_ticket)
        for races in cases:
            with self.subTest(races=races):
                with self.assertRaises((TypeError, ValueError)):
                    _evaluations(races)

    def test_event_context_is_nullable_but_invalid_values_fail_closed(self) -> None:
        unknown = _race()
        unknown.update({
            "event_day": None,
            "event_phase": None,
            "event_day_label": None,
        })
        row = _evaluations([unknown])[0]
        self.assertIsNone(row["event_day"])
        self.assertIsNone(row["event_phase"])
        self.assertIsNone(row["event_day_label"])
        for field, invalid in (
            ("event_day", 13),
            ("event_day", True),
            ("event_phase", "QUALIFYING"),
            ("event_day_label", " 最終日"),
        ):
            race = _race()
            race[field] = invalid
            with self.subTest(field=field, invalid=invalid):
                with self.assertRaises(ValueError):
                    _evaluations([race])


class LegacySemanticTest(unittest.TestCase):
    def test_technical_strength_change_is_ignored_when_identity_and_grade_hold(self) -> None:
        first = _semantics(_evaluations())
        changed = _race(signal=_signal(strength=1.3))
        second = _semantics(_evaluations([changed]))
        self.assertEqual(first["sha256"], second["sha256"])

    def test_material_grade_target_reason_and_signal_identity_changes_differ(self) -> None:
        original = _semantics(_evaluations())
        variants = [
            [_race(grade="A", signal=_signal(strength=2.0, priority="A"))],
            [_race(race_no=2)],
            [_race(signal=_signal(lane=3))],
        ]
        reason_change = _evaluations()
        reason_change[0]["degradation_codes"].append("FEATURE_CHANGED")
        for evaluations in [_evaluations(variant) for variant in variants] + [reason_change]:
            with self.subTest(evaluations=evaluations):
                self.assertNotEqual(original["sha256"], _semantics(evaluations)["sha256"])

    def test_identical_zero_candidate_late_is_suppressed_without_outbox(self) -> None:
        semantic = _semantics(_evaluations())
        cursor = _LegacyCursor(baseline=_baseline(semantic))
        result = late_diff.evaluate_legacy_preterminal_suppression(
            cursor,
            run_execution_id=11,
            target_date=TARGET,
            stage="LATE",
            semantic_result=semantic,
        )
        self.assertTrue(late_diff.validate_legacy_suppression_result(result))
        self.assertIsNone(result["outbox_id"])
        self.assertEqual(result["status"], "SUPPRESSED")
        self.assertTrue(any(
            "pg_advisory_xact_lock" in sql for sql in cursor.calls
        ))

    def test_any_unchanged_s_a_fails_open_even_without_ticket(self) -> None:
        semantic = _semantics(_evaluations([
            _race(
                grade="A",
                signal=_signal(strength=2.0, priority="A"),
                tickets=[],
            )
        ]))
        cursor = _LegacyCursor(baseline=_baseline(semantic))
        result = late_diff.evaluate_legacy_preterminal_suppression(
            cursor,
            run_execution_id=11,
            target_date=TARGET,
            stage="LATE",
            semantic_result=semantic,
        )
        self.assertFalse(result["suppressed"])
        self.assertEqual(
            result["reason_code"],
            "BASELINE_CANDIDATE_NOT_PREVIOUSLY_PRESENTED",
        )

    def test_missing_baseline_and_existing_late_outbox_fail_open(self) -> None:
        semantic = _semantics(_evaluations())
        missing = _LegacyCursor(baseline=None)
        result = late_diff.evaluate_legacy_preterminal_suppression(
            missing,
            run_execution_id=11,
            target_date=TARGET,
            stage="LATE",
            semantic_result=semantic,
        )
        self.assertEqual(result["reason_code"], "ACCEPTED_EARLY_BASELINE_MISSING")

        existing = _LegacyCursor(baseline=_baseline(semantic), late_outbox=(99,))
        result = late_diff.evaluate_legacy_preterminal_suppression(
            existing,
            run_execution_id=11,
            target_date=TARGET,
            stage="LATE",
            semantic_result=semantic,
        )
        self.assertEqual(result["reason_code"], "LATE_OUTBOX_EXISTS")

    def test_exact_terminal_replay_suppresses_again_without_outbox(self) -> None:
        semantic = _semantics(_evaluations())
        details = late_diff.legacy_semantic_detail_fields(semantic)
        cursor = _LegacyCursor(
            baseline=_baseline(semantic),
            current=(TARGET, "LATE", "SUCCEEDED", details),
        )
        result = late_diff.evaluate_legacy_terminal_suppression(cursor, 11)
        self.assertTrue(result["suppressed"])
        self.assertIsNone(result["outbox_id"])

    def test_changed_after_suppressed_fails_open_as_material_change(self) -> None:
        early = _semantics(_evaluations())
        changed = _semantics(_evaluations([_race(race_no=2)]))
        cursor = _LegacyCursor(baseline=_baseline(early))
        result = late_diff.evaluate_legacy_preterminal_suppression(
            cursor,
            run_execution_id=11,
            target_date=TARGET,
            stage="LATE",
            semantic_result=changed,
        )
        self.assertEqual(result["reason_code"], "MATERIAL_DECISION_CHANGE")

    def test_capability_requires_both_v2_functions(self) -> None:
        self.assertTrue(late_diff.v2_rpc_available(_LegacyCursor(
            baseline=None,
            v2_available=True,
        )))
        self.assertFalse(late_diff.v2_rpc_available(_LegacyCursor(
            baseline=None,
            v2_available=False,
        )))


class V2SuppressionEnvelopeTest(unittest.TestCase):
    def test_exact_v2_suppression_is_valid(self) -> None:
        result = {
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
        self.assertTrue(late_diff.validate_suppression_result(
            result,
            expected_run_execution_id=11,
        ))
        forged = copy.deepcopy(result)
        forged["outbox_id"] = 999
        self.assertFalse(late_diff.validate_suppression_result(forged))

    def test_v2_stage_replay_after_no_outbox_suppression_is_valid(self) -> None:
        result = {
            "schema_version": "boatrace-morning-digest-outbox-result-v4",
            "run_execution_id": 12,
            "decision_id": 31,
            "outbox_id": None,
            "status": "SUPPRESSED",
            "inserted": True,
            "deduplicated": True,
            "replayed": False,
            "suppressed": True,
            "delivery_required": False,
            "canonical_delivery_pending": False,
            "decision_reason_code": "STAGE_ALREADY_FINALIZED",
            "semantic_policy_id": "morning-late-diff-v1",
            "canonical_semantic_sha256": "d" * 64,
            "baseline_run_execution_id": None,
            "baseline_outbox_id": None,
            "change_kinds": [],
        }
        self.assertTrue(late_diff.validate_suppression_result(result))

    def test_v2_status_rpc_replay_of_genuine_suppression_is_valid(self) -> None:
        result = {
            "schema_version": "boatrace-morning-digest-outbox-result-v4",
            "run_execution_id": 11,
            "decision_id": 30,
            "outbox_id": None,
            "status": "SUPPRESSED",
            "inserted": False,
            "deduplicated": True,
            "replayed": True,
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
        self.assertTrue(late_diff.validate_suppression_result(
            result,
            expected_run_execution_id=11,
        ))


if __name__ == "__main__":
    unittest.main()
