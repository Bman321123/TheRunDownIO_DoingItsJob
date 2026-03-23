"""
og_scraper.py
=============
Fast OG.com (og.com) prediction-market scraper — category-page-only extraction.

OG is a CFTC-regulated prediction market (Crypto.com / CDNA) offering
moneyline, spread, and total contracts across sports, politics, crypto, and
more.  It has NO public API; pages are Next.js SSR so all odds data is baked
into the raw HTML as <a href> contract links with probabilities in the anchor
text.

Pipeline (single phase — no individual page fetches)
-----------------------------------------------------
For each sport, fetch ONE category page (e.g. /pro-basketball).  Parse all
contract <a> links from the rendered HTML.  Group by market slug, extract
team names, odds, spreads, and totals.  Return normalised event dicts.

This approach uses ~5 HTTP requests total instead of 50+, completing in 2-3
seconds instead of 20-40 seconds.

Output schema (identical to bovada_scraper.py)
----------------------------------------------
{
    "source":     "og",
    "sport":      str,       # lowercase internal key, e.g. "nba", "tennis"
    "home_team":  str,
    "away_team":  str,
    "start_time": str,       # ISO-8601 UTC
    "event_url":  str,
    "markets": [
        {
            "market_type":   "moneyline" | "spread" | "total",
            "selection":     "home" | "away" | "over" | "under",
            "team":          str,
            "american_odds": int,
            "decimal_odds":  float,
            "line_value":    float | None,
            "source":        "og",
            "ask_price":     float | None,   # raw 0-1 implied probability
            "updated_at":    str,            # ISO-8601 UTC
        }
    ]
}

Odds conversion
---------------
  Percent  XX%  →  decimal = 100 / XX  (= 1 / implied_prob)
  American is always derived from decimal to keep sign conventions consistent.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import ssl
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
import certifi
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

OG_BASE        = "https://og.com"
_TIMEOUT_S     = 20.0
_MAX_RETRIES   = 3
_BACKOFF_BASE  = 2.0
_RETRY_STATUSES: set[int] = {429, 503, 502}

# OG category URL slug → internal sport key used by server.py / bovada_scraper
_SPORT_PATHS: dict[str, str] = {
    # Overlaps with existing bot sources
    "ncaab":            "college-basketball",
    "ncaawb":           "college-basketball-w",
    "nba":              "pro-basketball",
    "nfl":              "pro-football",
    "mlb":              "baseball",
    "nhl":              "hockey",
    "ncaaf":            "college-football",
    # Additional sports OG offers
    "tennis":           "tennis",
    "epl":              "epl",
    "la_liga":          "la-liga",
    "bundesliga":       "bundesliga",
    "serie_a":          "serie-a",
    "ligue_1":          "ligue-1",
    "champions_league": "champions-league",
    "mls":              "mls",
    "world_cup":        "world-cup",
    "golf":             "golf",
    "f1":               "f1",
    "crypto":           "crypto",
    "politics":         "politics",
    "economics":        "economics",
    "companies":        "companies",
    "climate":          "climate",
}

# ──────────────────────────────────────────────────────────────────────────────
# USER-AGENT POOL
# ──────────────────────────────────────────────────────────────────────────────

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

_SEC_CH_UA_VALUES = [
    '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    '"Chromium";v="123", "Google Chrome";v="123", "Not-A.Brand";v="99"',
]

# ──────────────────────────────────────────────────────────────────────────────
# COMPILED REGEXES
# ──────────────────────────────────────────────────────────────────────────────

_SLUG_DATE_RE   = re.compile(r'(\d{4}-\d{2}-\d{2})')
_NON_ALNUM      = re.compile(r'[^a-z0-9\s]')
_MULTISPACE     = re.compile(r'\s+')

# Contract link text patterns (from category page HTML):
#   "32%"           → moneyline
#   "+7.554%"       → spread
#   "O 218.550%"    → total over
#   "U 218.551%"    → total under
_RE_BARE_PCT    = re.compile(r'^(\d{1,3}(?:\.\d+)?)\s*%$')
_RE_SPREAD      = re.compile(r'^([+-]\d+(?:\.\d+)?)\s*(\d{1,3}(?:\.\d+)?)\s*%$')
_RE_TOTAL       = re.compile(r'^([OoUu])\s+(\d+(?:\.\d+)?)\s*(\d{1,3}(?:\.\d+)?)\s*%$')

# Slugs that are never individual game markets
_SLUG_BLOCKLIST = frozenset({
    "parlay", "bracket", "trending", "live",
    "college-basketball-champion", "to-reach",
    "college-basketball-w-champion",
})


# ──────────────────────────────────────────────────────────────────────────────
# ODDS CONVERSION
# ──────────────────────────────────────────────────────────────────────────────

def _prob_to_decimal(prob_pct: float) -> Optional[float]:
    """Implied probability percent (e.g. 49.0) → decimal odds."""
    try:
        p = float(prob_pct) / 100.0
        if 0.005 < p < 0.995:
            return round(1.0 / p, 6)
        return None
    except (TypeError, ValueError):
        return None


def _decimal_to_american(decimal: float) -> int:
    """Decimal odds → American odds integer."""
    if decimal >= 2.0:
        return int(round((decimal - 1.0) * 100))
    return int(round(-100.0 / (decimal - 1.0)))


# ──────────────────────────────────────────────────────────────────────────────
# HTTP HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _build_headers(referer: Optional[str] = None) -> dict[str, str]:
    ua = random.choice(_USER_AGENTS)
    h: dict[str, str] = {
        "User-Agent":                ua,
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
        "Connection":                "keep-alive",
        "Cache-Control":             "no-cache, no-store, must-revalidate",
        "Pragma":                    "no-cache",
    }
    if "Chrome" in ua:
        h["Sec-Ch-Ua"]          = random.choice(_SEC_CH_UA_VALUES)
        h["Sec-Ch-Ua-Mobile"]   = "?0"
        h["Sec-Ch-Ua-Platform"] = '"macOS"' if "Mac" in ua else '"Windows"'
    if referer:
        h["Referer"] = referer
    return h


async def _fetch_html(
    session:         aiohttp.ClientSession,
    url:             str,
    referer:         Optional[str] = None,
    timeout_seconds: float = _TIMEOUT_S,
) -> Optional[str]:
    """GET a page and return raw HTML. Retries on 429/503 with exponential back-off."""
    for attempt in range(1, _MAX_RETRIES + 1):
        headers = _build_headers(referer=referer or OG_BASE)
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    return await resp.text(encoding="utf-8", errors="replace")
                if resp.status in _RETRY_STATUSES:
                    delay = _BACKOFF_BASE ** attempt
                    logger.warning("OG HTTP %d for %s; retry %d/%d in %.1fs",
                                   resp.status, url, attempt, _MAX_RETRIES, delay)
                    await asyncio.sleep(delay)
                    continue
                if resp.status == 404:
                    logger.debug("OG 404: %s", url)
                    return None
                logger.warning("OG HTTP %d for %s", resp.status, url)
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("OG request error %s (attempt %d/%d): %s",
                           url, attempt, _MAX_RETRIES, exc)
        await asyncio.sleep(_BACKOFF_BASE ** attempt)
    logger.error("OG exhausted retries for %s", url)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# TEAM-NAME + DATE HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def normalize_team_name(name: str) -> str:
    text = name.strip().lower()
    text = _NON_ALNUM.sub(" ", text)
    return _MULTISPACE.sub(" ", text).strip()


def _split_vs(title: str) -> tuple[str, str]:
    """Split 'Team A vs Team B' into (home, away).
    OG convention is 'Team1 vs Team2'. Returns ("","") when parsing fails.
    """
    for sep in (" vs ", " vs. ", " v ", " @ ", " at "):
        idx = title.lower().find(sep.lower())
        if idx >= 0:
            return title[:idx].strip(), title[idx + len(sep):].strip()
    return "", ""


def _teams_from_slug(slug: str) -> tuple[str, str]:
    """Best-effort team extraction from slug when HTML title parsing fails."""
    clean = re.sub(r"-\d{4}-\d{2}-\d{2}[^a-z]*.*$", "", slug, flags=re.IGNORECASE)
    parts = clean.split("-vs-", 1)
    if len(parts) == 2:
        return (
            parts[0].replace("-", " ").title(),
            parts[1].replace("-", " ").title(),
        )
    return "", ""


def _slug_to_iso(slug: str) -> str:
    m = _SLUG_DATE_RE.search(slug)
    if m:
        try:
            return (
                datetime.strptime(m.group(1), "%Y-%m-%d")
                .replace(tzinfo=timezone.utc)
                .isoformat()
            )
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc).isoformat()


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _is_valid_market_slug(slug: str) -> bool:
    """Return True only for slugs that look like individual game markets."""
    slug_l = slug.lower()
    if "vs" not in slug_l and "-at-" not in slug_l:
        return False
    for blocked in _SLUG_BLOCKLIST:
        if blocked in slug_l:
            return False
    return True


def _make_entry(
    mtype:     str,
    selection: str,
    team:      str,
    dec:       float,
    prob:      Optional[float],
    line:      Optional[float],
    sport_key: str,
    fetch_ts:  str,
) -> dict[str, Any]:
    return {
        "market_type":   mtype,
        "selection":     selection,
        "team":          team,
        "american_odds": _decimal_to_american(dec),
        "decimal_odds":  round(dec, 6),
        "line_value":    line,
        "source":        "og",
        "ask_price":     prob,
        "updated_at":    fetch_ts,
    }


# ──────────────────────────────────────────────────────────────────────────────
# CATEGORY PAGE PARSER (single-request extraction)
# ──────────────────────────────────────────────────────────────────────────────

def _parse_category_page(
    html:      str,
    sport_key: str,
    fetch_ts:  str,
) -> list[dict[str, Any]]:
    """
    Parse a category page's HTML and extract all events with odds.

    Contract <a> links on the category page look like:
      <a href="/markets/slug?contract=UUID:buy">32%</a>       ← moneyline
      <a href="/markets/slug?contract=UUID:buy">+7.554%</a>   ← spread
      <a href="/markets/slug?contract=UUID:buy">O 218.550%</a>← total
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── Step 1: Group contract links by market slug ──────────────────────────
    slug_contracts: OrderedDict[str, list[dict]] = OrderedDict()
    for a in soup.find_all("a", href=re.compile(r"/markets/.*contract=")):
        href = a["href"]
        slug = href.split("/markets/")[1].split("?")[0].rstrip("/").rstrip("\\")
        if not slug or not _is_valid_market_slug(slug):
            continue
        text = a.get_text(strip=True)
        if not text:
            continue
        slug_contracts.setdefault(slug, []).append({
            "text": text,
            "href": href,
        })

    # ── Step 2: Build a slug → team-names index ─────────────────────────────
    # OG renders team names in <h2> tags and in title <a> links
    slug_teams: dict[str, tuple[str, str]] = {}

    # From title anchor links (most reliable — text is "Portland vs Denver")
    for a in soup.find_all("a", href=re.compile(r"^/markets/")):
        href = a["href"]
        if "contract=" in href:
            continue
        slug = href.split("/markets/")[1].split("?")[0].rstrip("/").rstrip("\\")
        if not slug:
            continue
        text = a.get_text(separator=" ", strip=True)
        # Normalise CSS-smashed "BrooklynvsSacramento" → "Brooklyn vs Sacramento"
        text = re.sub(r"(?<=[a-z])VS(?=[A-Z])", " vs ", text)
        text = re.sub(r"(?<=[a-z])vs(?=[A-Z])", " vs ", text)
        if " vs " in text.lower() or " vs. " in text.lower():
            home, away = _split_vs(text)
            if home and away:
                slug_teams[slug] = (home, away)

    # ── Step 3: Parse contract texts into market entries per slug ─────────────
    events: list[dict[str, Any]] = []

    for slug, contracts in slug_contracts.items():
        # Get team names
        if slug in slug_teams:
            home, away = slug_teams[slug]
        else:
            home, away = _teams_from_slug(slug)
        if not home or not away:
            continue

        start_time = _slug_to_iso(slug)
        event_url  = f"{OG_BASE}/markets/{slug}"
        markets: list[dict[str, Any]] = []

        ml_count = 0  # track moneyline entries to assign home/away

        for contract in contracts:
            text = contract["text"]

            # ── Total: "O 218.550%" or "U 218.551%" ──────────────────────
            m = _RE_TOTAL.match(text)
            if m:
                side_char = m.group(1).upper()
                line_val  = float(m.group(2))
                pct       = float(m.group(3))
                sel       = "over" if side_char == "O" else "under"
                dec       = _prob_to_decimal(pct)
                if dec:
                    markets.append(_make_entry(
                        "total", sel, "", dec, pct / 100.0, line_val, sport_key, fetch_ts
                    ))
                continue

            # ── Spread: "+7.554%" or "-7.549%" ───────────────────────────
            m = _RE_SPREAD.match(text)
            if m:
                line_val = float(m.group(1))
                pct      = float(m.group(2))
                dec      = _prob_to_decimal(pct)
                if dec:
                    # Positive spread → underdog → away; negative → favorite → home
                    sel  = "home" if line_val < 0 else "away"
                    team = home if sel == "home" else away
                    markets.append(_make_entry(
                        "spread", sel, team, dec, pct / 100.0, line_val, sport_key, fetch_ts
                    ))
                continue

            # ── Moneyline: bare "32%" ────────────────────────────────────
            m = _RE_BARE_PCT.match(text)
            if m:
                pct = float(m.group(1))
                dec = _prob_to_decimal(pct)
                if dec:
                    # First moneyline = home, second = away
                    sel  = "home" if ml_count == 0 else "away"
                    team = home   if sel == "home" else away
                    markets.append(_make_entry(
                        "moneyline", sel, team, dec, pct / 100.0, None, sport_key, fetch_ts
                    ))
                    ml_count += 1
                continue

        if markets:
            events.append({
                "source":     "og",
                "sport":      sport_key,
                "home_team":  home,
                "away_team":  away,
                "start_time": start_time,
                "event_url":  event_url,
                "markets":    markets,
            })

    return events


