"use client";

import { useId, type ReactNode } from "react";
import { cx } from "./cx";

interface FieldProps {
  label: string;
  hint?: string;
  error?: string;
  children: (props: {
    id: string;
    "aria-describedby"?: string;
    "aria-invalid"?: true;
  }) => ReactNode;
  className?: string;
}

export function Field({
  label,
  hint,
  error,
  children,
  className,
}: FieldProps) {
  const id = useId();
  const descriptionId = hint || error ? `${id}-description` : undefined;
  return (
    <div className={cx("grid gap-1.5", className)}>
      <label htmlFor={id} className="text-xs font-semibold text-foreground">
        {label}
      </label>
      {children({
        id,
        "aria-describedby": descriptionId,
        "aria-invalid": error ? true : undefined,
      })}
      {(error || hint) && (
        <p
          id={descriptionId}
          className={cx(
            "text-xs leading-5",
            error ? "text-red-700 dark:text-red-300" : "text-muted",
          )}
        >
          {error || hint}
        </p>
      )}
    </div>
  );
}
