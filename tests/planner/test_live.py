import copy
from datetime import datetime
import unittest

from packages.aeon_planner import plan_live_day


def source(provider, reference, *, assumption, synthetic=False):
    return {
        "provider": provider,
        "reference": reference,
        "synthetic": synthetic,
        "assumption": assumption,
    }


def event(event_id, start, end, *, flexible=False):
    return {
        "id": event_id,
        "title": event_id.title(),
        "planned_start": start,
        "planned_end": end,
        "classification": "flexible" if flexible else "fixed",
        "private": flexible,
        "overrun_minutes": {"min": 0, "mode": 0, "max": 0},
        "source": source(
            "google_calendar",
            f"google_calendar:primary:{event_id}",
            assumption="observed event",
        ),
    }


def live_payload():
    return {
        "schema_version": "1.0",
        "scenario_id": "live-planning",
        "mode": "live",
        "timezone": "Europe/Paris",
        "horizon": {
            "start": "2026-10-25T08:00:00Z",
            "end": "2026-10-25T18:00:00Z",
        },
        "seed": 42,
        "samples": 20,
        "events": [
            event("meeting", "2026-10-25T10:00:00Z", "2026-10-25T11:00:00Z"),
            event(
                "focus",
                "2026-10-25T11:00:00Z",
                "2026-10-25T12:00:00Z",
                flexible=True,
            ),
            event("dinner", "2026-10-25T13:00:00Z", "2026-10-25T14:00:00Z"),
        ],
        "travel_edges": [
            {
                "from_event_id": "meeting",
                "to_event_id": "focus",
                "duration_minutes": {"min": 0, "mode": 0, "max": 0},
                "source": source(
                    "user",
                    "user:same-place:office",
                    assumption="declared same office",
                    synthetic=False,
                ),
            },
            {
                "from_event_id": "focus",
                "to_event_id": "dinner",
                "duration_minutes": {"min": 90, "mode": 90, "max": 90},
                "source": source(
                    "google_routes",
                    "google_routes:office:restaurant",
                    assumption="uncalibrated point observation",
                ),
            },
        ],
        "constraints": [
            {
                "id": "gmail-arrival",
                "type": "arrival_deadline",
                "event_id": "dinner",
                "deadline": "2026-10-25T12:45:00Z",
                "source": source(
                    "gmail",
                    "gmail:confirmed-deadline",
                    assumption="deadline confirmed by the user",
                ),
            }
        ],
    }


def selection(**changes):
    value = {
        "target_event_id": "dinner",
        "movable_event_ids": ["focus"],
        "window": {
            "start": "2026-10-25T08:00:00Z",
            "end": "2026-10-25T12:00:00Z",
        },
        "step_minutes": 30,
        "max_candidates": 100,
    }
    value.update(changes)
    return value


