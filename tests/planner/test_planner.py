import copy
import json
from pathlib import Path
import unittest

from packages.aeon_planner import plan_day

from test_validation import reference_context


ROOT = Path(__file__).resolve().parents[2]


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads((ROOT / "fixtures/demo/day-v1.json").read_text())
        self.context = reference_context(self.payload)

    def test_reference_day_produces_only_real_improvements_without_mutation(self):
        original_payload = copy.deepcopy(self.payload)
        original_context = copy.deepcopy(self.context)

        result = plan_day(self.payload, self.context)

        self.assertEqual("plans_found", result["status"])
        self.assertGreater(len(result["plans"]), 0)
        self.assertLessEqual(len(result["plans"]), 3)
        json.dumps(result, allow_nan=False)
        for plan in result["plans"]:
            self.assertLess(plan["target_risk_after"], plan["target_risk_before"])
            self.assertEqual(["PROTECTS_FIXED_EVENT", "LOWER_RESIDUAL_RISK"], plan["reason_codes"])
            self.assertTrue(plan["requires_approval"])
            self.assertEqual(1, len(plan["operations"]))
            self.assertEqual("focus", plan["operations"][0]["event_id"])
            ordered = plan["scenario"]["events"]
            self.assertTrue(
                all(
                    _instant(left["planned_end"]) <= _instant(right["planned_start"])
                    for left, right in zip(ordered, ordered[1:])
                )
            )
            self.assertLessEqual(
                _instant(plan["operations"][0]["after"]["planned_end"]),
                _instant(self.payload["horizon"]["end"]),
            )
        self.assertEqual(original_payload, self.payload)
        self.assertEqual(original_context, self.context)
        self.assertEqual(0, result["llm_calls"])

    def test_result_is_reproducible_and_sorted_by_contract(self):
        first = plan_day(self.payload, self.context)
        second = plan_day(self.payload, self.context)
        self.assertEqual(first, second)
        keys = [
            (plan["target_risk_after"], abs(plan["shift_minutes"]), plan["id"])
            for plan in first["plans"]
        ]
        self.assertEqual(sorted(keys), keys)

    def test_reference_candidate_moves_focus_before_meeting(self):
        result = plan_day(self.payload, self.context)
        meeting_start = self.payload["events"][0]["planned_start"]
        self.assertTrue(
            any(
                plan["operations"][0]["after"]["planned_end"] <= "2026-09-14T14:00:00Z"
                and meeting_start == "2026-09-14T16:00:00+02:00"
                for plan in result["plans"]
            )
        )
        self.assertEqual(-90, result["plans"][0]["shift_minutes"])

    def test_fixed_events_and_constraints_are_preserved(self):
        result = plan_day(self.payload, self.context)
        candidate = result["plans"][0]["scenario"]
        original_by_id = {event["id"]: event for event in self.payload["events"]}
        candidate_by_id = {event["id"]: event for event in candidate["events"]}
        self.assertEqual(original_by_id["meeting"], candidate_by_id["meeting"])
        self.assertEqual(original_by_id["dinner"], candidate_by_id["dinner"])
        self.assertEqual(self.payload["constraints"], candidate["constraints"])
        self.assertEqual(self.payload["horizon"], candidate["horizon"])
        self.assertEqual(self.payload["seed"], candidate["seed"])
        self.assertEqual(self.payload["samples"], candidate["samples"])

    def test_return_route_allows_focus_after_dinner(self):
        route = copy.deepcopy(self.context["route_catalog"][0])
        route["from_location_id"] = "restaurant"
        route["to_location_id"] = "office"
        route["source"]["reference"] = "synthetic:route:return"
        self.context["route_catalog"].append(route)
        self.context["step_minutes"] = 60
        self.context["max_candidates"] = 100

        result = plan_day(self.payload, self.context)

        dinner_end = "2026-09-14T17:00:00Z"
        self.assertTrue(
            any(plan["operations"][0]["after"]["planned_start"] >= dinner_end for plan in result["plans"])
        )

    def test_missing_route_is_reported_and_never_assumed_zero(self):
        self.context["route_catalog"] = []
        self.context["step_minutes"] = 60
        self.context["max_candidates"] = 100

        result = plan_day(self.payload, self.context)

        self.assertEqual("no_better_plan", result["status"])
        self.assertEqual(0, result["search"]["evaluated_candidates"])
        self.assertIn("MISSING_ROUTE", result["search"]["rejected_reasons"])

    def test_catalog_cannot_change_baseline_travel_to_claim_an_improvement(self):
        self.context["max_candidates"] = 1
        for duration in ({"min": 0, "mode": 0, "max": 0},
                         {"min": 30, "mode": 37, "max": 55}):
            with self.subTest(duration=duration):
                self.context["route_catalog"][0]["duration_minutes"] = duration
                with self.assertRaisesRegex(ValueError, "baseline.*duration"):
                    plan_day(self.payload, self.context)

    def test_implicit_same_location_zero_cannot_erase_baseline_travel(self):
        self.payload["travel_edges"][0]["duration_minutes"] = {"min": 5, "mode": 5, "max": 5}
        with self.assertRaisesRegex(ValueError, "baseline.*duration"):
            plan_day(self.payload, self.context)

    def test_explicit_same_location_zero_must_also_match_baseline(self):
        edge = self.payload["travel_edges"][0]
        self.context["route_catalog"].append({
            "from_location_id": "office", "to_location_id": "office",
            "duration_minutes": copy.deepcopy(edge["duration_minutes"]),
            "source": copy.deepcopy(edge["source"]),
        })
        edge["duration_minutes"] = {"min": 5, "mode": 5, "max": 5}
        with self.assertRaisesRegex(ValueError, "baseline.*duration"):
            plan_day(self.payload, self.context)

    def test_incident_requires_matching_catalog_and_remains_plannable(self):
        edge = self.payload["travel_edges"][-1]
        edge["duration_minutes"] = {key: value + 12 for key, value in edge["duration_minutes"].items()}
        edge["source"]["assumption"] += " Synthetic incident: +12 minutes."
        with self.assertRaisesRegex(ValueError, "baseline.*duration"):
            plan_day(self.payload, self.context)
        self.context["route_catalog"][0]["duration_minutes"] = copy.deepcopy(edge["duration_minutes"])
        self.context["route_catalog"][0]["source"] = copy.deepcopy(edge["source"])
        result = plan_day(self.payload, self.context)
        self.assertEqual(result["status"], "plans_found")
        self.assertTrue(all(plan["target_risk_after"] < plan["target_risk_before"]
                            for plan in result["plans"]))

    def test_search_limit_is_independent_of_improvement_count(self):
        self.context["max_candidates"] = 1
        result = plan_day(self.payload, self.context)
        self.assertEqual(1, result["search"]["evaluated_candidates"])
        self.assertTrue(result["search"]["truncated"])

    def test_zero_baseline_risk_yields_no_better_plan(self):
        self.context["target_event_id"] = "meeting"
        self.context["max_candidates"] = 5
        result = plan_day(self.payload, self.context)
        self.assertEqual("no_better_plan", result["status"])
        self.assertEqual([], result["plans"])

    def test_departure_marker_without_changed_scenario_cannot_claim_reduction(self):
        self.context["movable_event_ids"] = []
        self.context["departure_marker"] = "2026-09-14T15:00:00Z"
        result = plan_day(self.payload, self.context)
        self.assertEqual("no_better_plan", result["status"])
        self.assertEqual(0, result["search"]["evaluated_candidates"])

    def test_operations_preserve_duration_and_plan_id_is_sha256(self):
        plan = plan_day(self.payload, self.context)["plans"][0]
        operation = plan["operations"][0]
        self.assertEqual(64, len(plan["id"]))
        before_start = operation["before"]["planned_start"]
        before_end = operation["before"]["planned_end"]
        after_start = operation["after"]["planned_start"]
        after_end = operation["after"]["planned_end"]
        self.assertEqual(
            _minutes(before_start, before_end),
            _minutes(after_start, after_end),
        )

    def test_fractional_instants_remain_exact_in_scenario_and_operation(self):
        self.payload["horizon"]["start"] = "2026-09-14T00:00:00.500000+02:00"
        self.context["step_minutes"] = 60
        self.context["max_candidates"] = 100

        plan = plan_day(self.payload, self.context)["plans"][0]
        operation = plan["operations"][0]
        moved = next(
            event for event in plan["scenario"]["events"] if event["id"] == "focus"
        )

        self.assertTrue(operation["after"]["planned_start"].endswith(".500000Z"))
        self.assertEqual(operation["after"]["planned_start"], moved["planned_start"])
        self.assertEqual(operation["after"]["planned_end"], moved["planned_end"])


def _minutes(start, end):
    return (_instant(end) - _instant(start)).total_seconds() / 60


def _instant(value):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


if __name__ == "__main__":
    unittest.main()
