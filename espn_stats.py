"""Daily ESPN -> Google Sheet. Pulls final scores and every player's box-score line for every
league in LEAGUES, and writes them to the Sheet:

  Games   one row per final game: date, league, away/home, scores
  Stats   one row per player per stat group per game. 'Stats' holds every column ESPN lists for
          that group, e.g. "REC=6; YDS=118; AVG=19.7; TD=1; LONG=43; TGTS=11".
          Football passers also get an extra row (group 'passing-long') with their longest completion.
  Golf    final leaderboard for completed PGA tournaments
  Status  heartbeat (last run time, counts, any leagues that errored)

Env vars:
  GOOGLE_SERVICE_ACCOUNT_JSON  full contents of the service account key file
  SHEET_ID                     id from the Google Sheet URL
  LEAGUES        optional comma list (default below)
  LOOKBACK_DAYS  optional, days of scoreboards to scan (default 3)
  KEEP_DAYS      optional, days of Stats rows to keep (default 7); Games/Golf keep 45
  DRY_RUN        set to anything to skip the Sheet and just print
"""
import os, sys, json, time, datetime as dt
from zoneinfo import ZoneInfo
import requests

ROOT = "https://site.api.espn.com/apis/site/v2/sports/{path}"
UA = {"User-Agent": "Mozilla/5.0 (personal betting log script)"}

# league key -> (ESPN path, extra scoreboard params)
LEAGUES = {
    "nfl":                       ("football/nfl", {}),
    "college-football":          ("football/college-football", {"groups": 80}),
    "nba":                       ("basketball/nba", {}),
    "wnba":                      ("basketball/wnba", {}),
    "mens-college-basketball":   ("basketball/mens-college-basketball", {"groups": 50}),
    "womens-college-basketball": ("basketball/womens-college-basketball", {"groups": 50}),
    "mlb":                       ("baseball/mlb", {}),
    "nhl":                       ("hockey/nhl", {}),
}
GOLF = {"pga": "golf/pga"}
DEFAULT = ",".join(list(LEAGUES) + ["pga"])

STAT_COLS = ["Date", "League", "GameID", "Matchup", "Team", "Player", "PlayerID", "Group", "Stats"]
GAME_COLS = ["Date", "League", "GameID", "Away", "Home", "AwayScore", "HomeScore", "Status"]
GOLF_COLS = ["Date", "League", "EventID", "Tournament", "Player", "PlayerID", "Pos", "ToPar", "R1", "R2", "R3", "R4", "Total"]
CENTRAL = ZoneInfo("America/Chicago")


def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=30)
            if r.status_code == 200:
                return r.json()
            print(f"  HTTP {r.status_code} for {url}", file=sys.stderr)
        except Exception as e:
            print(f"  error {e} for {url}", file=sys.stderr)
        time.sleep(2 * (i + 1))
    return None


def central_date(iso):
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(CENTRAL).date().isoformat()


def completed_events(path, extra, day):
    params = {"dates": day.strftime("%Y%m%d"), "limit": 400, **extra}
    data = get(ROOT.format(path=path) + "/scoreboard", params)
    return [ev for ev in (data or {}).get("events", []) if ev.get("status", {}).get("type", {}).get("completed")]


def passer_longs(summary):
    """Football: longest completion per passer id from play-by-play (box score has no passing long)."""
    longs = {}
    drives = summary.get("drives", {})
    for d in list(drives.get("previous", [])) + ([drives["current"]] if drives.get("current") else []):
        for p in d.get("plays", []):
            if (p.get("type") or {}).get("text", "") not in ("Pass Reception", "Passing Touchdown"):
                continue
            yds = p.get("statYardage")
            if yds is None:
                continue
            for part in p.get("participants", []):
                if part.get("type") == "passer":
                    a = part.get("athlete") or {}
                    pid = str(a.get("id") or a.get("$ref", "").split("/")[-1].split("?")[0])
                    longs[pid] = max(longs.get(pid, -999), int(yds))
    return longs


def parse_game(lg, path, ev):
    eid = ev["id"]
    comp = ev["competitions"][0]
    teams = {c["homeAway"]: c for c in comp["competitors"]}
    away, home = teams["away"]["team"]["abbreviation"], teams["home"]["team"]["abbreviation"]
    date = central_date(ev["date"])

    def score(c):
        try:
            return int(float(c.get("score") or 0))
        except Exception:
            return 0
    game_row = [date, lg, eid, away, home, score(teams["away"]), score(teams["home"]), "Final"]
    summ = get(ROOT.format(path=path) + "/summary", {"event": eid})
    if not summ:
        print(f"  no box score for {lg} {eid}", file=sys.stderr)
        return game_row, []
    matchup = f"{away} @ {home}"
    rows, seen_pids = [], {}
    for tblock in summ.get("boxscore", {}).get("players", []):
        team = tblock["team"]["abbreviation"]
        for grp in tblock.get("statistics", []):
            gname = grp.get("name") or grp.get("type") or "stats"
            labels = grp.get("labels") or grp.get("keys") or []
            for a in grp.get("athletes", []):
                ath = a.get("athlete", {})
                stats = a.get("stats", [])
                text = "; ".join(f"{l}={v}" for l, v in zip(labels, stats))
                if not text:
                    continue
                rows.append([date, lg, eid, matchup, team, ath.get("displayName", ""), str(ath.get("id", "")), gname, text])
                seen_pids[str(ath.get("id", ""))] = (team, ath.get("displayName", ""))
    if lg in ("nfl", "college-football"):
        for pid, longest in passer_longs(summ).items():
            team, name = seen_pids.get(pid, ("", pid))
            rows.append([date, lg, eid, matchup, team, name, pid, "passing-long", f"LONG_COMPLETION={longest}"])
    return game_row, rows


