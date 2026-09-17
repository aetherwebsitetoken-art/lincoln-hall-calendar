#!/usr/bin/env python3
"""
Lincoln Hall Athletics -- QuickScores auto-scraper
====================================================

What this does
---------------
1. Fetches the Little Nine Conference "Schedules List" page on QuickScores,
   which lists EVERY current league (every sport, every season, every level)
   -- not a hardcoded set of 4 leagues. This is what makes new teams (like a
   Boys Volleyball squad that appears after tryouts) show up automatically
   the moment QuickScores creates a league page for them.
2. Visits every one of those league pages and pulls out every game.
3. Keeps only the games (and tournament/playoff notices) that involve
   Lincoln Hall.
4. Writes everything to events.json in this same folder.

This script is meant to be run automatically by the GitHub Action in
.github/workflows/update-schedule.yml, but you can also run it yourself:

    pip install requests beautifulsoup4
    python3 scrape_schedule.py

Notes / honest limitations
---------------------------
- This works by reading the *visible text* of QuickScores' public pages, in
  order, and pattern-matching it (dates, times, team-name links, scores,
  officials). It is not using a private/undocumented API, and it respects
  QuickScores' robots.txt (the /Orgs/ pages used here are not disallowed).
- QuickScores prints each game's date as two separate lines: the weekday,
  then the date below it. Crucially, the year appears ONLY when it isn't the
  current year -- this year's games render as a bare "Sep 8", while other
  years render as "Apr 6, 2027". An earlier version of this script required
  the year, which silently dropped every game in the current season while
  past and future seasons parsed fine. Both forms are handled now, and a
  missing year is filled in from the season header on the page itself (so a
  "Winter 2026-27" league puts November in 2026 and January in 2027).
- Fetching uses a plain HTTP request, not a headless browser. QuickScores
  is a plain server-rendered site (the game data is present in the raw
  HTML), so a browser was never actually needed -- an earlier version of
  this script used one anyway "just in case," and that turned out to
  actively cause a real bug: heavier, mid-season pages could time out
  waiting for the browser's "network idle" signal and get silently
  skipped, which is why games from an active season could go missing while
  a lighter, not-yet-started season's games showed up fine. Plain requests
  don't have that failure mode.
- If QuickScores changes their layout, the script may need small tweaks.
  It's written defensively (it logs what it finds per league, and skips
  anything it can't confidently parse rather than guessing), and if it
  ever finds zero events overall it automatically prints a raw dump of
  what it actually saw, so a layout change can be diagnosed directly from
  the Action log.
- One known gap: a schedule note that isn't attached to a specific date
  (e.g. a vague "opponent still TBD" tournament blurb with no date of its
  own) won't be picked up. Anything with an actual date will be.
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone

import requests
from bs4 import BeautifulSoup

# --- Configuration -----------------------------------------------------

ORG = "little9"
BASE = "https://www.quickscores.com"
SCHEDULES_URL = f"{BASE}/Orgs/Schedules.php?OrgDir={ORG}"
TEAM_NAME = "Lincoln Hall"      # exact team-name text as QuickScores shows it
TEAM_ABBREV = "L.H."            # sometimes used in free-text notes
HEADERS = {
    "User-Agent": "LincolnHallCalendarBot/1.0 (parent-run schedule sync)"
}
REQUEST_DELAY_SECONDS = 2       # be polite -- don't hammer their server
REQUEST_TIMEOUT_SECONDS = 15    # per-attempt timeout for a single page
MAX_FETCH_ATTEMPTS = 3          # retry a slow/failed request before giving up on that league

# --- Regex helpers -------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

WEEKDAY_RE = re.compile(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,?$', re.IGNORECASE)
# QuickScores prints a game's date on the line after the weekday. It includes
# the year ONLY when that year isn't the current one -- this year's games show
# as just "Sep 8", while other years show as "Apr 6, 2027". Both forms must be
# handled, or every game in the current season silently disappears.
FULLDATE_RE = re.compile(r'^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,\s*(\d{4})$', re.IGNORECASE)
NOYEAR_DATE_RE = re.compile(r'^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?$', re.IGNORECASE)
# The season header shown on each league page, e.g. "Fall 2026  -  Soccer".
PAGE_SEASON_RE = re.compile(r'^(Spring|Summer|Fall|Winter)\s+(\d{4})(?:-(\d{2}))?\s*-?\s*(.*)$', re.IGNORECASE)
WEEK_RE = re.compile(r'^Week\s*\d+\s*:?$', re.IGNORECASE)
STOP_RE = re.compile(r'Show Schedule Analysis|Schedule Analysis|Time Slot Distribution', re.IGNORECASE)
SEASON_HEADER_RE = re.compile(r'^(Spring|Summer|Fall|Winter)\s+(\d{4})(?:-(\d{2}))?\s*-?\s*(.+)$', re.IGNORECASE)
LEAGUE_LINK_RE = re.compile(r'^\[([^\]]+)\]\(([^)]*ResultsDisplay\.php\?OrgDir=' + ORG + r'&LeagueID=(\d+))\)$')
TEAM_LINK_RE = re.compile(r'^\[([^\]]+)\]\(([^)]*ResultsDisplay\.php\?OrgDir=' + ORG + r'&LeagueID=(\d+)&TeamID=(\d+)[^)]*)\)$')
LOC_LINK_RE = re.compile(r'^\[([^\]]+)\]\(([^)]*LocationDetails\.php[^)]*)\)$')
TIME_RE = re.compile(r'^(\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)\.?$')
SCORE_RE = re.compile(r'^\d+(\.\d+)?$')


def _looks_like_ref_name(candidate):
    """Loose sanity check so stray sentences don't get mistaken for an
    official's name."""
    if not candidate:
        return False
    words = candidate.split()
    if len(words) > 3:
        return False
    if any(ch.isdigit() for ch in candidate):
        return False
    lowered = candidate.lower()
    if lowered.startswith(("no ", "bye", "tournament", "play", "tbd")):
        return False
    return True


