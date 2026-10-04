import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.db import get_connection
from app.hlc import max_ts, ts_eq, ts_gt
from app.locks import lock_for
from app.models import ChangeRequest, ResolveRequest

NEW_RECORD_VERSION: Dict[str, int] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _blank_record(record_id: str) -> Dict[str, Any]:
    now = _now()
    return {
        "id": record_id,
        "status": "active",
        "fields": {},
        "version": {},
        "delete_ts": None,
        "pending_conflict": None,
        "created_at": now,
        "updated_at": now,
    }


def _row_to_record(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "fields": json.loads(row["fields"]),
        "version": json.loads(row["version"]),
        "delete_ts": json.loads(row["delete_ts"]) if row["delete_ts"] else None,
        "pending_conflict": json.loads(row["pending_conflict"]) if row["pending_conflict"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def load_record(conn: sqlite3.Connection, record_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        return None
    return _row_to_record(row)


def save_record(conn: sqlite3.Connection, record: Dict[str, Any]) -> None:
    record["updated_at"] = _now()
    conn.execute(
        """
        INSERT INTO records (id, status, fields, version, delete_ts, pending_conflict, created_at, updated_at)
        VALUES (:id, :status, :fields, :version, :delete_ts, :pending_conflict, :created_at, :updated_at)
        ON CONFLICT(id) DO UPDATE SET
            status=excluded.status,
            fields=excluded.fields,
            version=excluded.version,
            delete_ts=excluded.delete_ts,
            pending_conflict=excluded.pending_conflict,
            updated_at=excluded.updated_at
        """,
        {
            "id": record["id"],
            "status": record["status"],
            "fields": json.dumps(record["fields"]),
            "version": json.dumps(record["version"]),
            "delete_ts": json.dumps(record["delete_ts"]) if record["delete_ts"] else None,
            "pending_conflict": json.dumps(record["pending_conflict"]) if record["pending_conflict"] else None,
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        },
    )


def list_records(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM records ORDER BY updated_at DESC").fetchall()
    return [_row_to_record(r) for r in rows]


def get_history(conn: sqlite3.Connection, record_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM change_log WHERE record_id = ? ORDER BY id ASC", (record_id,)
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "device_id": r["device_id"],
                "change_id": r["change_id"],
                "operation": r["operation"],
                "outcome": r["outcome"],
                "applied_fields": json.loads(r["applied_fields"]) if r["applied_fields"] else None,
                "rejected_fields": json.loads(r["rejected_fields"]) if r["rejected_fields"] else None,
                "server_version_after": json.loads(r["server_version_after"]) if r["server_version_after"] else None,
                "created_at": r["created_at"],
            }
        )
    return out


def _log_change(
    conn: sqlite3.Connection,
    record_id: str,
    device_id: str,
    change_id: str,
    operation: str,
    outcome: str,
    applied_fields: Optional[dict],
    rejected_fields: Optional[dict],
    server_version_after: Optional[dict],
) -> None:
    conn.execute(
        """
        INSERT INTO change_log
            (record_id, device_id, change_id, operation, outcome, applied_fields, rejected_fields, server_version_after, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            device_id,
            change_id,
            operation,
            outcome,
            json.dumps(applied_fields) if applied_fields is not None else None,
            json.dumps(rejected_fields) if rejected_fields is not None else None,
            json.dumps(server_version_after) if server_version_after is not None else None,
            _now(),
        ),
    )


def _get_processed(conn: sqlite3.Connection, device_id: str, change_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT result FROM processed_changes WHERE device_id = ? AND change_id = ?",
        (device_id, change_id),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["result"])


def _store_processed(conn: sqlite3.Connection, device_id: str, change_id: str, record_id: str, result: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO processed_changes (device_id, change_id, record_id, result, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (device_id, change_id, record_id, json.dumps(result), _now()),
    )


def _is_stale(record: Dict[str, Any], base_version: Dict[str, int]) -> bool:
    for device, count in record["version"].items():
        if count > base_version.get(device, 0):
            return True
    return False


def _merge_version(version: Dict[str, int], device_id: str) -> Dict[str, int]:
    merged = dict(version)
    merged[device_id] = merged.get(device_id, 0) + 1
    return merged


def _apply_upsert(record: Dict[str, Any], change: ChangeRequest, device_id: str) -> Dict[str, Any]:
    field_results: Dict[str, Any] = {}
    applied: Dict[str, Any] = {}
    rejected: Dict[str, Any] = {}
    applied_any = False

    for name, incoming in change.fields.items():
        current = record["fields"].get(name)
        if current is None or ts_gt(incoming.timestamp, current["timestamp"]):
            record["fields"][name] = {"value": incoming.value, "timestamp": list(incoming.timestamp)}
            field_results[name] = {"status": "accepted"}
            applied[name] = {"value": incoming.value, "timestamp": list(incoming.timestamp)}
            applied_any = True
        elif ts_eq(incoming.timestamp, current["timestamp"]):
            field_results[name] = {"status": "accepted", "note": "duplicate event, no-op"}
        else:
            field_results[name] = {
                "status": "rejected",
                "reason": "stale",
                "current_value": current["value"],
                "current_timestamp": current["timestamp"],
            }
            rejected[name] = field_results[name]

    if applied_any:
        record["version"] = _merge_version(record["version"], device_id)

    statuses = {r["status"] for r in field_results.values()}
    if statuses == {"accepted"}:
        overall = "accepted"
    elif "accepted" in statuses:
        overall = "merged"
    else:
        overall = "rejected"

    return {
        "overall": overall,
        "fields": field_results,
        "applied": applied,
        "rejected": rejected,
    }


def process_change(conn: sqlite3.Connection, device_id: str, change: ChangeRequest) -> Dict[str, Any]:
    cached = _get_processed(conn, device_id, change.change_id)
    if cached is not None:
        return cached

    lock = lock_for(change.record_id)
    with lock:
        record = load_record(conn, change.record_id)
        is_new = record is None
        if record is None:
            record = _blank_record(change.record_id)

        stale = _is_stale(record, change.base_version)
        server_version_before = dict(record["version"])

        if change.operation == "delete":
            result = _apply_delete(record, change, device_id)
        else:
            if record["status"] == "deleted":
                result = _apply_upsert_to_deleted(record, change, device_id)
            elif record["status"] == "conflict":
                result = {
                    "overall": "conflict_pending",
                    "fields": {},
                    "applied": {},
                    "rejected": {},
                    "message": "record has an unresolved conflict; call /records/{id}/resolve first",
                }
            else:
                result = _apply_upsert(record, change, device_id)

        save_record(conn, record)

        response = {
            "change_id": change.change_id,
            "record_id": change.record_id,
            "status": result["overall"],
            "stale": stale,
            "was_new_record": is_new,
            "server_version": record["version"],
            "fields": result.get("fields", {}),
            "record_status": record["status"],
        }
        if "message" in result:
            response["message"] = result["message"]

        _log_change(
            conn,
            change.record_id,
            device_id,
            change.change_id,
            change.operation,
            result["overall"],
            result.get("applied") or None,
            result.get("rejected") or None,
            record["version"],
        )
        _store_processed(conn, device_id, change.change_id, change.record_id, response)
        conn.commit()
        return response


def _apply_delete(record: Dict[str, Any], change: ChangeRequest, device_id: str) -> Dict[str, Any]:
    delete_ts = list(change.timestamp)

    if record["status"] == "deleted":
        if ts_gt(delete_ts, record["delete_ts"]):
            record["delete_ts"] = delete_ts
        return {"overall": "accepted", "fields": {}, "applied": {}, "rejected": {}}

    if record["status"] == "conflict":
        return {
            "overall": "conflict_pending",
            "fields": {},
            "applied": {},
            "rejected": {},
            "message": "record has an unresolved conflict; call /records/{id}/resolve first",
        }

    newer_fields = {
        name: f for name, f in record["fields"].items() if ts_gt(f["timestamp"], delete_ts)
    }
    if newer_fields:
        record["status"] = "conflict"
        record["pending_conflict"] = {
            "type": "delete_vs_edit",
            "delete_ts": delete_ts,
            "deleting_device": device_id,
            "change_id": change.change_id,
            "conflicting_fields": newer_fields,
        }
        return {
            "overall": "conflict",
            "fields": {},
            "applied": {},
            "rejected": {},
            "message": "a field was edited concurrently with this delete; manual resolution required",
        }

    record["status"] = "deleted"
    record["delete_ts"] = delete_ts
    record["version"] = _merge_version(record["version"], device_id)
    return {"overall": "accepted", "fields": {}, "applied": {}, "rejected": {}}


def _apply_upsert_to_deleted(record: Dict[str, Any], change: ChangeRequest, device_id: str) -> Dict[str, Any]:
    change_ts = max_ts([f.timestamp for f in change.fields.values()])
    delete_ts = tuple(record["delete_ts"])

    if change_ts > delete_ts:
        record["status"] = "conflict"
        record["pending_conflict"] = {
            "type": "edit_vs_delete",
            "delete_ts": list(delete_ts),
            "editing_device": device_id,
            "change_id": change.change_id,
            "pending_fields": {k: {"value": v.value, "timestamp": list(v.timestamp)} for k, v in change.fields.items()},
        }
        return {
            "overall": "conflict",
            "fields": {},
            "applied": {},
            "rejected": {},
            "message": "record was deleted on another device after this edit; manual resolution required",
        }

    field_results = {
        name: {"status": "rejected", "reason": "record_deleted", "deleted_at": list(delete_ts)}
        for name in change.fields
    }
    return {"overall": "rejected", "fields": field_results, "applied": {}, "rejected": field_results}


def resolve_conflict(conn: sqlite3.Connection, record_id: str, req: ResolveRequest) -> Dict[str, Any]:
    lock = lock_for(record_id)
    with lock:
        record = load_record(conn, record_id)
        if record is None:
            raise ValueError("record not found")
        if record["status"] != "conflict":
            raise ValueError("record has no pending conflict")

        pending = record["pending_conflict"]

        if req.resolution == "keep_deleted":
            record["status"] = "deleted"
            if pending["type"] == "delete_vs_edit":
                record["delete_ts"] = pending["delete_ts"]
            record["pending_conflict"] = None
            record["version"] = _merge_version(record["version"], req.device_id)
        else:
            record["status"] = "active"
            if pending["type"] == "edit_vs_delete":
                for name, f in pending["pending_fields"].items():
                    record["fields"][name] = f
            record["delete_ts"] = None
            record["pending_conflict"] = None
            record["version"] = _merge_version(record["version"], req.device_id)

        save_record(conn, record)
        _log_change(
            conn,
            record_id,
            req.device_id,
            f"resolve-{req.device_id}-{req.timestamp[0]}-{req.timestamp[1]}",
            "resolve",
            req.resolution,
            record["fields"] if req.resolution == "restore" else None,
            None,
            record["version"],
        )
        conn.commit()
        return record
