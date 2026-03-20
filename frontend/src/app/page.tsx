"use client";

import { useEffect, useMemo, useState } from "react";
import type { Arb, RawLine, BestLine, ScanNowResponse } from "@/lib/types";
import { ArbCard } from "@/components/ArbCard";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { UnderlineTabs } from "@/components/ui/underline-tabs";
import { AnimatedTextReveal } from "@/components/ui/animated-text-reveal";
import { Badge } from "@/components/ui/badge";

const DEFAULT_BASE = "http://127.0.0.1:3030";

const SPORTS = ["NCAAF", "NFL", "MLB", "NBA", "NCAAB", "NHL", "NCAAWB", "MMA"] as const;
type Sport = (typeof SPORTS)[number];

const SCAN_LABELS = ["Scanning lines…", "Fetching odds…", "Matching games…", "Calculating arbs…"] as const;
export default function HomePage() {
  const baseUrl = process.env.NEXT_PUBLIC_ARBS_URL || DEFAULT_BASE;
  const arbsUrl = `${baseUrl.replace(/\/$/, "")}/arbs`;
  const scanUrl = `${baseUrl.replace(/\/$/, "")}/scan-now`;

  const [selectedSports, setSelectedSports] = useState<Set<Sport>>(() => new Set(SPORTS));
  const [arbs, setArbs] = useState<Arb[]>([]);
  const [lines, setLines] = useState<RawLine[]>([]);
  const [bestLines, setBestLines] = useState<BestLine[]>([]);
  const [status, setStatus] = useState<"idle" | "loading" | "ok" | "error">("idle");
  const [scanStatus, setScanStatus] = useState<"idle" | "scanning" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [lastScannedMs, setLastScannedMs] = useState<number | null>(null);
  const [dataAge, setDataAge] = useState<number | null>(null);
  const [dpRemaining, setDpRemaining] = useState<string | null>(null);
  // Intentionally no "arb entrance" stagger animation (handled by PRD UI fixes).

  const [activeTab, setActiveTab] = useState<"arbs" | "lines" | "best">("arbs");
  const [displayTab, setDisplayTab] = useState<"arbs" | "lines" | "best">(activeTab);
  const [tabKey, setTabKey] = useState(0);
  const [expandedBooks, setExpandedBooks] = useState<Set<string>>(new Set());
  const [openDrawers, setOpenDrawers] = useState<Set<string>>(new Set());

  const toggleDrawer = (drawerKey: string) =>
    setOpenDrawers((prev) => {
      const next = new Set(prev);
      next.has(drawerKey) ? next.delete(drawerKey) : next.add(drawerKey);
      return next;
    });

  const count = arbs.length;
  const header = useMemo(() => {
    if (status === "loading") return "Loading…";
    if (status === "error") return "Disconnected";
    return "Cached";
  }, [status]);

  // Initial load: fetch cached arbs once
  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        setStatus("loading");
        const resp = await fetch(arbsUrl, { cache: "no-store" });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        if (!mounted) return;
        setArbs(Array.isArray(data.arbs) ? data.arbs : Array.isArray(data) ? data : []);
        setLines(Array.isArray(data.lines) ? data.lines : []);
        setBestLines(Array.isArray(data.bestLines) ? data.bestLines : []);
        if (typeof data.dataAge === "number") setDataAge(data.dataAge);
        if (data.dpRemaining) setDpRemaining(data.dpRemaining);
        setStatus("ok");
        setError(null);
      } catch (e: any) {
        if (!mounted) return;
        setStatus("error");
        setError(e?.message || "Failed to load");
      }
    })();
    return () => {
      mounted = false;
    };
  }, [arbsUrl]);

  useEffect(() => {
    if (activeTab === displayTab) return;

    // Tiny delay so the fade-out of old content isn't jarring.
    const timer = window.setTimeout(() => {
      setDisplayTab(activeTab);
      setTabKey((k) => k + 1);
    }, 80);

    return () => window.clearTimeout(timer);
  }, [activeTab, displayTab]);

  async function scanNow() {
    const sports = Array.from(selectedSports);
    if (sports.length === 0) return;
    try {
      setScanStatus("scanning");
      setError(null);
      const resp = await fetch(scanUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sports }),
      });
      const payload = (await resp.json()) as ScanNowResponse;
      if (!resp.ok || !payload?.ok) {
        throw new Error(payload?.error || `HTTP ${resp.status}`);
      }
      setArbs(Array.isArray(payload.arbs) ? payload.arbs : []);
      setLines(Array.isArray(payload.lines) ? payload.lines : []);
      setBestLines(Array.isArray(payload.bestLines) ? payload.bestLines : []);
      setLastScannedMs(payload.lastScanMs ?? Date.now());
      if (typeof payload.dataAge === "number") setDataAge(payload.dataAge);
      if (payload.dpRemaining) setDpRemaining(payload.dpRemaining);
      setScanStatus("idle");
    } catch (e: any) {
      setScanStatus("error");
      setError(e?.message || "Scan failed");
    }
  }

  return (
    <main className="space-y-5">
      <header className="flex flex-col gap-4 rounded-xl border border-subtle bg-raised px-6 py-5 shadow-card transition-all duration-200 hover:shadow-card-hover hover:border-emphasis">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-2xl font-semibold tracking-tight text-primary">
              <AnimatedTextReveal text="Arbitrage Dashboard" />
            </div>
            <div className="mt-1 text-sm text-secondary">
              Scan and compare book lines to surface both-sides value.{" "}
              <span className="font-mono tracking-tight text-muted-text">{baseUrl}</span>
            </div>
          </div>

          <div className="flex items-center gap-4">
            {dataAge !== null && (
              <div className="text-right">
                <div className="text-xs text-[#9898B8]">Data Age</div>
                <div className={[
                  "text-sm font-semibold",
                  dataAge > 300 ? "text-red-700" : dataAge > 120 ? "text-amber-700" : "text-emerald-700",
                ].join(" ")}>
                  {dataAge < 60 ? `${dataAge}s` : `${Math.floor(dataAge / 60)}m ${dataAge % 60}s`}
                </div>
              </div>
            )}
            {dpRemaining && (
              <div className="text-right">
                <div className="text-xs text-[#9898B8]">DP Left</div>
                <div className="text-sm font-semibold text-[#E8E8F8]">{dpRemaining}</div>
              </div>
            )}
            <div className="text-right">
              <div className="text-xs text-[#9898B8]">Status</div>
              <div className="text-sm font-semibold text-[#E8E8F8]">{header}</div>
            </div>
          </div>
        </div>

        {dataAge !== null && dataAge > 300 && (
          <div className="rounded-lg bg-amber-500/10 border border-amber-500/25 px-3 py-2 text-xs text-amber-200">
            Data is {Math.floor(dataAge / 60)}+ minutes old. Odds may have changed. Click &quot;Scan Lines&quot; to refresh.
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2">
          {SPORTS.map((s) => {
            const active = selectedSports.has(s);
            return (
              <button
                key={s}
                type="button"
                onClick={() => {
                  setSelectedSports((prev) => {
                    const next = new Set(prev);
                    if (next.has(s)) next.delete(s);
                    else next.add(s);
                    return next;
                  });
                }}
                className={[
                  "rounded-pill px-3 py-1 text-xs font-semibold transition-colors duration-150 ring-1",
                  active
                    ? "bg-emerald-500/20 text-emerald-300 ring-emerald-400/50"
                    : "bg-white/[0.05] text-[#9898B8] ring-white/[0.10] hover:text-[#D4D4E8] hover:bg-white/[0.08]",
                ].join(" ")}
              >
                {s}
              </button>
            );
          })}
          <div className="flex-1" />
          <Button
            type="button"
            variant="scan"
            loading={scanStatus === "scanning"}
            disabled={selectedSports.size === 0}
            scanLabels={Array.from(SCAN_LABELS)}
            onClick={scanNow}
          >
            Scan Lines
          </Button>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-xs text-[#9898B8]">
            Opportunities: <span className="font-semibold text-[#E8E8F8]">{count}</span>
          </div>
          <div className="text-xs text-[#9898B8]">
            Last scanned:{" "}
            <span className="font-semibold text-[#E8E8F8]">
              {lastScannedMs ? new Date(lastScannedMs).toLocaleTimeString() : "—"}
            </span>
          </div>
        </div>

        {status === "error" || scanStatus === "error" ? (
          <div className="text-xs text-[#FCA5A5]">
            Error: {error}. Make sure `/Users/seniortech/therundownioV1/server.py` is running on port 3030.
          </div>
        ) : null}

        <UnderlineTabs
          className="pt-2"
          value={activeTab}
          onValueChange={setActiveTab}
          tabs={[
            { value: "arbs", label: "Arbitrage Opportunities" },
            { value: "lines", label: "Raw Book Lines" },
            { value: "best", label: "Best Lines (Odds Shopping)" },
          ]}
        />
      </header>

      <div>
        <div key={tabKey} className="tab-fade-in">
        {displayTab === "arbs" && (
          <section className="grid grid-cols-1 gap-5">
            {arbs.map((arb, idx) => (
              <ArbCard key={`${arb.game}-${arb.market_kind}-${arb.line_label ?? ""}-${idx}`} arb={arb} />
            ))}
          </section>
        )}

        {displayTab === "lines" && (
          <section className="space-y-4">
          {Array.from(new Set(lines.map((l) => l.book)))
            .sort()
            .map((bookName) => {
              const isExpanded = expandedBooks.has(bookName);
              const bookLines = lines.filter((l) => l.book === bookName);
              return (
                <Card
                  key={bookName}
                  className="overflow-hidden rounded-xl border border-subtle bg-surface shadow-card hover:shadow-card-hover hover:border-emphasis transition-all duration-200"
                >
                  <button
                    onClick={() =>
                      setExpandedBooks((prev) => {
                        const next = new Set(prev);
                        if (next.has(bookName)) next.delete(bookName);
                        else next.add(bookName);
                        return next;
                      })
                    }
                    className="w-full flex items-center justify-between px-5 py-4 bg-surface hover:bg-overlay/40 transition"
                  >
                    <div className="flex items-center gap-3 text-primary font-semibold">
                      <span className="text-lg">{bookName}</span>
                      <span className="text-xs font-mono tracking-tight text-muted-text bg-overlay/40 px-2 py-0.5 rounded-pill border border-border">
                        {bookLines.length} lines pulled
                      </span>
                    </div>
                    <div className="text-secondary">{isExpanded ? "▲ Hide" : "▼ Show"}</div>
                  </button>
                  {isExpanded && (
                    <div className="p-5 border-t border-border drawer-open">
                      <table className="w-full text-left text-sm text-[#D4D4E8]">
                        <thead>
                          <tr className="border-b border-subtle text-[#9898B8]">
                            <th className="font-medium pb-2">Time</th>
                            <th className="font-medium pb-2">Sport</th>
                            <th className="font-medium pb-2">Game</th>
                            <th className="font-medium pb-2">Market</th>
                            <th className="font-medium pb-2">Side</th>
                            <th className="font-medium text-right pb-2">American Odds</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-border/70">
                          {bookLines.map((l, i) => (
                            <tr key={i} className="hover:bg-overlay/40 transition group">
                              <td className="py-2.5 px-2 text-xs font-mono tracking-tight text-[#D4D4E8] whitespace-nowrap">
                                {l.updated_at ? new Date(l.updated_at).toLocaleTimeString() : "N/A"}
                              </td>
                              <td className="py-2.5 px-2 font-semibold text-[#D4D4E8]">{l.sport}</td>
                              <td className="py-2.5 px-2 text-[#D4D4E8]">{l.game}</td>
                              <td className="py-2.5 px-2">
                                <span className="inline-flex bg-surface rounded px-1.5 py-0.5 text-xs text-[#D4D4E8] capitalize border border-border">
                                  {l.market_kind} {l.line_label}
                                </span>
                              </td>
                              <td className="py-2.5 px-2 font-medium">{l.side}</td>
                              <td
                                className={`py-2.5 px-2 text-right font-bold tabular-nums ${
                                  l.odds_am > 0 ? "text-[#34D399]" : "text-[#F87171]"
                                }`}
                              >
                                {l.odds_am > 0 ? `+${l.odds_am}` : l.odds_am}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </Card>
              );
            })}
        </section>
      )}

      {displayTab === "best" && (
        <section className="space-y-4">
          {bestLines.length === 0 ? (
            <Card className="rounded-lg border border-subtle bg-surface px-5 py-4 text-sm text-secondary shadow-card">
              No eligible markets found for the current scan. Try scanning again or adjusting sports selection.
            </Card>
          ) : (
            Array.from(
              bestLines.reduce((acc, bl) => {
                const key = `${bl.sport}::${bl.game}`;
                const list = acc.get(key) ?? [];
                list.push(bl);
                acc.set(key, list);
                return acc;
              }, new Map<string, BestLine[]>())
            )
              .map(([key, linesForEvent]) => {
                return { key, linesForEvent };
              })
              .sort((a, b) => a.key.localeCompare(b.key))
              .map(({ key, linesForEvent }) => {
              const [sport, game] = key.split("::");
              const ml = linesForEvent.filter((l) => l.type === "moneyline");
              const spreads = linesForEvent.filter((l) => l.type === "spread");
              const totals = linesForEvent.filter((l) => l.type === "total");
              const props = linesForEvent.filter((l) => l.type === "prop");

              const spreadKey = `spread::${key}`;
              const totalKey = `total::${key}`;
              const propKey = `prop::${key}`;
              const spreadsOpen = openDrawers.has(spreadKey);
              const totalsOpen = openDrawers.has(totalKey);
              const propsOpen = openDrawers.has(propKey);

              const shortBook = (name: string) => {
                const lower = name.toLowerCase();
                if (lower.includes("fanduel")) return "FD";
                if (lower.includes("draftk")) return "DK";
                if (lower.includes("betmgm") || lower.includes("mgm")) return "BMG";
                if (lower.includes("kalshi")) return "Kalshi";
                if (lower.includes("polymarket")) return "PM";
                if (lower.includes("bovada")) return "Bovada";
                if (lower.includes("hard rock") || lower.includes("hardrock")) return "HRB";
                return name;
              };
              const formatOdds = (v: number | undefined | null) => {
                if (v == null) return "—";
                return v > 0 ? `+${v}` : `${v}`;
              };
              const oddsColor = (v: number | undefined | null) =>
                v == null
                  ? "text-secondary tabular-nums"
                  : v > 0
                    ? "font-bold tabular-nums text-[#34D399]"
                    : "font-bold tabular-nums text-[#F87171]";

              const amToDec = (am: number) =>
                am >= 100 ? am / 100 + 1 : am <= -100 ? 100 / Math.abs(am) + 1 : 0;

              const pairValue = (oddsA: number, oddsB: number) => {
                const decA = amToDec(oddsA);
                const decB = amToDec(oddsB);
                if (decA <= 1 || decB <= 1) return null;
                return Math.round((1 - (1 / decA + 1 / decB)) * 10000) / 100;
              };

              const valueBadge = (val: number | null) => {
                if (val == null) return null;
                const positive = val >= 0;
                return (
                  <Badge
                    variant={positive ? "success" : "danger"}
                    className="font-mono text-[11px] font-bold"
                  >
                    {positive ? "+" : ""}
                    {val.toFixed(1)}%
                  </Badge>
                );
              };
              const homeName = ml[0]?.home_team ?? spreads.find((s) => s.side === "home")?.team ?? "Home";
              const awayName = ml[0]?.away_team ?? spreads.find((s) => s.side === "away")?.team ?? "Away";

              const spreadPairs = (() => {
                const byAbs = new Map<number, BestLine[]>();
                for (const s of spreads) {
                  if (s.line == null || !s.pick?.book || !s.side) continue;
                  const abs = Math.abs(s.line);
                  const list = byAbs.get(abs) ?? [];
                  list.push(s);
                  byAbs.set(abs, list);
                }
                return Array.from(byAbs.entries()).sort(([absA], [absB]) => absA - absB);
              })();

              return (
                <Card
                  key={key}
                  className="overflow-hidden rounded-xl border border-subtle bg-surface shadow-card"
                >
                  <div className="px-5 py-4">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <div className="text-xs uppercase tracking-wide text-secondary">{sport}</div>
                        <div className="text-base font-semibold text-primary">{game}</div>
                      </div>
                    </div>

                    {ml.length > 0 && ml.map((bl, idx) => {
                      if (!bl.home || !bl.away || !bl.home_team || !bl.away_team) return null;
                      if (bl.home.odds_am == null || bl.away.odds_am == null) return null;
                      const mlVal = pairValue(bl.home.odds_am, bl.away.odds_am);
                      return (
                        <div key={`ml-${idx}`} className="mt-3">
                          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                            <div className="flex items-center justify-between rounded-xl bg-overlay px-4 py-3">
                              <div>
                                <div className="text-[10px] uppercase tracking-wider text-secondary">Home</div>
                                <div className="text-sm font-bold text-primary">{bl.home_team}</div>
                              </div>
                              <div className="flex items-center gap-2">
                                <span className={`font-mono text-lg font-bold ${oddsColor(bl.home.odds_am)}`}>
                                  {formatOdds(bl.home.odds_am)}
                                </span>
                                <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-semibold text-primary">
                                  {shortBook(bl.home.book)}
                                </span>
                              </div>
                            </div>
                            <div className="flex items-center justify-between rounded-xl bg-overlay px-4 py-3">
                              <div>
                                <div className="text-[10px] uppercase tracking-wider text-secondary">Away</div>
                                <div className="text-sm font-bold text-primary">{bl.away_team}</div>
                              </div>
                              <div className="flex items-center gap-2">
                                <span className={`font-mono text-lg font-bold ${oddsColor(bl.away.odds_am)}`}>
                                  {formatOdds(bl.away.odds_am)}
                                </span>
                                <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-semibold text-primary">
                                  {shortBook(bl.away.book)}
                                </span>
                              </div>
                            </div>
                          </div>
                          {!spreadsOpen && !totalsOpen && mlVal != null && (
                            <div className="mt-1.5 flex items-center gap-2 text-[10px] text-muted-text">
                              <span>Both-sides value:</span> {valueBadge(mlVal)}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>

                  {(spreads.length > 0 || totals.length > 0 || props.length > 0) && (
                    <div className="flex border-t border-border">
                      {spreads.length > 0 && (
                        <button
                          type="button"
                          onClick={() => toggleDrawer(spreadKey)}
                          className={[
                            "flex-1 flex items-center justify-center gap-2 px-4 py-2.5 text-xs font-semibold transition",
                            spreadsOpen
                              ? "bg-overlay text-accent-green"
                              : "bg-surface text-secondary hover:text-primary hover:bg-overlay",
                          ].join(" ")}
                        >
                          <span>{spreadsOpen ? "▾" : "▸"}</span>
                          Spreads
                          <span className="font-mono text-[10px] text-zinc-500">({spreadPairs.length})</span>
                        </button>
                      )}
                      {totals.length > 0 && (
                        <button
                          type="button"
                          onClick={() => toggleDrawer(totalKey)}
                          className={[
                            "flex-1 flex items-center justify-center gap-2 px-4 py-2.5 text-xs font-semibold transition",
                            spreads.length > 0 ? "border-l border-border" : "",
                            totalsOpen
                              ? "bg-overlay text-accent-green"
                              : "bg-surface text-secondary hover:text-primary hover:bg-overlay",
                          ].join(" ")}
                        >
                          <span>{totalsOpen ? "▾" : "▸"}</span>
                          Totals
                          <span className="font-mono text-[10px] text-zinc-500">({totals.length})</span>
                        </button>
                      )}
                      {props.length > 0 && (
                        <button
                          type="button"
                          onClick={() => toggleDrawer(propKey)}
                          className={[
                            "flex-1 flex items-center justify-center gap-2 px-4 py-2.5 text-xs font-semibold transition",
                            spreads.length > 0 || totals.length > 0 ? "border-l border-border" : "",
                            propsOpen
                              ? "bg-overlay text-accent-green"
                              : "bg-surface text-secondary hover:text-primary hover:bg-overlay",
                          ].join(" ")}
                        >
                          <span>{propsOpen ? "▾" : "▸"}</span>
                          Props
                          <span className="font-mono text-[10px] text-zinc-500">({props.length})</span>
                        </button>
                      )}
                    </div>
                  )}

                  {spreadsOpen && spreadPairs.length > 0 && (
                    <div className="border-t border-border px-5 py-3 space-y-3">
                      {spreadPairs.map(([absLine, entries]) => {
                        const homeNeg = entries.find((e) => e.side === "home" && (e.line ?? 0) < 0);
                        const awayPos = entries.find((e) => e.side === "away" && (e.line ?? 0) > 0);
                        const homePos = entries.find((e) => e.side === "home" && (e.line ?? 0) > 0);
                        const awayNeg = entries.find((e) => e.side === "away" && (e.line ?? 0) < 0);

                        const pairA = homeNeg && awayPos;
                        const pairB = homePos && awayNeg;
                        if (!pairA && !pairB) return null;

                        const valA = pairA ? pairValue(homeNeg!.pick!.odds_am, awayPos!.pick!.odds_am) : null;
                        const valB = pairB ? pairValue(homePos!.pick!.odds_am, awayNeg!.pick!.odds_am) : null;
                        const showA = valA != null && (valB == null || valA >= valB);
                        const showB = valB != null && (valA == null || valB > valA);
                        return (
                          <div key={`sp-${absLine}`} className="space-y-1.5">
                              <div className="text-[10px] uppercase tracking-wider text-secondary font-semibold">
                              Spread {absLine}
                            </div>

                            {pairA && (
                              <div>
                                <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                                  <div className="flex items-center justify-between rounded-lg bg-overlay px-3 py-1.5">
                                    <span className="text-xs text-primary">
                                      {homeName}{" "}
                                      <span className={`font-mono ${oddsColor(homeNeg!.line)}`}>
                                        {formatOdds(homeNeg!.line)}
                                      </span>
                                    </span>
                                    <div className="flex items-center gap-1.5">
                                      <span className={`font-mono text-sm font-semibold ${oddsColor(homeNeg!.pick!.odds_am)}`}>
                                        {formatOdds(homeNeg!.pick!.odds_am)}
                                      </span>
                                      <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                                        {shortBook(homeNeg!.pick!.book)}
                                      </span>
                                    </div>
                                  </div>
                                  <div className="flex items-center justify-between rounded-lg bg-overlay px-3 py-1.5">
                                    <span className="text-xs text-primary">
                                      {awayName}{" "}
                                      <span className={`font-mono ${oddsColor(awayPos!.line)}`}>
                                        {formatOdds(awayPos!.line)}
                                      </span>
                                    </span>
                                    <div className="flex items-center gap-1.5">
                                      <span className={`font-mono text-sm font-semibold ${oddsColor(awayPos!.pick!.odds_am)}`}>
                                        {formatOdds(awayPos!.pick!.odds_am)}
                                      </span>
                                      <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                                        {shortBook(awayPos!.pick!.book)}
                                      </span>
                                    </div>
                                  </div>
                                </div>
                                {showA && valA != null && (
                                  <div className="mt-1 flex items-center gap-1.5 pl-1 text-[10px] text-muted-text">
                                    Value: {valueBadge(valA)}
                                  </div>
                                )}
                              </div>
                            )}

                            {pairB && (
                              <div>
                                <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                                  <div className="flex items-center justify-between rounded-lg bg-overlay px-3 py-1.5">
                                    <span className="text-xs text-primary">
                                      {homeName}{" "}
                                      <span className={`font-mono ${oddsColor(homePos!.line)}`}>
                                        {formatOdds(homePos!.line)}
                                      </span>
                                    </span>
                                    <div className="flex items-center gap-1.5">
                                      <span className={`font-mono text-sm font-semibold ${oddsColor(homePos!.pick!.odds_am)}`}>
                                        {formatOdds(homePos!.pick!.odds_am)}
                                      </span>
                                      <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                                        {shortBook(homePos!.pick!.book)}
                                      </span>
                                    </div>
                                  </div>
                                  <div className="flex items-center justify-between rounded-lg bg-overlay px-3 py-1.5">
                                    <span className="text-xs text-primary">
                                      {awayName}{" "}
                                      <span className={`font-mono ${oddsColor(awayNeg!.line)}`}>
                                        {formatOdds(awayNeg!.line)}
                                      </span>
                                    </span>
                                    <div className="flex items-center gap-1.5">
                                      <span className={`font-mono text-sm font-semibold ${oddsColor(awayNeg!.pick!.odds_am)}`}>
                                        {formatOdds(awayNeg!.pick!.odds_am)}
                                      </span>
                                      <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                                        {shortBook(awayNeg!.pick!.book)}
                                      </span>
                                    </div>
                                  </div>
                                </div>
                                {showB && valB != null && (
                                  <div className="mt-1 flex items-center gap-1.5 pl-1 text-[10px] text-muted-text">
                                    Value: {valueBadge(valB)}
                                  </div>
                                )}
                              </div>
                            )}

                            {pairA && pairB && <div className="border-b border-border/60 mt-1.5" />}
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {totalsOpen && totals.length > 0 && (
                    <div className="border-t border-border px-5 py-3 space-y-1.5">
                      {totals
                        .slice()
                        .sort((a, b) => (a.line ?? 0) - (b.line ?? 0))
                        .map((bl, idx) => {
                          if (!bl.over?.book || !bl.under?.book) return null;
                          const tVal = pairValue(bl.over.odds_am, bl.under.odds_am);
                          return (
                            <div
                              key={`total-${bl.line}-${idx}`}
                              className="flex items-center justify-between rounded-lg bg-overlay px-3 py-1.5"
                            >
                              <span className="text-xs font-mono text-muted-text">
                                {bl.line != null ? `Total ${bl.line}` : "Total"}
                              </span>
                              <div className="flex items-center gap-3 text-xs">
                                {tVal != null && valueBadge(tVal)}
                                <div className="flex items-center gap-1.5">
                                  <span className="text-[10px] text-secondary">O</span>
                                  <span className={`font-mono ${oddsColor(bl.over.odds_am)}`}>
                                    {formatOdds(bl.over.odds_am)}
                                  </span>
                                  <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                                    {shortBook(bl.over.book)}
                                  </span>
                                </div>
                                <div className="flex items-center gap-1.5">
                                  <span className="text-[10px] text-secondary">U</span>
                                  <span className={`font-mono ${oddsColor(bl.under.odds_am)}`}>
                                    {formatOdds(bl.under.odds_am)}
                                  </span>
                                  <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                                    {shortBook(bl.under.book)}
                                  </span>
                                </div>
                              </div>
                            </div>
                          );
                        })}
                    </div>
                  )}

                  {propsOpen && props.length > 0 && (
                    <div className="border-t border-border px-5 py-3 space-y-1.5">
                      {props
                        .slice()
                        .sort((a, b) => {
                          const pCmp = (a.player ?? "").localeCompare(b.player ?? "");
                          if (pCmp !== 0) return pCmp;
                          const tCmp = (a.prop_type ?? "").localeCompare(b.prop_type ?? "");
                          if (tCmp !== 0) return tCmp;
                          return (a.line ?? 0) - (b.line ?? 0);
                        })
                        .map((bl, idx) => {
                          if (!bl.over?.book || !bl.under?.book) return null;
                          const propLabel = (bl.prop_type ?? "prop").replace(/^player_/, "").replace(/_/g, " ");
                          return (
                            <div
                              key={`prop-${bl.player}-${bl.prop_type}-${bl.line}-${idx}`}
                              className="flex flex-col gap-1 rounded-lg bg-overlay px-3 py-2"
                            >
                              <div className="flex items-center justify-between gap-3">
                                <span className="text-xs font-semibold text-primary">
                                  {bl.player ?? "Player"} -{" "}
                                  <span className="text-secondary capitalize">{propLabel}</span>
                                  {bl.line != null ? ` ${bl.line}` : ""}
                                </span>
                              </div>
                              <div className="flex items-center gap-3 text-xs">
                                <div className="flex items-center gap-1.5">
                                  <span className="text-[10px] text-secondary">O</span>
                                  <span className={`font-mono ${oddsColor(bl.over.odds_am)}`}>
                                    {formatOdds(bl.over.odds_am)}
                                  </span>
                                  <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                                    {shortBook(bl.over.book)}
                                  </span>
                                </div>
                                <div className="flex items-center gap-1.5">
                                  <span className="text-[10px] text-secondary">U</span>
                                  <span className={`font-mono ${oddsColor(bl.under.odds_am)}`}>
                                    {formatOdds(bl.under.odds_am)}
                                  </span>
                                  <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                                    {shortBook(bl.under.book)}
                                  </span>
                                </div>
                              </div>
                            </div>
                          );
                        })}
                    </div>
                  )}
                </Card>
              );
            })
          )}
        </section>
      )}
      </div>
    </div>

    </main>
  );
}

