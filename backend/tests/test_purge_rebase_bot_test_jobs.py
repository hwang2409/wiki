from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "purge_rebase_bot_test_jobs.py"


class PurgeRebaseBotTestJobsTests(unittest.TestCase):
    def test_purge_removes_job_and_delivery_id_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime_dir = Path(raw)
            state_path = runtime_dir / "rebase-bot" / "state.json"
            state_path.parent.mkdir(parents=True)
            first_delivery_id = "175:retry-test-sha:escalated:retry-test-head"
            second_delivery_id = "176:retry-test-other:resolved:retry-test-other-head"
            state_path.write_text(
                json.dumps(
                    {
                        "jobs": {
                            "175:retry-test-sha": {
                                "pr_number": 175,
                                "expected_sha": "retry-test-sha",
                                "status": "completed",
                                "result": {
                                    "status": "escalated",
                                    "head_sha": "retry-test-head",
                                },
                            },
                            "176:retry-test-other": {
                                "pr_number": 176,
                                "expected_sha": "retry-test-other",
                                "status": "completed",
                                "result": {
                                    "status": "resolved",
                                    "head_sha": "retry-test-other-head",
                                },
                            },
                        },
                        "outbox": {},
                        "delivered": [first_delivery_id, second_delivery_id],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            first = subprocess.run(
                [sys.executable, str(SCRIPT), "--runtime-dir", str(runtime_dir)],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            self.assertIn("purged 2 retry-test job(s)", first.stdout)
            cleaned = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(cleaned, {"jobs": {}, "outbox": {}, "delivered": []})

            second = subprocess.run(
                [sys.executable, str(SCRIPT), "--runtime-dir", str(runtime_dir)],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            self.assertIn("purged 0 retry-test job(s)", second.stdout)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                {"jobs": {}, "outbox": {}, "delivered": []},
            )

    def test_purge_removes_orphaned_retry_delivery_ids_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime_dir = Path(raw)
            state_path = runtime_dir / "rebase-bot" / "state.json"
            state_path.parent.mkdir(parents=True)
            production_delivery_id = "177:production-sha:escalated:production-head"
            orphaned_retry_delivery_id = "178:retry-test-pruned:escalated:retry-test-head"
            state_path.write_text(
                json.dumps(
                    {
                        "jobs": {},
                        "outbox": {},
                        "delivered": [
                            production_delivery_id,
                            orphaned_retry_delivery_id,
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--runtime-dir", str(runtime_dir)],
                capture_output=True,
                text=True,
                check=True,
                env=os.environ.copy(),
            )

            self.assertIn("purged 0 retry-test job(s)", result.stdout)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                {
                    "jobs": {},
                    "outbox": {},
                    "delivered": [production_delivery_id],
                },
            )


if __name__ == "__main__":
    unittest.main()
