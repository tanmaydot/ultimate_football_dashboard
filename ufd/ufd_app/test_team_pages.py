import json
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from . import dashboard, team_pages
from .test_dashboard import built_data


class SquadPageTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pl.json"
        self.path.write_text(json.dumps(built_data()), encoding="utf-8")
        for patcher in (mock.patch.object(dashboard, "PL_DATA_PATH", self.path),
                        mock.patch.object(dashboard, "load_club_extras", return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)
        dashboard._data_cache.update(path=None, mtime=None, data=None)

    def page(self, team_id=61):
        return self.client.get(reverse("ufd_app:squad", args=[team_id]))

    def test_every_player_is_listed_with_pitchapi_numbers(self):
        response = self.page()
        self.assertEqual(response.status_code, 200)
        players = {p["name"]: p for p in response.context["players"]}
        self.assertEqual(set(players), {"Chelsea Keeper", "Chelsea Winger", "Chelsea Unknown"})
        winger = players["Chelsea Winger"]
        self.assertEqual((winger["goals"], winger["assists"], winger["has_stats"]), (3, 3, True))
        # a squad player who never appeared gets an honest zero (the club has played), not blanks
        self.assertEqual((players["Chelsea Unknown"]["goals"], players["Chelsea Unknown"]["appearances"]), (0, 0))
        self.assertContains(response, "Chelsea squad")
        self.assertContains(response, "PitchAPI match data")

    def test_players_are_grouped_goalkeeper_first(self):
        groups = [p["group"] for p in self.page().context["players"]]
        self.assertEqual(groups, ["Goalkeeper", "Defence", "Offence"])

    def test_summary(self):
        summary = self.page().context["summary"]
        self.assertEqual(summary["players"], 3)
        self.assertEqual(summary["nationalities"], 3)
        self.assertEqual(summary["without_stats"], 0)  # a squad player who never appeared has an honest zero record
        self.assertEqual([g for g, _ in summary["groups"]], ["Goalkeeper", "Defence", "Offence"])

    def test_sidebar_keeps_you_on_the_squad_page_when_you_pick_another_club(self):
        html = self.page().content.decode()
        self.assertIn(f'data-href="{reverse("ufd_app:squad", args=[66])}"', html)

    def test_back_link_and_unknown_club(self):
        self.assertContains(self.page(), reverse("ufd_app:dashboard", args=[61]))
        self.assertEqual(self.page(999).status_code, 200)  # friendly error page, not a crash
        self.assertContains(self.page(999), "isn&#x27;t in the Premier League data")

    def test_missing_data_file_explains_how_to_build_it(self):
        self.path.unlink()
        dashboard._data_cache.update(path=None, mtime=None, data=None)
        self.assertContains(self.page(), "build_pl_data")

    def test_squad_widget_title_links_to_the_page(self):
        response = self.client.get(reverse("ufd_app:dashboard", args=[61]))
        layout = {w["id"]: w for w in response.context["default_layout"]}
        self.assertEqual(layout["squad"]["url"], reverse("ufd_app:squad", args=[61]))
        self.assertNotIn("url", layout["stats"])  # only widgets with a page link out
        self.assertContains(response, 'target="_blank" rel="noopener"')

    def test_rating_zero_is_treated_as_no_rating(self):
        rows = team_pages.squad_rows([{"name": "A", "position": "Goalkeeper", "pitchapi": {"rating": 0.0, "goals": 0}},
                                      {"name": "B", "position": "Goalkeeper", "pitchapi": {"rating": 6.8}}])
        self.assertEqual([r["rating"] for r in rows], [None, 6.8])
        self.assertEqual(rows[0]["goals"], 0)  # a real zero stays a zero


class ManagerAndFormationPageTests(SquadPageTests):
    """Reuses the squad tests' data setup (the inherited squad tests run again, harmlessly)."""

    def test_manager_page(self):
        response = self.client.get(reverse("ufd_app:manager", args=[61]))
        self.assertEqual(response.status_code, 200)
        manager = response.context["manager"]
        self.assertEqual((manager["name"], manager["nationality"]), ("Chelsea Boss", "England"))
        self.assertEqual((manager["contract_start"], manager["contract_until"]), ("2026-01", "2028-06"))
        self.assertEqual(manager["age"], dashboard._age("1975-01-01"))
        record = response.context["record"]
        self.assertEqual((record["played"], record["won"], record["win_rate"], record["avg_points"]), (3, 2, 67, "2.33"))
        self.assertEqual([(f["formation"], f["count"], f["share"]) for f in response.context["formations"]],
                         [("4-2-3-1", 2, 67), ("4-3-3", 1, 33)])
        self.assertContains(response, "This widget is under development")  # no assistants in the hand-kept file
        self.assertContains(response, "may include games under a previous manager")

    def test_manager_page_lists_hand_kept_assistants(self):
        extras = {"61": {"assistant_coaches": [{"name": "Jane Assistant", "role": "First-team coach"}]}}
        with mock.patch.object(dashboard, "load_club_extras", return_value=extras):
            response = self.client.get(reverse("ufd_app:manager", args=[61]))
        self.assertContains(response, "Jane Assistant")
        self.assertNotContains(response, "This widget is under development")

    def test_manager_page_without_a_manager(self):
        data = built_data()
        data["teams"]["61"]["profile"]["coach"] = None
        self.path.write_text(json.dumps(data), encoding="utf-8")
        dashboard._data_cache.update(path=None, mtime=None, data=None)
        response = self.client.get(reverse("ufd_app:manager", args=[61]))
        self.assertIsNone(response.context["manager"])
        self.assertContains(response, "No manager is listed")

    def test_contract_time_left(self):
        with mock.patch.object(team_pages, "date") as fake_date:
            fake_date.today.return_value = date(2026, 10, 7)
            self.assertEqual(team_pages._contract_label(team_pages._months_until("2028-06")), "1 year 8 months left")
            self.assertEqual(team_pages._contract_label(team_pages._months_until("2026-10")), "Contract ends this month")
            self.assertEqual(team_pages._contract_label(team_pages._months_until("2026-03")), "Contract has ended")
            self.assertIsNone(team_pages._contract_label(team_pages._months_until(None)))

    def test_formation_page_opens_on_the_most_used_formation(self):
        response = self.client.get(reverse("ufd_app:formation", args=[61]))
        ctx = response.context
        self.assertEqual(ctx["selected"], "4-2-3-1")
        self.assertEqual([o["formation"] for o in ctx["options"]], ["4-2-3-1", "4-3-3"])
        by_name = {o["formation"]: o for o in ctx["options"]}
        self.assertTrue(by_name["4-2-3-1"]["is_most_used"])
        self.assertFalse(by_name["4-3-3"]["is_most_used"])
        self.assertTrue(by_name["4-2-3-1"]["pitch"]["has_names"])  # each formation has its own XI
        self.assertEqual(ctx["matches_counted"], 3)
        html = response.content.decode()
        self.assertEqual(html.count('class="formation-panel'), 2)
        self.assertEqual(html.count("formation-panel\" data-formation=\"4-3-3\" hidden"), 1)  # only the selected panel is visible
        self.assertContains(response, "Most used formation")
        self.assertNotContains(response, "Starting XI")
        self.assertContains(response, "Edit tactics")
        self.assertContains(response, "Formation info")
        self.assertContains(response, "pitch-art")  # markings are drawn
        self.assertEqual(len(ctx["dock"]), 3)

    def test_old_data_without_per_formation_details_still_works(self):
        data = built_data()
        del data["teams"]["61"]["formation"]["formations"]
        self.path.write_text(json.dumps(data), encoding="utf-8")
        dashboard._data_cache.update(path=None, mtime=None, data=None)
        ctx = self.client.get(reverse("ufd_app:formation", args=[61])).context
        self.assertEqual({o["formation"] for o in ctx["options"]}, {"4-2-3-1", "4-3-3"})
        self.assertFalse(any(o["pitch"]["has_names"] for o in ctx["options"]))  # positions only, no invented names

    def test_formation_page_without_any_formation_data(self):
        data = built_data(with_pitchapi=False)
        self.path.write_text(json.dumps(data), encoding="utf-8")
        dashboard._data_cache.update(path=None, mtime=None, data=None)
        response = self.client.get(reverse("ufd_app:formation", args=[61]))
        self.assertEqual(response.context["options"], [])
        self.assertContains(response, "No formation data for this club yet")

    def test_all_three_widget_titles_link_out(self):
        layout = {w["id"]: w.get("url") for w in self.client.get(reverse("ufd_app:dashboard", args=[61])).context["default_layout"]}
        self.assertNotIn("manager", layout)  # merged into the squad widget
        self.assertEqual(layout["coaches"], reverse("ufd_app:manager", args=[61]))
        self.assertEqual(layout["formation"], reverse("ufd_app:formation", args=[61]))
        self.assertEqual(layout["squad"], reverse("ufd_app:squad", args=[61]))
        self.assertIsNone(layout["stats"])


class PlayerPageTests(TestCase):
    """Step 12: every player has a page, linked from the squad and formation views."""

    setUp = SquadPageTests.setUp

    def page(self, team_id=61, player_id=612):
        return self.client.get(reverse("ufd_app:player", args=[team_id, player_id]))

    def test_player_page_shows_profile_and_numbers(self):
        response = self.page()
        self.assertEqual(response.status_code, 200)
        p = response.context["p"]
        self.assertEqual((p["name"], p["full_name"], p["goals"], p["minutes"], p["captain"], p["injured"]),
                         ("Chelsea Winger", "Chelsea Winger", 3, 270, False, False))
        self.assertContains(response, "Chelsea Winger")
        self.assertNotContains(response, "Injured")  # PitchAPI has no injury list
        headings = {s["heading"]: {i["label"]: i["value"] for i in s["items"]} for s in response.context["sections"]}
        self.assertEqual(headings["Attacking"]["Shots"], 3)
        self.assertEqual(headings["Passing"]["Pass accuracy"], "80%")  # (3 x 40) / (3 x 50) accurate passes
        self.assertEqual(headings["Defending"]["Tackles"], 6)
        # outfield players get no goalkeeping box
        self.assertNotIn("Goalkeeping", headings)

    def test_player_without_stats_still_has_a_page(self):
        self.path.write_text(json.dumps(built_data(with_pitchapi=False)), encoding="utf-8")
        dashboard._data_cache.update(path=None, mtime=None, data=None)
        response = self.page(player_id=613)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["has_stats"])
        self.assertContains(response, "couldn't be matched")

    def test_a_player_who_never_appeared_shows_real_zeros(self):
        response = self.page(player_id=613)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["has_stats"])
        self.assertEqual(response.context["p"]["goals"], 0)

    def test_unknown_player_or_club(self):
        self.assertEqual(self.page(player_id=999999).status_code, 200)  # friendly explanation page
        self.assertContains(self.page(player_id=999999), "in this club")
        self.assertContains(self.page(team_id=1), "in the Premier League")

    def test_squad_page_and_dashboard_link_to_player_pages(self):
        url = reverse("ufd_app:player", args=[61, 612])
        self.assertContains(self.client.get(reverse("ufd_app:squad", args=[61])), f'href="{url}"')
        self.assertContains(self.client.get(reverse("ufd_app:dashboard", args=[61])), f'href="{url}"')

    def test_formation_slots_carry_player_ids(self):
        squad = [{"id": 1, "name": "Robert Sánchez"}, {"id": 2, "name": "Cole Palmer"}, {"id": 3, "name": "Enzo Fernández"},
                 {"id": 4, "name": "Moises Caicedo"}, {"id": 5, "name": "Nicolas Jackson"}, {"id": 6, "name": "Pedro Neto"},
                 {"id": 7, "name": "Pedro Porro"}, {"id": 8, "name": "Marc Cucurella"}, {"id": 9, "name": "Levi Colwill"},
                 {"id": 10, "name": "Trevoh Chalobah"}, {"id": 11, "name": "Reece James"}]
        xi = ["R. Sanchez", "R. James", "L. Colwill", "T. Chalobah", "M. Cucurella", "M. Caicedo", "Enzo Fernandez",
              "C. Palmer", "P. Neto", "N. Jackson", "Someone Unknown"]
        pitch = dashboard.build_formation("4-3-3", xi, squad)
        ids = {s["name"]: s["id"] for line in pitch["lines"] for s in line["slots"]}
        self.assertEqual((ids["Sanchez"], ids["Palmer"], ids["Fernandez"], ids["Unknown"]), (1, 2, 3, None))

    def test_ambiguous_lineup_name_is_not_linked(self):
        squad = [{"id": 1, "name": "Pedro Neto"}, {"id": 2, "name": "Pedro Porro"}, {"id": 3, "name": "Luis Neto"}]
        self.assertEqual(dashboard.find_player_id("P. Neto", squad), 1)
        self.assertIsNone(dashboard.find_player_id("Neto", squad))
        self.assertIsNone(dashboard.find_player_id("", squad))

    def test_formation_page_names_link_when_matched(self):
        pitch = dashboard.build_formation("4-3-3", ["Chelsea Keeper"] + ["x"] * 10, [{"id": 611, "name": "Chelsea Keeper"}])
        self.assertEqual(pitch["lines"][0]["slots"][0]["id"], 611)


