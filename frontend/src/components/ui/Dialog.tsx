"use client";

import { useId, useRef, type ReactNode } from "react";
import { ModalHeader } from "./ModalHeader";
import { cx } from "./cx";
import { useModalFocus } from "./useModalFocus";

interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  closeLabel: string;
  children: ReactNode;
  description?: string;
  className?: string;
}

export function Dialog({
  open,
  onClose,
  title,
  closeLabel,
  children,
  description,
  className,
}: DialogProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useModalFocus(open, panelRef, onClose);
  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[70] grid place-items-center bg-black/45 p-3 sm:p-6" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={cx(
          "max-h-[calc(100dvh-1.5rem)] w-full max-w-xl overflow-y-auto rounded-xl border border-border bg-surface shadow-dialog outline-none",
          className,
        )}
      >
        <ModalHeader
          titleId={titleId}
          title={title}
          closeLabel={closeLabel}
          onClose={onClose}
          description={description}
          className="sticky top-0 z-10 bg-surface sm:px-5"
        />
        {children}
      </div>
    </div>
  );
}
