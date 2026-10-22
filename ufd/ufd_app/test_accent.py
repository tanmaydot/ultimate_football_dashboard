from django.test import SimpleTestCase

from . import accent


class AccentTests(SimpleTestCase):
    CLUBS = {
        "Arsenal": ["#dc2626", "#ffffff"], "Chelsea": ["#1e4fb3", "#ffffff"], "Man City": ["#6cabdd", "#ffffff"],
        "Aston Villa": ["#7a263a", "#6cabdd"], "Tottenham": ["#1b2a4e", "#ffffff"], "Newcastle": ["#111827", "#ffffff"],
        "Fulham": ["#ffffff", "#111827"], "Hull": ["#f97316", "#111827"], "Leeds": ["#ffffff", "#2563eb"],
    }

    def test_every_club_is_readable_in_both_themes(self):
        for club, colours in self.CLUBS.items():
            tokens = accent.accent_tokens(tuple(colours))
            for theme, rules in accent.THEMES.items():
                t = tokens[theme]
                with self.subTest(club=club, theme=theme):
                    self.assertGreaterEqual(accent.contrast(t["a-fill"], t["a-ink"]), 4.5)
                    self.assertGreaterEqual(min(accent.contrast(t["accent-text"], s) for s in rules["text_against"]), 4.5)
                    self.assertGreaterEqual(min(accent.contrast(t["a-fill"], s) for s in rules["fill_against"]), 3.0)

    def test_hue_is_kept(self):
        self.assertEqual(accent.accent_tokens(("#dc2626",))["light"]["a-fill"], "#dc2626")

    def test_black_and_white_clubs_flip_with_the_theme(self):
        tokens = accent.accent_tokens(("#111827", "#ffffff"))
        self.assertEqual(tokens["light"]["a-fill"], "#111827")
        self.assertEqual(tokens["dark"]["a-fill"], "#ffffff")

    def test_unknown_or_unsafe_input_falls_back_to_the_default(self):
        css = accent.accent_css(["not a colour", "red; } body { display:none"])
        self.assertNotIn("display", css)
        self.assertIn(accent.DEFAULT_COLOURS[0], css)
