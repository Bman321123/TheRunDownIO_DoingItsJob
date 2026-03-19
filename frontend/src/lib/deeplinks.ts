import { normalizeBookName, type BookKey } from "./books";

/**
 * Build a deep link URL to a sportsbook's search page for a given team.
 * Falls back to the sportsbook's homepage if the book is unknown.
 */
export function buildDeepLink(bookName: string, teamName: string): string {
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
    default:
      return "#";
  }
}
