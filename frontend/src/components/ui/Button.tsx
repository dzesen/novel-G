"use client";

import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cx } from "./cx";

export type ButtonVariant =
  | "primary"
  | "secondary"
  | "quiet"
  | "danger";
export type ButtonSize = "sm" | "md";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
}

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    "border-transparent bg-accent text-on-accent hover:bg-accent-hover hover:text-on-accent-hover disabled:bg-accent",
  secondary:
    "border-border bg-surface text-foreground hover:border-border-strong hover:bg-surface-secondary",
  quiet:
    "border-transparent bg-transparent text-muted hover:bg-surface-secondary hover:text-foreground",
  danger:
    "border-transparent bg-red-600 text-white hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-600",
};

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: "min-h-9 px-3 text-xs",
  md: "min-h-10 px-4 text-sm",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    {
      variant = "secondary",
      size = "md",
      loading = false,
      disabled,
      className,
      children,
      type = "button",
      ...props
    },
    ref,
  ) {
    const unavailable = disabled || loading;
    return (
      <button
        {...props}
        ref={ref}
        type={type}
        disabled={unavailable}
        aria-busy={loading || undefined}
        className={cx(
          "inline-flex shrink-0 items-center justify-center gap-2 rounded-md border font-semibold transition-[background-color,border-color,color,box-shadow] duration-150",
          "touch-target",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2 focus-visible:ring-offset-background",
          "disabled:cursor-not-allowed disabled:opacity-50",
          VARIANT_CLASSES[variant],
          SIZE_CLASSES[size],
          className,
        )}
      >
        {loading && (
          <span
            aria-hidden="true"
            className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent"
          />
        )}
        {children}
      </button>
    );
  },
);
