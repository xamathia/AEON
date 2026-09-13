"""Assemble trusted observations into a canonical engine scenario."""

from __future__ import annotations

from datetime import datetime, timezone as utc_timezone
import math
from typing import Any, Dict, List, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from packages.aeon_engine.validation import validate_payload


_MAX_EVENTS = 100
_MAX_HORIZON_SECONDS = 7 * 24 * 60 * 60
_VALIDATION_REFERENCE_PREFIX = "aeon_scenario:missing-edge-validation:"


def assemble_scenario(
    *,
    scenario_id: Any,
    timezone: Any,
    horizon: Any,
    calendar: Any,
    travel_edges: Any,
    constraints: Any,
    seed: Any = 42,
    samples: Any = 1000,
) -> dict:
    """Return a validated live scenario, or deterministic completeness issues."""
    inputs = _json_copy(
        {
            "scenario_id": scenario_id,
            "timezone": timezone,
            "horizon": horizon,
            "calendar": calendar,
            "travel_edges": travel_edges,
            "constraints": constraints,
            "seed": seed,
            "samples": samples,
        }
    )
    scenario_id = _non_empty_string(inputs["scenario_id"], "scenario_id")
    timezone_name = _timezone(inputs["timezone"])
    horizon_value, horizon_start, horizon_end = _horizon(inputs["horizon"])
    seed_value = _bounded_integer(inputs["seed"], "seed", 0, 2**32 - 1)
    samples_value = _bounded_integer(inputs["samples"], "samples", 1, 10_000)

    calendar_value = _object(inputs["calendar"], "calendar")
    events_value = _array(calendar_value.get("events"), "calendar.events")
    if len(events_value) > _MAX_EVENTS:
        raise ValueError("calendar.events must contain at most 100 items")
    complete = calendar_value.get("complete")
    if not isinstance(complete, bool):
        raise ValueError("calendar.complete must be a boolean")
    read_at = _finite_number(calendar_value.get("read_at"), "calendar.read_at")

    ordered_events, event_ids = _events(
        events_value, horizon_start, horizon_end
    )
    expected_pairs = [
        (left["id"], right["id"])
        for left, right in zip(ordered_events, ordered_events[1:])
    ]
    ordered_edges, missing_pairs = _travel_edges(
        inputs["travel_edges"], expected_pairs
    )
    ordered_constraints = _constraints(
        inputs["constraints"], event_ids, horizon_start, horizon_end
    )

    issues = []
    if not complete:
        issues.append({"code": "CALENDAR_INCOMPLETE", "event_ids": []})
    if not ordered_events:
        issues.append({"code": "NO_EVENTS", "event_ids": []})
    issues.extend(
        {"code": "MISSING_TRAVEL_EDGE", "event_ids": list(pair)}
        for pair in missing_pairs
    )
    issues.sort(key=lambda issue: (issue["code"], issue["event_ids"]))

    assumptions: List[str] = []
    scenario = None
    if ordered_events:
        validation_edges, placeholder_references = _validation_edges(
            ordered_edges,
            missing_pairs,
            _source_references(ordered_events, ordered_edges, ordered_constraints),
        )
        candidate = {
            "schema_version": "1.0",
            "scenario_id": scenario_id,
            "mode": "live",
            "timezone": timezone_name,
            "horizon": horizon_value,
            "seed": seed_value,
            "samples": samples_value,
            "events": ordered_events,
            "travel_edges": validation_edges,
            "constraints": ordered_constraints,
        }
        validated = validate_payload(candidate)
        assumptions = sorted(
            {
                source.assumption
                for source in validated.sources
                if source.reference not in placeholder_references
                and source.assumption
            }
        )
        _require_explicit_zero_assumptions(ordered_edges)
        if not issues:
            candidate["travel_edges"] = ordered_edges
            scenario = candidate

    return {
        "status": "ready" if scenario is not None else "incomplete",
        "scenario": scenario,
        "issues": issues,
        "provenance": {
            "calendar_read_at": read_at,
            "calendar_complete": complete,
        },
        "assumptions": assumptions,
    }


def _events(
    values: List[Any], horizon_start: datetime, horizon_end: datetime
) -> Tuple[List[dict], Set[str]]:
    result = []
    identifiers: Set[str] = set()
    for index, value in enumerate(values):
        path = f"calendar.events[{index}]"
        event = _object(value, path)
        event_id = _non_empty_string(event.get("id"), f"{path}.id")
        if event_id in identifiers:
            raise ValueError("calendar.events ids must be unique")
        identifiers.add(event_id)
        start = _instant(event.get("planned_start"), f"{path}.planned_start")
        end = _instant(event.get("planned_end"), f"{path}.planned_end")
        if end <= start:
            raise ValueError(f"{path} must have a positive planned duration")
        if start < horizon_start or end > horizon_end:
            raise ValueError(f"{path} must be entirely inside the horizon")
        result.append((start, event_id, event))
    result.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in result], identifiers


