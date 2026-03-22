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

# Platform fee rate — applied to PROFIT, not settlement value
POLYMARKET_FEE_RATE: float = 0.02  # 2% of profit (taker)

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


def _prob_to_odds(prob: float) -> tuple[int, float] | None:
    """
    Polymarket price (0.0-1.0) -> (american_odds, decimal_odds) after fees.

    Fee is applied to PROFIT (net payout), not settlement value:
        net_payout   = 1 - fee_rate × (1 - prob)
        decimal_odds = net_payout / prob
    """
    if prob <= 0.01 or prob >= 0.99:
        return None

    net_payout = 1.0 - POLYMARKET_FEE_RATE * (1.0 - prob)
    dec = net_payout / prob
    if dec <= 1.0:
        return None

    if dec >= 2.0:
        am = round((dec - 1) * 100)
    else:
        am = round(-100 / (dec - 1))
    return am, round(dec, 6)


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
            # Carry endDate as start_time for cross-date matching guards
            end_date = group.get("endDate") or group.get("end_date_iso")
            if end_date:
                parsed["start_time"] = str(end_date)
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

        # Polymarket convention (verified empirically against ESPN schedules):
        # outcomes[0] / prices[0] = away team (first in "X vs Y" title)
        # outcomes[1] / prices[1] = home team (second in title)
        # Reject events where both sides' prices are too low (thin books)
        if prices[0] + prices[1] < 0.85:
            continue

        odds_away = _prob_to_odds(prices[0])
        odds_home = _prob_to_odds(prices[1])
        if odds_away is None or odds_home is None:
            continue
        am_away, dec_away = odds_away
        am_home, dec_home = odds_home

        away_team, home_team = _parse_teams_from_title(group_title)

        def _looks_generic_team_label(team: str) -> bool:
            return str(team or "").strip().lower() in ("home", "away")

        # If outcomes/questions devolve into generic "Away"/"Home" labels, recover
        # the real teams from the group title (it should contain the actual matchup).
        if (
            not away_team
            or not home_team
            or _looks_generic_team_label(away_team)
            or _looks_generic_team_label(home_team)
        ):
            parsed = _parse_teams_from_title(group_title)
            if parsed and parsed[0] and parsed[1] and not (
                _looks_generic_team_label(parsed[0]) or _looks_generic_team_label(parsed[1])
            ):
                away_team, home_team = parsed
            else:
                away_team, home_team = o1, o2

        # If we still couldn't resolve real team labels, skip the event.
        if _looks_generic_team_label(away_team) or _looks_generic_team_label(home_team):
            return None

        # Build direct event URL from the group slug
        slug = group.get("slug") or ""
        event_url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com/sports"

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return {
            "home_team": home_team,
            "away_team": away_team,
            "source": "polymarket",
            "event_url": event_url,
            "markets": [
                {
                    "market_type": "moneyline",
                    "selection": "home",
                    "affiliate_name": "Polymarket",
                    "american_odds": am_home,
                    "decimal_odds": dec_home,
                    "line_value": None,
                    "updated_at": now,
                },
                {
                    "market_type": "moneyline",
                    "selection": "away",
                    "affiliate_name": "Polymarket",
                    "american_odds": am_away,
                    "decimal_odds": dec_away,
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
