import unittest

from packages.aeon_planner.validation import validate_context


class OpenRoutingSourceTests(unittest.TestCase):
    def test_osrm_route_is_accepted_without_changing_existing_providers(self):
        payload = {
            "mode": "live",
            "events": [
                {"id": "a", "private": True, "classification": "flexible"},
                {"id": "b", "private": True, "classification": "fixed"},
            ],
        }
        context = {
            "target_event_id": "b",
            "movable_event_ids": ["a"],
            "location_by_event_id": {"a": "origin", "b": "destination"},
            "route_catalog": [
                {"from_location_id": "origin", "to_location_id": "destination", "duration_minutes": {"min": 8, "mode": 10, "max": 14}, "source": {"provider": "osrm", "reference": "osrm:opaque", "synthetic": False, "assumption": "without live traffic"}}
            ],
            "step_minutes": 15,
            "max_candidates": 10,
        }
        validated = validate_context(payload, context)
        self.assertEqual("osrm", validated.routes[("origin", "destination")].source["provider"])


if __name__ == "__main__":
    unittest.main()
