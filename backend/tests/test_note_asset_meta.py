from __future__ import annotations

import io
import unittest

from PIL import Image

from backend.app.main import _extract_note_image_paths


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color=(255, 128, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


class ExtractNoteImagePathsTests(unittest.TestCase):
    def test_bare_relative_paths(self) -> None:
        content = "![hero](hero.png)\n"
        self.assertEqual(_extract_note_image_paths(content, "index.md"), ["hero.png"])

    def test_link_with_title_is_extracted(self) -> None:
        content = '![alt](hero.png "with title")\n'
        self.assertEqual(_extract_note_image_paths(content, "index.md"), ["hero.png"])

    def test_link_with_single_quoted_title(self) -> None:
        content = "![alt](hero.png 'with title')\n"
        self.assertEqual(_extract_note_image_paths(content, "index.md"), ["hero.png"])

    def test_link_with_paren_title(self) -> None:
        content = "![alt](hero.png (with title))\n"
        self.assertEqual(_extract_note_image_paths(content, "index.md"), ["hero.png"])

    def test_angle_bracket_path_with_spaces(self) -> None:
        content = "![alt](<my hero.png>)\n"
        self.assertEqual(
            _extract_note_image_paths(content, "index.md"),
            ["my hero.png"],
        )

    def test_percent_encoded_spaces(self) -> None:
        content = "![alt](my%20hero.png)\n"
        self.assertEqual(
            _extract_note_image_paths(content, "index.md"),
            ["my hero.png"],
        )

    def test_percent_encoded_unicode(self) -> None:
        content = "![alt](%E4%B8%AD%E6%96%87.png)\n"
        self.assertEqual(
            _extract_note_image_paths(content, "index.md"),
            ["中文.png"],
        )

    def test_obsidian_embed_extracted(self) -> None:
        content = "![[nested/photo.png|300]]"
        self.assertEqual(
            _extract_note_image_paths(content, "index.md"),
            ["nested/photo.png"],
        )

    def test_external_urls_skipped(self) -> None:
        content = "![](https://example.com/hero.png)\n![](//cdn/hero.png)\n![](/root/hero.png)\n"
        self.assertEqual(_extract_note_image_paths(content, "index.md"), [])

    def test_non_image_extensions_skipped(self) -> None:
        content = "![](notes/readme.md)\n![](script.js)\n"
        self.assertEqual(_extract_note_image_paths(content, "index.md"), [])

    def test_note_relative_and_root_both_returned(self) -> None:
        content = "![](image.png)\n"
        # From nested/note.md the resolver yields both `nested/image.png`
        # (note-relative) and `image.png` (vault root) so the backend can
        # pick whichever one exists.
        self.assertEqual(
            _extract_note_image_paths(content, "nested/note.md"),
            ["nested/image.png", "image.png"],
        )

    def test_parent_relative_paths(self) -> None:
        content = "![](../attachments/hero.png)\n"
        self.assertEqual(
            _extract_note_image_paths(content, "nested/note.md"),
            ["attachments/hero.png"],
        )

    def test_query_and_fragment_stripped_for_extension_check(self) -> None:
        content = "![](hero.png?v=2)\n![](hero.png#fragment)\n"
        self.assertEqual(_extract_note_image_paths(content, "index.md"), ["hero.png"])

    def test_percent_encoded_hash_in_filename_survives_decode(self) -> None:
        # `%23` is a literal `#` in the filename, not a fragment delimiter.
        # A naive decode-before-split would treat `hero%23draft.png` as
        # `hero` and drop the extension, silently returning no metadata.
        content = "![](hero%23draft.png)\n"
        self.assertEqual(
            _extract_note_image_paths(content, "index.md"),
            ["hero#draft.png"],
        )

    def test_percent_encoded_question_mark_in_filename_survives_decode(self) -> None:
        # Same story for `%3F` — literal `?` in the filename must not be
        # mistaken for a query string opener.
        content = "![](hero%3Fdraft.png)\n"
        self.assertEqual(
            _extract_note_image_paths(content, "index.md"),
            ["hero?draft.png"],
        )


if __name__ == "__main__":
    unittest.main()
