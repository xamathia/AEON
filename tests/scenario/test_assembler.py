import copy
import math
import traceback
import unittest

from packages.aeon_engine import simulate_day
from packages.aeon_connectors import route_to_prior
from packages.aeon_scenario import assemble_scenario


HORIZON = {
    "start": "2026-10-25T00:00:00+02:00",
    "end": "2026-10-25T06:00:00+01:00",
}


def source(provider, reference, *, synthetic=False, assumption=""):
    return {
        "provider": provider,
        "reference": reference,
        "synthetic": synthetic,
        "assumption": assumption,
    }


def event(event_id, start, end, **changes):
    value = {
        "id": event_id,
        "title": f"Event {event_id}",
        "planned_start": start,
        "planned_end": end,
        "classification": "fixed",
        "private": False,
        "overrun_minutes": {"min": 0, "mode": 0, "max": 0},
        "source": source(
            "google_calendar",
            f"google_calendar:primary:{event_id}",
            assumption="uncalibrated overrun",
        ),
        "calendar_id": "primary",
        "etag": f"etag-{event_id}",
    }
    value.update(changes)
    return value


def edge(left, right, *, zero=False):
    if zero:
        return {
            "from_event_id": left,
            "to_event_id": right,
            "duration_minutes": {"min": 0, "mode": 0, "max": 0},
            "source": source(
                "user",
                f"user:same-place:{left}:{right}",
                synthetic=True,
                assumption="explicit same-location declaration",
            ),
        }
    return {
        "from_event_id": left,
        "to_event_id": right,
        "duration_minutes": {"min": 8, "mode": 10, "max": 14},
        "source": source(
            "google_routes",
            f"google_routes:{left}:{right}",
            assumption="uncalibrated ÆON distribution derived from a point observation",
        ),
    }


def constraint(event_id="dinner"):
    return {
        "id": "gmail-arrival",
        "type": "arrival_deadline",
        "event_id": event_id,
        "deadline": "2026-10-25T02:50:00+01:00",
        "source": source(
            "gmail",
            "gmail:trusted-constraint",
            assumption="arrival time confirmed by the caller",
        ),
    }


def observations():
    meeting = event(
        "meeting",
        "2026-10-25T02:30:00+02:00",
        "2026-10-25T03:00:00+02:00",
        overrun_minutes={"min": 0, "mode": 5, "max": 10},
    )
    focus = event(
        "focus",
        "2026-10-25T02:15:00+01:00",
        "2026-10-25T02:45:00+01:00",
        classification="flexible",
        private=True,
        organizer_self=True,
        attendees_count=0,
    )
    dinner = event(
        "dinner",
        "2026-10-25T03:00:00+01:00",
        "2026-10-25T04:00:00+01:00",
    )
    return {
        "calendar": {
            "events": [dinner, focus, meeting],
            "complete": True,
            "read_at": 1792890000,
        },
        "travel_edges": [edge("focus", "dinner"), edge("meeting", "focus", zero=True)],
        "constraints": [constraint()],
    }


def assemble(values=None, **changes):
    values = observations() if values is None else values
    arguments = {
        "scenario_id": "live-day",
        "timezone": "Europe/Paris",
        "horizon": HORIZON,
        "calendar": values["calendar"],
        "travel_edges": values["travel_edges"],
        "constraints": values["constraints"],
        "seed": 42,
        "samples": 100,
    }
    arguments.update(changes)
    return assemble_scenario(**arguments)


