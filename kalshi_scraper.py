"""
kalshi_scraper.py
Fetch open Kalshi sports markets and normalise to the market-dict schema
used by therundown.build_market_index / analyze_event.

Uses Kalshi's Events API (/trade-api/v2/events) with series_ticker
to get individual H2H game events with nested sub-markets.
Prices are in yes_ask_dollars (0.00–1.00 USD).
"""
import re
import time
import datetime
import requests
from typing import Any

KALSHI_BASE = "https://api.elections.kalshi.com"
TIMEOUT_S   = 12
CLOB_MIN_SIZE = 1.0  # minimum executable notional for an accepted ask quote

# Platform fee rate — applied to PROFIT, not settlement value
KALSHI_FEE_RATE: float = 0.07  # ~7% of profit for sports taker orders

_SPORT_TO_SERIES: dict[str, list[str]] = {
    "nba":    ["KXNBAGAME"],
    "nfl":    ["KXNFLGAME"],
    "nhl":    ["KXNHLGAME"],
    "mlb":    ["KXMLBGAME"],
    "ncaab":  ["KXNCAAMBGAME"],
    "ncaawb": ["KXNCAAWBGAME"],
    "mma":    ["KXUFCFIGHT"],
    "ncaaf":  ["KXNCAAFGAME"],
}

ALL_SPORT_KEYS = list(_SPORT_TO_SERIES.keys())

# Series ticker → URL slug for deep links
# e.g. KXNHLGAME → "nhl-game", KXUFCFIGHT → "ufc-fight"
_SERIES_SLUG_MAP: dict[str, str] = {
    "KXNHLGAME":     "nhl-game",
    "KXNBAGAME":     "nba-game",
    "KXNFLGAME":     "nfl-game",
    "KXMLBGAME":     "mlb-game",
    "KXNCAAMBGAME":  "ncaamb-game",
    "KXNCAAWBGAME":  "ncaawb-game",
    "KXUFCFIGHT":    "ufc-fight",
    "KXNCAAFGAME":   "ncaaf-game",
}


def _series_to_slug(series_ticker: str) -> str:
    """Convert a Kalshi series ticker to the URL slug used in /markets/ paths."""
    slug = _SERIES_SLUG_MAP.get(series_ticker.upper())
    if slug:
        return slug
    # Fallback: strip "KX" prefix, split camelCase, lowercase, join with "-"
    base = series_ticker.upper().removeprefix("KX").lower()
    # Insert hyphen before the last known suffix
    for suffix in ("game", "fight"):
        if base.endswith(suffix):
            return f"{base[:-len(suffix)]}-{suffix}"
    return base


def _dollars_to_odds(price: float) -> tuple[int, float] | None:
    """
    Kalshi yes_ask_dollars (0.00–1.00 USD) -> (american_odds, decimal_odds) after fees.

    Fee is applied to PROFIT (net payout), not settlement value:
        net_payout   = 1 - fee_rate × (1 - price)
        decimal_odds = net_payout / price
    """
    if price <= 0.01 or price >= 0.99:
        return None

    net_payout = 1.0 - KALSHI_FEE_RATE * (1.0 - price)
    dec = net_payout / price
    if dec <= 1.0:
        return None

    if dec >= 2.0:
        am = round((dec - 1) * 100)
    else:
        am = round(-100 / (dec - 1))
    return am, round(dec, 6)


def _parse_teams_from_title(title: str) -> tuple[str, str] | None:
    """
    Extract (away_team, home_team) from Kalshi event title.

    "at"/"@" format is reliable (Away at Home).
    "vs"/"v" format is ambiguous and returns None.
    """
    cleaned = re.sub(r"\s*[\(\[].*", "", title.strip()).strip()
    cleaned = re.sub(r"^[A-Z0-9]+(?:/[A-Z0-9]+)?:\s*", "", cleaned).strip()

    low = cleaned.lower()
    for sep in (" at ", " @ "):
        if sep in low:
            idx = low.find(sep)
            away = cleaned[:idx].strip()
            home = cleaned[idx + len(sep):].strip()
            if away and home:
                return away, home
    return None


