"use client";

import {
  Fragment,
  useState,
  useEffect,
  useCallback,
  useRef,
} from "react";
import { useTranslations } from "next-intl";
import { useRouter, usePathname, useSearchParams } from "next/navigation";
import { Button } from "@heroui/react";
import { ApiError, apiGet, apiPost, apiPostRaw } from "@/lib/api";
import {
  deleteCardAvatarHandoffs,
  loadCardAvatarHandoff,
} from "@/lib/cardAvatarHandoff";
import {
  CardAvatarSourceUnavailable,
  isPermanentCardAvatarTransferFailure,
} from "@/lib/cardAvatarTransfer";
import type {
  CreateNovelRequest,
  NovelDetail,
  NovelRewriteFieldKey,
  WritingDraft,
  WritingDraftRewriteState,
} from "@/types/novel";
import { clearWritingDraft, loadWritingDraft, updateWritingDraft } from "@/lib/writingDraft";
import { normalizeRewriteState } from "@/lib/rewriteDraftState";
import NovelInfoSection, { type SectionKey } from "./NovelInfoSection";
import NovelRewriteAssistant from "./NovelRewriteAssistant";
import StickyActionBar from "./StickyActionBar";
import NovelCoverPanel from "./NovelCoverPanel";

const SECTIONS: SectionKey[] = ["basic", "creative", "scale", "content", "style"];

interface NovelInfoWorkspaceProps {
  mode: "create" | "edit";
  novelId?: string;
}