class ReadyScenarioTests(unittest.TestCase):
    def test_one_event_is_ready_without_an_invented_edge(self):
        values = observations()
        values["calendar"]["events"] = [values["calendar"]["events"][0]]
        values["travel_edges"] = []
        values["constraints"] = []
        result = assemble(values)
        self.assertEqual("ready", result["status"])
        self.assertEqual([], result["scenario"]["travel_edges"])

    def test_unsorted_dst_events_produce_a_traceable_deterministic_simulation(self):
        values = observations()
        original = copy.deepcopy(values)
        first = assemble(values)
        second = assemble(values)

        self.assertEqual(first, second)
        self.assertEqual(original, values)
        self.assertEqual("ready", first["status"])
        self.assertEqual([], first["issues"])
        scenario = first["scenario"]
        self.assertEqual("live", scenario["mode"])
        self.assertEqual(
            ["meeting", "focus", "dinner"],
            [item["id"] for item in scenario["events"]],
        )
        self.assertEqual("etag-meeting", scenario["events"][0]["etag"])
        self.assertEqual(values["calendar"]["events"][2], scenario["events"][0])
        self.assertEqual("fixed", scenario["events"][0]["classification"])
        self.assertFalse(scenario["events"][0]["private"])
        self.assertTrue(scenario["travel_edges"][0]["source"]["synthetic"])
        self.assertEqual(
            ["meeting", "focus"],
            [
                scenario["travel_edges"][0]["from_event_id"],
                scenario["travel_edges"][0]["to_event_id"],
            ],
        )
        self.assertEqual(
            sorted(first["assumptions"]), first["assumptions"]
        )
        self.assertIn("explicit same-location declaration", first["assumptions"])
        simulation = simulate_day(scenario)
        self.assertEqual(simulation, simulate_day(scenario))
        expected_sources = {
            item["source"]["reference"]: item["source"]
            for item in (
                values["calendar"]["events"]
                + values["travel_edges"]
                + values["constraints"]
            )
        }
        self.assertEqual(
            [expected_sources[key] for key in sorted(expected_sources)],
            simulation["sources"],
        )
        self.assertEqual(first["assumptions"], simulation["assumptions"])
        self.assertEqual(
            {"calendar_complete": True, "calendar_read_at": 1792890000.0},
            first["provenance"],
        )

    def test_shared_source_descriptor_is_preserved_and_deduplicated(self):
        values = observations()
        descriptor = copy.deepcopy(values["calendar"]["events"][0]["source"])
        values["calendar"]["events"][1]["source"] = descriptor
        result = assemble(values)
        references = [item["reference"] for item in simulate_day(result["scenario"])["sources"]]
        self.assertEqual(1, references.count(descriptor["reference"]))

    def test_route_prior_output_can_feed_an_adjacent_edge_without_conversion(self):
        values = observations()
        prior = route_to_prior(
            {
                "duration_seconds": 600,
                "source": source(
                    "google_routes",
                    "google_routes:observation",
                    assumption="point observation",
                ),
            }
        )
        values["travel_edges"][0] = {
            "from_event_id": "focus",
            "to_event_id": "dinner",
            **prior,
        }
        result = assemble(values)
        self.assertEqual("ready", result["status"])
        self.assertEqual(prior["duration_minutes"], result["scenario"]["travel_edges"][1]["duration_minutes"])
        self.assertEqual(prior["source"], result["scenario"]["travel_edges"][1]["source"])


class IncompleteScenarioTests(unittest.TestCase):
    def test_partial_calendar_and_multiple_missing_edges_are_cumulative(self):
        values = observations()
        values["calendar"]["complete"] = False
        values["travel_edges"] = []
        result = assemble(values)
        self.assertEqual("incomplete", result["status"])
        self.assertIsNone(result["scenario"])
        self.assertEqual(
            [
                {"code": "CALENDAR_INCOMPLETE", "event_ids": []},
                {"code": "MISSING_TRAVEL_EDGE", "event_ids": ["focus", "dinner"]},
                {"code": "MISSING_TRAVEL_EDGE", "event_ids": ["meeting", "focus"]},
            ],
            result["issues"],
        )
        self.assertNotIn("internal validation", result["assumptions"])

    def test_empty_partial_calendar_never_invents_events_or_travel(self):
        values = {
            "calendar": {"events": [], "complete": False, "read_at": 0},
            "travel_edges": [],
            "constraints": [],
        }
        result = assemble(values)
        self.assertEqual("incomplete", result["status"])
        self.assertIsNone(result["scenario"])
        self.assertEqual(
            [
                {"code": "CALENDAR_INCOMPLETE", "event_ids": []},
                {"code": "NO_EVENTS", "event_ids": []},
            ],
            result["issues"],
        )
        self.assertEqual([], result["assumptions"])

    def test_missing_edge_does_not_hide_a_malformed_provided_edge(self):
        values = observations()
        values["travel_edges"] = [edge("meeting", "focus", zero=True)]
        values["travel_edges"][0]["duration_minutes"]["mode"] = -1
        with self.assertRaisesRegex(ValueError, "duration_minutes"):
            assemble(values)


