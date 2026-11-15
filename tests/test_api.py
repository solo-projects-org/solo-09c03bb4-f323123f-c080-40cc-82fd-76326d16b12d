"""HTTP-level tests via FastAPI's TestClient.

These require the web stack (fastapi, python-multipart, httpx). They run in
the container image / CI where requirements.txt is installed; in a stdlib-only
environment the whole module is skipped so the core suite stays runnable.
"""

import tempfile
import unittest
from pathlib import Path

import app.db as db_module


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
        except Exception as exc:  # pragma: no cover - environment dependent
            raise unittest.SkipTest(f"web stack unavailable: {exc}")

        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_file = Path(cls.tmp.name) / "api.db"
        cls._orig = db_module.DB_PATH
        db_module.DB_PATH = cls.db_file

        from app.main import app

        # lifespan opens the connections against the patched path
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "client"):
            cls.client.__exit__(None, None, None)
            db_module.DB_PATH = cls._orig
            cls.tmp.cleanup()

    def _upload(self, name: str, content: bytes):
        return self.client.post(
            "/upload",
            files={"file": (name, content, "text/csv")},
        )

    def test_dst_flow_over_http(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)

        initial = (
            "sample_id,local_time,pass_count\n"
            "100,2024-11-03T00:45:00,120\n"
            "128,2024-11-03T01:20:00,340\n"
            "148,2024-11-03T01:55:00,300\n"
            "160,2024-11-03T02:15:00,260\n"
            "180,2024-11-03T03:10:00,210\n"
        ).encode()
        r = self._upload("initial.csv", initial)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(r.json()["inserted_sample_ids"]),
                         [100, 128, 148, 160, 180])

        r = self.client.get("/traffic").json()
        self.assertEqual(r["pending_sample_ids"], [128, 148])
        self.assertEqual(r["determined_count"], 3)

        # idempotent re-upload
        r = self._upload("initial.csv", initial)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["inserted_sample_ids"], [])

        # conflict -> 409
        r = self._upload(
            "conflict.csv",
            b"sample_id,local_time,pass_count\n128,2024-11-03T01:20:00,999\n",
        )
        self.assertEqual(r.status_code, 409)
        self.assertFalse(r.json()["accepted"])

        # illegal timeline -> 422
        r = self._upload(
            "illegal.csv",
            b"sample_id,local_time,pass_count\n"
            b"127,2024-11-03T01:20:00,80\n129,2024-11-03T01:20:00,84\n",
        )
        self.assertEqual(r.status_code, 422)

        # disambiguating backfill
        r = self._upload(
            "backfill.csv",
            b"sample_id,local_time,pass_count\n127,2024-11-03T01:20:00,84\n",
        )
        self.assertEqual(r.status_code, 200)

        r = self.client.get("/traffic").json()
        self.assertEqual(r["pending_sample_ids"], [])
        self.assertEqual(r["determined_count"], 6)
        by_id = {d["sample_id"]: d["utc_time"] for d in r["details"]}
        self.assertEqual(by_id[127], "2024-11-03T05:20:00+00:00")
        self.assertEqual(by_id[128], "2024-11-03T06:20:00+00:00")
        self.assertEqual(by_id[148], "2024-11-03T06:55:00+00:00")

        audits = self.client.get("/batches").json()["batches"]
        self.assertEqual(
            [a["status"] for a in audits],
            ["accepted", "accepted", "rejected", "rejected", "accepted"],
        )

    def test_malformed_csv_returns_400(self):
        r = self._upload("bad.csv", b"id,local_time,pass_count\n1,x,2\n")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
