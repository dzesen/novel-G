"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { Button, Card } from "@heroui/react";
import AICreateStepper from "./AICreateStepper";
import CardDrivenCreatePanel from "./CardDrivenCreatePanel";
import { clearAICreateCache } from "@/lib/aiCreateCache";
import { deleteCardAvatarHandoffs } from "@/lib/cardAvatarHandoff";
import {
  createBlankWritingDraft,
  createGeneratedWritingDraft,
} from "@/lib/novelCreationDraft";
import {
  clearWritingDraft,
  loadCurrentWritingDraft,
  saveWritingDraft,
  type StoredWritingDraft,
} from "@/lib/writingDraft";
import type {
  AICreateResponse,
  BlueprintGenerationSource,
} from "@/types/novel";

interface NewNovelPanelProps {
  onCancel: () => void;
}

type CreationMethod = "choose" | "ai" | "cards";

export default function NewNovelPanel({ onCancel }: NewNovelPanelProps) {
  const t = useTranslations("create");
  const tb = useTranslations("bookshelf");
  const router = useRouter();
  const pathname = usePathname();
  const locale = pathname.startsWith("/en") ? "en" : "zh";
  const [method, setMethod] = useState<CreationMethod>("choose");
  const [recoverableDraft, setRecoverableDraft] =
    useState<StoredWritingDraft | null>(null);
  const [redirecting, setRedirecting] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setRecoverableDraft(loadCurrentWritingDraft());
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  const openDraft = (draftId: string) => {
    setRedirecting(true);
    router.push(`/${locale}/writing/new?draft=${encodeURIComponent(draftId)}`);
  };

  const handleBlankStart = () => {
    clearAICreateCache();
    openDraft(saveWritingDraft(createBlankWritingDraft()));
  };

  const handleAIComplete = (
    result: AICreateResponse,
    generationSource: BlueprintGenerationSource,
  ) => {
    const draft = createGeneratedWritingDraft({
      result,
      generationSource,
      origin: "ai_idea",
    });
    const draftId = saveWritingDraft(draft);
    clearAICreateCache();
    openDraft(draftId);
  };

  const handleDiscardRecovery = async () => {
    if (!recoverableDraft) return;
    await deleteCardAvatarHandoffs(
      recoverableDraft.draft.card_avatar_proposal_ids ?? [],
    ).catch(() => undefined);
    clearWritingDraft(recoverableDraft.draftId);
    clearAICreateCache();
    setRecoverableDraft(null);
  };

  if (method === "cards") {
    return <CardDrivenCreatePanel onCancel={() => setMethod("choose")} />;
  }

  return (
    <div className="flex h-full min-w-0 flex-col">
      <Card className="flex h-full min-w-0 flex-col overflow-hidden">
        <Card.Header className="shrink-0 border-b border-border">
          <div className="flex w-full min-w-0 items-center justify-between gap-3">
            <div className="min-w-0">
              <p className="text-xs font-medium uppercase tracking-[0.18em] text-accent">
                {t("entry.eyebrow")}
              </p>
              <h2 className="truncate text-lg font-bold text-foreground">
                {method === "ai" ? t("entry.aiTitle") : t("entry.title")}
              </h2>
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="shrink-0"
              onPress={method === "ai" ? () => setMethod("choose") : onCancel}
            >
              {method === "ai" ? t("entry.backToMethods") : t("back")}
            </Button>
          </div>
        </Card.Header>

        <Card.Content className="flex-1 overflow-y-auto">
          {redirecting ? (
            <div className="flex h-32 items-center justify-center px-4 text-center">
              <p className="text-sm text-muted">{tb("draftRedirect")}</p>
            </div>
          ) : method === "ai" ? (
            <AICreateStepper onComplete={handleAIComplete} />
          ) : (
            <div className="mx-auto w-full max-w-4xl space-y-5 py-2">
              <div>
                <p className="text-sm leading-6 text-muted">
                  {t("entry.description")}
                </p>
                <p className="mt-2 text-xs leading-5 text-muted">
                  {t("entry.sharedDraftHint")}
                </p>
              </div>

              {recoverableDraft && (
                <section className="rounded-xl border border-accent/35 bg-accent/5 p-4">
                  <p className="text-xs font-medium uppercase tracking-[0.14em] text-accent">
                    {t("entry.recoveryEyebrow")}
                  </p>
                  <p className="mt-1 break-words text-sm font-semibold text-foreground">
                    {recoverableDraft.draft.title.trim() ||
                      t("entry.untitledDraft")}
                  </p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button
                      variant="primary"
                      size="sm"
                      onPress={() => openDraft(recoverableDraft.draftId)}
                    >
                      {t("entry.continueDraft")}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onPress={() => void handleDiscardRecovery()}
                    >
                      {t("entry.discardDraft")}
                    </Button>
                  </div>
                </section>
              )}

              <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
                <CreationMethodCard
                  eyebrow={t("entry.method.blank.eyebrow")}
                  title={t("entry.method.blank.title")}
                  description={t("entry.method.blank.description")}
                  onPress={handleBlankStart}
                />
                <CreationMethodCard
                  eyebrow={t("entry.method.ai.eyebrow")}
                  title={t("entry.method.ai.title")}
                  description={t("entry.method.ai.description")}
                  onPress={() => setMethod("ai")}
                />
                <CreationMethodCard
                  eyebrow={t("entry.method.cards.eyebrow")}
                  title={t("entry.method.cards.title")}
                  description={t("entry.method.cards.description")}
                  onPress={() => setMethod("cards")}
                />
              </div>
            </div>
          )}
        </Card.Content>
      </Card>
    </div>
  );
}

function CreationMethodCard({
  eyebrow,
  title,
  description,
  onPress,
}: {
  eyebrow: string;
  title: string;
  description: string;
  onPress: () => void;
}) {
  return (
    <button
      type="button"
      className="min-w-0 rounded-xl border border-border bg-surface p-4 text-left transition-colors hover:border-accent/50 hover:bg-accent/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
      onClick={onPress}
    >
      <span className="text-xs font-medium uppercase tracking-[0.14em] text-accent">
        {eyebrow}
      </span>
      <span className="mt-2 block text-base font-semibold text-foreground">
        {title}
      </span>
      <span className="mt-2 block text-sm leading-6 text-muted">
        {description}
      </span>
    </button>
  );
}