export default function NovelInfoWorkspace({ mode, novelId }: NovelInfoWorkspaceProps) {
  const tw = useTranslations("writing.novelInfo");
  const twd = useTranslations("writing");
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const locale = pathname.startsWith("/en") ? "en" : "zh";
  const draftId = searchParams.get("draft") || undefined;
  const didLoadDraftRef = useRef(false);

  /* state */
  const [data, setData] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(mode === "edit");
  const [loadError, setLoadError] = useState(false);
  const [noDraft, setNoDraft] = useState(false);
  const [editingSection, setEditingSection] = useState<SectionKey | null>(null);
  const [creating, setCreating] = useState(false);
  const [hasChapters, setHasChapters] = useState(false);

  /* danger confirm */
  const [showDangerModal, setShowDangerModal] = useState(false);
  const [dangerResolve, setDangerResolve] = useState<((v: boolean) => void) | null>(null);

  const persistCreateDraft = useCallback(
    (nextData: Record<string, unknown>) => {
      if (mode !== "create") {
        return;
      }

      updateWritingDraft(draftId, (draft) => ({
        ...draft,
        ...(nextData as Partial<WritingDraft>),
      }));
    },
    [draftId, mode],
  );

  const updateCreateData = useCallback(
    (updater: (prev: Record<string, unknown>) => Record<string, unknown>) => {
      setData((prev) => {
        const nextData = updater(prev);
        // 创建态表单是本地草稿驱动，任何字段和改写状态变化都立即写回草稿。
        persistCreateDraft(nextData);
        return nextData;
      });
    },
    [persistCreateDraft],
  );

  /* load data */
  const loadNovel = useCallback(async () => {
    if (!novelId) return;
    try {
      setLoading(true);
      setLoadError(false);
      const novel = await apiGet<NovelDetail>(`/api/novels/${novelId}`);
      setData(novel as unknown as Record<string, unknown>);
      setHasChapters((novel.stats?.chapter_count ?? 0) > 0);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 404) {
        router.replace(`/${locale}`);
        return;
      }
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [locale, novelId, router]);

  useEffect(() => {
    if (mode === "edit") {
      loadNovel();
      return;
    }

    if (didLoadDraftRef.current) {
      return;
    }
    didLoadDraftRef.current = true;

    const draft: WritingDraft | null = loadWritingDraft(draftId);
    if (draft) {
      setData(draft as unknown as Record<string, unknown>);
      setNoDraft(false);
    } else {
      setNoDraft(true);
    }
  }, [mode, loadNovel, draftId]);

  /* create novel */
  const handleCreate = async (openCardCuration: boolean) => {
    let createdNovelId: string | null = null;
    try {
      setCreating(true);
      const sourceDraft = data as Partial<WritingDraft>;
      const payload: CreateNovelRequest = {
        title: String(data.title || ""),
        subtitle: data.subtitle ? String(data.subtitle) : undefined,
        genre: data.genre ? String(data.genre) : undefined,
        tags: Array.isArray(data.tags) ? data.tags : undefined,
        introduction: data.introduction ? String(data.introduction) : undefined,
        summary: data.summary ? String(data.summary) : undefined,
        core_seed: data.core_seed ? String(data.core_seed) : undefined,
        worldview: data.worldview ? String(data.worldview) : undefined,
        writing_style: data.writing_style ? String(data.writing_style) : undefined,
        narrative_pov: data.narrative_pov ? String(data.narrative_pov) : undefined,
        era_background: data.era_background ? String(data.era_background) : undefined,
        cover_image: data.cover_image ? String(data.cover_image) : undefined,
        plot: data.plot ? String(data.plot) : undefined,
        tone: data.tone ? String(data.tone) : undefined,
        target_audience: data.target_audience ? String(data.target_audience) : undefined,
        core_idea: data.core_idea ? String(data.core_idea) : undefined,
        number_of_chapters: data.number_of_chapters ? Number(data.number_of_chapters) : undefined,
        words_per_chapter: data.words_per_chapter ? Number(data.words_per_chapter) : undefined,
        style_controls:
          typeof data.style_controls === "object" && data.style_controls !== null
            ? data.style_controls as CreateNovelRequest["style_controls"]
            : undefined,
        creation_mode:
          sourceDraft.creation_mode === "ai" ? "ai" : "manual",
        creative_direction: sourceDraft.creative_direction,
        card_creation_id: sourceDraft.card_creation_id,
        card_imports: sourceDraft.card_imports,
      };
      const res = await apiPost<{ id: string }>("/api/novels/create", payload);
      createdNovelId = res.id;
      const avatarProposalIds = sourceDraft.card_avatar_proposal_ids ?? [];
      const completedAvatarProposalIds: string[] = [];
      const permanentlyRejectedAvatarProposalIds: string[] = [];
      const retryableAvatarProposalIds: string[] = [];
      for (const proposalId of avatarProposalIds) {
        try {
          const source = await loadCardAvatarHandoff(proposalId);
          if (!source) {
            throw new CardAvatarSourceUnavailable();
          }
          await apiPostRaw(
            `/api/card-imports/proposals/${proposalId}/avatar`,
            source.blob,
            source.contentType,
          );
          completedAvatarProposalIds.push(proposalId);
        } catch (cause) {
          if (isPermanentCardAvatarTransferFailure(cause)) {
            permanentlyRejectedAvatarProposalIds.push(proposalId);
          } else {
            retryableAvatarProposalIds.push(proposalId);
          }
        }
      }
      await deleteCardAvatarHandoffs([
        ...completedAvatarProposalIds,
        ...permanentlyRejectedAvatarProposalIds,
      ]).catch(() => undefined);
      if (retryableAvatarProposalIds.length > 0) {
        updateCreateData((previous) => ({
          ...previous,
          card_avatar_proposal_ids: retryableAvatarProposalIds,
        }));
        alert(
          permanentlyRejectedAvatarProposalIds.length > 0
            ? tw("avatarTransferMixedFailure")
            : tw("avatarTransferFailed"),
        );
        return;
      }
      clearWritingDraft(draftId);
      if (permanentlyRejectedAvatarProposalIds.length > 0) {
        alert(tw("avatarTransferRejected"));
        router.push(
          `/${locale}/writing/${createdNovelId}?cardType=character`,
        );
        return;
      }
      const destinationParams = new URLSearchParams(searchParams.toString());
      destinationParams.delete("draft");
      destinationParams.delete("cardType");
      destinationParams.delete("curateCards");
      if (openCardCuration) {
        destinationParams.set("cardType", "character");
        destinationParams.set("curateCards", "1");
      }
      const destinationSearch = destinationParams.toString();
      router.push(
        `/${locale}/writing/${res.id}${
          destinationSearch ? `?${destinationSearch}` : ""
        }`,
      );
    } catch {
      alert(
        createdNovelId
          ? tw("avatarTransferFailed")
          : tw("createFailed"),
      );
    } finally {
      setCreating(false);
    }
  };

  /* field change (create mode) */
  const handleFieldChange = (field: string, value: unknown) => {
    updateCreateData((prev) => ({ ...prev, [field]: value }));
  };

  const handleRewriteStateChange = (rewriteState: WritingDraftRewriteState) => {
    updateCreateData((prev) => ({
      ...prev,
      _rewriteState: normalizeRewriteState(rewriteState),
    }));
  };

  const handleApplyRewrite = (
    field: NovelRewriteFieldKey,
    value: string | string[],
    rewriteState: WritingDraftRewriteState,
  ) => {
    updateCreateData((prev) => ({
      ...prev,
      [field]: value,
      _rewriteState: normalizeRewriteState(rewriteState),
    }));
  };

  const discardCreateDraft = async () => {
    const avatarProposalIds =
      (data as Partial<WritingDraft>).card_avatar_proposal_ids ?? [];
    await deleteCardAvatarHandoffs(avatarProposalIds).catch(
      () => undefined,
    );
    clearWritingDraft(draftId);
    router.push(`/${locale}`);
  };

  /* danger confirm promise */
  const requestDangerConfirm = (): Promise<boolean> => {
    return new Promise((resolve) => {
      setDangerResolve(() => resolve);
      setShowDangerModal(true);
    });
  };

  const handleDangerResponse = (confirmed: boolean) => {
    setShowDangerModal(false);
    dangerResolve?.(confirmed);
    setDangerResolve(null);
  };

  /* render */

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-muted">{twd("loading")}</p>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-3">
        <p className="text-muted">{twd("loadFailed")}</p>
        <Button variant="outline" onPress={loadNovel}>{tw("saveSection")}</Button>
      </div>
    );
  }

  if (mode === "create" && noDraft) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-3">
        <p className="text-muted">{tw("noDraft")}</p>
        <Button
          variant="outline"
          onPress={() => {
            clearWritingDraft(draftId);
            router.push(`/${locale}`);
          }}
        >
          {twd("backToCreate")}
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Danger Confirm Modal */}
      {showDangerModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-background rounded-xl shadow-xl p-6 max-w-md mx-4 border border-border">
            <div className="flex items-center gap-3 mb-3">
              <div className="w-10 h-10 rounded-full bg-warning/10 flex items-center justify-center shrink-0">
                <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-warning">
                  <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" />
                  <path d="M12 9v4" /><path d="M12 17h.01" />
                </svg>
              </div>
              <h3 className="text-lg font-semibold text-foreground">{twd("dangerEditTitle")}</h3>
            </div>
            <p className="text-sm text-muted mb-5">{twd("dangerEditMessage")}</p>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="sm" onPress={() => handleDangerResponse(false)}>
                {twd("cancel")}
              </Button>
              <Button variant="primary" size="sm" className="bg-accent text-white hover:bg-accent-hover" onPress={() => handleDangerResponse(true)}>
                {twd("confirmContinue")}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Header */}
      <div className="px-6 py-4 border-b border-border">
        <h2 className="text-lg font-bold text-foreground">
          {mode === "create" ? tw("createTitle") : tw("editTitle")}
        </h2>
        {mode === "create" && (
          <p className="text-sm text-muted mt-1">{tw("createDescription")}</p>
        )}
      </div>

      {/* Sections */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        {SECTIONS.map((sk) => (
          <Fragment key={sk}>
            <NovelInfoSection
              sectionKey={sk}
              data={data}
              novelId={novelId}
              isCreateMode={mode === "create"}
              isEditing={mode === "create" || editingSection === sk}
              onStartEdit={() => setEditingSection(sk)}
              onCancelEdit={() => setEditingSection(null)}
              onSaved={() => {
                setEditingSection(null);
                loadNovel();
              }}
              onChange={mode === "create" ? handleFieldChange : undefined}
              hasChapters={hasChapters}
              onDangerConfirm={requestDangerConfirm}
            />
            {sk === "basic" && mode === "edit" && novelId && (
              <NovelCoverPanel
                novelId={novelId}
                novelTitle={String(data.title || "")}
                coverAssetId={
                  data.cover_asset_id
                    ? String(data.cover_asset_id)
                    : null
                }
                coverImage={
                  data.cover_image ? String(data.cover_image) : null
                }
                hasUnsavedChanges={editingSection !== null}
                onCoverChanged={loadNovel}
              />
            )}
          </Fragment>
        ))}
      </div>

      {/* Sticky action bar - create mode */}
      {mode === "create" && (
        <StickyActionBar>
          <Button
            variant="ghost"
            onPress={() => void discardCreateDraft()}
          >
            {twd("backToCreate")}
          </Button>
          <Button
            variant="outline"
            onPress={() => void handleCreate(false)}
            isDisabled={creating || !data.title}
          >
            {creating ? tw("creating") : tw("saveOnly")}
          </Button>
          <Button
            variant="primary"
            onPress={() => void handleCreate(true)}
            isDisabled={creating || !data.title}
            className="bg-accent text-white hover:bg-accent-hover"
          >
            {creating ? tw("creating") : tw("saveAndCurate")}
          </Button>
        </StickyActionBar>
      )}

      {mode === "create" && (
        <NovelRewriteAssistant
          data={data}
          rewriteState={normalizeRewriteState(data._rewriteState as WritingDraftRewriteState | undefined)}
          onApplyRewrite={handleApplyRewrite}
          onRewriteStateChange={handleRewriteStateChange}
        />
      )}
    </div>
  );
}
