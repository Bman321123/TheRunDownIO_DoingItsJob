export type BookKey = "draftkings" | "fanduel" | "betmgm" | "kalshi" | "polymarket" | "bovada" | "pinnacle" | "novig" | "og" | "unknown";

export function normalizeBookName(name: string): BookKey {
  const n = (name || "").toLowerCase().replace(/\s+/g, "");
  if (n.includes("draft")) return "draftkings";
  if (n.includes("fan")) return "fanduel";
  if (n.includes("betmgm") || n.includes("mgm")) return "betmgm";
  if (n.includes("kalshi")) return "kalshi";
  if (n.includes("polymarket")) return "polymarket";
  if (n.includes("bovada") || n.includes("bodog")) return "bovada";
  if (n.includes("pinnacle")) return "pinnacle";
  if (n.includes("novig")) return "novig";
  if (n === "og") return "og";
  return "unknown";
}

export function bookLogoPath(bookName: string): string {
  const key = normalizeBookName(bookName);
  if (key === "draftkings") return "/books/draftkings.svg";
  if (key === "fanduel") return "/books/fanduel.svg";
  if (key === "betmgm") return "/books/betmgm.svg";
  if (key === "kalshi") return "/books/kalshi.svg";
  if (key === "polymarket") return "/books/polymarket.svg";
  if (key === "bovada") return "/books/bovada.svg";
  if (key === "pinnacle") return "/books/pinnacle.svg";
  if (key === "novig") return "/books/novig.svg";
  if (key === "og") return "/books/og.svg";
  return "/books/book.svg";
}

