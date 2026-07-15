from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from backend.app import main


class FilesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name).resolve()
        (self.vault / "src").mkdir()
        (self.vault / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
        (self.vault / "note.md").write_text("# Note\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_tree_lists_vault_files_and_excludes_hidden_directories(self) -> None:
        (self.vault / ".git").mkdir()
        (self.vault / ".git" / "config").write_text("secret", encoding="utf-8")
        (self.vault / ".obsidian").mkdir()
        (self.vault / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        (self.vault / "node_modules").mkdir()
        (self.vault / "node_modules" / "package.js").write_text("ignored", encoding="utf-8")
        (self.vault / ".secret").write_text("ignored", encoding="utf-8")

        with mock.patch.object(main, "VAULT_DIR", self.vault):
            paths = [entry.path for entry in main.list_files()]

        self.assertEqual(paths, ["note.md", "src/app.py"])

    def test_content_is_utf8_text(self) -> None:
        with mock.patch.object(main, "VAULT_DIR", self.vault):
            result = main.get_file_content("src/app.py")

        self.assertEqual(result.path, "src/app.py")
        self.assertEqual(result.content, "print('hello')\n")
        self.assertFalse(result.binary)

    def test_traversal_absolute_and_symlink_escape_are_not_found(self) -> None:
        outside = self.vault.parent / "outside.py"
        outside.write_text("outside", encoding="utf-8")
        (self.vault / "escape.py").symlink_to(outside)

        with mock.patch.object(main, "VAULT_DIR", self.vault):
            for path in ("../outside.py", str(outside), "escape.py"):
                with self.subTest(path=path), self.assertRaises(HTTPException) as raised:
                    main.get_file_content(path)
                self.assertEqual(raised.exception.status_code, 404)

    def test_size_cap_returns_structured_error(self) -> None:
        (self.vault / "large.txt").write_bytes(b"x" * (main.MAX_FILE_BYTES + 1))

        with mock.patch.object(main, "VAULT_DIR", self.vault):
            with self.assertRaises(HTTPException) as raised:
                main.get_file_content("large.txt")

        self.assertEqual(raised.exception.status_code, 413)
        self.assertEqual(raised.exception.detail["code"], "file_too_large")

    def test_binary_and_invalid_utf8_return_structured_binary_response(self) -> None:
        (self.vault / "image.bin").write_bytes(b"PNG\x00bytes")
        (self.vault / "invalid.bin").write_bytes(b"\xff\xfe")

        with mock.patch.object(main, "VAULT_DIR", self.vault):
            binary = main.get_file_content("image.bin")
            invalid = main.get_file_content("invalid.bin")

        self.assertTrue(binary.binary)
        self.assertIsNone(binary.content)
        self.assertEqual(binary.error, "binary file")
        self.assertTrue(invalid.binary)
        self.assertIsNone(invalid.content)
        self.assertEqual(invalid.error, "binary file")


if __name__ == "__main__":
    unittest.main()