# ──────────────────────────────────────────────────────────────────────────────
# INDIVIDUAL MARKET PAGE PARSER (RSC payload extraction for accurate odds)
# ──────────────────────────────────────────────────────────────────────────────

_RSC_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)')


def _parse_rsc_event_data(
    html:      str,
    slug:      str,
    sport_key: str,
    fetch_ts:  str,
) -> Optional[dict[str, Any]]:
    """
    Parse an individual market page's RSC (React Server Components) payload
    to extract accurate live odds.

    The RSC payload contains structured JSON with:
      - contracts[]          → moneyline contracts
      - spread_contracts[]   → spread contracts
      - total_contracts[]    → total contracts

    Each contract has:
      - yes: "0.22"  → YES probability (what we convert to odds)
      - prediction_outcome_type: "Home"/"Away"/"Over"/"Under"
      - prediction_value: "-7.5" (for spreads/totals)
      - participant_name: "Boston Celtics"
      - market_type_config.name: "moneyline"/"spread"/"total"
    """
    # Extract RSC chunks — use the LAST one with initialEventDetail
    # (streaming SSR may emit multiple chunks; later ones have fresher data)
    rsc_data = None
    for m in _RSC_RE.finditer(html):
        chunk = m.group(1).replace('\\"', '"').replace('\\n', ' ')
        if 'initialEventDetail' in chunk:
            rsc_data = chunk  # keep going — last match wins

    if not rsc_data:
        return None

    # Extract event metadata
    title_m = re.search(r'"title":"([^"]+)"', rsc_data)
    date_m = re.search(r'"event_date":"([^"]+)"', rsc_data)

    title = title_m.group(1) if title_m else ""
    home, away = _split_vs(title)
    if not home or not away:
        home, away = _teams_from_slug(slug)
    if not home or not away:
        return None

    start_time = date_m.group(1) if date_m else _slug_to_iso(slug)
    event_url = f"{OG_BASE}/markets/{slug}"
    markets: list[dict[str, Any]] = []

    def _parse_contract_fields(contract_str: str, mtype: str) -> None:
        """Parse a single contract JSON fragment and append to markets."""
        yes_m = re.search(r'"yes":"([\d.]+)"', contract_str)
        outcome_m = re.search(r'"prediction_outcome_type":"(\w+)"', contract_str)
        value_m = re.search(r'"prediction_value":"([^"]*)"', contract_str)
        status_m = re.search(r'"status":"(\w+)"', contract_str)

        if not yes_m or not outcome_m:
            return
        if status_m and status_m.group(1) != "active":
            return

        yes_prob = float(yes_m.group(1))
        prob_pct = yes_prob * 100.0
        outcome_type = outcome_m.group(1)
        pred_value = float(value_m.group(1)) if value_m and value_m.group(1) else None

        dec = _prob_to_decimal(prob_pct)
        if not dec:
            return

        if mtype == "moneyline":
            # OG titles list "Away vs Home", so _split_vs returns
            # (away_team_as_home, home_team_as_away).  The RSC contract's
            # prediction_outcome_type refers to the ACTUAL game home/away.
            # Flip the mapping so team-odds pairing matches category page.
            sel = "away" if outcome_type == "Home" else "home"
            team = home if sel == "home" else away
            markets.append(_make_entry(
                "moneyline", sel, team, dec, yes_prob, None, sport_key, fetch_ts
            ))
        elif mtype == "spread":
            sel = "away" if outcome_type == "Home" else "home"
            team = home if sel == "home" else away
            if pred_value is not None:
                markets.append(_make_entry(
                    "spread", sel, team, dec, yes_prob, pred_value, sport_key, fetch_ts
                ))
        elif mtype == "total":
            sel = "over" if outcome_type == "Over" else "under"
            if pred_value is not None:
                markets.append(_make_entry(
                    "total", sel, "", dec, yes_prob, pred_value, sport_key, fetch_ts
                ))

    def _extract_contract_array(key: str) -> list[str]:
        """Extract individual contract JSON fragments from a named array in RSC data."""
        # Find the array: "key":[{...},{...},...]
        start_pat = f'"{key}":['
        idx = rsc_data.find(start_pat)
        if idx < 0:
            return []
        idx += len(start_pat)

        # Walk forward to find the matching ']', tracking brace depth
        depth = 1
        end = idx
        for i in range(idx, min(idx + 100000, len(rsc_data))):
            if rsc_data[i] == '[':
                depth += 1
            elif rsc_data[i] == ']':
                depth -= 1
                if depth == 0:
                    end = i
                    break

        array_str = rsc_data[idx:end]

        # Split into individual contract objects by tracking braces
        contracts: list[str] = []
        obj_start = None
        obj_depth = 0
        for i, ch in enumerate(array_str):
            if ch == '{':
                if obj_depth == 0:
                    obj_start = i
                obj_depth += 1
            elif ch == '}':
                obj_depth -= 1
                if obj_depth == 0 and obj_start is not None:
                    contracts.append(array_str[obj_start:i + 1])
                    obj_start = None
        return contracts

    # Parse all three contract arrays
    for contract_str in _extract_contract_array("contracts"):
        _parse_contract_fields(contract_str, "moneyline")

    for contract_str in _extract_contract_array("spread_contracts"):
        _parse_contract_fields(contract_str, "spread")

    for contract_str in _extract_contract_array("total_contracts"):
        _parse_contract_fields(contract_str, "total")

    if not markets:
        return None

    return {
        "source":     "og",
        "sport":      sport_key,
        "home_team":  home,
        "away_team":  away,
        "start_time": start_time,
        "event_url":  event_url,
        "markets":    markets,
    }