def _walk_yes_ask_from_orderbook(orderbook_fp: dict[str, Any], min_notional: float) -> float | None:
    """
    Walk executable YES ask from Kalshi orderbook.

    The orderbook exposes bids; YES asks are derived from NO bids:
        yes_ask = 1 - no_bid
    """
    no_levels = orderbook_fp.get("no_dollars") or []
    if not isinstance(no_levels, list):
        return None

    cum_notional = 0.0
    worst_yes_ask = None
    for level in no_levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        no_bid = _safe_float(level[0])
        size = _safe_float(level[1])
        if no_bid is None or size is None or size <= 0:
            continue
        yes_ask = 1.0 - no_bid
        if not (0 < yes_ask < 1):
            continue
        cum_notional += yes_ask * size
        worst_yes_ask = yes_ask
        if cum_notional >= min_notional:
            return worst_yes_ask
    return None


def _resolve_executable_yes_ask(
    market: dict[str, Any],
    orderbook_cache: dict[str, float | None],
    min_notional: float = CLOB_MIN_SIZE,
) -> float | None:
    """
    Resolve executable YES ask for a Kalshi market.

    Prefers yes_ask_dollars when top-of-book size can fill min_notional;
    otherwise walks the orderbook depth.
    """
    ask_dollars = _safe_float(market.get("yes_ask_dollars"))

    # Use the displayed ask price when available — it's the best top-of-book quote.
    # The orderbook depth walk often returns worse (deeper) prices that are misleading.
    if ask_dollars is not None:
        return ask_dollars

    # Last-resort fallback if *_dollars is absent.
    return _safe_float(market.get("yes_ask"))


def fetch_kalshi_markets(sport_keys: list[str] | None = None) -> list[dict[str, Any]]:
    """
    Returns a list of event-like dicts, one per unique game found:
    [
      {
        "home_team": str,
        "away_team": str,
        "source":    "kalshi",
        "markets":   [
          { "market_type": "moneyline", "selection": "home"|"away",
            "affiliate_name": "Kalshi", "american_odds": int,
            "line_value": None, "updated_at": str },
          ...
        ]
      }, ...
    ]
    """
    if sport_keys is None:
        sport_keys = ALL_SPORT_KEYS

    all_events: list[dict[str, Any]] = []

    for i, sport in enumerate(sport_keys):
        series_list = _SPORT_TO_SERIES.get(sport, [])
        for series_ticker in series_list:
            if i > 0:
                time.sleep(0.25)
            events = _fetch_series(series_ticker)
            all_events.extend(events)

    return all_events


def _fetch_series(series_ticker: str) -> list[dict[str, Any]]:
    """Fetch all open events for one Kalshi series via the events endpoint."""
    results: list[dict[str, Any]] = []
    cursor: str | None = None
    orderbook_cache: dict[str, float | None] = {}

    while True:
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "with_nested_markets": "true",
            "status": "open",
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor

        try:
            resp = requests.get(
                f"{KALSHI_BASE}/trade-api/v2/events",
                params=params,
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"KALSHI_ERROR ({series_ticker}): {e}")
            break

        raw_events = data.get("events") or []
        for raw in raw_events:
            parsed = _parse_event(raw, orderbook_cache)
            if parsed:
                results.append(parsed)

        if data.get("limited") and data.get("cursor"):
            cursor = data["cursor"]
        else:
            break

    return results


