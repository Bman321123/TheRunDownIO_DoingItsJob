"""
pm_debug.py — Standalone diagnostic for Kalshi + Polymarket + TheRundown pipeline.

Usage:
    python3 pm_debug.py nba nhl ncaab
    python3 pm_debug.py              # defaults to nba
"""

import sys
import asyncio
import logging

from dotenv import load_dotenv
load_dotenv()

# `therundown.py` reads/parses `sys.argv` at import-time and assumes numeric IDs.
# When pm_debug is invoked as `python3 pm_debug.py nba nhl ...` that import would crash.
_ORIG_ARGV = list(sys.argv)
sys.argv = [_ORIG_ARGV[0]]

import prediction_markets
import therundown
import server
from server import (
    scan_arbs_once,
    _normalize_rundown_events_for_matching,
    _merge_all_sources,
    _match_source_to_hubs,
    _run_all_cross_source_arbs,
    _RUNDOWN_TO_PM_SPORT,
    _sport_id_by_name,
    _team_match_score,
)

sys.argv = _ORIG_ARGV

logging.basicConfig(level=logging.WARNING)

_SEP = "═" * 60


def _odds_str(american: int | float | None) -> str:
    if american is None:
        return "N/A"
    am = int(round(float(american)))
    return f"+{am}" if am > 0 else str(am)


def _resolve_sport_args(raw_args: list[str]) -> tuple[list[int], list[str]]:
    """Return (sport_ids, sport_keys) from CLI args like ['nba', 'nhl']."""
    name_map = _sport_id_by_name()
    sport_ids: list[int] = []
    sport_keys: list[str] = []

    for arg in raw_args:
        lower = arg.lower()
        sid = name_map.get(lower)
        if sid is None:
            print(f"WARNING: unknown sport '{arg}', skipping (valid: {list(name_map.keys())})")
            continue
        sport_ids.append(sid)
        upper = therundown.ALL_SPORTS.get(sid, lower.upper())
        pm_key = _RUNDOWN_TO_PM_SPORT.get(upper, lower)
        sport_keys.append(pm_key)

    return sport_ids, sport_keys


# ──────────────────────────────────────────────
#  SECTION 1 — KALSHI RAW DATA
# ──────────────────────────────────────────────

async def section_1_kalshi(sport_keys: list[str]) -> list[dict]:
    print(f"\n{_SEP}")
    print("  SECTION 1 — KALSHI RAW DATA")
    print(_SEP)

    try:
        events = await prediction_markets.fetch_kalshi_events(sport_keys)
    except Exception as exc:
        print(f"  ERROR fetching Kalshi events: {exc}")
        return []

    print(f"  Total Kalshi events: {len(events)}")

    if not events:
        print("  KALSHI: 0 events returned — check KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH are set correctly")
        return []

    for i, evt in enumerate(events, 1):
        sport = evt.get("sport", "?")
        home = evt.get("home_team", "?")
        away = evt.get("away_team", "?")
        start = evt.get("start_time", "?")
        print(f"\n  [{i}] {sport.upper()} — {away} @ {home}  ({start})")

        for m in evt.get("markets") or []:
            mtype = m.get("market_type", "?")
            sel = m.get("selection", "?")
            ask = m.get("ask_price")
            am = m.get("american_odds")
            dec = m.get("decimal_odds")
            line = m.get("line_value")
            line_str = f"  line={line}" if line is not None else ""
            ask_str = f"ask={ask:.3f}" if ask is not None else "ask=N/A"
            print(
                f"       {mtype:<12} {sel:<8} {ask_str}  "
                f"am={_odds_str(am)}  dec={dec or 'N/A'}{line_str}"
            )

    return events


# ──────────────────────────────────────────────
#  SECTION 2 — POLYMARKET RAW DATA (MID-PRICES)
# ──────────────────────────────────────────────

