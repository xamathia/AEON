import copy
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import unittest

from packages.aeon_engine import simulate_day
from packages.aeon_engine.engine import _random_for


ROOT = Path(__file__).resolve().parents[2]


def source(reference, provider="user", assumption=""):
    return {
        "provider": provider,
        "reference": reference,
        "synthetic": True,
        "assumption": assumption,
    }


def distribution(minimum=0, mode=0, maximum=0):
    return {"min": minimum, "mode": mode, "max": maximum}


def event(event_id, start, end, overrun=None):
    return {
        "id": event_id,
        "title": event_id,
        "planned_start": start,
        "planned_end": end,
        "classification": "fixed",
        "private": False,
        "overrun_minutes": overrun or distribution(),
        "source": source(f"event:{event_id}", "google_calendar"),
    }


def scenario(events, travel_edges=None, constraints=None, samples=5, seed=7):
    return {
        "schema_version": "1.0",
        "scenario_id": "test-scenario",
        "mode": "synthetic",
        "timezone": "Europe/Paris",
        "horizon": {
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-02T00:00:00Z",
        },
        "seed": seed,
        "samples": samples,
        "events": events,
        "travel_edges": travel_edges or [],
        "constraints": constraints or [],
    }


def edge(left, right, duration=None):
    return {
        "from_event_id": left,
        "to_event_id": right,
        "duration_minutes": duration or distribution(),
        "source": source(f"travel:{left}:{right}", "google_routes"),
    }


