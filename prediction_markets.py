"""
prediction_markets.py

Standalone direct API clients for Kalshi and Polymarket prediction markets.
Zero dependency on TheRundown or Bovada — this is an independent data pipeline.

Returns events in the exact same schema as bovada_scraper.py so server.py
can merge them transparently:

    {
        "source":     "kalshi" | "polymarket",
        "sport":      str,          # lowercase: "nba", "nfl", "nhl", etc.
        "home_team":  str,
        "away_team":  str,
        "start_time": str,          # ISO-8601 UTC
        "event_url":  str,
        "markets": [
            {
                "market_type":   "moneyline" | "spread" | "total",
                "selection":     "home" | "away" | "over" | "under",
                "team":          str,
                "american_odds": int,
                "decimal_odds":  float,
                "line_value":    float | None,
                "source":        "kalshi" | "polymarket",
                "ask_price":     float | None,   # raw 0–1 PM price
                "updated_at":    str,            # ISO-8601 UTC (= fetch time for live)
            }
        ]
    }

Prediction market price → decimal odds:
    decimal = 1 / ask_price        e.g. 0.65 → 1.538
    american = decimal_to_american(decimal)

Arb condition (same formula as sportsbooks):
    (1/dec_a) + (1/dec_b) < 1.0   ← profit > 0
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import aiohttp
import certifi
import ssl

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
KALSHI_BASE_URL       = "https://api.elections.kalshi.com"
POLYMARKET_GAMMA_URL  = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_URL   = "https://clob.polymarket.com"

KALSHI_API_KEY        = os.getenv("KALSHI_API_KEY", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
KALSHI_PRIVATE_KEY_PEM  = os.getenv("KALSHI_PRIVATE_KEY_PEM", "")

# Max parallel workers for batch requests
KALSHI_WORKERS  = 5
POLY_WORKERS    = 3
CLOB_WORKERS    = 10

# Polymarket CLOB: market depth minimum for a valid ask price
CLOB_MIN_SIZE   = 1.0   # $1 minimum depth — below this treat as illiquid

# Platform fee rates — applied to PROFIT, not settlement value
#
# Formula (for a 0-1 ask price):
#   net_payout   = 1 - fee_rate * (1 - ask)
#   decimal_odds = net_payout / ask
POLYMARKET_FEE_RATE: float = 0.02   # 2% of profit (taker)
KALSHI_FEE_RATE: float     = 0.07   # ~7% of profit for sports taker orders

# ─────────────────────────────────────────────
# SPORT → SERIES / TAG MAPPINGS
# ─────────────────────────────────────────────

# Kalshi: sport key (lowercase) → list of series tickers to scan
# Moneyline game series
_SPORT_TO_KALSHI_SERIES: dict[str, list[str]] = {
    "nba":    ["KXNBAGAME"],
    "nfl":    ["KXNFLGAME"],
    "nhl":    ["KXNHLGAME"],
    "mlb":    ["KXMLBGAME"],
    "ncaab":  ["KXNCAAMBGAME"],
    "ncaawb": ["KXNCAAWBGAME"],
    "mma":    ["KXUFCFIGHT"],
    "ncaaf":  ["KXNCAAFGAME"],
}

# Kalshi: additional series for spread/total markets (sport-specific)
# These exist for high-volume sports; others fall back to moneyline only
_SPORT_TO_KALSHI_PROP_SERIES: dict[str, list[str]] = {
    "nba": ["KXNBAGMPTS", "KXNBAGMSPRD"],
    "nfl": ["KXNFLGMPTS", "KXNFLGMSPRD"],
}

# Polymarket: sport key → tag slugs (legacy, kept for reference)
_SPORT_TO_POLY_TAGS: dict[str, list[str]] = {
    "nba":    ["nba"],
    "nfl":    ["nfl"],
    "nhl":    ["nhl"],
    "mlb":    ["mlb"],
    "ncaab":  ["ncaab", "ncaamb"],
    "ncaawb": ["ncaawb"],
    "mma":    ["ufc", "mma"],
    "ncaaf":  ["ncaaf", "college-football"],
}

# Polymarket: sport key → Gamma series IDs (current API)
_SPORT_TO_POLY_SERIES: dict[str, list[str]] = {
    "nba":    ["10345"],
    "nfl":    ["10187"],
    "nhl":    ["10346"],
    "mlb":    ["3"],
    "ncaab":  ["39"],
    "ncaawb": ["10471"],
    "mma":    ["10500"],
    "ncaaf":  ["10210"],
}

# ─────────────────────────────────────────────
# PRICE CONVERSION
# ─────────────────────────────────────────────

def pm_price_to_decimal(ask: float, fee_rate: float = 0.0) -> float:
    """
    Convert a 0–1 prediction market ask price to decimal odds after fees.

    fee_rate: fraction of PROFIT taken as platform fee.
              e.g. POLYMARKET_FEE_RATE = 0.02, KALSHI_FEE_RATE = 0.07

    Formula:
        net_payout   = 1 - fee_rate × (1 - ask)
        decimal_odds = net_payout / ask
    """
    if ask <= 0 or ask >= 1:
        return 0.0

    net_payout = 1.0 - fee_rate * (1.0 - ask)
    if net_payout <= 0:
        return 0.0

    return round(net_payout / ask, 6)


def decimal_to_american(dec: float) -> int:
    """Standard decimal → American odds conversion."""
    if dec <= 1.0:
        return 0
    if dec >= 2.0:
        return int(round((dec - 1) * 100))
    return int(round(-100 / (dec - 1)))


def pm_price_to_american(ask: float, fee_rate: float = 0.0) -> int:
    """Shortcut: 0–1 ask price → American odds integer, after fees."""
    dec = pm_price_to_decimal(ask, fee_rate=fee_rate)
    if dec <= 1.0:
        return 0
    return decimal_to_american(dec)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
# TEAM NAME NORMALIZATION
# ─────────────────────────────────────────────

_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_MULTISPACE = re.compile(r"\s+")

# Comprehensive alias map for prediction market team name variants.
# Keys are already lowercased + stripped.
_TEAM_ALIAS: dict[str, str] = {
    # ── NBA ──────────────────────────────────────────────────────────────
    "lal": "los angeles lakers",
    "la lakers": "los angeles lakers",
    "lakers": "los angeles lakers",
    "lac": "los angeles clippers",
    "la clippers": "los angeles clippers",
    "clippers": "los angeles clippers",
    "gsw": "golden state warriors",
    "golden state": "golden state warriors",
    "warriors": "golden state warriors",
    "bos": "boston celtics",
    "celtics": "boston celtics",
    "mia": "miami heat",
    "heat": "miami heat",
    "nyk": "new york knicks",
    "knicks": "new york knicks",
    "chi": "chicago bulls",
    "bulls": "chicago bulls",
    "bkn": "brooklyn nets",
    "nets": "brooklyn nets",
    "phi": "philadelphia 76ers",
    "sixers": "philadelphia 76ers",
    "76ers": "philadelphia 76ers",
    "mil": "milwaukee bucks",
    "bucks": "milwaukee bucks",
    "tor": "toronto raptors",
    "raptors": "toronto raptors",
    "atl": "atlanta hawks",
    "hawks": "atlanta hawks",
    "cle": "cleveland cavaliers",
    "cavaliers": "cleveland cavaliers",
    "cavs": "cleveland cavaliers",
    "ind": "indiana pacers",
    "pacers": "indiana pacers",
    "det": "detroit pistons",
    "pistons": "detroit pistons",
    "orl": "orlando magic",
    "magic": "orlando magic",
    "was": "washington wizards",
    "wsh": "washington wizards",
    "wizards": "washington wizards",
    "cha": "charlotte hornets",
    "hornets": "charlotte hornets",
    "den": "denver nuggets",
    "nuggets": "denver nuggets",
    "okc": "oklahoma city thunder",
    "thunder": "oklahoma city thunder",
    "por": "portland trail blazers",
    "blazers": "portland trail blazers",
    "trail blazers": "portland trail blazers",
    "uta": "utah jazz",
    "jazz": "utah jazz",
    "min": "minnesota timberwolves",
    "timberwolves": "minnesota timberwolves",
    "wolves": "minnesota timberwolves",
    "nop": "new orleans pelicans",
    "pelicans": "new orleans pelicans",
    "hou": "houston rockets",
    "rockets": "houston rockets",
    "dal": "dallas mavericks",
    "mavericks": "dallas mavericks",
    "mavs": "dallas mavericks",
    "mem": "memphis grizzlies",
    "grizzlies": "memphis grizzlies",
    "sas": "san antonio spurs",
    "spurs": "san antonio spurs",
    "sac": "sacramento kings",
    "kings": "sacramento kings",
    "phx": "phoenix suns",
    "suns": "phoenix suns",

    # ── NFL ──────────────────────────────────────────────────────────────
    "ne": "new england patriots",
    "patriots": "new england patriots",
    "kc": "kansas city chiefs",
    "chiefs": "kansas city chiefs",
    "buf": "buffalo bills",
    "bills": "buffalo bills",
    "mia dolphins": "miami dolphins",
    "dolphins": "miami dolphins",
    "nyj": "new york jets",
    "jets": "new york jets",
    "nyg": "new york giants",
    "giants": "new york giants",
    "phi eagles": "philadelphia eagles",
    "eagles": "philadelphia eagles",
    "dal cowboys": "dallas cowboys",
    "cowboys": "dallas cowboys",
    "was commanders": "washington commanders",
    "commanders": "washington commanders",
    "gb": "green bay packers",
    "packers": "green bay packers",
    "chi bears": "chicago bears",
    "bears": "chicago bears",
    "det lions": "detroit lions",
    "lions": "detroit lions",
    "min vikings": "minnesota vikings",
    "vikings": "minnesota vikings",
    "tb": "tampa bay buccaneers",
    "buccaneers": "tampa bay buccaneers",
    "bucs": "tampa bay buccaneers",
    "no": "new orleans saints",
    "saints": "new orleans saints",
    "atl falcons": "atlanta falcons",
    "falcons": "atlanta falcons",
    "car": "carolina panthers",
    "panthers": "carolina panthers",
    "sf": "san francisco 49ers",
    "49ers": "san francisco 49ers",
    "niners": "san francisco 49ers",
    "sea": "seattle seahawks",
    "seahawks": "seattle seahawks",
    "lar": "los angeles rams",
    "rams": "los angeles rams",
    "ari": "arizona cardinals",
    "cardinals": "arizona cardinals",
    "pit": "pittsburgh steelers",
    "steelers": "pittsburgh steelers",
    "bal": "baltimore ravens",
    "ravens": "baltimore ravens",
    "cle browns": "cleveland browns",
    "browns": "cleveland browns",
    "cin": "cincinnati bengals",
    "bengals": "cincinnati bengals",
    "hou texans": "houston texans",
    "texans": "houston texans",
    "ind colts": "indianapolis colts",
    "colts": "indianapolis colts",
    "jax": "jacksonville jaguars",
    "jaguars": "jacksonville jaguars",
    "ten": "tennessee titans",
    "titans": "tennessee titans",
    "den broncos": "denver broncos",
    "broncos": "denver broncos",
    "lv": "las vegas raiders",
    "raiders": "las vegas raiders",
    "lac chargers": "los angeles chargers",
    "chargers": "los angeles chargers",

    # ── NHL ──────────────────────────────────────────────────────────────
    "bos bruins": "boston bruins",
    "bruins": "boston bruins",
    "tor maple leafs": "toronto maple leafs",
    "maple leafs": "toronto maple leafs",
    "leafs": "toronto maple leafs",
    "mtl": "montreal canadiens",
    "canadiens": "montreal canadiens",
    "habs": "montreal canadiens",
    "buf sabres": "buffalo sabres",
    "sabres": "buffalo sabres",
    "ott": "ottawa senators",
    "senators": "ottawa senators",
    "det red wings": "detroit red wings",
    "red wings": "detroit red wings",
    "chi blackhawks": "chicago blackhawks",
    "blackhawks": "chicago blackhawks",
    "col": "colorado avalanche",
    "avalanche": "colorado avalanche",
    "avs": "colorado avalanche",
    "min wild": "minnesota wild",
    "wild": "minnesota wild",
    "dal stars": "dallas stars",
    "stars": "dallas stars",
    "stl": "st. louis blues",
    "blues": "st. louis blues",
    "nsh": "nashville predators",
    "predators": "nashville predators",
    "wpg": "winnipeg jets",
    "jets": "winnipeg jets",
    "vgk": "vegas golden knights",
    "golden knights": "vegas golden knights",
    "sea kraken": "seattle kraken",
    "kraken": "seattle kraken",
    "edm": "edmonton oilers",
    "oilers": "edmonton oilers",
    "cgy": "calgary flames",
    "flames": "calgary flames",
    "van": "vancouver canucks",
    "canucks": "vancouver canucks",
    "pit penguins": "pittsburgh penguins",
    "penguins": "pittsburgh penguins",
    "phi flyers": "philadelphia flyers",
    "flyers": "philadelphia flyers",
    "nyr": "new york rangers",
    "rangers": "new york rangers",
    "nyj islanders": "new york islanders",
    "islanders": "new york islanders",
    "njd": "new jersey devils",
    "devils": "new jersey devils",
    "car hurricanes": "carolina hurricanes",
    "hurricanes": "carolina hurricanes",
    "wsh capitals": "washington capitals",
    "capitals": "washington capitals",
    "caps": "washington capitals",
    "fla": "florida panthers",
    "florida panthers": "florida panthers",
    "tbl": "tampa bay lightning",
    "lightning": "tampa bay lightning",
    "ott senators": "ottawa senators",
    "col avalanche": "colorado avalanche",
    "ari coyotes": "arizona coyotes",
    "coyotes": "arizona coyotes",
    "ana": "anaheim ducks",
    "ducks": "anaheim ducks",
    "lak": "los angeles kings",
    "la kings": "los angeles kings",
    "sjs": "san jose sharks",
    "sharks": "san jose sharks",
    "cbj": "columbus blue jackets",
    "blue jackets": "columbus blue jackets",

    # ── MLB ──────────────────────────────────────────────────────────────
    "nyy": "new york yankees",
    "yankees": "new york yankees",
    "nym": "new york mets",
    "mets": "new york mets",
    "bos red sox": "boston red sox",
    "red sox": "boston red sox",
    "tb rays": "tampa bay rays",
    "rays": "tampa bay rays",
    "bal orioles": "baltimore orioles",
    "orioles": "baltimore orioles",
    "tor blue jays": "toronto blue jays",
    "blue jays": "toronto blue jays",
    "chi cubs": "chicago cubs",
    "cubs": "chicago cubs",
    "chi white sox": "chicago white sox",
    "white sox": "chicago white sox",
    "cle guardians": "cleveland guardians",
    "guardians": "cleveland guardians",
    "det tigers": "detroit tigers",
    "tigers": "detroit tigers",
    "min twins": "minnesota twins",
    "twins": "minnesota twins",
    "kc royals": "kansas city royals",
    "royals": "kansas city royals",
    "hou astros": "houston astros",
    "astros": "houston astros",
    "tex": "texas rangers",
    "tex rangers": "texas rangers",
    "rangers": "texas rangers",
    "laa": "los angeles angels",
    "angels": "los angeles angels",
    "oak": "oakland athletics",
    "athletics": "oakland athletics",
    "sea mariners": "seattle mariners",
    "mariners": "seattle mariners",
    "atl braves": "atlanta braves",
    "braves": "atlanta braves",
    "mia marlins": "miami marlins",
    "marlins": "miami marlins",
    "phi phillies": "philadelphia phillies",
    "phillies": "philadelphia phillies",
    "nyl": "new york mets",
    "was nationals": "washington nationals",
    "nationals": "washington nationals",
    "nym mets": "new york mets",
    "col rockies": "colorado rockies",
    "rockies": "colorado rockies",
    "sd": "san diego padres",
    "padres": "san diego padres",
    "sf giants": "san francisco giants",
    "giants": "san francisco giants",
    "lad": "los angeles dodgers",
    "dodgers": "los angeles dodgers",
    "ari diamondbacks": "arizona diamondbacks",
    "diamondbacks": "arizona diamondbacks",
    "dbacks": "arizona diamondbacks",
    "cin reds": "cincinnati reds",
    "reds": "cincinnati reds",
    "mil brewers": "milwaukee brewers",
    "brewers": "milwaukee brewers",
    "pit pirates": "pittsburgh pirates",
    "pirates": "pittsburgh pirates",
    "stl cardinals": "st. louis cardinals",
    "cardinals": "st. louis cardinals",

    # ── MMA / UFC ────────────────────────────────────────────────────────
    # UFC fighters — common shortened versions
    "usman": "kamaru usman",
    "adesanya": "israel adesanya",
    "volkanovski": "alexander volkanovski",
    "makhachev": "islam makhachev",
    "poirier": "dustin poirier",
    "oliveira": "charles oliveira",
    "gaethje": "justin gaethje",
    "holloway": "max holloway",
    "o'malley": "sean o'malley",
    "omalley": "sean o'malley",
    "pereira": "alex pereira",
    "jones": "jon jones",

    # ── NCAAB abbreviations (common) ─────────────────────────────────────
    "uva": "virginia cavaliers",
    "virginia": "virginia cavaliers",
    "osu": "ohio state buckeyes",
    "ohio st": "ohio state buckeyes",
    "ohio st.": "ohio state buckeyes",
    "ohio state": "ohio state buckeyes",
    "unc": "north carolina tar heels",
    "north carolina": "north carolina tar heels",
    "duke": "duke blue devils",
    "kansas": "kansas jayhawks",
    "ku": "kansas jayhawks",
    "kentucky": "kentucky wildcats",
    "uk": "kentucky wildcats",
    "gonzaga": "gonzaga bulldogs",
    "houston": "houston cougars",
    "uconn": "connecticut huskies",
    "connecticut": "connecticut huskies",
    "purdue": "purdue boilermakers",
    "illinois": "illinois fighting illini",
    "michigan st": "michigan state spartans",
    "michigan state": "michigan state spartans",
    "msu": "michigan state spartans",
    "michigan": "michigan wolverines",
    "texas": "texas longhorns",
    "ut": "texas longhorns",
    "baylor": "baylor bears",
    "indiana": "indiana hoosiers",
    "iowa": "iowa hawkeyes",
    "iowa st": "iowa state cyclones",
    "iowa state": "iowa state cyclones",
    "tennessee": "tennessee volunteers",
    "vols": "tennessee volunteers",
    "arkansas": "arkansas razorbacks",
    "auburn": "auburn tigers",
    "lsu": "lsu tigers",
    "alabama": "alabama crimson tide",
    "bama": "alabama crimson tide",
    "florida": "florida gators",
    "uf": "florida gators",
    "florida st": "florida state seminoles",
    "florida state": "florida state seminoles",
    "fsu": "florida state seminoles",
    "uf seminoles": "florida state seminoles",
    "creighton": "creighton bluejays",
    "marquette": "marquette golden eagles",
    "villanova": "villanova wildcats",
    "nova": "villanova wildcats",
    "xavier": "xavier musketeers",
    "butler": "butler bulldogs",
    "seton hall": "seton hall pirates",
    "st john s": "st. john's red storm",
    "st johns": "st. john's red storm",
    "providence": "providence friars",
}


def normalize_name(name: str) -> str:
    """Normalize + alias-resolve a team name for cross-source matching."""
    text = (name or "").strip().lower()
    text = text.replace(".", " ")
    text = _NON_ALNUM.sub(" ", text)
    text = _MULTISPACE.sub(" ", text).strip()
    return _TEAM_ALIAS.get(text, text)


# ─────────────────────────────────────────────
# KALSHI AUTH
# ─────────────────────────────────────────────

def _load_kalshi_private_key():
    """Load RSA private key. Logs explicit errors for every failure mode."""
    pem_str  = KALSHI_PRIVATE_KEY_PEM
    pem_path = KALSHI_PRIVATE_KEY_PATH

    if not KALSHI_API_KEY:
        logger.error("KALSHI_API_KEY is not set — set it in .env")
        return None

    pem_bytes: bytes | None = None

    if pem_str:
        pem_bytes = pem_str.encode("utf-8")
        logger.info("Kalshi: loading private key from KALSHI_PRIVATE_KEY_PEM env var")
    elif pem_path:
        if not os.path.exists(pem_path):
            logger.error("Kalshi private key file not found: %s", pem_path)
            return None
        try:
            with open(pem_path, "rb") as f:
                pem_bytes = f.read()
            logger.info("Kalshi: loaded private key from %s", pem_path)
        except Exception as e:
            logger.error("Kalshi: failed to read private key file %s: %s", pem_path, e)
            return None
    else:
        logger.error(
            "No Kalshi private key configured. "
            "Set KALSHI_PRIVATE_KEY_PATH (path to .pem file) or "
            "KALSHI_PRIVATE_KEY_PEM (inline PEM string) in .env"
        )
        return None

    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        key = load_pem_private_key(pem_bytes, password=None)
        logger.info("Kalshi: private key loaded successfully")
        return key
    except Exception as e:
        logger.error("Kalshi: failed to parse private key PEM: %s", e)
        return None


def _kalshi_auth_headers(api_key: str, private_key, method: str, path: str) -> dict[str, str]:
    """Generate RSA-signed Kalshi auth headers per Kalshi v2 API spec."""
    ts = str(int(time.time() * 1000))

    path_no_query = path.split("?")[0]

    # Message = timestamp + METHOD_UPPERCASE + path_without_query
    msg = ts + method.upper() + path_no_query

    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
        sig_bytes = private_key.sign(
            msg.encode("utf-8"),
            asym_padding.PSS(
                mgf=asym_padding.MGF1(hashes.SHA256()),
                salt_length=asym_padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig = base64.b64encode(sig_bytes).decode("utf-8")
    except Exception as e:
        logger.error("Kalshi RSA signing failed: %s", e)
        sig = ""

    return {
        "KALSHI-ACCESS-KEY":       api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type":            "application/json",
        "Accept":                  "application/json",
    }


# ─────────────────────────────────────────────
# KALSHI — TITLE / TEAM PARSING
# ─────────────────────────────────────────────

_TITLE_SPORT_PREFIX = re.compile(r"^[A-Z0-9]+(?:/[A-Z0-9]+)?:\s*")
_LINE_VALUE_RE = re.compile(r"[-+]?\d+\.?\d*")


def _strip_sport_prefix(title: str) -> str:
    return _TITLE_SPORT_PREFIX.sub("", title).strip()


def _parse_teams_from_title(title: str) -> tuple[str, str] | None:
    """
    Extract (home, away) from Kalshi event title.
    Handles: "NBA: Lakers vs Celtics", "Team A at Team B", etc.
    Returns None if unparseable.
    """
    cleaned = _strip_sport_prefix(title)
    for sep, home_is_right in [(" vs. ", False), (" vs ", False), (" at ", True), (" @ ", True), (" v ", False)]:
        if sep in cleaned:
            parts = cleaned.split(sep, 1)
            if len(parts) == 2:
                left = parts[0].strip()
                right = parts[1].strip()
                # Remove date suffixes like "(Mar 15)" or "on March 15"
                right = re.sub(r"\s*[\(\[].*", "", right).strip()
                left = re.sub(r"\s*[\(\[].*", "", left).strip()
                if home_is_right:
                    return right, left  # (home, away) — "Away at Home"
                else:
                    return left, right  # (home, away) — "Home vs Away"
    return None


def _classify_kalshi_market_type(subtitle: str, title: str) -> tuple[str, str | None, float | None]:
    """
    Classify a Kalshi sub-market by its subtitle/title.
    Returns (market_type, selection, line_value).
    market_type: "moneyline" | "spread" | "total" | "unknown"
    selection: "home" | "away" | "over" | "under" | None
    line_value: float if spread/total, else None
    """
    lower = subtitle.lower()
    title_lower = title.lower()

    # Total points / over-under
    if any(kw in lower for kw in ["over", "under", "total", "points", "goals", "runs"]):
        nums = _LINE_VALUE_RE.findall(subtitle)
        line = float(nums[0]) if nums else None
        selection = "over" if "over" in lower else "under" if "under" in lower else None
        return "total", selection, line

    # Spread / handicap
    if any(kw in lower for kw in ["spread", "cover", "ats", "handicap", "-", "+"]):
        nums = re.findall(r"[-+]\d+\.?\d*", subtitle)
        if nums:
            line = float(nums[0])
            # Negative line = favourite (home -X)
            selection = "home" if line < 0 else "away"
            return "spread", selection, abs(line)

    # Default → moneyline (team name = selection)
    return "moneyline", None, None


# ─────────────────────────────────────────────
# KALSHI — FETCH & PARSE
# ─────────────────────────────────────────────

def _walk_kalshi_yes_ask_from_orderbook(
    orderbook_fp: dict[str, Any],
    min_notional: float,
) -> float | None:
    """
    Walk executable YES ask from Kalshi orderbook levels.

    Kalshi orderbook endpoint returns bids only. YES asks are derived from NO
    bids with the identity: yes_ask = 1 - no_bid.
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


