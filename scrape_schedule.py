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
- If QuickScores redesigns their page layout, this script may need small
  tweaks. It's written defensively (it logs what it finds, and skips
  anything it can't confidently parse rather than guessing), so a layout
  change should show up as "0 events found" in the Action log rather than
  silently producing garbage.
- One known gap: a schedule note that isn't attached to a specific weekday
  date (e.g. a vague "opponent still TBD" tournament blurb with no date of
  its own) won't be picked up. Anything with an actual date will be.
"""

import json
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
REQUEST_DELAY_SECONDS = 1.5     # be polite -- don't hammer their server

# --- Regex helpers -------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

DATE_RE = re.compile(r'^-?\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+([A-Za-z]{3})\s+(\d{1,2})$')
WEEK_RE = re.compile(r'^Week\s+\d+$', re.IGNORECASE)
STOP_RE = re.compile(r'Show Schedule Analysis', re.IGNORECASE)
SEASON_HEADER_RE = re.compile(r'^(Spring|Summer|Fall|Winter)\s+(\d{4})(?:-(\d{2}))?\s+(.+)$')
LEAGUE_LINK_RE = re.compile(r'^\[([^\]]+)\]\(([^)]*ResultsDisplay\.php\?OrgDir=' + ORG + r'&LeagueID=(\d+))\)$')
TEAM_LINK_RE = re.compile(r'^\[([^\]]+)\]\(([^)]*ResultsDisplay\.php\?OrgDir=' + ORG + r'&LeagueID=(\d+)&TeamID=(\d+)[^)]*)\)$')
TIME_RE = re.compile(r'^(\d{1,2}:\d{2}\s*[AP]M)\b\s*(.*)$', re.IGNORECASE)
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


# --- Fetching & flattening ------------------------------------------------

def fetch(url):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


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

def season_years(season_label):
    m = re.search(r'(\d{4})(?:-(\d{2}))?', season_label or "")
    if not m:
        today = date.today()
        return today.year, today.year
    y1 = int(m.group(1))
    y2 = int(str(y1)[:2] + m.group(2)) if m.group(2) else y1
    return y1, y2


def resolve_date(mon_abbr, day, season_label):
    y1, y2 = season_years(season_label)
    month = MONTHS.get(mon_abbr[:3].title())
    if month is None:
        return None
    # Winter seasons like "2026-27" span two calendar years: Aug-Dec use the
    # first year, Jan-Jun use the second.
    year = y1 if (y1 == y2 or month >= 7) else y2
    try:
        return date(year, month, int(day))
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


# --- Game/event parsing --------------------------------------------------

def _process_block(current_date, block, league_name):
    """Turn the lines belonging to a single date into 0+ event dicts."""
    if not block:
        return []

    time_str = ""
    loc_name = ""
    remaining = block
    m = TIME_RE.match(block[0])
    if m:
        time_str = m.group(1).upper()
        rest = m.group(2).strip()
        lm = re.match(r'^\[([^\]]+)\]\(', rest)
        loc_name = lm.group(1) if lm else rest
        remaining = block[1:]

    team_matches = [TEAM_LINK_RE.match(bl) for bl in remaining]
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
                if bl and not TEAM_LINK_RE.match(bl) and not SCORE_RE.match(bl) and _looks_like_ref_name(bl):
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
        free_text = " ".join(remaining).strip()
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


def parse_league_games(lines, league_name, season_label):
    events = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]

        if STOP_RE.search(ln):
            break  # everything after this is stats/footer, not schedule data

        if WEEK_RE.match(ln):
            i += 1
            continue

        m = DATE_RE.match(ln)
        if m:
            current_date = resolve_date(m.group(2), m.group(3), season_label)
            i += 1
            block = []
            while i < n and not DATE_RE.match(lines[i]) and not WEEK_RE.match(lines[i]) and not STOP_RE.search(lines[i]):
                block.append(lines[i])
                i += 1
            if current_date:
                events.extend(_process_block(current_date, block, league_name))
            continue

        i += 1
    return events


# --- Main -----------------------------------------------------------------

def main():
    print(f"Fetching schedules list: {SCHEDULES_URL}")
    schedules_html = fetch(SCHEDULES_URL)
    lines = html_to_lines(schedules_html)
    leagues = discover_leagues(lines)
    print(f"Discovered {len(leagues)} league(s)")

    all_events = []
    for lg in leagues:
        url = f"{BASE}/Orgs/ResultsDisplay.php?OrgDir={ORG}&LeagueID={lg['league_id']}"
        print(f"  -> {lg['season']} / {lg['name']}  ({url})")
        try:
            html = fetch(url)
        except Exception as e:
            print(f"     ERROR fetching league {lg['league_id']}: {e}", file=sys.stderr)
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        page_lines = html_to_lines(html)
        events = parse_league_games(page_lines, lg["name"], lg["season"])
        print(f"     found {len(events)} Lincoln Hall event(s)")
        all_events.extend(events)
        time.sleep(REQUEST_DELAY_SECONDS)

    all_events.sort(key=lambda e: (e["date"], e["time"]))

    by_date = {}
    for ev in all_events:
        by_date.setdefault(ev["date"], []).append({
            "sport": ev["sport"], "match": ev["match"], "loc": ev["loc"],
            "time": ev["time"], "note": ev["note"], "ref": ev["ref"],
            "type": ev["type"],
        })

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "team": TEAM_NAME,
        "events": by_date,
    }

    with open("events.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWrote events.json: {len(all_events)} total events across {len(by_date)} dates.")

    if len(all_events) == 0:
        print("WARNING: zero events found. QuickScores may have changed its "
              "page layout, or the season currently has no games yet. Check "
              "the log above.", file=sys.stderr)


if __name__ == "__main__":
    main()
