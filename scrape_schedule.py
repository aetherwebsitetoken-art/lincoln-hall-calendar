#!/usr/bin/env python3
"""
Lincoln Hall Athletics -- QuickScores schedule scraper
======================================================

What this does
---------------
1. Fetches the Little Nine Conference "Schedules List" page, which lists
   EVERY league (every sport, season and level). Nothing is hardcoded, so a
   brand-new team (e.g. Boys Volleyball after tryouts) is picked up
   automatically as soon as QuickScores creates a page for it.
2. For each league, gets that league's games -- preferring QuickScores' own
   calendar feed (DownloadSchedule.php), and falling back to parsing the
   HTML schedule page if the feed isn't usable.
3. Keeps only games involving Lincoln Hall.
4. Writes everything to events.json next to this script.

Why the calendar feed is preferred
-----------------------------------
The HTML schedule page prints dates as human-readable text, and QuickScores
omits the year whenever it's the current year ("Sep 8" now, but
"Apr 6, 2027" for other years). Parsing that correctly is fiddly and was the
source of repeated bugs where an entire season silently vanished.

The calendar feed is structured data: every event carries a full, explicit
date (DTSTART:20260908T161500). There is no year to infer and no layout to
guess at, so it can't fail in that particular way. The HTML parser is kept
as a fallback for any league whose feed is missing or empty.

Run it yourself with:
    pip install requests beautifulsoup4
    python3 scrape_schedule.py

Honest limitations
-------------------
- This reads QuickScores' public pages and public calendar feed; it uses no
  private API and identifies itself honestly in its User-Agent.
- If a game has no usable date in either source, it's skipped rather than
  guessed at.
- Every run prints a per-league summary (and which source it used), so a
  problem shows up in the log rather than as a silent gap on the calendar.
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# --- Configuration -----------------------------------------------------

ORG = "little9"
BASE = "https://www.quickscores.com"
SCHEDULES_URL = f"{BASE}/Orgs/Schedules.php?OrgDir={ORG}"
TEAM_NAME = "Lincoln Hall"
TEAM_ABBREV = "L.H."
HEADERS = {
    "User-Agent": "LincolnHallCalendarBot/2.0 (parent-run school schedule sync)"
}
REQUEST_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 20
MAX_FETCH_ATTEMPTS = 3

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

# --- Networking -----------------------------------------------------------


def fetch(url):
    """GET a URL, retrying on timeouts/transient errors before giving up."""
    last_error = None
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < MAX_FETCH_ATTEMPTS:
                print(f"       (attempt {attempt} failed: {e} -- retrying)", file=sys.stderr)
                time.sleep(REQUEST_DELAY_SECONDS)
    raise last_error


def html_to_lines(html):
    """Flatten HTML to ordered text lines, preserving links as
    [text](href) so link targets survive."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for a in soup.find_all("a"):
        href = a.get("href", "") or ""
        text = a.get_text(" ", strip=True)
        a.replace_with(f"[{text}]({href})")
    return [ln.strip() for ln in soup.get_text("\n").split("\n") if ln.strip()]


# --- League discovery -----------------------------------------------------

LEAGUE_LINK_RE = re.compile(
    r'^\[([^\]]+)\]\(([^)]*ResultsDisplay\.php\?OrgDir=' + ORG + r'&LeagueID=(\d+))\)$')
SEASON_HEADER_RE = re.compile(
    r'^(Spring|Summer|Fall|Winter)\s+(\d{4})(?:-(\d{2}))?\s*-?\s*(.*)$', re.IGNORECASE)


def discover_leagues(lines):
    """Every league on the schedules list, tagged with its season heading."""
    leagues, seen, current_season = [], set(), ""
    for ln in lines:
        m = SEASON_HEADER_RE.match(ln)
        if m:
            current_season = ln
            continue
        m = LEAGUE_LINK_RE.match(ln)
        if m:
            league_id = m.group(3)
            if league_id in seen:
                continue
            seen.add(league_id)
            leagues.append({
                "league_id": league_id,
                "name": m.group(1).strip(),
                "season": current_season,
            })
    return leagues


def sport_type(league_name):
    n = (league_name or "").lower()
    if "soccer" in n:
        return "soccer"
    if "volleyball" in n:
        return "volleyball"
    if "basketball" in n:
        return "basketball"
    return "other"