async def _fetch_kalshi_executable_yes_ask(
    session: aiohttp.ClientSession,
    api_key: str,
    private_key,
    market_ticker: str,
    orderbook_cache: dict[str, float | None],
    min_notional: float = CLOB_MIN_SIZE,
) -> float | None:
    """Fetch executable YES ask by walking Kalshi orderbook depth."""
    if not market_ticker:
        return None
    if market_ticker in orderbook_cache:
        return orderbook_cache[market_ticker]

    path = f"/trade-api/v2/markets/{market_ticker}/orderbook"
    headers = _kalshi_auth_headers(api_key, private_key, "GET", path)
    ask_value: float | None = None
    try:
        url = KALSHI_BASE_URL + path
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status == 200:
                data = await resp.json()
                orderbook_fp = data.get("orderbook_fp") or {}
                ask_value = _walk_kalshi_yes_ask_from_orderbook(orderbook_fp, min_notional=min_notional)
    except Exception as e:
        logger.debug("Kalshi orderbook fetch failed ticker=%s: %s", market_ticker, e)

    orderbook_cache[market_ticker] = ask_value
    return ask_value

async def _fetch_kalshi_series(
    session: aiohttp.ClientSession,
    series_ticker: str,
    api_key: str,
    private_key,
    sport: str,
    fetch_ts: str,
) -> list[dict]:
    """Fetch all open events for one Kalshi series. Paginated."""
    events_out: list[dict] = []
    cursor: str | None = None

    orderbook_cache: dict[str, float | None] = {}

    while True:
        path = f"/trade-api/v2/events"
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "with_nested_markets": "true",
            "status": "open",
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor

        # Build query string for signing
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        full_path = f"{path}?{qs}"
        headers = _kalshi_auth_headers(api_key, private_key, "GET", full_path)

        try:
            url = KALSHI_BASE_URL + full_path
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 401:
                    logger.error("Kalshi 401: check KALSHI_API_KEY and private key")
                    break
                if resp.status != 200:
                    logger.warning("Kalshi %s returned HTTP %d", series_ticker, resp.status)
                    break
                data = await resp.json()
        except Exception as e:
            logger.warning("Kalshi fetch %s failed: %s", series_ticker, e)
            break

        raw_events = data.get("events") or []
        for raw in raw_events:
            for m in raw.get("markets") or []:
                if m.get("status") not in ("open", "active") or m.get("result"):
                    continue

                executable_ask = None
                ask_dollars = _safe_float(m.get("yes_ask_dollars"))
                ask_size_fp = _safe_float(m.get("yes_ask_size_fp"))
                if ask_dollars is not None:
                    top_notional = ask_dollars * ask_size_fp if ask_size_fp is not None else 0.0
                    if top_notional >= CLOB_MIN_SIZE:
                        executable_ask = ask_dollars
                    else:
                        ticker = str(m.get("ticker") or "")
                        executable_ask = await _fetch_kalshi_executable_yes_ask(
                            session,
                            api_key,
                            private_key,
                            ticker,
                            orderbook_cache,
                            min_notional=CLOB_MIN_SIZE,
                        )
                else:
                    # Last-resort legacy fallback if *_dollars is absent.
                    executable_ask = _safe_float(m.get("yes_ask"))

                m["_executable_yes_ask"] = executable_ask
                m["_executable_yes_ask_checked"] = True

        for raw in raw_events:
            parsed = _parse_kalshi_event(raw, sport, fetch_ts)
            if parsed:
                events_out.extend(parsed)

        # Pagination
        if data.get("limited") and data.get("cursor"):
            cursor = data["cursor"]
        else:
            break

    logger.info("Kalshi %s: %d events for sport=%s", series_ticker, len(events_out), sport)
    return events_out