async def section_2_polymarket(sport_keys: list[str]) -> list[dict]:
    print(f"\n{_SEP}")
    print("  SECTION 2 — POLYMARKET RAW DATA (MID-PRICES)")
    print(_SEP)

    try:
        events = await asyncio.wait_for(
            prediction_markets.fetch_polymarket_events(sport_keys),
            timeout=30.0,
        )
    except Exception as exc:
        print(f"  ERROR fetching Polymarket events: {exc}")
        return []

    print(f"  Total Polymarket events (mid-price stage): {len(events)}")

    if not events:
        print("  POLYMARKET: 0 events returned — Gamma API may be down or tag slugs returned no results")
        return []

    for i, evt in enumerate(events, 1):
        sport = evt.get("sport", "?")
        home = evt.get("home_team", "?")
        away = evt.get("away_team", "?")
        start = evt.get("start_time", "?")
        url = evt.get("event_url", "")
        print(f"\n  [{i}] {sport.upper()} — {away} @ {home}  ({start})")
        if url:
            print(f"       URL: {url}")

        for m in evt.get("markets") or []:
            mtype = m.get("market_type", "?")
            sel = m.get("selection", "?")
            mid = m.get("ask_price")
            am = m.get("american_odds")
            mid_str = f"mid={mid:.3f}" if mid is not None else "mid=N/A"
            print(f"       {mtype:<12} {sel:<8} {mid_str}  am={_odds_str(am)}")

    return events


# ──────────────────────────────────────────────
#  SECTION 3 — POLYMARKET CLOB PRICE UPDATE
# ──────────────────────────────────────────────

async def section_3_clob(poly_events: list[dict]) -> list[dict]:
    print(f"\n{_SEP}")
    print("  SECTION 3 — POLYMARKET CLOB PRICE UPDATE")
    print(_SEP)

    if not poly_events:
        print("  Skipped — no Polymarket events to update.")
        return poly_events

    mid_snapshot: dict[str, float] = {}
    tokens_total = 0
    for evt in poly_events:
        for m in evt.get("markets") or []:
            tok = m.get("_clob_token")
            if tok:
                tokens_total += 1
                key = f"{evt.get('home_team')}|{m.get('market_type')}|{m.get('selection')}"
                mid = m.get("ask_price")
                if mid is not None:
                    mid_snapshot[key] = float(mid)

    print(f"  Markets with _clob_token: {tokens_total}")

    try:
        poly_events = await asyncio.wait_for(
            prediction_markets.update_polymarket_clob_prices(poly_events),
            timeout=30.0,
        )
    except Exception as exc:
        print(f"  ERROR updating CLOB prices: {exc}")
        return poly_events

    updated_count = 0
    for evt in poly_events:
        game_label = f"{evt.get('away_team', '?')} @ {evt.get('home_team', '?')}"
        for m in evt.get("markets") or []:
            tok = m.get("_clob_token")
            if not tok:
                continue
            mtype = m.get("market_type", "?")
            sel = m.get("selection", "?")
            key = f"{evt.get('home_team')}|{mtype}|{sel}"

            if m.get("_clob_updated"):
                updated_count += 1
                clob_ask = m.get("ask_price")
                mid_price = mid_snapshot.get(key)
                if clob_ask is not None and mid_price is not None:
                    diff = abs(clob_ask - mid_price)
                    if diff > 0.05:
                        print(
                            f"  CLOB DRIFT: {game_label} {mtype} {sel} "
                            f"mid={mid_price:.3f} clob={clob_ask:.3f} diff={diff:.3f}"
                        )
            else:
                print(f"  CLOB MISS: {game_label} {mtype} {sel} token={tok}")

    print(f"  CLOB successfully updated: {updated_count}/{tokens_total}")

    return poly_events


# ──────────────────────────────────────────────
#  SECTION 4 — THERUNDOWN RAW DATA
# ──────────────────────────────────────────────