def season_years(text):
    """Calendar year(s) a season heading spans, e.g. 'Winter 2026-27'."""
    m = re.search(r'(\d{4})(?:-(\d{2}))?', text or "")
    if not m:
        return None
    y1 = int(m.group(1))
    y2 = int(str(y1)[:2] + m.group(2)) if m.group(2) else y1
    return y1, y2


# =========================================================================
# STRATEGY 1 (preferred): QuickScores' calendar feed
# =========================================================================

def unfold_ics(text):
    """iCalendar wraps long lines by starting continuations with a space or
    tab. Join them back together before parsing."""
    out = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def ics_unescape(value):
    return (value.replace("\\n", " ").replace("\\N", " ")
                 .replace("\\,", ",").replace("\\;", ";")
                 .replace("\\\\", "\\").strip())


DTSTART_RE = re.compile(
    r'^DTSTART([^:]*):(\d{8})(?:T(\d{2})(\d{2})(\d{2}))?(Z?)', re.IGNORECASE)
DTEND_RE = re.compile(
    r'^DTEND([^:]*):(\d{8})(?:T(\d{2})(\d{2})(\d{2}))?(Z?)', re.IGNORECASE)
MAX_EVENT_SPAN_DAYS = 30   # guard against a runaway multi-year entry

# Calendar feeds commonly publish times in UTC (a trailing "Z"). Reading the
# raw digits then shows a 5:00 PM event as 10:00 PM -- and can roll an
# evening event onto the following day -- so UTC stamps are converted to
# local school time before anything else looks at them.
LOCAL_TZ_NAME = "America/Chicago"

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:          # pragma: no cover - only if tzdata is unavailable
    LOCAL_TZ = None
    print(f"WARNING: timezone data for {LOCAL_TZ_NAME} unavailable; "
          f"UTC times will not be converted.", file=sys.stderr)


def ics_datetime(params, ymd, hh, mm, ss, zulu):
    """Turn an iCal DTSTART/DTEND into (date, 'H:MM AM/PM' or '').

    - VALUE=DATE or no time part  -> an all-day entry (no time)
    - a trailing Z                -> UTC, converted to local time
    - a TZID= parameter or bare   -> already local, used as-is
    """
    try:
        y, mo, d = int(ymd[0:4]), int(ymd[4:6]), int(ymd[6:8])
    except ValueError:
        return None, ""

    if hh is None or "VALUE=DATE" in (params or "").upper():
        return date(y, mo, d), ""          # all-day

    hour, minute = int(hh), int(mm or 0)
    if zulu and LOCAL_TZ is not None:
        dt = datetime(y, mo, d, hour, minute, int(ss or 0), tzinfo=timezone.utc)
        dt = dt.astimezone(LOCAL_TZ)       # date may shift here -- intentional
        day, hour, minute = dt.date(), dt.hour, dt.minute
    else:
        day = date(y, mo, d)

    # Midnight with no offset is how many feeds encode an all-day event.
    if hour == 0 and minute == 0 and not zulu:
        return day, ""

    suffix = "AM" if hour < 12 else "PM"
    return day, f"{hour % 12 or 12}:{minute:02d} {suffix}"


def parse_ics_events(text):
    """Pull VEVENTs out of an iCalendar feed. Returns dicts with an exact
    date (no inference), optional time, summary and location."""
    events, cur, in_event = [], None, False
    for line in unfold_ics(text):
        upper = line.upper()
        if upper.startswith("BEGIN:VEVENT"):
            in_event, cur = True, {}
            continue
        if upper.startswith("END:VEVENT"):
            if in_event and cur and cur.get("date"):
                events.append(cur)
            in_event, cur = False, None
            continue
        if not in_event or cur is None:
            continue

        m = DTSTART_RE.match(line)
        if m:
            day, tm = ics_datetime(m.group(1), m.group(2), m.group(3),
                                   m.group(4), m.group(5), m.group(6))
            if day:
                cur["date"] = day
            if tm:
                cur["time"] = tm
            continue

        m = DTEND_RE.match(line)
        if m:
            day, _ = ics_datetime(m.group(1), m.group(2), m.group(3),
                                  m.group(4), m.group(5), m.group(6))
            if day:
                cur["end"] = day
            continue

        for field, key in (("SUMMARY", "summary"), ("LOCATION", "location"),
                           ("DESCRIPTION", "description")):
            if upper.startswith(field + ":") or upper.startswith(field + ";"):
                _, _, value = line.partition(":")
                cur[key] = ics_unescape(value)
                break
    return events


