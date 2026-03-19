"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

export type UnderlineTab<T extends string> = {
  value: T;
  label: string;
};

export function UnderlineTabs<T extends string>(props: {
  tabs: UnderlineTab<T>[];
  value: T;
  onValueChange: (value: T) => void;
  className?: string;
}) {
  const { tabs, value, onValueChange, className } = props;
  const containerRef = React.useRef<HTMLDivElement | null>(null);
  const triggerRefs = React.useRef(new Map<T, HTMLButtonElement>());
  const [indicator, setIndicator] = React.useState<{ left: number; width: number } | null>(null);

  const recalc = React.useCallback(() => {
    const container = containerRef.current;
    const active = triggerRefs.current.get(value);
    if (!container || !active) return;
    const c = container.getBoundingClientRect();
    const a = active.getBoundingClientRect();
    setIndicator({ left: a.left - c.left, width: a.width });
  }, [value]);

  React.useLayoutEffect(() => {
    recalc();
  }, [recalc, tabs.length]);

  React.useEffect(() => {
    const onResize = () => recalc();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [recalc]);

  return (
    <div ref={containerRef} className={cn("relative flex gap-6 border-b border-border", className)}>
      {tabs.map((t) => {
        const active = t.value === value;
        return (
          <button
            key={t.value}
            type="button"
            ref={(el) => {
              if (!el) return;
              triggerRefs.current.set(t.value, el);
            }}
            onClick={() => onValueChange(t.value)}
            className={cn(
              "pb-2 text-sm font-semibold transition-colors duration-150",
              active ? "text-primary" : "text-secondary hover:text-primary"
            )}
          >
            {t.label}
          </button>
        );
      })}
      <span
        aria-hidden="true"
        className="absolute bottom-0 h-[1.5px] bg-primary transition-all duration-150 ease-in-out"
        style={{
          width: indicator?.width ?? 0,
          transform: `translateX(${indicator?.left ?? 0}px)`,
          opacity: indicator ? 1 : 0,
        }}
      />
    </div>
  );
}

