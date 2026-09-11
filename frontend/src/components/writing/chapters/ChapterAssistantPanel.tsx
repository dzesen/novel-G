"use client";

import { useTranslations } from "next-intl";
import { SCENE_ILLUSTRATION_ENABLED } from "@/lib/featureFlags";
import { Button } from "@/components/ui/Button";

interface ChapterAssistantPanelProps {
  hasChapter: boolean;
  compact?: boolean;
  onOpenChapterOutline: () => void;
  onOpenProse: () => void;
  canGenerateProse: boolean;
  onOpenSceneIllustration: () => void;
  canGenerateSceneIllustration: boolean;
  onOpenStateBackfill: () => void;
  hasContent: boolean;
  onOpenJudgeReviews: () => void;
}

interface AssistantActionProps {
  title: string;
  description: string;
  onClick: () => void;
  disabled?: boolean;
  disabledReason?: string;
  primary?: boolean;
}

function AssistantAction({
  title,
  description,
  onClick,
  disabled,
  disabledReason,
  primary,
}: AssistantActionProps) {
  return (
    <div className="studio-assistant-action">
      <Button className="studio-assistant-action-button" variant={primary ? "primary" : "secondary"} size="sm" onClick={onClick} disabled={disabled} title={disabled ? disabledReason : undefined}>
        <span>{title}</span>
        <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M4 12h16m-6-6 6 6-6 6" /></svg>
      </Button>
      <p>{disabled && disabledReason ? disabledReason : description}</p>
    </div>
  );
}

export default function ChapterAssistantPanel({
  hasChapter,
  compact = false,
  onOpenChapterOutline,
  onOpenProse,
  canGenerateProse,
  onOpenSceneIllustration,
  canGenerateSceneIllustration,
  onOpenStateBackfill,
  hasContent,
  onOpenJudgeReviews,
}: ChapterAssistantPanelProps) {
  const tw = useTranslations("writing.chapterEditor.workspace");
  const tOutline = useTranslations("writing.outline");
  const tProse = useTranslations("writing.prose");
  const tSceneIllustration = useTranslations("writing.sceneIllustration");
  const tStateBackfill = useTranslations("stateBackfill");
  const tReviews = useTranslations("writing.judgeReviews");

  if (compact) {
    return (
      <div className="studio-ai-shortcuts" role="group" aria-label={tw("quickActions")} data-testid="chapter-ai-shortcuts">
        <Button variant={canGenerateProse ? "secondary" : "primary"} size="sm" onClick={onOpenChapterOutline} disabled={!hasChapter}>
          {tOutline("chapterTitle")}
        </Button>
        <Button variant={canGenerateProse ? "primary" : "secondary"} size="sm" onClick={onOpenProse} disabled={!hasChapter || !canGenerateProse} title={!canGenerateProse ? tProse("needOutline") : undefined}>
          {tProse("title")}
        </Button>
      </div>
    );
  }

  return (
    <div className="studio-assistant-panel" data-testid="chapter-ai-assistant">
      <div className="studio-assistant-intro">
        <h2>{tw(!hasChapter ? "assistantChooseChapter" : canGenerateProse ? "assistantReadyTitle" : "assistantStartTitle")}</h2>
        <p>{tw(!hasChapter ? "assistantChooseChapterHint" : canGenerateProse ? "assistantReadyHint" : "assistantStartHint")}</p>
      </div>
      <AssistantAction
        title={tOutline("chapterTitle")}
        description={tw("assistantOutlineDescription")}
        onClick={onOpenChapterOutline}
        disabled={!hasChapter}
        primary={!canGenerateProse}
      />
      <AssistantAction
        title={tProse("title")}
        description={tw("assistantProseDescription")}
        onClick={onOpenProse}
        disabled={!hasChapter || !canGenerateProse}
        disabledReason={hasChapter ? tProse("needOutline") : undefined}
        primary={canGenerateProse}
      />
      {SCENE_ILLUSTRATION_ENABLED && (
        <AssistantAction
          title={tSceneIllustration("openButton")}
          description={tw("assistantIllustrationDescription")}
          onClick={onOpenSceneIllustration}
          disabled={!hasChapter || !canGenerateSceneIllustration}
          disabledReason={hasChapter ? tSceneIllustration("needOutline") : undefined}
        />
      )}
      <AssistantAction
        title={tStateBackfill("openButton")}
        description={tw("assistantStateDescription")}
        onClick={onOpenStateBackfill}
        disabled={!hasChapter || !hasContent}
        disabledReason={hasChapter ? tStateBackfill("needContent") : undefined}
      />
      <div className="studio-assistant-records">
        <Button variant="quiet" size="sm" className="w-full justify-start" onClick={onOpenJudgeReviews} disabled={!hasChapter}>
          <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M3 11a9 9 0 1 1 2.5 7M3 4v7h7M12 7v5l3 2" /></svg>
          {tReviews("title")}
        </Button>
        <p>{tReviews("entryDescription")}</p>
      </div>
      <p className="studio-assistant-note">{tw("assistantSafety")}</p>
    </div>
  );
}
