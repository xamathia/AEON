from datetime import datetime, timedelta, timezone
import io
import math
import traceback
import unittest
from unittest import mock
import urllib.error

from packages.aeon_connectors.open_routing import (
    OpenRoutingClient,
    OpenRoutingError,
    _LAST_DEPARTURE,
    _MAX_RESPONSE_BYTES,
    _NoRedirectHandler,
    _OpenDataTransport,
    _RATE_LOCK,
)
from packages.aeon_connectors.transport import HttpResponse


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers):
        self.calls.append((method, url, headers))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def geocode(label, lat, lon):
    return HttpResponse(200, {}, [{"display_name": label, "lat": lat, "lon": lon}])


def route(duration):
    return HttpResponse(200, {}, {"code": "Ok", "routes": [{"duration": duration}]})


class OpenRoutingClientTests(unittest.TestCase):
    def setUp(self):
        with _RATE_LOCK:
            _LAST_DEPARTURE.clear()

    def test_computes_directed_route_with_honest_prior_and_fixed_requests(self):
        clock = FakeClock()
        transport = FakeTransport(
            [geocode("Paris, France", "48.8566", "2.3522"), geocode("Lyon, France", "45.764", "4.8357"), route(600)]
        )
        client = OpenRoutingClient(transport=transport, clock=clock, sleeper=clock.sleep)

        result = client.compute_route(" Paris ", "Lyon")

        self.assertEqual({"min": 8.0, "mode": 10.0, "max": 14.0}, result["duration_minutes"])
        self.assertEqual("2026-01-01T12:00:01Z", result["observed_at"])
        self.assertEqual("Paris, France", result["origin_label"])
        self.assertEqual("Lyon, France", result["destination_label"])
        self.assertEqual("osrm", result["source"]["provider"])
        self.assertFalse(result["source"]["synthetic"])
        self.assertIn("without live traffic", result["source"]["assumption"])
        self.assertIn("uncalibrated", result["source"]["assumption"])
        self.assertTrue(result["source"]["reference"].startswith("osrm:"))
        self.assertNotIn("Paris", result["source"]["reference"])

        self.assertEqual("GET", transport.calls[0][0])
        self.assertEqual(
            "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q=Paris",
            transport.calls[0][1],
        )
        self.assertEqual(
            "https://routing.openstreetmap.de/routed-car/route/v1/driving/2.3522,48.8566;4.8357,45.764?overview=false&steps=false",
            transport.calls[2][1],
        )
        self.assertEqual(
            "AEON-Hackathon/1.0 (+https://github.com/xamathia/hackathon-AEON)",
            transport.calls[0][2]["User-Agent"],
        )
        self.assertEqual([1.0], clock.sleeps)

    def test_cache_is_per_instance_fifo_bounded_and_avoids_network(self):
        clock = FakeClock()
        responses = [geocode(f"Match {index}", "48", str(index)) for index in range(65)]
        responses.append(geocode("Match first again", "48", "0"))
        transport = FakeTransport(responses)
        client = OpenRoutingClient(transport=transport, clock=clock, sleeper=clock.sleep)

        for index in range(65):
            client._geocode(f"Address {index}")
        calls_after_fill = len(transport.calls)
        client._geocode("Address 64")
        self.assertEqual(calls_after_fill, len(transport.calls))
        client._geocode("Address 0")
        self.assertEqual(calls_after_fill + 1, len(transport.calls))
        self.assertEqual(64, len(client._geocode_cache))

    def test_rate_limit_is_process_wide_across_clients_and_waits_outside_lock(self):
        clock = FakeClock()
        first = OpenRoutingClient(
            transport=FakeTransport([geocode("A", "1", "1")]),
            clock=clock,
            sleeper=clock.sleep,
        )
        second = OpenRoutingClient(
            transport=FakeTransport([geocode("B", "2", "2")]),
            clock=clock,
            sleeper=clock.sleep,
        )
        lock_owned = []

        def sleeper(seconds):
            acquired = _RATE_LOCK.acquire(blocking=False)
            lock_owned.append(not acquired)
            if acquired:
                _RATE_LOCK.release()
            clock.sleep(seconds)

        second._sleeper = sleeper
        first._geocode("First")
        second._geocode("Second")

        self.assertEqual([1.0], clock.sleeps)
        self.assertEqual([False], lock_owned)

    def test_rate_limit_rechecks_actual_time_after_oversleep(self):
        clock = FakeClock()
        departures = []

        class RecordingTransport(FakeTransport):
            def request(self, method, url, headers):
                departures.append(clock().timestamp())
                return super().request(method, url, headers)

        transport = RecordingTransport(
            [
                geocode("One", "1", "1"),
                geocode("Two", "2", "2"),
                geocode("Three", "3", "3"),
            ]
        )

        def oversleep(seconds):
            clock.value += timedelta(seconds=seconds + 2)

        client = OpenRoutingClient(transport=transport, clock=clock, sleeper=oversleep)
        client._geocode("One")
        client._geocode("Two")
        client._geocode("Three")

        intervals = [right - left for left, right in zip(departures, departures[1:])]
        self.assertEqual([3.0, 3.0], intervals)

    def test_rejects_bad_addresses_before_network_with_one_fixed_error(self):
        invalid = (None, "", "x" * 501, "https://example.test/place", "Paris\nFrance", "\ud800")
        for address in invalid:
            with self.subTest(address=repr(address)):
                transport = FakeTransport([])
                client = OpenRoutingClient(transport=transport, clock=FakeClock(), sleeper=lambda _: None)
                with self.assertRaises(OpenRoutingError) as raised:
                    client.compute_route(address, "Lyon")
                self.assertEqual("open routing request failed", str(raised.exception))
                self.assertEqual([], transport.calls)

    def test_validates_both_addresses_before_any_network_departure(self):
        transport = FakeTransport([geocode("Public origin", "48", "2")])
        client = OpenRoutingClient(
            transport=transport, clock=FakeClock(), sleeper=lambda _: None
        )
        with self.assertRaises(OpenRoutingError):
            client.compute_route("Public origin", "https://invalid.example/place")
        self.assertEqual([], transport.calls)

    def test_colon_in_plain_address_is_not_mistaken_for_a_url(self):
        clock = FakeClock()
        transport = FakeTransport(
            [geocode("Building A", "48", "2"), geocode("Lyon", "46", "5"), route(60)]
        )
        client = OpenRoutingClient(transport=transport, clock=clock, sleeper=clock.sleep)
        client.compute_route("Main site: building A", "Lyon")
        self.assertIn("q=Main+site%3A+building+A", transport.calls[0][1])

    def test_rejects_empty_malformed_nonfinite_and_invalid_remote_values(self):
        bad_geocodes = (
            [],
            {},
            [{"display_name": "Paris", "lat": "NaN", "lon": "2"}],
            [{"display_name": "Paris", "lat": "91", "lon": "2"}],
            [{"display_name": "", "lat": "48", "lon": "2"}],
        )
        for body in bad_geocodes:
            with self.subTest(body=body):
                clock = FakeClock()
                client = OpenRoutingClient(
                    transport=FakeTransport([HttpResponse(200, {}, body)]),
                    clock=clock,
                    sleeper=clock.sleep,
                )
                with self.assertRaises(OpenRoutingError):
                    client.compute_route("Paris", "Lyon")

        for body in ({"code": "NoRoute"}, {"code": "Ok", "routes": []}, {"code": "Ok", "routes": [{"duration": math.inf}]}):
            with self.subTest(route=body):
                clock = FakeClock()
                client = OpenRoutingClient(
                    transport=FakeTransport([geocode("Paris", "48", "2"), geocode("Lyon", "46", "5"), HttpResponse(200, {}, body)]),
                    clock=clock,
                    sleeper=clock.sleep,
                )
                with self.assertRaises(OpenRoutingError):
                    client.compute_route("Paris", "Lyon")

    def test_failures_are_not_retried_or_reflected(self):
        secret = "Private Street 123"
        transport = FakeTransport([RuntimeError(secret)])
        client = OpenRoutingClient(transport=transport, clock=FakeClock(), sleeper=lambda _: None)
        with self.assertRaises(OpenRoutingError) as raised:
            client.compute_route(secret, "Lyon")
        self.assertEqual(1, len(transport.calls))
        formatted = "".join(traceback.format_exception(type(raised.exception), raised.exception, raised.exception.__traceback__))
        self.assertNotIn(secret, formatted)


