"""
novig_scraper.py
================
Fetch live odds from the Novig exchange via their public GraphQL + REST APIs.

Novig is a sports betting exchange with order-book style bids/asks.
Public asks are typically empty, so we derive effective taker prices:
    effective_ask_for_A = 1 - best_bid_for_B

This avoids the heavyweight Playwright approach and calls APIs directly.

SETUP: pip install aiohttp certifi
"""
import asyncio
import logging
import math
import ssl
from datetime import datetime, timezone
from typing import Any

import aiohttp
import certifi

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

NOVIG_GRAPHQL_URL = "https://api.novig.us/v1/graphql"
NOVIG_BOOK_URL    = "https://api.novig.us/nbx/v1/markets/book/batch"
TIMEOUT_S         = 12

# Minimum liquidity (in USD) for a price level to be considered valid.
# Change this value at any time to adjust the filter.
NOVIG_MIN_LIQUIDITY: float = 50.0

# Max markets per order-book batch request (Novig may limit URL length)
_BOOK_BATCH_SIZE = 20

# ─────────────────────────────────────────────
# SPORT MAPPING
# ─────────────────────────────────────────────
# Our internal sport keys → Novig league names (from GraphQL `league` field)
_SPORT_TO_LEAGUES: dict[str, list[str]] = {
    "nba":    ["NBA"],
    "nfl":    ["NFL"],
    "nhl":    ["NHL"],
    "mlb":    ["MLB"],
    "ncaab":  ["NCAAB"],
    "ncaaf":  ["NCAAF"],
    "ncaawb": ["NCAAWB"],
    "mma":    ["UFC"],
}

ALL_SPORT_KEYS = list(_SPORT_TO_LEAGUES.keys())


# ─────────────────────────────────────────────
# ODDS CONVERSION
# ─────────────────────────────────────────────

def _decimal_to_american(dec: float) -> int:
    """Decimal odds → American odds integer."""
    if dec >= 2.0:
        return round((dec - 1) * 100)
    elif dec > 1.0:
        return round(-100 / (dec - 1))
    return 0


def _prob_to_decimal(prob: float) -> float:
    """0-1 probability → decimal odds. No fee adjustment (exchange spread only)."""
    if prob <= 0 or prob >= 1:
        return 0.0
    return round(1.0 / prob, 6)


# ─────────────────────────────────────────────
# GRAPHQL: FETCH EVENTS
# ─────────────────────────────────────────────

_HOME_QUERY = """
query Home_Query($where_event: event_bool_exp, $order_by_event: [event_order_by!], $limit_count: Int!) @cached(ttl: 5) {
  event(where: $where_event, order_by: $order_by_event, limit: $limit_count) {
    id
    type
    description
    status
    league
    scheduled_start
    game {
      id
      sport
      league
      homeTeam { id name symbol short_name }
      awayTeam { id name symbol short_name }
      __typename
    }
    markets(where: {status: {_eq: "OPEN"}}) {
      id
      type
      strike
      status
      description
      outcomes { id index description }
      __typename
    }
    __typename
  }
}
"""


def _build_event_where(leagues: list[str]) -> dict:
    """Build the GraphQL where clause filtering by league and open status."""
    return {
        "_and": [
            {"league": {"_in": leagues}},
            {"_or": [
                {"_and": [
                    {"_or": [
                        {"status": {"_eq": "CLOSED_PREGAME"}},
                        {"status": {"_eq": "OPEN_PREGAME"}},
                    ]},
                    {"is_visible_pregame": {"_eq": True}},
                    {"markets": {"status": {"_eq": "OPEN"}}},
                ]},
                {"_and": [
                    {"status": {"_eq": "OPEN_INGAME"}},
                    {"is_visible_live": {"_eq": True}},
                ]},
            ]},
        ]
    }


