from datetime import datetime, timezone
import math
import unittest

from packages.aeon_connectors import (
    ConnectorError,
    GoogleRoutesClient,
    HttpResponse,
    route_to_prior,
)


class FakeTransport:
    def __init__(self, response=None):
        self.response = response or HttpResponse(200, {}, {})
        self.calls = []

    def request(self, *args):
        self.calls.append(args)
        return self.response


class GoogleRoutesClientTests(unittest.TestCase):
    def test_computes_traffic_route_with_decimal_seconds_and_stable_source(self):
        transport = FakeTransport(
            HttpResponse(
                200,
                {},
                {"routes": [{"duration": "901.250s", "staticDuration": "840s", "distanceMeters": 12345}]},
            )
        )
        clock = lambda: datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        client = GoogleRoutesClient("api-key-value", transport, clock)
        result = client.compute_route(
            {"address": "10 rue de Rivoli, Paris"},
            {"location": {"latLng": {"latitude": 48.8584, "longitude": 2.2945}}},
            "2026-01-01T13:00:00+01:00",
        )
        method, url, headers, body = transport.calls[0]
        self.assertEqual("POST", method)
        self.assertEqual("https://routes.googleapis.com/directions/v2:computeRoutes", url)
        self.assertEqual("api-key-value", headers["X-Goog-Api-Key"])
        self.assertEqual(
            "routes.duration,routes.staticDuration,routes.distanceMeters",
            headers["X-Goog-FieldMask"],
        )
        self.assertEqual("DRIVE", body["travelMode"])
        self.assertEqual("TRAFFIC_AWARE", body["routingPreference"])
        self.assertEqual("2026-01-01T13:00:00+01:00", body["departureTime"])
        self.assertEqual(901.25, result["duration_seconds"])
        self.assertEqual(840.0, result["static_duration_seconds"])
        self.assertEqual(12345, result["distance_meters"])
        self.assertEqual("2026-01-01T12:00:00Z", result["observed_at"])
        self.assertEqual("google_routes", result["source"]["provider"])
        self.assertFalse(result["source"]["synthetic"])
        again = client.compute_route(
            {"address": "10 rue de Rivoli, Paris"},
            {"location": {"latLng": {"latitude": 48.8584, "longitude": 2.2945}}},
            "2026-01-01T13:00:00+01:00",
        )
        self.assertEqual(result["source"]["reference"], again["source"]["reference"])

    def test_no_route_is_not_a_zero_duration(self):
        client = GoogleRoutesClient("key", FakeTransport(HttpResponse(200, {}, {"routes": []})))
        with self.assertRaises(ConnectorError) as raised:
            client.compute_route(
                {"address": "Paris"}, {"address": "Lyon"}, "2026-01-01T12:00:00Z"
            )
        self.assertEqual("no_route", raised.exception.code)

    def test_rejects_urls_invalid_coordinates_times_and_modes(self):
        valid_origin = {"address": "Paris"}
        valid_destination = {"address": "Lyon"}
        invalid_cases = [
            ({"address": "https://example.test/private"}, valid_destination, "2026-01-01T12:00:00Z", "DRIVE"),
            ({"location": {"latLng": {"latitude": 91, "longitude": 2}}}, valid_destination, "2026-01-01T12:00:00Z", "DRIVE"),
            (valid_origin, valid_destination, "2026-01-01T12:00:00", "DRIVE"),
            (valid_origin, valid_destination, "2026-01-01T12:00:00Z", "WALK"),
        ]
        for origin, destination, departure, mode in invalid_cases:
            with self.subTest(origin=origin, mode=mode), self.assertRaises(ConnectorError) as raised:
                GoogleRoutesClient("key", FakeTransport()).compute_route(
                    origin, destination, departure, travel_mode=mode
                )
            self.assertEqual("invalid_arguments", raised.exception.code)

    def test_invalid_duration_and_clock_are_explicit(self):
        for route in (
            {"duration": "NaNs", "staticDuration": "1s", "distanceMeters": 1},
            {"duration": "1e3s", "staticDuration": "1s", "distanceMeters": 1},
            {"duration": "1.1234567890s", "staticDuration": "1s", "distanceMeters": 1},
            {"duration": "1s", "staticDuration": "1s", "distanceMeters": -1},
            {"duration": "1s", "staticDuration": "1s", "distanceMeters": 1.5},
        ):
            with self.subTest(route=route), self.assertRaises(ConnectorError) as raised:
                GoogleRoutesClient("key", FakeTransport(HttpResponse(200, {}, {"routes": [route]}))).compute_route(
                    {"address": "Paris"}, {"address": "Lyon"}, "2026-01-01T12:00:00Z"
                )
            self.assertEqual("invalid_response", raised.exception.code)

        route = {"duration": "1s", "staticDuration": "1s", "distanceMeters": 1}
        with self.assertRaises(ConnectorError) as raised:
            GoogleRoutesClient(
                "key", FakeTransport(HttpResponse(200, {}, {"routes": [route]})), lambda: datetime(2026, 1, 1)
            ).compute_route({"address": "Paris"}, {"address": "Lyon"}, "2026-01-01T12:00:00Z")
        self.assertEqual("invalid_clock", raised.exception.code)


class RoutePriorTests(unittest.TestCase):
    def test_derives_explicit_non_calibrated_triangular_prior(self):
        observation = {
            "duration_seconds": 600,
            "source": {
                "provider": "google_routes",
                "reference": "route:stable",
                "synthetic": False,
                "assumption": "point observation",
            },
        }
        prior = route_to_prior(observation, lower_factor=0.5, upper_factor=1.5)
        self.assertEqual({"min": 5.0, "mode": 10.0, "max": 15.0}, prior["duration_minutes"])
        self.assertEqual("route:stable", prior["source"]["reference"])
        self.assertIn("uncalibrated ÆON triangular distribution", prior["source"]["assumption"])
        self.assertIn("lower=0.5, upper=1.5", prior["source"]["assumption"])
        self.assertEqual("point observation", observation["source"]["assumption"])

    def test_rejects_invalid_factors_and_observations(self):
        source = {"provider": "google_routes", "reference": "r", "synthetic": False}
        for seconds, lower, upper in (
            (60, 1.1, 1.4),
            (60, 0.8, 0.9),
            (-1, 0.8, 1.4),
            (math.inf, 0.8, 1.4),
            (True, 0.8, 1.4),
        ):
            with self.subTest(seconds=seconds, lower=lower, upper=upper), self.assertRaises(ValueError):
                route_to_prior(
                    {"duration_seconds": seconds, "source": source},
                    lower_factor=lower,
                    upper_factor=upper,
                )


if __name__ == "__main__":
    unittest.main()
