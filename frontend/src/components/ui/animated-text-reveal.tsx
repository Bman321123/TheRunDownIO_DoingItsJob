"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

function usePrefersReducedMotion() {
  const [reduced, setReduced] = React.useState(false);

  React.useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return reduced;
}

function useInViewport<T extends Element>() {
  const ref = React.useRef<T | null>(null);
  const [inView, setInView] = React.useState(false);

  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting) setInView(true);
      },
      { threshold: 0.2 }
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return { ref, inView } as const;
}

export function AnimatedTextReveal(props: {
  text: string;
  className?: string;
  wordDurationMs?: number;
  staggerMs?: number;
}) {
  const { text, className, wordDurationMs = 400, staggerMs = 50 } = props;
  const reducedMotion = usePrefersReducedMotion();
  const { ref, inView } = useInViewport<HTMLSpanElement>();

  if (reducedMotion) {
    return <span className={className}>{text}</span>;
  }

  const words = text.split(/\s+/).filter(Boolean);

  return (
    <span ref={ref} className={cn("inline-block", className)} aria-label={text}>
      {words.map((w, i) => (
        <span key={`${w}-${i}`} className="inline-block">
          <span
            className={cn(
              "inline-block opacity-0",
              inView && "opacity-100"
            )}
            style={{
              transitionProperty: "opacity",
              transitionDuration: `${wordDurationMs}ms`,
              transitionTimingFunction: "ease",
              transitionDelay: `${i * staggerMs}ms`,
            }}
          >
            {w}
          </span>
          {i < words.length - 1 ? <span>{" "}</span> : null}
        </span>
      ))}
    </span>
  );
}

