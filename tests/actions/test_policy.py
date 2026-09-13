import copy
import hashlib
import json
import math
import unittest

from packages.aeon_actions import evaluate_plan, plan_hash


def operation(event_id="focus"):
    return {
        "event_id": event_id,
        "before": {
            "planned_start": "2026-09-14T15:00:00Z",
            "planned_end": "2026-09-14T16:00:00Z",
        },
        "after": {
            "planned_start": "2026-09-14T13:00:00Z",
            "planned_end": "2026-09-14T14:00:00Z",
        },
    }


def plan(*operations):
    return {
        "id": "untrusted-plan-id",
        "operations": list(operations or [operation()]),
        "requires_approval": False,
    }


def snapshot(**changes):
    value = {
        "calendar_id": "primary",
        "external_event_id": "google-focus",
        "etag": "etag-1",
        "planned_start": "2026-09-14T15:00:00Z",
        "planned_end": "2026-09-14T16:00:00Z",
        "private": True,
        "classification": "flexible",
        "organizer_self": True,
        "attendees_count": 0,
    }
    value.update(changes)
    return value


def policy(level=3, **changes):
    value = {
        "autonomy_level": level,
        "allowed_calendar_ids": ["primary"],
        "allowed_event_ids": ["focus"],
        "approved_plan_hash": None,
        "kill_switch": False,
    }
    value.update(changes)
    return value


class PlanHashTests(unittest.TestCase):
    def test_hash_is_canonical_stable_and_does_not_mutate(self):
        value = {"z": ["é", 1], "a": {"value": True}}
        reordered = {"a": {"value": True}, "z": ["é", 1]}
        original = copy.deepcopy(value)
        expected = hashlib.sha256(
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(expected, plan_hash(value))
        self.assertEqual(plan_hash(value), plan_hash(reordered))
        self.assertEqual(original, value)

    def test_rejects_non_json_cycles_and_non_finite_numbers(self):
        cyclic = {}
        cyclic["self"] = cyclic
        for value in ([], {"value": object()}, {"value": math.nan}, cyclic):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                plan_hash(value)


class EvaluatePlanTests(unittest.TestCase):
    def test_level_three_allows_only_trusted_private_flexible_event(self):
        proposed = plan()
        events = {"focus": snapshot()}
        rules = policy()
        originals = copy.deepcopy((proposed, events, rules))
        self.assertEqual(
            {"allowed": True, "reason_codes": []},
            evaluate_plan(proposed, events, rules),
        )
        self.assertEqual(originals, (proposed, events, rules))

    def test_levels_zero_and_one_forbid_and_level_two_requires_exact_approval(self):
        proposed = plan()
        events = {"focus": snapshot()}
        for level in (0, 1):
            decision = evaluate_plan(proposed, events, policy(level))
            self.assertIn("AUTONOMY_LEVEL_FORBIDS_ACTION", decision["reason_codes"])
        self.assertIn(
            "PLAN_APPROVAL_REQUIRED",
            evaluate_plan(proposed, events, policy(2))["reason_codes"],
        )
        self.assertIn(
            "PLAN_HASH_MISMATCH",
            evaluate_plan(
                proposed,
                events,
                policy(2, approved_plan_hash="0" * 64),
            )["reason_codes"],
        )
        self.assertTrue(
            evaluate_plan(
                proposed,
                events,
                policy(2, approved_plan_hash=plan_hash(proposed)),
            )["allowed"]
        )

    def test_kill_switch_and_allowlists_cannot_be_overridden_by_plan(self):
        proposed = plan()
        proposed["allowed_event_ids"] = ["focus"]
        proposed["autonomy_level"] = 3
        decision = evaluate_plan(
            proposed,
            {"focus": snapshot()},
            policy(kill_switch=True, allowed_calendar_ids=[], allowed_event_ids=[]),
        )
        self.assertEqual(
            ["CALENDAR_NOT_ALLOWED", "EVENT_NOT_ALLOWED", "KILL_SWITCH_ACTIVE"],
            decision["reason_codes"],
        )

    def test_rejects_participants_fixed_public_or_unowned_events(self):
        cases = (
            (snapshot(attendees_count=1), "EVENT_HAS_ATTENDEES"),
            (snapshot(classification="fixed"), "EVENT_NOT_FLEXIBLE"),
            (snapshot(private=False), "EVENT_NOT_PRIVATE"),
            (snapshot(organizer_self=False), "EVENT_NOT_OWNED"),
        )
        for event, reason in cases:
            with self.subTest(reason=reason):
                decision = evaluate_plan(plan(), {"focus": event}, policy())
                self.assertFalse(decision["allowed"])
                self.assertIn(reason, decision["reason_codes"])

    def test_rejects_missing_duplicate_empty_invalid_or_changed_operations(self):
        cases = [
            (plan(), {}, "EVENT_NOT_FOUND"),
            (plan(operation(), operation()), {"focus": snapshot()}, "DUPLICATE_EVENT_TARGET"),
            ({"operations": []}, {"focus": snapshot()}, "EMPTY_OPERATIONS"),
            ({"operations": "bad"}, {"focus": snapshot()}, "INVALID_PLAN"),
            (plan(operation()), {"focus": snapshot(planned_start="2026-09-14T15:30:00Z")}, "PLAN_NO_LONGER_VALID"),
        ]
        for proposed, events, reason in cases:
            with self.subTest(reason=reason):
                decision = evaluate_plan(proposed, events, policy())
                self.assertFalse(decision["allowed"])
                self.assertIn(reason, decision["reason_codes"])

    def test_rejects_duration_change_invalid_range_and_noop(self):
        changed_duration = operation()
        changed_duration["after"]["planned_end"] = "2026-09-14T14:30:00Z"
        invalid_range = operation()
        invalid_range["after"]["planned_end"] = invalid_range["after"]["planned_start"]
        noop = operation()
        noop["after"] = copy.deepcopy(noop["before"])
        for candidate, reason in (
            (changed_duration, "DURATION_CHANGED"),
            (invalid_range, "INVALID_TIME_RANGE"),
            (noop, "EMPTY_OPERATION"),
        ):
            with self.subTest(reason=reason):
                decision = evaluate_plan(plan(candidate), {"focus": snapshot()}, policy())
                self.assertIn(reason, decision["reason_codes"])

    def test_missing_or_malformed_policy_and_current_event_are_denied(self):
        self.assertEqual(
            {"allowed": False, "reason_codes": ["INVALID_POLICY"]},
            evaluate_plan(plan(), {"focus": snapshot()}, {}),
        )
        self.assertIn(
            "INVALID_CURRENT_EVENT",
            evaluate_plan(plan(), {"focus": {"private": True}}, policy())["reason_codes"],
        )
        self.assertIn(
            "INVALID_CURRENT_EVENTS",
            evaluate_plan(plan(), [], policy())["reason_codes"],
        )
        for classification in ([], {}):
            with self.subTest(classification=type(classification).__name__):
                self.assertIn(
                    "INVALID_CURRENT_EVENT",
                    evaluate_plan(
                        plan(),
                        {"focus": snapshot(classification=classification)},
                        policy(),
                    )["reason_codes"],
                )


if __name__ == "__main__":
    unittest.main()