def parse_golf(lg, path, ev):
    comp = ev["competitions"][0]
    date = central_date(comp.get("endDate") or ev["date"])
    out = []
    for c in comp.get("competitors", []):
        ath = c.get("athlete", {})
        rounds = {}
        for ls in c.get("linescores", []):
            try:
                rounds[int(ls.get("period"))] = ls.get("value")
            except Exception:
                pass
        pos = ((c.get("status") or {}).get("position") or {}).get("displayName") or c.get("order", "")
        total = sum(v for v in rounds.values() if isinstance(v, (int, float))) if rounds else ""
        out.append([date, lg, ev["id"], ev.get("name", ""), ath.get("displayName", ""), str(ath.get("id", "")),
                    pos, c.get("score", ""), rounds.get(1, ""), rounds.get(2, ""), rounds.get(3, ""), rounds.get(4, ""), total])
    return out


def open_sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])


def write_tab(sh, title, cols, new_rows, key_idx, keep_after):
    """Replace rows for the games/events in new_rows, keep other recent rows, drop old ones."""
    try:
        ws = sh.worksheet(title)
    except Exception:
        ws = sh.add_worksheet(title, rows=2000, cols=len(cols))
    existing = ws.get_all_values()
    body = existing[1:] if existing and existing[0] == cols else []
    new_keys = {(r[1], str(r[key_idx])) for r in new_rows}
    kept = [r for r in body if len(r) > key_idx and (r[1], r[key_idx]) not in new_keys and r[0] >= keep_after]
    allrows = kept + new_rows
    ws.clear()
    if ws.row_count < len(allrows) + 5:
        ws.resize(rows=len(allrows) + 50, cols=len(cols))
    ws.update(range_name="A1", values=[cols] + allrows, value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    return len(allrows)


def main():
    wanted = [x.strip() for x in os.environ.get("LEAGUES", DEFAULT).split(",") if x.strip()]
    lookback = int(os.environ.get("LOOKBACK_DAYS", 3))
    keep = int(os.environ.get("KEEP_DAYS", 7))
    today = dt.datetime.utcnow().date()
    games, stats, golf, errors = [], [], [], []
    for lg in wanted:
        try:
            seen = set()
            if lg in LEAGUES:
                path, extra = LEAGUES[lg]
                for back in range(lookback + 1):
                    for ev in completed_events(path, extra, today - dt.timedelta(days=back)):
                        if ev["id"] in seen:
                            continue
                        seen.add(ev["id"])
                        g, rows = parse_game(lg, path, ev)
                        games.append(g); stats += rows
                        print(f"{lg} {g[3]} @ {g[4]} {g[5]}-{g[6]}: {len(rows)} stat rows")
            elif lg in GOLF:
                path = GOLF[lg]
                for back in range(lookback + 1):
                    for ev in completed_events(path, {}, today - dt.timedelta(days=back)):
                        if ev["id"] in seen:
                            continue
                        seen.add(ev["id"])
                        golf += parse_golf(lg, path, ev)
                        print(f"{lg} {ev.get('name')}: final leaderboard")
            else:
                errors.append(f"unknown league {lg}")
        except Exception as e:
            errors.append(f"{lg}: {e}")
            print(f"ERROR in {lg}: {e}", file=sys.stderr)
    print(f"Total: {len(games)} games, {len(stats)} stat rows, {len(golf)} golf rows, errors: {errors}")
    if os.environ.get("DRY_RUN"):
        return
    sh = open_sheet()
    cut = lambda d: (today - dt.timedelta(days=d)).isoformat()
    print("Games:", write_tab(sh, "Games", GAME_COLS, games, 2, cut(45)))
    print("Stats:", write_tab(sh, "Stats", STAT_COLS, stats, 2, cut(keep)))
    if golf or "pga" in wanted:
        print("Golf:", write_tab(sh, "Golf", GOLF_COLS, golf, 2, cut(45)))
    try:
        hb = sh.worksheet("Status")
    except Exception:
        hb = sh.add_worksheet("Status", rows=20, cols=4)
    hb.update(range_name="A1", values=[["Stats script last run (UTC)", dt.datetime.utcnow().isoformat(timespec="seconds"),
                                       f"{len(games)} games / {len(stats)} stat rows / {len(golf)} golf rows",
                                       "; ".join(errors) or "no errors"]])


if __name__ == "__main__":
    main()
