#!/usr/bin/env python3
"""Probe free sources for the data that goes in club_extras.json.  READ-ONLY.

Run from the repo root:

    pip install requests
    python probe_club_extras.py                  # all 20 clubs
    python probe_club_extras.py --only 57 66     # just Arsenal and Man Utd
    python probe_club_extras.py --skip-tm        # Wikidata only
    python probe_club_extras.py --skip-wikidata  # Transfermarkt API only

What it does for every club in pl_teams.json:
  * Wikidata:      finds the club, then its current stadium (capacity, opened, city),
                   latest revenue (P2139) and awards (P166).
  * Transfermarkt: (unofficial API at transfermarkt-api.fly.dev) stadium, head coach
                   and achievements.  It has NO assistant coaches / staff endpoint.

It prints a coverage table so you can see which source fills which field, and writes:
  probe_report.json            everything it found, per club, incl. which Wikidata item
                               / Transfermarkt id it matched (CHECK THESE MATCHES!)
  club_extras.suggested.json   the same data in club_extras.json format, for you to
                               review and copy across by hand. It never edits
                               club_extras.json itself.

Neither source gives default_formation / starting_xi (the build job derives those)
or assistant coaches, so they are never filled in.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
TM_API = "https://transfermarkt-api.fly.dev"
# Wikimedia requires a descriptive User-Agent; edit the contact if you like.
UA = "ultimate-football-dashboard-probe/1.0 (personal project; python-requests)"

Q_ASSOCIATION_FOOTBALL_CLUB = "Q476028"
Q_GBP = "Q25224"

# football-data.org name  ->  better search text where plain stripping fails
NAME_FIXES = {
    "Brighton & Hove Albion FC": "Brighton & Hove Albion",
    "Manchester City FC": "Manchester City",
    "Manchester United FC": "Manchester United",
    "Newcastle United FC": "Newcastle United",
    "Tottenham Hotspur FC": "Tottenham Hotspur",
    "AFC Bournemouth": "AFC Bournemouth",
    "Sunderland AFC": "Sunderland",
    "Hull City AFC": "Hull City",
}

# Transfermarkt achievement titles we can map confidently. Anything else (including the
# many untitled groups the API returns) is reported but NOT put in the suggested file.
TM_TITLE_MAP = {
    "english champion": "English League Champions",
    "fa cup winner": "FA Cup",
    "english league cup winner": "League Cup",
    "english supercup winner": "Community Shield",
    "cup winners cup winner": "Cup Winners' Cup",
    "inter-cities fairs cup winner": "Inter-Cities Fairs Cup",
    "champions league winner": "Champions League",
    "uefa cup winner": "UEFA Cup",
    "europa league winner": "Europa League",
    "uefa conference league winner": "Conference League",
    "uefa super cup winner": "UEFA Super Cup",
    "fifa club world cup winner": "Club World Cup",
}

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "application/json"})


def get_json(url, params=None, retries=3, pause=2.0):
    last = None
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=45)
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(pause * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            last = str(exc)
            time.sleep(pause * (attempt + 1))
    raise RuntimeError(f"{url} failed: {last}")


def clean_name(name):
    if name in NAME_FIXES:
        return NAME_FIXES[name]
    return re.sub(r"\s+(FC|AFC)$", "", name).strip()


# --------------------------------------------------------------------------- #
# Wikidata
# --------------------------------------------------------------------------- #
def sparql(query):
    data = get_json(WIKIDATA_SPARQL, params={"query": query, "format": "json"})
    return data["results"]["bindings"]


def val(row, key):
    return row[key]["value"] if key in row else None


def wd_find_club(search_text):
    """Return (qid, label) of the best-matching association football club, or (None, None)."""
    hits = get_json(WIKIDATA_API, params={
        "action": "wbsearchentities", "search": search_text, "language": "en",
        "type": "item", "limit": 15, "format": "json"}).get("search", [])
    ids = [h["id"] for h in hits]
    if not ids:
        return None, None
    values = " ".join(f"wd:{i}" for i in ids)
    rows = sparql(f"""
        SELECT ?club ?clubLabel WHERE {{
          VALUES ?club {{ {values} }}
          ?club wdt:P31/wdt:P279* wd:{Q_ASSOCIATION_FOOTBALL_CLUB} .
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
        }}""")
    ok = {val(r, "club").rsplit("/", 1)[-1]: val(r, "clubLabel") for r in rows}
    for qid in ids:  # keep Wikidata's own relevance order
        if qid in ok:
            return qid, ok[qid]
    return None, None


def wd_stadium(qid):
    rows = sparql(f"""
        SELECT ?venue ?venueLabel ?cap ?inc ?cityLabel WHERE {{
          wd:{qid} p:P115 ?vs . ?vs ps:P115 ?venue .
          FILTER NOT EXISTS {{ ?vs pq:P582 ?ended }}
          OPTIONAL {{ ?venue wdt:P1083 ?cap }}
          OPTIONAL {{ ?venue wdt:P571 ?inc }}
          OPTIONAL {{ ?venue wdt:P131 ?city }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
        }}""")
    if not rows:
        return None
    # if several rows, prefer the one with a capacity, then the largest
    def cap(r):
        try:
            return int(float(val(r, "cap")))
        except (TypeError, ValueError):
            return -1
    best = max(rows, key=cap)
    inc = val(best, "inc")
    return {
        "venue": val(best, "venueLabel"),
        "capacity": cap(best) if cap(best) > 0 else None,
        "opened": int(inc[:4]) if inc and re.match(r"^-?\d{4}", inc) else None,
        "city": val(best, "cityLabel"),
    }


def wd_revenue(qid):
    rows = sparql(f"""
        SELECT ?rev ?date ?unit ?unitLabel WHERE {{
          wd:{qid} p:P2139 ?rs . ?rs ps:P2139 ?rev .
          OPTIONAL {{ ?rs pq:P585 ?date }}
          OPTIONAL {{ ?rs psv:P2139/wikibase:quantityUnit ?unit }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
        }}""")
    if not rows:
        return None
    best = max(rows, key=lambda r: val(r, "date") or "")
    unit = (val(best, "unit") or "").rsplit("/", 1)[-1]
    return {
        "amount": float(val(best, "rev")),
        "unit": val(best, "unitLabel"),
        "is_gbp": unit == Q_GBP,
        "date": (val(best, "date") or "")[:10] or None,
    }


def wd_honours(qid):
    rows = sparql(f"""
        SELECT ?awardLabel (COUNT(?s) AS ?n) WHERE {{
          wd:{qid} p:P166 ?s . ?s ps:P166 ?award .
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
        }} GROUP BY ?awardLabel ORDER BY DESC(?n)""")
    return [{"name": val(r, "awardLabel"), "count": int(val(r, "n"))} for r in rows]


def probe_wikidata(club):
    out = {"matched": None, "stadium": None, "revenue": None, "honours": [], "error": None}
    try:
        qid, label = wd_find_club(clean_name(club["name"]))
        if not qid:
            out["error"] = "no matching football club found"
            return out
        out["matched"] = {"qid": qid, "label": label, "url": f"https://www.wikidata.org/wiki/{qid}"}
        out["stadium"] = wd_stadium(qid)
        time.sleep(1)
        out["revenue"] = wd_revenue(qid)
        time.sleep(1)
        out["honours"] = wd_honours(qid)
    except Exception as exc:  # report, keep going
        out["error"] = str(exc)
    return out


# --------------------------------------------------------------------------- #
# Transfermarkt (unofficial API)
# --------------------------------------------------------------------------- #
def tm_find_club(search_text):
    data = get_json(f"{TM_API}/clubs/search/{requests.utils.quote(search_text)}")
    results = data.get("results") or []
    if not results:
        return None
    want = search_text.lower()
    for r in results:  # exact-ish name match first
        n = (r.get("name") or "").lower()
        if n == want or n == want + " fc":
            return r
    return results[0]


def probe_transfermarkt(club):
    out = {"matched": None, "stadium": None, "coach": None, "honours_titled": [],
           "honours_untitled_groups": 0, "error": None}
    try:
        hit = tm_find_club(clean_name(club["name"]))
        if not hit:
            out["error"] = "no search result"
            return out
        cid = hit["id"]
        out["matched"] = {"id": cid, "name": hit.get("name"),
                          "url": f"https://www.transfermarkt.com/-/startseite/verein/{cid}",
                          "country": hit.get("country"), "league": hit.get("league") or hit.get("competition")}
        profile = get_json(f"{TM_API}/clubs/{cid}/profile")
        st = profile.get("stadium") or {}
        cap = st.get("capacity")
        out["stadium"] = {
            "venue": st.get("name") or profile.get("stadiumName"),
            "capacity": int(str(cap).replace(",", "").replace(".", "")) if cap else None,
            "opened": st.get("buildYear"),
            "city": (st.get("addressLine2") or "").strip() or None,
        }
        out["coach"] = profile.get("coach") or None
        time.sleep(1)
        ach = get_json(f"{TM_API}/clubs/{cid}/achievements").get("achievements") or []
        for a in ach:
            title = (a.get("title") or "").strip()
            if not title:
                out["honours_untitled_groups"] += 1
                continue
            out["honours_titled"].append({
                "tm_title": title,
                "name": TM_TITLE_MAP.get(title.lower()),  # None = not mapped, review by hand
                "count": a.get("count"),
            })
    except Exception as exc:
        out["error"] = str(exc)
    return out


# --------------------------------------------------------------------------- #
# Report + suggested file
# --------------------------------------------------------------------------- #
def suggest(wd, tm):
    """Build a club_extras.json entry. Wikidata first, Transfermarkt fills the gaps.
    Honours / revenue are only suggested when the source is reasonably clean; always review."""
    entry, notes = {}, []
    stadium = {}
    for src_name, src in (("wikidata", wd.get("stadium")), ("transfermarkt", tm.get("stadium"))):
        if not src:
            continue
        for key in ("capacity", "city", "opened"):
            if key not in stadium and src.get(key):
                stadium[key] = src[key]
                notes.append(f"stadium.{key} from {src_name}")
    if stadium:
        entry["stadium"] = stadium

    rev = wd.get("revenue")
    if rev and rev["is_gbp"]:
        entry["revenue"] = {"amount_m": round(rev["amount"] / 1e6, 1), "currency": "GBP",
                            "season": rev["date"] or "?", "source": "Wikidata (P2139) - verify"}
        notes.append("revenue from wikidata (check year!)")

    honours = [{"name": h["name"], "count": h["count"]} for h in tm.get("honours_titled", []) if h["name"]]
    if honours:
        entry["honours"] = honours
        notes.append("honours from transfermarkt (League titles pre-1992 are 'First Division'; no Premier League row)")
    return entry, notes


def yn(x):
    return "yes" if x else " - "


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--teams", default="ufd/ufd_app/data/pl_teams.json")
    ap.add_argument("--only", nargs="*", help="football-data team ids to probe")
    ap.add_argument("--skip-wikidata", action="store_true")
    ap.add_argument("--skip-tm", action="store_true")
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args()

    path = Path(args.teams)
    if not path.exists():
        sys.exit(f"{path} not found - run from the repo root or pass --teams")
    teams = json.loads(path.read_text(encoding="utf-8"))["teams"]
    clubs = [{"id": tid, "name": t["profile"]["name"], "fd_coach": (t["profile"].get("coach") or {}).get("name")}
             for tid, t in teams.items() if not args.only or tid in args.only]

    report, suggested = {}, {}
    print(f"{'id':>5}  {'club':<28} | WD:stad rev  hon | TM:stad coach hon(titled) | match check")
    print("-" * 100)
    for club in clubs:
        wd = {} if args.skip_wikidata else probe_wikidata(club)
        tm = {} if args.skip_tm else probe_transfermarkt(club)
        report[club["id"]] = {"name": club["name"], "wikidata": wd, "transfermarkt": tm}
        entry, notes = suggest(wd, tm)
        if entry:
            suggested[club["id"]] = {"_note": f"{club['name']}: " + "; ".join(notes), **entry}

        wd_s = wd.get("stadium") or {}
        tm_s = tm.get("stadium") or {}
        tm_coach = (tm.get("coach") or {}).get("name")
        coach_ok = "" if not tm_coach else ("coach OK" if (club["fd_coach"] or "").split()[-1:] == tm_coach.split()[-1:] else f"COACH DIFFERS ({tm_coach})")
        wd_lbl = (wd.get("matched") or {}).get("label", "-")
        tm_lbl = (tm.get("matched") or {}).get("name", "-")
        print(f"{club['id']:>5}  {club['name']:<28} |"
              f"  {yn(wd_s.get('capacity')):>5} {yn(wd.get('revenue')):>4} {yn(wd.get('honours')):>4} |"
              f"  {yn(tm_s.get('capacity')):>5} {yn(tm_coach):>5} {len(tm.get('honours_titled', [])):>8}      |"
              f" WD='{wd_lbl}' TM='{tm_lbl}' {coach_ok}")
        for src, d in (("WD", wd), ("TM", tm)):
            if d.get("error"):
                print(f"{'':>7}! {src} error: {d['error']}")
        time.sleep(1)

    outdir = Path(args.outdir)
    (outdir / "probe_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (outdir / "club_extras.suggested.json").write_text(
        json.dumps({"_readme": "SUGGESTED ONLY. Review every value, then copy entries into club_extras.json.", **suggested},
                   indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nWrote probe_report.json and club_extras.suggested.json (club_extras.json untouched).")
    print("Open probe_report.json and check each 'matched' entry is really the right club before trusting anything.")
    print("Assistant coaches, default_formation and starting_xi are not available from either source.")


if __name__ == "__main__":
    main()