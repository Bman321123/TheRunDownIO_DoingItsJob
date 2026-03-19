"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

export type TextFieldVariant = "underline" | "boxed";

export interface TextFieldProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "size"> {
  label: string;
  error?: string | null;
  helperText?: string;
  variant?: TextFieldVariant;
}

export const TextField = React.forwardRef<HTMLInputElement, TextFieldProps>(
  (
    {
      id,
      label,
      error,
      helperText,
      variant = "boxed",
      className,
      ...props
    },
    ref
  ) => {
    const inputId = id ?? React.useId();
    const isErrored = Boolean(error);

    return (
      <div className="space-y-1.5">
        <div className="relative">
          <input
            id={inputId}
            ref={ref}
            placeholder=" "
            className={cn(
              [
                "peer w-full bg-transparent text-sm text-zinc-950",
                "h-12 px-3 pt-5 pb-2",
                "transition-colors duration-150",
                "focus:outline-none",
              ].join(" "),
              variant === "boxed"
                ? [
                    "rounded-lg border",
                    isErrored ? "border-red-500" : "border-border",
                    "focus:border-zinc-950",
                  ].join(" ")
                : [
                    "border-b",
                    isErrored ? "border-red-500" : "border-border",
                    "focus:border-zinc-950",
                    "rounded-none px-0",
                  ].join(" "),
              className
            )}
            aria-invalid={isErrored || undefined}
            {...props}
          />

          <label
            htmlFor={inputId}
            className={cn(
              [
                "pointer-events-none absolute left-3 top-3 origin-[0] select-none",
                "text-xs",
                "transition-all duration-150 ease-in-out",
                "peer-placeholder-shown:top-3.5 peer-placeholder-shown:text-sm",
                "peer-focus:top-3 peer-focus:text-xs",
              ].join(" "),
              variant === "underline" && "left-0",
              isErrored
                ? "text-red-600"
                : "text-zinc-500 peer-focus:text-zinc-950"
            )}
          >
            {label}
          </label>
        </div>

        {isErrored ? (
          <div className="text-xs text-red-600">{error}</div>
        ) : helperText ? (
          <div className="text-xs text-zinc-500">{helperText}</div>
        ) : null}
      </div>
    );
  }
);
TextField.displayName = "TextField";

