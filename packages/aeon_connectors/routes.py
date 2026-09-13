"""Google Routes observation adapter and explicit ÆON prior derivation."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from typing import Any, Callable, Dict
import urllib.parse

from ._google import perform_request, require_secret
from .transport import ConnectorError, HttpTransport


_PROVIDER = "google_routes"
_FIELD_MASK = "routes.duration,routes.staticDuration,routes.distanceMeters"
_DURATION_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,9})?s$")


class GoogleRoutesClient:
    def __init__(
        self,
        api_key: str,
        transport: Any = None,
        clock: Callable[[], datetime] = None,
    ) -> None:
        self._api_key = require_secret(api_key, "api_key")
        self._transport = transport if transport is not None else HttpTransport()
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def compute_route(
        self,
        origin: dict,
        destination: dict,
        departure_time: str,
        *,
        travel_mode: str = "DRIVE",
    ) -> dict:
        normalized_origin = _waypoint(origin, "origin")
        normalized_destination = _waypoint(destination, "destination")
        _departure_time(departure_time)
        if travel_mode != "DRIVE":
            raise ConnectorError(
                _PROVIDER,
                "invalid_arguments",
                False,
                "only DRIVE supports the configured traffic-aware route",
            )
        request_body = {
            "origin": normalized_origin,
            "destination": normalized_destination,
            "travelMode": travel_mode,
            "routingPreference": "TRAFFIC_AWARE",
            "departureTime": departure_time,
        }
        response = perform_request(
            _PROVIDER,
            self._transport,
            "POST",
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            {"X-Goog-Api-Key": self._api_key, "X-Goog-FieldMask": _FIELD_MASK},
            request_body,
        )
        routes = response.body.get("routes", [])
        if not isinstance(routes, list):
            raise ConnectorError(_PROVIDER, "invalid_response", False, "Routes response is invalid")
        if not routes:
            raise ConnectorError(_PROVIDER, "no_route", False, "Google returned no route")
        route = routes[0]
        if not isinstance(route, dict):
            raise ConnectorError(_PROVIDER, "invalid_response", False, "Routes response is invalid")
        duration = _duration(route.get("duration"), "duration")
        static_duration = _duration(route.get("staticDuration"), "staticDuration")
        distance = route.get("distanceMeters")
        if isinstance(distance, bool) or not isinstance(distance, int):
            raise ConnectorError(_PROVIDER, "invalid_response", False, "distanceMeters is invalid")
        if distance < 0:
            raise ConnectorError(_PROVIDER, "invalid_response", False, "distanceMeters is invalid")
        observed_at = self._clock()
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise ConnectorError(_PROVIDER, "invalid_clock", False, "clock must return an aware datetime")
        reference_payload = {
            "origin": normalized_origin,
            "destination": normalized_destination,
            "departure_time": departure_time,
            "travel_mode": travel_mode,
        }
        digest = hashlib.sha256(
            json.dumps(
                reference_payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "duration_seconds": duration,
            "static_duration_seconds": static_duration,
            "distance_meters": distance,
            "observed_at": _utc_iso(observed_at),
            "source": {
                "provider": _PROVIDER,
                "reference": f"google_routes:{digest}",
                "synthetic": False,
                "assumption": "Google Routes point observation, not a quantile",
            },
        }


def route_to_prior(
    observation: dict,
    *,
    lower_factor: float = 0.8,
    upper_factor: float = 1.4,
) -> dict:
    if not isinstance(observation, dict):
        raise ValueError("observation must be an object")
    lower = _factor(lower_factor, "lower_factor")
    upper = _factor(upper_factor, "upper_factor")
    if not 0 <= lower <= 1 <= upper:
        raise ValueError("factors must satisfy 0 <= lower_factor <= 1 <= upper_factor")
    seconds = observation.get("duration_seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("observation.duration_seconds must be a finite non-negative number")
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("observation.duration_seconds must be a finite non-negative number")
    raw_source = observation.get("source")
    if not isinstance(raw_source, dict):
        raise ValueError("observation.source must be an object")
    source = dict(raw_source)
    if not isinstance(source.get("provider"), str) or not source["provider"]:
        raise ValueError("observation.source.provider must be a non-empty string")
    if not isinstance(source.get("reference"), str) or not source["reference"]:
        raise ValueError("observation.source.reference must be a non-empty string")
    if not isinstance(source.get("synthetic"), bool):
        raise ValueError("observation.source.synthetic must be a boolean")
    previous_assumption = source.get("assumption", "")
    if not isinstance(previous_assumption, str):
        raise ValueError("observation.source.assumption must be a string")
    factors = f"lower={lower:g}, upper={upper:g}"
    derived = f"uncalibrated ÆON triangular distribution, factors {factors}"
    source["assumption"] = (
        f"{derived}; observation: {previous_assumption}"
        if previous_assumption
        else derived
    )
    base = seconds / 60.0
    return {
        "duration_minutes": {
            "min": base * lower,
            "mode": base,
            "max": base * upper,
        },
        "source": source,
    }


def _waypoint(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ConnectorError(_PROVIDER, "invalid_arguments", False, f"{name} is invalid")
    has_address = "address" in value
    has_location = "location" in value
    if has_address == has_location or set(value) - {"address", "location"}:
        raise ConnectorError(_PROVIDER, "invalid_arguments", False, f"{name} is invalid")
    if has_address:
        address = value["address"]
        if not isinstance(address, str) or not address.strip() or _looks_like_url(address):
            raise ConnectorError(_PROVIDER, "invalid_arguments", False, f"{name} address is invalid")
        return {"address": address}
    location = value["location"]
    if not isinstance(location, dict) or set(location) != {"latLng"}:
        raise ConnectorError(_PROVIDER, "invalid_arguments", False, f"{name} coordinates are invalid")
    lat_lng = location["latLng"]
    if not isinstance(lat_lng, dict) or set(lat_lng) != {"latitude", "longitude"}:
        raise ConnectorError(_PROVIDER, "invalid_arguments", False, f"{name} coordinates are invalid")
    latitude = _coordinate(lat_lng["latitude"], -90, 90, name)
    longitude = _coordinate(lat_lng["longitude"], -180, 180, name)
    return {"location": {"latLng": {"latitude": latitude, "longitude": longitude}}}


def _coordinate(value: Any, minimum: float, maximum: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConnectorError(_PROVIDER, "invalid_arguments", False, f"{name} coordinates are invalid")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConnectorError(_PROVIDER, "invalid_arguments", False, f"{name} coordinates are invalid")
    return value


def _looks_like_url(value: str) -> bool:
    parsed = urllib.parse.urlsplit(value.strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _departure_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ConnectorError(_PROVIDER, "invalid_arguments", False, "departure_time is invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError:
        raise ConnectorError(_PROVIDER, "invalid_arguments", False, "departure_time is invalid") from None
    if result.tzinfo is None:
        raise ConnectorError(_PROVIDER, "invalid_arguments", False, "departure_time needs an offset")
    return result


def _duration(value: Any, field: str) -> float:
    if not isinstance(value, str) or _DURATION_PATTERN.fullmatch(value) is None:
        raise ConnectorError(_PROVIDER, "invalid_response", False, f"{field} is invalid")
    try:
        seconds = Decimal(value[:-1])
    except InvalidOperation:
        raise ConnectorError(_PROVIDER, "invalid_response", False, f"{field} is invalid") from None
    if not seconds.is_finite() or seconds < 0:
        raise ConnectorError(_PROVIDER, "invalid_response", False, f"{field} is invalid")
    result = float(seconds)
    if not math.isfinite(result):
        raise ConnectorError(_PROVIDER, "invalid_response", False, f"{field} is invalid")
    return result


def _factor(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
