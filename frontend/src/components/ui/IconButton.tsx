"use client";

import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cx } from "./cx";

export interface IconButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  size?: "sm" | "md";
  selected?: boolean;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(
  function IconButton(
    {
      label,
      size = "md",
      selected = false,
      className,
      children,
      type = "button",
      ...props
    },
    ref,
  ) {
    return (
      <button
        {...props}
        ref={ref}
        type={type}
        aria-label={label}
        aria-pressed={selected || undefined}
        title={label}
        className={cx(
          "inline-grid shrink-0 place-items-center rounded-md border transition-[background-color,border-color,color,box-shadow] duration-150",
          "touch-target",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2 focus-visible:ring-offset-background",
          "disabled:cursor-not-allowed disabled:opacity-45",
          size === "sm" ? "h-8 w-8" : "h-10 w-10",
          selected
            ? "border-accent/30 bg-accent/10 text-accent"
            : "border-transparent text-muted hover:border-border hover:bg-surface-secondary hover:text-foreground",
          className,
        )}
      >
        {children}
      </button>
    );
  },
);
