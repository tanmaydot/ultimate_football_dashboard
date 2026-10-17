"""Download as much as possible from PitchAPI and keep every raw response on disk.

Layout of the output folder (default ``ufd/pitchapi_raw``)::

    leagues.json                    GET /v1/leagues
    league_<id>.json                GET /v1/leagues/{id}
    matches/<season>.played.json    GET /v1/leagues/{id}/matches?season=..&status=played
    matches/<season>.upcoming.json  ... status=upcoming (fixtures)
    match/<match id>/<endpoint>.json  one file per match endpoint (summary, lineups, stats, ...)
    teams/<team id>.json            GET /v1/teams/{id}
    players/<player id>.json        GET /v1/players/{id}
    _index.json                     what was fetched, what the API said no to, totals

Properties that matter for a one-off run with a key that will soon be rotated:
* resumable: a file that already exists is never fetched again (use --refresh to redo), so a stop
  (Ctrl-C, a dropped connection) loses nothing and re-running just continues;
* an answer of "this match has no advanced data" (404 ANALYTICS_UNAVAILABLE etc.) is recorded, not retried;
* every write is atomic (temp file + rename), so a stop never leaves half a file;
* the API key is never written anywhere.
"""
import json
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path

from .pitchapi import PitchApiAuthError, PitchApiError

# (file name, path under /v1/matches/{id}, query params). Order = most valuable first.
MATCH_ENDPOINTS = [
    ("summary", "", None),
    ("lineups", "/lineups", None),
    ("stats", "/stats", None),
    ("events", "/events", None),
    ("players", "/players", None),
    ("shots", "/shots", None),
    ("momentum", "/momentum", None),
    ("h2h", "/h2h", None),
    ("advanced", "/advanced", None),
    ("advanced_players", "/advanced/players", None),
    ("advanced_network", "/advanced/network", None),
    ("players_halves", "/players/halves", None),
    ("heatmaps", "/heatmaps", None),
    ("heatmaps_home_ltr", "/heatmaps", {"frame": "home_ltr"}),
]
# Needs a player id, and is mostly a re-slice of the bulk files above: opt in with --per-player.
PER_PLAYER_ENDPOINTS = [
    ("player", "/players/{pid}", None),
    ("player_halves", "/players/{pid}/halves", None),
    ("player_shots", "/players/{pid}/shots", None),
    ("advanced_player", "/advanced/players/{pid}", None),
]
UPCOMING_ENDPOINTS = ("summary", "lineups", "h2h")  # all that make sense before kick-off

_ID_RE = {"team": re.compile(r'"(t_[A-Za-z0-9_-]+)"'), "player": re.compile(r'"(p_[A-Za-z0-9_-]+)"')}
_SEASON_RE = re.compile(r"^\d{4}(?:[/-]\d{2,4})?$")
_NOT_RETRIED = {"ANALYTICS_UNAVAILABLE", "HALVES_UNAVAILABLE", "RESOURCE_NOT_FOUND"}


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-") or "x"


