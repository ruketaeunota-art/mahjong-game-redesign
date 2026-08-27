from __future__ import annotations

import copy
import math
import unittest

import runner


def _entrants() -> list[dict[str, object]]:
    return [
        {
            "lane": lane,
            "racer_no": 4000 + lane,
            "racer_name": f"entrant-{lane}",
            "market_probability": {
                "1": 1.0 / 6.0,
                "2": 1.0 / 6.0,
                "3": 1.0 / 6.0,
            },
            "performance_probability": {
                "1": 1.0 / 6.0,
                "2": 1.0 / 6.0,
                "3": 1.0 / 6.0,
            },
        }
        for lane in range(1, 7)
    ]


class HandoffProbabilitiesTest(unittest.TestCase):
    def test_complete_six_by_three_probabilities_are_retained(self) -> None:
        result = runner.build_handoff_entrants(list(reversed(_entrants())))

        self.assertEqual([entrant["lane"] for entrant in result], list(range(1, 7)))
        for kind in ("market_probability", "performance_probability"):
            for position in ("1", "2", "3"):
                self.assertTrue(math.isclose(
                    sum(entrant[kind][position] for entrant in result),
                    1.0,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                ))
        self.assertEqual(
            result[0]["market_probability"],
            _entrants()[0]["market_probability"],
        )

    def test_missing_position_fails_closed(self) -> None:
        entrants = _entrants()
        del entrants[0]["market_probability"]["3"]
        with self.assertRaisesRegex(ValueError, "HANDOFF_PROBABILITY_INVALID"):
            runner.build_handoff_entrants(entrants)

    def test_negative_nan_and_boolean_probabilities_fail_closed(self) -> None:
        for invalid in (-0.1, float("nan"), True):
            with self.subTest(invalid=invalid):
                entrants = _entrants()
                entrants[0]["performance_probability"]["2"] = invalid
                with self.assertRaisesRegex(ValueError, "HANDOFF_PROBABILITY_INVALID"):
                    runner.build_handoff_entrants(entrants)

    def test_each_position_must_sum_to_one(self) -> None:
        entrants = _entrants()
        entrants[0]["market_probability"]["1"] = 0.2
        with self.assertRaisesRegex(ValueError, "HANDOFF_PROBABILITY_SUM_INVALID"):
            runner.build_handoff_entrants(entrants)

    def test_exactly_six_unique_lanes_are_required(self) -> None:
        entrants = _entrants()
        entrants[5]["lane"] = 5
        with self.assertRaisesRegex(ValueError, "HANDOFF_ENTRANTS_INVALID"):
            runner.build_handoff_entrants(entrants)

    def test_probability_objects_reject_extra_positions(self) -> None:
        entrants = copy.deepcopy(_entrants())
        entrants[0]["market_probability"]["4"] = 0.0
        with self.assertRaisesRegex(ValueError, "HANDOFF_PROBABILITY_INVALID"):
            runner.build_handoff_entrants(entrants)


if __name__ == "__main__":
    unittest.main()
