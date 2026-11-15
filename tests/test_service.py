"""SQLite-backed service tests: atomic batches, conflicts, idempotency."""

import tempfile
import unittest
from pathlib import Path

from app.db import connect, init_db
from app.service import (
    ConflictError,
    IngestionError,
    UnresolvableTimelineError,
    ingest_batch,
    query_traffic,
)
from app.timeline import Record
from app.csv_utils import CsvValidationError, parse_counter_csv


def r(sid: int, local: str, count: int) -> Record:
    return Record(sid, local, count)


INITIAL = [
    r(100, "2024-11-03T00:45:00", 120),
    r(128, "2024-11-03T01:20:00", 340),
    r(148, "2024-11-03T01:55:00", 300),
    r(160, "2024-11-03T02:15:00", 260),
    r(180, "2024-11-03T03:10:00", 210),
]


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = connect(self.db_path)
        self.audit_conn = connect(self.db_path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.audit_conn.close()
        self.tmp.cleanup()

    def _ingest(self, records):
        return ingest_batch(
            self.conn, records, audit_conn=self.audit_conn
        )

    def test_full_backfill_scenario(self):
        self._ingest(INITIAL)
        view = query_traffic(self.conn)
        self.assertEqual(view["total_records"], 5)
        self.assertEqual(view["determined_count"], 3)
        self.assertEqual(view["pending_sample_ids"], [128, 148])
        self.assertEqual(
            [d["sample_id"] for d in view["details"]], [100, 160, 180]
        )
        # pending payload exposes the two possible UTC instants
        pending_by_id = {p["sample_id"]: p for p in view["pending"]}
        self.assertEqual(
            pending_by_id[128]["possible_utc_earliest"],
            "2024-11-03T05:20:00+00:00",
        )
        self.assertEqual(
            pending_by_id[128]["possible_utc_latest"],
            "2024-11-03T06:20:00+00:00",
        )

        # one same-wall backfill row disambiguates the whole repeated hour
        self._ingest([r(127, "2024-11-03T01:20:00", 84)])
        view = query_traffic(self.conn)
        self.assertEqual(view["pending_sample_ids"], [])
        self.assertEqual(view["determined_count"], 6)
        by_id = {d["sample_id"]: d for d in view["details"]}
        self.assertEqual(by_id[127]["utc_time"], "2024-11-03T05:20:00+00:00")
        self.assertEqual(by_id[128]["utc_time"], "2024-11-03T06:20:00+00:00")
        self.assertEqual(by_id[148]["utc_time"], "2024-11-03T06:55:00+00:00")

    def test_same_id_same_content_is_idempotent(self):
        batch = [r(128, "2024-11-03T01:20:00", 340)]
        first = self._ingest(batch)
        self.assertEqual(first.inserted, [128])
        second = self._ingest(batch)
        self.assertEqual(second.inserted, [])
        self.assertEqual(second.skipped_duplicates, [128])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 1
        )

    def test_same_id_different_value_rejects_whole_batch(self):
        self._ingest([r(128, "2024-11-03T01:20:00", 340)])
        with self.assertRaises(ConflictError):
            self._ingest([
                r(128, "2024-11-03T01:20:00", 999),
                r(199, "2024-11-03T04:00:00", 10),
            ])
        # neither row of the rejected batch landed
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 1
        )
        # the rejection itself is audited on the independent connection
        audit = self.conn.execute(
            "SELECT status FROM batch_audit ORDER BY id"
        ).fetchall()
        self.assertEqual([row[0] for row in audit], ["accepted", "rejected"])

    def test_illegal_timeline_rejects_whole_batch(self):
        self._ingest([r(100, "2024-11-03T00:45:00", 120)])
        with self.assertRaises(UnresolvableTimelineError):
            self._ingest([
                r(5, "2024-11-03T03:00:00", 50),
                r(15, "2024-11-03T00:30:00", 60),
            ])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 1
        )

    def test_backfill_infeasible_against_stored_data_is_rolled_back(self):
        self._ingest(INITIAL)
        # Stored 128 plus two more readings at the same ambiguous wall time
        # gives three samples with only two UTC slots -> impossible ordering.
        with self.assertRaises(UnresolvableTimelineError):
            self._ingest([
                r(127, "2024-11-03T01:20:00", 80),
                r(129, "2024-11-03T01:20:00", 84),
            ])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0], 5
        )
        audit = self.conn.execute(
            "SELECT status FROM batch_audit ORDER BY id"
        ).fetchall()
        self.assertEqual([row[0] for row in audit], ["accepted", "rejected"])

    def test_empty_batch_rejected(self):
        with self.assertRaises(IngestionError):
            self._ingest([])

    def test_csv_parsing_rules(self):
        good = (
            "sample_id,local_time,pass_count\n"
            "128,2024-11-03T01:20:00,340\n"
            "148,2024-11-03T01:55:00,300\n"
        ).encode()
        records = parse_counter_csv(good)
        self.assertEqual(len(records), 2)

        with self.assertRaises(CsvValidationError):
            parse_counter_csv(b"id,local_time,pass_count\n1,x,2\n")
        with self.assertRaises(CsvValidationError):
            parse_counter_csv(
                b"sample_id,local_time,pass_count\n"
                b"1,2024-11-03T01:20:00-04:00,2\n"
            )
        with self.assertRaises(CsvValidationError):
            parse_counter_csv(
                b"sample_id,local_time,pass_count\n"
                b"1,2024-11-03T01:20:00,2\n1,2024-11-03T01:20:00,2\n"
            )
        with self.assertRaises(CsvValidationError):
            parse_counter_csv(b"sample_id,local_time,pass_count\n")
        with self.assertRaises(CsvValidationError):
            parse_counter_csv(
                b"sample_id,local_time,pass_count\n0,2024-11-03T01:20:00,2\n"
            )
        with self.assertRaises(CsvValidationError):
            parse_counter_csv(
                b"sample_id,local_time,pass_count\n1,2024-11-03T01:20:00,-2\n"
            )


if __name__ == "__main__":
    unittest.main()
