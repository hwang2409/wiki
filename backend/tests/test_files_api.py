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
        self.repo = Path(self.tmp.name).resolve()
        self.vault = self.repo / "vault"
        (self.repo / "src").mkdir()
        self.vault.mkdir()
        (self.repo / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
        (self.vault / "note.md").write_text("# Note\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_tree_lists_repo_files_and_excludes_hidden_directories(self) -> None:
        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "config").write_text("secret", encoding="utf-8")
        (self.repo / ".obsidian").mkdir()
        (self.repo / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        for ignored in ("node_modules", "target", "dist", ".codex", "__pycache__", ".venv"):
            (self.repo / ignored).mkdir()
            (self.repo / ignored / "ignored.txt").write_text("ignored", encoding="utf-8")
        (self.repo / ".secret").write_text("ignored", encoding="utf-8")
        (self.repo / "secret-alias").symlink_to(self.repo / ".secret")
        (self.repo / "git-alias").symlink_to(self.repo / ".git" / "config")

        with mock.patch.object(main, "FILES_ROOT", self.repo):
            tree = main.list_files()

        self.assertEqual([entry.path for entry in tree.files], ["src/app.py", "vault/note.md"])
        self.assertFalse(tree.truncated)

    def test_tree_prunes_excluded_directories_before_descending(self) -> None:
        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "config").write_text("secret", encoding="utf-8")
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules" / "package.js").write_text("ignored", encoding="utf-8")
        real_scandir = main.os.scandir
        scanned: list[Path] = []

        def tracking_scandir(path: str | bytes | Path):
            scanned.append(Path(path))
            return real_scandir(path)

        with mock.patch.object(main, "FILES_ROOT", self.repo), mock.patch.object(
            main.os, "scandir", tracking_scandir
        ):
            main.list_files()

        self.assertNotIn(self.repo / ".git", scanned)
        self.assertNotIn(self.repo / "node_modules", scanned)

    def test_content_is_utf8_text(self) -> None:
        with mock.patch.object(main, "FILES_ROOT", self.repo):
            result = main.get_file_content("src/app.py")

        self.assertEqual(result.path, "src/app.py")
        self.assertEqual(result.content, "print('hello')\n")
        self.assertFalse(result.binary)

    def test_traversal_absolute_and_symlink_escape_are_not_found(self) -> None:
        outside = self.repo.parent / "outside.py"
        outside.write_text("outside", encoding="utf-8")
        (self.repo / "escape.py").symlink_to(outside)

        with mock.patch.object(main, "FILES_ROOT", self.repo):
            for path in (
                "",
                "../outside.py",
                "./src/app.py",
                "src/./app.py",
                "src//app.py",
                "src/../src/app.py",
                str(outside),
                "escape.py",
            ):
                with self.subTest(path=path), self.assertRaises(HTTPException) as raised:
                    main.get_file_content(path)
                self.assertEqual(raised.exception.status_code, 404)

    def test_symlink_aliases_to_hidden_files_are_not_listed_or_served(self) -> None:
        (self.repo / ".secret").write_text("secret", encoding="utf-8")
        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "config").write_text("git secret", encoding="utf-8")
        (self.repo / "secret-alias").symlink_to(self.repo / ".secret")
        (self.repo / "git-alias").symlink_to(self.repo / ".git" / "config")

        with mock.patch.object(main, "FILES_ROOT", self.repo):
            tree = main.list_files()
            for path in ("secret-alias", "git-alias"):
                with self.subTest(path=path), self.assertRaises(HTTPException) as raised:
                    main.get_file_content(path)
                self.assertEqual(raised.exception.status_code, 404)

        listed_paths = {entry.path for entry in tree.files}
        self.assertNotIn("secret-alias", listed_paths)
        self.assertNotIn("git-alias", listed_paths)

    def test_nul_in_path_is_not_found(self) -> None:
        with mock.patch.object(main, "FILES_ROOT", self.repo):
            with self.assertRaises(HTTPException) as raised:
                main.get_file_content("src/app.py\x00")

        self.assertEqual(raised.exception.status_code, 404)

    def test_size_cap_returns_structured_error(self) -> None:
        (self.repo / "at-limit.txt").write_bytes(b"x" * main.MAX_FILE_BYTES)
        (self.repo / "large.txt").write_bytes(b"x" * (main.MAX_FILE_BYTES + 1))

        with mock.patch.object(main, "FILES_ROOT", self.repo):
            at_limit = main.get_file_content("at-limit.txt")
            with self.assertRaises(HTTPException) as raised:
                main.get_file_content("large.txt")

        self.assertEqual(len(at_limit.content or ""), main.MAX_FILE_BYTES)
        self.assertEqual(raised.exception.status_code, 413)
        self.assertEqual(raised.exception.detail["code"], "file_too_large")

    def test_tree_reports_truncation_at_the_result_limit(self) -> None:
        for index in range(3):
            (self.vault / f"file-{index}.txt").write_text("x", encoding="utf-8")

        with mock.patch.object(main, "FILES_ROOT", self.repo), mock.patch.object(
            main, "MAX_FILE_TREE_ENTRIES", 2
        ):
            tree = main.list_files()

        self.assertEqual(len(tree.files), 2)
        self.assertTrue(tree.truncated)

    def test_tree_at_exact_result_limit_is_not_truncated(self) -> None:
        (self.vault / "file-0.txt").write_text("x", encoding="utf-8")
        (self.vault / "file-1.txt").write_text("x", encoding="utf-8")

        with mock.patch.object(main, "FILES_ROOT", self.repo), mock.patch.object(
            main, "MAX_FILE_TREE_ENTRIES", 4
        ):
            tree = main.list_files()

        self.assertEqual(len(tree.files), 4)
        self.assertFalse(tree.truncated)

    def test_descriptor_containment_walks_from_the_repo_root(self) -> None:
        target = self.repo / "src" / "app.py"
        outside = self.repo.parent / "outside.py"
        outside.write_text("outside", encoding="utf-8")

        with target.open("rb") as opened:
            self.assertTrue(main.opened_file_is_safe(opened.fileno(), target, self.repo))
            self.assertFalse(main.opened_file_is_safe(opened.fileno(), outside, self.repo))

    def test_binary_and_invalid_utf8_return_structured_binary_response(self) -> None:
        (self.repo / "image.bin").write_bytes(b"PNG\x00bytes")
        (self.repo / "invalid.bin").write_bytes(b"\xff\xfe")

        with mock.patch.object(main, "FILES_ROOT", self.repo):
            binary = main.get_file_content("image.bin")
            invalid = main.get_file_content("invalid.bin")

        self.assertTrue(binary.binary)
        self.assertIsNone(binary.content)
        self.assertEqual(binary.error, "binary file")
        self.assertTrue(invalid.binary)
        self.assertIsNone(invalid.content)
        self.assertEqual(invalid.error, "binary file")

    def test_vault_asset_content_type_and_svg_security_headers(self) -> None:
        image = self.vault / "images" / "diagram.SVG"
        image.parent.mkdir()
        image.write_text("<svg><style>svg { color: red }</style></svg>", encoding="utf-8")

        with mock.patch.object(main, "VAULT_DIR", self.vault):
            response = main.get_vault_asset("images/diagram.SVG")

        self.assertEqual(response.media_type, "image/svg+xml")
        self.assertEqual(response.body, image.read_bytes())
        self.assertEqual(
            response.headers["content-security-policy"],
            "default-src 'none'; style-src 'unsafe-inline'",
        )
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_vault_asset_maps_raster_content_types(self) -> None:
        image_types = (
            ("png", "image/png"),
            ("jpg", "image/jpeg"),
            ("jpeg", "image/jpeg"),
            ("gif", "image/gif"),
            ("webp", "image/webp"),
        )
        for suffix, media_type in image_types:
            with self.subTest(suffix=suffix):
                target = self.vault / f"image.{suffix}"
                target.write_bytes(b"image")
                with mock.patch.object(main, "VAULT_DIR", self.vault):
                    response = main.get_vault_asset(target.name)
                self.assertEqual(response.media_type, media_type)
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_vault_asset_rejects_traversal_absolute_dot_segments_and_symlink_escape(self) -> None:
        image = self.vault / "image.png"
        image.write_bytes(b"image")
        outside = self.repo.parent / "outside.png"
        outside.write_bytes(b"outside")
        (self.vault / "escape.png").symlink_to(outside)

        with mock.patch.object(main, "VAULT_DIR", self.vault):
            for path in (
                "../outside.png",
                "image.png\x00",
                "./image.png",
                "nested/../image.png",
                "nested//image.png",
                str(outside),
                "escape.png",
                "image.txt",
            ):
                with self.subTest(path=path), self.assertRaises(HTTPException) as raised:
                    main.get_vault_asset(path)
                self.assertEqual(raised.exception.status_code, 404)

    def test_vault_asset_missing_and_oversized_files(self) -> None:
        (self.vault / "large.webp").write_bytes(b"x" * (main.MAX_FILE_BYTES + 1))

        with mock.patch.object(main, "VAULT_DIR", self.vault):
            with self.assertRaises(HTTPException) as missing:
                main.get_vault_asset("missing.png")
            with self.assertRaises(HTTPException) as oversized:
                main.get_vault_asset("large.webp")

        self.assertEqual(missing.exception.status_code, 404)
        self.assertEqual(oversized.exception.status_code, 413)
        self.assertEqual(oversized.exception.detail["code"], "file_too_large")

    def test_vault_asset_route_binds_descriptor_check_to_vault_root(self) -> None:
        image = self.vault / "bound.png"
        image.write_bytes(b"image")

        with mock.patch.object(main, "VAULT_DIR", self.vault), mock.patch.object(
            main, "opened_file_is_safe", wraps=main.opened_file_is_safe
        ) as opened_file_is_safe:
            response = main.get_vault_asset("bound.png")

        self.assertEqual(response.body, b"image")
        opened_file_is_safe.assert_called_once()
        self.assertIsInstance(opened_file_is_safe.call_args.args[0], int)
        self.assertEqual(opened_file_is_safe.call_args.args[2], self.vault.resolve())


if __name__ == "__main__":
    unittest.main()