def _parse_event(raw: dict, orderbook_cache: dict[str, float | None]) -> dict[str, Any] | None:
    """Parse a single Kalshi event (with nested sub-markets) into a normalized event dict."""
    title = raw.get("title") or ""
    markets = raw.get("markets") or []
    active = [m for m in markets if m.get("status") in ("open", "active") and not m.get("result")]

    if len(active) != 2:
        return None

    parsed = _parse_teams_from_title(title)

    sub_a = active[0]
    sub_b = active[1]
    name_a = _extract_team_from_sub_title(sub_a)
    name_b = _extract_team_from_sub_title(sub_b)

    price_a = _resolve_executable_yes_ask(sub_a, orderbook_cache, min_notional=CLOB_MIN_SIZE)
    price_b = _resolve_executable_yes_ask(sub_b, orderbook_cache, min_notional=CLOB_MIN_SIZE)

    if price_a is None or price_b is None:
        return None

    # Reject events where both sides' ask prices are too low (thin books / phantom arbs)
    if price_a + price_b < 0.85:
        print(f"KALSHI_SKIP: thin books — price_a={price_a:.3f} + price_b={price_b:.3f} = "
              f"{price_a + price_b:.3f} < 0.85 for {title!r}")
        return None

    odds_a = _dollars_to_odds(price_a)
    odds_b = _dollars_to_odds(price_b)
    if odds_a is None or odds_b is None:
        return None

    am_a, dec_a = odds_a
    am_b, dec_b = odds_b

    home_am: int
    away_am: int
    home_dec: float
    away_dec: float
    home_team: str
    away_team: str

    if parsed is not None:
        away_raw, home_raw = parsed
        if _name_matches(name_a, home_raw):
            home_team, away_team = name_a or home_raw, name_b or away_raw
            home_am, away_am = am_a, am_b
            home_dec, away_dec = dec_a, dec_b
        elif _name_matches(name_b, home_raw):
            home_team, away_team = name_b or home_raw, name_a or away_raw
            home_am, away_am = am_b, am_a
            home_dec, away_dec = dec_b, dec_a
        elif _name_matches(name_a, away_raw):
            home_team, away_team = name_b or home_raw, name_a or away_raw
            home_am, away_am = am_b, am_a
            home_dec, away_dec = dec_b, dec_a
        elif _name_matches(name_b, away_raw):
            home_team, away_team = name_a or home_raw, name_b or away_raw
            home_am, away_am = am_a, am_b
            home_dec, away_dec = dec_a, dec_b
        else:
            # Also try matching sub-market tickers against title team names
            ticker_a = (sub_a.get("ticker") or "").lower()
            ticker_b = (sub_b.get("ticker") or "").lower()
            home_low = home_raw.lower().replace(" ", "")

            if any(tok in ticker_a for tok in home_low.split() if len(tok) > 2) or home_low in ticker_a:
                home_team, away_team = name_a or home_raw, name_b or away_raw
                home_am, away_am = am_a, am_b
                home_dec, away_dec = dec_a, dec_b
            elif any(tok in ticker_b for tok in home_low.split() if len(tok) > 2) or home_low in ticker_b:
                home_team, away_team = name_b or home_raw, name_a or away_raw
                home_am, away_am = am_b, am_a
                home_dec, away_dec = dec_b, dec_a
            else:
                # Cannot reliably determine home/away — skip rather than guess
                print(f"KALSHI_SKIP: cannot match sub-markets to teams. title={title!r}, "
                      f"name_a={name_a!r}, name_b={name_b!r}, "
                      f"ticker_a={ticker_a!r}, ticker_b={ticker_b!r}")
                return None
    else:
        # "vs" titles are ambiguous on home/away ordering.  Kalshi almost always
        # uses "Away at Home" format which is handled above.  If we reach here
        # the title used "vs" and we cannot reliably determine home/away, so
        # skip this event rather than guess (alphabetical sort has no correlation
        # with actual home/away).
        return None

    # Build direct event URL: https://kalshi.com/markets/{series}/{slug}/{event}
    event_ticker = raw.get("event_ticker") or ""
    series_ticker = raw.get("series_ticker") or ""
    if event_ticker and series_ticker:
        slug = _series_to_slug(series_ticker)
        event_url = f"https://kalshi.com/markets/{series_ticker.lower()}/{slug}/{event_ticker.lower()}"
    else:
        event_url = "https://kalshi.com/browse/sports"

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "home_team": home_team,
        "away_team": away_team,
        "source": "kalshi",
        "event_url": event_url,
        "markets": [
            {
                "market_type": "moneyline",
                "selection": "home",
                "affiliate_name": "Kalshi",
                "american_odds": home_am,
                "decimal_odds": home_dec,
                "line_value": None,
                "updated_at": now,
            },
            {
                "market_type": "moneyline",
                "selection": "away",
                "affiliate_name": "Kalshi",
                "american_odds": away_am,
                "decimal_odds": away_dec,
                "line_value": None,
                "updated_at": now,
            },
        ],
    }


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _extract_team_from_sub_title(sub_market: dict) -> str:
    """
    Extract a team name from a sub-market using all available fields:
    yes_sub_title, subtitle, and the market's own title (e.g. "Will UConn win?").
    """
    for field in ("yes_sub_title", "subtitle"):
        val = (sub_market.get(field) or "").strip()
        if val:
            return val

    # Try extracting from the market title, e.g. "Will UConn win?"
    mtitle = (sub_market.get("title") or "").strip()
    m = re.match(r"(?:will\s+)?(.+?)\s+win\b", mtitle, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return mtitle


# Map common Kalshi abbreviations / alternate names to title-form names.
# Kalshi yes_sub_title often uses "OTT Senators" while the event title says "Ottawa".
_KALSHI_ALIAS: dict[str, list[str]] = {
    # NHL
    "ott": ["ottawa"], "nyr": ["new york r", "rangers"], "nyi": ["new york i", "islanders"],
    "la kings": ["los angeles"], "lak": ["los angeles"], "uta mammoth": ["utah"],
    "tor": ["toronto"], "mtl": ["montreal"], "van": ["vancouver"],
    "wpg": ["winnipeg"], "cgy": ["calgary"], "edm": ["edmonton"],
    "det": ["detroit"], "chi": ["chicago"], "stl": ["st. louis", "st louis"],
    "nsh": ["nashville"], "dal": ["dallas"], "col": ["colorado"],
    "min": ["minnesota"], "fla": ["florida"], "tbl": ["tampa bay", "tampa"],
    "car": ["carolina"], "cbj": ["columbus"], "pit": ["pittsburgh"],
    "phi": ["philadelphia"], "buf": ["buffalo"], "bos": ["boston"],
    "wsh": ["washington"], "nj": ["new jersey"], "sea": ["seattle"],
    "sj": ["san jose"], "ana": ["anaheim"],
    # NBA short forms
    "phx": ["phoenix"], "mil": ["milwaukee"], "gsw": ["golden state"],
    "lac": ["la clippers", "los angeles c"], "lal": ["la lakers", "los angeles l"],
    "okc": ["oklahoma"], "por": ["portland"], "sac": ["sacramento"],
    "cha": ["charlotte"], "ind": ["indiana"], "atl": ["atlanta"],
    "mem": ["memphis"], "hou": ["houston"], "den": ["denver"],
    "sas": ["san antonio"], "orl": ["orlando"], "bkn": ["brooklyn"],
    "nop": ["new orleans"],
    # MLB
    "az": ["arizona"], "lad": ["los angeles d", "dodgers"], "sf": ["san francisco"],
    "sd": ["san diego"], "cle": ["cleveland"], "nyy": ["new york y", "yankees"],
    "nym": ["new york m", "mets"], "cws": ["chicago w", "white sox"],
    "chc": ["chicago c", "cubs"], "kc": ["kansas city"],
    "tb": ["tampa bay"], "bal": ["baltimore"], "tex": ["texas"],
    "cin": ["cincinnati"], "oak": ["oakland"],
}


def _name_matches(subtitle: str, team_raw: str) -> bool:
    """Check if a sub-market label matches a team name (case-insensitive, alias-aware)."""
    a = subtitle.lower().strip()
    b = team_raw.lower().strip()
    if not a or not b:
        return False
    # Direct substring match
    if a in b or b in a:
        return True
    # Word overlap (for multi-word names)
    a_words = set(a.split())
    b_words = set(b.split())
    overlap = {w for w in (a_words & b_words) if len(w) > 2}
    if overlap:
        return True
    # Check alias table: does any token in `a` have an alias that matches `b`?
    for token in a.split():
        aliases = _KALSHI_ALIAS.get(token, [])
        # Also check full `a` string as key (e.g., "la kings")
        aliases = aliases or _KALSHI_ALIAS.get(a, [])
        for alias in aliases:
            if alias in b or b in alias:
                return True
    return False