def _parse_kalshi_event(raw: dict, sport: str, fetch_ts: str) -> list[dict]:
    """
    Parse a single Kalshi event into 0–N normalized event dicts.
    Usually returns 1 event (one game). May return 0 if unparseable or settled.
    """
    title = raw.get("title") or ""
    event_ticker = raw.get("event_ticker") or ""
    event_url = f"https://kalshi.com/markets/{event_ticker}"

    markets = raw.get("markets") or []
    active = [m for m in markets if m.get("status") in ("open", "active") and not m.get("result")]
    if not active:
        return []

    # Parse start time from event category or date field
    start_time = _parse_kalshi_date(raw, event_ticker) or fetch_ts

    # Determine event type: moneyline game vs spread/total
    # For game moneylines: each sub-market subtitle = team name
    # For spread/total: subtitle contains line value info

    # Separate markets by type
    ml_markets: list[dict] = []
    spread_markets: list[dict] = []
    total_markets: list[dict] = []

    for m in active:
        subtitle = m.get("yes_sub_title") or m.get("subtitle") or m.get("title") or ""
        mtype, sel, lv = _classify_kalshi_market_type(subtitle, title)
        m["_mtype"] = mtype
        m["_selection"] = sel
        m["_line_value"] = lv
        if mtype == "total":
            total_markets.append(m)
        elif mtype == "spread":
            spread_markets.append(m)
        else:
            ml_markets.append(m)

    results: list[dict] = []

    # ── Moneyline markets ──────────────────────────────────────────────
    if len(ml_markets) == 2:
        # Each sub-market = one team winning
        team_a_m = ml_markets[0]
        team_b_m = ml_markets[1]
        sub_a = (team_a_m.get("yes_sub_title") or team_a_m.get("subtitle") or "").strip()
        sub_b = (team_b_m.get("yes_sub_title") or team_b_m.get("subtitle") or "").strip()

        yes_ask_a = _safe_float(team_a_m.get("_executable_yes_ask"))
        yes_ask_b = _safe_float(team_b_m.get("_executable_yes_ask"))

        if yes_ask_a and yes_ask_b and sub_a and sub_b:
            dec_a = pm_price_to_decimal(yes_ask_a, fee_rate=KALSHI_FEE_RATE)
            dec_b = pm_price_to_decimal(yes_ask_b, fee_rate=KALSHI_FEE_RATE)
            if dec_a > 1.0 and dec_b > 1.0:
                # Try to determine home/away from title
                teams_from_title = _parse_teams_from_title(title)
                if teams_from_title:
                    home_raw, away_raw = teams_from_title
                    # Match sub-market labels to home/away
                    if normalize_name(sub_a) == normalize_name(home_raw) or _fuzzy_name_match(sub_a, home_raw) > 80:
                        home_team, away_team = sub_a, sub_b
                        home_ask, away_ask = yes_ask_a, yes_ask_b
                        home_dec, away_dec = dec_a, dec_b
                    else:
                        home_team, away_team = sub_b, sub_a
                        home_ask, away_ask = yes_ask_b, yes_ask_a
                        home_dec, away_dec = dec_b, dec_a
                else:
                    home_team, away_team = sub_a, sub_b
                    home_ask, away_ask = yes_ask_a, yes_ask_b
                    home_dec, away_dec = dec_a, dec_b

                moneyline_markets: list[dict[str, Any]] = [
                    {
                        "market_type": "moneyline",
                        "selection": "home",
                        "team": home_team,
                        "american_odds": decimal_to_american(home_dec),
                        "decimal_odds": home_dec,
                        "line_value": None,
                        "source": "kalshi",
                        "_contract_type": "YES",
                        "_source_book": "kalshi",
                        "ask_price": home_ask,
                        "updated_at": fetch_ts,
                    },
                    {
                        "market_type": "moneyline",
                        "selection": "away",
                        "team": away_team,
                        "american_odds": decimal_to_american(away_dec),
                        "decimal_odds": away_dec,
                        "line_value": None,
                        "source": "kalshi",
                        "_contract_type": "YES",
                        "_source_book": "kalshi",
                        "ask_price": away_ask,
                        "updated_at": fetch_ts,
                    },
                ]

                # Parse Kalshi NO contract asks. NO on one team means the
                # opposing team wins, so selection is the opposite side.
                no_ask_a = _first_valid_price(team_a_m.get("no_ask_dollars"), team_a_m.get("no_ask"))
                no_ask_b = _first_valid_price(team_b_m.get("no_ask_dollars"), team_b_m.get("no_ask"))
                if no_ask_a:
                    no_dec_a = pm_price_to_decimal(no_ask_a, fee_rate=KALSHI_FEE_RATE)
                    if no_dec_a > 1.0:
                        no_selection_a = "away" if home_team == sub_a else "home"
                        moneyline_markets.append({
                            "market_type": "moneyline",
                            "selection": no_selection_a,
                            "team": away_team if no_selection_a == "away" else home_team,
                            "american_odds": decimal_to_american(no_dec_a),
                            "decimal_odds": no_dec_a,
                            "line_value": None,
                            "source": "kalshi",
                            "_contract_type": "NO",
                            "_source_book": "kalshi",
                            "ask_price": no_ask_a,
                            "updated_at": fetch_ts,
                        })
                if no_ask_b:
                    no_dec_b = pm_price_to_decimal(no_ask_b, fee_rate=KALSHI_FEE_RATE)
                    if no_dec_b > 1.0:
                        no_selection_b = "away" if home_team == sub_b else "home"
                        moneyline_markets.append({
                            "market_type": "moneyline",
                            "selection": no_selection_b,
                            "team": away_team if no_selection_b == "away" else home_team,
                            "american_odds": decimal_to_american(no_dec_b),
                            "decimal_odds": no_dec_b,
                            "line_value": None,
                            "source": "kalshi",
                            "_contract_type": "NO",
                            "_source_book": "kalshi",
                            "ask_price": no_ask_b,
                            "updated_at": fetch_ts,
                        })

                results.append({
                    "source": "kalshi",
                    "sport": sport,
                    "home_team": home_team,
                    "away_team": away_team,
                    "start_time": start_time,
                    "event_url": event_url,
                    "markets": moneyline_markets,
                })

    # ── Spread markets ─────────────────────────────────────────────────
    if spread_markets:
        for m in spread_markets:
            sel = m.get("_selection")
            lv = m.get("_line_value")
            if sel is None or lv is None:
                continue
            yes_ask = _safe_float(m.get("_executable_yes_ask"))
            if not yes_ask:
                continue
            dec = pm_price_to_decimal(yes_ask, fee_rate=KALSHI_FEE_RATE)
            if dec <= 1.0:
                continue

            teams_from_title = _parse_teams_from_title(title)
            if not teams_from_title:
                continue
            home_team, away_team = teams_from_title

            # Attach to matching game event if it exists, else create standalone
            matched = next((r for r in results if r.get("home_team") and r.get("away_team")), None)
            target = matched if matched else {
                "source": "kalshi",
                "sport": sport,
                "home_team": home_team,
                "away_team": away_team,
                "start_time": start_time,
                "event_url": event_url,
                "markets": [],
            }
            if matched is None:
                results.append(target)

            # signed_lv: home spread is negative (e.g. -3.5), away is positive
            signed_lv = -lv if sel == "home" else lv
            target["markets"].append({
                "market_type": "spread",
                "selection": sel,
                "team": home_team if sel == "home" else away_team,
                "american_odds": decimal_to_american(dec),
                "decimal_odds": dec,
                "line_value": signed_lv,
                "source": "kalshi",
                "_contract_type": "YES",
                "_source_book": "kalshi",
                "ask_price": yes_ask,
                "updated_at": fetch_ts,
            })

    # ── Total markets ──────────────────────────────────────────────────
    if len(total_markets) >= 2:
        over_m = next((m for m in total_markets if m.get("_selection") == "over"), None)
        under_m = next((m for m in total_markets if m.get("_selection") == "under"), None)
        if over_m and under_m:
            over_ask = _safe_float(over_m.get("_executable_yes_ask"))
            under_ask = _safe_float(under_m.get("_executable_yes_ask"))
            lv = over_m.get("_line_value") or under_m.get("_line_value")
            if over_ask and under_ask and lv:
                over_dec = pm_price_to_decimal(over_ask, fee_rate=KALSHI_FEE_RATE)
                under_dec = pm_price_to_decimal(under_ask, fee_rate=KALSHI_FEE_RATE)
                if over_dec > 1.0 and under_dec > 1.0:
                    teams_from_title = _parse_teams_from_title(title)
                    if teams_from_title:
                        home_team, away_team = teams_from_title
                    else:
                        home_team, away_team = "Home", "Away"

                    matched = next((r for r in results if r.get("home_team")), None)
                    target = matched if matched else {
                        "source": "kalshi",
                        "sport": sport,
                        "home_team": home_team,
                        "away_team": away_team,
                        "start_time": start_time,
                        "event_url": event_url,
                        "markets": [],
                    }
                    if matched is None:
                        results.append(target)

                    target["markets"].extend([
                        {
                            "market_type": "total",
                            "selection": "over",
                            "team": "",
                            "american_odds": decimal_to_american(over_dec),
                            "decimal_odds": over_dec,
                            "line_value": lv,
                            "source": "kalshi",
                            "_contract_type": "YES",
                            "_source_book": "kalshi",
                            "ask_price": over_ask,
                            "updated_at": fetch_ts,
                        },
                        {
                            "market_type": "total",
                            "selection": "under",
                            "team": "",
                            "american_odds": decimal_to_american(under_dec),
                            "decimal_odds": under_dec,
                            "line_value": lv,
                            "source": "kalshi",
                            "_contract_type": "YES",
                            "_source_book": "kalshi",
                            "ask_price": under_ask,
                            "updated_at": fetch_ts,
                        },
                    ])

    return results