class PenaltyTests(PlayerPageTests):
    def test_penalties_are_listed_even_when_zero(self):
        response = self.page()
        pens = next(sec for sec in response.context["sections"] if sec["heading"] == "Penalties")
        self.assertEqual({i["label"]: i["value"] for i in pens["items"]},
                         {"Penalties scored": 1, "Penalties missed": 0, "Penalties won": 0, "Penalties conceded": 0})
        self.assertContains(response, "Penalties scored")

    def test_no_penalty_rows_for_a_player_without_any_numbers(self):
        self.path.write_text(json.dumps(built_data(with_pitchapi=False)), encoding="utf-8")
        dashboard._data_cache.update(path=None, mtime=None, data=None)
        self.assertNotContains(self.page(player_id=613), "Penalties")


class MobileNoticeTests(PlayerPageTests):
    def test_every_page_carries_the_small_screen_notice(self):
        for name, args in (("dashboard", [61]), ("squad", [61]), ("formation", [61]), ("manager", [61]), ("player", [61, 612])):
            response = self.client.get(reverse(f"ufd_app:{name}", args=args))
            self.assertContains(response, "Best on a bigger screen")
            self.assertContains(response, "Continue anyway")


class SquadManagerWidgetTests(PlayerPageTests):
    def test_one_widget_holds_manager_and_position_columns(self):
        html = self.client.get(reverse("ufd_app:dashboard", args=[61])).content.decode()
        squad_url = reverse("ufd_app:squad", args=[61])
        self.assertNotIn('id="tpl-manager"', html)
        self.assertIn('class="squad-strip"', html)
        self.assertIn(reverse("ufd_app:manager", args=[61]), html)  # the Manager column links to the managers page
        self.assertIn(f"{squad_url}?group=Goalkeeper", html)
        self.assertIn(f"{squad_url}?group=Defence", html)

    def test_squad_page_opens_filtered_from_the_group_link(self):
        html = self.client.get(reverse("ufd_app:squad", args=[61]) + "?group=Defence").content.decode()
        self.assertIn("URLSearchParams", html)
        self.assertIn('data-group="Defence"', html)