def normalize_time(raw):
    """Turn '4:15 p.m.' / '4:15pm' / '4:15 PM' into a clean '4:15 PM'."""
    m = re.match(r'^(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\.?$', raw.strip())
    if not m:
        return raw.strip().upper()
    return f"{m.group(1)}:{m.group(2)} {m.group(3).upper()}M"


# --- Fetching & flattening ------------------------------------------------

def fetch(url):
    """Fetch a page, retrying a couple of times on a timeout or transient
    network error before giving up. QuickScores appears to respond slowly
    or inconsistently to automated requests sometimes -- without retries, a
    single slow response drops that whole league's games for this run, even
    though the page is fine and would have loaded a few seconds later."""
    last_error = None
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < MAX_FETCH_ATTEMPTS:
                print(f"     (attempt {attempt} failed: {e} -- retrying)", file=sys.stderr)
                time.sleep(REQUEST_DELAY_SECONDS)
    raise last_error


def html_to_lines(html):
    """Flatten a page to an ordered list of text lines, turning every <a>
    into a markdown-style [text](href) so link targets survive the
    flattening (this is what lets us tell teams/locations apart from plain
    text using just the URL pattern)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for a in soup.find_all("a"):
        href = a.get("href", "") or ""
        text = a.get_text(" ", strip=True)
        a.replace_with(f"[{text}]({href})")
    text = soup.get_text("\n")
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


# --- League discovery -----------------------------------------------------

def discover_leagues(lines):
    """Walk the Schedules List page and return every league found, each
    tagged with its season label, e.g. 'Fall 2026 Soccer'."""
    leagues = []
    current_season = None
    for ln in lines:
        m = SEASON_HEADER_RE.match(ln)
        if m:
            current_season = ln
            continue
        m = LEAGUE_LINK_RE.match(ln)
        if m:
            leagues.append({
                "league_id": m.group(3),
                "name": m.group(1),
                "season": current_season or "",
            })
    return leagues


# --- Date resolution --------------------------------------------------
#
# QuickScores prints each game's date as TWO separate lines -- the weekday
# abbreviation alone ("Tue"), then the date below it. The year is included
# only when it differs from the current year, so both "Apr 6, 2027" and a
# bare "Sep 8" must be handled.

def _is_month_token(token):
    return token[:3].title() in MONTHS


def find_page_season_years(lines):
    """Read the season header off a league page (e.g. "Fall 2026  -  Soccer")
    and return the calendar year(s) it spans. Used to fill in the year for
    dates that QuickScores printed without one."""
    for ln in lines[:80]:
        m = PAGE_SEASON_RE.match(ln)
        if m:
            y1 = int(m.group(2))
            y2 = int(str(y1)[:2] + m.group(3)) if m.group(3) else y1
            return y1, y2
    return None


def resolve_explicit_date(month_token, day_str, year_str):
    month = MONTHS.get(month_token[:3].title())
    if month is None:
        return None
    try:
        return date(int(year_str), month, int(day_str))
    except ValueError:
        return None


def resolve_undated_year(month_token, day_str, season_years):
    """Work out the year for a date QuickScores printed without one. Prefer
    the page's own season header (so a "Winter 2026-27" league puts November
    in 2026 and January in 2027); fall back to the current year, which is
    what an omitted year means by QuickScores' own convention."""
    month = MONTHS.get(month_token[:3].title())
    if month is None:
        return None
    if season_years:
        y1, y2 = season_years
        year = y1 if (y1 == y2 or month >= 7) else y2
    else:
        year = date.today().year
    try:
        return date(year, month, int(day_str))
    except ValueError:
        return None


