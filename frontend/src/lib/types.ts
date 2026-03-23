export type Arb = {
  sport: string;
  game: string;
  market_kind: "ml" | "spread" | "total" | string;
  line_label?: string;

  side_a: string;
  book_a: string;
  odds_a_am: number;
  updated_at_a?: string | number | null;
  url_a?: string;

  side_b: string;
  book_b: string;
  odds_b_am: number;
  updated_at_b?: string | number | null;
  url_b?: string;

  profit: number;
  stake_a: number;
  stake_b: number;

  same_book?: boolean;
  fresh_age_s?: number | null;
  stale_age_s?: number | null;
  arb_source?: "live" | "combined" | "rundown";
};

export type RawLine = {
  sport: string;
  game: string;
  market_kind: "ml" | "spread" | "total" | string;
  line_label?: string;
  side: string;
  book: string;
  odds_am: number;
  updated_at?: string | number | null;
  url?: string;
};

export type BestLine = {
  type?: "moneyline" | "spread" | "total" | string;
  sport: string;
  game: string;
  line?: number | null;
  player?: string;
  prop_type?: "player_points" | "player_rebounds" | "player_assists" | string;
  home_team?: string;
  away_team?: string;
  home?: {
    book: string;
    odds_am: number;
    url?: string;
  };
  away?: {
    book: string;
    odds_am: number;
    url?: string;
  };
  over?: {
    book: string;
    odds_am: number;
    url?: string;
  };
  under?: {
    book: string;
    odds_am: number;
    url?: string;
  };
  side?: "home" | "away" | string;
  team?: string;
  pick?: {
    book: string;
    odds_am: number;
    url?: string;
  };
};

export type MatchedMarket = {
  market_type?: string;
  selection?: string;
  team?: string;
  american_odds?: number;
  decimal_odds?: number;
  line_value?: number | null;
};

export type MatchedGame = {
  sport: string;
  start_time?: string;
  home_team: string;
  away_team: string;
  matchScore?: number;
  books: {
    bovada: MatchedMarket[];
    therundown: Record<string, unknown>[];
  };
};

export type ScanNowResponse = {
  ok: boolean;
  sports?: string[];
  lastScanMs?: number;
  count?: number;
  arbs?: Arb[];
  lines?: RawLine[];
  bestLines?: BestLine[];
  matchedGames?: number;
  sourceCounts?: {
    therundown: number;
    bovada: number;
    kalshi: number;
    polymarket: number;
    novig: number;
    og: number;
  };
  bovadaError?: string | null;
  pmError?: string | null;
  novigError?: string | null;
  ogError?: string | null;
  error?: string;
  dataAge?: number;
  dpRemaining?: string;
};

export type ScanStreamEvent = {
  phase: "fast" | "complete";
  arbs?: Arb[];
  lines?: RawLine[];
  bestLines?: BestLine[];
  sourceCounts?: Record<string, number>;
  lastScanMs?: number;
  matchedGames?: number;
  bovadaError?: string | null;
  pmError?: string | null;
  novigError?: string | null;
  ogError?: string | null;
  dataAge?: number;
  dpRemaining?: string;
};
