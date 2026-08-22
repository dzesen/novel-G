"use client";

import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/Button";

interface ChapterAssistantPanelProps {
  onOpenChapterOutline: () => void;
  onOpenProse: () => void;
  canGenerateProse: boolean;
  onOpenSceneIllustration: () => void;
  canGenerateSceneIllustration: boolean;
  onOpenStateBackfill: () => void;
  hasContent: boolean;
}

interface AssistantActionProps {
  title: string;
  description: string;
  onClick: () => void;
  disabled?: boolean;
  disabledReason?: string;
}

function AssistantAction({
  title,
  description,
  onClick,
  disabled,
  disabledReason,
}: AssistantActionProps) {
  return (
    <div className="border-b border-border py-4 last:border-b-0">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-foreground">{title}</h3>
          <p className="mt-1 text-xs leading-5 text-muted">{description}</p>
          {disabled && disabledReason && (
            <p className="mt-2 text-xs leading-5 text-amber-700 dark:text-amber-300">{disabledReason}</p>
          )}
        </div>
        <Button size="sm" onClick={onClick} disabled={disabled} title={disabled ? disabledReason : undefined}>
          {title}
        </Button>
      </div>
    </div>
  );
}

export default function ChapterAssistantPanel({
  onOpenChapterOutline,
  onOpenProse,
  canGenerateProse,
  onOpenSceneIllustration,
  canGenerateSceneIllustration,
  onOpenStateBackfill,
  hasContent,
}: ChapterAssistantPanelProps) {
  const tw = useTranslations("writing.chapterEditor.workspace");
  const tOutline = useTranslations("writing.outline");
  const tProse = useTranslations("writing.prose");
  const tSceneIllustration = useTranslations("writing.sceneIllustration");
  const tStateBackfill = useTranslations("stateBackfill");

  return (
    <div className="px-5 py-2">
      <p className="border-b border-border py-4 text-xs leading-6 text-muted">
        {tw("assistantSafety")}
      </p>
      <AssistantAction
        title={tOutline("chapterTitle")}
        description={tw("assistantOutlineDescription")}
        onClick={onOpenChapterOutline}
      />
      <AssistantAction
        title={tProse("title")}
        description={tw("assistantProseDescription")}
        onClick={onOpenProse}
        disabled={!canGenerateProse}
        disabledReason={tProse("needOutline")}
      />
      <AssistantAction
        title={tSceneIllustration("openButton")}
        description={tw("assistantIllustrationDescription")}
        onClick={onOpenSceneIllustration}
        disabled={!canGenerateSceneIllustration}
        disabledReason={tSceneIllustration("needOutline")}
      />
      <AssistantAction
        title={tStateBackfill("openButton")}
        description={tw("assistantStateDescription")}
        onClick={onOpenStateBackfill}
        disabled={!hasContent}
        disabledReason={tStateBackfill("needContent")}
      />
    </div>
  );
}