def sport_type(league_name):
    n = league_name.lower()
    if "soccer" in n:
        return "soccer"
    if "volleyball" in n:
        return "volleyball"
    if "basketball" in n:
        return "basketball"
    return "other"


def _date_line_match(line):
    """Return (month_token, day, year_or_None) if this line is a game date."""
    m = FULLDATE_RE.match(line)
    if m and _is_month_token(m.group(1)):
        return m.group(1), m.group(2), m.group(3)
    m = NOYEAR_DATE_RE.match(line)
    if m and _is_month_token(m.group(1)):
        # The month check matters: without it, lines like "Week 1" match this
        # pattern's shape and would be mistaken for dates.
        return m.group(1), m.group(2), None
    return None


def _is_game_header(lines, i):
    """True when lines[i] is a weekday line immediately followed by a date
    line -- this marks the start of a new game entry."""
    if i + 1 >= len(lines):
        return False
    return bool(WEEKDAY_RE.match(lines[i])) and _date_line_match(lines[i + 1]) is not None


# --- Game/event parsing --------------------------------------------------

def _team_link_match(line, league_id):
    """Match a team-link line, but only if it points at THIS league. Some
    QuickScores pages include a static team directory/navigation block that
    links to every team across every league -- without this check, that
    directory gets miscounted as if it were part of the actual schedule."""
    m = TEAM_LINK_RE.match(line)
    if m and m.group(3) == str(league_id):
        return m
    return None


def _process_block(current_date, block, league_name, league_id):
    """Turn the lines belonging to a single game into 0+ event dicts.
    Expected shape (each on its own line): time, location link, home team
    link, [score], away team link, [score], [official's name]."""
    if not block:
        return []

    idx = 0
    time_str = ""
    loc_name = ""

    if idx < len(block):
        m = TIME_RE.match(block[idx])
        if m:
            time_str = normalize_time(m.group(1))
            idx += 1

    if idx < len(block) and not _team_link_match(block[idx], league_id):
        loc_m = LOC_LINK_RE.match(block[idx])
        if loc_m:
            loc_name = loc_m.group(1)
            idx += 1
        elif block[idx].strip() and any(_team_link_match(bl, league_id) for bl in block[idx:]):
            # A plain-text location (e.g. "Niles West", no link) -- only
            # treated as one if real team links still follow, so a
            # bye/tournament note doesn't get mistaken for a place name.
            loc_name = block[idx].strip()
            idx += 1

    remaining = block[idx:]
    team_matches = [_team_link_match(bl, league_id) for bl in remaining]
    team_matches = [tm for tm in team_matches if tm]

    events = []
    if len(team_matches) >= 2:
        home_name = team_matches[0].group(1).strip()
        away_name = team_matches[1].group(1).strip()
        if TEAM_NAME in (home_name, away_name):
            scores = [bl for bl in remaining if SCORE_RE.match(bl)]
            home_score = scores[0] if len(scores) > 0 else ""
            away_score = scores[1] if len(scores) > 1 else ""
            ref = ""
            for bl in reversed(remaining):
                if bl and not _team_link_match(bl, league_id) and not SCORE_RE.match(bl) and _looks_like_ref_name(bl):
                    ref = bl
                    break
            note = ""
            if home_score and away_score:
                lh_score = home_score if home_name == TEAM_NAME else away_score
                opp_score = away_score if home_name == TEAM_NAME else home_score
                try:
                    lh_i, opp_i = int(float(lh_score)), int(float(opp_score))
                    result = "Win" if lh_i > opp_i else ("Loss" if lh_i < opp_i else "Tie")
                    note = f"{lh_score}-{opp_score} ({result})"
                except ValueError:
                    pass
            events.append({
                "date": current_date.isoformat(),
                "sport": league_name,
                "type": sport_type(league_name),
                "match": f"{home_name} vs {away_name}",
                "loc": loc_name or "",
                "time": time_str,
                "note": note,
                "ref": ref,
            })
    elif not team_matches:
        non_link_lines = [bl for bl in remaining if not TEAM_LINK_RE.match(bl)]
        free_text = " ".join(non_link_lines).strip()
        mention_lh = (TEAM_NAME in free_text) or (TEAM_ABBREV in free_text)
        is_tournament = re.search(r'tournament|playoff|final', free_text, re.IGNORECASE)
        if free_text and (mention_lh or is_tournament):
            events.append({
                "date": current_date.isoformat(),
                "sport": league_name,
                "type": sport_type(league_name),
                "match": free_text[:150],
                "loc": loc_name or "",
                "time": time_str,
                "note": "Tournament/placeholder - confirm details on QuickScores" if is_tournament else "",
                "ref": "",
            })
    return events