class FakeUrlResponse:
    def __init__(self, body, headers=None):
        self.status = 200
        self.headers = headers or {}
        self.body = io.BytesIO(body)

    def read(self, size=-1):
        return self.body.read(size)

    def close(self):
        pass


class OpenDataTransportTests(unittest.TestCase):
    def test_uses_fifteen_second_timeout_and_one_mebibyte_bound(self):
        transport = _OpenDataTransport()
        transport._opener = mock.Mock()
        transport._opener.open.return_value = FakeUrlResponse(b"[]")
        response = transport.request("GET", "https://nominatim.openstreetmap.org/search", {})
        self.assertEqual([], response.body)
        self.assertEqual(15.0, transport._opener.open.call_args.kwargs["timeout"])

        transport._opener.open.return_value = FakeUrlResponse(
            b"[]", {"Content-Length": str(_MAX_RESPONSE_BYTES + 1)}
        )
        with self.assertRaises(OpenRoutingError):
            transport.request("GET", "https://nominatim.openstreetmap.org/search", {})

    def test_redirect_handler_never_creates_a_followup_request(self):
        handler = _NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(
                mock.Mock(), None, 302, "Found", {}, "https://nominatim.openstreetmap.org/other"
            )
        )

    def test_invalid_json_nonfinite_and_http_errors_are_fixed(self):
        for response in (
            FakeUrlResponse(b""),
            FakeUrlResponse(b'{"value":NaN}'),
            urllib.error.HTTPError("https://example.test", 503, "private", {}, None),
        ):
            transport = _OpenDataTransport()
            transport._opener = mock.Mock()
            transport._opener.open.side_effect = response if isinstance(response, Exception) else None
            if not isinstance(response, Exception):
                transport._opener.open.return_value = response
            with self.assertRaises(OpenRoutingError) as raised:
                transport.request("GET", "https://nominatim.openstreetmap.org/search", {})
            self.assertEqual("open routing request failed", str(raised.exception))

    def test_response_cleanup_failure_keeps_a_fixed_redacted_error(self):
        import traceback
        class BrokenClose(FakeUrlResponse):
            def close(self):
                raise OSError("PRIVATE_RESPONSE_CLEANUP")
        for raw in (b"[]", b"invalid"):
            transport = _OpenDataTransport()
            transport._opener = mock.Mock()
            transport._opener.open.return_value = BrokenClose(raw)
            try:
                transport.request("GET", "https://nominatim.openstreetmap.org/search", {})
            except OpenRoutingError as error:
                self.assertEqual("open routing request failed", str(error))
                self.assertNotIn("PRIVATE_RESPONSE_CLEANUP", traceback.format_exc())
            else:
                self.fail("Cleanup failure must fail closed")


if __name__ == "__main__":
    unittest.main()