def section_4_therundown(sport_ids: list[int]) -> list[dict]:
    print(f"\n{_SEP}")
    print("  SECTION 4 — THERUNDOWN RAW DATA")
    print(_SEP)

    try:
        scan_arbs_once(sport_ids)
    except Exception as exc:
        print(f"  ERROR running scan_arbs_once: {exc}")

    try:
        hub_events = _normalize_rundown_events_for_matching(sport_ids)
    except Exception as exc:
        print(f"  ERROR normalizing rundown events: {exc}")
        return []

    sport_counts: dict[str, int] = {}
    for evt in hub_events:
        s = evt.get("sport", "?")
        sport_counts[s] = sport_counts.get(s, 0) + 1

    for sport, count in sport_counts.items():
        print(f"  {sport.upper()}: {count} events")

    for i, evt in enumerate(hub_events, 1):
        sport = evt.get("sport", "?")
        home = evt.get("home_team", "?")
        away = evt.get("away_team", "?")
        start = evt.get("start_time", "?")
        markets = evt.get("markets") or []

        raw = evt.get("_raw_event") or {}
        books_with_data: set[str] = set()
        for line_market in raw.get("lines", {}).values():
            if isinstance(line_market, dict):
                for book_id_str in line_market.keys():
                    try:
                        bid = int(book_id_str)
                        bname = therundown.KNOWN_BOOKS.get(bid)
                        if bname:
                            books_with_data.add(bname)
                    except (ValueError, TypeError):
                        pass
        for mkt in raw.get("markets", []):
            if isinstance(mkt, dict):
                for part in mkt.get("participants", []):
                    if isinstance(part, dict):
                        for ln in part.get("lines", []):
                            if isinstance(ln, dict):
                                for book_id_str in ln.get("prices", {}).keys():
                                    try:
                                        bid = int(book_id_str)
                                        bname = therundown.KNOWN_BOOKS.get(bid)
                                        if bname:
                                            books_with_data.add(bname)
                                    except (ValueError, TypeError):
                                        pass

        key_books = {"BetMGM", "FanDuel", "DraftKings"} & books_with_data
        books_str = ", ".join(sorted(key_books)) if key_books else "(none of BetMGM/FanDuel/DraftKings)"

        print(
            f"  [{i}] {sport.upper()} — {away} @ {home}  ({start})  "
            f"markets={len(markets)}  books=[{books_str}]"
        )

    print(f"\n  Total TheRundown hub events: {len(hub_events)}")
    return hub_events


# ──────────────────────────────────────────────
#  SECTION 5 — CROSS-SOURCE MATCHING
# ──────────────────────────────────────────────