def _infer_sport_from_slug(slug: str) -> str:
    """Best-effort sport-key inference for /live-page events."""
    s = slug.lower()
    if any(x in s for x in ("basketball", "nba", "ncaa-m", "ncaab")):
        return "nba"
    if any(x in s for x in ("football", "nfl")):
        return "nfl"
    if any(x in s for x in ("hockey", "nhl")):
        return "nhl"
    if any(x in s for x in ("baseball", "mlb")):
        return "mlb"
    if "tennis" in s:
        return "tennis"
    if any(x in s for x in ("soccer", "epl", "bundesliga", "liga", "serie")):
        return "epl"
    return "unknown"


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_og(sport: Optional[str] = None) -> list[dict[str, Any]]:
    """
    Primary entry point — mirrors ``bovada_scraper.fetch_bovada()``.

    Parameters
    ----------
    sport : str or None
        Internal sport key (e.g. 'nba', 'tennis', 'champions_league').
        Pass None to fetch every configured sport.

    Returns
    -------
    list[dict]
        Normalised event dicts with source='og'.
    """
    if sport is None:
        pairs = list(_SPORT_PATHS.items())
    else:
        path = _SPORT_PATHS.get(sport.lower())
        if not path:
            logger.warning("OG: unknown sport key '%s'. Valid: %s", sport, list(_SPORT_PATHS))
            return []
        pairs = [(sport.lower(), path)]

    return await _run_pipeline(pairs)


