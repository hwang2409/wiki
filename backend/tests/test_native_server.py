from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from backend import native_server
from backend.app import nofile_limit


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
            nofile_limit.resource, "getrlimit", return_value=(256, 10240)
        ), patch.object(nofile_limit.resource, "setrlimit") as setrlimit:
            with self.assertLogs(nofile_limit.logger, level="INFO") as logs:
                result = native_server.raise_nofile_limit()

        self.assertEqual(result, (10240, 10240))
        setrlimit.assert_called_once_with(
            nofile_limit.resource.RLIMIT_NOFILE, (10240, 10240)
        )
        self.assertIn(
            "rlimit_nofile before_soft=256 before_hard=10240 "
            "after_soft=10240 after_hard=10240",
            logs.output[0],
        )

    def test_raise_nofile_limit_darwin_falls_back_to_10240(self) -> None:
        with patch.object(
            nofile_limit.sys, "platform", "darwin"
        ), patch.object(
            nofile_limit.resource,
            "getrlimit",
            return_value=(256, nofile_limit.resource.RLIM_INFINITY),
        ), patch.object(
            nofile_limit.resource,
            "setrlimit",
            side_effect=[OSError("kern.maxfilesperproc"), None],
        ) as setrlimit:
            result = native_server.raise_nofile_limit()

        self.assertEqual(
            result, (nofile_limit.DARWIN_FALLBACK_LIMIT, nofile_limit.resource.RLIM_INFINITY)
        )
        self.assertEqual(setrlimit.call_count, 2)
        setrlimit.assert_any_call(
            nofile_limit.resource.RLIMIT_NOFILE,
            (nofile_limit.DARWIN_FALLBACK_LIMIT, nofile_limit.resource.RLIM_INFINITY),
        )

    def test_raise_nofile_limit_does_nothing_when_already_at_hard_limit(self) -> None:
        with patch.object(
            nofile_limit.resource, "getrlimit", return_value=(10240, 10240)
        ), patch.object(nofile_limit.resource, "setrlimit") as setrlimit:
            result = native_server.raise_nofile_limit()

        self.assertEqual(result, (10240, 10240))
        setrlimit.assert_not_called()

    def test_raise_nofile_limit_logs_warning_and_survives_set_failure(self) -> None:
        with patch.object(
            nofile_limit.resource, "getrlimit", return_value=(256, 10240)
        ), patch.object(
            nofile_limit.resource,
            "setrlimit",
            side_effect=OSError("permission denied"),
        ), self.assertLogs(nofile_limit.logger, level="WARNING") as logs:
            result = native_server.raise_nofile_limit()

        self.assertEqual(result, (256, 10240))
        self.assertIn("rlimit_nofile_raise_failed", logs.output[0])


if __name__ == "__main__":
    unittest.main()