class LivePlannerTests(unittest.TestCase):
    def test_live_plan_finds_real_improvement_and_preserves_inputs_and_evidence(self):
        payload = live_payload()
        selected = selection()
        original_payload = copy.deepcopy(payload)
        original_selection = copy.deepcopy(selected)

        result = plan_live_day(payload, selected)

        self.assertEqual("plans_found", result["status"])
        self.assertEqual(0, result["llm_calls"])
        self.assertEqual(original_payload, payload)
        self.assertEqual(original_selection, selected)
        self.assertTrue(result["plans"])
        plan = result["plans"][0]
        self.assertLess(plan["target_risk_after"], plan["target_risk_before"])
        self.assertTrue(plan["requires_approval"])
        self.assertEqual(original_payload["horizon"], plan["scenario"]["horizon"])
        self.assertEqual(original_payload["constraints"], plan["scenario"]["constraints"])
        candidate_by_id = {item["id"]: item for item in plan["scenario"]["events"]}
        original_by_id = {item["id"]: item for item in original_payload["events"]}
        self.assertEqual(original_by_id["meeting"], candidate_by_id["meeting"])
        self.assertEqual(original_by_id["dinner"], candidate_by_id["dinner"])
        operation = plan["operations"][0]
        self.assertGreaterEqual(_instant(operation["after"]["planned_start"]), _instant(selected["window"]["start"]))
        self.assertLessEqual(_instant(operation["after"]["planned_end"]), _instant(selected["window"]["end"]))
        sources = {edge["source"]["reference"]: edge["source"] for edge in plan["scenario"]["travel_edges"]}
        self.assertIn("user:same-place:office", sources)
        self.assertFalse(sources["user:same-place:office"]["synthetic"])
        self.assertIn("google_routes:office:restaurant", sources)
        self.assertIn("not a new traffic observation", sources["google_routes:office:restaurant"]["assumption"])

    def test_same_location_equivalence_is_symmetric_but_travel_is_not(self):
        result = plan_live_day(live_payload(), selection())
        plan = result["plans"][0]
        pairs = {
            (edge["from_event_id"], edge["to_event_id"]): edge
            for edge in plan["scenario"]["travel_edges"]
        }
        self.assertIn(("focus", "meeting"), pairs)
        self.assertEqual("user", pairs[("focus", "meeting")]["source"]["provider"])

        after_dinner = selection(
            window={
                "start": "2026-10-25T14:00:00Z",
                "end": "2026-10-25T16:00:00Z",
            }
        )
        unavailable = plan_live_day(live_payload(), after_dinner)
        self.assertEqual("no_better_plan", unavailable["status"])
        self.assertEqual(0, unavailable["search"]["evaluated_candidates"])
        self.assertIn("MISSING_ROUTE", unavailable["search"]["rejected_reasons"])

    def test_no_real_improvement_is_reported_honestly(self):
        result = plan_live_day(
            live_payload(), selection(target_event_id="meeting")
        )
        self.assertEqual("no_better_plan", result["status"])
        self.assertEqual([], result["plans"])

    def test_candidate_budget_and_window_are_enforced(self):
        result = plan_live_day(
            live_payload(), selection(max_candidates=1, step_minutes=15)
        )
        self.assertEqual(1, result["search"]["evaluated_candidates"])
        self.assertTrue(result["search"]["truncated"])
        for plan in result["plans"]:
            operation = plan["operations"][0]
            self.assertGreaterEqual(
                _instant(operation["after"]["planned_start"]),
                _instant("2026-10-25T08:00:00Z"),
            )
            self.assertLessEqual(
                _instant(operation["after"]["planned_end"]),
                _instant("2026-10-25T12:00:00Z"),
            )

    def test_window_offsets_are_compared_as_utc_instants(self):
        selected = selection(
            window={
                "start": "2026-10-25T10:00:00+02:00",
                "end": "2026-10-25T13:00:00+01:00",
            }
        )
        result = plan_live_day(live_payload(), selected)
        self.assertEqual("plans_found", result["status"])

    def test_invalid_selection_is_rejected(self):
        cases = (
            selection(extra=True),
            selection(movable_event_ids=[]),
            selection(movable_event_ids=["focus", "focus"]),
            selection(movable_event_ids=["meeting"]),
            selection(movable_event_ids=["unknown"]),
            selection(target_event_id="focus"),
            selection(step_minutes=True),
            selection(max_candidates=101),
            selection(window={"start": "2026-10-25T08:00:00", "end": "2026-10-25T12:00:00Z"}),
            selection(window={"start": "2026-10-25T12:00:00Z", "end": "2026-10-25T08:00:00Z"}),
            selection(window={"start": "2026-10-25T07:00:00Z", "end": "2026-10-25T12:00:00Z"}),
        )
        for selected in cases:
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                plan_live_day(live_payload(), selected)

    def test_synthetic_mode_and_ambiguous_extra_observation_are_rejected(self):
        synthetic = live_payload()
        synthetic["mode"] = "synthetic"
        with self.assertRaisesRegex(ValueError, "must be live"):
            plan_live_day(synthetic, selection())

        ambiguous = live_payload()
        extra = copy.deepcopy(ambiguous["travel_edges"][1])
        extra["duration_minutes"] = {"min": 80, "mode": 80, "max": 80}
        ambiguous["travel_edges"].append(extra)
        with self.assertRaises(ValueError):
            plan_live_day(ambiguous, selection())


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


if __name__ == "__main__":
    unittest.main()
