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


def _dollars_to_american(price: float) -> int | None:
    """
    Kalshi yes_ask_dollars (0.00–1.00 USD) -> American odds integer after fees.

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
        return round((dec - 1) * 100)
    return round(-100 / (dec - 1))


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
    ask_size_fp = _safe_float(market.get("yes_ask_size_fp"))
    if ask_dollars is not None:
        top_notional = ask_dollars * ask_size_fp if ask_size_fp is not None else 0.0
        if top_notional >= min_notional:
            return ask_dollars

        ticker = str(market.get("ticker") or "")
        if not ticker:
            return None
        if ticker in orderbook_cache:
            return orderbook_cache[ticker]

        try:
            resp = requests.get(
                f"{KALSHI_BASE}/trade-api/v2/markets/{ticker}/orderbook",
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json()
            orderbook_fp = data.get("orderbook_fp") or {}
            ask_from_book = _walk_yes_ask_from_orderbook(orderbook_fp, min_notional=min_notional)
            orderbook_cache[ticker] = ask_from_book
            return ask_from_book
        except Exception:
            orderbook_cache[ticker] = None
            return None

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
    name_a = (sub_a.get("yes_sub_title") or sub_a.get("subtitle") or "").strip()
    name_b = (sub_b.get("yes_sub_title") or sub_b.get("subtitle") or "").strip()

    price_a = _resolve_executable_yes_ask(sub_a, orderbook_cache, min_notional=CLOB_MIN_SIZE)
    price_b = _resolve_executable_yes_ask(sub_b, orderbook_cache, min_notional=CLOB_MIN_SIZE)

    if price_a is None or price_b is None:
        return None

    am_a = _dollars_to_american(price_a)
    am_b = _dollars_to_american(price_b)
    if am_a is None or am_b is None:
        return None

    home_price: int
    away_price: int
    home_team: str
    away_team: str

    if parsed is not None:
        away_raw, home_raw = parsed
        if _name_matches(name_a, home_raw):
            home_team, away_team = name_a or home_raw, name_b or away_raw
            home_price, away_price = am_a, am_b
        elif _name_matches(name_b, home_raw):
            home_team, away_team = name_b or home_raw, name_a or away_raw
            home_price, away_price = am_b, am_a
        elif _name_matches(name_a, away_raw):
            home_team, away_team = name_b or home_raw, name_a or away_raw
            home_price, away_price = am_b, am_a
        elif _name_matches(name_b, away_raw):
            home_team, away_team = name_a or home_raw, name_b or away_raw
            home_price, away_price = am_a, am_b
        else:
            home_team, away_team = home_raw, away_raw
            home_price, away_price = am_b, am_a
    else:
        # "vs" titles are ambiguous on home/away ordering.
        if not name_a or not name_b:
            return None
        if name_a.lower() <= name_b.lower():
            home_team, away_team = name_a, name_b
            home_price, away_price = am_a, am_b
        else:
            home_team, away_team = name_b, name_a
            home_price, away_price = am_b, am_a

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "home_team": home_team,
        "away_team": away_team,
        "source": "kalshi",
        "markets": [
            {
                "market_type": "moneyline",
                "selection": "home",
                "affiliate_name": "Kalshi",
                "american_odds": home_price,
                "line_value": None,
                "updated_at": now,
            },
            {
                "market_type": "moneyline",
                "selection": "away",
                "affiliate_name": "Kalshi",
                "american_odds": away_price,
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


def _name_matches(subtitle: str, team_raw: str) -> bool:
    """Check if a sub-market label matches a team name (case-insensitive substring)."""
    a = subtitle.lower().strip()
    b = team_raw.lower().strip()
    if not a or not b:
        return False
    return a in b or b in a