class TeamInfoStripTests(PlayerPageTests):
    def test_team_info_carries_stadium_and_neighbouring_clubs(self):
        extras = {"61": {"stadium": {"capacity": 40173}}, "66": {"stadium": {"capacity": 74310}}, "76": {"stadium": {"capacity": 31750}}}
        with mock.patch.object(dashboard, "load_club_extras", return_value=extras):
            response = self.client.get(reverse("ufd_app:dashboard", args=[61]))
        ctx = response.context
        self.assertEqual(ctx["stadium"]["capacity_pct"], 54)  # 40,173 of the biggest ground, 74,310
        self.assertEqual(ctx["stadium"]["capacity_rank"], "2nd largest in the league")
        self.assertEqual([r["team"]["id"] for r in ctx["neighbours"]], [61, 76, 66])
        self.assertEqual([r["current"] for r in ctx["neighbours"]], [True, False, False])
        html = response.content.decode()
        info = html[html.index('id="tpl-info"'):html.index('id="tpl-revenue"')]
        self.assertIn("info-strip", info)
        self.assertIn("Home ground", info)
        self.assertIn("40,173", info)
        # the neighbouring clubs are still worked out (context above) but the redesigned panel no longer links to them

    def test_stadium_widget_is_gone(self):
        layout = {w["id"]: w for w in self.client.get(reverse("ufd_app:dashboard", args=[61])).context["default_layout"]}
        self.assertNotIn("stadium", layout)  # its details live in Team info now
        self.assertFalse(layout["info"].get("hidden"))
