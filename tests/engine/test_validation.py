import copy
import json
from pathlib import Path
import unittest

from packages.aeon_engine.validation import validate_payload


ROOT = Path(__file__).resolve().parents[2]


class PayloadValidationTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads((ROOT / "fixtures/demo/day-v1.json").read_text())

    def test_demo_fixture_is_valid_without_mutation(self):
        original = copy.deepcopy(self.payload)
        scenario = validate_payload(self.payload)
        self.assertEqual("aeon-demo-day-v1", scenario.scenario_id)
        self.assertEqual(3, len(scenario.events))
        self.assertEqual(original, self.payload)

    def test_accepts_shared_but_acyclic_json_values(self):
        shared_source = self.payload["events"][0]["source"]
        self.payload["events"][1]["source"] = shared_source
        scenario = validate_payload(self.payload)
        self.assertEqual(shared_source["reference"], scenario.events[1].source.reference)

    def test_rejects_a_python_cycle(self):
        self.payload["extra"] = self.payload
        with self.assertRaisesRegex(ValueError, "acyclic"):
            validate_payload(self.payload)

    def test_rejects_boolean_integer_and_non_finite_number(self):
        for field, value in (("seed", True), ("samples", float("nan"))):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.payload)
                payload[field] = value
                with self.assertRaisesRegex(ValueError, field):
                    validate_payload(payload)

    def test_rejects_naive_datetime(self):
        self.payload["events"][0]["planned_start"] = "2026-09-14T16:00:00"
        with self.assertRaisesRegex(ValueError, "explicit UTC offset"):
            validate_payload(self.payload)

    def test_rejects_invalid_schema_timezone_and_distribution(self):
        mutations = (
            ("schema", lambda payload: payload.update(schema_version="2.0"), "schema_version"),
            ("timezone", lambda payload: payload.update(timezone="Mars/Olympus"), "timezone"),
            (
                "distribution",
                lambda payload: payload["events"][0].update(
                    overrun_minutes={"min": 4, "mode": 3, "max": 5}
                ),
                "0 <= min <= mode <= max",
            ),
        )
        for name, mutate, message in mutations:
            with self.subTest(name=name):
                payload = copy.deepcopy(self.payload)
                mutate(payload)
                with self.assertRaisesRegex(ValueError, message):
                    validate_payload(payload)

    def test_rejects_non_adjacent_edge(self):
        self.payload["travel_edges"][0]["to_event_id"] = "dinner"
        with self.assertRaisesRegex(ValueError, "adjacent"):
            validate_payload(self.payload)

    def test_rejects_missing_edge_and_unknown_constraint_target(self):
        missing_edge = copy.deepcopy(self.payload)
        missing_edge["travel_edges"].pop()
        with self.assertRaisesRegex(ValueError, "exactly one edge"):
            validate_payload(missing_edge)

        unknown_target = copy.deepcopy(self.payload)
        unknown_target["constraints"][0]["event_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "unknown event"):
            validate_payload(unknown_target)

    def test_rejects_unsorted_events_and_false_synthetic_source(self):
        unsorted = copy.deepcopy(self.payload)
        unsorted["events"][0], unsorted["events"][1] = (
            unsorted["events"][1],
            unsorted["events"][0],
        )
        with self.assertRaisesRegex(ValueError, "sorted"):
            validate_payload(unsorted)

        false_source = copy.deepcopy(self.payload)
        false_source["events"][0]["source"]["synthetic"] = False
        with self.assertRaisesRegex(ValueError, "true in synthetic mode"):
            validate_payload(false_source)

    def test_all_invalid_field_shapes_raise_value_error(self):
        mutations = (
            lambda payload: payload.update(mode=[]),
            lambda payload: payload["events"][0].update(classification=[]),
            lambda payload: payload["events"][0]["source"].update(provider=[]),
            lambda payload: payload["events"][0]["overrun_minutes"].update(max=10**1000),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                payload = copy.deepcopy(self.payload)
                mutate(payload)
                with self.assertRaises(ValueError):
                    validate_payload(payload)

    def test_rejects_ambiguous_source_reference(self):
        source = self.payload["events"][1]["source"]
        source["reference"] = self.payload["events"][0]["source"]["reference"]
        source["assumption"] = "Different descriptor"
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            validate_payload(self.payload)


if __name__ == "__main__":
    unittest.main()