def _parse_kalshi_date(raw: dict, ticker: str) -> str | None:
    """Try to extract ISO date from event data."""
    # Kalshi puts dates in event fields
    for field in ("start_date", "scheduled_close_time", "close_time", "created_time"):
        val = raw.get(field)
        if val:
            try:
                if isinstance(val, (int, float)):
                    return datetime.fromtimestamp(val / 1000, tz=timezone.utc).isoformat()
                return datetime.fromisoformat(str(val).replace("Z", "+00:00")).isoformat()
            except Exception:
                pass
    # Try to parse date from ticker like KXNBAGAME-26MAR18 → 2026-03-18
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", ticker)
    if m:
        try:
            year = 2000 + int(m.group(1))
            month_str = m.group(2)
            day = int(m.group(3))
            months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                      "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
            month = months.get(month_str, 0)
            if month:
                return datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    return None


async def fetch_kalshi_events(sport_keys: list[str]) -> list[dict]:
    """
    Fetch all open Kalshi events for the given sport keys.
    sport_keys: lowercase sport names, e.g. ["nba", "nfl", "nhl"]
    Returns list of normalized event dicts in bovada_scraper schema.
    """
    api_key = KALSHI_API_KEY
    if not api_key:
        logger.error("KALSHI_API_KEY is not set — skipping Kalshi fetch entirely")
        return []

    private_key = _load_kalshi_private_key()
    if private_key is None:
        logger.error("Kalshi private key failed to load — skipping Kalshi fetch entirely")
        return []

    logger.info("Kalshi: starting fetch for sports=%s", sport_keys)
    fetch_ts = _utc_now_iso()
    all_events: list[dict] = []
    seen_titles: set[str] = set()  # deduplicate across series

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx, limit=KALSHI_WORKERS)

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        for sport in sport_keys:
            sport_l = sport.lower()
            # Moneyline series
            for ticker in _SPORT_TO_KALSHI_SERIES.get(sport_l, []):
                tasks.append((sport_l, ticker))
            # Spread/total series (if available for sport)
            for ticker in _SPORT_TO_KALSHI_PROP_SERIES.get(sport_l, []):
                tasks.append((sport_l, ticker))

        # Semaphore to cap concurrency
        sem = asyncio.Semaphore(KALSHI_WORKERS)

        async def _fetch_with_sem(sport, ticker):
            async with sem:
                return await _fetch_kalshi_series(session, ticker, api_key, private_key, sport, fetch_ts)

        results = await asyncio.gather(
            *[_fetch_with_sem(s, t) for s, t in tasks],
            return_exceptions=True,
        )

        for res in results:
            if isinstance(res, Exception):
                logger.warning("Kalshi series fetch error: %s", res)
                continue
            for event in res:
                key = f"{event.get('sport')}:{normalize_name(event.get('home_team',''))}:{normalize_name(event.get('away_team',''))}"
                if key not in seen_titles:
                    seen_titles.add(key)
                    all_events.append(event)

    logger.info("Kalshi total: %d events across %d sports", len(all_events), len(sport_keys))
    return all_events


