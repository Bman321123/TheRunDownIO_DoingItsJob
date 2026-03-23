import asyncio
import concurrent.futures
import json
import time
import threading
import logging
import re
from typing import Any
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from aiohttp import web
from rapidfuzz import fuzz

import bovada_scraper
import therundown
import prediction_markets
from kalshi_scraper import fetch_kalshi_markets
from polymarket_scraper import fetch_polymarket_markets
from novig_scraper import fetch_novig_markets
import og_scraper

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
ENABLE_AUTO_SCAN: bool = False
DELTA_POLL_INTERVAL: int = 10  # seconds between delta polls
SNAPSHOT_MAX_AGE: int = 300    # 5 min — re-bootstrap snapshot if older

logger = logging.getLogger(__name__)

# Sports shared across TheRundown and Bovada feeds.
_RUNDOWN_TO_BOVADA_SPORT = {
    "NFL": "nfl",
    "NBA": "nba",
    "NCAAB": "ncaab",
    "MLB": "mlb",
    "NHL": "nhl",
    "MMA": "mma",
    "NCAAF": "ncaaf",
}

_RUNDOWN_TO_PM_SPORT: dict[str, str] = {
    "NFL":    "nfl",
    "NBA":    "nba",
    "NCAAB":  "ncaab",
    "MLB":    "mlb",
    "NHL":    "nhl",
    "NCAAWB": "ncaawb",
    "MMA":    "mma",
    "NCAAF":  "ncaaf",
}

_RUNDOWN_TO_NOVIG_SPORT: dict[str, str] = {
    "NFL":    "nfl",
    "NBA":    "nba",
    "NCAAB":  "ncaab",
    "MLB":    "mlb",
    "NHL":    "nhl",
    "NCAAWB": "ncaawb",
    "MMA":    "mma",
    "NCAAF":  "ncaaf",
}

_RUNDOWN_TO_OG_SPORT: dict[str, str] = {
    "NFL":    "nfl",
    "NBA":    "nba",
    "NCAAB":  "ncaab",
    "MLB":    "mlb",
    "NHL":    "nhl",
    "NCAAWB": "ncaawb",
    "NCAAF":  "ncaaf",
}

_BOVADA_TO_DISPLAY_SPORT = {
    "nfl": "NFL",
    "nba": "NBA",
    "ncaab": "NCAAB",
    "mlb": "MLB",
    "nhl": "NHL",
}

_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_MULTISPACE = re.compile(r"\s+")
_FUZZY_MATCH_THRESHOLD = 80.0