def parse_league_games(lines, league_name, league_id, season_label=""):
    events = []
    diag = {"date_lines": 0, "lh_link_lines": 0, "undated_years_inferred": 0}
    season_years = find_page_season_years(lines)
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]

        if _team_link_match(ln, league_id) and TEAM_NAME in ln:
            diag["lh_link_lines"] += 1

        if STOP_RE.search(ln):
            break  # everything after this is stats/footer, not schedule data

        if WEEK_RE.match(ln):
            i += 1
            continue

        if _is_game_header(lines, i):
            diag["date_lines"] += 1
            month_token, day_str, year_str = _date_line_match(lines[i + 1])
            if year_str is not None:
                current_date = resolve_explicit_date(month_token, day_str, year_str)
            else:
                current_date = resolve_undated_year(month_token, day_str, season_years)
                diag["undated_years_inferred"] += 1
            i += 2
            block = []
            while i < n and not _is_game_header(lines, i) and not WEEK_RE.match(lines[i]) and not STOP_RE.search(lines[i]):
                if _team_link_match(lines[i], league_id) and TEAM_NAME in lines[i]:
                    diag["lh_link_lines"] += 1
                block.append(lines[i])
                i += 1
            if current_date:
                events.extend(_process_block(current_date, block, league_name, league_id))
            continue

        i += 1
    return events, diag


# --- Main -----------------------------------------------------------------

