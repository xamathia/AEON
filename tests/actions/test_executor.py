import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from packages.aeon_actions import (
    AuditStore,
    CalendarConflict,
    CalendarUnknownOutcome,
    PlanExecutor,
    plan_hash,
)


def operation(
    event_id="focus",
    before_start="2026-09-14T15:00:00Z",
    after_start="2026-09-14T13:00:00Z",
):
    before_hour = int(before_start[11:13])
    after_hour = int(after_start[11:13])
    return {
        "event_id": event_id,
        "before": {
            "planned_start": before_start,
            "planned_end": before_start[:11] + f"{before_hour + 1:02d}" + before_start[13:],
        },
        "after": {
            "planned_start": after_start,
            "planned_end": after_start[:11] + f"{after_hour + 1:02d}" + after_start[13:],
        },
    }


def proposed_plan(*operations, **extra):
    value = {"id": "plan-proposal", "operations": list(operations or [operation()])}
    value.update(extra)
    return value


def event_snapshot(
    event_id="focus",
    start="2026-09-14T15:00:00Z",
    etag="etag-focus-1",
    **changes,
):
    hour = int(start[11:13])
    value = {
        "calendar_id": "primary",
        "external_event_id": f"google-{event_id}",
        "etag": etag,
        "planned_start": start,
        "planned_end": start[:11] + f"{hour + 1:02d}" + start[13:],
        "private": True,
        "classification": "flexible",
        "organizer_self": True,
        "attendees_count": 0,
    }
    value.update(changes)
    return value


def trusted_events(two=False):
    result = {"focus": event_snapshot()}
    if two:
        result["admin"] = event_snapshot(
            "admin", start="2026-09-14T17:00:00Z", etag="etag-admin-1"
        )
    return result


def policy(*event_ids, level=3, **changes):
    value = {
        "autonomy_level": level,
        "allowed_calendar_ids": ["primary"],
        "allowed_event_ids": list(event_ids or ["focus"]),
        "approved_plan_hash": None,
        "kill_switch": False,
    }
    value.update(changes)
    return value


class FakeCalendar:
    def __init__(self, events, failures=None):
        self.events = {
            value["external_event_id"]: copy.deepcopy(value) for value in events.values()
        }
        self.failures = failures or {}
        self.get_calls = []
        self.patch_calls = []

    def get_event(self, calendar_id, external_event_id):
        self.get_calls.append((calendar_id, external_event_id))
        return copy.deepcopy(self.events[external_event_id])

    def patch_times(
        self,
        calendar_id,
        external_event_id,
        *,
        planned_start,
        planned_end,
        if_match,
        send_updates="none",
    ):
        call_number = len(self.patch_calls) + 1
        self.patch_calls.append(
            {
                "calendar_id": calendar_id,
                "external_event_id": external_event_id,
                "planned_start": planned_start,
                "planned_end": planned_end,
                "if_match": if_match,
                "send_updates": send_updates,
            }
        )
        current = self.events[external_event_id]
        if current["etag"] != if_match:
            raise CalendarConflict("stale private etag")
        failure = self.failures.get(call_number)
        if failure == "conflict":
            raise CalendarConflict("human change private body")
        if failure == "error":
            raise RuntimeError("provider failure private body")
        if failure in {"unknown", "crash"}:
            current["planned_start"] = planned_start
            current["planned_end"] = planned_end
            current["etag"] = f"etag-write-{call_number}"
            if failure == "unknown":
                raise CalendarUnknownOutcome("timeout private body")
            raise SystemExit("simulated process loss")
        current["planned_start"] = planned_start
        current["planned_end"] = planned_end
        current["etag"] = f"etag-write-{call_number}"
        return copy.deepcopy(current)


class BarrierAuditStore(AuditStore):
    def __init__(self, path, intent_barrier):
        self._intent_barrier = intent_barrier
        super().__init__(path)

    def _get_or_create_intent(self, idempotency_hash, plan_digest, actions):
        self._intent_barrier.wait()
        return super()._get_or_create_intent(
            idempotency_hash, plan_digest, actions
        )


