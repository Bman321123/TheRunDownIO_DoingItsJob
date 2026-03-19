"""
polymarket_scraper.py
Fetch active Polymarket two-outcome sports markets and normalise to the
market-dict schema used by therundown.build_market_index / analyze_event.

Uses Polymarket's Gamma Events API (/events) with series_id to get
sport-specific event groups containing H2H moneyline sub-markets.
"""
import re
import json
import time
import datetime
import requests
from typing import Any

POLY_BASE  = "https://gamma-api.polymarket.com"
TIMEOUT_S  = 12

_SPORT_TO_SERIES: dict[str, list[str]] = {
    "nba":    ["10345"],
    "nfl":    ["10187"],
    "nhl":    ["10346"],
    "mlb":    ["3"],
    "ncaab":  ["39"],
    "ncaawb": ["10471"],
    "mma":    ["10500"],
    "ncaaf":  ["10210"],
}

ALL_SPORT_KEYS = list(_SPORT_TO_SERIES.keys())


def _prob_to_american(prob: float) -> int | None:
    """Decimal probability (0.0-1.0) -> American odds integer."""
    if prob <= 0.01 or prob >= 0.99:
        return None
    if prob >= 0.5:
        return round(-prob / (1 - prob) * 100)
    return round((1 - prob) / prob * 100)


def fetch_polymarket_markets(sport_keys: list[str] | None = None) -> list[dict[str, Any]]:
    """
    Returns the same event-like dict format as kalshi_scraper.fetch_kalshi_markets(),
    but sourced from Polymarket. source = "polymarket".
    """
    if sport_keys is None:
        sport_keys = ALL_SPORT_KEYS

    all_events: list[dict[str, Any]] = []

    for i, sport in enumerate(sport_keys):
        series_list = _SPORT_TO_SERIES.get(sport, [])
        for series_id in series_list:
            if i > 0:
                time.sleep(0.15)
            events = _fetch_series_events(series_id)
            all_events.extend(events)

    return all_events


def _fetch_series_events(series_id: str) -> list[dict[str, Any]]:
    """Fetch all active event groups for one Polymarket series."""
    results: list[dict[str, Any]] = []

    try:
        resp = requests.get(
            f"{POLY_BASE}/events",
            params={"series_id": series_id, "active": "true", "closed": "false", "limit": 100},
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        event_groups = data if isinstance(data, list) else (data.get("data") or data.get("events") or [])
    except Exception as e:
        print(f"POLYMARKET_ERROR (series {series_id}): {e}")
        return []

    for group in event_groups:
        sub_markets = group.get("markets") or []
        parsed = _find_moneyline_market(group, sub_markets)
        if parsed:
            results.append(parsed)

    return results


def _find_moneyline_market(group: dict, sub_markets: list[dict]) -> dict[str, Any] | None:
    """
    Find the H2H moneyline market within an event group's sub-markets.
    The moneyline market's question matches the event title (e.g., "Warriors vs. Celtics")
    and has exactly 2 outcomes that are team names (not "Yes"/"No", "Over"/"Under").
    """
    group_title = str(group.get("title") or "").strip()
    if not group_title:
        return None

    for m in sub_markets:
        if m.get("closed"):
            continue

        question = str(m.get("question") or "").strip()

        raw_outcomes = m.get("outcomes") or "[]"
        try:
            outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        except Exception:
            continue
        if not isinstance(outcomes, list) or len(outcomes) != 2:
            continue

        o1 = str(outcomes[0]).strip()
        o2 = str(outcomes[1]).strip()

        if o1.lower() in ("yes", "no", "over", "under") or o2.lower() in ("yes", "no", "over", "under"):
            continue

        if question.lower() != group_title.lower():
            continue

        raw_prices = m.get("outcomePrices") or "[]"
        try:
            prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            prices = [float(p) for p in prices]
        except Exception:
            continue
        if len(prices) != 2:
            continue

        am_away = _prob_to_american(prices[0])
        am_home = _prob_to_american(prices[1])
        if am_away is None or am_home is None:
            continue

        away_team, home_team = _parse_teams_from_title(group_title)
        if not away_team or not home_team:
            away_team, home_team = o1, o2

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return {
            "home_team": home_team,
            "away_team": away_team,
            "source": "polymarket",
            "markets": [
                {
                    "market_type": "moneyline",
                    "selection": "home",
                    "affiliate_name": "Polymarket",
                    "american_odds": am_home,
                    "line_value": None,
                    "updated_at": now,
                },
                {
                    "market_type": "moneyline",
                    "selection": "away",
                    "affiliate_name": "Polymarket",
                    "american_odds": am_away,
                    "line_value": None,
                    "updated_at": now,
                },
            ],
        }

    return None


def _parse_teams_from_title(title: str) -> tuple[str, str]:
    """
    Extract (away_team, home_team) from Polymarket event titles.
    Pattern: 'Away vs. Home' or 'Away vs Home'
    """
    parts = re.split(r"\s+vs\.?\s+", title.strip(), flags=re.IGNORECASE)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return "", ""
