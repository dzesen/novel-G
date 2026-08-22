import { IconButton } from "./IconButton";
import { cx } from "./cx";

interface ModalHeaderProps {
  titleId: string;
  title: string;
  closeLabel: string;
  onClose: () => void;
  description?: string;
  className?: string;
}

function CloseIcon() {
  return (
    <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="m6 6 12 12M18 6 6 18" />
    </svg>
  );
}

export function ModalHeader({
  titleId,
  title,
  closeLabel,
  onClose,
  description,
  className,
}: ModalHeaderProps) {
  return (
    <header className={cx("flex shrink-0 items-start gap-3 border-b border-border px-4 py-3", className)}>
      <div className="min-w-0 flex-1">
        <h2 id={titleId} className="text-base font-semibold text-foreground">{title}</h2>
        {description && <p className="mt-1 text-xs leading-5 text-muted">{description}</p>}
      </div>
      <IconButton label={closeLabel} size="sm" onClick={onClose} data-autofocus>
        <CloseIcon />
      </IconButton>
    </header>
  );
}