# Tier-2 alias map for common shorthand/abbreviations.
_TEAM_ALIAS_MAP: dict[str, str] = {
    # --- NBA ---
    "lakers": "los angeles lakers", "la lakers": "los angeles lakers", "lal": "los angeles lakers",
    "clippers": "los angeles clippers", "la clippers": "los angeles clippers", "lac": "los angeles clippers",
    "warriors": "golden state warriors", "gsw": "golden state warriors", "golden state": "golden state warriors",
    "celtics": "boston celtics", "bos": "boston celtics", "boston": "boston celtics",
    "heat": "miami heat", "mia": "miami heat", "mia heat": "miami heat", "miami": "miami heat",
    "knicks": "new york knicks", "nyk": "new york knicks",
    "nets": "brooklyn nets", "bkn": "brooklyn nets", "brooklyn": "brooklyn nets",
    "76ers": "philadelphia 76ers", "sixers": "philadelphia 76ers", "phi": "philadelphia 76ers", "philadelphia": "philadelphia 76ers",
    "bulls": "chicago bulls", "chi": "chicago bulls", "chicago": "chicago bulls",
    "bucks": "milwaukee bucks", "mil": "milwaukee bucks", "milwaukee": "milwaukee bucks",
    "raptors": "toronto raptors", "tor": "toronto raptors", "toronto": "toronto raptors",
    "hawks": "atlanta hawks", "atl": "atlanta hawks", "atlanta": "atlanta hawks",
    "cavaliers": "cleveland cavaliers", "cavs": "cleveland cavaliers", "cle": "cleveland cavaliers", "cleveland": "cleveland cavaliers",
    "pacers": "indiana pacers", "ind": "indiana pacers", "indiana": "indiana pacers",
    "pistons": "detroit pistons", "det": "detroit pistons", "detroit": "detroit pistons",
    "magic": "orlando magic", "orl": "orlando magic", "orlando": "orlando magic",
    "wizards": "washington wizards", "was": "washington wizards",
    "hornets": "charlotte hornets", "cha": "charlotte hornets", "charlotte": "charlotte hornets",
    "nuggets": "denver nuggets", "den": "denver nuggets", "denver": "denver nuggets",
    "thunder": "oklahoma city thunder", "okc": "oklahoma city thunder", "oklahoma city": "oklahoma city thunder",
    "trail blazers": "portland trail blazers", "blazers": "portland trail blazers", "por": "portland trail blazers", "portland": "portland trail blazers",
    "jazz": "utah jazz", "uta": "utah jazz", "utah": "utah jazz",
    "timberwolves": "minnesota timberwolves", "wolves": "minnesota timberwolves", "min": "minnesota timberwolves", "minnesota": "minnesota timberwolves",
    "pelicans": "new orleans pelicans", "nop": "new orleans pelicans", "new orleans": "new orleans pelicans",
    "rockets": "houston rockets", "hou": "houston rockets", "houston": "houston rockets",
    "mavericks": "dallas mavericks", "mavs": "dallas mavericks", "dal": "dallas mavericks", "dallas": "dallas mavericks",
    "grizzlies": "memphis grizzlies", "mem": "memphis grizzlies", "memphis": "memphis grizzlies",
    "spurs": "san antonio spurs", "sas": "san antonio spurs", "san antonio": "san antonio spurs",
    "kings": "sacramento kings", "sac": "sacramento kings", "sacramento": "sacramento kings",
    "suns": "phoenix suns", "phx": "phoenix suns", "phoenix": "phoenix suns",
    # --- NFL ---
    "patriots": "new england patriots", "ne": "new england patriots",
    "chiefs": "kansas city chiefs", "kc": "kansas city chiefs",
    "bills": "buffalo bills", "buf": "buffalo bills", "buffalo": "buffalo bills",
    "dolphins": "miami dolphins", "mia dolphins": "miami dolphins",
    "jets": "new york jets", "nyj": "new york jets",
    "giants": "new york giants", "nyg": "new york giants",
    "eagles": "philadelphia eagles", "phi eagles": "philadelphia eagles",
    "cowboys": "dallas cowboys", "dal cowboys": "dallas cowboys",
    "commanders": "washington commanders", "wsh": "washington commanders",
    "ravens": "baltimore ravens", "bal": "baltimore ravens", "baltimore": "baltimore ravens",
    "steelers": "pittsburgh steelers", "pit": "pittsburgh steelers", "pittsburgh": "pittsburgh steelers",
    "bengals": "cincinnati bengals", "cin": "cincinnati bengals", "cincinnati": "cincinnati bengals",
    "browns": "cleveland browns", "cle browns": "cleveland browns",
    "texans": "houston texans", "hou texans": "houston texans",
    "colts": "indianapolis colts", "ind colts": "indianapolis colts", "indianapolis": "indianapolis colts",
    "titans": "tennessee titans", "ten": "tennessee titans", "tennessee": "tennessee titans",
    "jaguars": "jacksonville jaguars", "jax": "jacksonville jaguars", "jacksonville": "jacksonville jaguars",
    "packers": "green bay packers", "gb": "green bay packers", "green bay": "green bay packers",
    "bears": "chicago bears", "chi bears": "chicago bears",
    "vikings": "minnesota vikings", "min vikings": "minnesota vikings",
    "lions": "detroit lions", "det lions": "detroit lions",
    "saints": "new orleans saints", "no": "new orleans saints",
    "buccaneers": "tampa bay buccaneers", "bucs": "tampa bay buccaneers", "tb": "tampa bay buccaneers", "tampa bay": "tampa bay buccaneers",
    "falcons": "atlanta falcons", "atl falcons": "atlanta falcons",
    "panthers": "carolina panthers", "car": "carolina panthers", "carolina": "carolina panthers",
    "49ers": "san francisco 49ers", "niners": "san francisco 49ers", "sf": "san francisco 49ers", "san francisco": "san francisco 49ers",
    "seahawks": "seattle seahawks", "sea": "seattle seahawks", "seattle": "seattle seahawks",
    "rams": "los angeles rams", "la rams": "los angeles rams",
    "cardinals": "arizona cardinals", "ari": "arizona cardinals", "arizona": "arizona cardinals",
    "chargers": "los angeles chargers", "la chargers": "los angeles chargers",
    "broncos": "denver broncos", "den broncos": "denver broncos",
    "raiders": "las vegas raiders", "lv": "las vegas raiders", "las vegas": "las vegas raiders",
    # --- NHL ---
    "bruins": "boston bruins", "bos bruins": "boston bruins",
    "sabres": "buffalo sabres", "buf sabres": "buffalo sabres",
    "flames": "calgary flames", "cgy": "calgary flames", "calgary": "calgary flames",
    "hurricanes": "carolina hurricanes", "car hurricanes": "carolina hurricanes",
    "blackhawks": "chicago blackhawks", "chi blackhawks": "chicago blackhawks",
    "avalanche": "colorado avalanche", "col": "colorado avalanche", "colorado": "colorado avalanche",
    "blue jackets": "columbus blue jackets", "cbj": "columbus blue jackets", "columbus": "columbus blue jackets",
    "stars": "dallas stars", "dal stars": "dallas stars",
    "red wings": "detroit red wings", "det red wings": "detroit red wings",
    "oilers": "edmonton oilers", "edm": "edmonton oilers", "edmonton": "edmonton oilers",
    "panthers nhl": "florida panthers", "fla": "florida panthers", "florida": "florida panthers",
    "kings nhl": "los angeles kings", "la kings": "los angeles kings",
    "wild": "minnesota wild", "min wild": "minnesota wild",
    "canadiens": "montreal canadiens", "mtl": "montreal canadiens", "montreal": "montreal canadiens",
    "predators": "nashville predators", "nsh": "nashville predators", "nashville": "nashville predators",
    "devils": "new jersey devils", "njd": "new jersey devils",
    "islanders": "new york islanders", "nyi": "new york islanders",
    "rangers": "new york rangers", "nyr": "new york rangers",
    "senators": "ottawa senators", "ott": "ottawa senators", "ottawa": "ottawa senators",
    "flyers": "philadelphia flyers", "phi flyers": "philadelphia flyers",
    "penguins": "pittsburgh penguins", "pit penguins": "pittsburgh penguins",
    "sharks": "san jose sharks", "sjs": "san jose sharks", "san jose": "san jose sharks",
    "kraken": "seattle kraken", "sea kraken": "seattle kraken",
    "blues": "st louis blues", "stl": "st louis blues", "st louis": "st louis blues",
    "lightning": "tampa bay lightning", "tbl": "tampa bay lightning", "tb lightning": "tampa bay lightning",
    "maple leafs": "toronto maple leafs", "leafs": "toronto maple leafs", "tor leafs": "toronto maple leafs",
    "canucks": "vancouver canucks", "van": "vancouver canucks", "vancouver": "vancouver canucks",
    "golden knights": "vegas golden knights", "vgk": "vegas golden knights", "vegas": "vegas golden knights",
    "capitals": "washington capitals", "wsh capitals": "washington capitals",
    "jets nhl": "winnipeg jets", "wpg": "winnipeg jets", "winnipeg": "winnipeg jets",
    # --- MLB ---
    "yankees": "new york yankees", "nyy": "new york yankees",
    "mets": "new york mets", "nym": "new york mets",
    "red sox": "boston red sox", "bos red sox": "boston red sox",
    "dodgers": "los angeles dodgers", "la dodgers": "los angeles dodgers",
    "cubs": "chicago cubs", "chc": "chicago cubs",
    "white sox": "chicago white sox", "cws": "chicago white sox",
    "braves": "atlanta braves", "atl braves": "atlanta braves",
    "astros": "houston astros", "hou astros": "houston astros",
    "phillies": "philadelphia phillies", "phi phillies": "philadelphia phillies",
    "padres": "san diego padres", "sd": "san diego padres", "san diego": "san diego padres",
    "guardians": "cleveland guardians", "cle guardians": "cleveland guardians",
    "mariners": "seattle mariners", "sea mariners": "seattle mariners",
    "blue jays": "toronto blue jays", "tor blue jays": "toronto blue jays",
    "twins": "minnesota twins", "min twins": "minnesota twins",
    "brewers": "milwaukee brewers", "mil brewers": "milwaukee brewers",
    "diamondbacks": "arizona diamondbacks", "ari dbacks": "arizona diamondbacks",
    "reds": "cincinnati reds", "cin reds": "cincinnati reds",
    "pirates": "pittsburgh pirates", "pit pirates": "pittsburgh pirates",
    "royals": "kansas city royals", "kc royals": "kansas city royals",
    "orioles": "baltimore orioles", "bal orioles": "baltimore orioles",
    "rays": "tampa bay rays", "tb rays": "tampa bay rays",
    "rockies": "colorado rockies", "col rockies": "colorado rockies",
    "tigers": "detroit tigers", "det tigers": "detroit tigers",
    "angels": "los angeles angels", "la angels": "los angeles angels", "laa": "los angeles angels",
    "athletics": "oakland athletics", "as": "oakland athletics", "oak": "oakland athletics", "oakland": "oakland athletics",
    "nationals": "washington nationals", "wsh nationals": "washington nationals",
    "marlins": "miami marlins", "mia marlins": "miami marlins",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as ex:
            resp = ex
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _serialize(obj: Any) -> Any:
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    return obj


# ──────────────────────────────────────────────
# IN-MEMORY EVENT STORE
# Maintains the latest state of all events per sport,
# updated by both snapshot and delta calls.
# ──────────────────────────────────────────────
class EventStore:
    def __init__(self):
        self._lock = threading.Lock()
        # sport_id -> { event_id -> event_dict }
        self.events: dict[int, dict[str, dict]] = {}
        # sport_id -> delta cursor string
        self.cursors: dict[int, str] = {}
        # sport_id -> timestamp of last snapshot
        self.snapshot_ts: dict[int, float] = {}
        # sport_id -> the date string (YYYY-MM-DD) used when the snapshot was last fetched
        self.snapshot_dates: dict[int, str] = {}
        # Timestamp of the most recent successfully fetched data
        self.last_update_ts: float = 0.0
        # Datapoint budget tracking
        self.dp_remaining: str = "?"

    def set_snapshot(self, sport_id: int, events: list[dict], cursor: str | None, date_str: str = ""):
        with self._lock:
            by_id = {}
            for e in events:
                eid = e.get("event_id")
                if eid:
                    by_id[eid] = e
            self.events[sport_id] = by_id
            if cursor:
                self.cursors[sport_id] = cursor
            self.snapshot_ts[sport_id] = time.time()
            if date_str:
                self.snapshot_dates[sport_id] = date_str
            self.last_update_ts = time.time()

    def merge_delta_events(self, sport_id: int, delta_events: list[dict], new_cursor: str | None):
        """Merge event-level delta updates."""
        with self._lock:
            store = self.events.setdefault(sport_id, {})
            for de in delta_events:
                eid = de.get("event_id")
                if eid:
                    store[eid] = de
            if new_cursor:
                self.cursors[sport_id] = new_cursor
            self.last_update_ts = time.time()

    def get_events(self, sport_id: int) -> list[dict]:
        with self._lock:
            return list(self.events.get(sport_id, {}).values())

    def needs_bootstrap(self, sport_id: int) -> bool:
        ts = self.snapshot_ts.get(sport_id, 0)
        return (time.time() - ts) > SNAPSHOT_MAX_AGE

    def freshest_update_age(self) -> float:
        """Seconds since last successful data update."""
        if self.last_update_ts == 0:
            return float("inf")
        return time.time() - self.last_update_ts


_STORE = EventStore()

# ──────────────────────────────────────────────
# MODULE-LEVEL CLIENT (one Session for process lifetime)
# ──────────────────────────────────────────────
# Shared across all scan_arbs_once() calls. scan_arbs_once() is called
# serially (asyncio.to_thread, one at a time), so no locking is needed.
_CLIENT = therundown.RundownClient(therundown.API_KEY, therundown.USE_RAPIDAPI)

# Synthetic book IDs for prediction market / external sources
KALSHI_BOOK_ID     = 9001
POLYMARKET_BOOK_ID = 9002
BOVADA_BOOK_ID     = 9003
NOVIG_BOOK_ID      = 9004
OG_BOOK_ID         = 9005

therundown.KNOWN_BOOKS[KALSHI_BOOK_ID]     = "Kalshi"
therundown.KNOWN_BOOKS[POLYMARKET_BOOK_ID] = "Polymarket"
therundown.KNOWN_BOOKS[BOVADA_BOOK_ID]     = "Bovada"
therundown.KNOWN_BOOKS[NOVIG_BOOK_ID]      = "Novig"
therundown.KNOWN_BOOKS[OG_BOOK_ID]         = "OG"
therundown.ALLOWED_BOOK_NAMES.update({"kalshi", "polymarket", "bovada", "novig", "og"})

# Affiliate cache — /affiliates data changes at most monthly; refresh daily.
_AFFILIATES_TTL: int = 86400
_affiliates_last_fetched: float = 0.0


def _refresh_affiliates_if_stale() -> None:
    """Fetch affiliate names at most once every _AFFILIATES_TTL seconds."""
    global _affiliates_last_fetched
    if time.time() - _affiliates_last_fetched < _AFFILIATES_TTL:
        return
    fresh = therundown.fetch_affiliates()
    if fresh:
        merged = dict(therundown._KNOWN_BOOKS_FALLBACK)
        merged.update(fresh)
        merged[KALSHI_BOOK_ID]     = "Kalshi"
        merged[POLYMARKET_BOOK_ID] = "Polymarket"
        merged[BOVADA_BOOK_ID]     = "Bovada"
        merged[NOVIG_BOOK_ID]      = "Novig"
        merged[OG_BOOK_ID]         = "OG"
        therundown.KNOWN_BOOKS = merged
        _affiliates_last_fetched = time.time()
        print(f"  AFFILIATES :: refreshed ({len(fresh)} books cached for {_AFFILIATES_TTL}s)")
    else:
        # Keep existing KNOWN_BOOKS and retry on next scan.
        print("  AFFILIATES :: refresh failed, retaining existing book names")


# ──────────────────────────────────────────────
# SCAN: SNAPSHOT + ANALYZE
# ──────────────────────────────────────────────
def scan_arbs_once(sport_ids: list[int]) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Fetch fresh events (snapshot + delta merge), then run arbitrage analysis.
    Returns (arbs, raw_lines).
    """
    _refresh_affiliates_if_stale()

    client = _CLIENT
    _ET = ZoneInfo("America/New_York")
    today_dt = datetime.now(_ET).date()
    today = today_dt.strftime("%Y-%m-%d")
    tomorrow = (today_dt + therundown.datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    def _filter_active_events(events: list[dict]) -> list[dict]:
        """Keep only events that are not completed and not too far in-progress."""
        now_utc = datetime.now(timezone.utc)
        active_events: list[dict] = []
        for evt in events:
            status = (evt.get("score") or {}).get("event_status", "")
            if isinstance(status, str) and status.lower() in {"final", "complete", "closed", "in_progress", "in progress"}:
                continue

            # Skip games that started more than 15 min ago (odds are unreliable)
            start_raw = evt.get("event_date") or evt.get("event_date_start")
            if start_raw:
                try:
                    start_dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=timezone.utc)
                    mins_since_start = (now_utc - start_dt).total_seconds() / 60.0
                    if mins_since_start > 15:
                        continue
                except Exception:
                    pass

            active_events.append(evt)
        return active_events

    def _unwrap_event_payload(event_payload: dict) -> dict:
        """
        V2 single-event responses can be either:
          - { "events": [ {...} ] }
          - { ...event fields... }
        """
        if not isinstance(event_payload, dict):
            return {}
        events_list = event_payload.get("events")
        if isinstance(events_list, list) and events_list and isinstance(events_list[0], dict):
            return events_list[0]
        return event_payload

    def _extract_prop_markets_from_event_payload(event_payload: dict) -> list[dict]:
        event_obj = _unwrap_event_payload(event_payload)
        markets = event_obj.get("markets") or []
        if not isinstance(markets, list):
            return []
        prop_markets: list[dict] = []
        for market in markets:
            if not isinstance(market, dict):
                continue
            # V2 may return market_id as either str or int.
            try:
                mid = int(market.get("market_id"))
            except (TypeError, ValueError):
                continue
            if mid in therundown.PROP_MARKET_IDS:
                market["market_id"] = mid
                prop_markets.append(market)
        return prop_markets

    def _market_signature(market: dict) -> tuple:
        """
        Build a stable-ish signature so prop-market dedupe survives same market_id
        with multiple players/line variants.
        """
        participants = market.get("participants") or []
        participant_names: list[str] = []
        line_values: list[str] = []
        if isinstance(participants, list):
            for p in participants:
                if not isinstance(p, dict):
                    continue
                participant_names.append(str(p.get("name") or ""))
                for ln in (p.get("lines") or []):
                    if not isinstance(ln, dict):
                        continue
                    line_values.append(str(ln.get("value")))
        return (
            market.get("market_id"),
            market.get("period_id"),
            market.get("name"),
            tuple(sorted(participant_names)[:6]),
            tuple(sorted(line_values)[:10]),
        )

    for sport_id in sport_ids:
        # Track whether we fetch a fresh snapshot this pass (vs. using cache).
        did_bootstrap = _STORE.needs_bootstrap(sport_id)

        if did_bootstrap:
            try:
                data = client.get_events(sport_id, today)
                events = (data or {}).get("events") or []
                cursor = (data or {}).get("meta", {}).get("delta_last_id")
                snap_date = today

                if not events:
                    # ET-today has zero events — fallback to ET-tomorrow immediately.
                    print(
                        f"  SNAPSHOT :: sport {sport_id} ET-today ({today}) returned 0 events"
                        f" — fetching ET-tomorrow ({tomorrow})"
                    )
                    time.sleep(therundown.get_safe_delay())
                    try:
                        tmr_data = client.get_events(sport_id, tomorrow)
                        tmr_events = (tmr_data or {}).get("events") or []
                        tmr_cursor = (tmr_data or {}).get("meta", {}).get("delta_last_id")
                        if tmr_events:
                            events = tmr_events
                            cursor = tmr_cursor
                            snap_date = tomorrow
                    except Exception as tmr_e:
                        print(f"  SNAPSHOT :: sport {sport_id} ET-tomorrow fallback failed: {tmr_e}")

                _STORE.set_snapshot(sport_id, events, cursor, snap_date)
                _STORE.dp_remaining = client.last_headers.get("X-Datapoints-Remaining", "?")
                print(
                    f"  SNAPSHOT :: sport {sport_id} loaded {len(events)} events"
                    f" (date={snap_date}, cursor={str(cursor)[:12]}…)"
                )
                time.sleep(therundown.get_safe_delay())
            except Exception as e:
                print(f"  SNAPSHOT :: sport {sport_id} failed: {e}")
                time.sleep(therundown.get_safe_delay())
        else:
            age = int(time.time() - _STORE.snapshot_ts.get(sport_id, 0))
            snap_date = _STORE.snapshot_dates.get(sport_id, today)
            print(f"  SNAPSHOT :: sport {sport_id} using cached data ({age}s old, max {SNAPSHOT_MAX_AGE}s, date={snap_date})")

        # Delta poll — cheap price-change check.
        cursor = _STORE.cursors.get(sport_id)
        if cursor:
            try:
                delta_data = client.get_markets_delta(sport_id, cursor)
                deltas = (delta_data or {}).get("deltas") or []
                new_cursor = (delta_data or {}).get("meta", {}).get("delta_last_id", cursor)
                _STORE.cursors[sport_id] = new_cursor
                _STORE.dp_remaining = client.last_headers.get("X-Datapoints-Remaining", "?")

                if deltas:
                    if did_bootstrap:
                        # Snapshot is already fresh — delta prices already reflected.
                        print(
                            f"  DELTA :: sport {sport_id} got {len(deltas)} price changes "
                            f"(snapshot already fresh, cursor → {str(new_cursor)[:12]}…)"
                        )
                    else:
                        # Cached data + new prices -> re-fetch snapshot now.
                        # Use the same date the snapshot was originally bootstrapped with.
                        refresh_date = _STORE.snapshot_dates.get(sport_id, today)
                        print(
                            f"  DELTA :: sport {sport_id} got {len(deltas)} price changes "
                            f"— refreshing snapshot ({refresh_date}) to incorporate updates"
                        )
                        time.sleep(therundown.get_safe_delay())
                        try:
                            refresh_data = client.get_events(sport_id, refresh_date)
                            fresh_events = (refresh_data or {}).get("events") or []
                            fresh_cursor = (refresh_data or {}).get("meta", {}).get("delta_last_id")
                            _STORE.set_snapshot(sport_id, fresh_events, fresh_cursor or new_cursor, refresh_date)
                            _STORE.dp_remaining = client.last_headers.get("X-Datapoints-Remaining", "?")
                            print(
                                f"  DELTA :: sport {sport_id} snapshot refreshed "
                                f"({len(fresh_events)} events from {refresh_date}, cursor → {str(fresh_cursor or new_cursor)[:12]}…)"
                            )
                        except Exception as refresh_err:
                            print(f"  DELTA :: sport {sport_id} snapshot refresh failed ({refresh_err})")
                else:
                    print(f"  DELTA :: sport {sport_id} no changes (cursor → {str(new_cursor)[:12]}…)")
                time.sleep(therundown.get_safe_delay())
            except Exception as e:
                print(f"  DELTA :: sport {sport_id} failed ({e}), cursor may be stale — will re-bootstrap next scan")
                _STORE.snapshot_ts[sport_id] = 0  # Force re-bootstrap
                time.sleep(therundown.get_safe_delay())

    # Analyze all events in the store
    all_results: list[dict] = []
    all_raw_lines: list[dict] = []
    all_best_lines: list[dict] = []

    for sport_id in sport_ids:
        events = _STORE.get_events(sport_id)
        sport_name = therundown.ALL_SPORTS.get(sport_id, str(sport_id))
        # Use the date the store was bootstrapped with (ET-today or ET-tomorrow fallback).
        analysis_date = _STORE.snapshot_dates.get(sport_id, today)
        active_events = _filter_active_events(events)

        if len(events) != len(active_events):
            print(
                f"  FILTER :: {sport_name}: {len(events)} total → {len(active_events)} active"
                f" (date={analysis_date}, dropped {len(events) - len(active_events)} started/finished)"
            )

        # ── Prop market enrichment ───────────────────────────────────────────
        # Fetch all prop markets for this sport/date in one API call.
        try:
            prop_data = client.get_prop_events(sport_id, analysis_date)
            prop_events = (prop_data or {}).get("events") or []

            # event_id -> prop markets
            prop_markets_by_event: dict[str, list[dict]] = {}
            for pe in prop_events:
                eid = pe.get("event_id")
                if not eid:
                    continue
                raw_markets = pe.get("markets") or []
                prop_markets: list[dict] = []
                for m in raw_markets:
                    if not isinstance(m, dict):
                        continue
                    try:
                        mid = int(m.get("market_id"))
                    except (TypeError, ValueError):
                        continue
                    if mid in therundown.PROP_MARKET_IDS:
                        m["market_id"] = mid
                        prop_markets.append(m)
                if prop_markets:
                    prop_markets_by_event[str(eid)] = prop_markets

            enrich_events_with_props = 0
            enrich_market_total = 0
            for evt in active_events:
                eid = str(evt.get("event_id") or "")
                if not eid or eid not in prop_markets_by_event:
                    continue

                existing_markets = evt.get("markets")
                if not isinstance(existing_markets, list):
                    existing_markets = []

                existing_sigs = {
                    _market_signature(m)
                    for m in existing_markets
                    if isinstance(m, dict)
                }
                new_prop_markets = [
                    m for m in prop_markets_by_event[eid]
                    if _market_signature(m) not in existing_sigs
                ]
                if new_prop_markets:
                    evt["markets"] = existing_markets + new_prop_markets
                    enrich_events_with_props += 1
                    enrich_market_total += len(new_prop_markets)

            print(
                f"  PROP ENRICH :: {sport_name} api_events_with_props={len(prop_markets_by_event)} "
                f"active_events_enriched={enrich_events_with_props}/{len(active_events)} "
                f"markets_merged={enrich_market_total}"
            )
            time.sleep(therundown.get_safe_delay())
        except Exception as e:
            print(f"  PROP ENRICH :: {sport_name} failed ({e}) — continuing without props")

        books_in_batch: set[str] = set()
        prop_raw_count = 0
        prop_best_count = 0
        for evt in active_events:
            new_arbs, new_lines = therundown.analyze_event(evt, sport_name)
            best_lines_for_event = therundown.compute_best_lines_for_event(evt, sport_name)
            prop_raw, prop_best = therundown.parse_player_props(evt, sport_name)
            new_lines.extend(prop_raw)
            best_lines_for_event.extend(prop_best)
            prop_raw_count += len(prop_raw)
            prop_best_count += len(prop_best)

            for line in new_lines:
                books_in_batch.add(line["book"].lower())
                print(
                    f"     RAW LINE :: [{line['sport']}] {line['game']} | {line['book']} | "
                    f"{line['market_kind'].upper()} {line.get('line_label','')} {line['side']} ({line['odds_am']})"
                )
            all_results.extend(new_arbs)
            all_raw_lines.extend(new_lines)
            all_best_lines.extend(best_lines_for_event)

        if active_events:
            print(
                f"  PROP SUMMARY :: {sport_name} events={len(active_events)} "
                f"raw_props={prop_raw_count} best_props={prop_best_count}"
            )
            if prop_raw_count == 0:
                print(
                    f"  ⚠ PROP SUMMARY :: {sport_name} has 0 parsed prop lines after enrichment "
                    f"(date={analysis_date})."
                )

        if active_events and "betmgm" not in books_in_batch:
            print(f"  ⚠ BETMGM NOT FOUND in {sport_name} ({len(active_events)} events)")

    all_results.sort(key=lambda r: float(r.get("profit", 0.0)), reverse=True)

    data_age = _STORE.freshest_update_age()
    data_age_display = "inf" if data_age == float("inf") else str(int(data_age))
    print(
        "  ── Scan complete: "
        f"{len(all_results)} arbs, {len(all_raw_lines)} lines, {len(all_best_lines)} best-lines, "
        f"data age: {data_age_display}s, dp_remaining: {_STORE.dp_remaining}"
    )

    return all_results, all_raw_lines, all_best_lines


# ──────────────────────────────────────────────
# STATE + HANDLERS
# ──────────────────────────────────────────────
class ArbState:
    def __init__(self):
        self.arbs: list[dict] = []
        self.lines: list[dict] = []
        self.best_lines: list[dict] = []
        self.last_scan_ms: int | None = None
        self.last_error: str | None = None
        self.last_scan_sports: list[str] = []
        self.kalshi_count: int = 0
        self.poly_count:   int = 0
        self.pm_error:     str | None = None


async def handle_health(request: web.Request) -> web.Response:
    state: ArbState = request.app["state"]
    age = _STORE.freshest_update_age()
    return web.json_response({
        "ok": True,
        "lastScanMs": state.last_scan_ms,
        "sports": state.last_scan_sports,
        "count": len(state.arbs),
        "error": state.last_error,
        "dataAge": None if age == float("inf") else int(age),
        "dpRemaining": _STORE.dp_remaining,
    })


async def handle_arbs(request: web.Request) -> web.Response:
    state: ArbState = request.app["state"]
    age = _STORE.freshest_update_age()
    return web.json_response({
        "arbs": state.arbs,
        "lines": state.lines,
        "bestLines": state.best_lines,
        "dataAge": None if age == float("inf") else int(age),
        "dpRemaining": _STORE.dp_remaining,
    }, dumps=lambda x: json.dumps(x, default=_serialize))


def _sport_name_by_id() -> dict[int, str]:
    return {int(k): str(v) for k, v in (therundown.ALL_SPORTS or {}).items()}


def _sport_id_by_name() -> dict[str, int]:
    out: dict[str, int] = {}
    for sid, name in _sport_name_by_id().items():
        out[name.lower()] = sid
    return out


def _normalize_team_name(team_name: str) -> str:
    """
    Tier-1 normalization + tier-2 alias mapping.
    """
    normalized = (team_name or "").strip().lower()
    normalized = normalized.replace(".", " ")
    normalized = _NON_ALNUM.sub(" ", normalized)
    normalized = _MULTISPACE.sub(" ", normalized).strip()
    return _TEAM_ALIAS_MAP.get(normalized, normalized)


def _team_match_score(a: str, b: str) -> float:
    """
    Tier-3 fuzzy fallback after normalization/alias checks.
    """
    left = _normalize_team_name(a)
    right = _normalize_team_name(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 100.0
    return float(fuzz.token_set_ratio(left, right))


def _normalize_rundown_events_for_matching(sport_ids: list[int]) -> list[dict[str, Any]]:
    """
    Build a matching-friendly view of TheRundown events currently in the store.
    Now also stores _raw_event for use by the cross-source arb engine.
    """
    normalized: list[dict[str, Any]] = []
    for sport_id in sport_ids:
        sport_name = _sport_name_by_id().get(sport_id, str(sport_id))
        for event in _STORE.get_events(sport_id):
            try:
                _game, home_team, away_team = therundown._resolve_teams(event)
            except Exception:
                continue
            if not home_team or not away_team:
                continue
            start_time = event.get("event_date") or event.get("event_date_start") or ""
            normalized.append({
                "source": "therundown",
                "sport": str(sport_name).lower(),
                "home_team": home_team,
                "away_team": away_team,
                "start_time": start_time,
                "event_url": "",
                "_raw_event": event,
                "markets": therundown.compute_best_lines_for_event(event, sport_name),
            })
    return normalized


async def _fetch_bovada_events_for_sports(selected_names: list[str]) -> list[dict[str, Any]]:
    bovada_sports = [
        _RUNDOWN_TO_BOVADA_SPORT[name]
        for name in selected_names
        if name in _RUNDOWN_TO_BOVADA_SPORT
    ]
    if not bovada_sports:
        return []

    tasks = [bovada_scraper.fetch_bovada(sport) for sport in bovada_sports]
    nested = await asyncio.gather(*tasks, return_exceptions=True)
    events: list[dict[str, Any]] = []
    for entry in nested:
        if isinstance(entry, Exception):
            logger.warning("Bovada sport fetch failed: %s", entry)
            continue
        events.extend(entry)
    return events


async def _fetch_pm_events_for_sports(
    selected_names: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Fetch Kalshi and Polymarket events for the selected sports.
    Returns (kalshi_events, polymarket_events).
    """
    sport_keys = [
        _RUNDOWN_TO_PM_SPORT[name]
        for name in selected_names
        if name in _RUNDOWN_TO_PM_SPORT
    ]
    if not sport_keys:
        return [], []

    kalshi_events, poly_events = await prediction_markets.fetch_all_prediction_markets(sport_keys)
    return kalshi_events, poly_events


async def _fetch_novig_events_for_sports(
    selected_names: list[str],
) -> list[dict[str, Any]]:
    """Fetch Novig exchange events for the selected sports."""
    sport_keys = [
        _RUNDOWN_TO_NOVIG_SPORT[name]
        for name in selected_names
        if name in _RUNDOWN_TO_NOVIG_SPORT
    ]
    if not sport_keys:
        return []
    return await fetch_novig_markets(sport_keys)


async def _fetch_og_events_for_sports(
    selected_names: list[str],
) -> list[dict[str, Any]]:
    """Fetch OG prediction-market events for the selected sports only."""
    sport_keys = [
        _RUNDOWN_TO_OG_SPORT[name]
        for name in selected_names
        if name in _RUNDOWN_TO_OG_SPORT
    ]
    if not sport_keys:
        return []
    all_events: list[dict[str, Any]] = []
    for sk in sport_keys:
        events = await og_scraper.fetch_og(sport=sk)
        all_events.extend(events)
    return all_events


_SELECTION_FLIP = {"home": "away", "away": "home"}


def _flip_market(m: dict[str, Any]) -> dict[str, Any]:
    """Flip a flat market's selection (home↔away) and negate spread line_value."""
    flipped = {**m, "selection": _SELECTION_FLIP.get(m.get("selection", ""), m.get("selection", ""))}
    if str(m.get("market_type", "")).lower() == "spread":
        lv = m.get("line_value")
        if lv is not None:
            try:
                flipped["line_value"] = -float(lv)
            except (TypeError, ValueError):
                pass
    return flipped


def _match_source_to_hubs(
    source_events: list[dict[str, Any]],
    hub_events: list[dict[str, Any]],
    source_name: str,
) -> tuple[dict[int, list[dict]], list[dict[str, Any]]]:
    """
    Match source_events (bovada/kalshi/polymarket) to hub_events (TheRundown).

    Uses the existing 3-tier match logic (normalize -> alias -> rapidfuzz >= 80).

    Returns:
        matched:    { hub_index: [market_dicts from source] }
        unmatched:  source events with no hub counterpart
    """
    matched:   dict[int, list[dict]] = {}
    unmatched: list[dict[str, Any]] = []
    used_hub_indices: set[int] = set()

    for src_event in source_events:
        s_sport = str(src_event.get("sport", "")).lower()
        s_home  = str(src_event.get("home_team", ""))
        s_away  = str(src_event.get("away_team", ""))
        if not s_home or not s_away:
            continue

        best_idx:    int | None = None
        best_score             = -1.0
        best_swapped           = False

        for idx, hub in enumerate(hub_events):
            if idx in used_hub_indices:
                continue
            if str(hub.get("sport", "")).lower() != s_sport:
                continue

            h_home = str(hub.get("home_team", ""))
            h_away = str(hub.get("away_team", ""))
            if not h_home or not h_away:
                continue

            direct_score  = min(_team_match_score(s_home, h_home), _team_match_score(s_away, h_away))
            swapped_score = min(_team_match_score(s_home, h_away), _team_match_score(s_away, h_home))

            if direct_score >= swapped_score:
                cand_score, cand_swapped = direct_score, False
            else:
                cand_score, cand_swapped = swapped_score, True

            if cand_score >= _FUZZY_MATCH_THRESHOLD and cand_score > best_score:
                best_score, best_idx, best_swapped = cand_score, idx, cand_swapped

        if best_idx is None:
            unmatched.append(src_event)
            continue

        used_hub_indices.add(best_idx)

        markets = list(src_event.get("markets") or [])
        if best_swapped:
            markets = [_flip_market(m) for m in markets]
            logger.debug("%s: swapped home/away for %s vs %s", source_name, s_home, s_away)

        matched.setdefault(best_idx, []).extend(markets)

    return matched, unmatched


def _tag_markets_with_event_url(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stamp each market dict inside every event with the parent event_url."""
    for evt in events:
        url = evt.get("event_url") or ""
        for m in evt.get("markets") or []:
            m["_event_url"] = url
    return events


def _merge_all_sources(
    rundown_events:  list[dict[str, Any]],
    bovada_events:   list[dict[str, Any]],
    kalshi_events:   list[dict[str, Any]],
    poly_events:     list[dict[str, Any]],
    novig_events:    list[dict[str, Any]] | None = None,
    og_events:       list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Match Bovada, Kalshi, Polymarket, Novig, and OG events to TheRundown hubs.
    Returns consolidated event list with books: {bovada, kalshi, polymarket, novig, og} + _raw_event.
    Markets must already be tagged with _event_url via _tag_markets_with_event_url().
    """
    if novig_events is None:
        novig_events = []
    if og_events is None:
        og_events = []
    bovada_match,   _bovada_unmatched   = _match_source_to_hubs(bovada_events,  rundown_events, "bovada")
    kalshi_match,   _kalshi_unmatched   = _match_source_to_hubs(kalshi_events,  rundown_events, "kalshi")
    poly_match,     _poly_unmatched     = _match_source_to_hubs(poly_events,    rundown_events, "polymarket")
    novig_match,    _novig_unmatched    = _match_source_to_hubs(novig_events,   rundown_events, "novig")
    og_match,       _og_unmatched       = _match_source_to_hubs(og_events,      rundown_events, "og")

    consolidated: list[dict[str, Any]] = []

    for idx, hub in enumerate(rundown_events):
        books = {
            "bovada":     bovada_match.get(idx, []),
            "kalshi":     kalshi_match.get(idx, []),
            "polymarket": poly_match.get(idx, []),
            "novig":      novig_match.get(idx, []),
            "og":         og_match.get(idx, []),
        }
        if not any(books.values()):
            continue

        consolidated.append({
            "sport":      hub.get("sport", ""),
            "home_team":  hub.get("home_team", ""),
            "away_team":  hub.get("away_team", ""),
            "start_time": hub.get("start_time", ""),
            "_raw_event": hub.get("_raw_event"),
            "books":      books,
        })

    logger.info(
        "Merged %d cross-source events (bovada:%d, kalshi:%d, poly:%d, novig:%d matched hubs)",
        len(consolidated),
        sum(1 for v in bovada_match.values() if v),
        sum(1 for v in kalshi_match.values() if v),
        sum(1 for v in poly_match.values() if v),
        sum(1 for v in novig_match.values() if v),
    )
    return consolidated


def _match_intersection_events(
    bovada_events: list[dict[str, Any]],
    rundown_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Match events across books using:
      Tier1 normalization -> Tier2 alias map -> Tier3 fuzzy (>=80).
    Only intersection games are returned.
    """
    matched_games: list[dict[str, Any]] = []
    used_rundown_indices: set[int] = set()

    for bovada_event in bovada_events:
        b_sport = str(bovada_event.get("sport", "")).lower()
        b_home = str(bovada_event.get("home_team", ""))
        b_away = str(bovada_event.get("away_team", ""))
        if not b_home or not b_away:
            continue

        best_idx: int | None = None
        best_score = -1.0
        best_swapped = False

        for idx, rundown_event in enumerate(rundown_events):
            if idx in used_rundown_indices:
                continue
            if str(rundown_event.get("sport", "")).lower() != b_sport:
                continue

            r_home = str(rundown_event.get("home_team", ""))
            r_away = str(rundown_event.get("away_team", ""))
            if not r_home or not r_away:
                continue

            direct_home = _team_match_score(b_home, r_home)
            direct_away = _team_match_score(b_away, r_away)
            direct_score = min(direct_home, direct_away)

            swapped_home = _team_match_score(b_home, r_away)
            swapped_away = _team_match_score(b_away, r_home)
            swapped_score = min(swapped_home, swapped_away)

            if direct_score >= swapped_score:
                candidate_score = direct_score
                candidate_swapped = False
            else:
                candidate_score = swapped_score
                candidate_swapped = True

            if candidate_score >= _FUZZY_MATCH_THRESHOLD and candidate_score > best_score:
                best_score = candidate_score
                best_idx = idx
                best_swapped = candidate_swapped

        if best_idx is None:
            continue

        used_rundown_indices.add(best_idx)
        matched_rundown = rundown_events[best_idx]

        bovada_markets = list(bovada_event.get("markets") or [])
        if best_swapped:
            home_team = str(bovada_event.get("away_team", ""))
            away_team = str(bovada_event.get("home_team", ""))
            # Flip market selections AND negate spread line values
            bovada_markets = [_flip_market(m) for m in bovada_markets]
        else:
            home_team = str(bovada_event.get("home_team", ""))
            away_team = str(bovada_event.get("away_team", ""))

        consolidated = {
            "sport": b_sport,
            "start_time": bovada_event.get("start_time") or matched_rundown.get("start_time"),
            "home_team": home_team,
            "away_team": away_team,
            "matchScore": round(best_score, 2),
            "books": {
                "bovada": bovada_markets,
                "therundown": matched_rundown.get("markets", []),
            },
        }

        # INSERT ARBITRAGE AND BEST LINE MATH LOGIC HERE
        matched_games.append(consolidated)

    return matched_games


def _best_american_option(options: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [o for o in options if isinstance(o.get("american_odds"), int)]
    if not valid:
        return None
    positives = [o for o in valid if int(o["american_odds"]) > 0]
    negatives = [o for o in valid if int(o["american_odds"]) < 0]
    if positives:
        return max(positives, key=lambda o: int(o["american_odds"]))
    if negatives:
        return max(negatives, key=lambda o: int(o["american_odds"]))
    return None


def _merge_best_lines(sources: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """
    Merge multiple best_lines lists into one entry per market.

    Important: the frontend groups cards by `sport::game`. Bovada frequently emits
    different-but-equivalent team naming (e.g. `Miami` vs `Miami Heat`), so we
    dedupe using normalized home/away identities and then overwrite the merged
    entry's `game`/team display fields with a canonical representation.
    """

    def _best_of(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any] | None:
        if a is None:
            return b
        if b is None:
            return a
        oa = a.get("odds_am")
        ob = b.get("odds_am")
        if not isinstance(oa, (int, float)):
            return b
        if not isinstance(ob, (int, float)):
            return a
        # Positive odds always beat negatives. Among negatives, closer to zero wins.
        if oa > 0 and ob <= 0:
            return a
        if ob > 0 and oa <= 0:
            return b
        return a if oa >= ob else b

    def _parse_game_away_home(game: str) -> tuple[str, str]:
        """Parse "Away @ Home" into (away, home)."""
        if not game:
            return "", ""
        m = re.match(r"^\s*(.*?)\s*@\s*(.*?)\s*$", str(game))
        if not m:
            return "", ""
        away_raw = (m.group(1) or "").strip()
        home_raw = (m.group(2) or "").strip()
        return away_raw, home_raw

    def _display_from_norm(norm: str) -> str:
        norm = (norm or "").strip()
        if not norm:
            return ""
        return " ".join(w[:1].upper() + w[1:] if w else w for w in norm.split(" "))

    def _norm_and_display(team: str) -> tuple[str, str]:
        norm = _normalize_team_name(team)
        return norm, _display_from_norm(norm)

    # Canonicalize matchups across naming variants (e.g. "New York" vs "New York Knicks")
    # by fuzzy-matching home+away teams to an existing canonical pair.
    canonical_pairs: list[dict[str, Any]] = []

    def _maybe_contextualize_la(team_raw: str, other_raw: str, sport: str) -> tuple[str, str]:
        """
        Contextual LA mapping:
        - If the other side is Lakers/Clippers, interpret "Los Angeles"/"LA" as the opposite.
        """
        norm = _normalize_team_name(team_raw)
        other_norm = _normalize_team_name(other_raw)
        sport_u = str(sport or "").upper()
        if sport_u == "NBA":
            low_norm = (norm or "").lower()
            # Only trigger for city-only LA labels, not when lakers/clippers are already present.
            if low_norm in {"los angeles", "la"} and "lakers" not in low_norm and "clippers" not in low_norm:
                if "clippers" in other_norm:
                    norm = "los angeles lakers"
                elif "lakers" in other_norm:
                    norm = "los angeles clippers"
        return norm, _display_from_norm(norm)

    def _canonical_game_for_pair(
        sport: str,
        away_raw: str,
        home_raw: str,
    ) -> tuple[str, str, str, str, str]:
        """
        Return (away_norm, home_norm, away_disp, home_disp, canonical_game)
        where canonical_* is chosen by fuzzy-matching to previously seen pairs.
        """
        away_norm, away_disp = _norm_and_display(away_raw)
        home_norm, home_disp = _norm_and_display(home_raw)

        # Optional contextual mapping for ambiguous LA city labels.
        away_norm, away_disp = _maybe_contextualize_la(away_raw, home_raw, sport)
        home_norm, home_disp = _maybe_contextualize_la(home_raw, away_raw, sport)

        sport_str = str(sport or "")
        for cp in canonical_pairs:
            if cp.get("sport") != sport_str:
                continue
            if (
                _team_match_score(away_raw, cp.get("away_disp", "")) >= _FUZZY_MATCH_THRESHOLD
                and _team_match_score(home_raw, cp.get("home_disp", "")) >= _FUZZY_MATCH_THRESHOLD
            ):
                canonical_game = cp.get("game") or ""
                return (
                    cp.get("away_norm", ""),
                    cp.get("home_norm", ""),
                    cp.get("away_disp", ""),
                    cp.get("home_disp", ""),
                    canonical_game,
                )

        canonical_game = f"{away_disp} @ {home_disp}"
        canonical_pairs.append({
            "sport": sport_str,
            "away_norm": away_norm,
            "home_norm": home_norm,
            "away_disp": away_disp,
            "home_disp": home_disp,
            "game": canonical_game,
        })
        return away_norm, home_norm, away_disp, home_disp, canonical_game

    ml_map: dict[tuple[Any, ...], dict[str, Any]] = {}
    spread_map: dict[tuple[Any, ...], dict[str, Any]] = {}
    total_map: dict[tuple[Any, ...], dict[str, Any]] = {}
    prop_map: dict[tuple[Any, ...], dict[str, Any]] = {}

    for source in sources:
        for bl in (source or []):
            if not isinstance(bl, dict):
                continue

            btype = bl.get("type")
            sport = bl.get("sport", "")
            line = bl.get("line")
            game_raw = bl.get("game", "") or ""

            away_norm = home_norm = ""
            away_disp = home_disp = ""
            canonical_game = ""

            if btype == "moneyline":
                away_raw = bl.get("away_team", "") or ""
                home_raw = bl.get("home_team", "") or ""
                away_norm, home_norm, away_disp, home_disp, canonical_game = _canonical_game_for_pair(
                    sport, away_raw, home_raw
                )
                if not away_norm or not home_norm:
                    continue
                key = (sport, away_norm, home_norm, "moneyline")
                existing = ml_map.get(key)
                if existing is None:
                    merged = dict(bl)
                    merged["game"] = canonical_game
                    merged["home_team"] = home_disp
                    merged["away_team"] = away_disp
                    merged["home"] = dict(bl.get("home") or {})
                    merged["away"] = dict(bl.get("away") or {})
                    ml_map[key] = merged
                else:
                    existing["home"] = _best_of(existing.get("home"), bl.get("home"))
                    existing["away"] = _best_of(existing.get("away"), bl.get("away"))
                    existing["home_team"] = home_disp
                    existing["away_team"] = away_disp
                    existing["game"] = canonical_game

            elif btype in {"spread", "total", "prop"}:
                away_raw, home_raw = _parse_game_away_home(game_raw)
                away_norm, home_norm, away_disp, home_disp, canonical_game = _canonical_game_for_pair(
                    sport, away_raw, home_raw
                )
                if not away_norm or not home_norm:
                    continue

                if btype == "spread":
                    side = bl.get("side", "")
                    key = (sport, away_norm, home_norm, "spread", line, side)
                    existing = spread_map.get(key)
                    if existing is None:
                        merged = dict(bl)
                        merged["game"] = canonical_game
                        merged["team"] = home_disp if side == "home" else away_disp
                        spread_map[key] = merged
                    else:
                        existing["pick"] = _best_of(existing.get("pick"), bl.get("pick"))
                        existing["game"] = canonical_game
                        existing["team"] = home_disp if side == "home" else away_disp

                elif btype == "total":
                    key = (sport, away_norm, home_norm, "total", line)
                    existing = total_map.get(key)
                    if existing is None:
                        merged = dict(bl)
                        merged["game"] = canonical_game
                        total_map[key] = merged
                    else:
                        existing["over"] = _best_of(existing.get("over"), bl.get("over"))
                        existing["under"] = _best_of(existing.get("under"), bl.get("under"))
                        existing["game"] = canonical_game

                else:  # prop
                    player = bl.get("player", "")
                    prop_type = bl.get("prop_type", "")
                    key = (sport, away_norm, home_norm, "prop", player, prop_type, line)
                    existing = prop_map.get(key)
                    if existing is None:
                        merged = dict(bl)
                        merged["game"] = canonical_game
                        prop_map[key] = merged
                    else:
                        existing["over"] = _best_of(existing.get("over"), bl.get("over"))
                        existing["under"] = _best_of(existing.get("under"), bl.get("under"))
                        existing["game"] = canonical_game

    # ── Sanity filter: reject moneyline entries with impossible implied
    #    probability.  For any valid moneyline, the sum of implied
    #    probabilities must be ≥ 1.0 (= 1.0 for fair, > 1.0 with vig).
    #    When both sides show big positive odds, the sum drops well below
    #    1.0, which is mathematically impossible and means prices from
    #    different teams were mixed up. ──
    def _implied_prob(american: int | float) -> float:
        """Convert American odds to implied probability."""
        am = float(american)
        if am > 0:
            return 100.0 / (am + 100.0)
        else:
            return abs(am) / (abs(am) + 100.0)

    sane_ml: list[dict[str, Any]] = []
    for entry in ml_map.values():
        home_info = entry.get("home")
        away_info = entry.get("away")
        if home_info and away_info:
            h_am = home_info.get("odds_am")
            a_am = away_info.get("odds_am")
            if isinstance(h_am, (int, float)) and isinstance(a_am, (int, float)):
                ip_sum = _implied_prob(h_am) + _implied_prob(a_am)
                if ip_sum < 0.90:
                    print(
                        f"  BEST_LINES_REJECT: implied prob sum={ip_sum:.3f} < 0.90 — "
                        f"{entry.get('game', '?')} home={int(h_am):+d} ({home_info.get('book','?')}) "
                        f"away={int(a_am):+d} ({away_info.get('book','?')}). Dropping."
                    )
                    continue
        sane_ml.append(entry)

    return sane_ml + list(spread_map.values()) + list(total_map.values()) + list(prop_map.values())


def _kalshi_prices_are_plausible(
    kalshi_home_am: int,
    kalshi_away_am: int,
    sportsbook_home_am: int | None,
    sportsbook_away_am: int | None,
) -> bool:
    """
    Sanity check that Kalshi and sportsbook baseline agree on favorite direction.
    """
    if sportsbook_home_am is None or sportsbook_away_am is None:
        return True

    kalshi_home_is_fav = kalshi_home_am < 0
    sportsbook_home_is_fav = sportsbook_home_am < 0
    if kalshi_home_is_fav != sportsbook_home_is_fav:
        if abs(sportsbook_home_am) > 150:
            return False
    return True


def _bovada_events_to_raw_lines(bovada_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    market_kind_map = {"moneyline": "ml", "spread": "spread", "total": "total"}
    side_map = {"home": "Home", "away": "Away", "over": "Over", "under": "Under"}

    for event in bovada_events:
        sport = _BOVADA_TO_DISPLAY_SPORT.get(str(event.get("sport", "")).lower(), str(event.get("sport", "")).upper())
        home = str(event.get("home_team", ""))
        away = str(event.get("away_team", ""))
        game = f"{away} @ {home}".strip()

        for market in event.get("markets", []):
            market_type = str(market.get("market_type", "")).lower()
            market_kind = market_kind_map.get(market_type)
            if market_kind is None:
                continue

            line_value = market.get("line_value")
            if market_kind == "ml":
                line_label = "ML"
            elif line_value is None:
                line_label = ""
            else:
                line_label = f"{float(line_value):g}"

            odds_am = market.get("american_odds")
            if not isinstance(odds_am, int):
                continue

            lines.append(
                {
                    "sport": sport,
                    "game": game,
                    "market_kind": market_kind,
                    "line_label": line_label,
                    "side": side_map.get(str(market.get("selection", "")).lower(), str(market.get("selection", "")).title()),
                    "book": "Bovada",
                    "odds_am": odds_am,
                    "updated_at": None,
                }
            )

    return lines


def _source_events_to_best_lines(
    events: list[dict[str, Any]],
    book_name: str,
    sport_display_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Convert flat source events into best-lines entries for any book."""
    best_lines: list[dict[str, Any]] = []
    for event in events:
        raw_sport = str(event.get("sport", "")).lower()
        if sport_display_map:
            sport = sport_display_map.get(raw_sport, raw_sport.upper())
        else:
            sport = raw_sport.upper()
        home_team = str(event.get("home_team", ""))
        away_team = str(event.get("away_team", ""))
        game = f"{away_team} @ {home_team}".strip()
        markets: list[dict[str, Any]] = [m for m in event.get("markets", []) if isinstance(m, dict)]
        if not markets:
            continue

        moneyline_home = _best_american_option(
            [m for m in markets if m.get("market_type") == "moneyline" and str(m.get("selection")).lower() == "home"]
        )
        moneyline_away = _best_american_option(
            [m for m in markets if m.get("market_type") == "moneyline" and str(m.get("selection")).lower() == "away"]
        )
        evt_url = str(event.get("event_url") or "")
        if moneyline_home and moneyline_away:
            best_lines.append(
                {
                    "type": "moneyline",
                    "sport": sport,
                    "game": game,
                    "home_team": home_team,
                    "away_team": away_team,
                    "home": {"book": book_name, "odds_am": int(moneyline_home["american_odds"]), "url": evt_url},
                    "away": {"book": book_name, "odds_am": int(moneyline_away["american_odds"]), "url": evt_url},
                }
            )

        # Spread: one record per signed side and line.
        for spread in [m for m in markets if m.get("market_type") == "spread"]:
            odds_am = spread.get("american_odds")
            if not isinstance(odds_am, int):
                continue
            line_value = spread.get("line_value")
            side = str(spread.get("selection", "")).lower()
            if side not in {"home", "away"}:
                continue
            team = home_team if side == "home" else away_team
            best_lines.append(
                {
                    "type": "spread",
                    "line": line_value,
                    "side": side,
                    "team": team,
                    "sport": sport,
                    "game": game,
                    "pick": {"book": book_name, "odds_am": odds_am, "url": evt_url},
                }
            )

        # Totals: pair over/under by line value.
        totals_by_line: dict[float, dict[str, list[dict[str, Any]]]] = {}
        for total in [m for m in markets if m.get("market_type") == "total"]:
            line_value = total.get("line_value")
            if line_value is None:
                continue
            try:
                key = float(line_value)
            except (TypeError, ValueError):
                continue
            bucket = totals_by_line.setdefault(key, {"over": [], "under": []})
            sel = str(total.get("selection", "")).lower()
            if sel in bucket:
                bucket[sel].append(total)

        for line_value, buckets in totals_by_line.items():
            best_over = _best_american_option(buckets["over"])
            best_under = _best_american_option(buckets["under"])
            if not best_over or not best_under:
                continue
            best_lines.append(
                {
                    "type": "total",
                    "line": line_value,
                    "sport": sport,
                    "game": game,
                    "over": {"book": book_name, "odds_am": int(best_over["american_odds"]), "url": evt_url},
                    "under": {"book": book_name, "odds_am": int(best_under["american_odds"]), "url": evt_url},
                }
            )

    return best_lines


def _bovada_events_to_best_lines(bovada_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _source_events_to_best_lines(bovada_events, "Bovada", _BOVADA_TO_DISPLAY_SPORT)


# ──────────────────────────────────────────────
# UNIFIED GAME POOL (PRD v1.0)
# ──────────────────────────────────────────────

def _norm(name: str) -> str:
    """Thin wrapper — reuses existing _normalize_team_name."""
    return _normalize_team_name(name or "")


def _flat_markets_to_nested(flat_markets: list[dict], book_id: int) -> list[dict]:
    """
    Convert flat Kalshi/Polymarket/Bovada market dicts into the nested
    participants -> lines -> prices shape that build_market_index expects.
    """
    # Polymarket: exclude any market where prediction_markets could not
    # resolve a real CLOB ask and we only have a Gamma midpoint placeholder.
    flat_markets = [
        m
        for m in flat_markets
        if not (isinstance(m, dict) and m.get("_price_is_stale_midprice"))
    ]

    nested: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    bid = str(book_id)

    ml_home = next((m for m in flat_markets
                    if str(m.get("market_type", "")).lower() == "moneyline"
                    and str(m.get("selection", "")).lower() == "home"), None)
    ml_away = next((m for m in flat_markets
                    if str(m.get("market_type", "")).lower() == "moneyline"
                    and str(m.get("selection", "")).lower() == "away"), None)
    if ml_home and ml_away:
        nested.append({
            "market_id": 1,
            "period_id": 0,
            "name": "moneyline",
            "participants": [
                {"name": "Away", "lines": [{"prices": {bid: {
                    "price": ml_away["american_odds"],
                    "updated_at": ml_away.get("updated_at") or now,
                    "_event_url": ml_away.get("_event_url", ""),
                }}}]},
                {"name": "Home", "lines": [{"prices": {bid: {
                    "price": ml_home["american_odds"],
                    "updated_at": ml_home.get("updated_at") or now,
                    "_event_url": ml_home.get("_event_url", ""),
                }}}]},
            ],
        })

    spread_by_abs: dict[float, dict[str, dict]] = {}
    total_by_line: dict[float, dict[str, dict]] = {}
    for m in flat_markets:
        mt = str(m.get("market_type", "")).lower()
        sel = str(m.get("selection", "")).lower()
        lv = m.get("line_value")
        if mt == "spread" and lv is not None and sel in ("home", "away"):
            try:
                spread_by_abs.setdefault(abs(float(lv)), {})[sel] = m
            except (TypeError, ValueError):
                pass
        elif mt == "total" and lv is not None and sel in ("over", "under"):
            try:
                total_by_line.setdefault(float(lv), {})[sel] = m
            except (TypeError, ValueError):
                pass

    for abs_lv, sides in spread_by_abs.items():
        sp_home = sides.get("home")
        sp_away = sides.get("away")
        if not sp_home or not sp_away:
            continue
        home_lv = float(sp_home.get("line_value", -abs_lv))
        away_lv = float(sp_away.get("line_value", abs_lv))
        nested.append({
            "market_id": 2,
            "period_id": 0,
            "name": "spread",
            "participants": [
                {"name": "Away", "lines": [{"value": away_lv, "prices": {bid: {
                    "price": sp_away["american_odds"],
                    "updated_at": sp_away.get("updated_at") or now,
                    "_event_url": sp_away.get("_event_url", ""),
                }}}]},
                {"name": "Home", "lines": [{"value": home_lv, "prices": {bid: {
                    "price": sp_home["american_odds"],
                    "updated_at": sp_home.get("updated_at") or now,
                    "_event_url": sp_home.get("_event_url", ""),
                }}}]},
            ],
        })

    for lv, sides in total_by_line.items():
        t_over = sides.get("over")
        t_under = sides.get("under")
        if not t_over or not t_under:
            continue
        nested.append({
            "market_id": 3,
            "period_id": 0,
            "name": "total",
            "participants": [
                {"name": "Over", "lines": [{"value": lv, "prices": {bid: {
                    "price": t_over["american_odds"],
                    "updated_at": t_over.get("updated_at") or now,
                    "_event_url": t_over.get("_event_url", ""),
                }}}]},
                {"name": "Under", "lines": [{"value": lv, "prices": {bid: {
                    "price": t_under["american_odds"],
                    "updated_at": t_under.get("updated_at") or now,
                    "_event_url": t_under.get("_event_url", ""),
                }}}]},
            ],
        })

    return nested


def _inject_matching_markets(
    bucket:        dict,
    source_events: list[dict],
    rd_home:       str,
    rd_away:       str,
    threshold:     int,
    book_id:       int,
) -> None:
    """
    Find the best-matching event in source_events for (rd_home, rd_away)
    and append its markets (converted to nested format) into the enriched
    event's markets list.  Handles home/away swap.
    """
    best_score  = 0
    best_markets: list[dict] = []
    best_swapped = False

    # ── Start-time guard ─────────────────────────────────────────────
    _MAX_DIFF_S = 1 * 3600
    bucket_st_raw = bucket.get("start_time")
    bucket_st: datetime | None = None
    if bucket_st_raw:
        try:
            if isinstance(bucket_st_raw, (int, float)):
                ts = float(bucket_st_raw)
                if ts > 1e10:
                    ts /= 1000
                bucket_st = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                bucket_st = datetime.fromisoformat(str(bucket_st_raw).replace("Z", "+00:00"))
        except Exception:
            pass

    for src in source_events:
        sh = _norm(src.get("home_team") or "")
        sa = _norm(src.get("away_team") or "")
        if not sh or not sa:
            continue

        # ── Start-time guard: skip if start_times differ by > 12h ────
        if bucket_st is not None:
            src_st_raw = src.get("start_time")
            if src_st_raw:
                try:
                    if isinstance(src_st_raw, (int, float)):
                        ts2 = float(src_st_raw)
                        if ts2 > 1e10:
                            ts2 /= 1000
                        src_st = datetime.fromtimestamp(ts2, tz=timezone.utc)
                    else:
                        src_st = datetime.fromisoformat(str(src_st_raw).replace("Z", "+00:00"))
                    if abs((bucket_st - src_st).total_seconds()) > _MAX_DIFF_S:
                        continue
                except Exception:
                    pass

        score_normal  = min(_team_match_score(rd_home, sh), _team_match_score(rd_away, sa))
        score_swapped = min(_team_match_score(rd_home, sa), _team_match_score(rd_away, sh))
        top = max(score_normal, score_swapped)

        if top > best_score:
            best_score   = top
            best_markets = list(src.get("markets") or [])
            best_swapped = score_swapped > score_normal

    if best_score < threshold or not best_markets:
        return

    if best_swapped:
        best_markets = [_flip_market(m) for m in best_markets]

    # Store flat reference markets from reliable sources (Bovada/Novig) so
    # the Kalshi plausibility check can use them when sportsbook data is missing.
    if book_id in (BOVADA_BOOK_ID, NOVIG_BOOK_ID, OG_BOOK_ID):
        bucket.setdefault("_flat_reference_markets", []).extend(best_markets)

    if book_id in (KALSHI_BOOK_ID, POLYMARKET_BOOK_ID):
        sb_home_am: int | None = None
        sb_away_am: int | None = None
        raw_evt = bucket.get("_enriched_event") or {}

        # Resolve home/away team names for name-based participant matching
        # (TheRundown participant ordering is NOT guaranteed).
        _rd_home_lc = rd_home.lower().strip()
        _rd_away_lc = rd_away.lower().strip()

        for mkt in (raw_evt.get("markets") or []):
            if mkt.get("market_id") == 1 and mkt.get("period_id") == 0:
                for idx, p in enumerate(mkt.get("participants") or []):
                    pname = (p.get("name") or "").lower().strip()
                    # Determine side by name matching first, then idx fallback
                    if _rd_home_lc and (_rd_home_lc in pname or pname in _rd_home_lc):
                        side = "home"
                    elif _rd_away_lc and (_rd_away_lc in pname or pname in _rd_away_lc):
                        side = "away"
                    else:
                        side = "away" if idx == 0 else "home"

                    prices = (p.get("lines") or [{}])[0].get("prices") or {}
                    for book_id_str, price_obj in prices.items():
                        try:
                            src_book_id = int(book_id_str)
                            book_name = therundown.KNOWN_BOOKS.get(src_book_id, "")
                            if book_name.lower() in ("betmgm", "draftkings", "fanduel"):
                                am = int(price_obj.get("price", 0))
                                if side == "away":
                                    sb_away_am = am
                                else:
                                    sb_home_am = am
                        except Exception:
                            pass

        kalshi_home = next(
            (m.get("american_odds") for m in best_markets if m.get("market_type") == "moneyline" and m.get("selection") == "home"),
            None,
        )
        kalshi_away = next(
            (m.get("american_odds") for m in best_markets if m.get("market_type") == "moneyline" and m.get("selection") == "away"),
            None,
        )
        if isinstance(kalshi_home, int) and isinstance(kalshi_away, int):
            # Fallback: if no sportsbook reference, use Bovada/Novig as reference
            if sb_home_am is None or sb_away_am is None:
                for ref_m in bucket.get("_flat_reference_markets", []):
                    if ref_m.get("market_type") == "moneyline":
                        if ref_m.get("selection") == "home" and sb_home_am is None:
                            sb_home_am = ref_m.get("american_odds")
                        elif ref_m.get("selection") == "away" and sb_away_am is None:
                            sb_away_am = ref_m.get("american_odds")

            if not _kalshi_prices_are_plausible(kalshi_home, kalshi_away, sb_home_am, sb_away_am):
                src_name = "Kalshi" if book_id == KALSHI_BOOK_ID else "Polymarket"
                print(
                    f"  {src_name}_SWAP_DETECTED [{bucket.get('home_team')} vs {bucket.get('away_team')}]: "
                    f"{src_name} home={kalshi_home} away={kalshi_away} vs ref home={sb_home_am} away={sb_away_am}. "
                    f"Swapping {src_name} prices."
                )
                flip = {"home": "away", "away": "home"}
                best_markets = [
                    {**m, "selection": flip.get(m.get("selection"), m.get("selection"))}
                    for m in best_markets
                ]

    nested_markets = _flat_markets_to_nested(best_markets, book_id)
    enriched = bucket["_enriched_event"]
    existing = enriched.get("markets") or []
    if not isinstance(existing, list):
        existing = []
    enriched["markets"] = existing + nested_markets

    src_name = therundown.KNOWN_BOOKS.get(book_id, "unknown")
    print(
        f"  MATCH [{src_name}] score={best_score:.0f}: "
        f"{bucket['home_team']} vs {bucket['away_team']}"
        + (" [swapped]" if best_swapped else "")
    )


def _build_unified_game_pool(
    rundown_events: list[dict],
    bovada_events:  list[dict],
    kalshi_events:  list[dict],
    poly_events:    list[dict],
    novig_events:   list[dict] | None = None,
    og_events:      list[dict] | None = None,
    match_threshold: int = 85,
) -> list[dict]:
    """
    Match events across all sources into unified game buckets.

    Each bucket wraps a shallow copy of the TheRundown raw event (the anchor)
    and injects nested-format markets from every matched source so
    therundown.analyze_event can process them all in one pass.
    """
    if novig_events is None:
        novig_events = []
    if og_events is None:
        og_events = []
    buckets: list[dict] = []

    for rd_evt in rundown_events:
        rd_home = _norm(rd_evt.get("home_team") or "")
        rd_away = _norm(rd_evt.get("away_team") or "")
        if not rd_home or not rd_away:
            continue

        raw_event = rd_evt.get("_raw_event")
        if not raw_event:
            continue

        enriched_event = dict(raw_event)
        enriched_event["markets"] = list(raw_event.get("markets") or [])

        bucket = {
            "sport":      rd_evt.get("sport", ""),
            "home_team":  rd_evt.get("home_team", ""),
            "away_team":  rd_evt.get("away_team", ""),
            "start_time": rd_evt.get("start_time"),
            "_enriched_event": enriched_event,
        }

        for source_list, bid in [
            (bovada_events, BOVADA_BOOK_ID),
            (novig_events, NOVIG_BOOK_ID),
            (kalshi_events, KALSHI_BOOK_ID),
            (poly_events, POLYMARKET_BOOK_ID),
            (og_events, OG_BOOK_ID),
        ]:
            _inject_matching_markets(bucket, source_list, rd_home, rd_away,
                                     match_threshold, bid)

        buckets.append(bucket)

    return buckets


def _run_arbs_on_pool(
    buckets: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Run therundown.analyze_event on every unified game bucket.
    Returns (all_arbs, all_raw_lines, all_best_lines).
    Only returns true arbs (profit > 0).
    """
    all_arbs:  list[dict] = []
    all_lines: list[dict] = []
    all_best:  list[dict] = []

    for bucket in buckets:
        sport_name = str(bucket.get("sport", "")).upper()
        enriched_event = bucket.get("_enriched_event")
        if not enriched_event:
            continue

        arbs, raw_lines = therundown.analyze_event(enriched_event, sport_name)
        true_arbs = [a for a in arbs if a.get("profit", 0) > 0]
        all_arbs.extend(true_arbs)
        all_lines.extend(raw_lines)

        best = therundown.compute_best_lines_for_event(enriched_event, sport_name)
        all_best.extend(best)

    return all_arbs, all_lines, all_best


def _source_events_to_raw_lines(
    events: list[dict[str, Any]],
    book_name: str,
    sport_label: str,
) -> list[dict]:
    """
    Convert raw source events (Kalshi/Polymarket/Bovada/Novig) directly into
    raw_lines entries.  This guarantees ALL fetched data from every source
    appears in the Raw Lines tab, even if the event didn't match a
    TheRundown hub or form a live-source cluster.
    """
    lines: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for evt in events:
        home = evt.get("home_team") or "?"
        away = evt.get("away_team") or "?"
        game = f"{away} @ {home}"
        evt_sport = str(evt.get("sport") or sport_label).upper()
        for m in evt.get("markets") or []:
            mtype = str(m.get("market_type") or "").lower()
            sel = str(m.get("selection") or "").lower()
            am = m.get("american_odds")
            if am is None:
                continue

            if mtype == "moneyline":
                kind = "ml"
                line_label = "ML"
            elif mtype == "spread":
                kind = "spread"
                lv = m.get("line_value")
                line_label = f"{lv:g}" if lv is not None else ""
            elif mtype == "total":
                kind = "total"
                lv = m.get("line_value")
                line_label = f"{lv:g}" if lv is not None else ""
            else:
                continue

            ct = m.get("_contract_type") or ""
            display_book = f"{book_name} {ct}".strip() if ct else book_name

            lines.append({
                "sport":       evt_sport,
                "game":        game,
                "market_kind": kind,
                "line_label":  line_label,
                "side":        sel.capitalize(),
                "book":        display_book,
                "odds_am":     am,
                "updated_at":  m.get("updated_at") or now_iso,
                "url":         m.get("_event_url") or evt.get("event_url") or "",
            })
    return lines


def _dedup_raw_lines(lines: list[dict]) -> list[dict]:
    """Deduplicate raw lines by (game, book, market_kind, line_label, side, odds_am)."""
    seen: set[tuple] = set()
    result: list[dict] = []
    for line in lines:
        key = (
            line.get("game", ""),
            line.get("book", ""),
            line.get("market_kind", ""),
            line.get("line_label", ""),
            line.get("side", ""),
            line.get("odds_am"),
        )
        if key not in seen:
            seen.add(key)
            result.append(line)
    return result


def _dedup_arbs(arbs: list[dict]) -> list[dict]:
    """
    Deduplicate by (game, market_kind, line_label, {book_a, book_b}, {side_a, side_b}).
    Keeps the higher-profit record when duplicates exist.
    Books and sides are treated as unordered pairs.
    """
    seen: dict[tuple, dict] = {}
    for arb in arbs:
        books = tuple(sorted([arb.get("book_a", ""), arb.get("book_b", "")]))
        sides = tuple(sorted([arb.get("side_a", ""), arb.get("side_b", "")]))
        key = (
            arb.get("game", ""),
            arb.get("market_kind", ""),
            arb.get("line_label", ""),
            books,
            sides,
        )
        existing = seen.get(key)
        if existing is None or arb.get("profit", 0) > existing.get("profit", 0):
            seen[key] = arb
    return list(seen.values())


def _am_to_dec(am: int | float) -> float:
    """American odds → decimal odds."""
    am = float(am)
    if am >= 100:
        return am / 100.0 + 1.0
    elif am <= -100:
        return 100.0 / abs(am) + 1.0
    return 0.0


def _derive_arbs_from_best_lines(best_lines: list[dict[str, Any]]) -> list[dict]:
    """
    Scan merged best lines for arb opportunities the matching engine missed.

    The Best Lines tab shows "+X% both-sides value" badges computed client-side.
    This function replicates that math server-side and creates arb entries so they
    also appear in the Arbitrage Opportunities tab.
    """
    derived: list[dict] = []

    for bl in best_lines:
        btype = bl.get("type")

        if btype == "moneyline":
            pairs = [("home", "away", "Home", "Away")]
        elif btype == "total":
            pairs = [("over", "under", "Over", "Under")]
        else:
            continue

        for key_a, key_b, label_a, label_b in pairs:
            info_a = bl.get(key_a)
            info_b = bl.get(key_b)
            if not info_a or not info_b:
                continue

            am_a = info_a.get("odds_am")
            am_b = info_b.get("odds_am")
            if not isinstance(am_a, (int, float)) or not isinstance(am_b, (int, float)):
                continue

            book_a = info_a.get("book", "")
            book_b = info_b.get("book", "")
            if book_a == book_b:
                continue  # same-book arbs are not real

            dec_a = _am_to_dec(am_a)
            dec_b = _am_to_dec(am_b)
            if dec_a <= 1.0 or dec_b <= 1.0:
                continue

            arb_sum = (1.0 / dec_a) + (1.0 / dec_b)
            profit = round((1.0 - arb_sum) * 100, 4)

            if profit <= 0.0:
                continue
            if profit > therundown.MAX_PROFIT_CAP:
                continue

            stake_a = round(therundown.TOTAL_STAKE * (1.0 / dec_a) / arb_sum, 2)
            stake_b = round(therundown.TOTAL_STAKE * (1.0 / dec_b) / arb_sum, 2)

            line_val = bl.get("line")
            line_label = "ML" if btype == "moneyline" else (
                f"O/U {line_val}" if line_val is not None else "Total"
            )

            derived.append({
                "sport":        bl.get("sport", ""),
                "game":         bl.get("game", ""),
                "market_kind":  "ml" if btype == "moneyline" else "total",
                "line_label":   line_label,
                "side_a":       label_a,
                "book_a":       book_a,
                "odds_a_am":    am_a,
                "updated_at_a": info_a.get("updated_at"),
                "url_a":        info_a.get("url", ""),
                "side_b":       label_b,
                "book_b":       book_b,
                "odds_b_am":    am_b,
                "updated_at_b": info_b.get("updated_at"),
                "url_b":        info_b.get("url", ""),
                "profit":       profit,
                "stake_a":      stake_a,
                "stake_b":      stake_b,
                "same_book":    False,
                "fresh_age_s":  None,
                "stale_age_s":  None,
                "arb_source":   "best_lines",
            })

    return derived


# ──────────────────────────────────────────────
# CROSS-SOURCE ARB ENGINE
# ──────────────────────────────────────────────

def _market_dict_to_price_entry(
    market: dict[str, Any],
    book_name: str,
    fetch_ts: str,
) -> tuple[tuple, str, dict] | None:
    """
    Convert a bovada/PM market dict to a per_line index entry.

    Returns (index_key, side, price_entry) or None if invalid.

    index_key = ("ml" | "spread" | "total", line_value | None)
    side      = "home" | "away" | "over" | "under"
    price_entry = {"book": str, "price_am": int|float, "price_dec": float, "updated_at": str}
    """
    mtype = str(market.get("market_type") or "").lower()
    sel   = str(market.get("selection")   or "").lower()

    if mtype == "moneyline":
        kind       = "ml"
        line_value = None
    elif mtype == "spread":
        kind       = "spread"
        line_value = market.get("line_value")
    elif mtype == "total":
        kind       = "total"
        line_value = market.get("line_value")
    else:
        return None

    if sel not in ("home", "away", "over", "under"):
        return None

    # Polymarket: exclude markets where Gamma mid-price was not replaced by
    # a resolved CLOB ask (tracked in prediction_markets.update_polymarket_clob_prices).
    if market.get("_price_is_stale_midprice"):
        return None

    price_am  = market.get("american_odds")
    price_dec = market.get("decimal_odds")

    if price_dec is None or price_am is None:
        return None
    try:
        price_dec = float(price_dec)
        price_am  = int(round(float(price_am)))
    except (TypeError, ValueError):
        return None
    if price_dec <= 1.0:
        return None

    updated_at = market.get("updated_at") or fetch_ts

    index_key   = (kind, line_value)
    price_entry = {
        "book":       book_name,
        "price_am":   price_am,
        "price_dec":  price_dec,
        "updated_at": updated_at,
        "_contract_type": market.get("_contract_type"),
        "_source_book": market.get("_source_book"),
        "_selection_for_validation": sel,
        "_price_is_stale_midprice": bool(market.get("_price_is_stale_midprice")),
        "_event_url": market.get("_event_url", ""),
    }
    return index_key, sel, price_entry


def _build_cross_source_per_line_index(
    consolidated: dict[str, Any],
    fetch_ts: str,
) -> tuple[dict, dict]:
    """
    Build a unified per_line index from all sources for one consolidated event.

    TheRundown prices come from build_market_index() on the raw event.
    Bovada/Kalshi/Polymarket prices come from their market dicts.

    Returns (per_line_index, spread_pairs) matching the format that
    therundown.analyze_event already uses.
    """
    per_line: dict[tuple, dict[str, list]] = {}
    spread_pairs: dict[float, dict[str, list]] = {}

    def _add(index_key, side, entry):
        bucket = per_line.setdefault(index_key, {"home": [], "away": [], "over": [], "under": []})
        # Allow multiple entries from same book if contract types differ
        # (e.g. Kalshi YES and Kalshi NO both contribute to the same side)
        ct = entry.get("_contract_type") or ""
        if not any(
            e["book"] == entry["book"] and (e.get("_contract_type") or "") == ct
            for e in bucket.get(side, [])
        ):
            bucket[side].append(entry)
            kind, lv = index_key
            if kind == "spread" and lv is not None:
                abs_lv = abs(lv)
                sp = spread_pairs.setdefault(abs_lv, {"home_minus": [], "away_plus": []})
                if side == "home" and lv < 0:
                    sp["home_minus"].append(entry)
                elif side == "away" and lv > 0:
                    sp["away_plus"].append(entry)

    raw_event = consolidated.get("_raw_event")
    if raw_event:
        try:
            td_index, td_spread_pairs, _, _ = therundown.build_market_index(raw_event)
            for (kind, lv), sides in td_index.items():
                for side_key, entries in sides.items():
                    for entry in entries:
                        _add((kind, lv), side_key, entry)
            for abs_lv, buckets in td_spread_pairs.items():
                sp = spread_pairs.setdefault(abs_lv, {"home_minus": [], "away_plus": []})
                for sub_key, sub_entries in buckets.items():
                    sp[sub_key].extend(e for e in sub_entries
                                       if not any(x["book"] == e["book"] for x in sp[sub_key]))
        except Exception as e:
            logger.warning("build_market_index failed for %s: %s", consolidated.get("home_team"), e)

    _BOOK_LABELS = {
        "bovada":     "Bovada",
        "kalshi":     "Kalshi",
        "polymarket": "Polymarket",
        "novig":      "Novig",
    }
    for source_key, book_label in _BOOK_LABELS.items():
        for market in consolidated.get("books", {}).get(source_key) or []:
            result = _market_dict_to_price_entry(market, book_label, fetch_ts)
            if result:
                index_key, side, entry = result
                _add(index_key, side, entry)

    return per_line, spread_pairs


_RUNDOWN_BOOKS: frozenset[str] = frozenset({
    "BetMGM", "FanDuel", "DraftKings",
    "Sportsbetting", "BetOnline", "LowVig",
    "Unibet", "YouWager", "Intertops", "Matchbook",
})


def _contracts_cover_both_outcomes(entry_a: dict, entry_b: dict) -> bool:
    """
    Validate that two PM contract entries jointly cover both game outcomes.
    """
    type_a = entry_a.get("_contract_type")
    type_b = entry_b.get("_contract_type")
    sel_a = entry_a.get("_selection_for_validation")
    sel_b = entry_b.get("_selection_for_validation")

    # Non-PM or mixed PM/sportsbook pairings remain allowed.
    if not type_a and not type_b:
        return True
    if not type_a or not type_b:
        return True

    same_selection = (sel_a == sel_b)
    same_type = (type_a == type_b)

    if same_selection and same_type:
        return False
    if same_selection and not same_type:
        return True
    if not same_selection and same_type:
        return True
    return False


def _run_cross_source_arb_for_event(
    consolidated: dict[str, Any],
    sport_name: str,
    fetch_ts: str,
) -> tuple[list[dict], list[dict]]:
    """
    Run cross-source arb detection for one consolidated event.

    Returns (arbs, raw_lines).
    Only arbs where at least one leg is from Bovada/Kalshi/Polymarket are returned
    (to avoid duplicating intra-Rundown arbs from scan_arbs_once).
    """
    raw_anchor = consolidated.get("_raw_event")
    if not raw_anchor:
        # Live-source-only buckets don't carry a full TheRundown event object.
        # Use the bucket's known home/away teams so we don't render placeholder
        # labels like "Away @ Home".
        home_name = consolidated.get("home_team", "Home")
        away_name = consolidated.get("away_team", "Away")
        game = f"{away_name} @ {home_name}"
    else:
        try:
            game, home_name, away_name = therundown._resolve_teams(raw_anchor)
        except Exception:
            home_name = consolidated.get("home_team", "?")
            away_name = consolidated.get("away_team", "?")
            game = f"{away_name} @ {home_name}"

    per_line, spread_pairs = _build_cross_source_per_line_index(consolidated, fetch_ts)
    if not per_line:
        return [], []

    arbs: list[dict] = []
    raw_lines: list[dict] = []

    now_utc = datetime.now(timezone.utc)

    def _parse_ts(val):
        try:
            if not val:
                return None
            if isinstance(val, (int, float)):
                ts = float(val)
                if ts > 1e10:
                    ts /= 1000
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        except Exception:
            return None

    def _is_external(book: str) -> bool:
        return book not in _RUNDOWN_BOOKS

    for (kind, line_value), sides in per_line.items():
        if kind == "ml":
            side_a_key, side_b_key = "home", "away"
            label = "Moneyline"
            market_line_label = "ML"
        elif kind == "total":
            side_a_key, side_b_key = "over", "under"
            lbl = f"{line_value:g}" if line_value is not None else ""
            label = f"Total {lbl}"
            market_line_label = lbl
        else:
            continue

        for side_key, side_display in [(side_a_key, side_a_key.capitalize()), (side_b_key, side_b_key.capitalize())]:
            for entry in sides.get(side_key) or []:
                raw_lines.append({
                    "sport":       sport_name,
                    "game":        game,
                    "market_kind": kind,
                    "line_label":  market_line_label,
                    "side":        side_display,
                    "book":        entry["book"],
                    "odds_am":     entry["price_am"],
                    "updated_at":  entry.get("updated_at"),
                    "url":         entry.get("_event_url", ""),
                })

        side_a_list = sides.get(side_a_key) or []
        side_b_list = sides.get(side_b_key) or []
        if not side_a_list or not side_b_list:
            continue

        best_a = max(side_a_list, key=lambda e: e["price_dec"])
        best_b = max(side_b_list, key=lambda e: e["price_dec"])

        ts_a = _parse_ts(best_a.get("updated_at"))
        ts_b = _parse_ts(best_b.get("updated_at"))
        if ts_a is None or ts_b is None:
            continue
        if (now_utc - ts_a).total_seconds() > therundown.ARB_MAX_LINE_AGE_S:
            continue
        if (now_utc - ts_b).total_seconds() > therundown.ARB_MAX_LINE_AGE_S:
            continue

        # Skip if both legs are pure Rundown books
        if not _is_external(best_a["book"]) and not _is_external(best_b["book"]):
            continue

        if kind == "ml" and not _contracts_cover_both_outcomes(best_a, best_b):
            logger.debug(
                "Skipping invalid PM pair: %s/%s/%s vs %s/%s/%s",
                best_a.get("book"),
                best_a.get("_contract_type"),
                best_a.get("_selection_for_validation"),
                best_b.get("book"),
                best_b.get("_contract_type"),
                best_b.get("_selection_for_validation"),
            )
            continue

        dec_a  = best_a["price_dec"]
        dec_b  = best_b["price_dec"]
        arb    = (1 / dec_a) + (1 / dec_b)
        profit = round((1 - arb) * 100, 4)

        if profit < therundown.ARB_THRESHOLD:
            continue
        if profit > therundown.MAX_PROFIT_CAP:
            continue

        stake_a = round(therundown.TOTAL_STAKE * (1 / dec_a) / arb, 2)
        stake_b = round(therundown.TOTAL_STAKE * (1 / dec_b) / arb, 2)

        ts_list = [t for t in (ts_a, ts_b) if t is not None]
        fresh_age_s = max((now_utc - t).total_seconds() for t in ts_list) if ts_list else None
        stale_age_s = min((now_utc - t).total_seconds() for t in ts_list) if ts_list else None

        arbs.append({
            "sport":        sport_name,
            "game":         game,
            "market_kind":  kind,
            "market_label": label,
            "line_value":   line_value,
            "line_label":   market_line_label,
            "side_a":       side_a_key.capitalize(),
            "book_a":       best_a["book"],
            "odds_a_am":    best_a["price_am"],
            "odds_a_dec":   dec_a,
            "updated_at_a": best_a.get("updated_at"),
            "url_a":        best_a.get("_event_url", ""),
            "side_b":       side_b_key.capitalize(),
            "book_b":       best_b["book"],
            "odds_b_am":    best_b["price_am"],
            "odds_b_dec":   dec_b,
            "updated_at_b": best_b.get("updated_at"),
            "url_b":        best_b.get("_event_url", ""),
            "arb_pct":      round(arb, 6),
            "profit":       profit,
            "stake_a":      stake_a,
            "stake_b":      stake_b,
            "is_arb":       profit > 0,
            "fresh_age_s":  fresh_age_s,
            "stale_age_s":  stale_age_s,
            "same_book":    best_a["book"] == best_b["book"],
        })

    for abs_lv, buckets in spread_pairs.items():
        home_minus = buckets.get("home_minus") or []
        away_plus  = buckets.get("away_plus")  or []

        for entry in home_minus:
            raw_lines.append({
                "sport":       sport_name,
                "game":        game,
                "market_kind": "spread",
                "line_label":  f"{abs_lv:g}",
                "side":        "Home",
                "book":        entry["book"],
                "odds_am":     entry["price_am"],
                "updated_at":  entry.get("updated_at"),
                "url":         entry.get("_event_url", ""),
            })
        for entry in away_plus:
            raw_lines.append({
                "sport":       sport_name,
                "game":        game,
                "market_kind": "spread",
                "line_label":  f"{abs_lv:g}",
                "side":        "Away",
                "book":        entry["book"],
                "odds_am":     entry["price_am"],
                "updated_at":  entry.get("updated_at"),
                "url":         entry.get("_event_url", ""),
            })

        if not home_minus or not away_plus:
            continue

        best_home = max(home_minus, key=lambda e: e["price_dec"])
        best_away = max(away_plus,  key=lambda e: e["price_dec"])

        ts_home = _parse_ts(best_home.get("updated_at"))
        ts_away = _parse_ts(best_away.get("updated_at"))
        if ts_home is None or ts_away is None:
            continue
        if (now_utc - ts_home).total_seconds() > therundown.ARB_MAX_LINE_AGE_S:
            continue
        if (now_utc - ts_away).total_seconds() > therundown.ARB_MAX_LINE_AGE_S:
            continue

        if not _is_external(best_home["book"]) and not _is_external(best_away["book"]):
            continue

        dec_h  = best_home["price_dec"]
        dec_a  = best_away["price_dec"]
        arb    = (1 / dec_h) + (1 / dec_a)
        profit = round((1 - arb) * 100, 4)

        if profit < therundown.ARB_THRESHOLD or profit > therundown.MAX_PROFIT_CAP:
            continue

        stake_h = round(therundown.TOTAL_STAKE * (1 / dec_h) / arb, 2)
        stake_a = round(therundown.TOTAL_STAKE * (1 / dec_a) / arb, 2)

        arbs.append({
            "sport":        sport_name,
            "game":         game,
            "market_kind":  "spread",
            "market_label": f"Spread {abs_lv:g}",
            "line_value":   abs_lv,
            "line_label":   f"{abs_lv:g}",
            "side_a":       "Home",
            "book_a":       best_home["book"],
            "odds_a_am":    best_home["price_am"],
            "odds_a_dec":   dec_h,
            "updated_at_a": best_home.get("updated_at"),
            "url_a":        best_home.get("_event_url", ""),
            "side_b":       "Away",
            "book_b":       best_away["book"],
            "odds_b_am":    best_away["price_am"],
            "odds_b_dec":   dec_a,
            "updated_at_b": best_away.get("updated_at"),
            "url_b":        best_away.get("_event_url", ""),
            "arb_pct":      round(arb, 6),
            "profit":       profit,
            "stake_a":      stake_h,
            "stake_b":      stake_a,
            "is_arb":       profit > 0,
            "fresh_age_s":  None,
            "stale_age_s":  None,
            "same_book":    best_home["book"] == best_away["book"],
        })

    return arbs, raw_lines


def _run_live_source_arbs(
    bovada_events: list[dict[str, Any]],
    kalshi_events: list[dict[str, Any]],
    poly_events: list[dict[str, Any]],
    novig_events: list[dict[str, Any]] | None = None,
    og_events: list[dict[str, Any]] | None = None,
    match_threshold: int = 85,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Detect arbs across live sources directly (Bovada/Kalshi/Polymarket/Novig/OG),
    without requiring a TheRundown anchor event.
    """
    if novig_events is None:
        novig_events = []
    if og_events is None:
        og_events = []
    fetch_ts = datetime.now(timezone.utc).isoformat()
    all_arbs: list[dict] = []
    all_lines: list[dict] = []
    all_best: list[dict] = []

    def _to_matchable(events: list[dict[str, Any]], source_label: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for evt in events:
            home = _norm(evt.get("home_team") or "")
            away = _norm(evt.get("away_team") or "")
            sport = str(evt.get("sport") or "").lower()
            if not home or not away or not sport:
                continue
            out.append({
                "home_norm": home,
                "away_norm": away,
                "sport": sport,
                "home_team": evt.get("home_team", ""),
                "away_team": evt.get("away_team", ""),
                "start_time": evt.get("start_time", ""),
                "markets": evt.get("markets") or [],
                "_source": source_label,
            })
        return out

    all_live = (
        _to_matchable(bovada_events, "bovada")
        + _to_matchable(novig_events or [], "novig")
        + _to_matchable(og_events or [], "og")
        + _to_matchable(kalshi_events, "kalshi")
        + _to_matchable(poly_events, "polymarket")
    )

    # ── Helper: parse a start_time string into a UTC datetime ──────────
    def _parse_start_time(val: Any) -> datetime | None:
        if not val:
            return None
        try:
            if isinstance(val, (int, float)):
                ts = float(val)
                if ts > 1e10:
                    ts /= 1000
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        except Exception:
            return None

    # ── Helper: check if two events' start_times are close enough ────
    _MAX_START_TIME_DIFF_S = 1 * 3600  # 1 hour

    def _start_times_compatible(a: dict, b: dict) -> bool:
        st_a = _parse_start_time(a.get("start_time"))
        st_b = _parse_start_time(b.get("start_time"))
        if st_a is None or st_b is None:
            return True  # allow when data missing
        return abs((st_a - st_b).total_seconds()) <= _MAX_START_TIME_DIFF_S

    # ── Helper: get the favorite direction for moneyline from markets ─
    def _get_home_fav_direction(markets: list[dict]) -> bool | None:
        home_am = None
        away_am = None
        for m in markets:
            if m.get("market_type") == "moneyline":
                sel = m.get("selection")
                am = m.get("american_odds")
                if sel == "home" and isinstance(am, (int, float)):
                    home_am = am
                elif sel == "away" and isinstance(am, (int, float)):
                    away_am = am
        if home_am is not None and away_am is not None:
            return home_am < away_am  # True if home is favorite
        return None

    # ── Helper: plausibility-check PM markets against sportsbook ref ──
    _PM_SOURCES = frozenset({"kalshi", "polymarket"})
    _SB_SOURCES = frozenset({"bovada", "novig"})

    def _apply_plausibility_corrections(cluster: list[dict[str, Any]]) -> None:
        """
        Within a cluster, compare prediction-market members against
        sportsbook members.  If the PM favourite direction disagrees with
        the sportsbook reference (spread > 150), flip the PM markets.
        """
        sb_direction: bool | None = None
        for member in cluster:
            if member.get("_source") in _SB_SOURCES:
                d = _get_home_fav_direction(member.get("markets") or [])
                if d is not None:
                    sb_direction = d
                    break
        if sb_direction is None:
            return

        for member in cluster:
            if member.get("_source") not in _PM_SOURCES:
                continue
            pm_direction = _get_home_fav_direction(member.get("markets") or [])
            if pm_direction is None:
                continue
            if pm_direction != sb_direction:
                # Check spread magnitude — only swap when it's clear-cut
                home_am = next(
                    (m.get("american_odds") for m in member.get("markets") or []
                     if m.get("market_type") == "moneyline" and m.get("selection") == "home"),
                    None,
                )
                if home_am is not None and abs(home_am) > 150:
                    print(
                        f"  LIVE_SWAP_DETECTED [{member.get('_source')}] "
                        f"{member.get('home_team')} vs {member.get('away_team')}: "
                        f"PM home_fav={pm_direction} vs SB home_fav={sb_direction}. Flipping."
                    )
                    flip = {"home": "away", "away": "home"}
                    member["home_norm"], member["away_norm"] = member["away_norm"], member["home_norm"]
                    member["home_team"], member["away_team"] = member["away_team"], member["home_team"]
                    member["markets"] = [
                        {**m, "selection": flip.get(m.get("selection"), m.get("selection"))}
                        for m in member.get("markets") or []
                    ]

    used: set[int] = set()
    clusters: list[list[dict[str, Any]]] = []
    for i, anchor in enumerate(all_live):
        if i in used:
            continue
        cluster = [anchor]
        used.add(i)
        for j, candidate in enumerate(all_live):
            if j in used:
                continue
            if candidate.get("sport") != anchor.get("sport"):
                continue

            # ── Start-time guard: reject cross-date matches ──────────
            if not _start_times_compatible(anchor, candidate):
                continue

            score_direct = min(
                _team_match_score(anchor["home_norm"], candidate["home_norm"]),
                _team_match_score(anchor["away_norm"], candidate["away_norm"]),
            )
            score_swapped = min(
                _team_match_score(anchor["home_norm"], candidate["away_norm"]),
                _team_match_score(anchor["away_norm"], candidate["home_norm"]),
            )
            if max(score_direct, score_swapped) < match_threshold:
                continue

            candidate_adj = candidate
            if score_swapped > score_direct:
                flip = {"home": "away", "away": "home"}
                candidate_adj = dict(candidate)
                candidate_adj["home_norm"], candidate_adj["away_norm"] = candidate["away_norm"], candidate["home_norm"]
                candidate_adj["home_team"], candidate_adj["away_team"] = candidate["away_team"], candidate["home_team"]
                candidate_adj["markets"] = [
                    {**m, "selection": flip.get(m.get("selection"), m.get("selection"))}
                    for m in candidate.get("markets") or []
                ]

            cluster.append(candidate_adj)
            used.add(j)
        if len(cluster) > 1:
            # ── Plausibility check: fix PM side-swaps vs sportsbook ──
            _apply_plausibility_corrections(cluster)
            clusters.append(cluster)

    for cluster in clusters:
        anchor_evt = cluster[0]
        books_dict: dict[str, list[dict]] = {"bovada": [], "kalshi": [], "polymarket": [], "novig": []}
        for member in cluster:
            src = str(member.get("_source") or "")
            if src in books_dict:
                books_dict[src].extend(member.get("markets") or [])

        consolidated = {
            "sport": anchor_evt.get("sport", ""),
            "home_team": anchor_evt.get("home_team", ""),
            "away_team": anchor_evt.get("away_team", ""),
            "start_time": anchor_evt.get("start_time", ""),
            "_raw_event": None,
            "books": books_dict,
        }
        sport_label = str(anchor_evt.get("sport") or "").upper()
        arbs, lines = _run_cross_source_arb_for_event(consolidated, sport_label, fetch_ts)
        all_arbs.extend(a for a in arbs if a.get("profit", 0) > 0)
        all_lines.extend(lines)

    all_arbs.sort(key=lambda r: r.get("profit", 0), reverse=True)
    logger.info("Live-source arbs: %d clusters -> %d arbs", len(clusters), len(all_arbs))
    return all_arbs, all_lines, all_best


def _run_all_cross_source_arbs(
    consolidated_events: list[dict[str, Any]],
) -> tuple[list[dict], list[dict]]:
    """Run cross-source arb engine over all matched events."""
    all_arbs:  list[dict] = []
    all_lines: list[dict] = []
    fetch_ts = datetime.now(timezone.utc).isoformat()

    for event in consolidated_events:
        sport = str(event.get("sport") or "").upper()
        arbs, lines = _run_cross_source_arb_for_event(event, sport, fetch_ts)
        all_arbs.extend(arbs)
        all_lines.extend(lines)

    all_arbs.sort(key=lambda r: r.get("profit", 0), reverse=True)
    return all_arbs, all_lines


async def handle_scan_now(request: web.Request) -> web.Response:
    state: ArbState = request.app["state"]
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    sports = payload.get("sports") if isinstance(payload, dict) else None
    if not isinstance(sports, list):
        return web.json_response(
            {"ok": False, "error": 'Expected JSON body: {"sports": ["NBA", ...]}'},
            status=400,
        )

    name_to_id      = _sport_id_by_name()
    selected_ids:   list[int] = []
    selected_names: list[str] = []
    for s in sports:
        if not isinstance(s, str):
            continue
        sid = name_to_id.get(s.lower())
        if sid is None:
            continue
        selected_ids.append(sid)
        selected_names.append(_sport_name_by_id().get(sid, s))

    if not selected_ids:
        return web.json_response(
            {"ok": False, "error": "No valid sports selected",
             "supported": list(name_to_id.keys())},
            status=400,
        )

    try:
        state.last_error = None

        # ── Step 1: Fetch all 4 sources concurrently ──────────────────
        sport_keys = [
            _RUNDOWN_TO_PM_SPORT[name]
            for name in selected_names
            if name in _RUNDOWN_TO_PM_SPORT
        ]
        rundown_task = asyncio.to_thread(scan_arbs_once, selected_ids)
        bovada_task  = asyncio.wait_for(
            _fetch_bovada_events_for_sports(selected_names),
            timeout=15.0,
        )

        pm_errors: list[str] = []
        kalshi_events: list[dict[str, Any]] = []
        poly_events: list[dict[str, Any]] = []

        pm_task = asyncio.wait_for(
            prediction_markets.fetch_all_prediction_markets(sport_keys),
            timeout=60.0,
        )

        novig_task = asyncio.wait_for(
            _fetch_novig_events_for_sports(selected_names),
            timeout=15.0,
        )

        og_task = asyncio.wait_for(
            _fetch_og_events_for_sports(selected_names),
            timeout=25.0,
        )

        rundown_result, bovada_result, pm_result, novig_result, og_result = await asyncio.gather(
            rundown_task, bovada_task, pm_task, novig_task, og_task, return_exceptions=True,
        )

        if isinstance(pm_result, Exception):
            pm_errors.append(f"PredictionMarkets: {pm_result}")
        else:
            kalshi_events, poly_events = pm_result

        # Fallback to legacy scrapers if the async pipeline is empty/failed.
        need_kalshi = not kalshi_events
        need_poly = not poly_events
        if (need_kalshi or need_poly) and sport_keys:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                kalshi_future = pool.submit(fetch_kalshi_markets, sport_keys) if need_kalshi else None
                poly_future   = pool.submit(fetch_polymarket_markets, sport_keys) if need_poly else None

                if kalshi_future is not None:
                    try:
                        kalshi_events = kalshi_future.result(timeout=30) or []
                    except Exception as e:
                        print(f"KALSHI_WARN: {e}")
                        kalshi_events = []
                        pm_errors.append(f"Kalshi legacy: {e}")

                if poly_future is not None:
                    try:
                        poly_events = poly_future.result(timeout=14) or []
                    except Exception as e:
                        print(f"POLYMARKET_WARN: {e}")
                        poly_events = []
                        pm_errors.append(f"Polymarket legacy: {e}")

        print(
            f"SOURCES: TheRundown=ok  "
            f"Bovada={'ok' if not isinstance(bovada_result, Exception) else 'FAIL'}  "
            f"Kalshi={len(kalshi_events)} events  "
            f"Polymarket={len(poly_events)} events  "
            f"Novig={'ok' if not isinstance(novig_result, Exception) else 'FAIL'}  "
            f"OG={'ok' if not isinstance(og_result, Exception) else 'FAIL'}"
        )

        # ── Step 2: Unpack fetched results independently ───────────────
        if isinstance(rundown_result, Exception):
            logger.warning("TheRundown scan failed: %s", rundown_result)
            rd_arbs, rd_lines, rd_best = [], [], []
        else:
            rd_arbs, rd_lines, rd_best = rundown_result

        bovada_error: str | None = None
        bovada_events: list[dict[str, Any]] = []
        if isinstance(bovada_result, Exception):
            bovada_error = str(bovada_result)
            logger.warning("Bovada scan failed: %s", bovada_error)
        else:
            bovada_events = bovada_result or []

        pm_error: str | None = "; ".join(pm_errors) if pm_errors else None

        novig_events: list[dict[str, Any]] = []
        if isinstance(novig_result, Exception):
            logger.warning("Novig scan failed: %s", novig_result)
        else:
            novig_events = novig_result or []

        og_events: list[dict[str, Any]] = []
        og_error: str | None = None
        if isinstance(og_result, Exception):
            og_error = str(og_result)
            logger.warning("OG scan failed: %s", og_error)
        else:
            og_events = og_result or []

        # Tag every market dict with its parent event_url (idempotent).
        _tag_markets_with_event_url(bovada_events)
        _tag_markets_with_event_url(kalshi_events)
        _tag_markets_with_event_url(poly_events)
        _tag_markets_with_event_url(novig_events)
        _tag_markets_with_event_url(og_events)

        # ── Step 3: Live-source-only arbs (no TheRundown dependency) ───
        live_arbs, live_lines, live_best = _run_live_source_arbs(
            bovada_events=bovada_events,
            kalshi_events=kalshi_events,
            poly_events=poly_events,
            novig_events=novig_events,
            og_events=og_events,
        )

        # ── Step 4: Combined pool with TheRundown as extra book ────────
        rundown_events = _normalize_rundown_events_for_matching(selected_ids)
        game_pool = _build_unified_game_pool(
            rundown_events=rundown_events,
            bovada_events=bovada_events,
            kalshi_events=kalshi_events,
            poly_events=poly_events,
            novig_events=novig_events,
            og_events=og_events,
        )
        cross_arbs, cross_lines, cross_best = _run_arbs_on_pool(game_pool)

        # Tag source provenance for UI display.
        for arb in rd_arbs:
            arb["arb_source"] = "rundown"
        for arb in live_arbs:
            arb["arb_source"] = "live"
        for arb in cross_arbs:
            arb["arb_source"] = "combined"

        # ── Step 5: Merge, dedup, store ───────────────────────────────
        bovada_best_lines = _bovada_events_to_best_lines(bovada_events)
        kalshi_best_lines = _source_events_to_best_lines(kalshi_events, "Kalshi")
        poly_best_lines = _source_events_to_best_lines(poly_events, "Polymarket")
        og_best_lines = _source_events_to_best_lines(og_events, "OG")
        novig_best_lines = _source_events_to_best_lines(novig_events, "Novig")

        # Generate raw lines directly from ALL source events so every
        # fetched line appears in the Raw Lines tab, regardless of matching.
        sport_label = ",".join(selected_names)
        source_raw_lines: list[dict] = []
        source_raw_lines.extend(_source_events_to_raw_lines(kalshi_events, "Kalshi", sport_label))
        source_raw_lines.extend(_source_events_to_raw_lines(poly_events, "Polymarket", sport_label))
        source_raw_lines.extend(_source_events_to_raw_lines(bovada_events, "Bovada", sport_label))
        source_raw_lines.extend(_source_events_to_raw_lines(novig_events, "Novig", sport_label))
        source_raw_lines.extend(_source_events_to_raw_lines(og_events, "OG", sport_label))

        state.best_lines = _merge_best_lines([rd_best, bovada_best_lines, kalshi_best_lines, poly_best_lines, og_best_lines, novig_best_lines, live_best, cross_best])
        best_line_arbs = _derive_arbs_from_best_lines(state.best_lines)
        state.arbs       = _dedup_arbs(rd_arbs + live_arbs + cross_arbs + best_line_arbs)
        state.arbs.sort(key=lambda r: r.get("profit", 0), reverse=True)
        state.lines      = _dedup_raw_lines(
            rd_lines + live_lines + cross_lines + source_raw_lines
        )
        state.last_scan_ms     = _now_ms()
        state.last_scan_sports = selected_names

        print(
            f"  SCAN COMPLETE: {len(rd_arbs)} rundown arbs + "
            f"{len(live_arbs)} live-source arbs + "
            f"{len(cross_arbs)} combined arbs + "
            f"{len(best_line_arbs)} best-line arbs → "
            f"{len(state.arbs)} total (deduped) | "
            f"Kalshi:{len(kalshi_events)} Poly:{len(poly_events)} "
            f"Bovada:{len(bovada_events)} Novig:{len(novig_events)} OG:{len(og_events)} events"
        )

        return web.json_response(
            {
                "ok":           True,
                "sports":       selected_names,
                "lastScanMs":   state.last_scan_ms,
                "count":        len(state.arbs),
                "arbs":         state.arbs,
                "lines":        state.lines,
                "bestLines":    state.best_lines,
                "matchedGames": len(game_pool),
                "sourceCounts": {
                    "therundown": len(rundown_events),
                    "bovada":     len(bovada_events),
                    "kalshi":     len(kalshi_events),
                    "polymarket": len(poly_events),
                    "novig":      len(novig_events),
                    "og":         len(og_events),
                },
                "bovadaError":  bovada_error,
                "pmError":      pm_error,
                "ogError":      og_error,
                "dataAge":      None if _STORE.freshest_update_age() == float("inf")
                                else int(_STORE.freshest_update_age()),
                "dpRemaining":  _STORE.dp_remaining,
            },
            dumps=lambda x: json.dumps(x, default=_serialize),
        )

    except Exception as e:
        state.last_error = str(e)
        logger.exception("handle_scan_now error")
        return web.json_response({"ok": False, "error": state.last_error}, status=500)


async def _send_sse(resp: web.StreamResponse, event: str, data: dict):
    """Write one Server-Sent Event frame."""
    payload = json.dumps(data, default=_serialize)
    await resp.write(f"event: {event}\ndata: {payload}\n\n".encode())


async def handle_scan_now_stream(request: web.Request) -> web.StreamResponse:
    """Progressive SSE scan — fast sources first, then TheRundown."""
    state: ArbState = request.app["state"]

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    sports = payload.get("sports") if isinstance(payload, dict) else None
    if not isinstance(sports, list):
        return web.json_response(
            {"ok": False, "error": 'Expected JSON body: {"sports": ["NBA", ...]}'},
            status=400,
        )

    name_to_id      = _sport_id_by_name()
    selected_ids:   list[int] = []
    selected_names: list[str] = []
    for s in sports:
        if not isinstance(s, str):
            continue
        sid = name_to_id.get(s.lower())
        if sid is None:
            continue
        selected_ids.append(sid)
        selected_names.append(_sport_name_by_id().get(sid, s))

    if not selected_ids:
        return web.json_response(
            {"ok": False, "error": "No valid sports selected"},
            status=400,
        )

    # Open SSE stream
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    await resp.prepare(request)

    try:
        state.last_error = None

        sport_keys = [
            _RUNDOWN_TO_PM_SPORT[name]
            for name in selected_names
            if name in _RUNDOWN_TO_PM_SPORT
        ]

        # Start ALL tasks
        rundown_task = asyncio.create_task(
            asyncio.to_thread(scan_arbs_once, selected_ids)
        )
        bovada_task = asyncio.create_task(asyncio.wait_for(
            _fetch_bovada_events_for_sports(selected_names), timeout=15.0,
        ))
        pm_task = asyncio.create_task(asyncio.wait_for(
            prediction_markets.fetch_all_prediction_markets(sport_keys), timeout=60.0,
        ))
        novig_task = asyncio.create_task(asyncio.wait_for(
            _fetch_novig_events_for_sports(selected_names), timeout=15.0,
        ))
        og_task = asyncio.create_task(asyncio.wait_for(
            _fetch_og_events_for_sports(selected_names), timeout=25.0,
        ))

        # ── Phase 1: Wait for fast sources only ──────────────────────
        fast_tasks = [bovada_task, pm_task, novig_task, og_task]
        done, pending = await asyncio.wait(fast_tasks, timeout=20.0, return_when=asyncio.ALL_COMPLETED)

        # Unpack fast results
        pm_errors: list[str] = []
        kalshi_events: list[dict[str, Any]] = []
        poly_events: list[dict[str, Any]] = []

        pm_result = pm_task.result() if pm_task.done() and not pm_task.cancelled() else pm_task.exception() if pm_task.done() else TimeoutError("PM timed out")
        try:
            pm_result = pm_task.result()
        except Exception as e:
            pm_result = e

        if isinstance(pm_result, Exception):
            pm_errors.append(f"PredictionMarkets: {pm_result}")
        else:
            kalshi_events, poly_events = pm_result

        # Kalshi/Poly legacy fallback
        need_kalshi = not kalshi_events
        need_poly = not poly_events
        if (need_kalshi or need_poly) and sport_keys:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                kalshi_future = pool.submit(fetch_kalshi_markets, sport_keys) if need_kalshi else None
                poly_future   = pool.submit(fetch_polymarket_markets, sport_keys) if need_poly else None
                if kalshi_future is not None:
                    try:
                        kalshi_events = kalshi_future.result(timeout=30) or []
                    except Exception as e:
                        kalshi_events = []
                        pm_errors.append(f"Kalshi legacy: {e}")
                if poly_future is not None:
                    try:
                        poly_events = poly_future.result(timeout=14) or []
                    except Exception as e:
                        poly_events = []
                        pm_errors.append(f"Polymarket legacy: {e}")

        bovada_error: str | None = None
        bovada_events: list[dict[str, Any]] = []
        try:
            bov_res = bovada_task.result()
            bovada_events = bov_res or []
        except Exception as e:
            bovada_error = str(e)

        novig_events: list[dict[str, Any]] = []
        try:
            novig_events = novig_task.result() or []
        except Exception as e:
            logger.warning("Novig scan failed: %s", e)

        og_events: list[dict[str, Any]] = []
        og_error: str | None = None
        try:
            og_events = og_task.result() or []
        except Exception as e:
            og_error = str(e)

        pm_error: str | None = "; ".join(pm_errors) if pm_errors else None

        # Tag markets with event URLs
        _tag_markets_with_event_url(bovada_events)
        _tag_markets_with_event_url(kalshi_events)
        _tag_markets_with_event_url(poly_events)
        _tag_markets_with_event_url(novig_events)
        _tag_markets_with_event_url(og_events)

        # Build fast-source arbs + lines + best lines
        live_arbs, live_lines, live_best = _run_live_source_arbs(
            bovada_events=bovada_events,
            kalshi_events=kalshi_events,
            poly_events=poly_events,
            novig_events=novig_events,
            og_events=og_events,
        )
        for arb in live_arbs:
            arb["arb_source"] = "live"

        sport_label = ",".join(selected_names)
        fast_raw_lines: list[dict] = []
        fast_raw_lines.extend(_source_events_to_raw_lines(kalshi_events, "Kalshi", sport_label))
        fast_raw_lines.extend(_source_events_to_raw_lines(poly_events, "Polymarket", sport_label))
        fast_raw_lines.extend(_source_events_to_raw_lines(bovada_events, "Bovada", sport_label))
        fast_raw_lines.extend(_source_events_to_raw_lines(novig_events, "Novig", sport_label))
        fast_raw_lines.extend(_source_events_to_raw_lines(og_events, "OG", sport_label))

        bovada_best = _bovada_events_to_best_lines(bovada_events)
        kalshi_best = _source_events_to_best_lines(kalshi_events, "Kalshi")
        poly_best   = _source_events_to_best_lines(poly_events, "Polymarket")
        og_best     = _source_events_to_best_lines(og_events, "OG")
        novig_best  = _source_events_to_best_lines(novig_events, "Novig")

        fast_best  = _merge_best_lines([bovada_best, kalshi_best, poly_best, og_best, novig_best, live_best])
        fast_bl_arbs = _derive_arbs_from_best_lines(fast_best)
        fast_arbs = _dedup_arbs(live_arbs + fast_bl_arbs)
        fast_arbs.sort(key=lambda r: r.get("profit", 0), reverse=True)
        fast_lines = _dedup_raw_lines(live_lines + fast_raw_lines)

        now_ms = _now_ms()

        # Update state with fast results immediately
        state.arbs = fast_arbs
        state.lines = fast_lines
        state.best_lines = fast_best
        state.last_scan_ms = now_ms
        state.last_scan_sports = selected_names

        print(
            f"  SSE PHASE 1 (fast): {len(live_arbs)} arbs | "
            f"Kalshi:{len(kalshi_events)} Poly:{len(poly_events)} "
            f"Bovada:{len(bovada_events)} Novig:{len(novig_events)} OG:{len(og_events)} events"
        )

        # Send Phase 1 SSE event
        await _send_sse(resp, "scan-update", {
            "phase":        "fast",
            "arbs":         fast_arbs,
            "lines":        fast_lines,
            "bestLines":    fast_best,
            "lastScanMs":   now_ms,
            "sourceCounts": {
                "therundown": 0,
                "bovada":     len(bovada_events),
                "kalshi":     len(kalshi_events),
                "polymarket": len(poly_events),
                "novig":      len(novig_events),
                "og":         len(og_events),
            },
            "bovadaError":  bovada_error,
            "pmError":      pm_error,
            "ogError":      og_error,
            "dataAge":      None if _STORE.freshest_update_age() == float("inf")
                            else int(_STORE.freshest_update_age()),
            "dpRemaining":  _STORE.dp_remaining,
        })

        # ── Phase 2: Wait for TheRundown ─────────────────────────────
        try:
            rundown_result = await asyncio.wait_for(rundown_task, timeout=45.0)
        except Exception as e:
            rundown_result = e

        if isinstance(rundown_result, Exception):
            logger.warning("TheRundown scan failed: %s", rundown_result)
            rd_arbs, rd_lines, rd_best = [], [], []
        else:
            rd_arbs, rd_lines, rd_best = rundown_result

        # Full pipeline with all sources
        rundown_events = _normalize_rundown_events_for_matching(selected_ids)
        game_pool = _build_unified_game_pool(
            rundown_events=rundown_events,
            bovada_events=bovada_events,
            kalshi_events=kalshi_events,
            poly_events=poly_events,
            novig_events=novig_events,
            og_events=og_events,
        )
        cross_arbs, cross_lines, cross_best = _run_arbs_on_pool(game_pool)

        for arb in rd_arbs:
            arb["arb_source"] = "rundown"
        for arb in cross_arbs:
            arb["arb_source"] = "combined"

        all_best  = _merge_best_lines([rd_best, bovada_best, kalshi_best, poly_best, og_best, novig_best, live_best, cross_best])
        all_bl_arbs = _derive_arbs_from_best_lines(all_best)
        all_arbs = _dedup_arbs(rd_arbs + live_arbs + cross_arbs + all_bl_arbs)
        all_arbs.sort(key=lambda r: r.get("profit", 0), reverse=True)
        all_lines = _dedup_raw_lines(rd_lines + live_lines + cross_lines + fast_raw_lines)

        now_ms = _now_ms()
        state.arbs = all_arbs
        state.lines = all_lines
        state.best_lines = all_best
        state.last_scan_ms = now_ms

        print(
            f"  SSE PHASE 2 (complete): {len(rd_arbs)} rundown + "
            f"{len(live_arbs)} live + {len(cross_arbs)} combined → "
            f"{len(all_arbs)} total arbs | "
            f"TheRundown:{len(rundown_events)} events"
        )

        # Send Phase 2 SSE event
        await _send_sse(resp, "scan-update", {
            "phase":        "complete",
            "arbs":         all_arbs,
            "lines":        all_lines,
            "bestLines":    all_best,
            "lastScanMs":   now_ms,
            "matchedGames": len(game_pool),
            "sourceCounts": {
                "therundown": len(rundown_events),
                "bovada":     len(bovada_events),
                "kalshi":     len(kalshi_events),
                "polymarket": len(poly_events),
                "novig":      len(novig_events),
                "og":         len(og_events),
            },
            "bovadaError":  bovada_error,
            "pmError":      pm_error,
            "ogError":      og_error,
            "dataAge":      None if _STORE.freshest_update_age() == float("inf")
                            else int(_STORE.freshest_update_age()),
            "dpRemaining":  _STORE.dp_remaining,
        })

        await _send_sse(resp, "scan-done", {"ok": True})

    except Exception as e:
        state.last_error = str(e)
        logger.exception("handle_scan_now_stream error")
        try:
            await _send_sse(resp, "scan-error", {"error": str(e)})
        except Exception:
            pass

    return resp


async def scan_loop(app: web.Application):
    state: ArbState = app["state"]
    sport_ids = app["sport_ids"]
    interval_s = app["interval_s"]

    while True:
        try:
            state.last_error = None
            arbs, lines, best_lines = scan_arbs_once(sport_ids)
            state.arbs = arbs
            state.lines = lines
            state.best_lines = best_lines
            state.last_scan_ms = _now_ms()
            state.last_scan_sports = [_sport_name_by_id().get(sid, str(sid)) for sid in sport_ids]
        except Exception as e:
            state.last_error = str(e)
        await asyncio.sleep(interval_s)


def create_app(sport_ids: list[int], interval_s: float) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app["state"] = ArbState()
    app["sport_ids"] = sport_ids
    app["interval_s"] = interval_s

    app.router.add_get("/health", handle_health)
    app.router.add_get("/arbs", handle_arbs)
    app.router.add_post("/scan-now", handle_scan_now)
    app.router.add_post("/scan-now-stream", handle_scan_now_stream)

    async def on_startup(app: web.Application):
        if ENABLE_AUTO_SCAN:
            app["scan_task"] = asyncio.create_task(scan_loop(app))

    async def on_cleanup(app: web.Application):
        task = app.get("scan_task")
        if task:
            task.cancel()
            try:
                await task
            except Exception:
                pass

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    import sys
    import traceback
    print("Initialize Server: Checking port binding on 0.0.0.0:3030...")
    try:
        app = create_app(sport_ids=[4], interval_s=5.0)
        web.run_app(app, host="0.0.0.0", port=3030)
    except Exception as e:
        print(f"FATAL STARTUP ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)
