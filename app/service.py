"""Business logic: batch ingestion rules and ambiguity-aware queries.

No web-framework imports here -- the layer is plain Python so it can be unit
tested directly against SQLite.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from .db import read_transaction, write_transaction
from .timeline import Record, analyze_timeline


class IngestionError(Exception):
    """Base error; ``status_code`` mirrors the HTTP response."""

    status_code = 400

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ConflictError(IngestionError):
    """Same sample id, different content."""

    status_code = 409


class UnresolvableTimelineError(IngestionError):
    """No legal monotonic UTC timeline exists."""

    status_code = 422


@dataclass
class IngestionResult:
    batch_ref: str
    accepted: bool
    inserted: list[int]
    skipped_duplicates: list[int]
    record_count: int


def _all_rows(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        "SELECT sample_id, local_time, pass_count FROM records"
    ).fetchall()
    return {row["sample_id"]: row for row in rows}


def _write_audit(
    audit_conn: sqlite3.Connection | None,
    batch_ref: str,
    status: str,
    reason: str | None,
    count: int,
) -> None:
    """Persist audit on the independent autocommit connection.

    Audit rows therefore survive the rollback of a rejected business batch.
    When no audit connection is supplied (unit tests) the entry is skipped.
    """

    if audit_conn is None:
        return
    audit_conn.execute(
        "INSERT INTO batch_audit (batch_ref, status, reason, record_count) "
        "VALUES (?, ?, ?, ?)",
        (batch_ref, status, reason, count),
    )


def ingest_batch(
    conn: sqlite3.Connection,
    records: list[Record],
    audit_conn: sqlite3.Connection | None = None,
) -> IngestionResult:
    """Atomically accept or reject one CSV batch.

    Rules:
      * same sample id + same content -> idempotent skip (accepted once);
      * same sample id + different content -> whole batch rejected (409);
      * merged set admits no legal timeline -> whole batch rejected (422).
    """

    if not records:
        raise IngestionError("batch contains no records")

    batch_ref = uuid.uuid4().hex

    class _Rollback(Exception):
        def __init__(self, error: IngestionError):
            super().__init__(error.reason)
            self.error = error

    rejection: IngestionError | None = None
    accepted_result: IngestionResult | None = None

    try:
        with write_transaction(conn) as tx:
            # Full-table snapshot: feasibility must hold across *all*
            # warehoused records, not only ids touched by this batch.
            existing = _all_rows(tx)

            # 1) conflict check against already-warehoused records
            for rec in records:
                row = existing.get(rec.sample_id)
                if row is not None and (
                    row["local_time"] != rec.local_time
                    or row["pass_count"] != rec.pass_count
                ):
                    raise _Rollback(ConflictError(
                        f"sample_id {rec.sample_id} already stored with different "
                        f"content (local_time={row['local_time']}, "
                        f"pass_count={row['pass_count']})"
                    ))

            # 2) merge the whole warehouse with this batch, then validate
            merged = [
                Record(
                    sample_id=row["sample_id"],
                    local_time=row["local_time"],
                    pass_count=row["pass_count"],
                )
                for row in existing.values()
            ]
            known_keys = {r.key() for r in merged}
            new_records: list[Record] = []
            skipped: list[int] = []
            for rec in records:
                if rec.key() in known_keys:
                    skipped.append(rec.sample_id)
                    continue
                merged.append(rec)
                new_records.append(rec)
                known_keys.add(rec.key())

            if not analyze_timeline(merged).feasible:
                raise _Rollback(UnresolvableTimelineError(
                    "no legal timeline: sample ids must map to strictly "
                    "increasing UTC instants across the DST transition"
                ))

            # 3) commit the new rows
            tx.executemany(
                "INSERT INTO records (sample_id, local_time, pass_count) "
                "VALUES (?, ?, ?)",
                [(r.sample_id, r.local_time, r.pass_count) for r in new_records],
            )
            accepted_result = IngestionResult(
                batch_ref=batch_ref,
                accepted=True,
                inserted=[r.sample_id for r in new_records],
                skipped_duplicates=sorted(set(skipped)),
                record_count=len(records),
            )
    except _Rollback as exc:
        rejection = exc.error

    # The write lock is released here; the audit connection can always write,
    # and rejection entries survive the business transaction rollback.
    if rejection is not None:
        _write_audit(
            audit_conn, batch_ref, "rejected", rejection.reason, len(records)
        )
        raise rejection

    _write_audit(audit_conn, batch_ref, "accepted", None, len(records))
    assert accepted_result is not None
    return accepted_result


def query_traffic(conn: sqlite3.Connection) -> dict:
    """Return determined passenger details plus the still-pending sample ids.

    Everything is computed from one read snapshot in a single transaction, so
    a concurrent backfill can never produce half-new, half-stale verdicts.
    """

    with read_transaction(conn) as tx:
        rows = tx.execute(
            "SELECT sample_id, local_time, pass_count FROM records "
            "ORDER BY sample_id"
        ).fetchall()

    records = [
        Record(
            sample_id=row["sample_id"],
            local_time=row["local_time"],
            pass_count=row["pass_count"],
        )
        for row in rows
    ]
    result = analyze_timeline(records)
    if not result.feasible:  # defensive: ingestion never commits an infeasible set
        return {
            "total_records": len(records),
            "determined_count": 0,
            "pending_count": 0,
            "details": [],
            "pending_sample_ids": [],
            "pending": [],
            "error": "stored records admit no legal timeline; contact data steward",
        }

    details: list[dict] = []
    pending: list[dict] = []
    for rec in records:
        earliest = result.earliest[rec.sample_id]
        latest = result.latest[rec.sample_id]
        if earliest == latest:
            details.append(
                {
                    "sample_id": rec.sample_id,
                    "local_time": rec.local_time,
                    "pass_count": rec.pass_count,
                    "utc_time": earliest.isoformat(),
                }
            )
        else:
            pending.append(
                {
                    "sample_id": rec.sample_id,
                    "local_time": rec.local_time,
                    "pass_count": rec.pass_count,
                    "possible_utc_earliest": earliest.isoformat(),
                    "possible_utc_latest": latest.isoformat(),
                }
            )

    return {
        "total_records": len(records),
        "determined_count": len(details),
        "pending_count": len(pending),
        "details": details,
        "pending_sample_ids": [p["sample_id"] for p in pending],
        "pending": pending,
    }


def list_audit(conn: sqlite3.Connection) -> list[dict]:
    with read_transaction(conn) as tx:
        rows = tx.execute(
            "SELECT batch_ref, status, reason, record_count, created_at "
            "FROM batch_audit ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]
