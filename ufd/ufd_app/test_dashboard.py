import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from . import dashboard, pl_data
from .pl_fixtures import TODAY, FakeApiFootball, FakeFootballData

NO_CACHE = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}


def built_data(with_apifootball=True):
    ap = FakeApiFootball() if with_apifootball else FakeApiFootball(configured=False)
    with mock.patch.object(pl_data, "MIN_TEAMS", 3):
        return pl_data.build_dataset(FakeFootballData(), ap, log=lambda *_: None, today=TODAY)


class DashboardFromJsonTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "pl_teams.json"
        self.write(built_data())
        patchers = [
            mock.patch.object(dashboard, "PL_DATA_PATH", self.path),
            mock.patch.object(dashboard, "load_club_extras", return_value={}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        dashboard._data_cache.update(path=None, mtime=None, data=None)

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def page(self, team_id=61):
        return self.client.get(reverse("ufd_app:dashboard", args=[team_id]))

    def test_every_widget_is_filled_from_the_file(self):
        response = self.page()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertEqual(html.count('<template id="tpl-'), 7)  # no league table (sidebar) and no badge (it's beside the title)
        ctx = response.context
        self.assertEqual(ctx["stats"]["avg_points"], "2.33")
        self.assertEqual(ctx["stats"]["clean_sheets"], 2)
        self.assertEqual(ctx["stats"]["possession"], 55.7)
        self.assertIn("55.7%", html)
        self.assertEqual(ctx["stadium"]["capacity"], "40,173")
        self.assertEqual(ctx["coach"]["name"], "Chelsea Boss")
        self.assertEqual(ctx["assistants"], [])  # APIs don't give reliable staff; only club_extras.json does
        self.assertEqual(ctx["formation"]["label"], "4-2-3-1")  # Chelsea's most used formation
        self.assertEqual(ctx["formation"]["kind"], "most_used")
        self.assertEqual(ctx["team"]["position"], "1st of 3")
        self.assertEqual(ctx["total_players"], 3)
        self.assertEqual(len(ctx["table"]), 3)

    def test_formation_pitch_shows_the_starting_xi_in_order(self):
        formation = self.page().context["formation"]
        self.assertTrue(formation["has_names"])
        self.assertEqual([len(line["slots"]) for line in formation["lines"]], [1, 4, 2, 3, 1])
        xi = built_data()["teams"]["61"]["formation"]["starting_xi"]
        short = dashboard._short_name
        self.assertTrue(xi[0].endswith("Player 1"))  # position 1 (the goalkeeper) comes first
        self.assertEqual(formation["lines"][0]["slots"][0]["name"], short(xi[0]))
        # every outfield row is mirrored: the lineup's first defender is drawn on the right
        self.assertEqual([s["name"] for s in formation["lines"][1]["slots"]], [short(x) for x in reversed(xi[1:5])])
        self.assertEqual([s["name"] for s in formation["lines"][3]["slots"]], [short(x) for x in reversed(xi[7:10])])

    def test_formation_widget_says_most_used_formation(self):
        self.assertContains(self.page(), "Most used formation")
        self.assertNotContains(self.page(), "Last used formation")

    def test_widgets_without_data_say_under_development(self):
        html = self.page().content.decode()
        self.assertEqual(html.count("This widget is under development"), 3)  # revenue, assistants, trophies
        self.assertIn("Most used formation", html)
        self.assertNotIn("Premier League titles", html)

    def test_sidebar_has_the_scrollable_table_and_no_powered_by(self):
        response = self.page()
        html = response.content.decode()
        self.assertIn('id="sidebarClose"', html)
        self.assertIn("sidebar-table", html)
        self.assertEqual(html.count('club-row is-current'), 1)
        self.assertNotIn("Powered by", html)
        self.assertNotIn("Football-Data.org", html)

    def test_info_widget_shows_the_league_position(self):
        self.assertContains(self.page(66), "League position")
        self.assertEqual(self.page(66).context["team"]["position"], "3rd of 3")

    def test_footer_links(self):
        html = self.page().content.decode()
        for target in ("https://github.com/tanmaydot", "https://www.linkedin.com/in/tanmaydot/",
                       "mailto:reachtanmays@gmail.com", "fa-github", "fa-linkedin-in", "fa-envelope"):
            self.assertIn(target, html)

    def test_squad_rows_carry_apifootball_goals_and_assists(self):
        groups = dict(self.page().context["squad_groups"])
        winger = groups["Offence"][0]
        self.assertEqual((winger["goals"], winger["assists"]), (4, 2))
        self.assertIsNone(groups["Defence"][0]["goals"])  # unmatched player: no invented numbers

    def test_works_when_apifootball_was_not_available(self):
        self.write(built_data(with_apifootball=False))
        response = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["formation"])
        self.assertIsNone(response.context["stats"]["possession"])
        self.assertIsNone(response.context["stadium"]["capacity"])
        self.assertContains(response, "N/A")
        self.assertContains(response, "No default formation set")

    def test_club_extras_override_the_generated_data(self):
        extras = {"61": {"default_formation": "3-5-2", "revenue": {"amount_m": 612.4, "currency": "GBP", "season": "2025/26"},
                         "stadium": {"capacity": 41000, "opened": 1877},
                         "honours": [{"name": "Premier League", "count": 6}]}}
        with mock.patch.object(dashboard, "load_club_extras", return_value=extras):
            response = self.page()
        ctx = response.context
        self.assertEqual(ctx["formation"]["label"], "3-5-2")
        self.assertEqual(ctx["stadium"]["capacity"], "41,000")
        self.assertEqual(ctx["revenue"]["display"], "£612m")
        self.assertEqual(ctx["trophies"]["honours"][0]["count"], 6)
        self.assertNotContains(response, "×6 under")  # real data replaces the notice for those widgets
        self.assertEqual(response.content.decode().count("This widget is under development"), 1)  # only assistants left

    def test_missing_data_file_explains_how_to_build_it(self):
        self.path.unlink()
        for url in (reverse("ufd_app:dashboard_home"), reverse("ufd_app:dashboard", args=[61])):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "build_pl_data")
                self.assertContains(response, "No Premier League data yet")

    def test_corrupt_or_empty_data_file_is_treated_as_missing(self):
        for content in ("{not json", "[]", json.dumps({"teams": {}})):
            with self.subTest(content=content):
                self.path.write_text(content)
                dashboard._data_cache.update(path=None, mtime=None, data=None)
                self.assertContains(self.client.get(reverse("ufd_app:dashboard", args=[61])), "No Premier League data yet")

    def test_unknown_club_shows_a_message_not_an_error(self):
        response = self.page(999)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dashboard unavailable")

    def test_home_redirects_to_first_club_then_to_the_last_one_viewed(self):
        home = reverse("ufd_app:dashboard_home")
        self.assertRedirects(self.client.get(home), reverse("ufd_app:dashboard", args=[61]), fetch_redirect_response=False)
        self.page(76)
        self.assertRedirects(self.client.get(home), reverse("ufd_app:dashboard", args=[76]), fetch_redirect_response=False)

    def test_rebuilt_file_is_picked_up_without_restart(self):
        self.assertEqual(self.page().context["total_players"], 3)
        data = built_data()
        data["teams"]["61"]["profile"]["squad"] = data["teams"]["61"]["profile"]["squad"][:1]
        self.write(data)
        import os
        os.utime(self.path, (self.path.stat().st_atime, self.path.stat().st_mtime + 5))
        self.assertEqual(self.page().context["total_players"], 1)

    def test_a_team_with_missing_fields_still_renders(self):
        data = built_data()
        data["teams"]["61"] = {"profile": {"id": 61, "name": "Bare FC"}}
        self.write(data)
        dashboard._data_cache.update(path=None, mtime=None, data=None)
        response = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bare FC")