def _travel_edges(
    value: Any, expected_pairs: List[Tuple[str, str]]
) -> Tuple[List[dict], List[Tuple[str, str]]]:
    values = _array(value, "travel_edges")
    expected = set(expected_pairs)
    by_pair = {}
    for index, raw in enumerate(values):
        path = f"travel_edges[{index}]"
        edge = _object(raw, path)
        pair = (
            _non_empty_string(edge.get("from_event_id"), f"{path}.from_event_id"),
            _non_empty_string(edge.get("to_event_id"), f"{path}.to_event_id"),
        )
        if pair not in expected:
            raise ValueError(f"{path} must connect an adjacent event pair")
        if pair in by_pair:
            raise ValueError("travel_edges must not duplicate an adjacent pair")
        by_pair[pair] = edge
    missing = [pair for pair in expected_pairs if pair not in by_pair]
    return [by_pair[pair] for pair in expected_pairs if pair in by_pair], missing


def _constraints(
    value: Any,
    event_ids: Set[str],
    horizon_start: datetime,
    horizon_end: datetime,
) -> List[dict]:
    values = _array(value, "constraints")
    identifiers = set()
    result = []
    for index, raw in enumerate(values):
        path = f"constraints[{index}]"
        constraint = _object(raw, path)
        constraint_id = _non_empty_string(constraint.get("id"), f"{path}.id")
        if constraint_id in identifiers:
            raise ValueError("constraints ids must be unique")
        identifiers.add(constraint_id)
        if constraint.get("type") != "arrival_deadline":
            raise ValueError(f"{path}.type must be 'arrival_deadline'")
        event_id = _non_empty_string(
            constraint.get("event_id"), f"{path}.event_id"
        )
        if event_id not in event_ids:
            raise ValueError(f"{path}.event_id references an unknown event")
        deadline = _instant(constraint.get("deadline"), f"{path}.deadline")
        if deadline < horizon_start or deadline > horizon_end:
            raise ValueError(f"{path}.deadline must be inside the horizon")
        result.append(constraint)
    result.sort(key=lambda item: item["id"])
    return result


def _validation_edges(
    edges: List[dict],
    missing_pairs: List[Tuple[str, str]],
    used_references: Set[str],
) -> Tuple[List[dict], Set[str]]:
    result = list(edges)
    placeholder_references = set()
    for index, pair in enumerate(missing_pairs):
        reference = f"{_VALIDATION_REFERENCE_PREFIX}{index}"
        while reference in used_references:
            reference += "-"
        used_references.add(reference)
        placeholder_references.add(reference)
        result.append(
            {
                "from_event_id": pair[0],
                "to_event_id": pair[1],
                "duration_minutes": {"min": 0, "mode": 0, "max": 0},
                "source": {
                    "provider": "user",
                    "reference": reference,
                    "synthetic": True,
                    "assumption": "internal validation of a missing edge",
                },
            }
        )
    return result, placeholder_references


def _source_references(*collections: List[dict]) -> Set[str]:
    result = set()
    for collection in collections:
        for item in collection:
            source = item.get("source")
            if isinstance(source, dict) and isinstance(source.get("reference"), str):
                result.add(source["reference"])
    return result


def _require_explicit_zero_assumptions(edges: List[dict]) -> None:
    for edge in edges:
        duration = edge["duration_minutes"]
        if all(duration[name] == 0 for name in ("min", "mode", "max")):
            if not edge["source"]["assumption"].strip():
                raise ValueError(
                    "zero-duration travel edge requires an explicit assumption"
                )


def _json_copy(value: Any) -> Any:
    try:
        return _copy_json(value, set())
    except RecursionError as exc:
        raise ValueError("inputs exceed the supported JSON nesting depth") from exc


def _copy_json(value: Any, active: Set[int]) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("inputs must contain valid Unicode") from None
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("inputs must contain only finite JSON values")
        return value
    if isinstance(value, (dict, list)):
        identity = id(value)
        if identity in active:
            raise ValueError("inputs must contain acyclic JSON values")
        active.add(identity)
        try:
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise ValueError("input object keys must be strings")
                result = {}
                for key, item in value.items():
                    try:
                        key.encode("utf-8")
                    except UnicodeEncodeError:
                        raise ValueError(
                            "inputs must contain valid Unicode"
                        ) from None
                    result[key] = _copy_json(item, active)
                return result
            return [_copy_json(item, active) for item in value]
        finally:
            active.remove(identity)
    raise ValueError("inputs must contain only JSON values")


def _horizon(value: Any) -> Tuple[dict, datetime, datetime]:
    horizon = _object(value, "horizon")
    start_text = horizon.get("start")
    end_text = horizon.get("end")
    start = _instant(start_text, "horizon.start")
    end = _instant(end_text, "horizon.end")
    seconds = (end - start).total_seconds()
    if seconds <= 0 or seconds > _MAX_HORIZON_SECONDS:
        raise ValueError("horizon must be positive and no longer than seven days")
    return {"start": start_text, "end": end_text}, start, end


def _timezone(value: Any) -> str:
    name = _non_empty_string(value, "timezone")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("timezone must be a valid IANA identifier") from None
    return name


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
        return result.astimezone(utc_timezone.utc)
    except (OverflowError, ValueError):
        raise ValueError(f"{path} must be representable in UTC") from None


def _object(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def _array(value: Any, path: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be an array")
    return value


def _non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{path} must contain valid Unicode") from None
    return value


def _bounded_integer(value: Any, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{path} is outside the supported range")
    return value


def _finite_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite number")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError(f"{path} must be a finite number") from None
    if not math.isfinite(result):
        raise ValueError(f"{path} must be a finite number")
    return result