def section_5_matching(
    rundown_events: list[dict],
    kalshi_events: list[dict],
    poly_events: list[dict],
) -> tuple[list[dict], dict]:
    print(f"\n{_SEP}")
    print("  SECTION 5 — CROSS-SOURCE MATCHING")
    print(_SEP)

    kalshi_matched, kalshi_unmatched = _match_source_to_hubs(kalshi_events, rundown_events, "kalshi")
    poly_matched, poly_unmatched = _match_source_to_hubs(poly_events, rundown_events, "polymarket")

    try:
        consolidated = _merge_all_sources(rundown_events, [], kalshi_events, poly_events)
    except Exception as exc:
        print(f"  ERROR in _merge_all_sources: {exc}")
        consolidated = []

    # ── matched game table ──
    print(f"\n  {'SPORT':<7} {'THERUNDOWN NAME':<30} {'KALSHI NAME':<25} {'POLY NAME':<25} {'SCORE':>5}")
    print("  " + "-" * 95)

    matched_count = 0
    for idx, hub in enumerate(rundown_events):
        k_markets = kalshi_matched.get(idx, [])
        p_markets = poly_matched.get(idx, [])
        if not k_markets and not p_markets:
            continue
        matched_count += 1

        sport = hub.get("sport", "?").upper()
        hub_name = f"{hub.get('away_team', '?')} @ {hub.get('home_team', '?')}"

        k_name = _find_source_name_for_hub(idx, kalshi_events, kalshi_matched, "kalshi")
        p_name = _find_source_name_for_hub(idx, poly_events, poly_matched, "polymarket")

        print(f"  {sport:<7} {hub_name:<30} {k_name:<25} {p_name:<25}")

    if matched_count == 0:
        print("  (no cross-source matches)")

    # ── unmatched ──
    if kalshi_unmatched:
        print(f"\n  KALSHI UNMATCHED (no TheRundown counterpart found):")
        for evt in kalshi_unmatched:
            sport = evt.get("sport", "?")
            home = evt.get("home_team", "?")
            away = evt.get("away_team", "?")
            start = (evt.get("start_time") or "")[:10]
            print(f"    - {sport}: {away} vs {home} ({start})")

    if poly_unmatched:
        print(f"\n  POLYMARKET UNMATCHED (no TheRundown counterpart found):")
        for evt in poly_unmatched:
            sport = evt.get("sport", "?")
            home = evt.get("home_team", "?")
            away = evt.get("away_team", "?")
            start = (evt.get("start_time") or "")[:10]
            print(f"    - {sport}: {away} vs {home} ({start})")

    # ── PM-only pairs ──
    pm_pairs = _find_pm_only_pairs(kalshi_unmatched, poly_unmatched)
    if pm_pairs:
        print(f"\n  PM-ONLY PAIR (neither matched TheRundown — PM vs PM arb still possible):")
        for k_evt, p_evt in pm_pairs:
            sport = k_evt.get("sport", "?")
            k_label = f"{k_evt.get('away_team', '?')} vs {k_evt.get('home_team', '?')}"
            p_label = f"{p_evt.get('away_team', '?')} vs {p_evt.get('home_team', '?')}"
            print(f"    - {sport}: {k_label} (Kalshi) / {p_label} (Polymarket)")

    stats = {
        "matched": matched_count,
        "kalshi_unmatched": len(kalshi_unmatched),
        "poly_unmatched": len(poly_unmatched),
    }

    return consolidated, stats


def _find_source_name_for_hub(
    hub_idx: int,
    source_events: list[dict],
    match_dict: dict[int, list[dict]],
    source_name: str,
) -> str:
    """Reverse-lookup the source event that matched a given hub index."""
    if hub_idx not in match_dict:
        return "—"
    for evt in source_events:
        home = evt.get("home_team", "")
        away = evt.get("away_team", "")
        if home or away:
            return f"{away} @ {home}"
    return source_name


def _find_pm_only_pairs(
    kalshi_unmatched: list[dict],
    poly_unmatched: list[dict],
) -> list[tuple[dict, dict]]:
    pairs: list[tuple[dict, dict]] = []
    used_poly: set[int] = set()

    for k_evt in kalshi_unmatched:
        k_sport = str(k_evt.get("sport", "")).lower()
        k_home = str(k_evt.get("home_team", ""))
        k_away = str(k_evt.get("away_team", ""))

        for j, p_evt in enumerate(poly_unmatched):
            if j in used_poly:
                continue
            if str(p_evt.get("sport", "")).lower() != k_sport:
                continue

            p_home = str(p_evt.get("home_team", ""))
            p_away = str(p_evt.get("away_team", ""))

            score_direct = min(_team_match_score(k_home, p_home), _team_match_score(k_away, p_away))
            score_swap = min(_team_match_score(k_home, p_away), _team_match_score(k_away, p_home))
            best = max(score_direct, score_swap)

            if best >= 70:
                pairs.append((k_evt, p_evt))
                used_poly.add(j)
                break

    return pairs


# ──────────────────────────────────────────────
#  SECTION 6 — ARB DETECTION
# ──────────────────────────────────────────────

