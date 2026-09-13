import unittest
import urllib.parse

from packages.aeon_connectors import ConnectorError, GoogleCalendarClient, HttpResponse
from packages.aeon_engine.validation import validate_payload


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response or HttpResponse(200, {}, {})
        self.error = error
        self.calls = []

    def request(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.response


def timed_event(event_id, start="2026-01-01T09:00:00+01:00", end="2026-01-01T10:00:00+01:00", **extra):
    event = {
        "id": event_id,
        "summary": event_id,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "organizer": {"self": True},
        "attendees": [{"self": True}],
    }
    event.update(extra)
    return event


class GoogleCalendarClientTests(unittest.TestCase):
    def test_requests_one_page_and_normalizes_recurrence_and_provenance(self):
        raw = timed_event(
            "opaque/id",
            start="2026-01-01T09:00:00",
            end="2026-01-01T10:00:00",
            etag="etag-1",
            updated="2025-12-30T12:00:00Z",
            recurringEventId="series-1",
            visibility="private",
        )
        raw["start"]["timeZone"] = "Europe/Paris"
        raw["end"]["timeZone"] = "Europe/Paris"
        transport = FakeTransport(
            HttpResponse(
                200,
                {},
                {"items": [raw], "nextPageToken": "page-2", "nextSyncToken": "sync-2"},
            )
        )
        result = GoogleCalendarClient("token-value", transport).list_events(
            "owner@example.test",
            time_min="2026-01-01T00:00:00Z",
            time_max="2026-01-02T00:00:00Z",
            page_token="page-1",
            authorized_flexible_ids=["opaque/id"],
        )

        method, url, headers = transport.calls[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual("GET", method)
        self.assertIn("owner%40example.test", url)
        self.assertEqual(["true"], query["singleEvents"])
        self.assertEqual(["true"], query["showDeleted"])
        self.assertEqual(["startTime"], query["orderBy"])
        self.assertEqual(["page-1"], query["pageToken"])
        self.assertEqual("Bearer token-value", headers["Authorization"])
        event = result["events"][0]
        self.assertEqual("2026-01-01T09:00:00+01:00", event["planned_start"])
        self.assertEqual("flexible", event["classification"])
        self.assertTrue(event["private"])
        self.assertEqual("series-1", event["recurring_event_id"])
        self.assertEqual("private", event["visibility"])
        self.assertEqual("google_calendar", event["source"]["provider"])
        self.assertFalse(event["source"]["synthetic"])
        self.assertEqual("uncalibrated overrun, zero prior", event["source"]["assumption"])
        self.assertEqual("page-2", result["next_page_token"])
        self.assertEqual("sync-2", result["next_sync_token"])

    def test_excludes_all_day_malformed_and_outside_events_and_keeps_deletions(self):
        body = {
            "items": [
                {"id": "cancelled", "status": "cancelled"},
                {"id": "all-day", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}},
                timed_event("outside", start="2025-12-31T23:30:00Z", end="2026-01-01T01:00:00Z"),
                {"id": "broken", "start": {}, "end": {}},
                "not-an-object",
            ]
        }
        result = GoogleCalendarClient("token", FakeTransport(HttpResponse(200, {}, body))).list_events(
            "calendar",
            time_min="2026-01-01T00:00:00Z",
            time_max="2026-01-02T00:00:00Z",
        )
        self.assertEqual(["cancelled"], result["deleted_event_ids"])
        self.assertEqual(
            [
                {"id": "all-day", "reason": "all_day_requires_policy"},
                {"id": "outside", "reason": "event_outside_horizon"},
                {"id": "broken", "reason": "malformed_event"},
                {"id": "unknown:4", "reason": "malformed_event"},
            ],
            result["excluded_events"],
        )

    def test_flexibility_requires_self_organizer_and_no_third_party(self):
        not_authorized = timed_event("not-authorized")
        not_owned = timed_event("not-owned", organizer={"self": False}, attendees=[])
        third_party = timed_event(
            "third-party",
            attendees=[{"self": True}, {"email": "guest@example.test"}],
        )
        result = GoogleCalendarClient(
            "token",
            FakeTransport(HttpResponse(200, {}, {"items": [not_authorized, not_owned, third_party]})),
        ).list_events(
            "calendar",
            authorized_flexible_ids=["not-owned", "third-party"],
        )
        self.assertEqual(["fixed", "fixed", "fixed"], [item["classification"] for item in result["events"]])
        self.assertEqual([True, False, False], [item["private"] for item in result["events"]])
        self.assertEqual(2, result["events"][2]["attendees_count"])

    def test_omitted_attendees_prevent_private_or_flexible_classification(self):
        incomplete = timed_event(
            "incomplete-attendees",
            attendees=[],
            attendeesOmitted=True,
        )
        result = GoogleCalendarClient(
            "token",
            FakeTransport(HttpResponse(200, {}, {"items": [incomplete]})),
        ).list_events(
            "calendar",
            authorized_flexible_ids=["incomplete-attendees"],
        )
        event = result["events"][0]
        self.assertEqual("fixed", event["classification"])
        self.assertFalse(event["private"])
        self.assertTrue(event["attendees_omitted"])

    def test_sync_request_omits_forbidden_parameters_and_410_is_explicit(self):
        transport = FakeTransport(HttpResponse(200, {}, {}))
        GoogleCalendarClient("token", transport).list_events(
            "calendar", sync_token="sync", page_token="next"
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0][1]).query)
        self.assertEqual(["sync"], query["syncToken"])
        self.assertNotIn("orderBy", query)
        self.assertNotIn("timeMin", query)
        self.assertNotIn("timeMax", query)

        expired = GoogleCalendarClient("token", FakeTransport(HttpResponse(410, {}, {"private": "body"})))
        with self.assertRaises(ConnectorError) as raised:
            expired.list_events("calendar", sync_token="sync")
        self.assertEqual("sync_expired", raised.exception.code)
        self.assertNotIn("private", repr(raised.exception))

    def test_sync_token_cannot_mix_with_horizon(self):
        with self.assertRaises(ConnectorError) as raised:
            GoogleCalendarClient("token", FakeTransport()).list_events(
                "calendar", sync_token="sync", time_min="2026-01-01T00:00:00Z"
            )
        self.assertEqual("invalid_arguments", raised.exception.code)

    def test_normalized_event_satisfies_the_engine_live_contract(self):
        client = GoogleCalendarClient(
            "token",
            FakeTransport(HttpResponse(200, {}, {"items": [timed_event("engine-ready")]})),
        )
        event = client.list_events(
            "calendar",
            time_min="2026-01-01T00:00:00Z",
            time_max="2026-01-02T00:00:00Z",
        )["events"][0]
        scenario = {
            "schema_version": "1.0",
            "scenario_id": "connector-contract",
            "mode": "live",
            "timezone": "Europe/Paris",
            "horizon": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"},
            "seed": 1,
            "samples": 1,
            "events": [event],
            "travel_edges": [],
            "constraints": [],
        }
        validated = validate_payload(scenario)
        self.assertEqual("engine-ready", validated.events[0].event_id)


if __name__ == "__main__":
    unittest.main()
