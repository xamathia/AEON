"""Deterministic Monte Carlo simulation for the ÆON engine v1 contract."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import random
from typing import Any, Dict, List, Sequence, Tuple

from .validation import Scenario, TriangularDistribution, validate_payload


_QUANTILES = (("p10", 0.1), ("p50", 0.5), ("p90", 0.9))


def simulate_day(payload: dict) -> dict:
    """Simulate a canonical ÆON payload without mutating it."""
    scenario = validate_payload(payload)
    deadlines, constraint_references = _deadlines(scenario)
    overrun_generators = [
        _random_for(scenario.seed, "overrun", event.event_id) for event in scenario.events
    ]
    travel_generators = [
        _random_for(
            scenario.seed,
            "travel",
            [edge.from_event_id, edge.to_event_id],
        )
        for edge in scenario.travel_edges
    ]
    arrivals: List[List[float]] = [[] for _ in scenario.events]
    starts: List[List[float]] = [[] for _ in scenario.events]
    ends: List[List[float]] = [[] for _ in scenario.events]
    delays: List[List[float]] = [[] for _ in scenario.events]
    late_counts = [0 for _ in scenario.events]
    any_late_count = 0
    total_delay_minutes = 0.0

    try:
        for _ in range(scenario.samples):
            sampled_overruns = [
                _sample(distribution, generator)
                for distribution, generator in zip(
                    (event.overrun for event in scenario.events), overrun_generators
                )
            ]
            sampled_travel = [
                _sample(distribution, generator)
                for distribution, generator in zip(
                    (edge.duration for edge in scenario.travel_edges), travel_generators
                )
            ]
            sample_has_late_arrival = False
            previous_end = None
            sample_total_delay = 0.0
            for index, event in enumerate(scenario.events):
                if index == 0:
                    actual_arrival = event.planned_start
                else:
                    actual_arrival = previous_end + timedelta(minutes=sampled_travel[index - 1])
                actual_start = max(event.planned_start, actual_arrival)
                planned_duration = event.planned_end - event.planned_start
                actual_end = actual_start + planned_duration + timedelta(
                    minutes=sampled_overruns[index]
                )
                delay = max(
                    0.0,
                    (actual_arrival - deadlines[event.event_id]).total_seconds() / 60.0,
                )
                arrivals[index].append(actual_arrival.timestamp())
                starts[index].append(actual_start.timestamp())
                ends[index].append(actual_end.timestamp())
                delays[index].append(delay)
                if delay > 0:
                    late_counts[index] += 1
                    sample_has_late_arrival = True
                sample_total_delay += delay
                previous_end = actual_end
            if sample_has_late_arrival:
                any_late_count += 1
            total_delay_minutes += sample_total_delay
    except (OverflowError, OSError) as exc:
        raise ValueError("sampled duration exceeds the supported datetime range") from exc

    output_events = []
    for index, event in enumerate(scenario.events):
        probability = late_counts[index] / scenario.samples
        output_events.append(
            {
                "event_id": event.event_id,
                "arrival": _instant_quantiles(arrivals[index]),
                "start": _instant_quantiles(starts[index]),
                "end": _instant_quantiles(ends[index]),
                "late_arrival_probability": probability,
                "delay_minutes": _minute_quantiles(delays[index]),
                "deadline": _format_instant(deadlines[event.event_id].timestamp()),
                "reason_codes": _reason_codes(scenario, index) if probability > 0 else [],
                "evidence_refs": _evidence_references(
                    scenario, index, constraint_references[event.event_id]
                ),
            }
        )

    return {
        "schema_version": "1.0",
        "scenario_id": scenario.scenario_id,
        "mode": scenario.mode,
        "seed": scenario.seed,
        "samples": scenario.samples,
        "events": output_events,
        "summary": {
            "probability_any_late": any_late_count / scenario.samples,
            "expected_total_delay_minutes": round(
                total_delay_minutes / scenario.samples, 3
            ),
        },
        "sources": [source.as_dict() for source in scenario.sources],
        "assumptions": list(scenario.assumptions),
        "llm_calls": 0,
    }


def _random_for(seed: int, kind: str, identifier: Any) -> random.Random:
    encoded = json.dumps(
        [seed, kind, identifier],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    derived_seed = int.from_bytes(hashlib.sha256(encoded).digest(), "big")
    return random.Random(derived_seed)


def _sample(distribution: TriangularDistribution, generator: random.Random) -> float:
    if distribution.minimum == distribution.maximum:
        generator.random()
        return distribution.minimum
    return generator.triangular(
        distribution.minimum, distribution.maximum, distribution.mode
    )


def _deadlines(scenario: Scenario) -> Tuple[Dict[str, datetime], Dict[str, List[str]]]:
    deadlines = {event.event_id: event.planned_start for event in scenario.events}
    references = {event.event_id: [] for event in scenario.events}
    for constraint in scenario.constraints:
        deadlines[constraint.event_id] = min(
            deadlines[constraint.event_id], constraint.deadline
        )
        references[constraint.event_id].append(constraint.source.reference)
    return deadlines, references


def _quantile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = position - lower_index
    return ordered[lower_index] * (1.0 - weight) + ordered[upper_index] * weight


def _instant_quantiles(values: Sequence[float]) -> Dict[str, str]:
    return {
        label: _format_instant(_quantile(values, quantile))
        for label, quantile in _QUANTILES
    }


def _minute_quantiles(values: Sequence[float]) -> Dict[str, float]:
    return {
        label: round(_quantile(values, quantile), 3)
        for label, quantile in _QUANTILES
    }


def _format_instant(timestamp: float) -> str:
    rounded = round(timestamp)
    return datetime.fromtimestamp(rounded, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reason_codes(scenario: Scenario, event_index: int) -> List[str]:
    codes = set()
    if any(event.overrun.maximum > 0 for event in scenario.events[:event_index]):
        codes.add("MEETING_OVERRUN_LIKELY")
    if any(edge.duration.maximum > 0 for edge in scenario.travel_edges[:event_index]):
        codes.add("INSUFFICIENT_TRAVEL_BUFFER")
    event = scenario.events[event_index]
    if any(
        constraint.event_id == event.event_id
        and constraint.deadline < event.planned_start
        and constraint.source.provider == "gmail"
        for constraint in scenario.constraints
    ):
        codes.add("DEADLINE_AT_RISK")
    return sorted(codes)


def _evidence_references(
    scenario: Scenario, event_index: int, constraint_references: Sequence[str]
) -> List[str]:
    references = {
        event.source.reference for event in scenario.events[: event_index + 1]
    }
    references.update(
        edge.source.reference for edge in scenario.travel_edges[:event_index]
    )
    references.update(constraint_references)
    return sorted(references)
