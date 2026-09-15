import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup


ORG = "little9"
BASE = "https://www.quickscores.com"
SCHEDULES_URL = f"{BASE}/Orgs/Schedules.php?OrgDir={ORG}"
TEAM_NAME = "Lincoln Hall"      
TEAM_ABBREV = "L.H."            
HEADERS = {
    "User-Agent": "LincolnHallCalendarBot/1.0 (parent-run schedule sync)"
}
REQUEST_DELAY_SECONDS = 1.5    


MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

DATE_RE = re.compile(r'^-?\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,?\s+([A-Za-z]{3,9})\.?,?\s+(\d{1,2})(?:st|nd|rd|th)?\.?,?\s*$', re.IGNORECASE)
WEEK_RE = re.compile(r'^Week\s*\d+\s*:?$', re.IGNORECASE)
STOP_RE = re.compile(r'Show Schedule Analysis|Schedule Analysis|Time Slot Distribution', re.IGNORECASE)
SEASON_HEADER_RE = re.compile(r'^(Spring|Summer|Fall|Winter)\s+(\d{4})(?:-(\d{2}))?\s+(.+)$', re.IGNORECASE)
LEAGUE_LINK_RE = re.compile(r'^\[([^\]]+)\]\(([^)]*ResultsDisplay\.php\?OrgDir=' + ORG + r'&LeagueID=(\d+))\)$')
TEAM_LINK_RE = re.compile(r'^\[([^\]]+)\]\(([^)]*ResultsDisplay\.php\?OrgDir=' + ORG + r'&LeagueID=(\d+)&TeamID=(\d+)[^)]*)\)$')
TIME_RE = re.compile(r'^(\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)\s*(.*)$')
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



def fetch_all(page, url):
    page.goto(url, wait_until="networkidle", timeout=45000)
    return page.content()


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



def season_years(season_label):
    m = re.search(r'(\d{4})(?:-(\d{2}))?', season_label or "")
    if not m:
        today = date.today()
        return today.year, today.year
    y1 = int(m.group(1))
    y2 = int(str(y1)[:2] + m.group(2)) if m.group(2) else y1
    return y1, y2


def resolve_date(mon_token, day, season_label):
    y1, y2 = season_years(season_label)
    key = mon_token[:3].title()
    month = MONTHS.get(key)
    if month is None:
        return None
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
    """Turn the lines belonging to a single date into 0+ event dicts."""
    if not block:
        return []

    time_str = ""
    loc_name = ""
    remaining = block
    m = TIME_RE.match(block[0])
    if m:
        time_str = normalize_time(m.group(1))
        rest = re.sub(r'^[\s.,]+', '', m.group(2).strip())
        lm = re.match(r'^\[([^\]]+)\]\(', rest)
        loc_name = lm.group(1) if lm else rest
        remaining = block[1:]

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


def parse_league_games(lines, league_name, league_id, season_label):
    events = []
    diag = {"date_lines": 0, "lh_link_lines": 0}
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]

        if _team_link_match(ln, league_id) and TEAM_NAME in ln:
            diag["lh_link_lines"] += 1

        if STOP_RE.search(ln):
            break  

        if WEEK_RE.match(ln):
            i += 1
            continue

        m = DATE_RE.match(ln)
        if m:
            diag["date_lines"] += 1
            current_date = resolve_date(m.group(2), m.group(3), season_label)
            i += 1
            block = []
            while i < n and not DATE_RE.match(lines[i]) and not WEEK_RE.match(lines[i]) and not STOP_RE.search(lines[i]):
                if _team_link_match(lines[i], league_id) and TEAM_NAME in lines[i]:
                    diag["lh_link_lines"] += 1
                block.append(lines[i])
                i += 1
            if current_date:
                events.extend(_process_block(current_date, block, league_name, league_id))
            continue

        i += 1
    return events, diag



def main():
    all_events = []
    total_date_lines = 0
    total_lh_link_lines = 0
    first_league_dump = None

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])

        print(f"Fetching schedules list: {SCHEDULES_URL}")
        schedules_html = fetch_all(page, SCHEDULES_URL)
        lines = html_to_lines(schedules_html)
        leagues = discover_leagues(lines)
        print(f"Discovered {len(leagues)} league(s)")

        for lg in leagues:
            url = f"{BASE}/Orgs/ResultsDisplay.php?OrgDir={ORG}&LeagueID={lg['league_id']}"
            print(f"  -> {lg['season']} / {lg['name']}  ({url})")
            try:
                html = fetch_all(page, url)
            except Exception as e:
                print(f"     ERROR fetching league {lg['league_id']}: {e}", file=sys.stderr)
                time.sleep(REQUEST_DELAY_SECONDS)
                continue

            page_lines = html_to_lines(html)
            if first_league_dump is None:
                first_league_dump = page_lines[:150]
            events, diag = parse_league_games(page_lines, lg["name"], lg["league_id"], lg["season"])
            print(f"     found {len(events)} Lincoln Hall event(s)  "
                  f"(saw {diag['date_lines']} date headers, {diag['lh_link_lines']} Lincoln Hall team-link lines)")
            all_events.extend(events)
            total_date_lines += diag["date_lines"]
            total_lh_link_lines += diag["lh_link_lines"]
            time.sleep(REQUEST_DELAY_SECONDS)

        browser.close()

    all_events.sort(key=lambda e: (e["date"], e["time"]))

    by_date = {}
    for ev in all_events:
        by_date.setdefault(ev["date"], []).append({
            "sport": ev["sport"], "match": ev["match"], "loc": ev["loc"],
            "time": ev["time"], "note": ev["note"], "ref": ev["ref"],
            "type": ev["type"],
        })

    if len(all_events) == 0:
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
        if os.path.exists("events.json"):
            print("Leaving the existing events.json untouched rather than overwriting "
                  "it with an empty result.", file=sys.stderr)
            sys.exit(1)
        else:
            print("No existing events.json to preserve -- writing an empty one so the "
                  "site at least has valid JSON to load.", file=sys.stderr)

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