# ─────────────────────────────────────────────
# POLYMARKET — FETCH & PARSE
# ─────────────────────────────────────────────

_PM_SPREAD_KW  = re.compile(r"\bspread\b|\bcover\b|\bats\b|\bhandicap\b", re.I)
_PM_TOTAL_KW   = re.compile(r"\btotal\b|\bover/under\b|\bo/u\b|\bpoints scored\b|\bgoals scored\b|\bruns scored\b", re.I)
_PM_FUTURES_KW = re.compile(r"\bchampionship\b|\bwin the\b|\bseason\b|\bmvp\b|\bfinals\b|\bplayoff\b|\bdraft\b", re.I)
_PM_PROP_KW    = re.compile(r"\bpoints?\b.*player|\brebounds?\b|\bassists?\b|\bstrikes?\b|\bhome run\b", re.I)


def _pm_market_type(question: str) -> str | None:
    """Classify Polymarket question. Returns 'moneyline', 'spread', 'total', or None (skip)."""
    q = question or ""
    if _PM_FUTURES_KW.search(q):
        return None
    if _PM_PROP_KW.search(q):
        return None
    if _PM_TOTAL_KW.search(q):
        return "total"
    if _PM_SPREAD_KW.search(q):
        return "spread"
    return "moneyline"


def _extract_line_from_question(question: str) -> float | None:
    """Extract numeric line value from a spread/total question like 'over 215.5'."""
    nums = re.findall(r"\d+\.?\d*", question)
    if nums:
        # Take the last number — avoids dates/years at start
        try:
            return float(nums[-1])
        except ValueError:
            pass
    return None


