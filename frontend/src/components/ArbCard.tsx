import Image from "next/image";
import type { Arb } from "@/lib/types";
import { bookLogoPath } from "@/lib/books";
import { espnTeamLogoUrl, initials, parseMatchup } from "@/lib/teams";
import { buildDeepLink } from "@/lib/deeplinks";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

function formatProfit(p: number): string {
  const sign = p > 0 ? "+" : "";
  return `${sign}${p.toFixed(3)}%`;
}

function formatOddsAm(o: number): string {
  if (typeof o !== "number" || !Number.isFinite(o)) return "—";
  return o > 0 ? `+${o}` : `${o}`;
}

export function ArbCard({ arb }: { arb: Arb }) {
  const { away, home } = parseMatchup(arb.game);
  const league = arb.sport || "NBA";
  const awayLogo = espnTeamLogoUrl(league, away);
  const homeLogo = espnTeamLogoUrl(league, home);

  const linkA = buildDeepLink(arb.book_a, home, arb.url_a);
  const linkB = buildDeepLink(arb.book_b, home, arb.url_b);

  const handlePlaceBets = () => {
    // Open both sportsbook tabs. The first window.open is always allowed
    // within a user gesture handler. The second uses a short delay to avoid
    // browsers treating it as a blocked popup.
    window.open(linkA, "_blank", "noopener,noreferrer");
    setTimeout(() => {
      window.open(linkB, "_blank", "noopener,noreferrer");
    }, 100);
  };

  return (
    <Card className="rounded-xl border border-subtle bg-surface shadow-card transition-all duration-200 hover:shadow-card-hover hover:border-emphasis">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-4">
        <div className="flex items-center gap-3">
          <Badge variant="neutral">{arb.sport}</Badge>
          {arb.arb_source === "live" ? (
            <Badge variant="success" className="text-[10px]">Live</Badge>
          ) : arb.arb_source === "combined" ? (
            <Badge variant="info" className="text-[10px]">Mixed</Badge>
          ) : arb.arb_source === "rundown" ? (
            <Badge variant="neutral" className="text-[10px]">Rundown</Badge>
          ) : null}
          <span className="text-sm text-secondary">
            {arb.market_kind === "ml"
              ? "Moneyline"
              : arb.market_kind === "spread"
                ? `Spread ${arb.line_label ?? ""}`
                : arb.market_kind === "total"
                  ? `Total ${arb.line_label ?? ""}`
                  : arb.market_kind}
          </span>
        </div>

        <div className="flex items-center gap-3">
          <div className="text-right">
            <div className="text-xs text-secondary">Profit</div>
            <div className="text-lg font-bold tracking-tight text-accent-green">
              {formatProfit(arb.profit)}
            </div>
          </div>
        </div>
      </div>

      {/* Matchup row */}
      <div className="px-5 pt-4">
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="relative h-9 w-9 overflow-hidden rounded-full border border-border bg-surface">
              {awayLogo ? (
                <Image src={awayLogo} alt={away} fill sizes="36px" />
              ) : (
                <div className="grid h-full w-full place-items-center text-xs font-bold text-secondary">
                  {initials(away)}
                </div>
              )}
            </div>
            <div>
              <div className="text-sm font-semibold leading-tight text-primary">{away}</div>
              <div className="text-xs text-secondary">@</div>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <div>
              <div className="text-sm font-semibold leading-tight text-primary text-right">{home}</div>
              <div className="text-xs text-secondary text-right">Home</div>
            </div>
            <div className="relative h-9 w-9 overflow-hidden rounded-full border border-border bg-surface">
              {homeLogo ? (
                <Image src={homeLogo} alt={home} fill sizes="36px" />
              ) : (
                <div className="grid h-full w-full place-items-center text-xs font-bold text-secondary">
                  {initials(home)}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Legs + Place Bets button */}
      <div className="flex gap-3 px-5 py-4">
        {/* Two legs side by side */}
        <div className="flex-1 grid grid-cols-1 gap-3 md:grid-cols-2">
          <LegBlock
            label="Leg A"
            side={arb.side_a}
            book={arb.book_a}
            odds={arb.odds_a_am}
            stake={arb.stake_a}
            game={arb.game}
            league={league}
            url={arb.url_a}
          />
          <LegBlock
            label="Leg B"
            side={arb.side_b}
            book={arb.book_b}
            odds={arb.odds_b_am}
            stake={arb.stake_b}
            game={arb.game}
            league={league}
            url={arb.url_b}
            danger={arb.same_book === true}
          />
        </div>

        {/* Place Bets button spanning full height of both legs */}
        <button
          type="button"
          onClick={handlePlaceBets}
          className="flex flex-col items-center justify-center gap-2 rounded-lg bg-sky-500/15 border border-sky-500/30 px-3 min-w-[52px] text-sky-400 hover:bg-sky-500/25 hover:border-sky-400/50 transition-all cursor-pointer group"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 20 20"
            fill="currentColor"
            className="h-5 w-5 group-hover:scale-110 transition-transform"
          >
            <path
              fillRule="evenodd"
              d="M4.25 5.5a.75.75 0 0 0-.75.75v8.5c0 .414.336.75.75.75h8.5a.75.75 0 0 0 .75-.75v-4a.75.75 0 0 1 1.5 0v4A2.25 2.25 0 0 1 12.75 17h-8.5A2.25 2.25 0 0 1 2 14.75v-8.5A2.25 2.25 0 0 1 4.25 4h5a.75.75 0 0 1 0 1.5h-5Z"
              clipRule="evenodd"
            />
            <path
              fillRule="evenodd"
              d="M6.194 12.753a.75.75 0 0 0 1.06.053L16.5 4.44v2.81a.75.75 0 0 0 1.5 0v-4.5a.75.75 0 0 0-.75-.75h-4.5a.75.75 0 0 0 0 1.5h2.553l-9.056 8.194a.75.75 0 0 0-.053 1.06Z"
              clipRule="evenodd"
            />
          </svg>
          <span className="text-[10px] font-bold uppercase tracking-wider [writing-mode:vertical-lr] rotate-180">
            Place Bets
          </span>
        </button>
      </div>

      {arb.same_book ? (
        <div className="px-5 pb-4 text-xs text-accent-amber">
          Note: both legs are from the same book. Treat as low-confidence / likely unbettable.
        </div>
      ) : null}
    </Card>
  );
}

function LegBlock(props: {
  label: string;
  side: string;
  book: string;
  odds: number;
  stake: number;
  game: string;
  league: string;
  url?: string;
  danger?: boolean;
}) {
  const logo = bookLogoPath(props.book);
  const { home, away } = parseMatchup(props.game);
  const deepLink = buildDeepLink(props.book, home, props.url);

  const sideLower = props.side.toLowerCase();
  const teamName = sideLower.includes("home") ? home : sideLower.includes("away") ? away : props.side;
  const teamLogo = espnTeamLogoUrl(props.league, teamName);

  const oddsClass =
    props.odds > 0
      ? "tabular-nums text-[#34D399]"
      : props.odds < 0
        ? "tabular-nums text-[#F87171]"
        : "tabular-nums text-secondary";

  return (
    <div
      className={[
        "rounded-lg border border-subtle bg-surface p-4",
        props.danger ? "border-accent-amber/60" : "border-border",
      ].join(" ")}
    >
      {/* Top: team + odds */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <div className="relative h-8 w-8 shrink-0 overflow-hidden rounded-full border border-border bg-surface">
            {teamLogo ? (
              <Image src={teamLogo} alt={teamName} fill sizes="32px" />
            ) : (
              <div className="grid h-full w-full place-items-center text-[10px] font-bold text-secondary">
                {initials(teamName)}
              </div>
            )}
          </div>
          <div>
            <div className="text-xs font-semibold text-secondary">{props.label}</div>
            <div className="mt-0.5 text-sm font-bold text-primary">{teamName}</div>
          </div>
        </div>
        <div className={`text-xl font-extrabold font-mono tracking-tight ${oddsClass}`}>
          {formatOddsAm(props.odds)}
        </div>
      </div>

      {/* Bottom: book logo + stake */}
      <div className="mt-3 flex items-center justify-between">
        <a
          href={deepLink}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-2 group"
        >
          <div className="relative h-6 w-6 overflow-hidden rounded border border-border bg-surface group-hover:ring-2 group-hover:ring-emphasis/30 transition">
            <Image src={logo} alt={props.book} fill sizes="24px" />
          </div>
          <span className="text-xs text-secondary group-hover:text-primary transition">
            {props.book}
          </span>
        </a>
        <div className="text-right">
          <div className="text-[11px] text-secondary">Stake</div>
          <div className="text-sm font-semibold text-primary font-mono tracking-tight">
            ${props.stake.toFixed(2)}
          </div>
        </div>
      </div>
    </div>
  );
}
