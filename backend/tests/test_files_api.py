from __future__ import annotations

import os
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
        self.other_tmp = tempfile.TemporaryDirectory()
        self.other_repo = Path(self.other_tmp.name).resolve()
        self.vault = self.repo / "vault"
        (self.repo / "src").mkdir()
        (self.other_repo / "src").mkdir(parents=True)
        self.vault.mkdir()
        (self.repo / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
        (self.other_repo / "src" / "other.py").write_text("print('other')\n", encoding="utf-8")
        (self.vault / "note.md").write_text("# Note\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.other_tmp.cleanup()

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

    def test_workspaces_are_derived_from_orchestrators_and_deduplicated(self) -> None:
        dead_root = self.repo / "dead-workspace"
        dead_root.mkdir()
        missing = self.repo / "missing-workspace"
        registry = {
            "misc": {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(self.other_repo),
                    "run_id": "misc-run",
                    "control_attached": True,
                }
            },
            "a-dead": {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(self.other_repo),
                    "run_id": "dead-run",
                    "control_attached": False,
                }
            },
            "dead": {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(dead_root),
                    "run_id": "dead-run",
                    "control_attached": False,
                }
            },
            "missing": {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(missing),
                    "run_id": "missing-run",
                    "control_attached": True,
                }
            },
            "worker": {
                "current": {
                    "role": "implement",
                    "cwd": str(self.other_repo),
                    "run_id": "worker-run",
                    "control_attached": True,
                }
            },
            "zz-duplicate": {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(self.other_repo / ".." / "other-workspace"),
                    "run_id": "duplicate-run",
                    "control_attached": True,
                }
            },
        }

        with mock.patch.object(main, "FILES_ROOT", self.repo), mock.patch.object(
            main, "_read_agent_registry", return_value=registry
        ), mock.patch.object(main, "_supervisor_pid_is_alive", return_value=True):
            workspaces = main.list_workspaces().workspaces

        self.assertEqual([workspace.id for workspace in workspaces], ["wiki", "misc", "dead"])
        self.assertEqual(workspaces[0].root, str(self.repo))
        self.assertTrue(workspaces[0].live)
        self.assertTrue(workspaces[1].live)
        self.assertFalse(workspaces[2].live)

    def test_live_workspace_wins_deduplication_over_inactive_same_root(self) -> None:
        registry = {
            "a-dead": {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(self.other_repo),
                    "run_id": "dead-run",
                    "control_attached": False,
                }
            },
            "z-live": {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(self.other_repo),
                    "run_id": "live-run",
                    "control_attached": True,
                }
            },
        }

        with mock.patch.object(main, "FILES_ROOT", self.repo), mock.patch.object(
            main, "_read_agent_registry", return_value=registry
        ), mock.patch.object(main, "_supervisor_pid_is_alive", return_value=True):
            workspaces = main.list_workspaces().workspaces

        self.assertEqual([workspace.id for workspace in workspaces], ["wiki", "z-live"])
        self.assertTrue(workspaces[-1].live)

    def test_workspace_file_endpoints_use_the_selected_root_and_default_to_wiki(self) -> None:
        registry = {
            "misc": {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(self.other_repo),
                    "run_id": "misc-run",
                    "control_attached": True,
                }
            }
        }

        with mock.patch.object(main, "FILES_ROOT", self.repo), mock.patch.object(
            main, "_read_agent_registry", return_value=registry
        ), mock.patch.object(main, "_supervisor_pid_is_alive", return_value=True):
            default_tree = main.list_files()
            other_tree = main.list_files("misc")
            other_content = main.get_file_content("src/other.py", "misc")

        self.assertEqual([entry.path for entry in default_tree.files], ["src/app.py", "vault/note.md"])
        self.assertEqual([entry.path for entry in other_tree.files], ["src/other.py"])
        self.assertEqual(other_content.path, "src/other.py")
        self.assertEqual(other_content.content, "print('other')\n")

    def test_unknown_and_inactive_workspaces_are_not_file_roots(self) -> None:
        dead_root = self.repo / "dead-workspace"
        dead_root.mkdir()
        registry = {
            "dead": {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(dead_root),
                    "run_id": "dead-run",
                    "control_attached": False,
                }
            }
        }

        with mock.patch.object(main, "FILES_ROOT", self.repo), mock.patch.object(
            main, "_read_agent_registry", return_value=registry
        ), mock.patch.object(main, "_supervisor_pid_is_alive", return_value=True):
            with self.assertRaises(HTTPException) as inactive:
                main.list_files("dead")
            with self.assertRaises(HTTPException) as unknown:
                main.list_files("missing")
            with self.assertRaises(HTTPException) as separator:
                main.list_files("../wiki")

        self.assertEqual(inactive.exception.status_code, 404)
        self.assertIn("inactive", inactive.exception.detail)
        self.assertEqual(unknown.exception.status_code, 404)
        self.assertEqual(separator.exception.status_code, 404)

    def test_selected_workspace_retains_file_containment_guarantees(self) -> None:
        outside = self.repo / "outside.py"
        outside.write_text("outside", encoding="utf-8")
        (self.other_repo / "escape.py").symlink_to(outside)
        (self.other_repo / ".hidden").write_text("hidden", encoding="utf-8")
        (self.other_repo / "nested").mkdir()
        (self.other_repo / "nested" / "app.py").write_text("nested", encoding="utf-8")
        registry = {
            "misc": {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(self.other_repo),
                    "run_id": "misc-run",
                    "control_attached": True,
                }
            }
        }

        with mock.patch.object(main, "FILES_ROOT", self.repo), mock.patch.object(
            main, "_read_agent_registry", return_value=registry
        ), mock.patch.object(main, "_supervisor_pid_is_alive", return_value=True):
            tree = main.list_files("misc")
            for path in (
                "../outside.py",
                "./src/other.py",
                "nested/../src/other.py",
                "src/other.py\x00",
                "escape.py",
            ):
                with self.subTest(path=path), self.assertRaises(HTTPException) as raised:
                    main.get_file_content(path, "misc")
                self.assertEqual(raised.exception.status_code, 404)

        listed_paths = {entry.path for entry in tree.files}
        self.assertEqual(listed_paths, {"nested/app.py", "src/other.py"})

    def test_invalid_content_requests_do_not_leak_workspace_root_fds(self) -> None:
        registry = {
            "misc": {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(self.other_repo),
                    "run_id": "misc-run",
                    "control_attached": True,
                }
            }
        }

        with mock.patch.object(main, "FILES_ROOT", self.repo), mock.patch.object(
            main, "_read_agent_registry", return_value=registry
        ), mock.patch.object(main, "_supervisor_pid_is_alive", return_value=True):
            before = len(os.listdir("/dev/fd"))
            for _ in range(50):
                with self.assertRaises(HTTPException):
                    main.get_file_content("../outside", "misc")
            after = len(os.listdir("/dev/fd"))

        self.assertLessEqual(after, before + 1)

    def test_content_uses_pinned_root_when_path_is_replaced_after_resolution(self) -> None:
        original_root = self.other_repo
        outside = self.repo / "outside-root"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        (original_root / "secret.txt").write_text("inside", encoding="utf-8")
        registry = {
            "misc": {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(original_root),
                    "run_id": "misc-run",
                    "control_attached": True,
                }
            }
        }

        with mock.patch.object(main, "FILES_ROOT", self.repo), mock.patch.object(
            main, "_read_agent_registry", return_value=registry
        ), mock.patch.object(main, "_supervisor_pid_is_alive", return_value=True):
            resolution = main.resolve_workspace("misc")
            moved_root = original_root.with_name(f"{original_root.name}-moved")
            original_root.rename(moved_root)
            original_root.symlink_to(outside, target_is_directory=True)
            try:
                with mock.patch.object(main, "resolve_workspace", return_value=resolution):
                    result = main.get_file_content("secret.txt", "misc")
            finally:
                original_root.unlink()
                moved_root.rename(original_root)

        self.assertEqual(result.content, "inside")

    def test_tree_uses_pinned_root_when_path_is_replaced_after_resolution(self) -> None:
        original_root = self.other_repo
        outside = self.repo / "outside-tree"
        outside.mkdir()
        for index in range(25):
            (outside / f"outside-{index}.txt").write_text("outside", encoding="utf-8")
        (original_root / "inside.txt").write_text("inside", encoding="utf-8")
        registry = {
            "misc": {
                "current": {
                    "role": "orchestrator",
                    "cwd": str(original_root),
                    "run_id": "misc-run",
                    "control_attached": True,
                }
            }
        }

        with mock.patch.object(main, "FILES_ROOT", self.repo), mock.patch.object(
            main, "_read_agent_registry", return_value=registry
        ), mock.patch.object(main, "_supervisor_pid_is_alive", return_value=True):
            resolution = main.resolve_workspace("misc")
            moved_root = original_root.with_name(f"{original_root.name}-moved")
            original_root.rename(moved_root)
            original_root.symlink_to(outside, target_is_directory=True)
            try:
                with mock.patch.object(main, "resolve_workspace", return_value=resolution), mock.patch.object(
                    main, "MAX_FILE_TREE_ENTRIES", 1
                ):
                    tree = main.list_files("misc")
            finally:
                original_root.unlink()
                moved_root.rename(original_root)

        self.assertEqual([entry.path for entry in tree.files], ["inside.txt"])
        self.assertTrue(tree.truncated)

    def test_tree_prunes_excluded_directories_before_descending(self) -> None:
        (self.repo / ".git").mkdir()
        (self.repo / ".git" / "config").write_text("secret", encoding="utf-8")
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules" / "package.js").write_text("ignored", encoding="utf-8")
        real_scandir = main.os.scandir
        scanned: list[Path] = []

        def tracking_scandir(path: int | str | bytes | Path):
            original_path = path
            if isinstance(path, int):
                path = os.readlink(f"/dev/fd/{path}")
            scanned.append(Path(path))
            return real_scandir(original_path)

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