class DashboardOnlySiteTests(TestCase):
    """Step 4: the dashboard is the whole site."""

    def test_home_redirects_to_the_dashboard(self):
        response = self.client.get("/")
        self.assertRedirects(response, "/dashboard/", fetch_redirect_response=False)

    def test_old_pages_are_gone(self):
        for url in ("/league/PL/", "/league/PL/standings/", "/league/PL/matches/", "/team/57/"):
            self.assertEqual(self.client.get(url).status_code, 404, url)


class ThemeTests(TestCase):
    """Step 6: light / dark / amoled themes and seven accent colours, no hard-coded purple gradient."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "pl.json"
        path.write_text(json.dumps(built_data()), encoding="utf-8")
        for patcher in (mock.patch.object(dashboard, "PL_DATA_PATH", path),
                        mock.patch.object(dashboard, "load_club_extras", return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)
        dashboard._data_cache.update(path=None, mtime=None, data=None)
        self.html = self.client.get(reverse("ufd_app:dashboard", args=[61])).content.decode()

    def test_every_theme_and_accent_is_offered(self):
        for theme in ("light", "dark", "amoled"):
            self.assertIn(f'data-theme-choice="{theme}"', self.html)
            self.assertIn(f':root[data-theme="{theme}"]', self.html)
        for accent in ("red", "green", "blue", "orange", "purple", "pink", "yellow"):
            self.assertIn(f'data-accent-choice="{accent}"', self.html)
            self.assertIn(f':root[data-accent="{accent}"]', self.html)

    def test_choice_is_applied_before_first_paint_and_saved(self):
        self.assertIn("localStorage.getItem('ufd.theme')", self.html)
        self.assertIn("localStorage.setItem(key, value)", self.html)
        self.assertIn('data-theme="dark" data-accent="green"', self.html)  # fallback if scripts are blocked

    def test_old_gradient_look_is_gone(self):
        for leftover in ("gradient-text", "glass-card", "animate-glow", "text-purple-400", "daisyui", "from-purple-500"):
            self.assertNotIn(leftover, self.html)

    def test_widgets_resize_from_every_edge_and_corner(self):
        self.assertIn("handles: 'n, ne, e, se, s, sw, w, nw'", self.html)
        self.assertIn("minW: meta[i.id].minW || 2", self.html)  # a widget can't be dragged down to nothing


class ClubSelectionTests(TestCase):
    """Step 8: the club is chosen from the sidebar table; the dropdown is gone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "pl.json"
        path.write_text(json.dumps(built_data()), encoding="utf-8")
        for patcher in (mock.patch.object(dashboard, "PL_DATA_PATH", path),
                        mock.patch.object(dashboard, "load_club_extras", return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)
        dashboard._data_cache.update(path=None, mtime=None, data=None)

    def test_no_club_dropdown(self):
        html = self.client.get(reverse("ufd_app:dashboard", args=[61])).content.decode()
        self.assertNotIn("teamPicker", html)
        self.assertNotIn("<select", html)

    def test_every_table_row_opens_its_club(self):
        html = self.client.get(reverse("ufd_app:dashboard", args=[61])).content.decode()
        for team_id in (61, 66, 76):
            self.assertIn(f'data-href="{reverse("ufd_app:dashboard", args=[team_id])}"', html)
        self.assertEqual(html.count('aria-current="true"'), 1)
        self.assertIn("Click a club to switch", html)

    def test_dashboard_home_opens_a_club_that_is_in_the_table(self):
        response = self.client.get(reverse("ufd_app:dashboard_home"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard/", response["Location"])


class Step11CleanupTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "pl.json"
        path.write_text(json.dumps(built_data()), encoding="utf-8")
        for patcher in (mock.patch.object(dashboard, "PL_DATA_PATH", path),
                        mock.patch.object(dashboard, "load_club_extras", return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)
        dashboard._data_cache.update(path=None, mtime=None, data=None)
        response = self.client.get(reverse("ufd_app:dashboard", args=[61]))
        self.html = response.content.decode()
        self.ctx = response.context

    def test_badge_widget_is_gone_and_the_crest_sits_beside_the_title(self):
        self.assertNotIn("tpl-badge", self.html)
        self.assertNotIn("badge", [w["id"] for w in self.ctx["default_layout"]])
        title = self.html.index("Club dashboard</h1>")
        crest = self.html.rindex('alt="Chelsea FC badge"', 0, title)
        self.assertLess(title - crest, 250)  # the image is right next to the heading

    def test_instruction_line_is_gone(self):
        for text in ("drag a widget by its header", "data updated", "some stats via", "footballdata.io"):
            self.assertNotIn(text, self.html)

    def test_appearance_is_folded_away_by_default(self):
        self.assertIn('<details id="appearance"', self.html)
        self.assertNotIn('<details id="appearance" open', self.html)
        self.assertIn('data-theme-choice="amoled"', self.html)  # still inside the fold

    def test_pitch_ignores_the_accent(self):
        css = self.html[self.html.index(".pitch {"): self.html.index(".pitch-name {")]
        self.assertNotIn("var(--accent", css)
        self.assertIn("#15803d", css)
