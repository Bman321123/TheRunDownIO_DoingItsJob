"use client";
import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";
import { ScanIcon } from "@/components/ScanIcon";

const buttonVariants = cva(
  [
    "relative inline-flex items-center justify-center whitespace-nowrap",
    "rounded text-sm font-medium",
    "px-5 py-2.5",
    "transition-all duration-150 ease-in-out",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-900/20 focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
    "disabled:cursor-not-allowed disabled:opacity-40",
  ].join(" "),
  {
    variants: {
      variant: {
        primary: "bg-zinc-950 text-white hover:bg-zinc-900",
        secondary:
          "border border-border bg-transparent text-zinc-900 hover:bg-zinc-950/5",
        tertiary:
          "bg-transparent text-zinc-900 hover:underline underline-offset-4",
        scan: "bg-accent-green text-[#0A0A0F] hover:bg-accent-green/90 disabled:opacity-60 disabled:cursor-not-allowed",
      },
      size: {
        default: "h-10",
        sm: "h-9 px-4",
        lg: "h-11 px-6",
        icon: "h-10 w-10 px-0",
      },
    },
    defaultVariants: {
      variant: "primary",
      size: "default",
    },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
  loading?: boolean;
  scanLabels?: string[];
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  (
    { className, variant, size, asChild = false, loading = false, disabled, children, scanLabels, ...props },
    ref
  ) => {
    const Comp = asChild ? Slot : "button";
    const isDisabled = disabled || loading;
    const isScan = variant === "scan";

    const scanLabelList = isScan && scanLabels && scanLabels.length > 0 ? scanLabels : undefined;
    const scanDefaultLabel = isScan && typeof children === "string" ? children : undefined;

    const [scanLabelIdx, setScanLabelIdx] = React.useState(0);

    React.useEffect(() => {
      if (!isScan || !loading || !scanLabelList) return;
      setScanLabelIdx(0);
      const interval = window.setInterval(() => {
        setScanLabelIdx((i) => (i + 1) % scanLabelList.length);
      }, 900);
      return () => window.clearInterval(interval);
    }, [isScan, loading, scanLabelList]);

    if (asChild) {
      return (
        <Comp
          className={cn(buttonVariants({ variant, size, className }))}
          ref={ref}
          {...props}
        >
          {children}
        </Comp>
      );
    }

    if (isScan) {
      const scanningLabel = loading
        ? scanLabelList
          ? scanLabelList[scanLabelIdx]
          : scanDefaultLabel ?? "Scanning…"
        : children;

      return (
        <Comp
          className={cn(
            buttonVariants({ variant, size, className }),
            loading && "animate-pulse-glow shadow-glow-scan cursor-wait disabled:opacity-100 disabled:cursor-wait gap-2"
          )}
          ref={ref}
          disabled={isDisabled}
          aria-busy={loading || undefined}
          {...props}
        >
          {loading ? <ScanIcon spinning /> : null}
          <span>{scanningLabel}</span>
        </Comp>
      );
    }

    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        disabled={isDisabled}
        aria-busy={loading || undefined}
        {...props}
      >
        <span className={cn(loading && "opacity-0")}>{children}</span>
        {loading ? (
          <span aria-hidden="true" className="absolute inset-0 grid place-items-center">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-zinc-900/25 border-t-zinc-900" />
          </span>
        ) : null}
      </Comp>
    );
  }
);
Button.displayName = "Button";

export { Button, buttonVariants };
