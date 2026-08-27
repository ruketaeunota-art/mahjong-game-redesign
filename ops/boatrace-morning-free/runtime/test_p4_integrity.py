from __future__ import annotations

import copy
import inspect
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import production
import runner


class _FakeResponse:
    def __init__(self, body: bytes, text: str) -> None:
        self.content = body
        self.text = text
        self.apparent_encoding = "shift_jis"
        self.encoding: str | None = None

    def raise_for_status(self) -> None:
        return None


class P4IntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        runner.reset_source_manifest()

    def test_missing_comment_is_neutral_for_confidence_and_strength(self) -> None:
        base = {
            "entrants": [{
                "lane": 1,
                "comment_missing": True,
                "motor_recent5_missing": False,
                "evidence_confidence": 1.0,
            }],
            "signals": [{"lane": 1, "strength": 2.0, "priority": "A"}],
            "race_grade": "A",
        }
        with patch.object(production, "_original_score", return_value=copy.deepcopy(base)):
            missing = production.corrected_score_race({}, {}, {})
        base["entrants"][0]["comment_missing"] = False
        with patch.object(production, "_original_score", return_value=copy.deepcopy(base)):
            present = production.corrected_score_race({}, {}, {})

        self.assertEqual(missing["entrants"][0]["evidence_confidence"], 1.0)
        self.assertEqual(missing["signals"][0]["strength"], 2.0)
        self.assertEqual(
            missing["entrants"][0]["evidence_confidence"],
            present["entrants"][0]["evidence_confidence"],
        )
        self.assertEqual(
            missing["signals"][0]["strength"],
            present["signals"][0]["strength"],
        )

    def test_missing_motor_still_reduces_confidence(self) -> None:
        base = {
            "entrants": [{
                "lane": 1,
                "comment_missing": True,
                "motor_recent5_missing": True,
                "evidence_confidence": 0.75,
            }],
            "signals": [{"lane": 1, "strength": 2.0, "priority": "A"}],
            "race_grade": "A",
        }
        with patch.object(production, "_original_score", return_value=copy.deepcopy(base)):
            result = production.corrected_score_race({}, {}, {})
        self.assertEqual(result["entrants"][0]["evidence_confidence"], 0.75)
        self.assertEqual(result["signals"][0]["strength"], 2.0)

    def test_discover_records_index_fetch_failures(self) -> None:
        target = date(2026, 8, 27)

        def fake_get(url: str) -> str:
            if "jcd=02" in url:
                raise TimeoutError("bounded timeout")
            if "jcd=01" in url:
                return "eligible"
            return "ineligible"

        failures: list[str] = []
        with (
            patch.object(runner, "get", side_effect=fake_get),
            patch.object(
                runner,
                "event_context_from_text",
                side_effect=lambda html, _: (
                    runner.MeetingContext(1, "REGULAR", "初日")
                    if html == "eligible"
                    else None
                ),
            ),
        ):
            venues = runner.discover(target, failures)
        self.assertEqual(
            venues,
            [(1, runner.MeetingContext(1, "REGULAR", "初日"))],
        )
        self.assertEqual(failures, ["02-INDEX:TimeoutError:bounded timeout"])

    def test_production_acquire_resets_manifest_and_propagates_failure(self) -> None:
        runner._record_source_artifact(
            "https://www.boatrace.jp/stale",
            b"stale",
            retrieved_at_utc=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )

        def fake_discover(
            _: date,
            failures: list[str],
        ) -> list[tuple[int, runner.MeetingContext]]:
            failures.append("02-INDEX:TimeoutError:bounded timeout")
            return []

        with patch.object(runner, "discover", side_effect=fake_discover):
            races, failures = production.production_acquire(date(2026, 8, 27))
        self.assertEqual(races, [])
        self.assertEqual(failures, ["02-INDEX:TimeoutError:bounded timeout"])
        self.assertEqual(runner.source_manifest()["artifacts"], [])

    def test_exact_response_bytes_and_retrieval_time_are_recorded(self) -> None:
        url = "https://www.boatrace.jp/example"
        raw = b"\x82\xa0official-bytes"
        response = _FakeResponse(raw, "official text")
        fixed = datetime(2026, 8, 27, 0, 1, 2, tzinfo=timezone.utc)
        with (
            patch.object(runner.requests, "get", return_value=response),
            patch.object(runner, "datetime", wraps=datetime) as clock,
        ):
            clock.now.return_value = fixed
            self.assertEqual(runner.get(url), "official text")
        self.assertEqual(runner.source_manifest()["artifacts"], [{
            "source_url": url,
            "content_sha256": runner.sha(raw),
            "byte_length": len(raw),
            "retrieved_at_utc": "2026-08-27T00:01:02+00:00",
        }])

    def test_same_url_same_bytes_preserves_first_clock(self) -> None:
        url = "https://www.boatrace.jp/example"
        first = datetime(2026, 8, 27, 0, 1, tzinfo=timezone.utc)
        later = datetime(2026, 8, 27, 0, 2, tzinfo=timezone.utc)
        runner._record_source_artifact(url, b"same", retrieved_at_utc=first)
        runner._record_source_artifact(url, b"same", retrieved_at_utc=later)
        self.assertEqual(
            runner.source_manifest()["artifacts"][0]["retrieved_at_utc"],
            first.isoformat(),
        )

    def test_changed_bytes_and_naive_clock_fail_closed(self) -> None:
        url = "https://www.boatrace.jp/example"
        runner._record_source_artifact(
            url,
            b"first",
            retrieved_at_utc=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(RuntimeError, "SOURCE_BYTES_CHANGED_DURING_RUN"):
            runner._record_source_artifact(
                url,
                b"changed",
                retrieved_at_utc=datetime(2026, 8, 27, tzinfo=timezone.utc),
            )
        runner.reset_source_manifest()
        with self.assertRaisesRegex(ValueError, "SOURCE_RETRIEVAL_TIME_MUST_BE_AWARE"):
            runner._record_source_artifact(
                url,
                b"first",
                retrieved_at_utc=datetime(2026, 8, 27),
            )

    def test_empty_manifest_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "SOURCE_MANIFEST_EMPTY"):
            runner.source_manifest_sha256()

    def test_audit_overlay_uses_source_and_scorer_artifact_hashes(self) -> None:
        import audit_overlay

        source = inspect.getsource(audit_overlay.persist_with_skip_audit)
        self.assertIn("runner.source_manifest_sha256()", source)
        self.assertIn("boatrace-morning-scorer-output-manifest-v1", source)
        self.assertNotIn("runner.sha(runner.SOURCE_REF.encode())", source)


if __name__ == "__main__":
    unittest.main()