class StructuralValidationTests(unittest.TestCase):
    def test_rejects_nonfinite_boolean_numbers_naive_dates_and_long_horizon(self):
        cases = (
            ({"calendar": {**observations()["calendar"], "read_at": math.nan}}, None),
            ({"calendar": {**observations()["calendar"], "read_at": True}}, "read_at"),
            ({"seed": True}, "seed"),
            ({"samples": 0}, "samples"),
            (
                {"horizon": {"start": "2026-09-01T00:00:00Z", "end": "2026-09-09T00:00:00Z"}},
                "horizon",
            ),
        )
        for changes, message in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError) as raised:
                values = observations()
                call_changes = dict(changes)
                if "calendar" in call_changes:
                    values["calendar"] = call_changes.pop("calendar")
                assemble(values, **call_changes)
            if message:
                self.assertIn(message, str(raised.exception))

        values = observations()
        values["calendar"]["events"][0]["planned_start"] = "2026-10-25T03:00:00"
        with self.assertRaisesRegex(ValueError, "explicit UTC offset"):
            assemble(values)

    def test_rejects_duplicate_ids_edges_nonadjacent_edges_and_unknown_constraints(self):
        cases = []
        duplicate_events = observations()
        duplicate_events["calendar"]["events"][1]["id"] = "dinner"
        cases.append(duplicate_events)
        duplicate_edges = observations()
        duplicate_edges["travel_edges"].append(copy.deepcopy(duplicate_edges["travel_edges"][0]))
        cases.append(duplicate_edges)
        nonadjacent = observations()
        nonadjacent["travel_edges"] = [edge("meeting", "dinner")]
        cases.append(nonadjacent)
        unknown_constraint = observations()
        unknown_constraint["constraints"] = [constraint("unknown")]
        cases.append(unknown_constraint)
        for values in cases:
            with self.subTest(case=cases.index(values)), self.assertRaises(ValueError):
                assemble(values)

    def test_rejects_ambiguous_sources_and_zero_travel_without_assumption(self):
        ambiguous = observations()
        first_source = ambiguous["calendar"]["events"][0]["source"]
        second_source = ambiguous["calendar"]["events"][1]["source"]
        second_source["reference"] = first_source["reference"]
        second_source["assumption"] = "contradiction"
        with self.assertRaisesRegex(ValueError, "ambiguous source"):
            assemble(ambiguous)

        zero = observations()
        zero["travel_edges"][1]["source"]["assumption"] = ""
        with self.assertRaisesRegex(ValueError, "explicit assumption"):
            assemble(zero)

    def test_rejects_more_than_one_hundred_events_without_truncation(self):
        values = observations()
        template = values["calendar"]["events"][0]
        values["calendar"]["events"] = []
        for index in range(101):
            item = copy.deepcopy(template)
            item["id"] = f"event-{index}"
            values["calendar"]["events"].append(item)
        values["travel_edges"] = []
        values["constraints"] = []
        with self.assertRaisesRegex(ValueError, "at most 100"):
            assemble(values)

    def test_errors_never_repeat_sensitive_event_content(self):
        values = observations()
        secret = "private-title-7cbefdd0"
        values["calendar"]["events"][0]["title"] = secret
        values["calendar"]["events"][0]["classification"] = []
        with self.assertRaises(ValueError) as raised:
            assemble(values)
        self.assertNotIn(secret, str(raised.exception))

        values = observations()
        values["constraints"][0]["deadline"] = secret
        try:
            assemble(values)
        except ValueError:
            formatted = traceback.format_exc()
        else:
            self.fail("a malformed sensitive deadline must be rejected")
        self.assertNotIn(secret, formatted)

    def test_rejects_non_json_values_and_cycles_without_mutating_them(self):
        values = observations()
        values["calendar"]["extra"] = object()
        with self.assertRaisesRegex(ValueError, "JSON"):
            assemble(values)
        cyclic = observations()
        cyclic["calendar"]["cycle"] = cyclic["calendar"]
        with self.assertRaisesRegex(ValueError, "acyclic"):
            assemble(cyclic)

    def test_rejects_dictionary_keys_that_are_not_valid_utf8(self):
        values = observations()
        values["calendar"]["events"][0]["\ud800"] = "value"
        with self.assertRaisesRegex(ValueError, "valid Unicode"):
            assemble(values)

    def test_rejects_instants_that_overflow_when_normalized_to_utc(self):
        cases = (
            {
                "start": "0001-01-01T00:00:00+14:00",
                "end": "0001-01-01T01:00:00+14:00",
            },
            {
                "start": "9999-12-31T22:00:00-14:00",
                "end": "9999-12-31T23:00:00-14:00",
            },
        )
        for horizon in cases:
            with self.subTest(horizon=horizon), self.assertRaisesRegex(
                ValueError, "must be representable in UTC"
            ):
                assemble(horizon=horizon)


if __name__ == "__main__":
    unittest.main()