async def fetch_og_sport(sport: str) -> list[dict[str, Any]]:
    """Convenience wrapper: fetch a single sport."""
    return await fetch_og(sport=sport)


async def fetch_og_all_sports() -> list[dict[str, Any]]:
    """Convenience wrapper: fetch every configured sport."""
    return await fetch_og(sport=None)


async def _run_pipeline(sport_pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """
    Hybrid pipeline:
      Phase 1 — Fetch category pages to discover market slugs (~2s).
      Phase 2 — Fetch individual market pages for accurate RSC odds (~8-12s).
    """
    fetch_ts  = _utc_now()
    ssl_ctx   = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx, limit_per_host=6, enable_cleanup_closed=True)

    async with aiohttp.ClientSession(connector=connector) as session:

        # ── Phase 1: Discover slugs from category pages ──────────────────
        async def _fetch_category(sk: str, sp: str) -> list[dict[str, Any]]:
            url  = f"{OG_BASE}/{sp}"
            html = await _fetch_html(session, url, referer=OG_BASE)
            if not html:
                logger.warning("OG: failed to fetch category %s", url)
                return []
            events = _parse_category_page(html, sk, fetch_ts)
            logger.info("OG Phase1 [%s]: %d slugs discovered", sk, len(events))
            return events

        cat_tasks = [_fetch_category(sk, sp) for sk, sp in sport_pairs]

        # Also sweep /live
        async def _fetch_live() -> list[dict[str, Any]]:
            html = await _fetch_html(session, f"{OG_BASE}/live", referer=OG_BASE)
            if not html:
                return []
            soup = BeautifulSoup(html, "html.parser")
            slugs_seen: set[str] = set()
            sport_for_slug: dict[str, str] = {}
            for a in soup.find_all("a", href=re.compile(r"/markets/.*contract=")):
                slug = a["href"].split("/markets/")[1].split("?")[0].rstrip("/").rstrip("\\")
                if slug and slug not in slugs_seen:
                    slugs_seen.add(slug)
                    sport_for_slug[slug] = _infer_sport_from_slug(slug)

            events = _parse_category_page(html, "live", fetch_ts)
            for evt in events:
                slug = evt.get("event_url", "").split("/markets/")[-1].split("?")[0]
                inferred = sport_for_slug.get(slug, _infer_sport_from_slug(slug))
                if inferred != "unknown":
                    evt["sport"] = inferred
            logger.info("OG Phase1 [live]: %d slugs discovered", len(events))
            return events

        cat_tasks.append(_fetch_live())
        cat_results = await asyncio.gather(*cat_tasks, return_exceptions=True)

        # Build slug → metadata index from category page results
        discovered: OrderedDict[str, dict[str, str]] = OrderedDict()
        for res in cat_results:
            if isinstance(res, Exception):
                logger.warning("OG Phase1 category error: %s", res)
                continue
            for evt in res:
                slug = evt.get("event_url", "").split("/markets/")[-1].split("?")[0]
                if slug and slug not in discovered:
                    discovered[slug] = {
                        "sport_key": evt["sport"],
                        "home": evt["home_team"],
                        "away": evt["away_team"],
                    }

        logger.info("OG Phase1 complete: %d unique slugs discovered", len(discovered))

        if not discovered:
            return []

        # ── Phase 2: Fetch individual market pages for accurate odds ─────
        sem = asyncio.Semaphore(5)
        phase2_fetch_ts = _utc_now()  # fresh timestamp for accurate data

        async def _fetch_market(slug: str, info: dict[str, str]) -> Optional[dict[str, Any]]:
            async with sem:
                await asyncio.sleep(random.uniform(0.05, 0.2))  # jitter
                url = f"{OG_BASE}/markets/{slug}"
                html = await _fetch_html(session, url, referer=OG_BASE, timeout_seconds=12.0)
                if not html:
                    logger.debug("OG Phase2: failed to fetch %s", slug)
                    return None
                return _parse_rsc_event_data(html, slug, info["sport_key"], phase2_fetch_ts)

        market_tasks = [
            _fetch_market(slug, info)
            for slug, info in discovered.items()
        ]
        market_results = await asyncio.gather(*market_tasks, return_exceptions=True)

    # Collect successful results
    all_events: list[dict[str, Any]] = []
    phase2_ok = 0
    phase2_fail = 0

    for res in market_results:
        if isinstance(res, Exception):
            logger.debug("OG Phase2 error: %s", res)
            phase2_fail += 1
        elif res is not None:
            all_events.append(res)
            phase2_ok += 1
        else:
            phase2_fail += 1

    # If Phase 2 failed entirely (e.g. rate limited), fall back to category page data
    if phase2_ok == 0 and discovered:
        logger.warning("OG Phase2 returned 0 events — falling back to category page data")
        for res in cat_results:
            if isinstance(res, Exception):
                continue
            for evt in res:
                slug = evt.get("event_url", "").split("/markets/")[-1].split("?")[0]
                all_events.append(evt)

    logger.info(
        "OG scrape done: %d events (Phase2: %d ok, %d failed, %d slugs)",
        len(all_events), phase2_ok, phase2_fail, len(discovered),
    )
    return all_events


