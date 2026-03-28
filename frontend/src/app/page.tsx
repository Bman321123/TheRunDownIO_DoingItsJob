"use client";

import { useEffect, useMemo, useState } from "react";
import type { Arb, RawLine, BestLine, ScanNowResponse } from "@/lib/types";
import Image from "next/image";
import { ArbCard } from "@/components/ArbCard";
import { Card } from "@/components/ui/card";
import { UnderlineTabs } from "@/components/ui/underline-tabs";
import { AnimatedTextReveal } from "@/components/ui/animated-text-reveal";
import { Badge } from "@/components/ui/badge";
import { espnTeamLogoUrl, initials, parseMatchup } from "@/lib/teams";
import { buildDeepLink } from "@/lib/deeplinks";

const DEFAULT_BASE = "http://127.0.0.1:3030";

const SPORTS = ["NCAAF", "NFL", "MLB", "NBA", "NCAAB", "NHL", "NCAAWB", "MMA"] as const;
type Sport = (typeof SPORTS)[number];

const SCAN_LABELS = ["Scanning lines...", "Fetching odds...", "Matching games...", "Calculating arbs..."] as const;
const SCAN_COOLDOWN_MS = 3_000; // 3-second breather between scans
export default function HomePage() {
  const baseUrl = process.env.NEXT_PUBLIC_ARBS_URL || DEFAULT_BASE;
  const arbsUrl = `${baseUrl.replace(/\/$/, "")}/arbs`;
  const scanUrl = `${baseUrl.replace(/\/$/, "")}/scan-now`;
  const scanStreamUrl = `${baseUrl.replace(/\/$/, "")}/scan-now-stream`;

  const [selectedSports, setSelectedSports] = useState<Set<Sport>>(() => new Set(SPORTS));
  const [arbs, setArbs] = useState<Arb[]>([]);
  const [lines, setLines] = useState<RawLine[]>([]);
  const [bestLines, setBestLines] = useState<BestLine[]>([]);
  const [status, setStatus] = useState<"idle" | "loading" | "ok" | "error">("idle");
  const [scanStatus, setScanStatus] = useState<"idle" | "scanning" | "error">("idle");
  const [scanLabel, setScanLabel] = useState<string>(SCAN_LABELS[0]);
  const [error, setError] = useState<string | null>(null);
  const [lastScannedMs, setLastScannedMs] = useState<number | null>(null);
  const [dataAge, setDataAge] = useState<number | null>(null);
  const [dpRemaining, setDpRemaining] = useState<string | null>(null);
  const [secondsAgo, setSecondsAgo] = useState<number | null>(null);

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

  // Tick "seconds ago" counter every second
  useEffect(() => {
    const id = window.setInterval(() => {
      setLastScannedMs((prev) => {
        if (prev) setSecondsAgo(Math.floor((Date.now() - prev) / 1000));
        return prev;
      });
    }, 1_000);
    return () => window.clearInterval(id);
  }, []);

  // Cycle scan labels while scanning
  useEffect(() => {
    if (scanStatus !== "scanning") return;
    let idx = 0;
    setScanLabel(SCAN_LABELS[0]);
    const id = window.setInterval(() => {
      idx = (idx + 1) % SCAN_LABELS.length;
      setScanLabel(SCAN_LABELS[idx]);
    }, 900);
    return () => window.clearInterval(id);
  }, [scanStatus]);

  useEffect(() => {
    if (activeTab === displayTab) return;
    const timer = window.setTimeout(() => {
      setDisplayTab(activeTab);
      setTabKey((k) => k + 1);
    }, 80);
    return () => window.clearTimeout(timer);
  }, [activeTab, displayTab]);

  // Continuous scan loop: scan → cooldown → scan → cooldown → ...
  // Cancels cleanly when selectedSports or URLs change (effect re-runs).
  useEffect(() => {
    let cancelled = false;
    const abortCtrl = new AbortController();

    async function doOneScan() {
      const sports = Array.from(selectedSports);
      if (sports.length === 0) return;

      setScanStatus("scanning");
      setError(null);

      const resp = await fetch(scanStreamUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sports }),
        signal: abortCtrl.signal,
      });

      if (!resp.ok || !resp.body) {
        // Fallback to old endpoint if SSE not available
        const fallback = await fetch(scanUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sports }),
          signal: abortCtrl.signal,
        });
        const payload = (await fallback.json()) as ScanNowResponse;
        if (!fallback.ok || !payload?.ok) {
          throw new Error(payload?.error || `HTTP ${fallback.status}`);
        }
        setArbs(Array.isArray(payload.arbs) ? payload.arbs : []);
        setLines(Array.isArray(payload.lines) ? payload.lines : []);
        setBestLines(Array.isArray(payload.bestLines) ? payload.bestLines : []);
        setLastScannedMs(payload.lastScanMs ?? Date.now());
        setSecondsAgo(0);
        if (typeof payload.dataAge === "number") setDataAge(payload.dataAge);
        if (payload.dpRemaining) setDpRemaining(payload.dpRemaining);
        return;
      }

      // SSE streaming: read progressive updates
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (!cancelled) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const parts = buffer.split("\n\n");
        buffer = parts.pop()!;

        for (const raw of parts) {
          if (!raw.trim()) continue;
          const dataLine = raw.split("\n").find((l) => l.startsWith("data: "));
          if (!dataLine) continue;
          try {
            const payload = JSON.parse(dataLine.slice(6));
            if (Array.isArray(payload.arbs)) setArbs(payload.arbs);
            if (Array.isArray(payload.lines)) setLines(payload.lines);
            if (Array.isArray(payload.bestLines)) setBestLines(payload.bestLines);
            if (payload.lastScanMs) { setLastScannedMs(payload.lastScanMs); setSecondsAgo(0); }
            if (typeof payload.dataAge === "number") setDataAge(payload.dataAge);
            if (payload.dpRemaining) setDpRemaining(payload.dpRemaining);
          } catch {
            // skip malformed SSE data
          }
        }
      }
    }

    async function loop() {
      while (!cancelled) {
        try {
          await doOneScan();
          setScanStatus("idle");
        } catch (e: any) {
          if (cancelled) return;
          setScanStatus("error");
          setError(e?.message || "Scan failed");
        }
        // Brief cooldown before next scan
        if (!cancelled) {
          await new Promise<void>((r) => {
            const t = setTimeout(r, SCAN_COOLDOWN_MS);
            // If cancelled during cooldown, resolve immediately
            const check = setInterval(() => {
              if (cancelled) { clearTimeout(t); clearInterval(check); r(); }
            }, 200);
          });
        }
      }
    }

    loop();

    return () => {
      cancelled = true;
      abortCtrl.abort();
    };
  }, [selectedSports, scanStreamUrl, scanUrl]);

  return (
    <main className="space-y-5">
      <header className="flex flex-col gap-4 rounded-xl border border-subtle bg-raised px-6 py-5 shadow-card transition-all duration-200 hover:shadow-card-hover hover:border-emphasis">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-2xl font-semibold tracking-tight text-primary">
              <AnimatedTextReveal text="Arbitrage Dashboard" />
            </div>
            <div className="mt-1 text-sm text-secondary">
              Auto-scanning book lines to surface both-sides value.
            </div>
          </div>

          <div className="flex items-center gap-4">
            {dpRemaining && (
              <div className="text-right">
                <div className="text-xs text-[#9898B8]">DP Left</div>
                <div className="text-sm font-semibold text-[#E8E8F8]">{dpRemaining}</div>
              </div>
            )}
            {/* Live status indicator */}
            <div className="flex items-center gap-2">
              {scanStatus === "scanning" ? (
                <>
                  <span className="relative flex h-2.5 w-2.5">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                    <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-emerald-500" />
                  </span>
                  <span className="text-xs font-medium text-emerald-400 animate-pulse">{scanLabel}</span>
                </>
              ) : scanStatus === "error" ? (
                <>
                  <span className="h-2.5 w-2.5 rounded-full bg-red-500" />
                  <span className="text-xs font-medium text-red-400">Disconnected</span>
                </>
              ) : (
                <>
                  <span className="h-2.5 w-2.5 rounded-full bg-emerald-500" />
                  <span className="text-xs font-medium text-emerald-400">Live</span>
                </>
              )}
            </div>
          </div>
        </div>

        {/* Scanning progress bar */}
        {scanStatus === "scanning" && (
          <div className="relative h-1 w-full overflow-hidden rounded-full bg-white/[0.06]">
            <div className="absolute inset-0 h-full w-1/3 animate-scan-bar rounded-full bg-gradient-to-r from-transparent via-emerald-400 to-transparent" />
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
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-xs text-[#9898B8]">
            Opportunities: <span className="font-semibold text-[#E8E8F8]">{count}</span>
          </div>
          <div className="flex items-center gap-3">
            <div className="text-xs text-[#9898B8]">
              Updated:{" "}
              <span className="font-semibold text-[#E8E8F8]">
                {secondsAgo != null
                  ? secondsAgo < 5
                    ? "just now"
                    : secondsAgo < 60
                      ? `${secondsAgo}s ago`
                      : `${Math.floor(secondsAgo / 60)}m ${secondsAgo % 60}s ago`
                  : scanStatus === "scanning"
                    ? "scanning..."
                    : "—"}
              </span>
            </div>
            <div className="text-xs text-[#9898B8]">
              <span className="font-semibold text-[#E8E8F8]">
                {scanStatus === "scanning"
                  ? "Auto-refreshing"
                  : "Next scan queued"}
              </span>
            </div>
          </div>
        </div>

        {scanStatus === "error" ? (
          <div className="text-xs text-[#FCA5A5]">
            Error: {error}. Retrying automatically...
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
                            <th className="font-medium text-center pb-2 w-10"></th>
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
                              <td className="py-2.5 px-2 text-center">
                                <a
                                  href={buildDeepLink(l.book, parseMatchup(l.game).home, l.url)}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  className="inline-flex items-center justify-center h-6 w-6 rounded text-sky-400 hover:bg-sky-500/20 transition"
                                  title={`Open on ${l.book}`}
                                >
                                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" className="h-3.5 w-3.5">
                                    <path fillRule="evenodd" d="M4.22 11.78a.75.75 0 0 1 0-1.06L9.44 5.5H5.75a.75.75 0 0 1 0-1.5h5.5a.75.75 0 0 1 .75.75v5.5a.75.75 0 0 1-1.5 0V6.56l-5.22 5.22a.75.75 0 0 1-1.06 0Z" clipRule="evenodd" />
                                  </svg>
                                </a>
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
            (() => {
              // Hoist arb value calculation so we can sort by it
              const _amToDec = (am: number) =>
                am >= 100 ? am / 100 + 1 : am <= -100 ? 100 / Math.abs(am) + 1 : 0;
              const _bestArbValue = (lines: BestLine[]): number => {
                let best = -Infinity;
                for (const bl of lines) {
                  // Moneyline pair value
                  if (bl.type === "moneyline" && bl.home?.odds_am != null && bl.away?.odds_am != null) {
                    const dA = _amToDec(bl.home.odds_am);
                    const dB = _amToDec(bl.away.odds_am);
                    if (dA > 1 && dB > 1) {
                      const v = Math.round((1 - (1 / dA + 1 / dB)) * 10000) / 100;
                      if (v > best) best = v;
                    }
                  }
                  // Total pair value
                  if (bl.type === "total" && bl.over?.odds_am != null && bl.under?.odds_am != null) {
                    const dA = _amToDec(bl.over.odds_am);
                    const dB = _amToDec(bl.under.odds_am);
                    if (dA > 1 && dB > 1) {
                      const v = Math.round((1 - (1 / dA + 1 / dB)) * 10000) / 100;
                      if (v > best) best = v;
                    }
                  }
                }
                return best === -Infinity ? -999 : best;
              };
              return Array.from(
                bestLines.reduce((acc, bl) => {
                  const key = `${bl.sport}::${bl.game}`;
                  const list = acc.get(key) ?? [];
                  list.push(bl);
                  acc.set(key, list);
                  return acc;
                }, new Map<string, BestLine[]>())
              )
                .map(([key, linesForEvent]) => {
                  return { key, linesForEvent, _val: _bestArbValue(linesForEvent) };
                })
                .sort((a, b) => b._val - a._val || a.key.localeCompare(b.key));
            })()
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
              const bookBadge = (bookName: string, teamName: string, url?: string, size: "sm" | "md" = "sm") => {
                const link = buildDeepLink(bookName, teamName, url);
                const cls = size === "sm"
                  ? "rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold text-primary hover:bg-sky-500/20 hover:text-sky-400 transition"
                  : "rounded-full bg-muted px-2 py-0.5 text-xs font-semibold text-primary hover:bg-sky-500/20 hover:text-sky-400 transition";
                return (
                  <a href={link} target="_blank" rel="noopener noreferrer" className={cls}>
                    {shortBook(bookName)}
                  </a>
                );
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
                      const homeLogoUrl = espnTeamLogoUrl(sport, bl.home_team);
                      const awayLogoUrl = espnTeamLogoUrl(sport, bl.away_team);
                      const linkHome = buildDeepLink(bl.home.book, bl.home_team!, bl.home.url);
                      const linkAway = buildDeepLink(bl.away.book, bl.away_team!, bl.away.url);
                      const handlePlaceBothML = () => {
                        window.open(linkHome, "_blank", "noopener,noreferrer");
                        setTimeout(() => window.open(linkAway, "_blank", "noopener,noreferrer"), 100);
                      };
                      return (
                        <div key={`ml-${idx}`} className="mt-3">
                          <div className="flex gap-2">
                          <div className="flex-1 grid grid-cols-1 gap-2 sm:grid-cols-2">
                            <div className="flex items-center justify-between rounded-xl bg-overlay px-4 py-3">
                              <div className="flex items-center gap-2.5">
                                <div className="relative h-8 w-8 shrink-0 overflow-hidden rounded-full border border-border bg-surface">
                                  {homeLogoUrl ? (
                                    <Image src={homeLogoUrl} alt={bl.home_team} fill sizes="32px" />
                                  ) : (
                                    <div className="grid h-full w-full place-items-center text-[10px] font-bold text-secondary">
                                      {initials(bl.home_team)}
                                    </div>
                                  )}
                                </div>
                                <div>
                                  <div className="text-[10px] uppercase tracking-wider text-secondary">Home</div>
                                  <div className="text-sm font-bold text-primary">{bl.home_team}</div>
                                </div>
                              </div>
                              <div className="flex items-center gap-2">
                                <span className={`font-mono text-lg font-bold ${oddsColor(bl.home.odds_am)}`}>
                                  {formatOdds(bl.home.odds_am)}
                                </span>
                                {bookBadge(bl.home.book, bl.home_team!, bl.home.url, "md")}
                              </div>
                            </div>
                            <div className="flex items-center justify-between rounded-xl bg-overlay px-4 py-3">
                              <div className="flex items-center gap-2.5">
                                <div className="relative h-8 w-8 shrink-0 overflow-hidden rounded-full border border-border bg-surface">
                                  {awayLogoUrl ? (
                                    <Image src={awayLogoUrl} alt={bl.away_team} fill sizes="32px" />
                                  ) : (
                                    <div className="grid h-full w-full place-items-center text-[10px] font-bold text-secondary">
                                      {initials(bl.away_team)}
                                    </div>
                                  )}
                                </div>
                                <div>
                                  <div className="text-[10px] uppercase tracking-wider text-secondary">Away</div>
                                  <div className="text-sm font-bold text-primary">{bl.away_team}</div>
                                </div>
                              </div>
                              <div className="flex items-center gap-2">
                                <span className={`font-mono text-lg font-bold ${oddsColor(bl.away.odds_am)}`}>
                                  {formatOdds(bl.away.odds_am)}
                                </span>
                                {bookBadge(bl.away.book, bl.away_team!, bl.away.url, "md")}
                              </div>
                            </div>
                          </div>
                          <button
                            type="button"
                            onClick={handlePlaceBothML}
                            className="flex flex-col items-center justify-center gap-2 rounded-lg bg-sky-500/15 border border-sky-500/30 px-3 min-w-[52px] text-sky-400 hover:bg-sky-500/25 hover:border-sky-400/50 transition-all cursor-pointer group"
                          >
                            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-5 w-5 group-hover:scale-110 transition-transform">
                              <path fillRule="evenodd" d="M4.25 5.5a.75.75 0 0 0-.75.75v8.5c0 .414.336.75.75.75h8.5a.75.75 0 0 0 .75-.75v-4a.75.75 0 0 1 1.5 0v4A2.25 2.25 0 0 1 12.75 17h-8.5A2.25 2.25 0 0 1 2 14.75v-8.5A2.25 2.25 0 0 1 4.25 4h5a.75.75 0 0 1 0 1.5h-5Z" clipRule="evenodd" />
                              <path fillRule="evenodd" d="M6.194 12.753a.75.75 0 0 0 1.06.053L16.5 4.44v2.81a.75.75 0 0 0 1.5 0v-4.5a.75.75 0 0 0-.75-.75h-4.5a.75.75 0 0 0 0 1.5h2.553l-9.056 8.194a.75.75 0 0 0-.053 1.06Z" clipRule="evenodd" />
                            </svg>
                            <span className="text-[10px] font-bold uppercase tracking-wider [writing-mode:vertical-lr] rotate-180">Place Bets</span>
                          </button>
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
                                      {bookBadge(homeNeg!.pick!.book, homeName, homeNeg!.pick!.url)}
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
                                      {bookBadge(awayPos!.pick!.book, awayName, awayPos!.pick!.url)}
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
                                      {bookBadge(homePos!.pick!.book, homeName, homePos!.pick!.url)}
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
                                      {bookBadge(awayNeg!.pick!.book, awayName, awayNeg!.pick!.url)}
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
                                  {bookBadge(bl.over.book, homeName, bl.over.url)}
                                </div>
                                <div className="flex items-center gap-1.5">
                                  <span className="text-[10px] text-secondary">U</span>
                                  <span className={`font-mono ${oddsColor(bl.under.odds_am)}`}>
                                    {formatOdds(bl.under.odds_am)}
                                  </span>
                                  {bookBadge(bl.under.book, homeName, bl.under.url)}
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
                                  {bookBadge(bl.over.book, homeName, bl.over.url)}
                                </div>
                                <div className="flex items-center gap-1.5">
                                  <span className="text-[10px] text-secondary">U</span>
                                  <span className={`font-mono ${oddsColor(bl.under.odds_am)}`}>
                                    {formatOdds(bl.under.odds_am)}
                                  </span>
                                  {bookBadge(bl.under.book, homeName, bl.under.url)}
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

