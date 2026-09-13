"""Bounded, deterministic generation of private calendar plans."""

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Dict, List, Sequence, Tuple

from packages.aeon_engine import simulate_day

from .validation import (
    LiveSelection,
    PlanningContext,
    Route,
    validate_context,
    validate_live_selection,
)


_MISSING_ROUTE = "MISSING_ROUTE"
_PLANNED_OVERLAP = "PLANNED_OVERLAP"
_ROUTE_REUSE_ASSUMPTION = (
    "Prior observed earlier reused after schedule movement; "
    "not a new traffic observation."
)
_SAME_LOCATION_REUSE_ASSUMPTION = (
    "Explicit same-location declaration reused after schedule movement."
)


class _CandidateRejected(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def plan_day(payload: dict, context: dict) -> dict:
    """Return up to three risk-reducing one-event plans without side effects."""
    baseline = simulate_day(payload)
    planning = validate_context(payload, context)
    return _search_plans(
        payload,
        planning,
        baseline,
        _instant(payload["horizon"]["start"]),
        _instant(payload["horizon"]["end"]),
    )


def plan_live_day(payload: dict, selection: dict) -> dict:
    """Plan inside an explicit window using only canonical live travel evidence."""
    selected = validate_live_selection(payload, selection)
    baseline = simulate_day(payload)
    context = _live_context(payload, selected)
    planning = validate_context(payload, context)
    return _search_plans(
        payload,
        planning,
        baseline,
        selected.window_start,
        selected.window_end,
    )


def _search_plans(
    payload: dict,
    planning: PlanningContext,
    baseline: dict,
    search_start: datetime,
    search_end: datetime,
) -> dict:
    _validate_route_assumptions(payload, planning)
    baseline_risk = _target_risk(baseline, planning.target_event_id)
    event_by_id = {event["id"]: event for event in payload["events"]}
    rejected_reasons = set()
    evaluated_candidates = 0
    truncated = False
    improving_plans = []
    stop_search = False

    for movable_event_id in planning.movable_event_ids:
        original = event_by_id[movable_event_id]
        original_start = _instant(original["planned_start"])
        original_end = _instant(original["planned_end"])
        duration = original_end - original_start
        candidate_starts = sorted(
            _grid(search_start, search_end, duration, planning.step_minutes),
            key=lambda value: (abs(value - original_start), value),
        )
        for candidate_start in candidate_starts:
            if candidate_start == original_start:
                continue
            try:
                scenario = _candidate_scenario(
                    payload,
                    planning,
                    movable_event_id,
                    candidate_start,
                    candidate_start + duration,
                )
            except _CandidateRejected as exc:
                rejected_reasons.add(exc.reason)
                continue
            if evaluated_candidates >= planning.max_candidates:
                truncated = True
                stop_search = True
                break
            simulation = simulate_day(scenario)
            evaluated_candidates += 1
            candidate_risk = _target_risk(simulation, planning.target_event_id)
            if candidate_risk < baseline_risk:
                improving_plans.append(
                    _plan(
                        original,
                        movable_event_id,
                        candidate_start,
                        candidate_start + duration,
                        scenario,
                        simulation,
                        baseline_risk,
                        candidate_risk,
                    )
                )
        if stop_search:
            break

    improving_plans.sort(
        key=lambda item: (
            item["target_risk_after"],
            abs(item["shift_minutes"]),
            item["id"],
        )
    )
    plans = improving_plans[:3]
    return {
        "schema_version": "1.0",
        "status": "plans_found" if plans else "no_better_plan",
        "baseline": baseline,
        "plans": plans,
        "search": {
            "evaluated_candidates": evaluated_candidates,
            "truncated": truncated,
            "rejected_reasons": sorted(rejected_reasons),
        },
        "llm_calls": 0,
    }


def _live_context(payload: dict, selection: LiveSelection) -> dict:
    event_ids = [event["id"] for event in payload["events"]]
    parent = {event_id: event_id for event_id in event_ids}

    def find(event_id: str) -> str:
        while parent[event_id] != event_id:
            parent[event_id] = parent[parent[event_id]]
            event_id = parent[event_id]
        return event_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for edge in payload["travel_edges"]:
        if _is_explicit_same_location(edge):
            union(edge["from_event_id"], edge["to_event_id"])

    members: Dict[str, List[str]] = {}
    for event_id in event_ids:
        members.setdefault(find(event_id), []).append(event_id)
    location_by_root = {
        root: "live-location:"
        + hashlib.sha256(
            json.dumps(sorted(values), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        for root, values in members.items()
    }
    locations = {
        event_id: location_by_root[find(event_id)] for event_id in event_ids
    }

    routes: Dict[Tuple[str, str], dict] = {}
    for edge in payload["travel_edges"]:
        left = locations[edge["from_event_id"]]
        right = locations[edge["to_event_id"]]
        same_location = _is_explicit_same_location(edge)
        if left == right and not same_location:
            raise ValueError("live travel observations are ambiguous inside a location group")
        source = copy.deepcopy(edge["source"])
        assumption = (
            _SAME_LOCATION_REUSE_ASSUMPTION
            if same_location
            else _ROUTE_REUSE_ASSUMPTION
        )
        existing_assumption = source["assumption"].strip()
        source["assumption"] = (
            f"{existing_assumption} {assumption}" if existing_assumption else assumption
        )
        route = {
            "from_location_id": left,
            "to_location_id": right,
            "duration_minutes": copy.deepcopy(edge["duration_minutes"]),
            "source": source,
        }
        pair = (left, right)
        previous = routes.get(pair)
        if previous is not None and previous != route:
            raise ValueError("live travel observations are ambiguous for a location pair")
        routes[pair] = route

    return {
        "target_event_id": selection.target_event_id,
        "movable_event_ids": list(selection.movable_event_ids),
        "location_by_event_id": locations,
        "route_catalog": list(routes.values()),
        "step_minutes": selection.step_minutes,
        "max_candidates": selection.max_candidates,
    }


def _is_explicit_same_location(edge: dict) -> bool:
    duration = edge["duration_minutes"]
    return edge["source"]["provider"] == "user" and all(
        duration[name] == 0 for name in ("min", "mode", "max")
    )


def _validate_route_assumptions(payload: dict, planning: PlanningContext) -> None:
    """A scheduling comparison must not silently change its travel model."""
    for edge in payload["travel_edges"]:
        left = planning.location_by_event_id[edge["from_event_id"]]
        right = planning.location_by_event_id[edge["to_event_id"]]
        route = planning.routes.get((left, right))
        if route is None and left != right:
            # Keep the public MISSING_ROUTE candidate outcome for absent routes.
            continue
        expected = route.duration_minutes if route else {"min": 0, "mode": 0, "max": 0}
        if any(edge["duration_minutes"][key] != expected[key] for key in ("min", "mode", "max")):
            raise ValueError(
                f"route_catalog contradicts baseline travel duration for declared locations {(left, right)!r}"
            )


def _grid(
    horizon_start: datetime,
    horizon_end: datetime,
    duration: timedelta,
    step_minutes: int,
) -> Sequence[datetime]:
    values = []
    current = horizon_start
    latest_start = horizon_end - duration
    step = timedelta(minutes=step_minutes)
    while current <= latest_start:
        values.append(current)
        if latest_start - current < step:
            break
        current += step
    return values


def _candidate_scenario(
    payload: dict,
    planning: PlanningContext,
    movable_event_id: str,
    new_start: datetime,
    new_end: datetime,
) -> dict:
    scenario = copy.deepcopy(payload)
    indexed_events: List[Tuple[int, dict]] = []
    for index, event in enumerate(scenario["events"]):
        if event["id"] == movable_event_id:
            event["planned_start"] = _format_instant(new_start)
            event["planned_end"] = _format_instant(new_end)
        indexed_events.append((index, event))
    indexed_events.sort(key=lambda item: (_instant(item[1]["planned_start"]), item[0]))
    events = [event for _, event in indexed_events]
    if any(
        _instant(right["planned_start"]) < _instant(left["planned_end"])
        for left, right in zip(events, events[1:])
    ):
        raise _CandidateRejected(_PLANNED_OVERLAP)
    scenario["events"] = events
    scenario["travel_edges"] = [
        _travel_edge(left, right, planning)
        for left, right in zip(events, events[1:])
    ]
    return scenario


def _travel_edge(left: dict, right: dict, planning: PlanningContext) -> dict:
    left_location = planning.location_by_event_id[left["id"]]
    right_location = planning.location_by_event_id[right["id"]]
    pair = (left_location, right_location)
    route = planning.routes.get(pair)
    if route is None:
        if left_location != right_location:
            raise _CandidateRejected(_MISSING_ROUTE)
        route = _same_location_route(left_location)
    return {
        "from_event_id": left["id"],
        "to_event_id": right["id"],
        "duration_minutes": copy.deepcopy(route.duration_minutes),
        "source": copy.deepcopy(route.source),
    }


def _same_location_route(location_id: str) -> Route:
    digest = hashlib.sha256(location_id.encode("utf-8")).hexdigest()
    return Route(
        from_location_id=location_id,
        to_location_id=location_id,
        duration_minutes={"min": 0.0, "mode": 0.0, "max": 0.0},
        source={
            "provider": "user",
            "reference": f"planner:same-location:{digest}",
            "synthetic": True,
            "assumption": "Same declared location; zero travel assumed by the planner.",
        },
    )


def _plan(
    original: dict,
    event_id: str,
    new_start: datetime,
    new_end: datetime,
    scenario: dict,
    simulation: dict,
    baseline_risk: float,
    candidate_risk: float,
) -> dict:
    before_start = _format_instant(_instant(original["planned_start"]))
    before_end = _format_instant(_instant(original["planned_end"]))
    after_start = _format_instant(new_start)
    after_end = _format_instant(new_end)
    identifier = hashlib.sha256(
        json.dumps(
            [event_id, after_start, after_end],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    shift = (new_start - _instant(original["planned_start"])).total_seconds() / 60.0
    shift_value = int(shift) if shift.is_integer() else shift
    return {
        "id": identifier,
        "scenario": scenario,
        "simulation": simulation,
        "operations": [
            {
                "event_id": event_id,
                "before": {
                    "planned_start": before_start,
                    "planned_end": before_end,
                },
                "after": {
                    "planned_start": after_start,
                    "planned_end": after_end,
                },
            }
        ],
        "target_risk_before": baseline_risk,
        "target_risk_after": candidate_risk,
        "shift_minutes": shift_value,
        "reason_codes": ["PROTECTS_FIXED_EVENT", "LOWER_RESIDUAL_RISK"],
        "requires_approval": True,
    }


def _target_risk(simulation: dict, event_id: str) -> float:
    for event in simulation["events"]:
        if event["event_id"] == event_id:
            return event["late_arrival_probability"]
    raise ValueError(f"target event {event_id!r} is absent from the simulation")


def _instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("planner datetime must be a string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("planner datetime must be ISO 8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("planner datetime must include an offset")
    return result.astimezone(timezone.utc)


def _format_instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
