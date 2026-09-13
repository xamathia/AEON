"""Validation and normalization for the ÆON engine v1 payload."""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_PROVIDERS = {"google_calendar", "gmail", "google_routes", "osrm", "user"}
_CLASSIFICATIONS = {"fixed", "flexible"}
_MAX_HORIZON_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class TriangularDistribution:
    minimum: float
    mode: float
    maximum: float


@dataclass(frozen=True)
class Source:
    provider: str
    reference: str
    synthetic: bool
    assumption: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "reference": self.reference,
            "synthetic": self.synthetic,
            "assumption": self.assumption,
        }


@dataclass(frozen=True)
class Event:
    event_id: str
    planned_start: datetime
    planned_end: datetime
    overrun: TriangularDistribution
    source: Source


@dataclass(frozen=True)
class TravelEdge:
    from_event_id: str
    to_event_id: str
    duration: TriangularDistribution
    source: Source


@dataclass(frozen=True)
class Constraint:
    constraint_id: str
    event_id: str
    deadline: datetime
    source: Source


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    mode: str
    seed: int
    samples: int
    events: Tuple[Event, ...]
    travel_edges: Tuple[TravelEdge, ...]
    constraints: Tuple[Constraint, ...]
    sources: Tuple[Source, ...]
    assumptions: Tuple[str, ...]


