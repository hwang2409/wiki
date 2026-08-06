from __future__ import annotations

import unittest

from backend.app import installed_fonts


class InstalledFontsExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        installed_fonts.reset_cache_for_tests()

    def _entry(self, filename: str, family: str, *, enabled: str = "yes", tf_enabled: str = "yes") -> dict:
        return {
            "_name": filename,
            "enabled": enabled,
            "typefaces": [
                {"_name": family, "family": family, "enabled": tf_enabled},
            ],
        }

    def test_extracts_unique_enabled_family_names_sorted_case_insensitively(self) -> None:
        payload = {
            "SPFontsDataType": [
                self._entry("A.ttf", "JetBrains Mono"),
                self._entry("B.ttf", "Fira Code"),
                self._entry("C.ttf", "jetbrains mono"),  # dedupes exact case? no — casefold sort only
                self._entry("D.ttf", "Cascadia Code"),
            ]
        }
        families = installed_fonts._extract_families(payload)
        # Different case is a different family — no folding; sort is case-insensitive.
        self.assertIn("JetBrains Mono", families)
        self.assertIn("jetbrains mono", families)
        self.assertEqual(sorted(families, key=str.casefold), families)

    def test_distinct_nerd_font_families_survive_as_separate_entries(self) -> None:
        # WIKI-260 core requirement: variants must not be grouped.
        payload = {
            "SPFontsDataType": [
                self._entry("jetbrains-mono.ttf", "JetBrains Mono"),
                self._entry("jetbrains-nerd.ttf", "JetBrainsMono Nerd Font"),
                self._entry("jetbrains-nerd-mono.ttf", "JetBrainsMono Nerd Font Mono"),
                self._entry("jetbrains-nl-nerd.ttf", "JetBrainsMonoNL Nerd Font"),
            ]
        }
        families = installed_fonts._extract_families(payload)
        self.assertIn("JetBrains Mono", families)
        self.assertIn("JetBrainsMono Nerd Font", families)
        self.assertIn("JetBrainsMono Nerd Font Mono", families)
        self.assertIn("JetBrainsMonoNL Nerd Font", families)
        # Each is its own entry — no accidental dedupe.
        self.assertEqual(len(set(families)), len(families))

    def test_disabled_entries_and_typefaces_are_skipped(self) -> None:
        payload = {
            "SPFontsDataType": [
                self._entry("a.ttf", "Enabled Family"),
                self._entry("b.ttf", "Disabled File", enabled="no"),
                self._entry("c.ttf", "Disabled Face", tf_enabled="no"),
            ]
        }
        families = installed_fonts._extract_families(payload)
        self.assertEqual(families, ["Enabled Family"])

    def test_whitespace_and_missing_family_names_are_dropped(self) -> None:
        payload = {
            "SPFontsDataType": [
                {"_name": "a.ttf", "enabled": "yes", "typefaces": [{"family": "  ", "enabled": "yes"}]},
                {"_name": "b.ttf", "enabled": "yes", "typefaces": [{"enabled": "yes"}]},
                self._entry("c.ttf", "  Padded  "),
            ]
        }
        families = installed_fonts._extract_families(payload)
        self.assertEqual(families, ["Padded"])

    def test_malformed_payload_returns_empty(self) -> None:
        self.assertEqual(installed_fonts._extract_families({}), [])
        self.assertEqual(installed_fonts._extract_families(None), [])
        self.assertEqual(installed_fonts._extract_families({"SPFontsDataType": "junk"}), [])


class InstalledFontsCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        installed_fonts.reset_cache_for_tests()

    def tearDown(self) -> None:
        installed_fonts.reset_cache_for_tests()

    def test_enumerate_runs_once_and_result_is_cached(self) -> None:
        calls = {"n": 0}

        def fake_enumerate() -> list[str]:
            calls["n"] += 1
            return ["Fake Font"]

        original = installed_fonts._enumerate
        installed_fonts._enumerate = fake_enumerate  # type: ignore[assignment]
        try:
            self.assertEqual(installed_fonts.installed_families(), ["Fake Font"])
            self.assertEqual(installed_fonts.installed_families(), ["Fake Font"])
            self.assertEqual(calls["n"], 1)
        finally:
            installed_fonts._enumerate = original  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