def find_dicts(node):
    """Every dict anywhere inside a decoded JSON value."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from find_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from find_dicts(value)


def find_league(leagues_body, wanted="Premier League", country="england"):
    """The league dict for ``wanted``. Prefers an English one when the entries say which country they are in."""
    candidates = [d for d in find_dicts(leagues_body) if str(d.get("name") or "").strip().lower() == wanted.lower()
                  and isinstance(d.get("id"), str) and d["id"].startswith("l_")]
    if not candidates:
        return None
    def english(d):
        # /v1/leagues has four "Premier League"s (England, Russia, Egypt, Canada); country_code tells them apart
        return str(d.get("country_code") or "").upper() == "ENG" or country in json.dumps(d).lower()
    return next((d for d in candidates if english(d)), candidates[0])


def extract_seasons(league):
    """Season labels (like '2024/2025') found anywhere in a league entry, newest first."""
    found = set()
    def walk(node):
        if isinstance(node, str):
            if _SEASON_RE.match(node.strip()):
                found.add(node.strip())
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(league)
    return sorted(found, reverse=True)


def match_ids(body):
    seen, ids = set(), []
    for d in find_dicts(body):
        mid = d.get("id")
        if isinstance(mid, str) and mid.startswith("m_") and mid not in seen:
            seen.add(mid)
            ids.append(mid)
    return ids


class Harvester:
    def __init__(self, client, out_dir, log=print, refresh=False, max_requests=None, deadline=None):
        self.client = client
        self.out = Path(out_dir)
        self.log = log
        self.refresh = refresh
        self.max_requests = max_requests
        self.deadline = deadline          # time.time() value after which we stop cleanly
        self.stats = {}                   # endpoint kind -> {"ok", "cached", "unavailable", "failed"}
        self.failures = []
        self.stopped = None               # reason, when we stopped early
        self.auth_failed = False

    # ---- plumbing --------------------------------------------------------
    def _bump(self, kind, what):
        self.stats.setdefault(kind, {"ok": 0, "cached": 0, "unavailable": 0, "failed": 0})[what] += 1

    def _budget_left(self):
        if self.stopped:
            return False
        if self.max_requests is not None and self.client.requests_made >= self.max_requests:
            self.stopped = f"stopped after {self.client.requests_made} requests (--max-requests)"
        elif self.deadline is not None and time.time() >= self.deadline:
            self.stopped = "stopped at the --max-minutes limit"
        return not self.stopped

    @staticmethod
    def _write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def fetch(self, kind, path, dest, params=None):
        """Fetch one resource into ``dest`` unless it is already there. Returns the stored dict, or None."""
        if dest.exists() and not self.refresh:
            self._bump(kind, "cached")
            try:
                return json.loads(dest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass  # a damaged file: fetch it again
        if not self._budget_left():
            return None
        try:
            body = self.client.get(path, params)
        except PitchApiAuthError:
            self.stopped = "the API key was rejected (401): stopping"
            self.auth_failed = True
            return None
        except PitchApiError as exc:
            if exc.status == 404 and exc.code in _NOT_RETRIED:
                self._write(dest, {"_error": {"status": 404, "code": exc.code}, "_path": path})
                self._bump(kind, "unavailable")
            else:
                self._bump(kind, "failed")
                self.failures.append({"path": path, "params": params, "error": str(exc)})
                self.log(f"  failed  {path}: {exc}")
            return None
        self._write(dest, body)
        self._bump(kind, "ok")
        return body

    # ---- the crawl -------------------------------------------------------
    def run(self, league_name="Premier League", league_id=None, seasons=None, all_seasons=False, endpoints=None,
            include_upcoming=False, per_player=False, profiles=True):
        started = time.time()
        wanted = set(endpoints) if endpoints else None

        leagues = self.fetch("leagues", "/v1/leagues", self.out / "leagues.json")
        if leagues is None:
            self.log("Could not read /v1/leagues; nothing else can be fetched.")
            return self._finish(started)
        league = None
        if league_id:
            league = next((d for d in find_dicts(leagues) if d.get("id") == league_id), {"id": league_id})
        else:
            league = find_league(leagues, league_name)
        if not league:
            names = sorted({str(d.get("name")) for d in find_dicts(leagues) if d.get("name")})[:60]
            self.log(f"'{league_name}' not found in /v1/leagues. Names seen: {', '.join(names)}")
            self.failures.append({"path": "/v1/leagues", "error": f"{league_name} not found"})
            return self._finish(started)
        lid = league["id"]
        self.log(f"League: {league.get('name', league_name)} ({lid})")
        detail = self.fetch("league", f"/v1/leagues/{lid}", self.out / f"league_{lid}.json")

        season_list = [None]  # None = the current season (the API default)
        discovered = extract_seasons(league) or extract_seasons(detail or {})
        if seasons:
            season_list = list(seasons)
        elif all_seasons and discovered:
            season_list = discovered
        self.log(f"Seasons: {', '.join(s or 'current' for s in season_list)}"
                 + (f" (found {len(discovered)} listed by the API)" if discovered else ""))

        all_played, upcoming = [], []
        for season in season_list:
            label = safe_name(season or "current")
            params = {"season": season} if season else {}
            played = self.fetch("league_matches", f"/v1/leagues/{lid}/matches", self.out / "matches" / f"{label}.played.json",
                                {**params, "status": "played"})
            if played:
                ids = match_ids(played)
                self.log(f"  {season or 'current'}: {len(ids)} played matches")
                all_played += [i for i in ids if i not in all_played]
            if include_upcoming:
                fixtures = self.fetch("league_matches", f"/v1/leagues/{lid}/matches", self.out / "matches" / f"{label}.upcoming.json",
                                      {**params, "status": "upcoming"})
                if fixtures:
                    upcoming += [i for i in match_ids(fixtures) if i not in upcoming]

        todo = [(m, MATCH_ENDPOINTS) for m in all_played] + (
            [(m, [e for e in MATCH_ENDPOINTS if e[0] in UPCOMING_ENDPOINTS]) for m in upcoming] if include_upcoming else [])
        total = len(todo)
        for n, (mid, endpoint_list) in enumerate(todo, 1):
            if not self._budget_left():
                break
            for name, suffix, params in endpoint_list:
                if wanted and name not in wanted:
                    continue
                self.fetch(name, f"/v1/matches/{mid}{suffix}", self.out / "match" / mid / f"{name}.json", params)
            if per_player:
                self._per_player(mid)
            if n % 10 == 0 or n == total:
                self.log(f"  matches {n}/{total}  ({self.client.requests_made} requests so far)")

        if profiles and self._budget_left():
            self._profiles("team", "teams", "/v1/teams/{}")
            self._profiles("player", "players", "/v1/players/{}")
        return self._finish(started)

    def _ids_in_tree(self, kind):
        ids = set()
        for path in (self.out).rglob("*.json"):
            parts = path.parts
            if "teams" in parts[-2:-1] or "players" in parts[-2:-1] or path.name in ("_index.json",):
                continue
            try:
                ids.update(_ID_RE[kind].findall(path.read_text(encoding="utf-8")))
            except OSError:
                pass
        return sorted(ids)

    def _per_player(self, mid):
        players_file = self.out / "match" / mid / "players.json"
        if not players_file.exists():
            return
        try:
            body = json.loads(players_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        pids = sorted({d["id"] for d in find_dicts(body) if isinstance(d.get("id"), str) and d["id"].startswith("p_")})
        for pid in pids:
            for kind, suffix, params in PER_PLAYER_ENDPOINTS:
                self.fetch("per_player_" + kind, f"/v1/matches/{mid}{suffix.format(pid=pid)}",
                           self.out / "match" / mid / "players" / f"{kind}_{pid}.json", params)

    def _profiles(self, id_kind, folder, template):
        ids = self._ids_in_tree(id_kind)
        self.log(f"{folder}: {len(ids)} ids found in the downloaded matches")
        for n, rid in enumerate(ids, 1):
            if not self._budget_left():
                break
            self.fetch(folder, template.format(rid), self.out / folder / f"{rid}.json")
            if n % 50 == 0:
                self.log(f"  {folder} {n}/{len(ids)}")

    def _finish(self, started):
        index = {
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "seconds": round(time.time() - started),
            "requests_this_run": self.client.requests_made,
            "rate_limited_429": self.client.rate_limited,
            "stopped_early": self.stopped,
            "by_endpoint": self.stats,
            "failures": self.failures[:200],
            "files_on_disk": sum(1 for p in self.out.rglob("*.json") if p.name != "_index.json") if self.out.exists() else 0,
        }
        self._write(self.out / "_index.json", index)
        return index


def zip_folder(folder, zip_path):
    """Pack the harvest into one file that is easy to send or keep."""
    folder, zip_path = Path(folder), Path(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(folder.parent))
    return zip_path