class BlockingUndoCalendar(FakeCalendar):
    def __init__(self, events):
        super().__init__(events)
        self.undo_entered = threading.Event()
        self.release_undo = threading.Event()

    def patch_times(self, *args, **kwargs):
        if len(self.patch_calls) == 1:
            self.undo_entered.set()
            if not self.release_undo.wait(5):
                raise AssertionError("concurrent undo test did not release Calendar")
        return super().patch_times(*args, **kwargs)


class FailOnceGetCalendar(FakeCalendar):
    def __init__(self, events):
        super().__init__(events)
        self.fail_next_get = False

    def get_event(self, calendar_id, external_event_id):
        if self.fail_next_get:
            self.fail_next_get = False
            self.get_calls.append((calendar_id, external_event_id))
            raise TimeoutError("temporary private read failure")
        return super().get_event(calendar_id, external_event_id)


class ExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "audit.sqlite3"
        self.store = AuditStore(self.database)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def executor(self, calendar):
        return PlanExecutor(calendar, self.store)


class ExecuteTests(ExecutorTestCase):
    def test_denied_plan_is_audited_without_calendar_call(self):
        events = trusted_events()
        calendar = FakeCalendar(events)
        receipt = self.executor(calendar).execute(
            proposed_plan(secret_token="must-not-be-stored"),
            current_events=events,
            policy=policy(level=1),
            idempotency_key="denied-key",
        )
        self.assertEqual("denied", receipt["status"])
        self.assertIn("AUTONOMY_LEVEL_FORBIDS_ACTION", receipt["reason_codes"])
        self.assertEqual([], calendar.get_calls)
        self.assertEqual([], calendar.patch_calls)
        persisted = json.dumps(
            {"receipt": receipt, "audit": self.store.list_audit(receipt["id"])},
            sort_keys=True,
        )
        self.assertNotIn("must-not-be-stored", persisted)

    def test_preflight_checks_every_target_before_first_write(self):
        events = trusted_events(two=True)
        calendar = FakeCalendar(events)
        calendar.events["google-admin"]["etag"] = "human-etag"
        plan = proposed_plan(
            operation(),
            operation("admin", "2026-09-14T17:00:00Z", "2026-09-14T18:00:00Z"),
        )
        receipt = self.executor(calendar).execute(
            plan,
            current_events=events,
            policy=policy("focus", "admin"),
            idempotency_key="preflight-key",
        )
        self.assertEqual("conflict", receipt["status"])
        self.assertEqual(["PLAN_NO_LONGER_VALID"], receipt["reason_codes"])
        self.assertEqual(2, len(calendar.get_calls))
        self.assertEqual([], calendar.patch_calls)

    def test_permissions_changed_after_preview_prevent_every_write(self):
        events = trusted_events()
        calendar = FakeCalendar(events)
        calendar.events["google-focus"]["attendees_count"] = 1
        receipt = self.executor(calendar).execute(
            proposed_plan(),
            current_events=events,
            policy=policy(),
            idempotency_key="permissions-changed",
        )
        self.assertEqual("conflict", receipt["status"])
        self.assertIn("EVENT_HAS_ATTENDEES", receipt["reason_codes"])
        self.assertEqual([], calendar.patch_calls)

    def test_two_actions_apply_with_if_match_and_no_notifications(self):
        events = trusted_events(two=True)
        calendar = FakeCalendar(events)
        plan = proposed_plan(
            operation(),
            operation("admin", "2026-09-14T17:00:00Z", "2026-09-14T18:00:00Z"),
        )
        receipt = self.executor(calendar).execute(
            plan,
            current_events=events,
            policy=policy("focus", "admin"),
            idempotency_key="apply-key",
        )
        self.assertEqual("applied", receipt["status"])
        self.assertEqual(["applied", "applied"], [item["state"] for item in receipt["actions"]])
        self.assertEqual(["etag-focus-1", "etag-admin-1"], [call["if_match"] for call in calendar.patch_calls])
        self.assertTrue(all(call["send_updates"] == "none" for call in calendar.patch_calls))
        self.assertEqual("etag-write-1", receipt["actions"][0]["after"]["etag"])

    def test_second_failure_compensates_first_in_reverse(self):
        events = trusted_events(two=True)
        calendar = FakeCalendar(events, failures={2: "error"})
        plan = proposed_plan(
            operation(),
            operation("admin", "2026-09-14T17:00:00Z", "2026-09-14T18:00:00Z"),
        )
        receipt = self.executor(calendar).execute(
            plan,
            current_events=events,
            policy=policy("focus", "admin"),
            idempotency_key="compensate-key",
        )
        self.assertEqual("compensated", receipt["status"])
        self.assertEqual("compensated", receipt["actions"][0]["state"])
        self.assertEqual("failed", receipt["actions"][1]["state"])
        compensation = calendar.patch_calls[2]
        self.assertEqual("google-focus", compensation["external_event_id"])
        self.assertEqual("etag-write-1", compensation["if_match"])
        self.assertEqual(events["focus"]["planned_start"], calendar.events["google-focus"]["planned_start"])
        audit_kinds = [item["kind"] for item in self.store.list_audit(receipt["id"])]
        self.assertLess(
            audit_kinds.index("compensation_action_started"),
            audit_kinds.index("action_compensated"),
        )
        persisted = json.dumps(
            {"receipt": receipt, "audit": self.store.list_audit(receipt["id"])},
            sort_keys=True,
        )
        self.assertNotIn("provider failure private body", persisted)

    def test_human_change_prevents_compensation_without_overwrite(self):
        events = trusted_events(two=True)
        calendar = FakeCalendar(events, failures={2: "error", 3: "conflict"})
        plan = proposed_plan(
            operation(),
            operation("admin", "2026-09-14T17:00:00Z", "2026-09-14T18:00:00Z"),
        )
        receipt = self.executor(calendar).execute(
            plan,
            current_events=events,
            policy=policy("focus", "admin"),
            idempotency_key="partial-key",
        )
        self.assertEqual("partial", receipt["status"])
        self.assertEqual("compensation_conflict", receipt["actions"][0]["state"])
        self.assertEqual(3, len(calendar.patch_calls))

    def test_unknown_outcome_is_never_retried_after_reopen(self):
        events = trusted_events()
        calendar = FakeCalendar(events, failures={1: "unknown"})
        plan = proposed_plan(private_mail_body="must-not-be-stored")
        executor = self.executor(calendar)
        first = executor.execute(
            plan,
            current_events=events,
            policy=policy(),
            idempotency_key="unknown-key",
        )
        self.assertEqual("unknown", first["status"])
        self.assertEqual(1, len(calendar.patch_calls))
        self.store.close()
        self.store = AuditStore(self.database)
        second = self.executor(calendar).execute(
            plan,
            current_events=events,
            policy=policy(),
            idempotency_key="unknown-key",
        )
        self.assertEqual(first, second)
        self.assertEqual(1, len(calendar.patch_calls))
        persisted = json.dumps(self.store.list_audit(first["id"]), sort_keys=True)
        self.assertNotIn("must-not-be-stored", persisted)

    def test_persisted_intent_survives_process_loss_without_replay(self):
        events = trusted_events()
        calendar = FakeCalendar(events, failures={1: "crash"})
        plan = proposed_plan()
        with self.assertRaises(SystemExit):
            self.executor(calendar).execute(
                plan,
                current_events=events,
                policy=policy(),
                idempotency_key="crash-key",
            )
        self.store.close()
        self.store = AuditStore(self.database)
        receipt = self.executor(calendar).execute(
            plan,
            current_events=events,
            policy=policy(),
            idempotency_key="crash-key",
        )
        self.assertEqual("unknown", receipt["status"])
        self.assertEqual(1, len(calendar.patch_calls))

    def test_idempotency_returns_same_receipt_and_rejects_other_plan(self):
        events = trusted_events()
        calendar = FakeCalendar(events)
        executor = self.executor(calendar)
        proposed = proposed_plan()
        first = executor.execute(
            proposed,
            current_events=events,
            policy=policy(),
            idempotency_key="same-key",
        )
        calls = len(calendar.patch_calls)
        self.assertEqual(
            first,
            executor.execute(
                proposed,
                current_events=events,
                policy=policy(),
                idempotency_key="same-key",
            ),
        )
        self.assertEqual(calls, len(calendar.patch_calls))
        with self.assertRaises(ValueError):
            executor.execute(
                proposed_plan(operation(after_start="2026-09-14T12:00:00Z")),
                current_events=events,
                policy=policy(),
                idempotency_key="same-key",
            )

    def test_concurrent_execute_across_stores_returns_one_receipt_and_writes_once(self):
        self.store.close()
        barrier = threading.Barrier(2)
        first_store = BarrierAuditStore(self.database, barrier)
        second_store = BarrierAuditStore(self.database, barrier)
        self.store = first_store
        events = trusted_events()
        calendar = FakeCalendar(events)
        executors = (
            PlanExecutor(calendar, first_store),
            PlanExecutor(calendar, second_store),
        )
        results = []
        errors = []

        def run(executor):
            try:
                results.append(
                    executor.execute(
                        proposed_plan(),
                        current_events=events,
                        policy=policy(),
                        idempotency_key="concurrent-key",
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(executor,)) for executor in executors]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual([], errors)
            self.assertEqual(2, len(results))
            self.assertEqual(1, len({receipt["id"] for receipt in results}))
            self.assertEqual(1, len(calendar.patch_calls))
            persisted = first_store.get_receipt(results[0]["id"])
            self.assertEqual("applied", persisted["status"])
        finally:
            second_store.close()


