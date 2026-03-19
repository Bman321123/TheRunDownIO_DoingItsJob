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
    """Kalshi yes_ask_dollars (0.00–1.00 USD) -> American odds integer."""
    if price <= 0.01 or price >= 0.99:
        return None
    if price >= 0.50:
        return round(-price / (1 - price) * 100)
    return round((1 - price) / price * 100)


def _parse_teams_from_title(title: str) -> tuple[str, str]:
    """
    Extract (away_team, home_team) from Kalshi event titles.
    Pattern: 'Away at Home' or 'Away vs Home'
    Returns ("", "") if parsing fails.
    """
    parts = re.split(r"(?i)\s+at\s+|\s+vs\.?\s+", title.strip())
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return "", ""


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
            parsed = _parse_event(raw)
            if parsed:
                results.append(parsed)

        if data.get("limited") and data.get("cursor"):
            cursor = data["cursor"]
        else:
            break

    return results


def _parse_event(raw: dict) -> dict[str, Any] | None:
    """Parse a single Kalshi event (with nested sub-markets) into a normalized event dict."""
    title = raw.get("title") or ""
    markets = raw.get("markets") or []
    active = [m for m in markets if m.get("status") in ("open", "active") and not m.get("result")]

    if len(active) != 2:
        return None

    away_raw, home_raw = _parse_teams_from_title(title)
    if not away_raw or not home_raw:
        return None

    sub_a = active[0]
    sub_b = active[1]
    name_a = (sub_a.get("yes_sub_title") or sub_a.get("subtitle") or "").strip()
    name_b = (sub_b.get("yes_sub_title") or sub_b.get("subtitle") or "").strip()

    price_a = _safe_float(sub_a.get("yes_ask_dollars")) or _safe_float(sub_a.get("yes_ask"))
    price_b = _safe_float(sub_b.get("yes_ask_dollars")) or _safe_float(sub_b.get("yes_ask"))

    if not price_a or not price_b:
        return None

    am_a = _dollars_to_american(price_a)
    am_b = _dollars_to_american(price_b)
    if am_a is None or am_b is None:
        return None

    home_price, away_price = am_a, am_b
    home_team, away_team = home_raw, away_raw

    if _name_matches(name_a, home_raw):
        home_price, away_price = am_a, am_b
    elif _name_matches(name_b, home_raw):
        home_price, away_price = am_b, am_a
    elif _name_matches(name_a, away_raw):
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