# ──────────────────────────────────────────────────────────────────────────────
# STANDALONE DEBUG RUNNER
# ──────────────────────────────────────────────────────────────────────────────

def _am_str(am: Optional[int]) -> str:
    if am is None:
        return "N/A"
    return f"+{am}" if am > 0 else str(am)


async def _debug_main(sport_keys: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
    print(f"\n{'=' * 66}")
    print("  OG.com SCRAPER  —  DEBUG RUN (category-page-only)")
    print(f"  Sports: {sport_keys if sport_keys else 'ALL'}")
    print(f"{'=' * 66}\n")

    if sport_keys:
        events: list[dict] = []
        for sk in sport_keys:
            events.extend(await fetch_og_sport(sk))
    else:
        events = await fetch_og_all_sports()

    if not events:
        print("  WARNING: 0 events returned.")
        return

    print(f"  OK: {len(events)} event(s) returned\n")

    for i, evt in enumerate(events, 1):
        sport = evt.get("sport", "?").upper()
        home  = evt.get("home_team", "?")
        away  = evt.get("away_team", "?")
        start = (evt.get("start_time") or "")[:16]
        mkts  = evt.get("markets") or []

        print(f"  [{i:03d}] {sport}  {home} vs {away}  ({start})")

        for m in mkts:
            mt   = m["market_type"]
            sel  = m["selection"]
            am   = _am_str(m["american_odds"])
            dec  = m["decimal_odds"]
            lv   = m.get("line_value")
            line = f" line={lv:+.1f}" if lv is not None else ""
            prob = f" prob={m['ask_price']:.3f}" if m.get("ask_price") else ""
            print(f"    {mt:<10} {sel:<6} {am:<8} dec={dec:.3f}{line}{prob}")
        print()

    ml_n    = sum(len([m for m in e.get("markets", []) if m["market_type"] == "moneyline"]) for e in events)
    sprd_n  = sum(len([m for m in e.get("markets", []) if m["market_type"] == "spread"])    for e in events)
    total_n = sum(len([m for m in e.get("markets", []) if m["market_type"] == "total"])     for e in events)

    print(f"{'─' * 66}")
    print(f"  SUMMARY  events={len(events)}  ML={ml_n}  spread={sprd_n}  total={total_n}")
    print(f"{'─' * 66}\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(_debug_main(sport_keys=args))
