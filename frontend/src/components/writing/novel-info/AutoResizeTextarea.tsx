"use client";

import { useRef, useEffect, useCallback, type TextareaHTMLAttributes } from "react";

interface AutoResizeTextareaProps extends Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "value" | "onChange" | "defaultValue"> {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  minHeight?: number;
  maxHeight?: number;
  className?: string;
  disabled?: boolean;
}

export default function AutoResizeTextarea({
  value,
  onChange,
  placeholder,
  minHeight = 80,
  maxHeight = 320,
  className = "",
  disabled = false,
  style,
  ...props
}: AutoResizeTextareaProps) {
  const ref = useRef<HTMLTextAreaElement>(null);

  const resize = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    const next = Math.min(Math.max(el.scrollHeight, minHeight), maxHeight);
    el.style.height = `${next}px`;
    el.style.overflowY = el.scrollHeight > maxHeight ? "auto" : "hidden";
  }, [minHeight, maxHeight]);

  useEffect(() => {
    resize();
  }, [value, resize]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let previousWidth = el.getBoundingClientRect().width;
    // Height changes made by resize must not cause an observer feedback loop.
    const observer = new ResizeObserver(() => {
      const width = el.getBoundingClientRect().width;
      if (width === previousWidth) return;
      previousWidth = width;
      resize();
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [resize]);

  return (
    <textarea
      {...props}
      ref={ref}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      disabled={disabled}
      className={`w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted/50 focus:outline-none focus:ring-2 focus:ring-primary resize-none transition-colors ${disabled ? "opacity-60 cursor-not-allowed" : ""} ${className}`}
      style={{ ...style, minHeight: `${minHeight}px`, maxHeight: `${maxHeight}px` }}
    />
  );
}