def split_matchup(summary):
    """Best-effort home/away split of a feed's event title. QuickScores
    writes these a few different ways, so several separators are tried;
    if none fit, the original text is kept as-is rather than mangled."""
    if not summary:
        return None, None
    for sep, away_first in ((" at ", True), (" @ ", True),
                            (" vs. ", False), (" vs ", False), (" v. ", False)):
        if sep in summary:
            left, _, right = summary.partition(sep)
            left, right = left.strip(), right.strip()
            # "A at B" means B hosts; "A vs B" means A hosts.
            return (right, left) if away_first else (left, right)
    return None, None


def games_from_feed(league, diag):
    """Try the league's calendar feed. Returns [] if it isn't usable, so the
    caller can fall back to HTML."""
    url = f"{BASE}/Orgs/DownloadSchedule.php?OrgDir={ORG}&LeagueID={league['league_id']}"
    try:
        body = fetch(url)
    except Exception as e:
        diag["feed_error"] = str(e)
        return []

    if "BEGIN:VCALENDAR" not in body.upper():
        # Some installs return a small HTML page linking to the real .ics.
        m = re.search(r'href=["\']([^"\']+\.ics[^"\']*)["\']', body, re.IGNORECASE)
        if not m:
            diag["feed_error"] = "response was not a calendar feed"
            return []
        link = m.group(1)
        if link.startswith("/"):
            link = BASE + link
        elif not link.startswith("http"):
            link = f"{BASE}/Orgs/{link}"
        try:
            body = fetch(link)
        except Exception as e:
            diag["feed_error"] = f"linked .ics failed: {e}"
            return []
        if "BEGIN:VCALENDAR" not in body.upper():
            diag["feed_error"] = "linked file was not a calendar feed"
            return []

    raw_events = parse_ics_events(body)
    diag["feed_events_total"] = len(raw_events)

    out = []
    for ev in raw_events:
        blob = " ".join(filter(None, [ev.get("summary"), ev.get("location"),
                                      ev.get("description")]))
        if TEAM_NAME not in blob and TEAM_ABBREV not in blob:
            continue
        summary = ev.get("summary", "").strip()
        home, away = split_matchup(summary)
        match_text = f"{home} vs {away}" if home and away else (summary or "Game")
        out.append({
            "date": ev["date"].isoformat(),
            "sport": league["name"],
            "type": sport_type(league["name"]),
            "match": match_text,
            "loc": ev.get("location", "") or "",
            "time": ev.get("time", "") or "",
            "note": "",
            "ref": "",
            "_source": "feed",
        })
    return out


# =========================================================================
# DISTRICT-WIDE EVENTS (Lincolnwood SD74)
# =========================================================================
#
# The district's calendar page publishes a public iCal feed intended for
# calendar apps to subscribe to -- the same structured format used above,
# so the same parser handles it. This covers concerts, curriculum nights,
# board meetings, picture day, spirit days and so on.

DISTRICT_ICS_URL = (
    "https://calendar.google.com/calendar/ical/"
    "c_434d10cea58b170a51434a2f6e2b051def420ade92c062309647605353d7c139"
    "%40group.calendar.google.com/public/basic.ics"
)
ACADEMIC_FILE = "academic_calendar.json"

# Titles matching these are treated as no-school/half-day entries so the
# page can show them with their own icon.
NOSCHOOL_RE = re.compile(
    r'no school|district closed|holiday|break|institute day|non-attendance',
    re.IGNORECASE)
HALFDAY_RE = re.compile(r'am[- ]only|half day|early dismissal|am only', re.IGNORECASE)

# Some entries on the district feed are really athletics -- a "RED OUT for
# Girls Varsity Volleyball" is a game, not a district event -- so they're
# reclassified into the sport they belong to. The sport name in the title
# wins; a spirit-day phrase alone still counts as sports but without a
# specific sport.
SPORT_IN_TITLE = (
    ("volleyball", "volleyball"),
    ("soccer", "soccer"),
    ("basketball", "basketball"),
)
SPIRIT_RE = re.compile(
    r'red\s*out|pink\s*out|white\s*out|black\s*out|blackout|'
    r'senior night|homecoming', re.IGNORECASE)


