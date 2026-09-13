"""Validation for the bounded ÆON private-plan search context."""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Dict, Mapping, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_PROVIDERS = {"google_calendar", "gmail", "google_routes", "osrm", "user"}


@dataclass(frozen=True)
class Route:
    from_location_id: str
    to_location_id: str
    duration_minutes: Dict[str, float]
    source: Dict[str, Any]


@dataclass(frozen=True)
class PlanningContext:
    target_event_id: str
    movable_event_ids: Tuple[str, ...]
    location_by_event_id: Dict[str, str]
    routes: Dict[Tuple[str, str], Route]
    step_minutes: int
    max_candidates: int


@dataclass(frozen=True)
class LiveSelection:
    target_event_id: str
    movable_event_ids: Tuple[str, ...]
    window_start: datetime
    window_end: datetime
    step_minutes: int
    max_candidates: int


def validate_live_selection(
    payload: Mapping[str, Any], selection: Mapping[str, Any]
) -> LiveSelection:
    """Validate the exact user-controlled bounds for a live plan search."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if payload.get("mode") != "live":
        raise ValueError("payload.mode must be live")
    if not isinstance(selection, dict) or set(selection) != {
        "target_event_id",
        "movable_event_ids",
        "window",
        "step_minutes",
        "max_candidates",
    }:
        raise ValueError("selection must contain exactly the live planning fields")
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("payload.events must be an array")
    event_by_id = {}
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"payload.events[{index}] must be an object")
        event_id = _non_empty_string(event.get("id"), f"payload.events[{index}].id")
        if event_id in event_by_id:
            raise ValueError("payload event ids must be unique")
        event_by_id[event_id] = event

    target = _non_empty_string(selection["target_event_id"], "selection.target_event_id")
    if target not in event_by_id:
        raise ValueError("selection.target_event_id references an unknown event")
    movable_raw = selection["movable_event_ids"]
    if not isinstance(movable_raw, list) or not 1 <= len(movable_raw) <= 5:
        raise ValueError("selection.movable_event_ids must contain between 1 and 5 items")
    movable = tuple(
        _non_empty_string(value, f"selection.movable_event_ids[{index}]")
        for index, value in enumerate(movable_raw)
    )
    if len(set(movable)) != len(movable):
        raise ValueError("selection.movable_event_ids must not contain duplicates")
    if target in movable:
        raise ValueError("selection target must not be movable")
    for event_id in movable:
        event = event_by_id.get(event_id)
        if event is None:
            raise ValueError(f"movable event {event_id!r} does not exist")
        if event.get("private") is not True or event.get("classification") != "flexible":
            raise ValueError(f"movable event {event_id!r} must be private and flexible")

    window = selection["window"]
    if not isinstance(window, dict) or set(window) != {"start", "end"}:
        raise ValueError("selection.window must contain exactly start and end")
    window_start = _instant(window["start"], "selection.window.start")
    window_end = _instant(window["end"], "selection.window.end")
    horizon = payload.get("horizon")
    if not isinstance(horizon, dict):
        raise ValueError("payload.horizon must be an object")
    horizon_start = _instant(horizon.get("start"), "payload.horizon.start")
    horizon_end = _instant(horizon.get("end"), "payload.horizon.end")
    duration = (window_end - window_start).total_seconds()
    if duration <= 0 or duration > 24 * 60 * 60:
        raise ValueError("selection.window must be positive and no longer than 24 hours")
    if window_start < horizon_start or window_end > horizon_end:
        raise ValueError("selection.window must be inside the payload horizon")
    _timezone(payload.get("timezone"))
    return LiveSelection(
        target_event_id=target,
        movable_event_ids=movable,
        window_start=window_start,
        window_end=window_end,
        step_minutes=_bounded_integer(
            selection["step_minutes"], "selection.step_minutes", 5, 60
        ),
        max_candidates=_bounded_integer(
            selection["max_candidates"], "selection.max_candidates", 1, 100
        ),
    )


def validate_context(payload: Mapping[str, Any], context: Mapping[str, Any]) -> PlanningContext:
    """Validate planner-specific inputs without changing either argument."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if not isinstance(context, dict):
        raise ValueError("context must be a JSON object")
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("payload.events must be an array")
    event_by_id = {}
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"payload.events[{index}] must be an object")
        event_id = _non_empty_string(event.get("id"), f"payload.events[{index}].id")
        if event_id in event_by_id:
            raise ValueError("payload event ids must be unique")
        event_by_id[event_id] = event

    target_event_id = _non_empty_string(context.get("target_event_id"), "context.target_event_id")
    if target_event_id not in event_by_id:
        raise ValueError("context.target_event_id references an unknown event")

    movable_raw = context.get("movable_event_ids")
    if not isinstance(movable_raw, list):
        raise ValueError("context.movable_event_ids must be an array")
    movable_event_ids = tuple(
        _non_empty_string(value, f"context.movable_event_ids[{index}]")
        for index, value in enumerate(movable_raw)
    )
    if len(set(movable_event_ids)) != len(movable_event_ids):
        raise ValueError("context.movable_event_ids must not contain duplicates")
    for event_id in movable_event_ids:
        event = event_by_id.get(event_id)
        if event is None:
            raise ValueError(f"movable event {event_id!r} does not exist")
        if event.get("private") is not True or event.get("classification") != "flexible":
            raise ValueError(f"movable event {event_id!r} must be private and flexible")

    locations_raw = context.get("location_by_event_id")
    if not isinstance(locations_raw, dict):
        raise ValueError("context.location_by_event_id must be an object")
    locations = {}
    for event_id in event_by_id:
        if event_id not in locations_raw:
            raise ValueError(f"context.location_by_event_id is missing event {event_id!r}")
        locations[event_id] = _non_empty_string(
            locations_raw[event_id], f"context.location_by_event_id[{event_id!r}]"
        )

    routes_raw = context.get("route_catalog")
    if not isinstance(routes_raw, list):
        raise ValueError("context.route_catalog must be an array")
    routes = {}
    payload_mode = payload.get("mode")
    for index, raw_route in enumerate(routes_raw):
        path = f"context.route_catalog[{index}]"
        if not isinstance(raw_route, dict):
            raise ValueError(f"{path} must be an object")
        left = _non_empty_string(raw_route.get("from_location_id"), f"{path}.from_location_id")
        right = _non_empty_string(raw_route.get("to_location_id"), f"{path}.to_location_id")
        pair = (left, right)
        if pair in routes:
            raise ValueError(f"{path} duplicates the oriented location pair")
        duration = _distribution(raw_route.get("duration_minutes"), f"{path}.duration_minutes")
        source = _source(raw_route.get("source"), f"{path}.source", payload_mode)
        if left == right and (
            duration != {"min": 0.0, "mode": 0.0, "max": 0.0}
            or source["provider"] != "user"
        ):
            raise ValueError(
                f"{path} for one declared location must be constant zero with a user source"
            )
        routes[pair] = Route(left, right, duration, source)

    step_minutes = _bounded_integer(context.get("step_minutes", 15), "context.step_minutes", 5, 60)
    max_candidates = _bounded_integer(
        context.get("max_candidates", 60), "context.max_candidates", 1, 100
    )
    return PlanningContext(
        target_event_id=target_event_id,
        movable_event_ids=movable_event_ids,
        location_by_event_id=locations,
        routes=routes,
        step_minutes=step_minutes,
        max_candidates=max_candidates,
    )


