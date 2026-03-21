"""
Standalone Bovada sportsbook scraper.

This module fetches Bovada event JSON and returns plain Python dicts suitable
for downstream merging/arbitrage workflows.

Scope notes:
- Includes full-game moneyline/spread/total markets.
- Excludes player props in this standalone version.
- Uses decimal odds as the source of truth when both decimal/american exist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import ssl
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
import certifi

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BACKOFF_BASE = 2
_RETRY_STATUSES = {403, 429}

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

_SEC_CH_UA_VALUES = [
    '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    '"Chromium";v="123", "Google Chrome";v="123", "Not-A.Brand";v="99"',
    '"Chromium";v="124", "Microsoft Edge";v="124", "Not-A.Brand";v="99"',
]

_BOVADA_MARKET_MAP: dict[str, str] = {
    "moneyline": "moneyline",
    "point spread": "spread",
    "run line": "spread",
    "puck line": "spread",
    "total": "total",
    "total runs": "total",
    "total goals": "total",
    "total goals o/u": "total",   # Bovada full-game O/U label (explicit match)
    "total points o/u": "total",  # same pattern for basketball/football
}

# Substrings that, if found in a market description, disqualify it from being
# treated as a game-level total.  Checked BEFORE the positive-match loop so
# team/player-specific markets are never mis-classified.
_BOVADA_MARKET_EXCLUDE: set[str] = {
    "team total",      # "Team Total Goals" or "Team Total"
    "player total",    # player prop totals
    "total goals o/u -",  # "Total Goals O/U - <Team Name>" (Bovada per-team format)
    "total points o/u -", # same pattern for basketball/football
    "total runs o/u -",   # same pattern for baseball
}

_DEFAULT_SPORT_PATHS: dict[str, str] = {
    "nfl": "football/nfl",
    "nba": "basketball/nba",
    "ncaab": "basketball/ncaab",
    "mlb": "baseball/mlb",
    "nhl": "hockey/nhl",
}

_ALLOWED_GROUPS = {"game lines", "alternate lines"}
_ALLOWED_PERIODS = {"game", "match", "", "live game", "live match"}


def _build_headers(referer: Optional[str] = None) -> dict[str, str]:
    ua = random.choice(_USER_AGENTS)
    headers: dict[str, str] = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Connection": "keep-alive",
    }
    if "Chrome" in ua or "Edg" in ua:
        headers["Sec-Ch-Ua"] = random.choice(_SEC_CH_UA_VALUES)
        headers["Sec-Ch-Ua-Mobile"] = "?0"
        headers["Sec-Ch-Ua-Platform"] = '"macOS"' if "Mac" in ua else '"Windows"'
    if referer:
        headers["Referer"] = referer
    return headers


async def fetch_json(url: str, referer: str, timeout_seconds: float = 30.0) -> Optional[Any]:
    """Fetch JSON from Bovada with browser-like headers and retry logic."""
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    connector = aiohttp.TCPConnector(limit_per_host=3, ssl=ssl_ctx, enable_cleanup_closed=True)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        for attempt in range(1, _MAX_RETRIES + 1):
            headers = _build_headers(referer)
            try:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        try:
                            return await resp.json(content_type=None)
                        except Exception:
                            text_body = await resp.text()
                            try:
                                return json.loads(text_body)
                            except Exception:
                                logger.error("Could not parse JSON response from %s", url)
                                return None

                    if resp.status in _RETRY_STATUSES:
                        delay = _BACKOFF_BASE ** attempt
                        logger.warning(
                            "Bovada status %s for %s; retrying in %ss (%s/%s)",
                            resp.status,
                            url,
                            delay,
                            attempt,
                            _MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        continue

                    logger.error("Bovada returned status %s for %s", resp.status, url)
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(
                    "Request error for %s (attempt %s/%s): %s",
                    url,
                    attempt,
                    _MAX_RETRIES,
                    exc,
                )
            await asyncio.sleep(_BACKOFF_BASE ** attempt)

    logger.error("Exhausted retries for %s", url)
    return None


def _parse_american_odds(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if text == "EVEN":
        return 100
    try:
        return int(text.replace("+", "").split(".")[0])
    except (ValueError, TypeError):
        return None


def _parse_decimal_odds(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _american_to_decimal(american: int) -> float:
    if american > 0:
        return round(1 + american / 100, 6)
    return round(1 + 100 / abs(american), 6)


def _decimal_to_american(decimal: float) -> int:
    if decimal >= 2.0:
        return int(round((decimal - 1) * 100))
    return int(round(-100 / (decimal - 1)))


def _parse_start_time(ts: Any) -> str:
    if ts is None:
        return datetime.now(tz=timezone.utc).isoformat()
    try:
        dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError, OSError):
        return datetime.now(tz=timezone.utc).isoformat()


def _extract_teams(raw_event: dict[str, Any]) -> tuple[str, str]:
    competitors = raw_event.get("competitors") or []
    home = ""
    away = ""

    for competitor in competitors:
        name = (competitor.get("name") or "").strip()
        if not name:
            continue
        if competitor.get("home") is True:
            home = name
        else:
            away = name

    if home and away:
        return home, away

    description = (raw_event.get("description") or "").strip()
    for sep in (" @ ", " vs ", " vs. ", " at ", " v "):
        if sep in description:
            left, right = description.split(sep, 1)
            return right.strip(), left.strip()
    return "", ""


def _resolve_market_type(description: str) -> Optional[str]:
    desc_lower = description.lower().strip()
    # Reject team-specific / player-specific totals before any positive match.
    for excl in _BOVADA_MARKET_EXCLUDE:
        if excl in desc_lower:
            return None
    direct = _BOVADA_MARKET_MAP.get(desc_lower)
    if direct:
        return direct
    for key, market_type in _BOVADA_MARKET_MAP.items():
        if key in desc_lower:
            return market_type
    return None


def _infer_selection(
    outcome: dict[str, Any],
    market_type: str,
    label: str,
    home_team: str = "",
    away_team: str = "",
) -> Optional[str]:
    label_lower = label.lower()
    if market_type == "total":
        if "over" in label_lower:
            return "over"
        if "under" in label_lower:
            return "under"

    otype = (outcome.get("type") or "").upper()
    if otype == "H":
        return "home"
    if otype == "A":
        return "away"

    # Fallback: match outcome label against known team names
    if home_team and home_team.lower() in label_lower:
        return "home"
    if away_team and away_team.lower() in label_lower:
        return "away"

    # Cannot determine side — return None to skip this outcome rather than
    # default to "home" which causes both sides to be labeled home.
    logger.warning("Bovada: cannot infer selection for outcome label=%r type=%r", label, otype)
    return None


def _parse_outcome(
    outcome: dict[str, Any],
    market_type: str,
    home_team: str = "",
    away_team: str = "",
) -> Optional[dict[str, Any]]:
    price = outcome.get("price") or {}
    if not price:
        return None

    odds_am = _parse_american_odds(price.get("american"))
    odds_dec = _parse_decimal_odds(price.get("decimal"))

    if odds_am is not None and odds_dec is None:
        odds_dec = _american_to_decimal(odds_am)
    if odds_dec is not None and odds_am is None:
        odds_am = _decimal_to_american(odds_dec)
    if odds_am is None or odds_dec is None:
        return None

    # Decimal is source of truth to avoid american-sign drift.
    odds_am = _decimal_to_american(odds_dec)

    line_value: Optional[float] = None
    if market_type in ("spread", "total"):
        handicap_raw = price.get("handicap")
        if handicap_raw is not None:
            try:
                line_value = float(handicap_raw)
            except (ValueError, TypeError):
                line_value = None

    label = (outcome.get("description") or "").strip()
    selection = _infer_selection(outcome, market_type, label, home_team, away_team)
    if selection is None:
        return None

    return {
        "market_type": market_type,
        "selection": selection,
        "team": label,
        "american_odds": odds_am,
        "decimal_odds": odds_dec,
        "line_value": line_value,
    }


def _extract_odds(
    raw_event: dict[str, Any],
    home_team: str = "",
    away_team: str = "",
) -> list[dict[str, Any]]:
    odds_lines: list[dict[str, Any]] = []

    for group in raw_event.get("displayGroups", []):
        group_desc = (group.get("description") or "").strip().lower()
        if group_desc not in _ALLOWED_GROUPS:
            continue

        for market in group.get("markets", []):
            market_desc = (market.get("description") or "").strip()
            period_desc = ((market.get("period") or {}).get("description") or "").strip().lower()
            if period_desc not in _ALLOWED_PERIODS:
                continue

            market_type = _resolve_market_type(market_desc)
            if market_type is None:
                continue

            for outcome in market.get("outcomes", []):
                line = _parse_outcome(outcome, market_type, home_team, away_team)
                if line is not None:
                    odds_lines.append(line)

    return odds_lines


def _parse_single_event(raw_event: dict[str, Any], sport: str) -> Optional[dict[str, Any]]:
    description = (raw_event.get("description") or "").strip()
    if not description:
        return None

    if raw_event.get("type") not in ("GAMEEVENT", None):
        return None

    home_team, away_team = _extract_teams(raw_event)
    if not home_team or not away_team:
        return None

    start_time = _parse_start_time(raw_event.get("startTime"))
    markets = _extract_odds(raw_event, home_team, away_team)
    link = raw_event.get("link") or ""
    event_url = f"https://www.bovada.lv/sports{link}" if link else ""

    return {
        "source": "bovada",
        "sport": sport,
        "home_team": home_team,
        "away_team": away_team,
        "start_time": start_time,
        "event_url": event_url,
        "markets": markets,
    }


def _parse_response(data: Any, sport: str) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    events: list[dict[str, Any]] = []
    for group in data:
        if not isinstance(group, dict):
            continue
        for raw_event in group.get("events", []) or []:
            if not isinstance(raw_event, dict):
                continue
            parsed = _parse_single_event(raw_event, sport)
            if parsed is not None:
                events.append(parsed)
    return events


async def fetch_bovada_events(sport: str) -> list[dict[str, Any]]:
    sport_path = _DEFAULT_SPORT_PATHS.get(sport.lower())
    if not sport_path:
        logger.warning("No Bovada sport path configured for %s", sport)
        return []

    url = f"https://www.bovada.lv/services/sports/event/v2/events/A/description/{sport_path}"
    referer = f"https://www.bovada.lv/sports/{sport_path}"
    data = await fetch_json(url, referer=referer)
    if data is None:
        return []
    return _parse_response(data, sport.lower())


async def fetch_bovada(sport: str) -> list[dict[str, Any]]:
    """Public API: fetch normalized Bovada events for a single sport."""
    return await fetch_bovada_events(sport)


async def fetch_all_default_sports() -> dict[str, list[dict[str, Any]]]:
    """Fetch all events for default supported sports."""
    results: dict[str, list[dict[str, Any]]] = {}
    for sport in _DEFAULT_SPORT_PATHS:
        results[sport] = await fetch_bovada(sport)
    return results


_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_MULTISPACE = re.compile(r"\s+")


def normalize_team_name(name: str) -> str:
    """Normalize team names for cross-source event matching."""
    text = name.strip().lower()
    text = _NON_ALNUM.sub(" ", text)
    text = _MULTISPACE.sub(" ", text).strip()
    return text


def _event_join_key(event: dict[str, Any]) -> str:
    sport = str(event.get("sport", "")).lower()
    home = normalize_team_name(str(event.get("home_team", "")))
    away = normalize_team_name(str(event.get("away_team", "")))
    teams = sorted([home, away])

    start_time = str(event.get("start_time", ""))
    try:
        date_utc = datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(
            timezone.utc
        ).date().isoformat()
    except ValueError:
        date_utc = start_time[:10] if len(start_time) >= 10 else ""
    return f"{sport}:{date_utc}:{teams[0]}:{teams[1]}"


def merge_with_rundown(
    bovada_events: list[dict[str, Any]], rundown_events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    Merge Bovada and TheRundown events by sport + UTC date + normalized team pair.
    """
    merged_by_key: dict[str, dict[str, Any]] = {}

    for event in bovada_events:
        key = _event_join_key(event)
        base = merged_by_key.setdefault(
            key,
            {
                "sport": event.get("sport"),
                "home_team": event.get("home_team"),
                "away_team": event.get("away_team"),
                "start_time": event.get("start_time"),
                "books": {"therundown": [], "bovada": []},
            },
        )
        base["books"]["bovada"].extend(event.get("markets", []))

    for event in rundown_events:
        key = _event_join_key(event)
        base = merged_by_key.setdefault(
            key,
            {
                "sport": event.get("sport"),
                "home_team": event.get("home_team"),
                "away_team": event.get("away_team"),
                "start_time": event.get("start_time"),
                "books": {"therundown": [], "bovada": []},
            },
        )
        # If caller passes full event, prefer "markets"; otherwise keep raw.
        rundown_payload = event.get("markets")
        if isinstance(rundown_payload, list):
            base["books"]["therundown"].extend(rundown_payload)
        else:
            base["books"]["therundown"].append(event)

    return list(merged_by_key.values())


def _validate_odds_math() -> None:
    # Required spot checks from the implementation plan.
    assert _decimal_to_american(2.65) == 165
    assert _decimal_to_american(1.526316) == -190
    assert _decimal_to_american(1.909091) == -110
    assert _parse_american_odds("EVEN") == 100


async def main() -> None:
    _validate_odds_math()
    requested_sports = [sport.lower() for sport in sys.argv[1:]]
    if requested_sports:
        payload = {sport: await fetch_bovada(sport) for sport in requested_sports}
    else:
        payload = await fetch_all_default_sports()
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