def section_6_arbs(consolidated: list[dict]) -> tuple[list[dict], list[dict]]:
    print(f"\n{_SEP}")
    print("  SECTION 6 — ARB DETECTION")
    print(_SEP)

    if not consolidated:
        print("  Skipped — no consolidated events to analyze.")
        return [], []

    try:
        arbs, raw_lines = _run_all_cross_source_arbs(consolidated)
    except Exception as exc:
        print(f"  ERROR in _run_all_cross_source_arbs: {exc}")
        return [], []

    if arbs:
        for arb in arbs:
            profit = arb.get("profit", 0)
            game = arb.get("game", "?")
            sport = arb.get("sport", "?")
            mk = arb.get("market_kind", "?")
            label = arb.get("market_label") or arb.get("line_label") or mk

            side_a = arb.get("side_a", "?")
            book_a = arb.get("book_a", "?")
            odds_a = arb.get("odds_a_am")

            side_b = arb.get("side_b", "?")
            book_b = arb.get("book_b", "?")
            odds_b = arb.get("odds_b_am")

            ask_a = arb.get("ask_price_a")
            ask_b = arb.get("ask_price_b")
            ask_a_str = f"  ask=${ask_a:.2f}" if ask_a is not None else ""
            ask_b_str = f"  ask=${ask_b:.2f}" if ask_b is not None else ""

            arb_type = _classify_arb_type(book_a, book_b)

            print(f"\n  ✅ ARB FOUND")
            print(f"     Game:      {game} ({sport})")
            print(f"     Market:    {label}")
            print(f"     Leg A:     {side_a} @ {book_a}  odds={_odds_str(odds_a)}{ask_a_str}")
            print(f"     Leg B:     {side_b} @ {book_b}  odds={_odds_str(odds_b)}{ask_b_str}")
            print(f"     Profit:    +{profit:.2f}%")
            print(f"     Type:      {arb_type}")
    else:
        print("\n  NO ARBS FOUND")
        print("  Reasons to check:")
        print("    - Are ask prices fresh? (updated_at within 30 min)")
        print("    - Did matching succeed? (see Section 5)")
        print("    - Are decimal odds > 1.0 on both legs?")

    return arbs, raw_lines


_PM_BOOKS = {"Kalshi", "Polymarket", "Bovada"}

def _classify_arb_type(book_a: str, book_b: str) -> str:
    a_pm = book_a in {"Kalshi", "Polymarket"}
    b_pm = book_b in {"Kalshi", "Polymarket"}
    a_bovada = book_a == "Bovada"
    b_bovada = book_b == "Bovada"

    if a_pm and b_pm:
        return "PM vs PM"
    if (a_pm and b_bovada) or (b_pm and a_bovada):
        return "PM vs Bovada"
    if a_pm or b_pm:
        return "PM vs Sportsbook"
    return "Sportsbook vs Sportsbook"


# ──────────────────────────────────────────────
#  KALSHI AUTH PROBE
# ──────────────────────────────────────────────

async def kalshi_auth_probe():
    """Fire a single signed request to Kalshi /portfolio/balance to verify auth."""
    import prediction_markets as pm
    print("\n" + "═" * 60)
    print("  KALSHI AUTH PROBE")
    print("═" * 60)

    api_key = pm.KALSHI_API_KEY
    if not api_key:
        print("  SKIP — KALSHI_API_KEY not set")
        return

    private_key = pm._load_kalshi_private_key()
    if private_key is None:
        print("  SKIP — private key failed to load (see errors above)")
        return

    path = "/trade-api/v2/portfolio/balance"
    headers = pm._kalshi_auth_headers(api_key, private_key, "GET", path)

    print(f"  API Key:    {api_key[:8]}...{api_key[-4:]}")
    print(f"  Timestamp:  {headers['KALSHI-ACCESS-TIMESTAMP']}")
    print(f"  Sig prefix: {headers['KALSHI-ACCESS-SIGNATURE'][:20]}...")

    import ssl, certifi, aiohttp
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
        url = pm.KALSHI_BASE_URL + path
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                body = await resp.text()
                print(f"  HTTP Status: {resp.status}")
                if resp.status == 200:
                    print("  ✅ AUTH OK — Kalshi accepted the signed request")
                    print(f"  Response: {body[:200]}")
                elif resp.status == 401:
                    print("  ❌ AUTH FAILED — 401 Unauthorized")
                    print("  Check: API key and .pem file are from the SAME Kalshi key pair")
                    print("  Check: .pem file is RSA format (starts with -----BEGIN RSA PRIVATE KEY-----)")
                    print(f"  Response: {body[:200]}")
                elif resp.status == 403:
                    print("  ❌ AUTH FAILED — 403 Forbidden")
                    print("  Key is valid but lacks permission. Check key scopes in Kalshi dashboard.")
                else:
                    print(f"  ⚠️  Unexpected status {resp.status}")
                    print(f"  Response: {body[:200]}")
        except Exception as e:
            print(f"  ❌ Request failed: {e}")


