"""Durable receipts with an append-only SQLite audit journal."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional
import uuid


class AuditStore:
    """Persist minimal action state and immutable audit records."""

    def __init__(self, path: Any) -> None:
        try:
            database = os.fspath(path)
        except TypeError as exc:
            raise ValueError("audit path must be a filesystem path") from exc
        if not isinstance(database, str) or not database:
            raise ValueError("audit path must be a non-empty filesystem path")
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    id TEXT PRIMARY KEY,
                    idempotency_hash TEXT NOT NULL UNIQUE,
                    plan_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actions_json TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    audit_refs_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_records (
                    id TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    FOREIGN KEY(receipt_id) REFERENCES receipts(id)
                );
                CREATE TRIGGER IF NOT EXISTS audit_records_no_update
                BEFORE UPDATE ON audit_records
                BEGIN SELECT RAISE(ABORT, 'audit records are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS audit_records_no_delete
                BEFORE DELETE ON audit_records
                BEGIN SELECT RAISE(ABORT, 'audit records are append-only'); END;
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def get_receipt(self, receipt_id: str) -> Optional[dict]:
        if not isinstance(receipt_id, str) or not receipt_id:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM receipts WHERE id = ?", (receipt_id,)
            ).fetchone()
        return _receipt(row) if row is not None else None

    def list_audit(self, receipt_id: Optional[str] = None) -> List[dict]:
        with self._lock:
            if receipt_id is None:
                rows = self._connection.execute(
                    "SELECT * FROM audit_records ORDER BY rowid"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM audit_records WHERE receipt_id = ? ORDER BY rowid",
                    (receipt_id,),
                ).fetchall()
        return [
            {
                "id": row["id"],
                "receipt_id": row["receipt_id"],
                "kind": row["kind"],
                "data": json.loads(row["data_json"]),
            }
            for row in rows
        ]

    def _get_or_create_intent(
        self,
        idempotency_hash: str,
        plan_digest: str,
        actions: List[dict],
    ) -> tuple[dict, bool]:
        receipt_id = uuid.uuid4().hex
        receipt = {
            "id": receipt_id,
            "plan_hash": plan_digest,
            "status": "unknown",
            "actions": copy.deepcopy(actions),
            "reason_codes": [],
            "audit_refs": [],
        }
        audit_id = uuid.uuid4().hex
        receipt["audit_refs"].append(audit_id)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO receipts
                    (id, idempotency_hash, plan_hash, status, actions_json,
                     reason_codes_json, audit_refs_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_hash) DO NOTHING
                """,
                (
                    receipt_id,
                    idempotency_hash,
                    plan_digest,
                    receipt["status"],
                    _json(receipt["actions"]),
                    "[]",
                    _json(receipt["audit_refs"]),
                ),
            )
            created = cursor.rowcount == 1
            if created:
                self._connection.execute(
                    "INSERT INTO audit_records (id, receipt_id, kind, data_json) VALUES (?, ?, ?, ?)",
                    (audit_id, receipt_id, "intent_persisted", "{}"),
                )
                result = receipt
            else:
                row = self._connection.execute(
                    "SELECT * FROM receipts WHERE idempotency_hash = ?",
                    (idempotency_hash,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("idempotency conflict without a receipt")
                result = _receipt(row)
        if result["plan_hash"] != plan_digest:
            raise ValueError("idempotency_key is already bound to another plan")
        return copy.deepcopy(result), created

    def _create_denied(
        self,
        idempotency_hash: str,
        plan_digest: str,
        reason_codes: List[str],
    ) -> dict:
        receipt, created = self._get_or_create_intent(
            idempotency_hash, plan_digest, []
        )
        if not created:
            return receipt
        return self._record(
            receipt,
            status="denied",
            reason_codes=reason_codes,
            kind="policy_denied",
            data={"reason_codes": sorted(set(reason_codes))},
        )

    def _claim_undo(self, receipt_id: str) -> tuple[dict, bool]:
        """Atomically make one process responsible for an undo attempt."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM receipts WHERE id = ?", (receipt_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("receipt does not exist")
                current = _receipt(row)
                if current["status"] != "applied":
                    self._connection.commit()
                    return current, False
                updated = self._update_in_transaction(
                    current,
                    status="unknown",
                    reason_codes=[],
                    kind="undo_started",
                    data={
                        "event_ids": [
                            action["event_id"]
                            for action in reversed(current["actions"])
                        ]
                    },
                )
                self._connection.commit()
                return updated, True
            except BaseException:
                self._connection.rollback()
                raise

    def _record_if_status(
        self,
        receipt_id: str,
        *,
        expected_status: str,
        status: str,
        reason_codes: List[str],
        kind: str,
        data: dict,
    ) -> dict:
        """Record only while the durable receipt still has the expected state."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM receipts WHERE id = ?", (receipt_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("receipt does not exist")
                current = _receipt(row)
                if current["status"] != expected_status:
                    self._connection.commit()
                    return current
                updated = self._update_in_transaction(
                    current,
                    status=status,
                    reason_codes=reason_codes,
                    kind=kind,
                    data=data,
                )
                self._connection.commit()
                return updated
            except BaseException:
                self._connection.rollback()
                raise

    def _record(
        self,
        receipt: dict,
        *,
        status: str,
        actions: Optional[List[dict]] = None,
        reason_codes: Optional[List[str]] = None,
        kind: str,
        data: dict,
    ) -> dict:
        with self._lock, self._connection:
            return self._update_in_transaction(
                receipt,
                status=status,
                actions=actions,
                reason_codes=reason_codes,
                kind=kind,
                data=data,
            )

    def _update_in_transaction(
        self,
        receipt: dict,
        *,
        status: str,
        actions: Optional[List[dict]] = None,
        reason_codes: Optional[List[str]] = None,
        kind: str,
        data: dict,
    ) -> dict:
        updated = copy.deepcopy(receipt)
        updated["status"] = status
        if actions is not None:
            updated["actions"] = copy.deepcopy(actions)
        if reason_codes is not None:
            updated["reason_codes"] = sorted(set(reason_codes))
        audit_id = uuid.uuid4().hex
        updated["audit_refs"].append(audit_id)
        cursor = self._connection.execute(
            """
            UPDATE receipts
            SET status = ?, actions_json = ?, reason_codes_json = ?, audit_refs_json = ?
            WHERE id = ?
            """,
            (
                updated["status"],
                _json(updated["actions"]),
                _json(updated["reason_codes"]),
                _json(updated["audit_refs"]),
                updated["id"],
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("receipt does not exist")
        self._connection.execute(
            "INSERT INTO audit_records (id, receipt_id, kind, data_json) VALUES (?, ?, ?, ?)",
            (audit_id, updated["id"], kind, _json(data)),
        )
        return updated


def _receipt(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "plan_hash": row["plan_hash"],
        "status": row["status"],
        "actions": json.loads(row["actions_json"]),
        "reason_codes": json.loads(row["reason_codes_json"]),
        "audit_refs": json.loads(row["audit_refs_json"]),
    }


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("audit value must be finite acyclic JSON") from exc
