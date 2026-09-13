"""Bounded Nominatim and OSRM routing for the lightweight ÆON demo."""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import socket
import threading
import time
from typing import Any, Callable, Dict, Mapping
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


_PROVIDER = "osrm"
_USER_AGENT = "AEON-Hackathon/1.0 (+https://github.com/xamathia/hackathon-AEON)"
_NOMINATIM_HOST = "nominatim.openstreetmap.org"
_OSRM_HOST = "routing.openstreetmap.de"
_MAX_RESPONSE_BYTES = 1_048_576
_TIMEOUT_SECONDS = 15.0
_CACHE_SIZE = 64
_RATE_SECONDS = 1.0
_FAILURE_MESSAGE = "open routing request failed"
_RATE_LOCK = threading.Lock()
_LAST_DEPARTURE: Dict[str, float] = {}


class OpenRoutingError(Exception):
    """Stable public failure that never reflects an address or remote response."""

    def __init__(self) -> None:
        super().__init__(_FAILURE_MESSAGE)


class OpenRoutingClient:
    """Geocode two explicit addresses and observe one directed driving route."""

    def __init__(
        self,
        *,
        transport: Any = None,
        clock: Callable[[], datetime] = None,
        sleeper: Callable[[float], None] = None,
    ) -> None:
        self._transport = transport if transport is not None else _OpenDataTransport()
        self._clock = clock if clock is not None else _utc_now
        self._sleeper = sleeper if sleeper is not None else time.sleep
        if not callable(self._clock) or not callable(self._sleeper):
            raise OpenRoutingError()
        self._geocode_cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()

    def compute_route(self, origin_address: str, destination_address: str) -> dict:
        """Return one bounded OSRM observation without inferring reverse travel."""
        try:
            normalized_origin = _address(origin_address)
            normalized_destination = _address(destination_address)
            origin = self._geocode(normalized_origin)
            destination = self._geocode(normalized_destination)
            route = self._route(origin, destination)
            observed_at = _aware_now(self._clock)
            duration_minutes = route / 60.0
            digest = hashlib.sha256(
                json.dumps(
                    [origin["longitude"], origin["latitude"], destination["longitude"], destination["latitude"]],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            return {
                "duration_minutes": {
                    "min": duration_minutes * 0.8,
                    "mode": duration_minutes,
                    "max": duration_minutes * 1.4,
                },
                "observed_at": _utc_iso(observed_at),
                "origin_label": origin["label"],
                "destination_label": destination["label"],
                "source": {
                    "provider": _PROVIDER,
                    "reference": f"osrm:{digest}",
                    "synthetic": False,
                    "assumption": (
                        "OSRM route without live traffic; uncalibrated ÆON triangular "
                        "distribution (0.8/1/1.4); Nominatim matches require verification"
                    ),
                },
            }
        except OpenRoutingError:
            raise
        except Exception:
            raise OpenRoutingError() from None

    def _geocode(self, address: str) -> Dict[str, Any]:
        cached = self._geocode_cache.get(address)
        if cached is not None:
            return dict(cached)
        query = urllib.parse.urlencode(
            (("format", "jsonv2"), ("limit", "1"), ("q", address))
        )
        body = self._request(
            _NOMINATIM_HOST,
            f"https://{_NOMINATIM_HOST}/search?{query}",
        )
        if not isinstance(body, list) or not body or not isinstance(body[0], dict):
            raise OpenRoutingError()
        item = body[0]
        result = {
            "latitude": _coordinate(item.get("lat"), -90.0, 90.0),
            "longitude": _coordinate(item.get("lon"), -180.0, 180.0),
            "label": _label(item.get("display_name")),
        }
        self._geocode_cache[address] = result
        if len(self._geocode_cache) > _CACHE_SIZE:
            self._geocode_cache.popitem(last=False)
        return dict(result)

    def _route(self, origin: Mapping[str, Any], destination: Mapping[str, Any]) -> float:
        coordinates = ";".join(
            (
                f'{_coordinate_text(origin["longitude"])},{_coordinate_text(origin["latitude"])}',
                f'{_coordinate_text(destination["longitude"])},{_coordinate_text(destination["latitude"])}',
            )
        )
        body = self._request(
            _OSRM_HOST,
            f"https://{_OSRM_HOST}/routed-car/route/v1/driving/{coordinates}?overview=false&steps=false",
        )
        if not isinstance(body, dict) or body.get("code") != "Ok":
            raise OpenRoutingError()
        routes = body.get("routes")
        if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
            raise OpenRoutingError()
        return _duration(routes[0].get("duration"))

    def _request(self, domain: str, url: str) -> Any:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != domain or parsed.username is not None:
            raise OpenRoutingError()
        _reserve_departure(domain, self._clock, self._sleeper)
        try:
            response = self._transport.request(
                "GET",
                url,
                {"Accept": "application/json", "User-Agent": _USER_AGENT},
            )
        except Exception:
            raise OpenRoutingError() from None
        status = getattr(response, "status", None)
        if isinstance(status, bool) or not isinstance(status, int) or status != 200:
            raise OpenRoutingError()
        return getattr(response, "body", None)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _OpenDataTransport:
    """One-shot JSON GET transport accepting Nominatim arrays and OSRM objects."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def request(self, method: str, url: str, headers: Mapping[str, str]):
        if method != "GET":
            raise OpenRoutingError()
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        response = None
        try:
            response = self._opener.open(request, timeout=_TIMEOUT_SECONDS)
            raw = _bounded_read(response)
            body = json.loads(raw.decode("utf-8"), parse_constant=_invalid_constant)
            return _Response(int(response.status), body)
        except urllib.error.HTTPError as exc:
            response = exc
            raise OpenRoutingError() from None
        except (OpenRoutingError, UnicodeDecodeError, ValueError, TypeError):
            raise OpenRoutingError() from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise OpenRoutingError() from None
        finally:
            if response is not None:
                try:
                    close = getattr(response, "close", None)
                    if close is not None:
                        close()
                except Exception:
                    # Cleanup must not expose a provider exception or replace redaction.
                    raise OpenRoutingError() from None


class _Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body


def _bounded_read(response: Any) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_RESPONSE_BYTES:
                raise OpenRoutingError()
        except ValueError:
            pass
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise OpenRoutingError()
    return raw


def _address(value: Any) -> str:
    if not isinstance(value, str):
        raise OpenRoutingError()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise OpenRoutingError() from None
    result = value.strip()
    if not 1 <= len(result) <= 500:
        raise OpenRoutingError()
    if any(unicodedata.category(character).startswith("C") for character in result):
        raise OpenRoutingError()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", result) or re.match(
        r"^(?:https?|ftp|mailto):", result, re.IGNORECASE
    ):
        raise OpenRoutingError()
    return result


def _coordinate(value: Any, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise OpenRoutingError()
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise OpenRoutingError() from None
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise OpenRoutingError()
    return result


def _coordinate_text(value: Any) -> str:
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


def _label(value: Any) -> str:
    if not isinstance(value, str):
        raise OpenRoutingError()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise OpenRoutingError() from None
    result = value.strip()
    if not result or len(result) > 1_000:
        raise OpenRoutingError()
    if any(unicodedata.category(character).startswith("C") for character in result):
        raise OpenRoutingError()
    return result


def _duration(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OpenRoutingError()
    try:
        result = float(value)
    except OverflowError:
        raise OpenRoutingError() from None
    if not math.isfinite(result) or result < 0:
        raise OpenRoutingError()
    return result


def _reserve_departure(
    domain: str,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
) -> None:
    while True:
        now = _aware_now(clock).timestamp()
        with _RATE_LOCK:
            previous = _LAST_DEPARTURE.get(domain)
            if previous is None or now >= previous + _RATE_SECONDS:
                _LAST_DEPARTURE[domain] = now
                return
            delay = previous + _RATE_SECONDS - now
        try:
            sleeper(delay)
        except Exception:
            raise OpenRoutingError() from None


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
    except Exception:
        raise OpenRoutingError() from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OpenRoutingError()
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise OpenRoutingError() from None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _invalid_constant(value: str) -> None:
    raise ValueError(value)
