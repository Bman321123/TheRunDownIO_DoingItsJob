"""
TheRundown API - Sports Arbitrage Finder (Full Line Coverage)
=============================================================
python therundown.py              # all sports
python therundown.py 4            # NBA only
python therundown.py 4 6 5        # NBA + NHL + NCAAB
"""

import os
import sys
import time
import datetime
import json
import threading
from functools import wraps
import requests
from prettytable import PrettyTable

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
API_KEY: str = os.getenv(
    "THERUNDOWN_API_KEY",
    "b05e571893ea8db8950f9b6497d480ad7a5547ddcbc5064131a8e3bfe713599d"
)
API_KEYS: list[str] = [
    "10c79a340413a26b827a2d34b0c32e86e7bf5c964eb4d4c3b8e8cadc995d1c58",
    "b05e571893ea8db8950f9b6497d480ad7a5547ddcbc5064131a8e3bfe713599d",
    "80cb027743637f401c17b21854a6faad090619935f63369597ac462a1d35d1cc",
    "e6b5c7622d7411381bfcf2ade1f8776eaa247e22cddcade8ca495062991fac96",
]
_ENV_KEYS_RAW = os.getenv("THERUNDOWN_API_KEYS", "").strip()
if _ENV_KEYS_RAW:
    _parsed = [k.strip() for k in _ENV_KEYS_RAW.replace(";", ",").split(",")]
    API_KEYS = [k for k in _parsed if k]
if API_KEY and API_KEY not in API_KEYS:
    API_KEYS.insert(0, API_KEY)
USE_RAPIDAPI:   bool  = False
TOTAL_STAKE:    float = 100.0
POLL_INTERVAL:  int   = 60
REQUEST_DELAY:  float = 1.2
DAYS_AHEAD:     int   = 1
BEST_LINES_MODE: bool = os.getenv("BEST_LINES_MODE", "0") == "1"

# Live-game / near-arb configuration
LIVE_ONLY:        bool = True
# only consider games that have not started yet (with tolerance)
PRE_GAME_MINUTES: int  = 10     # include games starting within this many minutes of now
SHOW_NEAR_ARBS:   bool = False  # when False, hide near-arb section in output

# Book restriction (use only these books when enabled)
RESTRICT_BOOKS: bool = True
ALLOWED_BOOK_NAMES: set[str] = {"betmgm", "draftkings", "fanduel"}
# Player props are often unavailable from a strict 3-book subset, so allow
# broad book coverage for props by default.
RESTRICT_PROP_BOOKS: bool = os.getenv("RESTRICT_PROP_BOOKS", "0") == "1"
# Primary book for API fetch and coverage checks (BetMGM = 22 per TheRundown docs)
PRIMARY_BOOK_ID: int = 22
PROP_MARKET_IDS: set[int] = {
    29,   # player points
    33,   # player turnovers
    35,   # player rebounds
    38,   # 3pt made
    39,   # player assists
    93,   # player PRA
    98,   # player blocks
    99,   # player points + assists
    297,  # player points + rebounds
    298,  # player rebounds + assists
}
PROP_MARKET_NAMES: dict[int, str] = {
    29: "player_points",
    33: "player_turnovers",
    35: "player_rebounds",
    38: "player_threes_made",
    39: "player_assists",
    93: "player_pra",
    98: "player_blocks",
    99: "player_points_assists",
    297: "player_points_rebounds",
    298: "player_rebounds_assists",
}
CORE_MARKET_IDS: tuple[int, ...] = (1, 2, 3)
DEBUG_BOOK_COVERAGE: bool = False  # print one-line diagnostics when key books missing
ARB_MAX_LINE_AGE_S: int = 1800  # 30 min — both arb legs must be updated within this window

# Only keep true, positive-profit arbs (arb < 1.0)
ARB_THRESHOLD: float = 0.0

# Treat anything above this profit% as suspicious and don't report it as a real arb
MAX_PROFIT_CAP: float = 10.0

# When True, prints extra per-opportunity detail for manual verification
VALIDATION_MODE: bool = False
PROP_DIAGNOSTICS: bool = os.getenv("PROP_DIAGNOSTICS", "0") == "1"

ALL_SPORTS = {
    1: "NCAAF", 2: "NFL",    3: "MLB",
    4: "NBA",   5: "NCAAB",  6: "NHL",
    8: "NCAAWB", 9: "MMA",
}

SPORT_IDS = [int(x) for x in sys.argv[1:]] if len(sys.argv) > 1 else list(ALL_SPORTS.keys())

# Fallback book IDs if affiliates fetch fails (BetMGM = 22 per TheRundown docs)
_KNOWN_BOOKS_FALLBACK = {
    2: "Bovada", 4: "Sportsbetting", 6: "BetOnline",
    11: "LowVig", 12: "Bodog", 14: "Intertops", 16: "Matchbook",
    18: "YouWager", 19: "Draftkings", 21: "Unibet", 22: "BetMGM",
    23: "Fanduel", 25: "Kalshi", 26: "Polymarket",
}

KNOWN_BOOKS: dict[int, str] = dict(_KNOWN_BOOKS_FALLBACK)
_PROP_STRUCTURE_LOGGED: set[int] = set()
_PROP_ZERO_YIELD_LOGGED: set[tuple[str, int]] = set()