async def _fetch_events(
    session: aiohttp.ClientSession,
    leagues: list[str],
) -> list[dict]:
    """Fetch events from Novig GraphQL API."""
    payload = {
        "operationName": "Home_Query",
        "variables": {
            "where_event": _build_event_where(leagues),
            "order_by_event": [{"status": "asc"}, {"scheduled_start": "asc"}],
            "limit_count": 200,
        },
        "query": _HOME_QUERY,
    }
    try:
        async with session.post(
            NOVIG_GRAPHQL_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
        ) as resp:
            if resp.status != 200:
                logger.warning("Novig GraphQL returned HTTP %d", resp.status)
                return []
            data = await resp.json()
            if data.get("errors"):
                logger.warning("Novig GraphQL errors: %s", data["errors"])
            return data.get("data", {}).get("event", [])
    except Exception as e:
        logger.error("Novig GraphQL fetch failed: %s", e)
        return []


# ─────────────────────────────────────────────
# ORDER BOOK: FETCH PRICES
# ─────────────────────────────────────────────

async def _fetch_order_books(
    session: aiohttp.ClientSession,
    market_ids: list[str],
) -> dict[str, dict]:
    """
    Fetch order book data for a batch of market IDs.
    Returns {market_id: {outcome_id: {"bids": [...], "asks": [...]}}}
    """
    result: dict[str, dict] = {}
    # Split into batches to avoid URL length limits
    for i in range(0, len(market_ids), _BOOK_BATCH_SIZE):
        batch = market_ids[i : i + _BOOK_BATCH_SIZE]
        ids_param = ",".join(batch)
        url = f"{NOVIG_BOOK_URL}?marketIds={ids_param}&currency=CASH"
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Novig book batch HTTP %d", resp.status)
                    continue
                data = await resp.json()
                if not isinstance(data, list):
                    continue
                for entry in data:
                    mkt = entry.get("market", {})
                    mid = mkt.get("id", "")
                    ladders = entry.get("ladders", {})
                    if mid and ladders:
                        result[mid] = ladders
        except Exception as e:
            logger.warning("Novig book batch failed: %s", e)
    return result


def _best_bid(ladder: dict, min_liq_dollars: float) -> tuple[float, float] | None:
    """
    Find the best (highest) bid with sufficient liquidity.
    Returns (price, qty_dollars) or None.
    Novig qty is in cents — we convert to dollars before comparing.
    """
    bids = ladder.get("bids", [])
    if not bids:
        return None
    for bid in bids:  # bids are pre-sorted best-first
        price = bid.get("price", 0)
        qty = bid.get("qty", 0)
        if isinstance(price, (int, float)) and isinstance(qty, (int, float)):
            qty_dollars = qty / 100.0  # cents → dollars
            if qty_dollars >= min_liq_dollars and 0 < price < 1:
                return (float(price), qty_dollars)
    return None


# ─────────────────────────────────────────────
# EVENT PARSING
# ─────────────────────────────────────────────

