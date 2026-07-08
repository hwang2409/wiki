from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend.app import vaultops


class RenameNoteTests(unittest.TestCase):
    def test_rewrites_bare_and_path_wikilinks(self) -> None:
        with TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "meta").mkdir()
            (vault / "meta" / "target.md").write_text("# Target\n", encoding="utf-8")
            (vault / "meta" / "links.md").write_text(
                "[[target]]\n[[meta/target]]\n[[target|alias]]\n",
                encoding="utf-8",
            )

            changed = vaultops.rename_note(vault, "meta/target.md", "meta/renamed.md")

            self.assertIn("meta/renamed.md", changed)
            self.assertEqual(
                (vault / "meta" / "links.md").read_text(encoding="utf-8"),
                "[[renamed]]\n[[meta/renamed]]\n[[renamed|alias]]\n",
            )

    def test_rewrites_path_links_when_moving_same_slug(self) -> None:
        with TemporaryDirectory() as tmp:
            vault = Path(tmp).resolve()
            (vault / "meta").mkdir()
            (vault / "tools").mkdir()
            (vault / "meta" / "target.md").write_text("# Target\n", encoding="utf-8")
            (vault / "meta" / "links.md").write_text(
                "[[target]]\n[[meta/target]]\n",
                encoding="utf-8",
            )

            changed = vaultops.rename_note(vault, "meta/target.md", "tools/target.md")

            self.assertIn("tools/target.md", changed)
            self.assertEqual(
                (vault / "meta" / "links.md").read_text(encoding="utf-8"),
                "[[target]]\n[[tools/target]]\n",
            )


if __name__ == "__main__":
    unittest.main()