def _parse_polymarket_event(raw: dict, sport: str, fetch_ts: str) -> dict | None:
    """
    Parse a single Polymarket market dict into a normalized event.
    Returns None if the market should be skipped.
    """
    import json as _json

    question = raw.get("question") or raw.get("title") or ""
    mtype = _pm_market_type(question)
    if mtype is None:
        return None

    # Outcomes / prices / tokens may be JSON strings or lists
    def _ensure_list(val):
        if isinstance(val, str):
            try:
                return _json.loads(val)
            except (ValueError, TypeError):
                return []
        return val or []

    outcomes = _ensure_list(raw.get("outcomes"))
    outcome_prices = _ensure_list(raw.get("outcomePrices"))
    clob_token_ids = _ensure_list(raw.get("clobTokenIds"))

    # Filter to 2-outcome markets only (binary win/lose)
    if len(outcomes) != 2:
        return None

    # Parse team names from outcomes (Polymarket uses team names as outcome labels)
    outcome_a = str(outcomes[0]).strip()
    outcome_b = str(outcomes[1]).strip()

    if not outcome_a or not outcome_b:
        return None

    # Skip generic Yes/No outcomes — these aren't team markets
    if outcome_a.lower() in ("yes", "no") and outcome_b.lower() in ("yes", "no"):
        # For totals that use Yes/No, the line is in the question
        if mtype != "total":
            return None
        # For Yes/No totals, treat as over/under
        outcome_a = "over"
        outcome_b = "under"

    # Mid-prices (will be replaced by CLOB later)
    price_a = _safe_float(outcome_prices[0] if len(outcome_prices) > 0 else None) or 0.5
    price_b = _safe_float(outcome_prices[1] if len(outcome_prices) > 1 else None) or (1.0 - price_a)

    token_a = clob_token_ids[0] if len(clob_token_ids) > 0 else None
    token_b = clob_token_ids[1] if len(clob_token_ids) > 1 else None

    # Dates
    end_date = raw.get("endDate") or raw.get("end_date_iso") or ""
    start_time = _parse_pm_date(end_date) or fetch_ts

    event_url = f"https://polymarket.com/event/{raw.get('slug', '')}"

    if mtype == "moneyline":
        # outcome_a / outcome_b = team names
        # Some Polymarket moneyline markets can return generic outcome labels
        # like "Away"/"Home". Recover team names from the question/title.
        if outcome_a.lower() in ("home", "away") or outcome_b.lower() in ("home", "away"):
            parsed = None
            try:
                parts = re.split(r"\s+vs\.?\s+", str(question or raw.get("title") or "").strip(), flags=re.IGNORECASE)
                parts = [p.strip() for p in parts if p.strip()]
                if len(parts) >= 2:
                    parsed = (parts[0], parts[1])
            except Exception:
                parsed = None
            if parsed:
                outcome_a, outcome_b = parsed

        dec_a = pm_price_to_decimal(price_a, fee_rate=POLYMARKET_FEE_RATE) if price_a > 0 else 0
        dec_b = pm_price_to_decimal(price_b, fee_rate=POLYMARKET_FEE_RATE) if price_b > 0 else 0
        if dec_a <= 1.0 or dec_b <= 1.0:
            return None

        # Still generic after parsing -> skip the event to avoid polluting merges.
        if outcome_a.lower() in ("home", "away") or outcome_b.lower() in ("home", "away"):
            return None

        return {
            "source": "polymarket",
            "sport": sport,
            "home_team": outcome_a,
            "away_team": outcome_b,
            "start_time": start_time,
            "event_url": event_url,
            "markets": [
                {
                    "market_type": "moneyline",
                    "selection": "home",
                    "team": outcome_a,
                    "american_odds": decimal_to_american(dec_a),
                    "decimal_odds": dec_a,
                    "line_value": None,
                    "source": "polymarket",
                    "_contract_type": "YES",
                    "_source_book": "polymarket",
                    "ask_price": price_a,   # mid-price placeholder, updated by CLOB
                    "updated_at": fetch_ts,
                    "_clob_token": token_a,
                },
                {
                    "market_type": "moneyline",
                    "selection": "away",
                    "team": outcome_b,
                    "american_odds": decimal_to_american(dec_b),
                    "decimal_odds": dec_b,
                    "line_value": None,
                    "source": "polymarket",
                    "_contract_type": "YES",
                    "_source_book": "polymarket",
                    "ask_price": price_b,
                    "updated_at": fetch_ts,
                    "_clob_token": token_b,
                },
            ],
            "_raw_poly_id": raw.get("id"),
        }

    elif mtype == "total":
        line_value = _extract_line_from_question(question)
        over_sel = "over" if "over" in question.lower() else None
        # For Yes/No structure: Yes = over if "over" in question, else ambiguous
        sel_a = over_sel if over_sel else "over"
        sel_b = "under"

        dec_a = pm_price_to_decimal(price_a, fee_rate=POLYMARKET_FEE_RATE) if price_a > 0 else 0
        dec_b = pm_price_to_decimal(price_b, fee_rate=POLYMARKET_FEE_RATE) if price_b > 0 else 0
        if dec_a <= 1.0 or dec_b <= 1.0:
            return None

        return {
            "source": "polymarket",
            "sport": sport,
            "home_team": question[:60],   # game context in title
            "away_team": "",
            "start_time": start_time,
            "event_url": event_url,
            "markets": [
                {
                    "market_type": "total",
                    "selection": sel_a,
                    "team": "",
                    "american_odds": decimal_to_american(dec_a),
                    "decimal_odds": dec_a,
                    "line_value": line_value,
                    "source": "polymarket",
                    "_contract_type": "YES",
                    "_source_book": "polymarket",
                    "ask_price": price_a,
                    "updated_at": fetch_ts,
                    "_clob_token": token_a,
                },
                {
                    "market_type": "total",
                    "selection": sel_b,
                    "team": "",
                    "american_odds": decimal_to_american(dec_b),
                    "decimal_odds": dec_b,
                    "line_value": line_value,
                    "source": "polymarket",
                    "_contract_type": "YES",
                    "_source_book": "polymarket",
                    "ask_price": price_b,
                    "updated_at": fetch_ts,
                    "_clob_token": token_b,
                },
            ],
        }

    return None