def classify_district(title):
    """(type, category) for a district-feed entry."""
    low = title.lower()
    for word, sport in SPORT_IN_TITLE:
        if word in low:
            return sport, "sports"
    if SPIRIT_RE.search(title):
        return "other", "sports"
    # Half days and no-school days live alongside district events; they just
    # carry their own icon so they still stand out in the list.
    if HALFDAY_RE.search(title):
        return "halfday", "district"
    if NOSCHOOL_RE.search(title):
        return "noschool", "district"
    return "district", "district"


def expand_span(ev):
    """All dates an event covers. All-day iCal events use an EXCLUSIVE end
    date, so a one-day event has end = start + 1; subtract it back off.
    Multi-day entries (e.g. 'Safety Week') become one entry per day."""
    start = ev["date"]
    end = ev.get("end")
    if not end or end <= start:
        return [start]
    if not ev.get("time"):        # all-day -> end is exclusive
        end = end - timedelta(days=1)
    if end <= start:
        return [start]
    span = (end - start).days
    if span > MAX_EVENT_SPAN_DAYS:
        return [start]
    return [start + timedelta(days=i) for i in range(span + 1)]


def district_events():
    """Every event on the district calendar feed."""
    print(f"\nFetching district calendar feed")
    try:
        body = fetch(DISTRICT_ICS_URL)
    except Exception as e:
        print(f"  ERROR: could not fetch district feed: {e}", file=sys.stderr)
        return None, 0
    if "BEGIN:VCALENDAR" not in body.upper():
        print("  ERROR: district feed was not a calendar file", file=sys.stderr)
        return None, 0

    raw = parse_ics_events(body)
    out = []
    for ev in raw:
        title = (ev.get("summary") or "").strip()
        if not title:
            continue
        kind, cat = classify_district(title)
        if cat == "sports":
            label = "Athletics"
        elif kind == "district":
            label = "District Event"
        else:
            label = "District Calendar"
        for day in expand_span(ev):
            out.append({
                "date": day.isoformat(),
                "sport": label,
                "type": kind,
                "match": title,
                "loc": ev.get("location", "") or "",
                "time": ev.get("time", "") or "",
                "note": "",
                "ref": "",
                "cat": cat,
            })
    print(f"  {len(raw)} feed entries -> {len(out)} dated events")
    return out, len(raw)


def academic_events():
    """The school-year calendar transcribed from the district's PDF."""
    if not os.path.exists(ACADEMIC_FILE):
        print(f"\n(no {ACADEMIC_FILE} -- skipping academic calendar)")
        return []
    try:
        data = json.load(open(ACADEMIC_FILE, encoding="utf-8"))
    except Exception as e:
        print(f"\nERROR reading {ACADEMIC_FILE}: {e}", file=sys.stderr)
        return []
    out = []
    for day, evs in (data.get("events") or {}).items():
        for ev in evs:
            item = dict(ev)
            item["date"] = day
            item.setdefault("cat", "school")
            out.append(item)
    print(f"\nAcademic calendar: {len(out)} events ({data.get('source','')})")
    return out


# =========================================================================
# STRATEGY 2 (fallback): parse the HTML schedule page
# =========================================================================

