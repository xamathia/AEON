"""Conditional Calendar execution, compensation, and undo."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
from typing import Any, Dict, List, Tuple

from .audit import AuditStore
from .policy import (
    _event_snapshot,
    _operations,
    _policy,
    evaluate_plan,
    plan_hash,
)


class CalendarConflict(Exception):
    """The Calendar version changed and must not be overwritten."""


class CalendarUnknownOutcome(Exception):
    """A Calendar mutation may or may not have taken effect."""


class PlanExecutor:
    def __init__(self, calendar: Any, audit_store: AuditStore) -> None:
        if not isinstance(audit_store, AuditStore):
            raise ValueError("audit_store must be an AuditStore")
        if not callable(getattr(calendar, "get_event", None)) or not callable(
            getattr(calendar, "patch_times", None)
        ):
            raise ValueError("calendar must implement get_event and patch_times")
        self._calendar = calendar
        self._audit = audit_store

    def execute(
        self,
        plan: dict,
        *,
        current_events: dict,
        policy: dict,
        idempotency_key: str,
    ) -> dict:
        key_hash = _idempotency_hash(idempotency_key)
        digest = plan_hash(plan)
        decision = evaluate_plan(plan, current_events, policy)
        if not decision["allowed"]:
            return self._audit._create_denied(
                key_hash, digest, decision["reason_codes"]
            )
        operations = _operations(plan)
        if operations is None:
            raise AssertionError("allowed plan has invalid operations")
        actions = _action_records(operations, current_events)
        receipt, created = self._audit._get_or_create_intent(
            key_hash, digest, actions
        )
        if not created:
            return receipt

        remote_events, failure = self._preflight(actions, operations, policy)
        if failure is not None:
            status, reasons, kind = failure
            return self._audit._record(
                receipt,
                status=status,
                reason_codes=reasons,
                kind=kind,
                data={"reason_codes": reasons},
            )
        receipt = self._audit._record(
            receipt,
            status="unknown",
            kind="preflight_succeeded",
            data={"event_ids": [action["event_id"] for action in actions]},
        )
        return self._apply(receipt, remote_events)

    def undo(self, receipt_id: str, *, policy: dict) -> dict:
        receipt = self._audit.get_receipt(receipt_id)
        if receipt is None:
            raise ValueError("receipt does not exist")
        if receipt["status"] == "undone":
            return receipt
        if receipt["status"] != "applied":
            return receipt
        policy_reasons = _undo_policy_reasons(receipt, policy)
        if policy_reasons:
            return self._audit._record_if_status(
                receipt["id"],
                expected_status="applied",
                # The undo was denied, but the external plan is still applied.
                status="applied",
                reason_codes=policy_reasons,
                kind="undo_denied",
                data={"reason_codes": policy_reasons},
            )

        receipt, claimed = self._audit._claim_undo(receipt["id"])
        if not claimed:
            return receipt

        for action in reversed(receipt["actions"]):
            try:
                remote = self._calendar.get_event(
                    action["calendar_id"], action["external_event_id"]
                )
            except Exception:
                return self._audit._record(
                    receipt,
                    # No mutation has started, so a transient read failure may
                    # be retried after policy and ETag checks run again.
                    status="applied",
                    reason_codes=["UNDO_PREFLIGHT_FAILED"],
                    kind="undo_preflight_failed",
                    data={"event_id": action["event_id"]},
                )
            parsed = _event_snapshot(remote)
            if parsed is None or not _matches_applied(action, parsed):
                return self._audit._record(
                    receipt,
                    status="conflict",
                    reason_codes=["HUMAN_CHANGE_DETECTED"],
                    kind="undo_conflict",
                    data={"event_id": action["event_id"]},
                )
        actions = copy.deepcopy(receipt["actions"])
        undone_count = 0
        for index in range(len(actions) - 1, -1, -1):
            action = actions[index]
            receipt = self._audit._record(
                receipt,
                status="unknown",
                actions=actions,
                kind="undo_action_started",
                data={"event_id": action["event_id"]},
            )
            try:
                restored = self._calendar.patch_times(
                    action["calendar_id"],
                    action["external_event_id"],
                    planned_start=action["before"]["planned_start"],
                    planned_end=action["before"]["planned_end"],
                    if_match=action["after"]["etag"],
                    send_updates="none",
                )
            except CalendarUnknownOutcome:
                return self._audit._record(
                    receipt,
                    status="unknown",
                    actions=actions,
                    reason_codes=["UNKNOWN_CALENDAR_OUTCOME"],
                    kind="undo_unknown",
                    data={"event_id": action["event_id"]},
                )
            except Exception:
                status = "partial" if undone_count else "conflict"
                return self._audit._record(
                    receipt,
                    status=status,
                    actions=actions,
                    reason_codes=["HUMAN_CHANGE_DETECTED"],
                    kind="undo_failed",
                    data={"event_id": action["event_id"]},
                )
            parsed = _event_snapshot(restored)
            if parsed is None or not _matches_requested(action, parsed, "before"):
                return self._audit._record(
                    receipt,
                    status="unknown",
                    actions=actions,
                    reason_codes=["UNKNOWN_CALENDAR_OUTCOME"],
                    kind="undo_invalid_response",
                    data={"event_id": action["event_id"]},
                )
            action["state"] = "undone"
            action["undo_etag"] = parsed["etag"]
            undone_count += 1
            receipt = self._audit._record(
                receipt,
                status="partial",
                actions=actions,
                kind="undo_action_succeeded",
                data={"event_id": action["event_id"]},
            )
        return self._audit._record(
            receipt,
            status="undone",
            actions=actions,
            reason_codes=[],
            kind="undo_succeeded",
            data={},
        )

    def _preflight(
        self,
        actions: List[dict],
        operations: List[dict],
        policy: dict,
    ) -> Tuple[Dict[str, dict], Any]:
        remote_events = {}
        for action in actions:
            try:
                remote = self._calendar.get_event(
                    action["calendar_id"], action["external_event_id"]
                )
            except Exception:
                return {}, (
                    "conflict",
                    ["PREFLIGHT_FAILED"],
                    "preflight_failed",
                )
            parsed = _event_snapshot(remote)
            if parsed is None or not _matches_trusted(action, parsed):
                return {}, (
                    "conflict",
                    ["PLAN_NO_LONGER_VALID"],
                    "preflight_conflict",
                )
            remote_events[action["event_id"]] = remote
        decision = evaluate_plan(
            {"operations": [_serialized_operation(item) for item in operations]},
            remote_events,
            _level_three_copy(policy),
        )
        if not decision["allowed"]:
            return {}, (
                "conflict",
                decision["reason_codes"],
                "preflight_policy_denied",
            )
        return remote_events, None

    def _apply(
        self,
        receipt: dict,
        remote_events: Dict[str, dict],
    ) -> dict:
        actions = copy.deepcopy(receipt["actions"])
        applied_indices = []
        for index, action in enumerate(actions):
            receipt = self._audit._record(
                receipt,
                status="unknown",
                actions=actions,
                kind="action_started",
                data={"event_id": action["event_id"]},
            )
            try:
                updated = self._calendar.patch_times(
                    action["calendar_id"],
                    action["external_event_id"],
                    planned_start=action["after"]["planned_start"],
                    planned_end=action["after"]["planned_end"],
                    if_match=remote_events[action["event_id"]]["etag"],
                    send_updates="none",
                )
            except CalendarUnknownOutcome:
                action["state"] = "unknown"
                return self._audit._record(
                    receipt,
                    status="unknown",
                    actions=actions,
                    reason_codes=["UNKNOWN_CALENDAR_OUTCOME"],
                    kind="action_unknown",
                    data={"event_id": action["event_id"]},
                )
            except Exception as exc:
                action["state"] = "conflict" if isinstance(exc, CalendarConflict) else "failed"
                return self._compensate(receipt, actions, applied_indices, action["event_id"])
            parsed = _event_snapshot(updated)
            if parsed is None or not _matches_requested(action, parsed, "after"):
                action["state"] = "unknown"
                return self._audit._record(
                    receipt,
                    status="unknown",
                    actions=actions,
                    reason_codes=["UNKNOWN_CALENDAR_OUTCOME"],
                    kind="action_invalid_response",
                    data={"event_id": action["event_id"]},
                )
            action["state"] = "applied"
            action["after"]["etag"] = parsed["etag"]
            applied_indices.append(index)
            receipt = self._audit._record(
                receipt,
                status="unknown",
                actions=actions,
                kind="action_succeeded",
                data={"event_id": action["event_id"]},
            )
        return self._audit._record(
            receipt,
            status="applied",
            actions=actions,
            reason_codes=[],
            kind="plan_applied",
            data={},
        )

    def _compensate(
        self,
        receipt: dict,
        actions: List[dict],
        applied_indices: List[int],
        failed_event_id: str,
    ) -> dict:
        if not applied_indices:
            return self._audit._record(
                receipt,
                status="conflict",
                actions=actions,
                reason_codes=["CALENDAR_WRITE_FAILED"],
                kind="action_failed",
                data={"event_id": failed_event_id},
            )
        receipt = self._audit._record(
            receipt,
            status="partial",
            actions=actions,
            kind="compensation_started",
            data={"failed_event_id": failed_event_id},
        )
        for index in reversed(applied_indices):
            action = actions[index]
            receipt = self._audit._record(
                receipt,
                status="unknown",
                actions=actions,
                kind="compensation_action_started",
                data={"event_id": action["event_id"]},
            )
            try:
                restored = self._calendar.patch_times(
                    action["calendar_id"],
                    action["external_event_id"],
                    planned_start=action["before"]["planned_start"],
                    planned_end=action["before"]["planned_end"],
                    if_match=action["after"]["etag"],
                    send_updates="none",
                )
            except CalendarUnknownOutcome:
                action["state"] = "unknown"
                return self._audit._record(
                    receipt,
                    status="unknown",
                    actions=actions,
                    reason_codes=["UNKNOWN_CALENDAR_OUTCOME"],
                    kind="compensation_unknown",
                    data={"event_id": action["event_id"]},
                )
            except Exception as exc:
                action["state"] = (
                    "compensation_conflict"
                    if isinstance(exc, CalendarConflict)
                    else "compensation_failed"
                )
                return self._audit._record(
                    receipt,
                    status="partial",
                    actions=actions,
                    reason_codes=["COMPENSATION_FAILED"],
                    kind="compensation_failed",
                    data={"event_id": action["event_id"]},
                )
            parsed = _event_snapshot(restored)
            if parsed is None or not _matches_requested(action, parsed, "before"):
                action["state"] = "unknown"
                return self._audit._record(
                    receipt,
                    status="unknown",
                    actions=actions,
                    reason_codes=["UNKNOWN_CALENDAR_OUTCOME"],
                    kind="compensation_invalid_response",
                    data={"event_id": action["event_id"]},
                )
            action["state"] = "compensated"
            action["compensation_etag"] = parsed["etag"]
            receipt = self._audit._record(
                receipt,
                status="partial",
                actions=actions,
                kind="action_compensated",
                data={"event_id": action["event_id"]},
            )
        return self._audit._record(
            receipt,
            status="compensated",
            actions=actions,
            reason_codes=["CALENDAR_WRITE_FAILED"],
            kind="plan_compensated",
            data={"failed_event_id": failed_event_id},
        )


def _idempotency_hash(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("idempotency_key must be a non-empty string of at most 256 characters")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _action_records(operations: List[dict], current_events: dict) -> List[dict]:
    result = []
    for operation in operations:
        event_id = operation["event_id"]
        snapshot = _event_snapshot(current_events[event_id])
        if snapshot is None:
            raise AssertionError("allowed event snapshot is invalid")
        result.append(
            {
                "event_id": event_id,
                "calendar_id": snapshot["calendar_id"],
                "external_event_id": snapshot["external_event_id"],
                "state": "pending",
                "before": {
                    "planned_start": _iso(operation["before"]["planned_start"]),
                    "planned_end": _iso(operation["before"]["planned_end"]),
                    "etag": snapshot["etag"],
                },
                "after": {
                    "planned_start": _iso(operation["after"]["planned_start"]),
                    "planned_end": _iso(operation["after"]["planned_end"]),
                    "etag": None,
                },
            }
        )
    return result


def _serialized_operation(operation: dict) -> dict:
    return {
        "event_id": operation["event_id"],
        "before": {
            "planned_start": _iso(operation["before"]["planned_start"]),
            "planned_end": _iso(operation["before"]["planned_end"]),
        },
        "after": {
            "planned_start": _iso(operation["after"]["planned_start"]),
            "planned_end": _iso(operation["after"]["planned_end"]),
        },
    }


def _matches_trusted(action: dict, snapshot: dict) -> bool:
    return (
        snapshot["calendar_id"] == action["calendar_id"]
        and snapshot["external_event_id"] == action["external_event_id"]
        and snapshot["etag"] == action["before"]["etag"]
        and _iso(snapshot["planned_start"]) == action["before"]["planned_start"]
        and _iso(snapshot["planned_end"]) == action["before"]["planned_end"]
    )


def _matches_applied(action: dict, snapshot: dict) -> bool:
    return (
        snapshot["calendar_id"] == action["calendar_id"]
        and snapshot["external_event_id"] == action["external_event_id"]
        and snapshot["etag"] == action["after"]["etag"]
        and _matches_requested(action, snapshot, "after")
        and snapshot["private"]
        and snapshot["classification"] == "flexible"
        and snapshot["organizer_self"]
        and snapshot["attendees_count"] == 0
    )


def _matches_requested(action: dict, snapshot: dict, side: str) -> bool:
    return (
        snapshot["calendar_id"] == action["calendar_id"]
        and snapshot["external_event_id"] == action["external_event_id"]
        and _iso(snapshot["planned_start"]) == action[side]["planned_start"]
        and _iso(snapshot["planned_end"]) == action[side]["planned_end"]
        and isinstance(snapshot["etag"], str)
        and bool(snapshot["etag"])
    )


def _level_three_copy(policy: dict) -> dict:
    result = copy.deepcopy(policy)
    result["autonomy_level"] = 3
    result["approved_plan_hash"] = None
    return result


def _undo_policy_reasons(receipt: dict, policy: dict) -> List[str]:
    parsed = _policy(policy)
    if parsed is None:
        return ["INVALID_POLICY"]
    level, calendars, event_ids, approved, kill_switch = parsed
    reasons = set()
    if kill_switch:
        reasons.add("KILL_SWITCH_ACTIVE")
    if level <= 1:
        reasons.add("AUTONOMY_LEVEL_FORBIDS_ACTION")
    elif level == 2 and approved != receipt["plan_hash"]:
        reasons.add("PLAN_HASH_MISMATCH" if approved else "PLAN_APPROVAL_REQUIRED")
    for action in receipt["actions"]:
        if action["calendar_id"] not in calendars:
            reasons.add("CALENDAR_NOT_ALLOWED")
        if action["event_id"] not in event_ids:
            reasons.add("EVENT_NOT_ALLOWED")
    return sorted(reasons)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