def fetch_affiliates() -> dict[int, str] | None:
    """Fetch affiliate_id -> name from /api/v2/affiliates (no auth). Returns None on failure."""
    try:
        resp = requests.get(
            "https://therundown.io/api/v2/affiliates",
            headers={"Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        affiliates = data.get("affiliates") or []
        return {int(a["affiliate_id"]): a.get("affiliate_name", f"Book{a['affiliate_id']}") for a in affiliates if isinstance(a, dict) and "affiliate_id" in a}
    except Exception:
        return None

LINE_TYPES = [
    ("Moneyline", "moneyline_home", "moneyline_away"),
    ("Spread",    "spread_home",    "spread_away"),
    ("Total",     "total_over",     "total_under"),
]


class AllKeysExpendedError(Exception):
    pass


class ApiKeyRotationState:
    """Process-wide API key state with per-key lockouts."""

    def __init__(self, keys: list[str]):
        clean = [k for k in keys if isinstance(k, str) and k.strip()]
        if not clean:
            raise ValueError("No API keys configured for TheRundown")
        self._keys = clean
        self._locked_until = [0.0 for _ in clean]
        self._current_idx = 0
        self._lock = threading.Lock()

    @property
    def total(self) -> int:
        return len(self._keys)

    def _now(self) -> float:
        return time.time()

    def get_next_available(self) -> tuple[str, int]:
        """Round-robin pick of a currently available key."""
        now = self._now()
        with self._lock:
            for offset in range(len(self._keys)):
                idx = (self._current_idx + offset) % len(self._keys)
                if now >= self._locked_until[idx]:
                    self._current_idx = idx
                    return self._keys[idx], idx
        raise AllKeysExpendedError(self.exhausted_message())

    def lock_key(self, idx: int, retry_seconds: int) -> None:
        retry_seconds = max(int(retry_seconds), 1)
        with self._lock:
            self._locked_until[idx] = self._now() + retry_seconds
            self._current_idx = (idx + 1) % len(self._keys)

    def min_wait_seconds(self) -> int:
        now = self._now()
        with self._lock:
            waits = [max(0, int(until - now)) for until in self._locked_until]
        positive = [w for w in waits if w > 0]
        return min(positive) if positive else 0

    def exhausted_message(self) -> str:
        wait_s = self.min_wait_seconds()
        unit = "second" if wait_s == 1 else "seconds"
        return f"ALL KEYS EXPENDED | NEXT AVAILABLE RUN IN {wait_s} {unit}"


_API_KEY_STATE = ApiKeyRotationState(API_KEYS)

# Updated from the X-Rate-Limit response header after each successful call.
# Starts at 1 (free-tier default) so REQUEST_DELAY is used until we see a
# response that tells us otherwise.
_observed_rate_limit: int = 1


def get_safe_delay() -> float:
    """
    Compute the safe inter-request sleep interval from the most recently
    observed X-Rate-Limit header value.

    Adds a 50ms safety buffer above the theoretical minimum (1/rate_limit)
    to stay comfortably under the burst ceiling under minor clock skew.
    Falls back to the configured REQUEST_DELAY until the first response is
    seen (i.e. while _observed_rate_limit is still 1 / free-tier default).

    Tier reference:
      free (1/s)    -> REQUEST_DELAY (1.2s default)
      starter (2/s) -> 0.55s
      pro (5/s)     -> 0.25s
      ultra (10/s)  -> 0.15s
      super (15/s)  -> 0.12s
      mega (20/s)   -> 0.10s
    """
    if _observed_rate_limit <= 1:
        return REQUEST_DELAY
    return round(1.0 / _observed_rate_limit + 0.05, 3)


def _parse_retry_after_seconds(resp_headers: dict) -> int:
    """Return Retry-After seconds from response headers; fallback to 60s.

    requests.Response.headers is a CaseInsensitiveDict, so a single lookup
    covers all capitalisation variants (retry-after, Retry-After, etc.).
    """
    raw = resp_headers.get("retry-after")
    try:
        return max(int(float(raw)), 1)
    except (TypeError, ValueError):
        return 60


def chunk_market_ids(ids: list[int] | set[int] | tuple[int, ...], chunk_size: int = 12) -> list[list[int]]:
    """
    Split market IDs into <= chunk_size lists.
    V2 /events/{eventID} requests silently ignore IDs beyond 12.
    """
    cleaned = sorted({int(i) for i in ids})
    if chunk_size <= 0:
        chunk_size = 12
    return [cleaned[i:i + chunk_size] for i in range(0, len(cleaned), chunk_size)]


def with_api_key_rotation(fn):
    """
    Decorator for RundownClient request methods.
    On 429, lock current key by Retry-After and retry with next key.
    """
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        attempts = 0
        seen_idxs: set[int] = set()
        while attempts < _API_KEY_STATE.total:
            key, idx = _API_KEY_STATE.get_next_available()
            self._apply_api_key(key)
            try:
                return fn(self, *args, **kwargs)
            except requests.exceptions.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                if status != 429:
                    raise
                retry_s = _parse_retry_after_seconds(getattr(e.response, "headers", {}) or {})
                _API_KEY_STATE.lock_key(idx, retry_s)
                seen_idxs.add(idx)
                attempts = len(seen_idxs)
        raise AllKeysExpendedError(_API_KEY_STATE.exhausted_message())
    return wrapper

# ──────────────────────────────────────────────
# API CLIENT
# ──────────────────────────────────────────────
class RundownClient:
    DIRECT_BASE = "https://therundown.io/api/v2"
    RAPID_BASE  = "https://therundown-proxy.p.rapidapi.com"
    RAPID_HOST  = "therundown-proxy.p.rapidapi.com"

    def __init__(self, api_key, use_rapidapi=False):
        self.base_url = self.RAPID_BASE if use_rapidapi else self.DIRECT_BASE
        self.use_rapidapi = use_rapidapi
        self.last_headers = {}
        self.session  = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._apply_api_key(api_key)

    def _apply_api_key(self, api_key: str):
        if self.use_rapidapi:
            self.session.headers.update({
                "X-RapidAPI-Key": api_key,
                "X-RapidAPI-Host": self.RAPID_HOST,
            })
            self.session.headers.pop("X-TheRundown-Key", None)
        else:
            self.session.headers.update({"X-TheRundown-Key": api_key})
            self.session.headers.pop("X-RapidAPI-Key", None)
            self.session.headers.pop("X-RapidAPI-Host", None)

    @with_api_key_rotation
    def _get(self, path, params=None):
        url = f"{self.base_url}{path}"
        print(f"API_REQUEST: GET {url} with params {params}")
        try:
            resp = self.session.get(url, params=params or {}, timeout=15)
            self.last_headers = dict(resp.headers)
            raw_payload = resp.text
            dp_cost = resp.headers.get("X-Datapoints", "?")
            dp_used = resp.headers.get("X-Datapoints-Used", "?")
            dp_rem  = resp.headers.get("X-Datapoints-Remaining", "?")
            # Update the observed rate-limit ceiling so get_safe_delay() can
            # compute the tightest safe inter-request interval for this key.
            global _observed_rate_limit
            _rl_raw = resp.headers.get("X-Rate-Limit")
            if _rl_raw:
                try:
                    _observed_rate_limit = max(1, int(_rl_raw))
                except (TypeError, ValueError):
                    pass
            print(f"API_RESPONSE: HTTP {resp.status_code} | {len(raw_payload)} bytes | dp_cost={dp_cost} used={dp_used} remaining={dp_rem}")
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                print("API_ERROR: 401 Unauthorized - Check your API key")
            elif e.response.status_code == 429:
                print("API_ERROR: 429 Too Many Requests - Rate limit exceeded")
            elif e.response.status_code >= 500:
                print(f"API_ERROR: {e.response.status_code} Server Error from upstream")
            raise e
        except Exception as e:
            print(f"API_ERROR: Request failed - {e}")
            raise e

    def get_events(self, sport_id, date_str):
        """Fetch full snapshot with tight filters to minimize datapoint cost."""
        market_ids = ",".join(str(mid) for mid in CORE_MARKET_IDS)
        return self._get(f"/sports/{sport_id}/events/{date_str}", params={
            "affiliate_ids": "22,19,23",   # BetMGM (22), DraftKings (19), FanDuel (23)
            "market_ids": market_ids,
            "offset": "300",               # 5-min data window alignment
            "include": "affiliates",
            "main_line": "true",
        })

    def get_available_markets_by_date(self, date_str, offset: str = "300"):
        """
        Discover available markets for all sports on a date.
        Response is keyed by sport_id strings.
        NOTE: Prefer get_sport_markets() for per-sport discovery.
        """
        return self._get(f"/sports/markets/{date_str}", params={"offset": str(offset)})

    def get_sport_markets(self, sport_id: int, date_str: str, offset: str = "300") -> dict:
        """
        Discover active markets for a specific sport/date.
        Uses V2 endpoint: /sports/{sport_id}/markets/{date}.
        """
        return self._get(f"/sports/{sport_id}/markets/{date_str}", params={"offset": offset})

    def get_prop_events(self, sport_id, date_str):
        """
        Fetch props-only event markets without restrictive affiliate filters.
        This improves prop visibility when a subset of books does not expose
        player props in the main snapshot.
        """
        prop_ids = ",".join(str(mid) for mid in sorted(PROP_MARKET_IDS))
        return self._get(f"/sports/{sport_id}/events/{date_str}", params={
            "market_ids": prop_ids,
            "offset": "300",
            "include": "affiliates",
        })

    def get_event_markets_catalog(self, event_ref):
        """
        Fetch market catalog for one event (available market IDs and metadata).
        event_ref may be event_id or event_uuid.
        """
        return self._get(f"/events/{event_ref}/markets", params={"offset": "300"})

    def get_event_markets(self, event_ref):
        """Alias for event market catalog lookup."""
        return self.get_event_markets_catalog(event_ref)

    def get_event_with_markets(self, event_ref, market_ids=None, affiliate_ids: str | None = None, main_line: str = "false"):
        """
        Fetch one event with selected market IDs.
        event_ref may be event_id or event_uuid.
        """
        params = {"offset": "300", "include": "affiliates"}
        if market_ids:
            if isinstance(market_ids, (list, set, tuple)):
                params["market_ids"] = ",".join(str(m) for m in market_ids)
            else:
                params["market_ids"] = str(market_ids)
        if affiliate_ids:
            params["affiliate_ids"] = str(affiliate_ids)
        if main_line in {"true", "false"}:
            params["main_line"] = main_line
        return self._get(f"/events/{event_ref}", params=params)

    def get_markets_delta(self, sport_id, last_id):
        """Lightweight delta: only returns prices that changed since last_id."""
        return self._get("/markets/delta", params={
            "last_id": last_id,
            "sport_id": sport_id,
            "market_ids": "1,2,3",
            "affiliate_ids": "22,19,23",   # BetMGM (22), DraftKings (19), FanDuel (23)
        })

    def get_delta(self, sport_id, last_id):
        """Legacy event-level delta (fallback)."""
        return self._get(f"/sports/{sport_id}/events/delta", params={"last_id": last_id})


# ──────────────────────────────────────────────
# RAW STRUCTURE INSPECTOR
# Runs once on first event to map the actual API layout
# ──────────────────────────────────────────────
def inspect_raw_structure(event: dict, sport_name: str):
    """Print the actual odds structure so we know what keys the API uses."""
    print(f"  ⚑ Inspecting raw structure for [{sport_name}] event")
    print(f"     Top-level keys: {list(event.keys())}")

    markets = event.get("markets") or []
    if not markets:
        print("     ⚠ No 'markets' key or empty list on event.")
        return

    first_market = markets[0]
    print(f"     First market keys: {list(first_market.keys())}")
    print(f"     market_id={first_market.get('market_id')}  period_id={first_market.get('period_id')}  name={first_market.get('name')!r}")

    participants = first_market.get("participants") or []
    print(f"     participants ({len(participants)}): {[p.get('name') for p in participants if isinstance(p, dict)]}")
    if not participants:
        return

    first_participant = participants[0]
    print(f"     First participant keys: {list(first_participant.keys())}")

    lines = first_participant.get("lines") or []
    print(f"     lines count: {len(lines)}")
    if lines:
        first_line = lines[0]
        print(f"     First line keys: {list(first_line.keys())}")
        prices = first_line.get("prices") or {}
        print(f"     prices keys (book ids): {list(prices.keys())}")


# ──────────────────────────────────────────────
# ODDS CONVERSION
# ──────────────────────────────────────────────
def american_to_decimal(val: float) -> float:
    val = float(val)
    if val >= 100:
        return round((val / 100) + 1, 6)
    elif val <= -100:
        return round((100 / abs(val)) + 1, 6)
    return 0.0

def to_decimal(val) -> float:
    try:
        val = float(val)
    except (TypeError, ValueError):
        return 0.0
    if val == 0:
        return 0.0
    if abs(val) >= 100:
        return american_to_decimal(val)
    if val > 1.0:
        return round(val, 6)  # already decimal
    return 0.0


# ──────────────────────────────────────────────
# BEST-LINE SELECTION (AMERICAN ODDS)
# ──────────────────────────────────────────────
def select_best_american_price(options: list[dict]) -> dict | None:
    """
    Given a list of price entries for the same outcome across books, select the
    single best price according to American-odds shopping rules:

      - If any prices are positive, choose the highest positive integer.
      - If all prices are negative, choose the negative value closest to zero.
      - If there is a mix of positive and negative, always choose the positive.

    Each option is expected to have at least:
        { "book": str, "price_am": int|float }
    """
    if not options:
        return None

    numeric: list[tuple[float, dict]] = []
    for opt in options:
        val = opt.get("price_am")
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if num == 0:
            continue
        numeric.append((num, opt))

    if not numeric:
        return None

    positives = [(v, o) for (v, o) in numeric if v > 0]
    negatives = [(v, o) for (v, o) in numeric if v < 0]

    if positives:
        # Highest positive value
        _, best = max(positives, key=lambda t: t[0])
        return best
    if negatives:
        # Negative closest to zero => numerically largest
        _, best = max(negatives, key=lambda t: t[0])
        return best
    return None


# ──────────────────────────────────────────────
# LINE EXTRACTION — markets-based, line-aware
# ──────────────────────────────────────────────
def build_market_index(event: dict) -> tuple[dict, dict, set[int], set[int]]:
    """
    Build a per-event index of available prices grouped by (market kind, line value).

    Returns:
        {
            ("ml", None): {
                "home": [ { "book": str, "price_am": int, "price_dec": float }, ... ],
                "away": [ ... ],
            },
            ("spread", 4.5): {
                "home": [...],
                "away": [...],
            },
            ("total", 226.5): {
                "over": [...],
                "under": [...],
            },
            ...
        }
    """
    markets = event.get("markets") or []
    if not isinstance(markets, list) or len(markets) == 0:
        event_id = event.get("event_id", "Unknown")
        print(f"PARSER_WARNING: 'markets' missing, empty, or invalid schema for event {event_id}. Upstream schema change?")
        return {}, {}, set(), set()

    index: dict[tuple[str, float | None], dict[str, list[dict]]] = {}
    # For spreads we also maintain complementary buckets by absolute line value:
    # abs_line -> {"home_minus": [...], "away_plus": [...]}
    spread_pairs: dict[float, dict[str, list[dict]]] = {}
    raw_book_ids_seen: set[int] = set()
    used_book_ids_seen: set[int] = set()

    for m in markets:
        if not isinstance(m, dict):
            continue
        # Only use full-game markets
        if m.get("period_id") != 0:
            continue

        market_id = m.get("market_id")
        m_name = (m.get("name") or "").lower()
        if "3-way" in m_name:
            continue

        participants = m.get("participants") or []
        if not isinstance(participants, list) or len(participants) == 0 or len(participants) > 2:
            continue

        # Determine market kind
        is_totals = market_id == 3 or "total" in m_name
        is_spread = market_id == 2 or "spread" in m_name
        is_ml     = market_id == 1 or "moneyline" in m_name or "money line" in m_name or "winner" in m_name

        if not (is_totals or is_spread or is_ml):
            continue

        for idx, participant in enumerate(participants):
            if not isinstance(participant, dict):
                continue

            pname = (participant.get("name") or "").lower()

            # Map participant to abstract side label
            side_label: str | None = None
            if is_totals:
                if "over" in pname:
                    side_label = "over"
                elif "under" in pname:
                    side_label = "under"
            elif is_spread:
                # Assume ordering: first = away, second = home
                side_label = "away" if idx == 0 else "home"
            elif is_ml:
                side_label = "away" if idx == 0 else "home"

            if not side_label:
                continue

            lines = participant.get("lines") or []
            if not isinstance(lines, list):
                continue

            for line in lines:
                if not isinstance(line, dict):
                    continue

                # Line value (spread or total points); moneyline has no line value
                line_value: float | None = None
                if is_totals or is_spread:
                    val = line.get("value")
                    if val is None:
                        val = line.get("point")
                    try:
                        line_value = float(val)
                    except (TypeError, ValueError):
                        continue

                prices = line.get("prices") or {}
                if not isinstance(prices, dict):
                    continue

                for raw_book_id, price_obj in prices.items():
                    try:
                        book_id = int(raw_book_id)
                    except (ValueError, TypeError):
                        continue
                    raw_book_ids_seen.add(book_id)
                    book_name_raw = KNOWN_BOOKS.get(book_id, f"Book{book_id}")

                    if RESTRICT_BOOKS and book_name_raw.lower().replace(" ", "") not in ALLOWED_BOOK_NAMES:
                        continue
                    if not isinstance(price_obj, dict):
                        continue

                    # Skip closed/delisted lines
                    if price_obj.get("closed_at"):
                        continue

                    price_am = price_obj.get("price")
                    # Reject sentinel value 0.0001 (means line is suspended)
                    if price_am == 0.0001 or price_am == "0.0001":
                        continue
                    if price_am in (None, 0, "0", ""):
                        # Fallback for EU-only or missing American odds formats
                        price_eu = price_obj.get("price_eu") or price_obj.get("decimal")
                        if price_eu:
                            price_am = price_eu
                        else:
                            continue
                    dec = to_decimal(price_am)
                    if dec <= 1.0:
                        continue
                        
                    try:
                        pam_float = float(price_am)
                        # Display integer American odds if >=100, else fallback to float for Decimals
                        pam_disp = int(pam_float) if (pam_float <= -100 or pam_float >= 100) else pam_float
                    except Exception:
                        pam_disp = price_am

                    updated_at = price_obj.get("updated_at")
                    used_book_ids_seen.add(book_id)

                    kind = "ml" if is_ml else ("spread" if is_spread else "total")
                    key = (kind, line_value if kind != "ml" else None)
                    per_line = index.setdefault(key, {"home": [], "away": [], "over": [], "under": []})
                    per_line[side_label].append(
                        {
                            "book": book_name_raw,
                            "price_am": pam_disp,
                            "price_dec": dec,
                            "updated_at": updated_at,
                        }
                    )

                    # Build complementary spread index: home -X vs away +X for same |X|
                    if kind == "spread" and line_value is not None:
                        abs_lv = abs(line_value)
                        buckets = spread_pairs.setdefault(abs_lv, {"home_minus": [], "away_plus": []})
                        if side_label == "home" and line_value < 0:
                            buckets["home_minus"].append(
                                {
                                    "book": book_name_raw,
                                    "price_am": pam_disp,
                                    "price_dec": dec,
                                    "updated_at": updated_at,
                                }
                            )
                        elif side_label == "away" and line_value > 0:
                            buckets["away_plus"].append(
                                {
                                    "book": book_name_raw,
                                    "price_am": pam_disp,
                                    "price_dec": dec,
                                    "updated_at": updated_at,
                                }
                            )

    # ── LEGACY FALLBACK: event["lines"] dict keyed by book_id ─────────
    # TheRundown API also provides a flat structure at event["lines"][book_id]
    # with keys like "moneyline_home", "spread_home", "total_over", etc.
    # Some books (notably DraftKings) may have data here that isn't in "markets".
    legacy_lines = event.get("lines")
    if isinstance(legacy_lines, dict):
        _LEGACY_MAP = {
            "moneyline_home": ("ml", None, "home"),
            "moneyline_away": ("ml", None, "away"),
            "spread_home":    ("spread", None, "home"),
            "spread_away":    ("spread", None, "away"),
            "total_over":     ("total", None, "over"),
            "total_under":    ("total", None, "under"),
        }
        for raw_book_id_str, book_data in legacy_lines.items():
            try:
                book_id = int(raw_book_id_str)
            except (ValueError, TypeError):
                continue
            raw_book_ids_seen.add(book_id)
            book_name_raw = KNOWN_BOOKS.get(book_id, f"Book{book_id}")

            if RESTRICT_BOOKS and book_name_raw.lower().replace(" ", "") not in ALLOWED_BOOK_NAMES:
                continue
            if not isinstance(book_data, dict):
                continue

            for legacy_key, (kind, _placeholder_lv, side_label) in _LEGACY_MAP.items():
                obj = book_data.get(legacy_key)
                if not isinstance(obj, dict):
                    continue

                price_am = obj.get("price") or obj.get("odds")
                if price_am in (None, 0, "0", ""):
                    price_eu = obj.get("price_eu") or obj.get("decimal")
                    if not price_eu:
                        continue
                    price_am = price_eu

                dec = to_decimal(price_am)
                if dec <= 1.0:
                    continue

                try:
                    pam_float = float(price_am)
                    pam_disp = int(pam_float) if (pam_float <= -100 or pam_float >= 100) else pam_float
                except Exception:
                    pam_disp = price_am

                # For spread/total, try to get the line value
                line_value: float | None = None
                if kind in ("spread", "total"):
                    lv_raw = obj.get("point") or obj.get("value") or obj.get("line")
                    if lv_raw is not None:
                        try:
                            line_value = float(lv_raw)
                        except (TypeError, ValueError):
                            pass

                updated_at = obj.get("updated_at")
                used_book_ids_seen.add(book_id)

                key = (kind, line_value if kind != "ml" else None)
                per_line = index.setdefault(key, {"home": [], "away": [], "over": [], "under": []})

                # Only add if this book doesn't already have an entry for this side
                # (avoid duplicating data already captured from the markets structure)
                already_has = any(e["book"] == book_name_raw for e in per_line[side_label])
                if not already_has:
                    per_line[side_label].append(
                        {
                            "book": book_name_raw,
                            "price_am": pam_disp,
                            "price_dec": dec,
                            "updated_at": updated_at,
                        }
                    )

    return index, spread_pairs, raw_book_ids_seen, used_book_ids_seen


# ──────────────────────────────────────────────
# ARBITRAGE CORE
# ──────────────────────────────────────────────
def _resolve_teams(event: dict) -> tuple[str, str, str]:
    """
    Resolve game and team labels (away @ home) for a given event.
    Returns (game_label, home_name, away_name).
    """
    teams = event.get("teams") or []
    if teams:
        home_team = next((t for t in teams if t.get("is_home")), teams[-1])
        away_team = next((t for t in teams if t.get("is_away")), teams[0])
        home = home_team.get("name", "Home")
        away = away_team.get("name", "Away")
    else:
        tn = event.get("teams_normalized", [])
        home = tn[0].get("name", "Home") if len(tn) > 0 else "Home"
        away = tn[1].get("name", "Away") if len(tn) > 1 else "Away"
    game = f"{away} @ {home}"
    return game, home, away


def parse_player_props(event: dict, sport_name: str) -> tuple[list[dict], list[dict]]:
    """
    Extract player prop lines from a single event.
    Returns (raw_lines, best_lines).
    Only processes market_ids in PROP_MARKET_IDS. Silently skips markets with no valid data.
    """
    game, _home, _away = _resolve_teams(event)
    markets = event.get("markets") or []
    raw_lines: list[dict] = []
    best_lines: list[dict] = []
    debug_enabled = VALIDATION_MODE or PROP_DIAGNOSTICS
    event_diag = {
        "prop_markets_seen": 0,
        "lines_considered": 0,
        "lines_accepted": 0,
        "unknown_participant_type": 0,
        "missing_side": 0,
        "missing_player": 0,
        "closed": 0,
        "sentinel_price": 0,
        "book_filtered": 0,
        "invalid_odds": 0,
    }

    def _normalize_player_name_from_side_participant(name: str) -> str:
        txt = str(name or "").strip()
        if not txt:
            return ""
        low = txt.lower()
        if low.endswith(" over"):
            return txt[:-5].strip()
        if low.endswith(" under"):
            return txt[:-6].strip()
        if low.startswith("over "):
            return txt[5:].strip()
        if low.startswith("under "):
            return txt[6:].strip()
        return txt

    for m in markets:
        if not isinstance(m, dict):
            continue

        # Normalise market_id to int — V2 API can return string or int
        try:
            market_id = int(m.get("market_id"))
        except (TypeError, ValueError):
            continue
        if market_id not in PROP_MARKET_IDS:
            continue

        event_diag["prop_markets_seen"] += 1
        prop_type = PROP_MARKET_NAMES[market_id]

        if market_id not in _PROP_STRUCTURE_LOGGED:
            _PROP_STRUCTURE_LOGGED.add(market_id)
            print(f"  [PROP_INSPECT] market_id={market_id} ({prop_type}) sport={sport_name}")
            for p in (m.get("participants") or [])[:2]:
                print(f"    participant type={p.get('type')!r} name={p.get('name')!r}")
                for ln in (p.get("lines") or [])[:1]:
                    print(
                        f"    line value={ln.get('value')!r} "
                        f"line_value_is_participant={ln.get('line_value_is_participant')}"
                    )
                    print(f"    prices keys (book ids): {list((ln.get('prices') or {}).keys())[:5]}")

        # V2 player props expose TYPE_OVER/TYPE_UNDER participants with names
        # like "LeBron James Over". Resolve player names inline below.

        prop_buckets: dict[tuple[str, float | None], dict[str, list[dict]]] = {}
        market_lines_accepted = 0

        for participant in m.get("participants") or []:
            if not isinstance(participant, dict):
                continue
            p_type = str(participant.get("type", "") or "")
            p_type_u = p_type.upper()
            p_name = str(participant.get("name", "") or "")
            p_name_l = p_name.lower()

            # Resolve side from participant type first, then fallback by name for TYPE_RESULT.
            side: str | None = None
            if p_type_u == "TYPE_OVER":
                side = "over"
            elif p_type_u == "TYPE_UNDER":
                side = "under"
            elif p_type_u == "TYPE_RESULT":
                if "over" in p_name_l:
                    side = "over"
                elif "under" in p_name_l:
                    side = "under"
            else:
                event_diag["unknown_participant_type"] += 1
                continue
            if not side:
                event_diag["missing_side"] += 1
                continue

            for line in participant.get("lines") or []:
                if not isinstance(line, dict):
                    continue
                event_diag["lines_considered"] += 1

                if line.get("line_value_is_participant"):
                    player_name = str(line.get("value") or "").strip()
                    line_threshold: float | None = None
                else:
                    player_name = _normalize_player_name_from_side_participant(p_name)
                    val = line.get("value")
                    if val is None:
                        val = line.get("point")
                    try:
                        line_threshold = float(val)
                    except (TypeError, ValueError):
                        line_threshold = None

                if not player_name:
                    event_diag["missing_player"] += 1
                    continue

                bucket_key = (player_name, line_threshold)
                bucket = prop_buckets.setdefault(bucket_key, {"over": [], "under": []})

                prices = line.get("prices") or {}
                if not isinstance(prices, dict):
                    continue

                for raw_book_id, price_obj in prices.items():
                    if not isinstance(price_obj, dict):
                        event_diag["invalid_odds"] += 1
                        continue
                    # Skip closed/delisted lines
                    if price_obj.get("closed_at"):
                        event_diag["closed"] += 1
                        continue

                    price_am = price_obj.get("price")
                    # Reject sentinel value 0.0001 (means line is suspended)
                    if price_am == 0.0001 or price_am == "0.0001":
                        event_diag["sentinel_price"] += 1
                        continue
                    if price_am in (None, 0, "0", ""):
                        # Fallback for EU-only or missing American odds formats
                        price_eu = price_obj.get("price_eu") or price_obj.get("decimal")
                        if price_eu:
                            price_am = price_eu
                        else:
                            event_diag["invalid_odds"] += 1
                            continue
                    dec = to_decimal(price_am)
                    if dec <= 1.0:
                        event_diag["invalid_odds"] += 1
                        continue

                    try:
                        book_id = int(raw_book_id)
                    except (ValueError, TypeError):
                        continue
                    book_name = KNOWN_BOOKS.get(book_id, f"Book{book_id}")
                    if RESTRICT_PROP_BOOKS and book_name.lower().replace(" ", "") not in ALLOWED_BOOK_NAMES:
                        event_diag["book_filtered"] += 1
                        continue

                    try:
                        pam_float = float(price_am)
                        pam_disp = int(pam_float) if (pam_float <= -100 or pam_float >= 100) else pam_float
                    except Exception:
                        pam_disp = price_am

                    updated_at = price_obj.get("updated_at")
                    label_prefix = "O" if side == "over" else "U"
                    line_label = (
                        f"{label_prefix} {line_threshold:g}" if line_threshold is not None else label_prefix
                    )

                    raw_lines.append({
                        "sport": sport_name,
                        "game": game,
                        "market_kind": "prop",
                        "line_label": line_label,
                        "side": side.capitalize(),
                        "book": book_name,
                        "odds_am": pam_disp,
                        "updated_at": updated_at,
                        "player": player_name,
                        "prop_type": prop_type,
                    })

                    bucket[side].append({
                        "book": book_name,
                        "price_am": pam_disp,
                        "price_dec": dec,
                        "updated_at": updated_at,
                    })
                    event_diag["lines_accepted"] += 1
                    market_lines_accepted += 1

        warn_key = (sport_name, market_id)
        if market_lines_accepted == 0 and warn_key not in _PROP_ZERO_YIELD_LOGGED:
            participants = m.get("participants") or []
            if participants:
                _PROP_ZERO_YIELD_LOGGED.add(warn_key)
                participant_types = sorted({str((p or {}).get("type", "")) for p in participants if isinstance(p, dict)})
                print(
                    f"  [PROP_WARN] zero accepted lines for market_id={market_id} ({prop_type}) "
                    f"sport={sport_name} participant_types={participant_types}"
                )

        for (player_name, line_threshold), sides in prop_buckets.items():
            best_over = select_best_american_price(sides["over"])
            best_under = select_best_american_price(sides["under"])
            if not best_over or not best_under:
                continue
            best_lines.append({
                "type": "prop",
                "sport": sport_name,
                "game": game,
                "player": player_name,
                "prop_type": prop_type,
                "line": line_threshold,
                "over": {"book": best_over["book"], "odds_am": best_over["price_am"]},
                "under": {"book": best_under["book"], "odds_am": best_under["price_am"]},
            })

    if debug_enabled and event_diag["prop_markets_seen"] > 0:
        print(
            "  [PROP_DIAG] "
            f"{sport_name} {game} | markets={event_diag['prop_markets_seen']} "
            f"considered={event_diag['lines_considered']} accepted={event_diag['lines_accepted']} "
            f"drops: unknown_type={event_diag['unknown_participant_type']} "
            f"missing_side={event_diag['missing_side']} missing_player={event_diag['missing_player']} "
            f"closed={event_diag['closed']} sentinel={event_diag['sentinel_price']} "
            f"book_filtered={event_diag['book_filtered']} invalid_odds={event_diag['invalid_odds']}"
        )

    return raw_lines, best_lines


def diagnose_prop_market_availability(
    client: "RundownClient",
    sport_id: int,
    date_str: str,
    affiliate_ids: str = "22,19,23",
) -> dict:
    """
    Read-only troubleshooting helper for prop visibility.
    Compares catalog availability vs event payload market presence.
    """
    requested = sorted(PROP_MARKET_IDS)
    out: dict = {
        "sport_id": sport_id,
        "date": date_str,
        "requested_prop_market_ids": requested,
        "catalog_prop_ids_present": [],
        "events_with_affiliates_prop_counts": {},
        "events_without_affiliates_prop_counts": {},
    }

    try:
        catalog = client._get(f"/sports/{sport_id}/markets/{date_str}", params={"offset": "300"}) or {}
        key = str(sport_id)
        entries = catalog.get(key) or catalog.get("markets") or []
        present = sorted(
            int(e.get("id"))
            for e in entries
            if isinstance(e, dict) and e.get("id") in PROP_MARKET_IDS
        )
        out["catalog_prop_ids_present"] = present
    except Exception as e:
        out["catalog_error"] = str(e)

    def _event_prop_counts(params: dict) -> dict:
        payload = client._get(f"/sports/{sport_id}/events/{date_str}", params=params) or {}
        counts: dict[int, int] = {mid: 0 for mid in requested}
        for evt in payload.get("events") or []:
            for m in evt.get("markets") or []:
                if not isinstance(m, dict):
                    continue
                mid = m.get("market_id")
                if mid in counts:
                    counts[mid] += 1
        return counts

    try:
        out["events_with_affiliates_prop_counts"] = _event_prop_counts({
            "affiliate_ids": affiliate_ids,
            "market_ids": ",".join(str(m) for m in requested),
            "offset": "300",
            "include": "affiliates",
            "main_line": "true",
        })
    except Exception as e:
        out["events_with_affiliates_error"] = str(e)

    try:
        out["events_without_affiliates_prop_counts"] = _event_prop_counts({
            "market_ids": ",".join(str(m) for m in requested),
            "offset": "300",
            "include": "affiliates",
            "main_line": "true",
        })
    except Exception as e:
        out["events_without_affiliates_error"] = str(e)

    return out


def analyze_event(event: dict, sport_name: str) -> tuple[list[dict], list[dict]]:
    """
    Find all near-arb and arb situations for an event.
    Returns (list_of_arbs, list_of_raw_lines).
    """
    # Prefer explicit home/away flags from event["teams"], fall back to teams_normalized.
    game, home, away = _resolve_teams(event)

    market_index, spread_pairs, raw_book_ids, used_book_ids = build_market_index(event)
    if not market_index:
        return [], []

    if DEBUG_BOOK_COVERAGE:
        missing = []
        if PRIMARY_BOOK_ID not in raw_book_ids:
            missing.append(KNOWN_BOOKS.get(PRIMARY_BOOK_ID, "betmgm").lower())
        if 23 not in raw_book_ids:
            missing.append("fanduel")
        if missing:
            raw_names = ", ".join(KNOWN_BOOKS.get(b, f"Book{b}") for b in sorted(raw_book_ids)[:10])
            print(f"  ⚠ [{sport_name}] {game} missing raw books: {', '.join(missing)} (raw seen: {raw_names})")

    results: list[dict] = []
    raw_lines: list[dict] = []

    # Helper to parse updated_at into UTC datetime
    def _parse_ts(val):
        try:
            if not val:
                return None
            if isinstance(val, (int, float)):
                ts = float(val)
                if ts > 1e10:
                    ts /= 1000  # assume milliseconds
                return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
            return datetime.datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        except Exception:
            return None

    # Handle moneyline and totals using the regular index
    for (kind, line_value), sides in market_index.items():
        if kind == "ml":
            label = "Moneyline"
            side_a_key, side_b_key = "home", "away"
            market_line_label = "ML"
        elif kind == "total":
            label = "Total"
            side_a_key, side_b_key = "over", "under"
            market_line_label = f"{line_value:g}" if line_value is not None else ""
        else:
            continue

        # Build raw lines for all sides and all books
        for side_key, side_display in [(side_a_key, side_a_key.capitalize()), (side_b_key, side_b_key.capitalize())]:
            for option in sides.get(side_key) or []:
                raw_lines.append({
                    "sport": sport_name,
                    "game": game,
                    "market_kind": kind,
                    "line_label": market_line_label,
                    "side": side_display,
                    "book": option["book"],
                    "odds_am": option["price_am"],
                    "updated_at": option.get("updated_at"),
                })

        side_a_list = sides.get(side_a_key) or []
        side_b_list = sides.get(side_b_key) or []
        if not side_a_list or not side_b_list:
            continue

        # Best prices per side (highest decimal odds) across all books at this exact line
        best_a = max(side_a_list, key=lambda e: e["price_dec"])
        best_b = max(side_b_list, key=lambda e: e["price_dec"])

        # Reject arbs where either leg has stale updated_at
        ts_a = _parse_ts(best_a.get("updated_at"))
        ts_b = _parse_ts(best_b.get("updated_at"))
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if ts_a and (now_utc - ts_a).total_seconds() > ARB_MAX_LINE_AGE_S:
            continue
        if ts_b and (now_utc - ts_b).total_seconds() > ARB_MAX_LINE_AGE_S:
            continue
        if ts_a is None or ts_b is None:
            continue  # no timestamp → can't verify freshness

        dec_a = best_a["price_dec"]
        dec_b = best_b["price_dec"]
        arb   = (1 / dec_a) + (1 / dec_b)
        profit = round((1 - arb) * 100, 4)

        # Filter out non-interesting / unrealistic results
        if profit < ARB_THRESHOLD:
            continue
        if profit > MAX_PROFIT_CAP:
            # Extremely high implied profit is almost certainly an artifact; skip reporting.
            continue

        stake_a = round(TOTAL_STAKE * (1 / dec_a) / arb, 2)
        stake_b = round(TOTAL_STAKE * (1 / dec_b) / arb, 2)

        # Freshness metrics: how recent are the two sides?
        ts_a = _parse_ts(best_a.get("updated_at"))
        ts_b = _parse_ts(best_b.get("updated_at"))
        fresh_ts = stale_ts = None
        fresh_age_s = stale_age_s = None
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        ts_list = [t for t in (ts_a, ts_b) if t is not None]
        if ts_list:
            fresh_ts = max(ts_list)
            stale_ts = min(ts_list)
            fresh_age_s = max((now_utc - t).total_seconds() for t in ts_list)
            stale_age_s = min((now_utc - t).total_seconds() for t in ts_list)

        results.append(
            {
                "sport":          sport_name,
                "game":           game,
                "market_kind":    kind,
                "market_label":   label,
                "line_value":     line_value,
                "line_label":     market_line_label,
                "side_a":         side_a_key.capitalize(),
                "book_a":         best_a["book"],
                "odds_a_am":      best_a["price_am"],
                "odds_a_dec":     dec_a,
                "updated_at_a":   best_a.get("updated_at"),
                "side_b":         side_b_key.capitalize(),
                "book_b":         best_b["book"],
                "odds_b_am":      best_b["price_am"],
                "odds_b_dec":     dec_b,
                "updated_at_b":   best_b.get("updated_at"),
                "fresh_ts":       fresh_ts,
                "stale_ts":       stale_ts,
                "fresh_age_s":    fresh_age_s,
                "stale_age_s":    stale_age_s,
                "arb_pct":        round(arb, 6),
                "profit":         profit,
                "stake_a":        stake_a,
                "stake_b":        stake_b,
                "is_arb":         profit > 0,
            }
        )

    # Handle spreads using complementary home -X vs away +X pairs
    for abs_lv, buckets in spread_pairs.items():
        home_minus = buckets.get("home_minus") or []
        away_plus  = buckets.get("away_plus") or []

        for side_key, line_list, side_display in [("home", home_minus, "Home"), ("away", away_plus, "Away")]:
            for option in line_list:
                raw_lines.append({
                    "sport": sport_name,
                    "game": game,
                    "market_kind": "spread",
                    "line_label": f"{abs_lv:g}",
                    "side": side_display,
                    "book": option["book"],
                    "odds_am": option["price_am"],
                    "updated_at": option.get("updated_at"),
                })

        if not home_minus or not away_plus:
            continue

        best_home = max(home_minus, key=lambda e: e["price_dec"])
        best_away = max(away_plus,  key=lambda e: e["price_dec"])

        # Reject arbs where either leg has stale updated_at
        ts_home = _parse_ts(best_home.get("updated_at"))
        ts_away = _parse_ts(best_away.get("updated_at"))
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if ts_home and (now_utc - ts_home).total_seconds() > ARB_MAX_LINE_AGE_S:
            continue
        if ts_away and (now_utc - ts_away).total_seconds() > ARB_MAX_LINE_AGE_S:
            continue
        if ts_home is None or ts_away is None:
            continue

        dec_home = best_home["price_dec"]
        dec_away = best_away["price_dec"]
        arb      = (1 / dec_home) + (1 / dec_away)
        profit   = round((1 - arb) * 100, 4)

        if profit < ARB_THRESHOLD:
            continue
        if profit > MAX_PROFIT_CAP:
            continue

        stake_home = round(TOTAL_STAKE * (1 / dec_home) / arb, 2)
        stake_away = round(TOTAL_STAKE * (1 / dec_away) / arb, 2)

        ts_home = _parse_ts(best_home.get("updated_at"))
        ts_away = _parse_ts(best_away.get("updated_at"))
        fresh_ts = stale_ts = None
        fresh_age_s = stale_age_s = None
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        ts_list = [t for t in (ts_home, ts_away) if t is not None]
        if ts_list:
            fresh_ts = max(ts_list)
            stale_ts = min(ts_list)
            fresh_age_s = max((now_utc - t).total_seconds() for t in ts_list)
            stale_age_s = min((now_utc - t).total_seconds() for t in ts_list)

        same_book = best_home["book"] == best_away["book"]

        results.append(
            {
                "sport":          sport_name,
                "game":           game,
                "market_kind":    "spread",
                "market_label":   "Spread",
                "line_value":     abs_lv,
                "line_label":     f"{abs_lv:g}",
                "side_a":         "Home",
                "book_a":         best_home["book"],
                "odds_a_am":      best_home["price_am"],
                "odds_a_dec":     dec_home,
                "updated_at_a":   best_home.get("updated_at"),
                "side_b":         "Away",
                "book_b":         best_away["book"],
                "odds_b_am":      best_away["price_am"],
                "odds_b_dec":     dec_away,
                "updated_at_b":   best_away.get("updated_at"),
                "fresh_ts":       fresh_ts,
                "stale_ts":       stale_ts,
                "fresh_age_s":    fresh_age_s,
                "stale_age_s":    stale_age_s,
                "same_book":      same_book,
                "arb_pct":        round(arb, 6),
                "profit":         profit,
                "stake_a":        stake_home,
                "stake_b":        stake_away,
                "is_arb":         profit > 0,
            }
        )

    print(f"PARSER_SUCCESS: Scanned {len(raw_lines)} lines for game [{game}] ({sport_name}) without schema errors.")
    return results, raw_lines


# ──────────────────────────────────────────────
# BEST-LINE AGGREGATION (MONEYLINE ONLY)
# ──────────────────────────────────────────────
def compute_best_moneyline_for_event(event: dict, sport_name: str) -> dict | None:
    """
    For a single head-to-head event, compute the best available Moneyline for
    each side (home, away) across all books, according to American-odds rules.

    Returns:
        {
            "sport": str,
            "game": str,
            "home_team": str,
            "away_team": str,
            "home": {"book": str, "odds_am": int|float},
            "away": {"book": str, "odds_am": int|float},
        }
    or None if no valid moneyline data is available for both sides.
    """
    game, home_name, away_name = _resolve_teams(event)
    market_index, _spread_pairs, _raw_book_ids, _used_book_ids = build_market_index(event)
    if not market_index:
        return None

    ml_sides = None
    for (kind, line_value), sides in market_index.items():
        if kind == "ml":
            ml_sides = sides
            break
    if not ml_sides:
        return None

    home_options = ml_sides.get("home") or []
    away_options = ml_sides.get("away") or []
    if not home_options or not away_options:
        return None

    best_home = select_best_american_price(home_options)
    best_away = select_best_american_price(away_options)
    if not best_home or not best_away:
        return None

    return {
        "sport": sport_name,
        "game": game,
        "home_team": home_name,
        "away_team": away_name,
        "home": {
            "book": best_home["book"],
            "odds_am": best_home["price_am"],
        },
        "away": {
            "book": best_away["book"],
            "odds_am": best_away["price_am"],
        },
    }


# ──────────────────────────────────────────────
# BEST-LINE AGGREGATION (ALL MAIN MARKETS)
# ──────────────────────────────────────────────
def compute_best_lines_for_event(event: dict, sport_name: str) -> list[dict]:
    """
    For a single head-to-head event, compute the best available odds across
    Moneyline, Spread, and Total markets.

    Returns a list of normalized best-line objects:
        Moneyline (paired home/away):
            { "type": "moneyline", "sport", "game", "home_team", "away_team",
              "home": {"book", "odds_am"}, "away": {"book", "odds_am"} }
        Spread (one entry per signed side):
            { "type": "spread", "line": signed_float, "side": "home"|"away",
              "team": str, "sport", "game",
              "pick": {"book", "odds_am"} }
        Total (paired over/under):
            { "type": "total", "line": float,
              "sport", "game",
              "over": {"book", "odds_am"}, "under": {"book", "odds_am"} }
    """
    game, home_name, away_name = _resolve_teams(event)
    market_index, _spread_pairs, _raw_book_ids, _used_book_ids = build_market_index(event)
    if not market_index:
        return []

    results: list[dict] = []

    for (kind, line_value), sides in market_index.items():
        if kind == "ml":
            home_options = sides.get("home") or []
            away_options = sides.get("away") or []
            if not home_options or not away_options:
                continue
            best_home = select_best_american_price(home_options)
            best_away = select_best_american_price(away_options)
            if not best_home or not best_away:
                continue
            results.append(
                {
                    "type": "moneyline",
                    "sport": sport_name,
                    "game": game,
                    "home_team": home_name,
                    "away_team": away_name,
                    "home": {
                        "book": best_home["book"],
                        "odds_am": best_home["price_am"],
                    },
                    "away": {
                        "book": best_away["book"],
                        "odds_am": best_away["price_am"],
                    },
                }
            )
        elif kind == "spread":
            for side_key, team_name in [("home", home_name), ("away", away_name)]:
                options = sides.get(side_key) or []
                if not options:
                    continue
                best = select_best_american_price(options)
                if not best:
                    continue
                results.append(
                    {
                        "type": "spread",
                        "line": line_value,
                        "side": side_key,
                        "team": team_name,
                        "sport": sport_name,
                        "game": game,
                        "pick": {
                            "book": best["book"],
                            "odds_am": best["price_am"],
                        },
                    }
                )
        elif kind == "total":
            over_options = sides.get("over") or []
            under_options = sides.get("under") or []
            if not over_options or not under_options:
                continue
            best_over = select_best_american_price(over_options)
            best_under = select_best_american_price(under_options)
            if not best_over or not best_under:
                continue
            results.append(
                {
                    "type": "total",
                    "line": line_value,
                    "sport": sport_name,
                    "game": game,
                    "over": {
                        "book": best_over["book"],
                        "odds_am": best_over["price_am"],
                    },
                    "under": {
                        "book": best_under["book"],
                        "odds_am": best_under["price_am"],
                    },
                }
            )

    if results:
        ml_c = sum(1 for r in results if r["type"] == "moneyline")
        sp_c = sum(1 for r in results if r["type"] == "spread")
        to_c = sum(1 for r in results if r["type"] == "total")
        print(f"    BEST_LINES [{sport_name}] {game}: {ml_c} ML, {sp_c} spread, {to_c} total")

    return results

# ──────────────────────────────────────────────
# DISPLAY
# ──────────────────────────────────────────────
def _format_staleness(updated_at) -> str:
    """Return human-readable age like '2m ago' or '' if not available."""
    if updated_at is None:
        return ""
    try:
        if isinstance(updated_at, (int, float)):
            ts = float(updated_at)
            if ts > 1e10:
                ts /= 1000  # assume milliseconds
        else:
            dt = datetime.datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
            ts = dt.timestamp()
        age_sec = time.time() - ts
        if age_sec < 60:
            return "just now"
        if age_sec < 3600:
            return f"{int(age_sec // 60)}m ago"
        if age_sec < 86400:
            return f"{int(age_sec // 3600)}h ago"
        return f"{int(age_sec // 86400)}d ago"
    except Exception:
        return ""


def display_results(all_results: list, books_seen: set, events_checked: int):
    print(f"\n{'═'*80}")
    threshold_label = "TRUE ARB ONLY" if ARB_THRESHOLD == 0 else f"within {abs(ARB_THRESHOLD):.0f}% of arb"
    print(f"  SCAN COMPLETE  |  {events_checked} events  |  "
          f"Books: {', '.join(sorted(books_seen)) or 'NONE FOUND'}  |  Filter: {threshold_label}")
    print(f"{'═'*80}")

    if not all_results:
        print(f"\n  Nothing found. Check 'Books: NONE FOUND' above — if books are empty,")
        print(f"  the line extraction logic needs adjusting for your API's response format.\n")
        return

    # Ordering: highest profit first, then by freshness
    def _profit_sort_key(r):
        ts = r.get("fresh_ts")
        ts_val = ts.timestamp() if isinstance(ts, datetime.datetime) else 0.0
        return (r.get("profit", 0.0), ts_val)

    ordered = sorted(all_results, key=_profit_sort_key, reverse=True)

    # Split true arbs from near-arbs, preserving profit-first order
    true_arbs  = [r for r in ordered if r["is_arb"]]
    near_arbs  = [r for r in ordered if not r["is_arb"]]

    def _format_age_seconds(age_s: float | None) -> str:
        if age_s is None:
            return ""
        try:
            if age_s < 60:
                return "just now"
            if age_s < 3600:
                return f"{int(age_s // 60)}m ago"
            if age_s < 86400:
                return f"{int(age_s // 3600)}h ago"
            return f"{int(age_s // 86400)}d ago"
        except Exception:
            return ""

    def make_table(rows, title):
        if not rows:
            return
        print(f"\n  ── {title} ({len(rows)}) ──")
        for i, r in enumerate(rows, 1):
            if r["market_kind"] == "ml":
                market_str = "Moneyline"
            elif r["market_kind"] == "spread":
                market_str = f"Spread {r['line_label']}"
            else:
                market_str = f"Total {r['line_label']}"

            staleness_a = _format_staleness(r.get("updated_at_a"))
            staleness_b = _format_staleness(r.get("updated_at_b"))
            suffix_a = f", {staleness_a}" if staleness_a else ""
            suffix_b = f", {staleness_b}" if staleness_b else ""

            profit_str = f"{'+' if r['profit']>0 else ''}{r['profit']:.3f}%"
            same_book_flag = " [same book]" if r.get("same_book") else ""
            print(f"  [{i}] {r['sport']} – {r['game']} – {market_str}{same_book_flag}")
            print(f"      Side A: {r['side_a']} @ {r['book_a']} ({r['odds_a_am']:+d}{suffix_a})")
            print(f"      Side B: {r['side_b']} @ {r['book_b']} ({r['odds_b_am']:+d}{suffix_b})")
            print(f"      Profit: {profit_str}  |  Stake A: ${r['stake_a']:.2f}  |  Stake B: ${r['stake_b']:.2f}")
            # Overall freshness summary line
            fresh_age = _format_age_seconds(r.get("fresh_age_s"))
            stale_age = _format_age_seconds(r.get("stale_age_s"))
            if fresh_age:
                if stale_age and stale_age != fresh_age:
                    print(f"      Freshness: newest price {fresh_age} (other side {stale_age})")
                else:
                    print(f"      Freshness: newest price {fresh_age}")
            print()

    make_table(true_arbs, "✅ TRUE ARBITRAGE OPPORTUNITIES")
    if SHOW_NEAR_ARBS:
        make_table(near_arbs, f"📊 NEAR-ARB (within {abs(ARB_THRESHOLD):.0f}% — validation data)")

    print(f"\n  Total comparisons shown: {len(all_results)}")
    print(f"  True arbs: {len(true_arbs)}  |  Near-arb: {len(near_arbs) if SHOW_NEAR_ARBS else 0}\n")


def display_best_moneylines(best_lines: list[dict], events_checked: int):
    """Render a simple table of best Moneyline per event (home & away)."""
    print(f"\n{'═'*80}")
    print(f"  BEST LINE VIEW  |  {events_checked} events with moneyline data")
    print(f"{'═'*80}")

    if not best_lines:
        print("\n  No moneyline data available for the selected sports/date window.\n")
        return

    table = PrettyTable(["Sport", "Game", "Home (Book @ Odds)", "Away (Book @ Odds)"])
    table.align = "l"
    for row in best_lines:
        home = row["home"]
        away = row["away"]
        home_str = f"{row['home_team']} – {home['book']} ({home['odds_am']:+})"
        away_str = f"{row['away_team']} – {away['book']} ({away['odds_am']:+})"
        table.add_row([row["sport"], row["game"], home_str, away_str])
    print(table)


def display_coverage(sport_event_counts: dict):
    t = PrettyTable(["Sport", "ID", "Events", "Lines Checked"])
    t.align = "l"
    total_e, total_l = 0, 0
    for sid, (sname, n) in sport_event_counts.items():
        nl = n * len(LINE_TYPES)
        t.add_row([sname, sid, n, nl])
        total_e += n
        total_l += nl
    t.add_row(["TOTAL", "—", total_e, total_l])
    print("\n── COVERAGE ──")
    print(t)


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    global KNOWN_BOOKS
    fresh = fetch_affiliates()
    if fresh:
        KNOWN_BOOKS = fresh
    else:
        KNOWN_BOOKS = dict(_KNOWN_BOOKS_FALLBACK)

    client = RundownClient(API_KEY, USE_RAPIDAPI)
    dates  = [
        (datetime.date.today() + datetime.timedelta(days=d)).strftime("%Y-%m-%d")
        for d in range(DAYS_AHEAD + 1)
    ]

    print("=" * 70)
    print("  TheRundown Arbitrage Finder  —  Full Line Coverage")
    sport_names = [ALL_SPORTS.get(s, f"Sport{s}") for s in SPORT_IDS]
    print(f"  Sports: {sport_names}")
    print(f"  Threshold: profit >= {ARB_THRESHOLD}%  (0 = real arb only)")
    print(f"  Mode: {'RapidAPI' if USE_RAPIDAPI else 'Direct'}")
    book_names = sorted(KNOWN_BOOKS.values())
    print(f"  Books available: {', '.join(book_names)}")
    print(f"  Data delay: 5 min (free tier)")
    print("=" * 70)

    all_results      = []
    all_best_lines   = []
    books_seen       = set()
    total_events     = 0
    sport_counts     = {}
    delta_cursors    = {}
    structure_dumped = False
    daily_exhausted  = False

    def _fetch_events_for_date(client, sport_id, sname, date_str):
        """Fetch events for one sport+date. Returns (events_list, got_quota_error)."""
        print(f"  [{sname}] {date_str}…", end=" ", flush=True)
        time.sleep(REQUEST_DELAY)
        try:
            data   = client.get_events(sport_id, date_str)
            events = (data or {}).get("events") or []
            cursor = (data or {}).get("meta", {}).get("delta_last_id")
            if cursor:
                delta_cursors[sport_id] = cursor
            dp_rem = client.last_headers.get("x-datapoints-remaining", "?")
            dp_lim = client.last_headers.get("x-datapoints-limit", "?")
            print(f"{len(events)} events  (dp: {dp_rem}/{dp_lim} remaining)")
            return events, False
        except requests.HTTPError as e:
            code = e.response.status_code
            hdrs = e.response.headers
            if code == 429:
                remaining = hdrs.get("x-datapoints-remaining", "?")
                retry_s   = int(hdrs.get("retry-after", "60"))
                if remaining == "0":
                    reset_time = (datetime.datetime.now() + datetime.timedelta(seconds=retry_s)).strftime("%H:%M")
                    used  = hdrs.get("x-datapoints-used", "?")
                    limit = hdrs.get("x-datapoints-limit", "?")
                    print(f"DAILY QUOTA EXHAUSTED ({used}/{limit} datapoints)")
                    print(f"  ✗ Resets at ~{reset_time} (in {retry_s // 60}m). Cannot continue until then.")
                    return [], True
                else:
                    print(f"rate limited — waiting 5s…")
                    time.sleep(5)
                    return [], False
            print(f"HTTP {code} — {'not on plan' if code in (403,404) else 'error'}")
            if code == 401:
                print("  ✗ Invalid API key.")
            return [], False
        except Exception as e:
            print(f"skipped ({e})")
            return [], False

    def _filter_live(events):
        """Apply LIVE_ONLY / PRE_GAME_MINUTES filter. Returns filtered list."""
        if not LIVE_ONLY:
            return list(events)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        kept = []
        for event in events:
            status = (event.get("score") or {}).get("event_status")
            if isinstance(status, str) and status.lower() in {"final", "complete", "closed"}:
                continue
            start_raw = event.get("event_date") or event.get("event_date_start")
            try:
                if not start_raw:
                    continue
                start_dt = datetime.datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=datetime.timezone.utc)
                delta_min = (now_utc - start_dt).total_seconds() / 60.0
                if delta_min <= PRE_GAME_MINUTES:
                    kept.append(event)
            except Exception:
                continue
        return kept

    def _analyze_events(filtered, sname):
        """Run analyze_event on a list of filtered events (arb mode)."""
        arbs = []
        lines = []
        bks  = set()
        for event in filtered:
            if VALIDATION_MODE and not structure_dumped:
                inspect_raw_structure(event, sname)
            results, r_lines = analyze_event(event, sname)
            for r in results:
                bks.add(r["book_a"])
                bks.add(r["book_b"])
            arbs.extend(results)
            lines.extend(r_lines)
        return arbs, lines, bks

    def _collect_best_lines(filtered, sname):
        """Collect best Moneyline per event (Best Line mode)."""
        bests = []
        bks = set()
        for event in filtered:
            best = compute_best_moneyline_for_event(event, sname)
            if not best:
                continue
            bests.append(best)
            bks.add(best["home"]["book"])
            bks.add(best["away"]["book"])
        return bests, bks

    for sport_id in SPORT_IDS:
        if daily_exhausted:
            break
        sname = ALL_SPORTS.get(sport_id, f"Sport{sport_id}")

        # -- Try today first --
        raw_today, quota_hit = _fetch_events_for_date(client, sport_id, sname, dates[0])
        if quota_hit:
            daily_exhausted = True
            break

        filtered_today = _filter_live(raw_today)

        if BEST_LINES_MODE:
            best_today, books_today = _collect_best_lines(filtered_today, sname)
            if best_today:
                sport_counts[sport_id] = (sname, len(filtered_today))
                total_events += len(filtered_today)
                books_seen.update(books_today)
                all_best_lines.extend(best_today)
            else:
                if len(dates) > 1:
                    print(f"  [{sname}] no moneylines today, checking {dates[1]}…")
                    raw_tmrw, quota_hit = _fetch_events_for_date(client, sport_id, sname, dates[1])
                    if quota_hit:
                        daily_exhausted = True
                        break
                    filtered_tmrw = _filter_live(raw_tmrw)
                    best_tmrw, books_tmrw = _collect_best_lines(filtered_tmrw, sname)
                    sport_counts[sport_id] = (sname, len(filtered_tmrw))
                    total_events += len(filtered_tmrw)
                    books_seen.update(books_tmrw)
                    all_best_lines.extend(best_tmrw)
                else:
                    sport_counts[sport_id] = (sname, len(filtered_today))
                    total_events += len(filtered_today)
        else:
            arbs_today, lines_today, books_today = _analyze_events(filtered_today, sname)

            if arbs_today:
                sport_counts[sport_id] = (sname, len(filtered_today))
                total_events += len(filtered_today)
                books_seen.update(books_today)
                all_results.extend(arbs_today)
            else:
                # -- Fallback to tomorrow if no arbs today and DAYS_AHEAD >= 1 --
                if len(dates) > 1:
                    print(f"  [{sname}] no arbs today, checking {dates[1]}…")
                    raw_tmrw, quota_hit = _fetch_events_for_date(client, sport_id, sname, dates[1])
                    if quota_hit:
                        daily_exhausted = True
                        break
                    filtered_tmrw = _filter_live(raw_tmrw)
                    arbs_tmrw, lines_tmrw, books_tmrw = _analyze_events(filtered_tmrw, sname)
                    sport_counts[sport_id] = (sname, len(filtered_tmrw))
                    total_events += len(filtered_tmrw)
                    books_seen.update(books_tmrw)
                    all_results.extend(arbs_tmrw)
                else:
                    sport_counts[sport_id] = (sname, len(filtered_today))
                    total_events += len(filtered_today)

    display_coverage(sport_counts)
    if BEST_LINES_MODE:
        display_best_moneylines(all_best_lines, total_events)
        return
    display_results(all_results, books_seen, total_events)

    # ── Delta polling ──────────────────────────
    if not delta_cursors:
        print("  ⚠  No delta cursors — one-shot scan only.\n")
        return

    print(f"  Delta polling every {POLL_INTERVAL}s. Ctrl-C to stop.\n")
    poll_n = 0
    while True:
        try:
            time.sleep(POLL_INTERVAL)
            poll_n += 1
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] Poll #{poll_n}")
            round_results = []
            round_events = 0

            for sport_id, last_id in list(delta_cursors.items()):
                sname = ALL_SPORTS.get(sport_id, f"Sport{sport_id}")
                time.sleep(REQUEST_DELAY)
                try:
                    raw    = client.get_delta(sport_id, last_id)
                    evts   = (raw or {}).get("events") or []
                    cursor = (raw or {}).get("meta", {}).get("delta_last_id", last_id)
                    delta_cursors[sport_id] = cursor
                    print(f"  [{sname}] {len(evts)} updated")
                    round_events += len(evts)
                    for event in evts:
                        for raw_key in event.get("lines", {}).keys():
                            try:
                                bid = int(raw_key)
                                if bid in KNOWN_BOOKS:
                                    books_seen.add(KNOWN_BOOKS[bid])
                            except (ValueError, TypeError):
                                pass
                        round_results.extend(analyze_event(event, sname))
                except requests.HTTPError as e:
                    if e.response.status_code == 429:
                        print(f"  ⚠ Rate limited, waiting 15s…")
                        time.sleep(15)
                    else:
                        print(f"  ✗ [{sname}] HTTP {e.response.status_code}")

            if round_results:
                display_results(round_results, books_seen, round_events)
            else:
                print(f"  No comparisons in threshold range this poll.")

        except KeyboardInterrupt:
            print("\n  Stopped. Goodbye!")
            break
        except Exception as e:
            print(f"  ✗ Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
