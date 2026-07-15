from __future__ import annotations

import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