def _parse_event(
    raw: dict,
    books: dict[str, dict],
    sport_key: str,
    fetch_ts: str,
    min_liq: float,
) -> dict[str, Any] | None:
    """
    Parse a single Novig event into the standard event dict.
    Only includes markets with sufficient liquidity on both sides.
    """
    game = raw.get("game")
    if not game:
        return None

    home_team_obj = game.get("homeTeam") or {}
    away_team_obj = game.get("awayTeam") or {}
    home_team = home_team_obj.get("name", "")
    away_team = away_team_obj.get("name", "")
    home_short = home_team_obj.get("short_name") or home_team_obj.get("symbol") or ""
    away_short = away_team_obj.get("short_name") or away_team_obj.get("symbol") or ""

    if not home_team or not away_team:
        return None

    event_id = raw.get("id", "")
    event_url = f"https://novig.com/event-markets/{event_id}" if event_id else "https://novig.com/events"
    start_time = raw.get("scheduled_start") or fetch_ts

    markets_out: list[dict[str, Any]] = []
    min_liq_dollars = min_liq  # already in dollars; _best_bid converts qty from cents internally

    for mkt in raw.get("markets", []):
        mkt_type = mkt.get("type", "")
        mkt_id = mkt.get("id", "")
        outcomes = mkt.get("outcomes", [])
        strike = mkt.get("strike")

        if len(outcomes) != 2:
            continue  # only handle binary markets

        ladders = books.get(mkt_id, {})
        if not ladders:
            continue

        o0 = outcomes[0]
        o1 = outcomes[1]
        o0_id = o0.get("id", "")
        o1_id = o1.get("id", "")
        o0_desc = o0.get("description", "")
        o1_desc = o1.get("description", "")

        ladder0 = ladders.get(o0_id, {})
        ladder1 = ladders.get(o1_id, {})

        # Get best bid for each outcome
        bid0 = _best_bid(ladder0, min_liq_dollars)
        bid1 = _best_bid(ladder1, min_liq_dollars)

        if bid0 is None or bid1 is None:
            continue

        bid0_price, bid0_liq = bid0
        bid1_price, bid1_liq = bid1

        # Derive effective ask (taker buy) prices:
        # ask_for_0 = 1 - bid_1, ask_for_1 = 1 - bid_0
        ask0 = 1.0 - bid1_price
        ask1 = 1.0 - bid0_price

        if ask0 <= 0 or ask0 >= 1 or ask1 <= 0 or ask1 >= 1:
            continue

        dec0 = _prob_to_decimal(ask0)
        dec1 = _prob_to_decimal(ask1)
        if dec0 <= 1.0 or dec1 <= 1.0:
            continue

        am0 = _decimal_to_american(dec0)
        am1 = _decimal_to_american(dec1)

        if mkt_type == "MONEY":
            # Moneyline — map outcomes to home/away
            sel0, sel1 = _map_outcomes_to_sides(
                o0_desc, o1_desc, home_short, away_short, home_team, away_team
            )
            if sel0 and sel1:
                markets_out.append(_market_dict("moneyline", sel0, o0_desc, am0, dec0, None, fetch_ts))
                markets_out.append(_market_dict("moneyline", sel1, o1_desc, am1, dec1, None, fetch_ts))

        elif mkt_type == "SPREAD":
            sel0, sel1 = _map_outcomes_to_sides(
                o0_desc, o1_desc, home_short, away_short, home_team, away_team
            )
            if sel0 and sel1 and strike is not None:
                # strike is the spread value from market description
                # outcome 0 typically has the negative spread (favorite), outcome 1 the positive
                lv0 = _parse_line_from_desc(o0_desc)
                lv1 = _parse_line_from_desc(o1_desc)
                if lv0 is not None and lv1 is not None:
                    markets_out.append(_market_dict("spread", sel0, o0_desc, am0, dec0, lv0, fetch_ts))
                    markets_out.append(_market_dict("spread", sel1, o1_desc, am1, dec1, lv1, fetch_ts))

        elif mkt_type == "TOTAL":
            if strike is not None:
                # Determine over/under from outcome descriptions
                sel0_ou = _parse_over_under(o0_desc)
                sel1_ou = _parse_over_under(o1_desc)
                if sel0_ou and sel1_ou:
                    lv = float(strike) if strike else None
                    markets_out.append(_market_dict("total", sel0_ou, o0_desc, am0, dec0, lv, fetch_ts))
                    markets_out.append(_market_dict("total", sel1_ou, o1_desc, am1, dec1, lv, fetch_ts))

    if not markets_out:
        return None

    return {
        "source": "novig",
        "sport": sport_key,
        "home_team": home_team,
        "away_team": away_team,
        "start_time": start_time,
        "event_url": event_url,
        "markets": markets_out,
    }


def _market_dict(
    market_type: str,
    selection: str,
    team: str,
    american_odds: int,
    decimal_odds: float,
    line_value: float | None,
    updated_at: str,
) -> dict[str, Any]:
    return {
        "market_type": market_type,
        "selection": selection,
        "team": team,
        "american_odds": american_odds,
        "decimal_odds": decimal_odds,
        "line_value": line_value,
        "affiliate_name": "Novig",
        "updated_at": updated_at,
    }


