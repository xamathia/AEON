import unittest

from packages.aeon_engine import simulate_day


class OpenRoutingSourceTests(unittest.TestCase):
    def test_osrm_source_is_accepted_and_preserved(self):
        payload = {
            "schema_version": "1.0",
            "scenario_id": "open-route",
            "mode": "live",
            "timezone": "Europe/Paris",
            "horizon": {"start": "2026-01-01T08:00:00Z", "end": "2026-01-01T12:00:00Z"},
            "seed": 1,
            "samples": 1,
            "events": [
                {"id": "a", "title": "A", "planned_start": "2026-01-01T09:00:00Z", "planned_end": "2026-01-01T09:30:00Z", "classification": "fixed", "private": True, "overrun_minutes": {"min": 0, "mode": 0, "max": 0}, "source": {"provider": "google_calendar", "reference": "calendar:a", "synthetic": False, "assumption": ""}},
                {"id": "b", "title": "B", "planned_start": "2026-01-01T10:00:00Z", "planned_end": "2026-01-01T10:30:00Z", "classification": "fixed", "private": True, "overrun_minutes": {"min": 0, "mode": 0, "max": 0}, "source": {"provider": "google_calendar", "reference": "calendar:b", "synthetic": False, "assumption": ""}},
            ],
            "travel_edges": [{"from_event_id": "a", "to_event_id": "b", "duration_minutes": {"min": 8, "mode": 10, "max": 14}, "source": {"provider": "osrm", "reference": "osrm:opaque", "synthetic": False, "assumption": "without live traffic"}}],
            "constraints": [],
        }
        result = simulate_day(payload)
        source = next(item for item in result["sources"] if item["reference"] == "osrm:opaque")
        self.assertEqual("osrm", source["provider"])
        self.assertEqual("without live traffic", source["assumption"])


if __name__ == "__main__":
    unittest.main()