WEEKDAY_RE = re.compile(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,?$', re.IGNORECASE)
FULLDATE_RE = re.compile(r'^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,\s*(\d{4})$', re.IGNORECASE)
NOYEAR_DATE_RE = re.compile(r'^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?$', re.IGNORECASE)
PAGE_SEASON_RE = re.compile(r'^(Spring|Summer|Fall|Winter)\s+(\d{4})(?:-(\d{2}))?\s*-?\s*(.*)$', re.IGNORECASE)
WEEK_RE = re.compile(r'^Week\s*\d+\s*:?$', re.IGNORECASE)
STOP_RE = re.compile(r'Show Schedule Analysis|Schedule Analysis|Time Slot Distribution', re.IGNORECASE)
TEAM_LINK_RE = re.compile(
    r'^\[([^\]]+)\]\(([^)]*ResultsDisplay\.php\?OrgDir=' + ORG + r'&LeagueID=(\d+)&TeamID=(\d+)[^)]*)\)$')
LOC_LINK_RE = re.compile(r'^\[([^\]]+)\]\(([^)]*LocationDetails\.php[^)]*)\)$')
TIME_RE = re.compile(r'^(\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)\.?$')
SCORE_RE = re.compile(r'^\d+$')


def _is_month_token(token):
    return token[:3].title() in MONTHS


def _date_line_match(line):
    """(month, day, year|None) if this line is a game date, else None."""
    m = FULLDATE_RE.match(line)
    if m and _is_month_token(m.group(1)):
        return m.group(1), m.group(2), m.group(3)
    m = NOYEAR_DATE_RE.match(line)
    if m and _is_month_token(m.group(1)):
        # The month check matters -- without it, "Week 1" matches this shape.
        return m.group(1), m.group(2), None
    return None


def _is_game_header(lines, i):
    if i + 1 >= len(lines):
        return False
    return bool(WEEKDAY_RE.match(lines[i])) and _date_line_match(lines[i + 1]) is not None


def find_page_season_years(lines):
    for ln in lines[:80]:
        if PAGE_SEASON_RE.match(ln):
            return season_years(ln)
    return None


def build_date(month_token, day_str, year_str, years):
    month = MONTHS.get(month_token[:3].title())
    if month is None:
        return None
    if year_str is not None:
        year = int(year_str)
    elif years:
        y1, y2 = years
        # A season like "Winter 2026-27": Aug-Dec is the first year,
        # Jan-Jul the second.
        year = y1 if (y1 == y2 or month >= 7) else y2
    else:
        year = date.today().year
    try:
        return date(year, month, int(day_str))
    except ValueError:
        return None


def _team_link(line, league_id):
    m = TEAM_LINK_RE.match(line)
    return m if (m and m.group(3) == str(league_id)) else None


def _looks_like_official(text):
    if not text or any(c.isdigit() for c in text) or len(text.split()) > 3:
        return False
    return not text.lower().startswith(("no ", "bye", "tournament", "play", "tbd", "final"))


def normalize_time(raw):
    m = re.match(r'^(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\.?$', raw.strip())
    return f"{m.group(1)}:{m.group(2)} {m.group(3).upper()}M" if m else raw.strip().upper()


def _block_to_events(day, block, league, league_id):
    if not block:
        return []
    idx, time_str, loc = 0, "", ""

    if idx < len(block):
        m = TIME_RE.match(block[idx])
        if m:
            time_str = normalize_time(m.group(1))
            idx += 1

    if idx < len(block) and not _team_link(block[idx], league_id):
        lm = LOC_LINK_RE.match(block[idx])
        if lm:
            loc = lm.group(1)
            idx += 1
        elif block[idx] and any(_team_link(b, league_id) for b in block[idx:]):
            loc = block[idx]
            idx += 1

    rest = block[idx:]
    teams = [t for t in (_team_link(b, league_id) for b in rest) if t]

    if len(teams) >= 2:
        home, away = teams[0].group(1).strip(), teams[1].group(1).strip()
        if TEAM_NAME not in (home, away):
            return []
        scores = [b for b in rest if SCORE_RE.match(b)]
        note = ""
        if len(scores) >= 2:
            hs, as_ = scores[0], scores[1]
            lh, opp = (hs, as_) if home == TEAM_NAME else (as_, hs)
            try:
                result = "Win" if int(lh) > int(opp) else ("Loss" if int(lh) < int(opp) else "Tie")
                note = f"{lh}-{opp} ({result})"
            except ValueError:
                pass
        official = ""
        for b in reversed(rest):
            if not _team_link(b, league_id) and not SCORE_RE.match(b) and _looks_like_official(b):
                official = b
                break
        return [{
            "date": day.isoformat(), "sport": league["name"],
            "type": sport_type(league["name"]), "match": f"{home} vs {away}",
            "loc": loc, "time": time_str, "note": note, "ref": official,
            "_source": "html",
        }]

    if not teams:
        text = " ".join(b for b in rest if not TEAM_LINK_RE.match(b)).strip()
        mentions_lh = TEAM_NAME in text or TEAM_ABBREV in text
        tourney = re.search(r'tournament|tourney|playoff|final', text, re.IGNORECASE)
        if text and (mentions_lh or tourney):
            return [{
                "date": day.isoformat(), "sport": league["name"],
                "type": sport_type(league["name"]), "match": text[:150],
                "loc": loc, "time": time_str,
                "note": "Tournament/placeholder - confirm on QuickScores" if tourney else "",
                "ref": "", "_source": "html",
            }]
    return []


def games_from_html(league, diag):
    url = f"{BASE}/Orgs/ResultsDisplay.php?OrgDir={ORG}&LeagueID={league['league_id']}"
    lines = html_to_lines(fetch(url))
    years = find_page_season_years(lines) or season_years(league.get("season", ""))
    league_id = league["league_id"]

    events, i, n = [], 0, len(lines)
    while i < n:
        if STOP_RE.search(lines[i]):
            break
        if WEEK_RE.match(lines[i]):
            i += 1
            continue
        if _is_game_header(lines, i):
            diag["html_date_headers"] += 1
            mo, dy, yr = _date_line_match(lines[i + 1])
            if yr is None:
                diag["html_years_inferred"] += 1
            day = build_date(mo, dy, yr, years)
            i += 2
            block = []
            while i < n and not _is_game_header(lines, i) \
                    and not WEEK_RE.match(lines[i]) and not STOP_RE.search(lines[i]):
                block.append(lines[i])
                i += 1
            if day:
                events.extend(_block_to_events(day, block, league, league_id))
            continue
        i += 1
    return events


# --- De-duplication ---------------------------------------------------------

STATUS_TYPES = ("noschool", "halfday")
STATUS_LABELS = {"noschool": "No School", "halfday": "Half Day"}
HALFDAY_TITLE = "Half Day - AM-Only Student Attendance"


def _norm_title(s):
    """Letters and digits only, so dash/spacing/case differences don't matter."""
    return re.sub(r'[^a-z0-9]+', '', (s or "").lower())


def dedupe(events):
    """Collapse duplicates that arrive from more than one source.

    Rules, in order:
      1. Same date + same title (ignoring punctuation/case) -> one event,
         regardless of which category each source assigned.
      2. At most ONE no-school entry and ONE half-day entry per date, even if
         the sources word them differently ("Labor Day - District Closed" vs
         "Labor Day - No School"). The more specific title is kept.
      3. Within district events on the same date, a title fully contained in
         another ("Columbus Day" inside "Columbus Day - No School") is the
         same event and is dropped in favour of the fuller one.
    When copies collide, a timed version beats an all-day one.
    """
    def key(ev):
        if ev.get("type") in STATUS_TYPES:
            return (ev["date"], "status", ev["type"])
        return (ev["date"], _norm_title(ev.get("match")))

    def better(new, cur):
        if ev_is_status(new):
            return len(new.get("match") or "") > len(cur.get("match") or "")
        return bool(new.get("time")) and not cur.get("time")

    def ev_is_status(ev):
        return ev.get("type") in STATUS_TYPES

    best, order = {}, []
    for ev in sorted(events, key=lambda e: (e["date"], e.get("time", "") == "", e.get("time", ""))):
        k = key(ev)
        if k not in best:
            best[k] = ev
            order.append(k)
        elif better(ev, best[k]):
            best[k] = ev

    kept = [best[k] for k in order]

    # Rule 3: drop district titles swallowed by a fuller one on the same day.
    by_day = {}
    for ev in kept:
        by_day.setdefault(ev["date"], []).append(ev)
    result = []
    for day, evs in by_day.items():
        district = [e for e in evs if e.get("cat") == "district"]
        for ev in evs:
            if ev.get("cat") == "district":
                me = _norm_title(ev.get("match"))
                if len(me) >= 5 and any(
                        other is not ev and me != _norm_title(other.get("match"))
                        and me in _norm_title(other.get("match"))
                        for other in district):
                    continue
            result.append({k: v for k, v in ev.items() if not k.startswith("_")})
    return sorted(result, key=lambda e: (e["date"], e.get("time", "") == "", e.get("time", "")))


# --- Keeping index.html in sync -------------------------------------------
#
# The calendar page carries two things the scraper can fill in automatically,
# so nothing has to be hand-edited after each run:
#
#   1. EVENTS_JSON_URL -- pointed at this repo's live events.json on jsDelivr,
#      so a copy of index.html pasted straight into Google Sites still picks
#      up live updates (a relative path has nothing to resolve against there).
#   2. FALLBACK -- refreshed with the data just scraped, so the built-in
#      "saved schedule" shown when the live fetch fails is current and
#      complete (including sports added since, like basketball) rather than
#      a stale hand-written snapshot.

# index.html is the TEMPLATE (the design). embed.html is what goes into
# Google Sites. Only this script writes embed.html, so the copy that gets
# pasted is always filled in -- uploading a new template can never leave
# Google Sites showing stale or saved-only data.
TEMPLATE_FILE = "index.html"
EMBED_FILE = "embed.html"
URL_REGION_RE = re.compile(r'(/\*URL_START\*/).*?(/\*URL_END\*/)', re.DOTALL)
FALLBACK_REGION_RE = re.compile(r'(/\*FALLBACK_START\*/).*?(/\*FALLBACK_END\*/)', re.DOTALL)

# How much history to bake into the offline copy. Everything upcoming is
# always included; older games are trimmed so the file stays small enough to
# paste comfortably into Google Sites.
FALLBACK_HISTORY_DAYS = 120


def build_fallback(by_date):
    cutoff = (date.today() - timedelta(days=FALLBACK_HISTORY_DAYS)).isoformat()
    return {d: evs for d, evs in sorted(by_date.items()) if d >= cutoff}


def build_embed(by_date):
    """Write embed.html: the template with the live-data link and a saved
    copy of the schedule filled in. The template itself is never modified."""
    if not os.path.exists(TEMPLATE_FILE):
        print(f"(no {TEMPLATE_FILE} beside the script -- can't build {EMBED_FILE})")
        return

    html = open(TEMPLATE_FILE, encoding="utf-8").read()
    notes = []

    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if repo and URL_REGION_RE.search(html):
        live_url = f"https://cdn.jsdelivr.net/gh/{repo}@main/events.json"
        html = URL_REGION_RE.sub(
            lambda m: f"{m.group(1)}'{live_url}'{m.group(2)}", html, count=1)
        notes.append(f"live link -> {live_url}")
    elif not repo:
        notes.append("live link left as-is (not running in GitHub Actions)")

    if FALLBACK_REGION_RE.search(html):
        fallback = build_fallback(by_date)
        # json.dumps output is valid JS object syntax, so it can be dropped
        # straight in. Escape any "</" so it can't terminate the <script> tag.
        payload = json.dumps(fallback, indent=2, sort_keys=True).replace("</", "<\\/")
        html = FALLBACK_REGION_RE.sub(
            lambda m: f"{m.group(1)}{payload}{m.group(2)}", html, count=1)
        notes.append(f"saved copy -> {len(fallback)} dates")
    else:
        notes.append(f"WARNING: no saved-copy markers found in {TEMPLATE_FILE}")

    before = open(EMBED_FILE, encoding="utf-8").read() if os.path.exists(EMBED_FILE) else None
    if html != before:
        with open(EMBED_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Wrote {EMBED_FILE}: " + "; ".join(notes))
    else:
        print(f"{EMBED_FILE} unchanged: " + "; ".join(notes))


# --- Main -----------------------------------------------------------------

def load_previous_events():
    """Last successful run's events, flattened -- used to carry a source
    forward when it's temporarily unreachable, instead of dropping it."""
    try:
        data = json.load(open("events.json")).get("events", {})
    except Exception:
        return []
    out = []
    for day, evs in data.items():
        for ev in evs:
            item = dict(ev)
            item["date"] = day
            out.append(item)
    return out


def main():
    previous = load_previous_events()
    carried = []

    print(f"Fetching league list: {SCHEDULES_URL}")
    try:
        leagues = discover_leagues(html_to_lines(fetch(SCHEDULES_URL)))
        print(f"Discovered {len(leagues)} league(s)\n")
    except Exception as e:
        # QuickScores being down must not take the district calendar down
        # with it. Keep last run's games and carry on.
        print(f"ERROR: could not load the league list: {e}", file=sys.stderr)
        leagues = []
        kept = [ev for ev in previous if ev.get("cat") == "sports"]
        carried.append(f"athletics ({len(kept)} events from the last good run)")
        print(f"Keeping {len(kept)} athletics events from the last good run.\n")
    else:
        kept = []

    all_events, summaries, failures = [], [], []
    all_events.extend(kept)

    for lg in leagues:
        label = f"{lg['season']} / {lg['name']}".strip(" /")
        print(f"  -> {label}  (LeagueID={lg['league_id']})")
        diag = {"html_date_headers": 0, "html_years_inferred": 0}
        events, source = [], "none"

        try:
            events = games_from_feed(lg, diag)
            if events:
                source = "feed"
            else:
                if diag.get("feed_error"):
                    print(f"       feed unavailable ({diag['feed_error']}) -- using HTML")
                else:
                    print("       feed had no Lincoln Hall games -- checking HTML")
                time.sleep(REQUEST_DELAY_SECONDS)
                events = games_from_html(lg, diag)
                source = "html" if events else "none"
        except Exception as e:
            print(f"       ERROR: {e}", file=sys.stderr)
            failures.append(label)
            summaries.append((label, None, "error"))
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        print(f"       {len(events)} Lincoln Hall event(s) via {source}")
        all_events.extend(events)
        summaries.append((label, len(events), source))
        time.sleep(REQUEST_DELAY_SECONDS)

    sports_count = len(all_events)
    for ev in all_events:
        ev.setdefault("cat", "sports")

    # --- District-wide events + the school-year calendar -----------------
    district, feed_raw = district_events()
    if district is None:
        # Feed unreachable: keep last run's district-side events rather than
        # letting them vanish. (Any overlap with the PDF calendar below is
        # removed by the de-duplication step.)
        district = [ev for ev in previous if ev.get("cat") == "district"]
        carried.append(f"district events ({len(district)} from the last good run)")
        print(f"  Keeping {len(district)} district events from the last good run.")
    all_events.extend(district)
    academic = academic_events()
    all_events.extend(academic)

    # Deduplicate: same date, sport and title from any source. The district
    # feed and the PDF calendar overlap on things like holidays, so this
    # keeps one copy rather than showing each twice.
    # --- Normalise school-status entries -----------------------------------
    # Half days and no-school days arrive from two sources (the live feed and
    # the PDF calendar file) that may disagree on category or wording. Settle
    # them to one consistent form BEFORE de-duplicating, and give half days a
    # title that actually says "Half Day".
    for ev in all_events:
        t = ev.get("type")
        # Only two categories exist now. Anything else (e.g. the retired
        # "school" category from an older academic_calendar.json) is
        # district-side, not athletics.
        if ev.get("cat") not in ("sports", "district"):
            ev["cat"] = "district"
        if t in STATUS_TYPES:
            ev["cat"] = "district"
            ev["sport"] = STATUS_LABELS[t]
        if t == "halfday":
            original = (ev.get("match") or "").strip()
            ev["match"] = HALFDAY_TITLE
            if original and _norm_title(original) != _norm_title(HALFDAY_TITLE):
                ev["note"] = ev.get("note") or original

    unique = dedupe(all_events)

    by_date = {}
    for ev in unique:
        by_date.setdefault(ev["date"], []).append(ev)

    print("\n--- Per-league summary (athletics) ---")
    for label, count, source in summaries:
        print(f"  FAILED     {label}" if count is None
              else f"  {count:>3} via {source:<5} {label}")

    cats = {}
    for ev in unique:
        cats[ev.get("cat", "?")] = cats.get(ev.get("cat", "?"), 0) + 1
    print("\n--- Totals by category ---")
    for c, n in sorted(cats.items()):
        print(f"  {n:>4}  {c}")

    years_found = sorted({d[:4] for d in by_date})
    print(f"\nTotal: {len(unique)} events across {len(by_date)} dates")
    print(f"  (athletics found: {sports_count}, district feed: {len(district)}, "
          f"academic calendar: {len(academic)}, after dedupe: {len(unique)})")
    print(f"Years represented: {', '.join(years_found) if years_found else '(none)'}")
    if carried:
        print("\nNOTE: a source was unreachable this run, so its last good data was kept:",
              file=sys.stderr)
        for c in carried:
            print(f"  - {c}", file=sys.stderr)

    if not district:
        print("\nWARNING: the district calendar feed returned nothing -- "
              "district events will be missing from the calendar.", file=sys.stderr)

    this_year = str(date.today().year)
    if years_found and this_year not in years_found:
        print(f"\nWARNING: no games found for the current year ({this_year}), "
              f"even though other years came through. That usually means the "
              f"current season's pages are being read differently -- check the "
              f"per-league summary above for leagues showing 0.", file=sys.stderr)

    if failures:
        print(f"\nWARNING: {len(failures)} league(s) failed:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)

    if not unique:
        print("\nERROR: no events found at all.", file=sys.stderr)
        if os.path.exists("events.json"):
            print("Keeping the existing events.json rather than emptying it.", file=sys.stderr)
            # Still rebuild embed.html from the last good schedule, so a
            # failed run never leaves Google Sites without a working page.
            try:
                build_embed(json.load(open("events.json")).get("events", {}))
            except Exception as e:
                print(f"Could not build {EMBED_FILE} from events.json: {e}", file=sys.stderr)
            sys.exit(1)

    with open("events.json", "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "team": TEAM_NAME,
            "events": by_date,
        }, f, indent=2)
    print("\nWrote events.json")

    build_embed(by_date)


if __name__ == "__main__":
    main()