class SimulationTests(unittest.TestCase):
    def test_constant_distributions_are_calculable_and_propagate(self):
        payload = scenario(
            [
                event(
                    "first",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:30:00Z",
                    distribution(10, 10, 10),
                ),
                event(
                    "second",
                    "2026-01-01T00:45:00Z",
                    "2026-01-01T01:15:00Z",
                ),
            ],
            [edge("first", "second", distribution(10, 10, 10))],
        )
        result = simulate_day(payload)
        first, second = result["events"]
        self.assertEqual("2026-01-01T00:40:00Z", first["end"]["p50"])
        self.assertEqual("2026-01-01T00:50:00Z", second["arrival"]["p50"])
        self.assertEqual("2026-01-01T00:50:00Z", second["start"]["p50"])
        self.assertEqual({"p10": 5.0, "p50": 5.0, "p90": 5.0}, second["delay_minutes"])
        self.assertEqual(1.0, second["late_arrival_probability"])
        self.assertEqual(1.0, result["summary"]["probability_any_late"])
        self.assertEqual(5.0, result["summary"]["expected_total_delay_minutes"])

    def test_arrival_before_deadline_waits_for_start_and_is_not_late(self):
        payload = scenario(
            [
                event("first", "2026-01-01T00:00:00Z", "2026-01-01T00:30:00Z"),
                event("second", "2026-01-01T01:00:00Z", "2026-01-01T01:30:00Z"),
            ],
            [edge("first", "second", distribution(20, 20, 20))],
            [
                {
                    "id": "arrival",
                    "type": "arrival_deadline",
                    "event_id": "second",
                    "deadline": "2026-01-01T00:55:00Z",
                    "source": source("gmail:arrival", "gmail"),
                }
            ],
        )
        second = simulate_day(payload)["events"][1]
        self.assertEqual("2026-01-01T00:50:00Z", second["arrival"]["p50"])
        self.assertEqual("2026-01-01T01:00:00Z", second["start"]["p50"])
        self.assertEqual(0.0, second["late_arrival_probability"])
        self.assertEqual([], second["reason_codes"])

    def test_probability_of_any_late_is_not_sum_of_event_probabilities(self):
        payload = scenario(
            [
                event("a", "2026-01-01T00:00:00Z", "2026-01-01T00:30:00Z"),
                event("b", "2026-01-01T00:30:00Z", "2026-01-01T01:00:00Z"),
                event("c", "2026-01-01T01:00:00Z", "2026-01-01T01:30:00Z"),
            ],
            [
                edge("a", "b", distribution(5, 5, 5)),
                edge("b", "c", distribution(5, 5, 5)),
            ],
        )
        result = simulate_day(payload)
        self.assertEqual([0.0, 1.0, 1.0], [item["late_arrival_probability"] for item in result["events"]])
        self.assertEqual(1.0, result["summary"]["probability_any_late"])

    def test_same_seed_is_reproducible_without_input_mutation(self):
        payload = json.loads((ROOT / "fixtures/demo/day-v1.json").read_text())
        original = copy.deepcopy(payload)
        first = simulate_day(payload)
        second = simulate_day(payload)
        self.assertEqual(first, second)
        self.assertEqual(original, payload)
        self.assertEqual(0, first["llm_calls"])
        self.assertEqual(1000, first["samples"])

    def test_stable_seed_uses_compact_json_and_full_sha256_digest(self):
        identifier = ["from", "to"]
        encoded = json.dumps(
            [7, "travel", identifier],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        expected = random.Random(
            int.from_bytes(hashlib.sha256(encoded).digest(), "big")
        ).random()
        self.assertEqual(expected, _random_for(7, "travel", identifier).random())

    def test_quantiles_probabilities_sources_and_reasons_follow_contract(self):
        payload = json.loads((ROOT / "fixtures/demo/day-v1.json").read_text())
        result = simulate_day(payload)
        dinner = result["events"][2]
        for field in ("arrival", "start", "end", "delay_minutes"):
            self.assertLessEqual(dinner[field]["p10"], dinner[field]["p50"])
            self.assertLessEqual(dinner[field]["p50"], dinner[field]["p90"])
        self.assertGreater(dinner["late_arrival_probability"], 0)
        self.assertLessEqual(dinner["late_arrival_probability"], 1)
        self.assertEqual(
            ["DEADLINE_AT_RISK", "INSUFFICIENT_TRAVEL_BUFFER", "MEETING_OVERRUN_LIKELY"],
            dinner["reason_codes"],
        )
        references = [item["reference"] for item in result["sources"]]
        self.assertEqual(sorted(references), references)
        self.assertIn("synthetic:gmail:reservation-update", dinner["evidence_refs"])
        self.assertEqual(sorted(result["assumptions"]), result["assumptions"])

    def test_user_arrival_preference_is_not_reported_as_gmail_deadline_risk(self):
        payload = json.loads((ROOT / "fixtures/demo/day-v1.json").read_text())
        constraint_source = payload["constraints"][0]["source"]
        constraint_source["provider"] = "user"
        constraint_source["reference"] = "user:arrival-preference"

        dinner = simulate_day(payload)["events"][2]

        self.assertGreater(dinner["late_arrival_probability"], 0)
        self.assertNotIn("DEADLINE_AT_RISK", dinner["reason_codes"])
        self.assertIn("user:arrival-preference", dinner["evidence_refs"])

    def test_multiple_deadlines_use_the_earliest(self):
        payload = scenario(
            [event("a", "2026-01-01T01:00:00Z", "2026-01-01T01:30:00Z")],
            constraints=[
                {
                    "id": "later",
                    "type": "arrival_deadline",
                    "event_id": "a",
                    "deadline": "2026-01-01T00:55:00Z",
                    "source": source("deadline:later", "gmail"),
                },
                {
                    "id": "earlier",
                    "type": "arrival_deadline",
                    "event_id": "a",
                    "deadline": "2026-01-01T00:50:00Z",
                    "source": source("deadline:earlier", "gmail"),
                },
            ],
        )
        item = simulate_day(payload)["events"][0]
        self.assertEqual("2026-01-01T00:50:00Z", item["deadline"])
        self.assertEqual(10.0, item["delay_minutes"]["p50"])

    def test_offsets_across_daylight_saving_transition_use_utc_instants(self):
        payload = scenario(
            [
                event(
                    "fallback",
                    "2026-10-25T02:30:00+02:00",
                    "2026-10-25T02:30:00+01:00",
                )
            ],
        )
        payload["horizon"] = {
            "start": "2026-10-25T00:00:00+02:00",
            "end": "2026-10-26T00:00:00+01:00",
        }
        item = simulate_day(payload)["events"][0]
        self.assertEqual("2026-10-25T00:30:00Z", item["start"]["p50"])
        self.assertEqual("2026-10-25T01:30:00Z", item["end"]["p50"])


class CommandLineTests(unittest.TestCase):
    def test_cli_writes_one_json_object(self):
        result = subprocess.run(
            [sys.executable, "-m", "packages.aeon_engine", "fixtures/demo/day-v1.json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertEqual(1, len(result.stdout.splitlines()))
        self.assertEqual("aeon-demo-day-v1", json.loads(result.stdout)["scenario_id"])

    def test_cli_reports_invalid_json_on_stderr(self):
        result = subprocess.run(
            [sys.executable, "-m", "packages.aeon_engine", "docs/product/ENGINE_CONTRACT_V1.md"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("error:", result.stderr)


if __name__ == "__main__":
    unittest.main()
