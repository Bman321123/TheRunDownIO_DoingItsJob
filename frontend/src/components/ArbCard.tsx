import Image from "next/image";
import type { Arb } from "@/lib/types";
import { bookLogoPath } from "@/lib/books";
import { espnTeamLogoUrl, initials, parseMatchup } from "@/lib/teams";
import { buildDeepLink } from "@/lib/deeplinks";
import { Card } from "@/components/ui/card";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
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

  return (
    <Card className="rounded-xl border border-subtle bg-surface shadow-card transition-all duration-200 hover:shadow-card-hover hover:border-emphasis">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-4">
        <div className="flex items-center gap-3">
          <Badge variant="neutral">{arb.sport}</Badge>
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

      <div className="px-5 py-4">
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

        <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2">
          <LegBlock
            label="Leg A"
            side={arb.side_a}
            book={arb.book_a}
            odds={arb.odds_a_am}
            stake={arb.stake_a}
            game={arb.game}
          />
          <LegBlock
            label="Leg B"
            side={arb.side_b}
            book={arb.book_b}
            odds={arb.odds_b_am}
            stake={arb.stake_b}
            game={arb.game}
            danger={arb.same_book === true}
          />
        </div>

        {arb.same_book ? (
          <div className="mt-3 text-xs text-accent-amber">
            Note: both legs are from the same book. Treat as low-confidence / likely unbettable.
          </div>
        ) : null}
      </div>
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
  danger?: boolean;
}) {
  const logo = bookLogoPath(props.book);
  const { home } = parseMatchup(props.game);
  const deepLink = buildDeepLink(props.book, home);

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
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-xs font-semibold text-secondary">{props.label}</div>
          <div className="mt-0.5 text-sm font-semibold text-primary">{props.side}</div>
          <a
            href={deepLink}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-1 inline-flex items-center gap-1.5 text-xs text-secondary hover:text-primary transition group"
          >
            {props.book}
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 16 16"
              fill="currentColor"
              className="h-3 w-3 opacity-0 group-hover:opacity-100 transition-opacity"
            >
              <path
                fillRule="evenodd"
                d="M4.22 11.78a.75.75 0 0 1 0-1.06L9.44 5.5H5.75a.75.75 0 0 1 0-1.5h5.5a.75.75 0 0 1 .75.75v5.5a.75.75 0 0 1-1.5 0V6.56l-5.22 5.22a.75.75 0 0 1-1.06 0Z"
                clipRule="evenodd"
              />
            </svg>
          </a>
        </div>

        <Tooltip>
          <TooltipTrigger asChild>
            <a href={deepLink} target="_blank" rel="noopener noreferrer" aria-label={`Open ${props.book}`}>
              <div className="relative h-8 w-8 overflow-hidden rounded border border-border bg-surface hover:ring-2 hover:ring-emphasis/30 transition">
                <Image src={logo} alt={props.book} fill sizes="32px" />
              </div>
            </a>
          </TooltipTrigger>
          <TooltipContent side="top">
            Open {props.book}
          </TooltipContent>
        </Tooltip>
      </div>

      <div className="mt-3 flex items-end justify-between">
        <div className={`text-2xl font-extrabold font-mono tracking-tight ${oddsClass}`}>
          {formatOddsAm(props.odds)}
        </div>
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
