from __future__ import annotations

import sys
import unittest
from io import StringIO
from unittest.mock import patch

from backend import native_server


class NativeServerDispatchTests(unittest.TestCase):
    def test_terminal_child_dispatches_before_backend_argument_parsing(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["wiki-backend", "--terminal-child", "/bin/sh", "/tmp"],
        ), patch("backend.app.terminal_child.main") as terminal_child_main:
            native_server.main()
            terminal_child_main.assert_called_once_with()
            self.assertEqual(sys.argv, ["wiki-backend", "/bin/sh", "/tmp"])


class NofileLimitTests(unittest.TestCase):
    def test_raise_nofile_limit_raises_soft_limit_and_logs_result(self) -> None:
        with patch.object(
            native_server.resource,
            "getrlimit",
            side_effect=[(256, 10240), (10240, 10240)],
        ) as getrlimit, patch.object(
            native_server.resource, "setrlimit"
        ) as setrlimit, patch.object(
            native_server.sys, "stderr", new_callable=StringIO
        ) as stderr:
            result = native_server.raise_nofile_limit()

        self.assertEqual(result, (10240, 10240))
        setrlimit.assert_called_once_with(
            native_server.resource.RLIMIT_NOFILE, (10240, 10240)
        )
        self.assertEqual(getrlimit.call_count, 2)
        self.assertIn("RLIMIT_NOFILE soft=10240 hard=10240", stderr.getvalue())

    def test_raise_nofile_limit_logs_and_survives_set_failure(self) -> None:
        with patch.object(
            native_server.resource, "getrlimit", return_value=(256, 10240)
        ), patch.object(
            native_server.resource,
            "setrlimit",
            side_effect=OSError("permission denied"),
        ), patch.object(
            native_server.sys, "stderr", new_callable=StringIO
        ) as stderr:
            result = native_server.raise_nofile_limit()

        self.assertEqual(result, (256, 10240))
        self.assertIn("unable to raise RLIMIT_NOFILE", stderr.getvalue())
        self.assertIn("RLIMIT_NOFILE soft=256 hard=10240", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
