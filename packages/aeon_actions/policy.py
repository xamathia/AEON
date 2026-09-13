"""Pure validation and authorization for private calendar moves."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_POLICY_FIELDS = {
    "autonomy_level",
    "allowed_calendar_ids",
    "allowed_event_ids",
    "approved_plan_hash",
    "kill_switch",
}
_REQUIRED_EVENT_FIELDS = {
    "calendar_id",
    "external_event_id",
    "etag",
    "planned_start",
    "planned_end",
    "private",
    "classification",
    "organizer_self",
    "attendees_count",
}


def plan_hash(plan: dict) -> str:
    """Return the SHA-256 of a strict canonical JSON representation."""
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object")
    try:
        encoded = json.dumps(
            plan,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("plan must contain finite acyclic JSON values") from exc
    return hashlib.sha256(encoded).hexdigest()


def evaluate_plan(plan: dict, current_events: dict, policy: dict) -> dict:
    """Return a deterministic decision without trusting claims inside the plan."""
    reasons = set()
    parsed_policy = _policy(policy)
    if parsed_policy is None:
        reasons.add("INVALID_POLICY")

    operations = _operations(plan)
    if operations is None:
        reasons.add("INVALID_PLAN")
        operations = []
    elif not operations:
        reasons.add("EMPTY_OPERATIONS")

    try:
        digest = plan_hash(plan)
    except ValueError:
        digest = None
        reasons.add("INVALID_PLAN")

    if not isinstance(current_events, dict):
        reasons.add("INVALID_CURRENT_EVENTS")
        current_events = {}

    if parsed_policy is not None:
        level, calendars, event_ids, approved_hash, kill_switch = parsed_policy
        if kill_switch:
            reasons.add("KILL_SWITCH_ACTIVE")
        if level <= 1:
            reasons.add("AUTONOMY_LEVEL_FORBIDS_ACTION")
        elif level == 2:
            if approved_hash is None:
                reasons.add("PLAN_APPROVAL_REQUIRED")
            elif digest is None or approved_hash != digest:
                reasons.add("PLAN_HASH_MISMATCH")

        seen = set()
        for operation in operations:
            event_id = operation["event_id"]
            if event_id in seen:
                reasons.add("DUPLICATE_EVENT_TARGET")
            seen.add(event_id)
            snapshot = current_events.get(event_id)
            parsed_snapshot = _event_snapshot(snapshot)
            if parsed_snapshot is None:
                reasons.add(
                    "EVENT_NOT_FOUND" if snapshot is None else "INVALID_CURRENT_EVENT"
                )
                continue
            calendar_id = parsed_snapshot["calendar_id"]
            if calendar_id not in calendars:
                reasons.add("CALENDAR_NOT_ALLOWED")
            if event_id not in event_ids:
                reasons.add("EVENT_NOT_ALLOWED")
            if not parsed_snapshot["private"]:
                reasons.add("EVENT_NOT_PRIVATE")
            if parsed_snapshot["classification"] != "flexible":
                reasons.add("EVENT_NOT_FLEXIBLE")
            if not parsed_snapshot["organizer_self"]:
                reasons.add("EVENT_NOT_OWNED")
            if parsed_snapshot["attendees_count"] != 0:
                reasons.add("EVENT_HAS_ATTENDEES")
            _validate_times(operation, parsed_snapshot, reasons)

    return {"allowed": not reasons, "reason_codes": sorted(reasons)}


def _policy(value: Any) -> Optional[Tuple[int, frozenset, frozenset, Optional[str], bool]]:
    if not isinstance(value, dict) or not _REQUIRED_POLICY_FIELDS <= set(value):
        return None
    level = value["autonomy_level"]
    calendars = _string_list(value["allowed_calendar_ids"])
    events = _string_list(value["allowed_event_ids"])
    approved = value["approved_plan_hash"]
    kill_switch = value["kill_switch"]
    if (
        isinstance(level, bool)
        or not isinstance(level, int)
        or not 0 <= level <= 3
        or calendars is None
        or events is None
        or (approved is not None and (not isinstance(approved, str) or _SHA256.fullmatch(approved) is None))
        or not isinstance(kill_switch, bool)
    ):
        return None
    return level, calendars, events, approved, kill_switch


def _string_list(value: Any) -> Optional[frozenset]:
    if not isinstance(value, list):
        return None
    if any(not isinstance(item, str) or not item for item in value):
        return None
    result = frozenset(value)
    if len(result) != len(value):
        return None
    return result


def _operations(plan: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(plan, dict) or not isinstance(plan.get("operations"), list):
        return None
    result = []
    for raw in plan["operations"]:
        if not isinstance(raw, dict):
            return None
        event_id = raw.get("event_id")
        before = _times(raw.get("before"))
        after = _times(raw.get("after"))
        if not isinstance(event_id, str) or not event_id or before is None or after is None:
            return None
        result.append({"event_id": event_id, "before": before, "after": after})
    return result


def _times(value: Any) -> Optional[Dict[str, datetime]]:
    if not isinstance(value, dict):
        return None
    try:
        start = _instant(value.get("planned_start"))
        end = _instant(value.get("planned_end"))
    except ValueError:
        return None
    return {"planned_start": start, "planned_end": end}


def _event_snapshot(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or not _REQUIRED_EVENT_FIELDS <= set(value):
        return None
    if any(
        not isinstance(value[field], str) or not value[field]
        for field in ("calendar_id", "external_event_id", "etag")
    ):
        return None
    if (
        not isinstance(value["classification"], str)
        or value["classification"] not in {"fixed", "flexible"}
    ):
        return None
    if not isinstance(value["private"], bool) or not isinstance(value["organizer_self"], bool):
        return None
    count = value["attendees_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    try:
        start = _instant(value["planned_start"])
        end = _instant(value["planned_end"])
    except ValueError:
        return None
    if end <= start:
        return None
    result = dict(value)
    result["planned_start"] = start
    result["planned_end"] = end
    return result


def _validate_times(operation: Dict[str, Any], snapshot: Dict[str, Any], reasons: set) -> None:
    before = operation["before"]
    after = operation["after"]
    if before["planned_end"] <= before["planned_start"] or after["planned_end"] <= after["planned_start"]:
        reasons.add("INVALID_TIME_RANGE")
        return
    if before["planned_end"] - before["planned_start"] != after["planned_end"] - after["planned_start"]:
        reasons.add("DURATION_CHANGED")
    if (
        before["planned_start"] != snapshot["planned_start"]
        or before["planned_end"] != snapshot["planned_end"]
    ):
        reasons.add("PLAN_NO_LONGER_VALID")
    if before == after:
        reasons.add("EMPTY_OPERATION")


def _instant(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("datetime must be a non-empty string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("datetime must be ISO 8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("datetime must include an offset")
    return result
