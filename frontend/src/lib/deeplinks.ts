import { normalizeBookName, type BookKey } from "./books";

/**
 * Build a deep link URL for a sportsbook.
 *
 * Priority:
 *   1. Direct event URL from backend (bovada/kalshi/polymarket scrapers)
 *   2. Search-based URL using team name (DraftKings, FanDuel, BetMGM)
 *   3. Sport-level fallback for books without search
 */
export function buildDeepLink(
  bookName: string,
  teamName: string,
  directUrl?: string,
): string {
  // If we have a direct event URL from the scraper, use it
  if (directUrl) return directUrl;

  const key: BookKey = normalizeBookName(bookName);
  const q = encodeURIComponent(teamName.trim());

  switch (key) {
    case "draftkings":
      return `https://sportsbook.draftkings.com/search/${q}`;
    case "fanduel":
      return `https://sportsbook.fanduel.com/search?query=${q}`;
    case "betmgm":
      return `https://sports.betmgm.com/en/sports?q=${q}`;
    case "kalshi":
      return "https://kalshi.com/browse/sports";
    case "polymarket":
      return "https://polymarket.com/sports";
    case "bovada":
      return "https://www.bovada.lv/sports";
    case "novig":
      return "https://novig.com/events";
    default:
      return "#";
  }
}