def _parse_pm_date(date_str: str) -> str | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(str(date_str).replace("Z", "+00:00")).isoformat()
    except Exception:
        return None


_POLY_RETRY_ATTEMPTS = 3
_POLY_RETRY_DELAYS   = [2.0, 5.0, 10.0]


async def _fetch_polymarket_tag(
    session: aiohttp.ClientSession,
    tag: str,
    sport: str,
    fetch_ts: str,
) -> list[dict]:
    """Fetch Polymarket events by series_id with retry on 429.

    Despite the function name (kept for compatibility), this now uses
    the /events endpoint with series_id instead of /markets with tag_slug.
    The `tag` parameter is now a Gamma series ID.
    """
    events: list[dict] = []
    offset = 0
    limit  = 100

    while True:
        url    = f"{POLYMARKET_GAMMA_URL}/events"
        params = {
            "series_id": tag,
            "active":    "true",
            "closed":    "false",
            "limit":     str(limit),
            "offset":    str(offset),
        }

        page_data = None
        for attempt in range(_POLY_RETRY_ATTEMPTS):
            try:
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status == 429:
                        wait = _POLY_RETRY_DELAYS[attempt]
                        logger.warning(
                            "Polymarket series=%s got 429, attempt %d/%d — waiting %.1fs",
                            tag, attempt + 1, _POLY_RETRY_ATTEMPTS, wait
                        )
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        logger.warning("Polymarket series=%s HTTP %d — skipping", tag, resp.status)
                        return events
                    page_data = await resp.json()
                    break
            except Exception as e:
                wait = _POLY_RETRY_DELAYS[attempt]
                logger.warning(
                    "Polymarket series=%s fetch error attempt %d/%d: %s — waiting %.1fs",
                    tag, attempt + 1, _POLY_RETRY_ATTEMPTS, e, wait
                )
                await asyncio.sleep(wait)

        if page_data is None:
            logger.error("Polymarket series=%s failed after %d attempts", tag, _POLY_RETRY_ATTEMPTS)
            break

        if not isinstance(page_data, list) or not page_data:
            break

        for event_obj in page_data:
            event_slug = event_obj.get("slug", "")
            nested_markets = event_obj.get("markets") or []
            for raw in nested_markets:
                if not raw.get("slug"):
                    raw["slug"] = event_slug
                parsed = _parse_polymarket_event(raw, sport, fetch_ts)
                if parsed:
                    events.append(parsed)

        if len(page_data) < limit:
            break

        offset += limit
        await asyncio.sleep(0.5)

    logger.info("Polymarket series=%s: %d events", tag, len(events))
    return events