# ──────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────

async def main():
    raw_sports = _ORIG_ARGV[1:] if len(_ORIG_ARGV) > 1 else ["nba"]
    sport_ids, sport_keys = _resolve_sport_args(raw_sports)

    if not sport_ids:
        print("ERROR: No valid sports specified.")
        sys.exit(1)

    print(_SEP)
    print("  PM DEBUG DIAGNOSTIC")
    print(f"  Sports: {sport_keys}")
    print(_SEP)

    # Section 1 — Kalshi
    kalshi_events = await section_1_kalshi(sport_keys)

    # Section 2 — Polymarket mid-prices
    poly_events = await section_2_polymarket(sport_keys)

    # Section 3 — Polymarket CLOB update
    poly_events = await section_3_clob(poly_events)

    # Section 4 — TheRundown (sync call)
    loop = asyncio.get_running_loop()
    rundown_events = await loop.run_in_executor(None, section_4_therundown, sport_ids)

    # Section 5 — Cross-source matching
    consolidated, match_stats = section_5_matching(rundown_events, kalshi_events, poly_events)

    # Section 6 — Arb detection
    arbs, raw_lines = section_6_arbs(consolidated)

    # ── AUTH PROBE ──
    await kalshi_auth_probe()

    # ── DIAGNOSTIC SUMMARY ──
    clob_updated = sum(
        1 for evt in poly_events
        for m in (evt.get("markets") or [])
        if m.get("_clob_updated")
    )
    clob_total = sum(
        1 for evt in poly_events
        for m in (evt.get("markets") or [])
        if m.get("_clob_token")
    )
    pm_vs_pm = sum(1 for a in arbs if _classify_arb_type(a.get("book_a", ""), a.get("book_b", "")) == "PM vs PM")
    pm_vs_sb = sum(1 for a in arbs if _classify_arb_type(a.get("book_a", ""), a.get("book_b", "")) == "PM vs Sportsbook")
    pm_vs_bov = sum(1 for a in arbs if _classify_arb_type(a.get("book_a", ""), a.get("book_b", "")) == "PM vs Bovada")

    print(f"\n{_SEP}")
    print("  DIAGNOSTIC SUMMARY")
    print(f"  Kalshi events:       {len(kalshi_events)}")
    print(f"  Polymarket events:   {len(poly_events)}  (CLOB updated: {clob_updated}/{clob_total})")
    print(f"  TheRundown events:   {len(rundown_events)}")
    print(f"  Matched games:       {match_stats.get('matched', 0)}")
    print(f"  Kalshi unmatched:    {match_stats.get('kalshi_unmatched', 0)}")
    print(f"  Poly unmatched:      {match_stats.get('poly_unmatched', 0)}")
    print(f"  Arbs found:          {len(arbs)}")
    print(f"    PM vs PM:          {pm_vs_pm}")
    print(f"    PM vs Sportsbook:  {pm_vs_sb}")
    print(f"    PM vs Bovada:      {pm_vs_bov}")
    print(_SEP)


if __name__ == "__main__":
    asyncio.run(main())
