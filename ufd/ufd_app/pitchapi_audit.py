"""Consistency checks on a PitchAPI harvest folder (no network, no key).

These are the checks that show PitchAPI is trustworthy enough to be the source for matches and players:
the data must agree with itself. ``audit(folder)`` returns {"checks": [{name, ok, detail}], "matches": n}.
"""
import math
from collections import defaultdict

from . import pitchapi_source as ps

NEEDED = ("lineups", "stats", "players", "events")


def _correlation(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx, syy = sum((x - mx) ** 2 for x in xs), sum((y - my) ** 2 for y in ys)
    if not sxx or not syy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def _pass_share(stats):
    home, away = ps.team_stat_pair(stats, "passes")
    return 100 * home / (home + away) if home is not None and (home + away) else None


def audit(folder):
    source = ps.HarvestSource(folder)
    matches = source.finished_matches()
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": ok, "detail": detail})

    missing, goal_bad, dup_goals, geometry_bad, cards_bad = [], [], 0, [], []
    possession, shares = [], []
    table = defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    for m in matches:
        bundle = source.bundle(m["id"])
        gone = [n for n in NEEDED if not bundle.get(n)]
        if gone:
            missing.append(f"{m['id']} ({', '.join(gone)})")
            continue
        home, away = m["home_team"]["id"], m["away_team"]["id"]
        # goals: the event's team_id is the side the goal counts for (own goals included)
        raw = [e for e in bundle["events"].get("events") or [] if e.get("event_type") == "goal"]
        kept = [e for e in ps.dedupe_events(bundle["events"].get("events")) if e.get("event_type") == "goal"]
        dup_goals += len(raw) - len(kept)
        got = (sum(1 for e in kept if e.get("team_id") == home), sum(1 for e in kept if e.get("team_id") == away))
        if got != (m.get("score_home"), m.get("score_away")):
            goal_bad.append(f"{m['id']} events {got} vs score {(m.get('score_home'), m.get('score_away'))}")
        for side in ("home", "away"):
            side_data = bundle["lineups"].get(side) or {}
            formation, xi = side_data.get("formation"), side_data.get("starters") or []
            ordered = ps._starters_in_order(side_data)
            lines = 0
            last = None
            for p in ordered:
                x = p.get("pitch_x") or 0
                if last is None or x - last > 0.05:
                    lines += 1
                last = x
            if not formation or len(xi) != 11 or lines != len(formation.split("-")) + 1:
                geometry_bad.append(f"{m['id']} {side} {formation}")
        lines_, _ = ps.player_lines(bundle["players"], bundle["lineups"], bundle["events"].get("events"))
        if ps.cards_reconcile(lines_, bundle["stats"]) is False:
            cards_bad.append(m["id"])
        ph = ps._stat_value(bundle["stats"], "BallPossesion", "home")
        share = _pass_share(bundle["stats"])
        if ph not in (None, "") and share is not None:
            possession.append(float(ph))
            shares.append(share)
        sh, sa = m.get("score_home") or 0, m.get("score_away") or 0
        for team, gf, ga in ((home, sh, sa), (away, sa, sh)):
            row = table[team]
            row[0] += 1; row[4] += gf; row[5] += ga
            row[1] += gf > ga; row[2] += gf == ga; row[3] += gf < ga

    add("endpoints present for every finished match", not missing,
        f"{len(matches) - len(missing)}/{len(matches)} complete" + (f"; missing: {', '.join(missing[:5])}" if missing else ""))
    add("goal events add up to the final score", not goal_bad,
        f"{len(matches) - len(goal_bad) - len(missing)} exact; {dup_goals} duplicated goal events removed"
        + (f"; off: {'; '.join(goal_bad[:3])}" if goal_bad else ""))
    add("formation matches the starters' pitch positions", not geometry_bad,
        "all consistent" if not geometry_bad else f"{len(geometry_bad)} sides differ, e.g. {', '.join(geometry_bad[:3])}")
    corr = _correlation(possession, shares)
    add("possession agrees with pass share", corr is not None and corr > 0.95,
        "not enough matches" if corr is None else f"correlation {corr:.3f} over {len(possession)} matches")
    add("cards add up to the team statistics", not cards_bad,
        f"{len(matches) - len(cards_bad) - len(missing)} of {len(matches) - len(missing)} reconcile"
        + (" (a few are off by one or two: shown in the build report)" if cards_bad else ""))
    add("a league table can be rebuilt from the results", bool(table),
        f"{len(table)} clubs, {sum(r[4] for r in table.values())} goals")
    return {"matches": len(matches), "checks": checks}
