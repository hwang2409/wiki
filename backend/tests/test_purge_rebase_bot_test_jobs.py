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
            delivery_id = "175:retry-test-sha:escalated:retry-test-head"
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
                            }
                        },
                        "outbox": {},
                        "delivered": [delivery_id],
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
            self.assertIn("purged 1 retry-test job(s)", first.stdout)
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


if __name__ == "__main__":
    unittest.main()
