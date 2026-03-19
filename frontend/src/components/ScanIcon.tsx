import * as React from "react";

export function ScanIcon({ spinning }: { spinning: boolean }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      className={spinning ? "animate-spin" : ""}
      style={{ animationDuration: "1s", animationTimingFunction: "linear" }}
      aria-hidden="true"
    >
      {/* Outer ring */}
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1" strokeOpacity="0.4" />
      {/* Sweep arc — 120deg */}
      <path
        d="M 8 8 L 8 1.5 A 6.5 6.5 0 0 1 13.63 11.25 Z"
        fill="currentColor"
        fillOpacity="0.6"
      />
      {/* Centre dot */}
      <circle cx="8" cy="8" r="1.5" fill="currentColor" />
    </svg>
  );
}