async def fetch_polymarket_events(sport_keys: list[str]) -> list[dict]:
    """
    Fetch all active Polymarket events for the given sport keys.
    Uses staggered requests to avoid 429 rate limiting.
    """
    fetch_ts    = _utc_now_iso()
    all_events: list[dict] = []
    seen:       set[str]   = set()

    ssl_ctx   = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx, limit=POLY_WORKERS)

    task_pairs: list[tuple[str, str]] = []
    for sport in sport_keys:
        sport_l = sport.lower()
        for series_id in _SPORT_TO_POLY_SERIES.get(sport_l, []):
            task_pairs.append((sport_l, series_id))

    async with aiohttp.ClientSession(connector=connector) as session:
        batch_size = POLY_WORKERS
        for i in range(0, len(task_pairs), batch_size):
            batch = task_pairs[i:i + batch_size]

            results = await asyncio.gather(
                *[_fetch_polymarket_tag(session, tag, sport, fetch_ts)
                  for sport, tag in batch],
                return_exceptions=True,
            )

            for res in results:
                if isinstance(res, Exception):
                    logger.warning("Polymarket batch fetch error: %s", res)
                    continue
                for event in res:
                    key = (
                        f"{event.get('sport')}:"
                        f"{normalize_name(event.get('home_team', ''))}:"
                        f"{normalize_name(event.get('away_team', ''))}"
                    )
                    if key not in seen:
                        seen.add(key)
                        all_events.append(event)

            if i + batch_size < len(task_pairs):
                await asyncio.sleep(1.5)

    logger.info(
        "Polymarket total: %d events across %d sports",
        len(all_events), len(sport_keys)
    )
    return all_events


# ─────────────────────────────────────────────
# POLYMARKET — CLOB PRICE UPDATE
# ─────────────────────────────────────────────

async def _fetch_clob_executable_ask(
    session: aiohttp.ClientSession,
    token_id: str,
) -> float | None:
    """
    Fetch executable buy price from Polymarket CLOB book for a token.

    We walk asks from best to worse until we can fill CLOB_MIN_SIZE notional.
    The returned value is the worst ask touched to guarantee an executable fill.
    """
    if not token_id:
        return None
    try:
        url = f"{POLYMARKET_CLOB_URL}/book"
        params = {"token_id": token_id}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            asks = data.get("asks") or []
            if not isinstance(asks, list) or not asks:
                return None

            # Polymarket returns asks in an arbitrary order; "best" isn't guaranteed
            # to be the first element. For an executable BUY we should walk asks
            # from lowest price upward until we can fill CLOB_MIN_SIZE notional.
            levels: list[tuple[float, float]] = []
            for level in asks:
                if not isinstance(level, dict):
                    continue
                price = _safe_float(level.get("price"))
                size = _safe_float(level.get("size"))
                if price is None or size is None or not (0 < price < 1) or size <= 0:
                    continue
                levels.append((price, size))

            if not levels:
                return None

            levels.sort(key=lambda ps: ps[0])  # cheapest ask first

            cum_notional = 0.0
            worst_ask: float | None = None
            for price, size in levels:
                cum_notional += price * size
                worst_ask = price
                if cum_notional >= CLOB_MIN_SIZE:
                    return worst_ask
    except Exception as e:
        logger.debug("CLOB fetch token=%s failed: %s", token_id, e)
    return None


async def update_polymarket_clob_prices(events: list[dict]) -> list[dict]:
    """
    Replace Polymarket mid-prices with real CLOB ask prices.
    Mutates events in-place and returns them.
    CLOB ask = what you actually pay to enter the position.
    """
    if not events:
        return events

    # Collect all markets that have a _clob_token
    token_markets: list[dict] = []
    for event in events:
        for market in event.get("markets") or []:
            if market.get("_clob_token"):
                token_markets.append(market)

    if not token_markets:
        return events

    fetch_ts = _utc_now_iso()
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx, limit=CLOB_WORKERS)
    sem = asyncio.Semaphore(CLOB_WORKERS)

    async with aiohttp.ClientSession(connector=connector) as session:
        async def _update(market: dict):
            async with sem:
                token = market["_clob_token"]
                ask = await _fetch_clob_executable_ask(session, token)
                if ask and 0 < ask < 1:
                    dec = pm_price_to_decimal(ask, fee_rate=POLYMARKET_FEE_RATE)
                    if dec > 1.0:
                        market["ask_price"] = ask
                        market["decimal_odds"] = dec
                        market["american_odds"] = decimal_to_american(dec)
                        market["updated_at"] = fetch_ts
                        market["_clob_updated"] = True

        await asyncio.gather(*[_update(m) for m in token_markets], return_exceptions=True)

    # Mark any market whose CLOB ask could not be resolved.
    # Gamma provided only a midpoint, so without a resolved CLOB ask we must
    # treat this as stale-mid placeholder and exclude it from arb detection.
    for m in token_markets:
        if not m.get("_clob_updated"):
            logger.warning(
                "Polymarket CLOB miss for token %s — mid-price placeholder remains. "
                "This market will be excluded from arb detection.",
                m.get("_clob_token", "unknown"),
            )
            m["_price_is_stale_midprice"] = True

    updated = sum(1 for m in token_markets if m.get("_clob_updated"))
    logger.info("Polymarket CLOB: updated %d/%d market prices", updated, len(token_markets))
    return events


# ─────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ─────────────────────────────────────────────

async def fetch_all_prediction_markets(sport_keys: list[str]) -> tuple[list[dict], list[dict]]:
    """
    Fetch Kalshi + Polymarket events concurrently for the given sports.
    Returns (kalshi_events, polymarket_events).
    Both returned lists use bovada_scraper schema.
    Polymarket events have CLOB ask prices applied.
    """
    kalshi_task = fetch_kalshi_events(sport_keys)
    poly_task   = fetch_polymarket_events(sport_keys)

    kalshi_events, poly_events_mid = await asyncio.gather(kalshi_task, poly_task)

    # Update Polymarket prices from CLOB
    poly_events = await update_polymarket_clob_prices(poly_events_mid)

    return kalshi_events, poly_events


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return f if f == f else None  # NaN check
    except (ValueError, TypeError):
        return None


def _first_valid_price(*field_values) -> float | None:
    """
    Return the first non-None float from the provided values.

    Uses explicit None check (NOT falsy check), so 0.0 is treated as valid and
    does not silently fall back to a different price field.
    """
    for v in field_values:
        f = _safe_float(v)
        if f is not None:
            return f
    return None


def _fuzzy_name_match(a: str, b: str) -> float:
    """Simple character-level similarity 0–100."""
    try:
        from rapidfuzz import fuzz
        return float(fuzz.token_set_ratio(normalize_name(a), normalize_name(b)))
    except ImportError:
        # Fallback: simple equality
        return 100.0 if normalize_name(a) == normalize_name(b) else 0.0
