import copy
import json
from pathlib import Path
import unittest

from packages.aeon_planner.validation import validate_context


ROOT = Path(__file__).resolve().parents[2]


def reference_context(payload):
    route = payload["travel_edges"][-1]
    return {
        "target_event_id": "dinner",
        "movable_event_ids": ["focus"],
        "location_by_event_id": {
            "meeting": "office",
            "focus": "office",
            "dinner": "restaurant",
        },
        "route_catalog": [
            {
                "from_location_id": "office",
                "to_location_id": "restaurant",
                "duration_minutes": copy.deepcopy(route["duration_minutes"]),
                "source": copy.deepcopy(route["source"]),
            }
        ],
    }


class ContextValidationTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads((ROOT / "fixtures/demo/day-v1.json").read_text())
        self.context = reference_context(self.payload)

    def test_reference_context_is_valid_with_defaults(self):
        result = validate_context(self.payload, self.context)
        self.assertEqual("dinner", result.target_event_id)
        self.assertEqual(("focus",), result.movable_event_ids)
        self.assertEqual(15, result.step_minutes)
        self.assertEqual(60, result.max_candidates)

    def test_rejects_unknown_target_and_incomplete_locations(self):
        for mutate, message in (
            (lambda context: context.update(target_event_id="missing"), "unknown event"),
            (lambda context: context["location_by_event_id"].pop("dinner"), "missing event"),
        ):
            with self.subTest(message=message):
                context = copy.deepcopy(self.context)
                mutate(context)
                with self.assertRaisesRegex(ValueError, message):
                    validate_context(self.payload, context)

    def test_rejects_movable_event_that_is_not_private_and_flexible(self):
        for field, value in (("private", False), ("classification", "fixed")):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.payload)
                payload["events"][1][field] = value
                with self.assertRaisesRegex(ValueError, "private and flexible"):
                    validate_context(payload, self.context)

    def test_rejects_unknown_or_duplicate_movable_event(self):
        for values, message in ((["missing"], "does not exist"), (["focus", "focus"], "duplicates")):
            with self.subTest(values=values):
                self.context["movable_event_ids"] = values
                with self.assertRaisesRegex(ValueError, message):
                    validate_context(self.payload, self.context)

    def test_rejects_duplicate_or_invalid_route(self):
        duplicate = copy.deepcopy(self.context)
        duplicate["route_catalog"].append(copy.deepcopy(duplicate["route_catalog"][0]))
        with self.assertRaisesRegex(ValueError, "duplicates"):
            validate_context(self.payload, duplicate)

        invalid = copy.deepcopy(self.context)
        invalid["route_catalog"][0]["duration_minutes"]["mode"] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite"):
            validate_context(self.payload, invalid)

    def test_same_location_catalog_entry_must_be_safe_zero_route(self):
        same = copy.deepcopy(self.context["route_catalog"][0])
        same["from_location_id"] = same["to_location_id"] = "office"
        same["duration_minutes"] = {"min": 0, "mode": 1, "max": 1}
        self.context["route_catalog"] = [same]
        with self.assertRaisesRegex(ValueError, "constant zero"):
            validate_context(self.payload, self.context)

    def test_rejects_invalid_search_bounds(self):
        for field, value in (("step_minutes", 4), ("step_minutes", True), ("max_candidates", 101)):
            with self.subTest(field=field, value=value):
                context = copy.deepcopy(self.context)
                context[field] = value
                with self.assertRaisesRegex(ValueError, "integer between"):
                    validate_context(self.payload, context)


if __name__ == "__main__":
    unittest.main()
