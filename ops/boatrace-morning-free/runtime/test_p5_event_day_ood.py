from __future__ import annotations

import re
import unittest
from datetime import date, datetime
from unittest import mock

import audit_overlay
import late_diff
import production  # noqa: F401  # installs production scoring/parser overlays
import runner


TARGET = date(2026, 8, 27)


def _race(*, event_day: int | None, event_phase: str = "REGULAR") -> dict:
    return {
        "venue_code": 1,
        "race_no": 7,
        "event_day": event_day,
        "event_phase": event_phase,
        "event_day_label": "最終日" if event_day is None else f"{event_day}日目",
        "deadline_time_jst": "11:30:00",
        "source_url": "https://www.boatrace.jp/official-fixture",
        "entrants": [{"lane": lane} for lane in range(1, 7)],
    }


class _ConfigMustNotBeRead(dict):
    def __getitem__(self, key):  # type: ignore[override]
        raise AssertionError(f"out-of-domain scorer read config key: {key}")


class _PersistenceCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self._next: tuple[bool] | None = None

    def execute(self, sql: str, params: object = None) -> None:
        self.calls.append((sql, params))
        if "to_regprocedure" in sql:
            self._next = (True,)

    def fetchone(self) -> tuple[bool] | None:
        result, self._next = self._next, None
        return result


class EventContextParserTest(unittest.TestCase):
    def test_parses_numeric_day_three_from_target_date_scope(self) -> None:
        html = """
        <div>2026年8月26日 2日目</div>
        <div>xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx</div>
        <section>2026年8月27日 第３日目 一般戦</section>
        """
        context = runner.event_context_from_text(html, TARGET)
        self.assertEqual(context, runner.MeetingContext(3, "REGULAR", "3日目"))

    def test_parses_days_one_through_twelve(self) -> None:
        for event_day in range(1, 13):
            context = runner.event_context_from_text(
                f"<main>2026年8月27日 第{event_day}日目</main>",
                TARGET,
            )
            with self.subTest(event_day=event_day):
                expected_label = "初日" if event_day == 1 else f"{event_day}日目"
                self.assertEqual(
                    context,
                    runner.MeetingContext(event_day, "REGULAR", expected_label),
                )

    def test_parses_official_day_without_me_suffix_and_kanji_day(self) -> None:
        numeric = runner.event_context_from_text(
            "<main>2026年8月27日 第3日</main>", TARGET
        )
        kanji = runner.event_context_from_text(
            "<main>2026年8月27日 第五日目 準優勝戦</main>", TARGET
        )
        self.assertEqual(numeric, runner.MeetingContext(3, "REGULAR", "3日目"))
        self.assertEqual(kanji, runner.MeetingContext(5, "REGULAR", "5日目"))

    def test_future_final_tab_does_not_reclassify_target_day(self) -> None:
        html = """
        <nav>
          8月25日初日 8月26日２日目 8月27日３日目
          8月28日４日目 8月29日５日目 8月30日最終日
        </nav>
        <main>準優勝戦へ向けた案内</main>
        """
        context = runner.event_context_from_text(html, TARGET)
        self.assertEqual(context, runner.MeetingContext(3, "REGULAR", "3日目"))

    def test_parses_semifinal_and_final_without_inventing_day_number(self) -> None:
        semifinal = runner.event_context_from_text(
            "<main>8月27日 準優勝戦</main>", TARGET
        )
        final = runner.event_context_from_text(
            "<main>2026/08/27 最終日 優勝戦</main>", TARGET
        )
        self.assertEqual(
            semifinal,
            runner.MeetingContext(None, "SEMIFINAL", "準優勝戦日"),
        )
        self.assertEqual(final, runner.MeetingContext(None, "FINAL", "最終日"))

    def test_requires_the_requested_date(self) -> None:
        self.assertIsNone(
            runner.event_context_from_text(
                "<main>2026年8月26日 3日目 準優勝戦</main>", TARGET
            )
        )

    def test_discover_keeps_day_three_and_final_meetings(self) -> None:
        def fake_get(url: str) -> str:
            venue = int(re.search(r"jcd=(\d{2})", url).group(1))
            if venue == 1:
                return "<main>2026年8月27日 3日目</main>"
            if venue == 2:
                return "<main>2026年8月27日 最終日 優勝戦</main>"
            return "<main>2026年8月27日 開催なし</main>"

        with mock.patch.object(runner, "get", side_effect=fake_get):
            discovered = runner.discover(TARGET, [])

        self.assertEqual(
            discovered,
            [
                (1, runner.MeetingContext(3, "REGULAR", "3日目")),
                (2, runner.MeetingContext(None, "FINAL", "最終日")),
            ],
        )


