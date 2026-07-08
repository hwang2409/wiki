from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend.app import vaultops


class RenameNoteTests(unittest.TestCase):
    def test_rewrites_bare_path_alias_and_heading_wikilinks(self) -> None:
        with TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "meta").mkdir()
            (vault / "meta" / "target.md").write_text("# Target\n", encoding="utf-8")
            (vault / "meta" / "links.md").write_text(
                "[[target]]\n"
                "[[meta/target]]\n"
                "[[meta/target|alias]]\n"
                "[[target#Heading]]\n"
                "[[meta/target#^block|alias]]\n",
                encoding="utf-8",
            )

            changed = vaultops.rename_note(vault, "meta/target.md", "meta/renamed.md")

            self.assertIn("meta/renamed.md", changed)
            self.assertEqual(
                (vault / "meta" / "links.md").read_text(encoding="utf-8"),
                "[[renamed]]\n"
                "[[meta/renamed]]\n"
                "[[meta/renamed|alias]]\n"
                "[[renamed#Heading]]\n"
                "[[meta/renamed#^block|alias]]\n",
            )

    def test_rewrites_path_links_when_moving_same_slug(self) -> None:
        with TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "meta").mkdir()
            (vault / "tools").mkdir()
            (vault / "meta" / "target.md").write_text("# Target\n", encoding="utf-8")
            (vault / "meta" / "links.md").write_text(
                "[[target]]\n[[target#Heading]]\n[[meta/target#^block|alias]]\n",
                encoding="utf-8",
            )

            changed = vaultops.rename_note(vault, "meta/target.md", "tools/target.md")

            self.assertIn("tools/target.md", changed)
            self.assertEqual(
                (vault / "meta" / "links.md").read_text(encoding="utf-8"),
                "[[target]]\n[[target#Heading]]\n[[tools/target#^block|alias]]\n",
            )

    def test_does_not_rewrite_prefix_matches_or_code_fences(self) -> None:
        with TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "meta").mkdir()
            (vault / "meta" / "wiki.md").write_text("# Wiki\n", encoding="utf-8")
            (vault / "meta" / "wiki-2.md").write_text("# Wiki 2\n", encoding="utf-8")
            (vault / "meta" / "links.md").write_text(
                "[[wiki]]\n"
                "[[wiki-2]]\n"
                "```md\n[[wiki]]\n[[meta/wiki#Heading]]\n```\n"
                "`[[wiki]]`\n",
                encoding="utf-8",
            )

            vaultops.rename_note(vault, "meta/wiki.md", "meta/renamed.md")

            self.assertEqual(
                (vault / "meta" / "links.md").read_text(encoding="utf-8"),
                "[[renamed]]\n"
                "[[wiki-2]]\n"
                "```md\n[[wiki]]\n[[meta/wiki#Heading]]\n```\n"
                "`[[wiki]]`\n",
            )


if __name__ == "__main__":
    unittest.main()
