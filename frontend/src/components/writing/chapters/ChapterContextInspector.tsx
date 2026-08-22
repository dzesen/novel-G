"use client";

import { useTranslations } from "next-intl";
import type { ChapterDraft } from "@/types/novel";
import type { ChapterSaveState } from "./ChapterEditorPane";
import type { StoredChapterOutline } from "./outline/outlineTypes";

interface ChapterContextInspectorProps {
  chapterId: string | null;
  draft: ChapterDraft | null;
  outline?: StoredChapterOutline;
  wordCount: number;
  saveState: ChapterSaveState;
  updatedAt?: string;
}

function StatusMark({ ready }: { ready: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={[
        "mt-1 h-2 w-2 shrink-0 rounded-full",
        ready ? "bg-emerald-600 dark:bg-emerald-400" : "bg-amber-500",
      ].join(" ")}
    />
  );
}

export default function ChapterContextInspector({
  chapterId,
  draft,
  outline,
  wordCount,
  saveState,
  updatedAt,
}: ChapterContextInspectorProps) {
  const t = useTranslations("writing.chapterEditor");
  const tw = useTranslations("writing.chapterEditor.workspace");

  if (!chapterId || !draft) {
    return (
      <div className="grid h-full min-h-56 place-items-center px-6 text-center">
        <div>
          <p className="text-sm font-semibold text-foreground">{tw("contextEmptyTitle")}</p>
          <p className="mt-1 text-xs leading-5 text-muted">{tw("contextEmptyDescription")}</p>
        </div>
      </div>
    );
  }

  const referencedCharacters = outline
    ? new Set([
        ...(outline.present_character_card_ids ?? []),
        ...(outline.mentioned_character_card_ids ?? []),
      ]).size
    : 0;

  return (
    <div className="h-full overflow-y-auto px-5 py-5">
      <div>
        <h2 className="text-base font-semibold text-foreground">{tw("context")}</h2>
        <p className="mt-1 text-xs leading-5 text-muted">{tw("contextDescription")}</p>
      </div>

      <section className="mt-6 border-t border-border pt-4">
        <div className="flex items-start gap-2">
          <StatusMark ready={Boolean(outline)} />
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-foreground">{tw("outlineContext")}</h3>
            <p className="mt-1 text-xs leading-5 text-muted">
              {outline ? tw("outlineReady") : tw("outlineMissing")}
            </p>
          </div>
        </div>
        {outline && (
          <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 text-xs">
            <div>
              <dt className="text-muted">{tw("sceneCount")}</dt>
              <dd className="mt-0.5 font-semibold tabular-nums text-foreground">{outline.scenes.length}</dd>
            </div>
            <div>
              <dt className="text-muted">{tw("targetWords")}</dt>
              <dd className="mt-0.5 font-semibold tabular-nums text-foreground">{outline.target_word_count}</dd>
            </div>
            <div>
              <dt className="text-muted">{tw("characterReferences")}</dt>
              <dd className="mt-0.5 font-semibold tabular-nums text-foreground">{referencedCharacters}</dd>
            </div>
            <div>
              <dt className="text-muted">{tw("worldReferences")}</dt>
              <dd className="mt-0.5 font-semibold tabular-nums text-foreground">{outline.referenced_worldbook_card_ids.length}</dd>
            </div>
          </dl>
        )}
      </section>

      <section className="mt-5 border-t border-border pt-4">
        <div className="flex items-start gap-2">
          <StatusMark ready={Boolean(draft.content.trim())} />
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-foreground">{tw("proseContext")}</h3>
            <p className="mt-1 text-xs leading-5 text-muted">
              {draft.content.trim() ? tw("proseReady") : tw("proseEmpty")}
            </p>
          </div>
        </div>
        <dl className="mt-4 space-y-3 text-xs">
          <div className="flex items-center justify-between gap-4">
            <dt className="text-muted">{tw("currentWords")}</dt>
            <dd className="font-semibold tabular-nums text-foreground">{wordCount}</dd>
          </div>
          <div className="flex items-center justify-between gap-4">
            <dt className="text-muted">{t("status")}</dt>
            <dd className="font-semibold text-foreground">{t(`statuses.${draft.status}`)}</dd>
          </div>
          <div className="flex items-center justify-between gap-4">
            <dt className="text-muted">{tw("saveStatus")}</dt>
            <dd className="text-right font-semibold text-foreground">{t(`saveState.${saveState}`)}</dd>
          </div>
        </dl>
        {updatedAt && (
          <p className="mt-4 text-[11px] leading-5 text-muted">
            {t("lastSaved", { time: new Date(updatedAt).toLocaleString() })}
          </p>
        )}
      </section>

      {draft.summary.trim() && (
        <section className="mt-5 border-t border-border pt-4">
          <h3 className="text-sm font-semibold text-foreground">{tw("summaryContext")}</h3>
          <p className="mt-2 whitespace-pre-wrap text-xs leading-6 text-muted">{draft.summary}</p>
        </section>
      )}
    </div>
  );
}