def _distribution(value: Any, path: str) -> Dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    result = {
        key: _finite_number(value.get(key), f"{path}.{key}")
        for key in ("min", "mode", "max")
    }
    if result["min"] < 0 or not result["min"] <= result["mode"] <= result["max"]:
        raise ValueError(f"{path} must satisfy 0 <= min <= mode <= max")
    return result


def _source(value: Any, path: str, payload_mode: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    provider = value.get("provider")
    if not isinstance(provider, str) or provider not in _PROVIDERS:
        raise ValueError(f"{path}.provider is not supported")
    synthetic = value.get("synthetic")
    if not isinstance(synthetic, bool):
        raise ValueError(f"{path}.synthetic must be a boolean")
    if payload_mode == "synthetic" and not synthetic:
        raise ValueError(f"{path}.synthetic must be true for a synthetic scenario")
    return {
        "provider": provider,
        "reference": _non_empty_string(value.get("reference"), f"{path}.reference"),
        "synthetic": synthetic,
        "assumption": _string(value.get("assumption"), f"{path}.assumption"),
    }


def _non_empty_string(value: Any, path: str) -> str:
    result = _string(value, path)
    if not result.strip():
        raise ValueError(f"{path} must be non-empty")
    return result


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{path} must contain valid Unicode") from exc
    return value


def _finite_number(value: Any, path: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{path} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{path} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{path} must be a finite number")
    return result


def _bounded_integer(value: Any, path: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{path} must be an integer between {minimum} and {maximum}")
    return value


def _instant(value: Any, path: str) -> datetime:
    text = _non_empty_string(value, path)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError(f"{path} must be an ISO 8601 datetime") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{path} must include an explicit UTC offset or Z")
    try:
        return result.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ValueError(f"{path} must be representable in UTC") from None


def _timezone(value: Any) -> str:
    name = _non_empty_string(value, "payload.timezone")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError("payload.timezone must be a valid IANA identifier") from None
    return name