class UndoTests(ExecutorTestCase):
    def test_undo_restores_with_post_apply_etag_and_second_undo_is_noop(self):
        events = trusted_events()
        calendar = FakeCalendar(events)
        executor = self.executor(calendar)
        applied = executor.execute(
            proposed_plan(),
            current_events=events,
            policy=policy(),
            idempotency_key="undo-key",
        )
        undone = executor.undo(applied["id"], policy=policy())
        self.assertEqual("undone", undone["status"])
        self.assertEqual("etag-write-1", calendar.patch_calls[1]["if_match"])
        self.assertEqual(events["focus"]["planned_start"], calendar.events["google-focus"]["planned_start"])
        call_count = len(calendar.patch_calls)
        self.assertEqual(undone, executor.undo(applied["id"], policy=policy()))
        self.assertEqual(call_count, len(calendar.patch_calls))

    def test_undo_preserves_a_later_human_change(self):
        events = trusted_events()
        calendar = FakeCalendar(events)
        executor = self.executor(calendar)
        applied = executor.execute(
            proposed_plan(),
            current_events=events,
            policy=policy(),
            idempotency_key="human-change-key",
        )
        calendar.events["google-focus"]["etag"] = "human-etag"
        receipt = executor.undo(applied["id"], policy=policy())
        self.assertEqual("conflict", receipt["status"])
        self.assertEqual(["HUMAN_CHANGE_DETECTED"], receipt["reason_codes"])
        self.assertEqual(1, len(calendar.patch_calls))

    def test_undo_read_failure_keeps_applied_state_and_can_be_retried(self):
        events = trusted_events()
        calendar = FailOnceGetCalendar(events)
        executor = self.executor(calendar)
        applied = executor.execute(
            proposed_plan(),
            current_events=events,
            policy=policy(),
            idempotency_key="undo-read-failure-key",
        )
        calendar.fail_next_get = True
        failed = executor.undo(applied["id"], policy=policy())
        self.assertEqual("applied", failed["status"])
        self.assertEqual(["UNDO_PREFLIGHT_FAILED"], failed["reason_codes"])
        self.assertEqual(1, len(calendar.patch_calls))
        undone = executor.undo(applied["id"], policy=policy())
        self.assertEqual("undone", undone["status"])
        self.assertEqual(2, len(calendar.patch_calls))

    def test_process_loss_during_undo_is_reconstructed_as_unknown(self):
        events = trusted_events()
        calendar = FakeCalendar(events, failures={2: "crash"})
        executor = self.executor(calendar)
        applied = executor.execute(
            proposed_plan(),
            current_events=events,
            policy=policy(),
            idempotency_key="undo-crash-key",
        )
        with self.assertRaises(SystemExit):
            executor.undo(applied["id"], policy=policy())
        self.store.close()
        self.store = AuditStore(self.database)
        recovered = self.store.get_receipt(applied["id"])
        self.assertEqual("unknown", recovered["status"])
        self.assertEqual("undo_action_started", self.store.list_audit(applied["id"])[-1]["kind"])

    def test_temporary_policy_denial_keeps_applied_state_and_allows_later_undo(self):
        events = trusted_events()
        calendar = FakeCalendar(events)
        executor = self.executor(calendar)
        applied = executor.execute(
            proposed_plan(),
            current_events=events,
            policy=policy(),
            idempotency_key="undo-policy-key",
        )
        denied = executor.undo(applied["id"], policy=policy(kill_switch=True))
        self.assertEqual("applied", denied["status"])
        self.assertEqual(["KILL_SWITCH_ACTIVE"], denied["reason_codes"])
        self.assertEqual(1, len(calendar.patch_calls))
        undone = executor.undo(applied["id"], policy=policy())
        self.assertEqual("undone", undone["status"])
        self.assertEqual(2, len(calendar.patch_calls))

    def test_concurrent_undo_across_stores_has_one_owner_and_one_patch(self):
        events = trusted_events()
        calendar = BlockingUndoCalendar(events)
        first_executor = self.executor(calendar)
        applied = first_executor.execute(
            proposed_plan(),
            current_events=events,
            policy=policy(),
            idempotency_key="concurrent-undo-key",
        )
        second_store = AuditStore(self.database)
        second_executor = PlanExecutor(calendar, second_store)
        start = threading.Barrier(2)
        one_finished = threading.Event()
        results = []
        errors = []

        def run(executor):
            try:
                start.wait()
                results.append(executor.undo(applied["id"], policy=policy()))
            except BaseException as exc:
                errors.append(exc)
            finally:
                one_finished.set()

        threads = [
            threading.Thread(target=run, args=(first_executor,)),
            threading.Thread(target=run, args=(second_executor,)),
        ]
        try:
            for thread in threads:
                thread.start()
            self.assertTrue(calendar.undo_entered.wait(5))
            self.assertTrue(one_finished.wait(5))
            self.assertEqual(1, len(calendar.patch_calls))
            calendar.release_undo.set()
            for thread in threads:
                thread.join(5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual([], errors)
            self.assertEqual(2, len(results))
            self.assertEqual({"unknown", "undone"}, {item["status"] for item in results})
            self.assertEqual(2, len(calendar.patch_calls))
            persisted = self.store.get_receipt(applied["id"])
            self.assertEqual("undone", persisted["status"])
            self.assertEqual(
                len(persisted["audit_refs"]),
                len(self.store.list_audit(applied["id"])),
            )
        finally:
            calendar.release_undo.set()
            second_store.close()


class AuditStoreTests(ExecutorTestCase):
    def test_receipts_and_audit_reopen_and_audit_rows_are_append_only(self):
        events = trusted_events()
        receipt = self.executor(FakeCalendar(events)).execute(
            proposed_plan(),
            current_events=events,
            policy=policy(),
            idempotency_key="durable-key",
        )
        audit = self.store.list_audit(receipt["id"])
        self.assertGreaterEqual(len(audit), 5)
        self.store.close()
        self.store = AuditStore(self.database)
        self.assertEqual(receipt, self.store.get_receipt(receipt["id"]))
        self.assertNotIn(b"durable-key", self.database.read_bytes())
        connection = sqlite3.connect(self.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE audit_records SET kind = 'changed'")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