def validate_payload(payload: Mapping[str, Any]) -> Scenario:
    """Validate a JSON-shaped payload and return an immutable scenario."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    _reject_non_finite(payload)

    if payload.get("schema_version") != "1.0":
        raise ValueError("schema_version must be '1.0'")
    scenario_id = _non_empty_string(payload.get("scenario_id"), "scenario_id")
    mode = payload.get("mode")
    if not isinstance(mode, str) or mode not in {"synthetic", "live"}:
        raise ValueError("mode must be 'synthetic' or 'live'")
    _validate_timezone(payload.get("timezone"))

    horizon = _object(payload.get("horizon"), "horizon")
    horizon_start = _instant(horizon.get("start"), "horizon.start")
    horizon_end = _instant(horizon.get("end"), "horizon.end")
    horizon_seconds = (horizon_end - horizon_start).total_seconds()
    if horizon_seconds <= 0 or horizon_seconds > _MAX_HORIZON_SECONDS:
        raise ValueError("horizon must be positive and no longer than seven days")

    seed = _integer(payload.get("seed"), "seed")
    if not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be between 0 and 2**32-1")
    samples = _integer(payload.get("samples"), "samples")
    if not 1 <= samples <= 10_000:
        raise ValueError("samples must be between 1 and 10000")

    source_registry: Dict[str, Source] = {}
    events = _events(
        payload.get("events"), horizon_start, horizon_end, mode, source_registry
    )
    travel_edges = _travel_edges(
        payload.get("travel_edges"), events, mode, source_registry
    )
    constraints = _constraints(
        payload.get("constraints"), events, horizon_start, horizon_end, mode, source_registry
    )
    sources = tuple(source_registry[key] for key in sorted(source_registry))
    assumptions = tuple(sorted({source.assumption for source in sources if source.assumption}))
    return Scenario(
        scenario_id=scenario_id,
        mode=mode,
        seed=seed,
        samples=samples,
        events=events,
        travel_edges=travel_edges,
        constraints=constraints,
        sources=sources,
        assumptions=assumptions,
    )


def _events(
    value: Any,
    horizon_start: datetime,
    horizon_end: datetime,
    mode: str,
    source_registry: Dict[str, Source],
) -> Tuple[Event, ...]:
    items = _array(value, "events")
    if not 1 <= len(items) <= 100:
        raise ValueError("events must contain between 1 and 100 items")
    result = []
    identifiers = set()
    previous_start: Optional[datetime] = None
    for index, raw in enumerate(items):
        path = f"events[{index}]"
        item = _object(raw, path)
        event_id = _non_empty_string(item.get("id"), f"{path}.id")
        if event_id in identifiers:
            raise ValueError(f"{path}.id must be unique")
        identifiers.add(event_id)
        _string(item.get("title"), f"{path}.title")
        planned_start = _instant(item.get("planned_start"), f"{path}.planned_start")
        planned_end = _instant(item.get("planned_end"), f"{path}.planned_end")
        if planned_end <= planned_start:
            raise ValueError(f"{path} must have a positive planned duration")
        if planned_start < horizon_start or planned_end > horizon_end:
            raise ValueError(f"{path} must be entirely inside the horizon")
        if previous_start is not None and planned_start < previous_start:
            raise ValueError("events must be sorted by planned_start")
        previous_start = planned_start
        classification = item.get("classification")
        if not isinstance(classification, str) or classification not in _CLASSIFICATIONS:
            raise ValueError(f"{path}.classification must be 'fixed' or 'flexible'")
        if not isinstance(item.get("private"), bool):
            raise ValueError(f"{path}.private must be a boolean")
        result.append(
            Event(
                event_id=event_id,
                planned_start=planned_start,
                planned_end=planned_end,
                overrun=_distribution(item.get("overrun_minutes"), f"{path}.overrun_minutes"),
                source=_source(item.get("source"), f"{path}.source", mode, source_registry),
            )
        )
    return tuple(result)


def _travel_edges(
    value: Any,
    events: Tuple[Event, ...],
    mode: str,
    source_registry: Dict[str, Source],
) -> Tuple[TravelEdge, ...]:
    items = _array(value, "travel_edges")
    expected_pairs = {
        (left.event_id, right.event_id) for left, right in zip(events, events[1:])
    }
    if len(items) != len(expected_pairs):
        raise ValueError("travel_edges must contain exactly one edge per adjacent event pair")
    result_by_pair: Dict[Tuple[str, str], TravelEdge] = {}
    for index, raw in enumerate(items):
        path = f"travel_edges[{index}]"
        item = _object(raw, path)
        pair = (
            _non_empty_string(item.get("from_event_id"), f"{path}.from_event_id"),
            _non_empty_string(item.get("to_event_id"), f"{path}.to_event_id"),
        )
        if pair not in expected_pairs:
            raise ValueError(f"{path} must connect an adjacent event pair")
        if pair in result_by_pair:
            raise ValueError(f"{path} duplicates an adjacent event pair")
        result_by_pair[pair] = TravelEdge(
            from_event_id=pair[0],
            to_event_id=pair[1],
            duration=_distribution(item.get("duration_minutes"), f"{path}.duration_minutes"),
            source=_source(item.get("source"), f"{path}.source", mode, source_registry),
        )
    ordered_pairs = [
        (left.event_id, right.event_id) for left, right in zip(events, events[1:])
    ]
    return tuple(result_by_pair[pair] for pair in ordered_pairs)


def _constraints(
    value: Any,
    events: Tuple[Event, ...],
    horizon_start: datetime,
    horizon_end: datetime,
    mode: str,
    source_registry: Dict[str, Source],
) -> Tuple[Constraint, ...]:
    items = _array(value, "constraints")
    event_ids = {event.event_id for event in events}
    identifiers = set()
    result = []
    for index, raw in enumerate(items):
        path = f"constraints[{index}]"
        item = _object(raw, path)
        constraint_id = _non_empty_string(item.get("id"), f"{path}.id")
        if constraint_id in identifiers:
            raise ValueError(f"{path}.id must be unique")
        identifiers.add(constraint_id)
        if item.get("type") != "arrival_deadline":
            raise ValueError(f"{path}.type must be 'arrival_deadline'")
        event_id = _non_empty_string(item.get("event_id"), f"{path}.event_id")
        if event_id not in event_ids:
            raise ValueError(f"{path}.event_id references an unknown event")
        deadline = _instant(item.get("deadline"), f"{path}.deadline")
        if deadline < horizon_start or deadline > horizon_end:
            raise ValueError(f"{path}.deadline must be inside the horizon")
        result.append(
            Constraint(
                constraint_id=constraint_id,
                event_id=event_id,
                deadline=deadline,
                source=_source(item.get("source"), f"{path}.source", mode, source_registry),
            )
        )
    return tuple(result)


def _distribution(value: Any, path: str) -> TriangularDistribution:
    item = _object(value, path)
    minimum = _number(item.get("min"), f"{path}.min")
    mode = _number(item.get("mode"), f"{path}.mode")
    maximum = _number(item.get("max"), f"{path}.max")
    if minimum < 0 or not minimum <= mode <= maximum:
        raise ValueError(f"{path} must satisfy 0 <= min <= mode <= max")
    return TriangularDistribution(minimum, mode, maximum)


def _source(
    value: Any,
    path: str,
    mode: str,
    registry: Dict[str, Source],
) -> Source:
    item = _object(value, path)
    provider = item.get("provider")
    if not isinstance(provider, str) or provider not in _PROVIDERS:
        raise ValueError(f"{path}.provider is not supported")
    source = Source(
        provider=provider,
        reference=_non_empty_string(item.get("reference"), f"{path}.reference"),
        synthetic=item.get("synthetic"),
        assumption=_string(item.get("assumption"), f"{path}.assumption"),
    )
    if not isinstance(source.synthetic, bool):
        raise ValueError(f"{path}.synthetic must be a boolean")
    if mode == "synthetic" and not source.synthetic:
        raise ValueError(f"{path}.synthetic must be true in synthetic mode")
    previous = registry.get(source.reference)
    if previous is not None and previous != source:
        raise ValueError(f"{path}.reference has an ambiguous source descriptor")
    registry[source.reference] = source
    return source


def _reject_non_finite(value: Any) -> None:
    pending = [(False, "payload", value)]
    active = set()
    while pending:
        leaving, path, current = pending.pop()
        if leaving:
            active.remove(id(current))
            continue
        if isinstance(current, float) and not math.isfinite(current):
            raise ValueError(f"{path} must not contain NaN or Infinity")
        if isinstance(current, (dict, list)):
            identity = id(current)
            if identity in active:
                raise ValueError(f"{path} must be an acyclic JSON value")
            active.add(identity)
            pending.append((True, path, current))
            children: Iterable[Tuple[str, Any]]
            if isinstance(current, dict):
                children = ((f"{path}.{key}", item) for key, item in current.items())
            else:
                children = ((f"{path}[{index}]", item) for index, item in enumerate(current))
            pending.extend((False, child_path, child) for child_path, child in children)


def _object(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def _array(value: Any, path: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be an array")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{path} must contain valid Unicode") from exc
    return value


def _non_empty_string(value: Any, path: str) -> str:
    result = _string(value, path)
    if not result.strip():
        raise ValueError(f"{path} must be non-empty")
    return result


def _integer(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{path} must be an integer")
    return value


def _number(value: Any, path: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{path} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{path} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{path} must be a finite number")
    return result


def _instant(value: Any, path: str) -> datetime:
    text = _string(value, path)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{path} must be an ISO 8601 datetime") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{path} must include an explicit UTC offset or Z")
    return result.astimezone(timezone.utc)


def _validate_timezone(value: Any) -> None:
    name = _non_empty_string(value, "timezone")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must be a valid IANA identifier") from exc