def main():
    all_events = []
    total_date_lines = 0
    total_lh_link_lines = 0
    first_league_dump = None
    league_summaries = []   # (season, name, league_id, event_count_or_None, error_or_None)
    failed_leagues = []
    suspicious_dumps_printed = 0
    MAX_SUSPICIOUS_DUMPS = 3

    print(f"Fetching schedules list: {SCHEDULES_URL}")
    schedules_html = fetch(SCHEDULES_URL)
    lines = html_to_lines(schedules_html)
    leagues = discover_leagues(lines)
    print(f"Discovered {len(leagues)} league(s)")

    for lg in leagues:
        url = f"{BASE}/Orgs/ResultsDisplay.php?OrgDir={ORG}&LeagueID={lg['league_id']}"
        print(f"  -> {lg['season']} / {lg['name']}  ({url})")
        try:
            html = fetch(url)
            page_lines = html_to_lines(html)
            if first_league_dump is None:
                first_league_dump = page_lines[:150]
            events, diag = parse_league_games(page_lines, lg["name"], lg["league_id"], lg["season"])
        except Exception as e:
            # Wrapping fetch+parse together (not just fetch) means a problem
            # anywhere in processing this one league gets caught, logged, and
            # skipped -- rather than either crashing the whole run, or
            # (worse) silently missing that league's games with no trace of
            # why in the log.
            print(f"     ERROR processing this league, skipping it: {e}", file=sys.stderr)
            failed_leagues.append(f"{lg['season']} / {lg['name']} (LeagueID={lg['league_id']})")
            league_summaries.append((lg["season"], lg["name"], lg["league_id"], None, str(e)))
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        print(f"     found {len(events)} Lincoln Hall event(s)  "
              f"(saw {diag['date_lines']} date headers, {diag['lh_link_lines']} Lincoln Hall team-link lines, "
              f"{diag['undated_years_inferred']} dates with the year inferred)")

        # This league clearly HAS Lincoln Hall as a participating team (we
        # saw team-link lines naming them), but somehow zero actual games
        # were parsed out for them. That combination -- present as a team,
        # absent from the schedule -- means this specific league's page is
        # formatted in a way this parser doesn't handle, and it's worth
        # seeing exactly what that page looks like rather than guessing.
        if diag["lh_link_lines"] > 0 and len(events) == 0 and suspicious_dumps_printed < MAX_SUSPICIOUS_DUMPS:
            suspicious_dumps_printed += 1
            print(f"     SUSPICIOUS: Lincoln Hall appears as a team here but 0 games were "
                  f"parsed. Dumping this league's page content for diagnosis:", file=sys.stderr)
            print(f"--- SUSPICIOUS LEAGUE DUMP START ({lg['season']} / {lg['name']}, "
                  f"LeagueID={lg['league_id']}) ---", file=sys.stderr)
            for idx, ln in enumerate(page_lines[:200]):
                print(f"{idx:4}: {ln}", file=sys.stderr)
            print("--- SUSPICIOUS LEAGUE DUMP END ---", file=sys.stderr)

        all_events.extend(events)
        total_date_lines += diag["date_lines"]
        total_lh_link_lines += diag["lh_link_lines"]
        league_summaries.append((lg["season"], lg["name"], lg["league_id"], len(events), None))
        time.sleep(REQUEST_DELAY_SECONDS)

    all_events.sort(key=lambda e: (e["date"], e["time"]))

    by_date = {}
    for ev in all_events:
        by_date.setdefault(ev["date"], []).append({
            "sport": ev["sport"], "match": ev["match"], "loc": ev["loc"],
            "time": ev["time"], "note": ev["note"], "ref": ev["ref"],
            "type": ev["type"],
        })

    # Print a compact, easy-to-scan roll call of every league and how many
    # Lincoln Hall events it produced -- this is what would have made the
    # "Fall 2026 is missing" problem obvious immediately, instead of only
    # showing up as a gap on the rendered calendar.
    print("\n--- Per-league summary ---")
    for season, name, lid, count, err in league_summaries:
        if err is not None:
            print(f"  FAILED    {season} / {name} (LeagueID={lid}): {err}")
        else:
            print(f"  {count:>3} event(s)  {season} / {name} (LeagueID={lid})")

    problem = False

    if len(all_events) == 0:
        problem = True
        print(f"\nWARNING: found 0 events overall (saw {total_date_lines} date headers "
              f"and {total_lh_link_lines} Lincoln Hall team-link lines across all leagues).",
              file=sys.stderr)
        if total_lh_link_lines > 0 and total_date_lines == 0:
            print("DIAGNOSIS: Lincoln Hall team links were found, but not a single date "
                  "header matched. Here are the first 150 lines the parser actually saw "
                  "for the first league page, so this can be diagnosed precisely:",
                  file=sys.stderr)
            print("--- DIAGNOSTIC DUMP START ---", file=sys.stderr)
            for idx, ln in enumerate(first_league_dump or []):
                print(f"{idx:4}: {ln}", file=sys.stderr)
            print("--- DIAGNOSTIC DUMP END ---", file=sys.stderr)
            print("Copy everything between DUMP START and DUMP END (plus this message) "
                  "and give it to Claude.", file=sys.stderr)

    # A handful of isolated failures (one league timing out, say) is treated
    # as tolerable -- it'll very likely succeed on the next scheduled run a
    # few hours later, so publishing the rest of the fresh data now is
    # better than freezing the whole calendar over one hiccup. But a LARGE
    # share of leagues failing suggests something systemic (the site being
    # down, a bug affecting many pages at once) -- in that case, publishing
    # would risk exactly the "looks complete but is missing a season"
    # problem this update is meant to fix, so the existing good data is
    # protected instead.
    fail_threshold = max(3, len(leagues) * 0.10)
    if failed_leagues:
        print(f"\nWARNING: {len(failed_leagues)} of {len(leagues)} league(s) failed and "
              f"were skipped -- if any of them are Lincoln Hall's, their games are "
              f"missing from this run:", file=sys.stderr)
        for fl in failed_leagues:
            print(f"  - {fl}", file=sys.stderr)
        if len(failed_leagues) >= fail_threshold:
            problem = True
            print("This is a large enough share of leagues that something systemic may "
                  "be wrong (rather than an isolated hiccup).", file=sys.stderr)

    if problem and os.path.exists("events.json"):
        print("\nLeaving the existing events.json untouched rather than publishing a "
              "possibly-incomplete result.", file=sys.stderr)
        sys.exit(1)
    elif problem:
        print("\nNo existing events.json to preserve -- writing this result anyway so "
              "the site has something to show, but it may be incomplete.", file=sys.stderr)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "team": TEAM_NAME,
        "events": by_date,
    }

    with open("events.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote events.json: {len(all_events)} total events across {len(by_date)} dates.")


if __name__ == "__main__":
    main()