class OodScoringTest(unittest.TestCase):
    def test_day_three_skips_without_reading_frozen_model(self) -> None:
        scored = runner.score_race(
            _race(event_day=3),
            _ConfigMustNotBeRead(),
            _ConfigMustNotBeRead(),
        )
        self.assertEqual(scored["race_grade"], "B")
        self.assertEqual(scored["decision_state"], "SKIP")
        self.assertEqual(scored["purchase_decision"], "SKIP")
        self.assertEqual(scored["signals"], [])
        self.assertEqual(scored["tickets"], [])
        self.assertEqual(scored["reason_codes"], [runner.MODEL_OOD_EVENT_DAY])
        self.assertEqual(
            scored["degradation_codes"], [runner.MODEL_OOD_EVENT_DAY]
        )

    def test_semifinal_is_ood_even_when_nominally_day_two(self) -> None:
        scored = runner.score_race(
            _race(event_day=2, event_phase="SEMIFINAL"),
            _ConfigMustNotBeRead(),
            _ConfigMustNotBeRead(),
        )
        self.assertEqual(scored["model_applicability"], "OOD_EVENT_DAY")
        self.assertEqual(scored["race_grade"], "B")
        self.assertFalse(scored["tickets"])

    def test_missing_or_boolean_event_day_fails_closed(self) -> None:
        self.assertFalse(runner.frozen_model_event_eligible(_race(event_day=None)))
        self.assertFalse(runner.frozen_model_event_eligible(_race(event_day=True)))

    def test_p2_evaluation_retains_context_ood_reason_and_zero_tickets(self) -> None:
        scored = runner.score_race(
            _race(event_day=5, event_phase="SEMIFINAL"),
            _ConfigMustNotBeRead(),
            _ConfigMustNotBeRead(),
        )
        evaluations = late_diff.build_full_evaluations(
            [scored],
            target_date=TARGET,
            stage="LATE",
            now=datetime(2026, 8, 27, 8, 30, tzinfo=runner.JST),
        )
        self.assertEqual(len(evaluations), 1)
        evaluation = evaluations[0]
        self.assertEqual(evaluation["event_day"], 5)
        self.assertEqual(evaluation["event_phase"], "SEMIFINAL")
        self.assertEqual(evaluation["event_day_label"], "5日目")
        self.assertEqual(evaluation["morning_grade"], "B")
        self.assertEqual(evaluation["purchase_decision"], "SKIP")
        self.assertIn(runner.MODEL_OOD_EVENT_DAY, evaluation["reason_codes"])
        self.assertIn(
            runner.MODEL_OOD_EVENT_DAY, evaluation["degradation_codes"]
        )
        self.assertEqual(evaluation["ticket_combinations"], [])

    def test_skip_card_audit_retains_ood_reason(self) -> None:
        scored = runner.score_race(
            _race(event_day=None, event_phase="FINAL"),
            _ConfigMustNotBeRead(),
            _ConfigMustNotBeRead(),
        )
        record = audit_overlay._skip_record(
            scored,
            datetime(2026, 8, 27, 11, 30, tzinfo=runner.JST),
        )
        self.assertIn(runner.MODEL_OOD_EVENT_DAY, record["reason_codes"])
        self.assertIn(
            runner.MODEL_OOD_EVENT_DAY, record["degradation_codes"]
        )

    def test_persistence_keeps_audit_row_input_but_builds_no_handoff(self) -> None:
        scored = runner.score_race(
            _race(event_day=6, event_phase="FINAL"),
            _ConfigMustNotBeRead(),
            _ConfigMustNotBeRead(),
        )
        runner.reset_source_manifest()
        runner._record_source_artifact(
            "https://www.boatrace.jp/official-fixture",
            b"official-program-bytes",
            retrieved_at_utc=datetime.fromisoformat("2026-08-26T23:30:00+00:00"),
        )
        cursor = _PersistenceCursor()
        result = audit_overlay.persist_with_skip_audit(
            cursor,
            123,
            TARGET,
            "LATE",
            [scored],
            [],
            datetime(2026, 8, 27, 8, 30, tzinfo=runner.JST),
        )

        sql = "\n".join(call[0] for call in cursor.calls)
        self.assertNotIn("purchase_assist.handoffs", sql)
        self.assertNotIn("ux_app.race_projections", sql)
        self.assertEqual(result["handoff"], 0)
        self.assertEqual(len(result["_morning_evaluations"]), 1)


if __name__ == "__main__":
    unittest.main()