def _map_outcomes_to_sides(
    desc0: str, desc1: str,
    home_short: str, away_short: str,
    home_name: str, away_name: str,
) -> tuple[str | None, str | None]:
    """
    Map two outcome descriptions to "home"/"away" based on team name matching.
    Returns (sel0, sel1) or (None, None) if matching fails.
    """
    # Strip any spread/line numbers from descriptions for matching
    clean0 = desc0.split()[0] if desc0 else ""
    clean1 = desc1.split()[0] if desc1 else ""

    def _is_home(desc: str) -> bool:
        d = desc.lower().strip()
        for ref in [home_short.lower(), home_name.lower()]:
            if ref and (d.startswith(ref) or ref.startswith(d)):
                return True
        return False

    def _is_away(desc: str) -> bool:
        d = desc.lower().strip()
        for ref in [away_short.lower(), away_name.lower()]:
            if ref and (d.startswith(ref) or ref.startswith(d)):
                return True
        return False

    if _is_home(clean0) and _is_away(clean1):
        return "home", "away"
    elif _is_away(clean0) and _is_home(clean1):
        return "away", "home"
    elif _is_home(clean0):
        return "home", "away"
    elif _is_home(clean1):
        return "away", "home"
    elif _is_away(clean0):
        return "away", "home"
    elif _is_away(clean1):
        return "home", "away"

    # Fallback: cannot determine mapping
    logger.debug("Novig: cannot map outcomes %r / %r to home=%r away=%r",
                 desc0, desc1, home_short, away_short)
    return None, None


def _parse_line_from_desc(desc: str) -> float | None:
    """Extract numeric line value from outcome description like 'LAD -5.5' or 'OAK +5.5'."""
    import re
    m = re.search(r'([+-]?\d+\.?\d*)\s*$', desc)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _parse_over_under(desc: str) -> str | None:
    """Determine if an outcome description is 'over' or 'under'."""
    d = desc.lower().strip()
    if d.startswith("over") or d.startswith("o "):
        return "over"
    if d.startswith("under") or d.startswith("u "):
        return "under"
    return None


# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

async def fetch_novig_markets(
    sport_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch Novig events and order books for the given sports.
    Returns a list of event dicts in the standard scraper schema.
    """
    if sport_keys is None:
        sport_keys = ALL_SPORT_KEYS

    # Collect all league names for the requested sports
    leagues: list[str] = []
    sport_by_league: dict[str, str] = {}
    for sk in sport_keys:
        for league in _SPORT_TO_LEAGUES.get(sk, []):
            leagues.append(league)
            sport_by_league[league] = sk

    if not leagues:
        return []

    fetch_ts = datetime.now(timezone.utc).isoformat()

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    conn = aiohttp.TCPConnector(ssl=ssl_ctx)
    async with aiohttp.ClientSession(
        connector=conn,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        }
    ) as session:
        # Step 1: Fetch events via GraphQL
        raw_events = await _fetch_events(session, leagues)
        if not raw_events:
            logger.info("Novig: 0 events returned from GraphQL")
            return []

        # Step 2: Collect all market IDs that we care about (MONEY, SPREAD, TOTAL)
        market_ids: list[str] = []
        for ev in raw_events:
            for mkt in ev.get("markets", []):
                if mkt.get("type") in ("MONEY", "SPREAD", "TOTAL") and mkt.get("id"):
                    market_ids.append(mkt["id"])

        if not market_ids:
            logger.info("Novig: %d events but 0 relevant markets", len(raw_events))
            return []

        # Step 3: Fetch order books in batch
        books = await _fetch_order_books(session, market_ids)
        logger.info("Novig: %d events, %d markets, %d books fetched",
                     len(raw_events), len(market_ids), len(books))

    # Step 4: Parse events
    results: list[dict[str, Any]] = []
    for ev in raw_events:
        league = ev.get("league", "")
        sport_key = sport_by_league.get(league)
        if not sport_key:
            continue
        parsed = _parse_event(ev, books, sport_key, fetch_ts, NOVIG_MIN_LIQUIDITY)
        if parsed:
            results.append(parsed)

    logger.info("Novig: %d parsed events with valid markets", len(results))
    return results


# ─────────────────────────────────────────────
# CLI TEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    events = asyncio.run(fetch_novig_markets())
    print(json.dumps(events, indent=2, default=str))
    print(f"\nTotal: {len(events)} events")
