"use client";

import { useId, useRef, type ReactNode } from "react";
import { ModalHeader } from "./ModalHeader";
import { cx } from "./cx";
import { useModalFocus } from "./useModalFocus";

interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: string;
  closeLabel: string;
  children: ReactNode;
  side?: "left" | "right";
  description?: string;
  className?: string;
  panelClassName?: string;
}

export function Drawer({
  open,
  onClose,
  title,
  closeLabel,
  children,
  side = "right",
  description,
  className,
  panelClassName,
}: DrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useModalFocus(open, panelRef, onClose);
  if (!open) return null;

  return (
    <div className={cx("fixed inset-0 z-[70] bg-black/45", className)} onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={cx(
          "workspace-drawer absolute inset-y-0 flex h-[100dvh] w-[min(24rem,calc(100vw-1.25rem))] flex-col border-border bg-surface shadow-drawer outline-none",
          side === "left"
            ? "left-0 border-r [--drawer-from:-1.5rem]"
            : "right-0 border-l [--drawer-from:1.5rem]",
          panelClassName,
        )}
      >
        <ModalHeader
          titleId={titleId}
          title={title}
          closeLabel={closeLabel}
          onClose={onClose}
          description={description}
        />
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </section>
    </div>
  );
}
