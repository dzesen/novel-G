"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiDelete, apiDownload, apiGet, apiPost, apiPut } from "@/lib/api";
import type {
  ChapterDetail,
  ChapterDraft,
  ChapterSummary,
  VolumeSummary,
} from "@/types/novel";
import ChapterEditorPane, { type ChapterSaveState } from "./ChapterEditorPane";
import ChapterNavigator from "./ChapterNavigator";
import BatchGenerationPanel from "./batch/BatchGenerationPanel";
import {
  chapterToDraft,
  clearLocalChapterDraft,
  countChapterWords,
  downloadTextFile,
  loadNewerLocalChapterDraft,
  saveLocalChapterDraft,
} from "./chapterUtils";
import VolumeOutlinePanel from "./outline/VolumeOutlinePanel";
import ChapterOutlinePanel from "./outline/ChapterOutlinePanel";
import type { StoredChapterOutline } from "./outline/outlineTypes";
import ProsePanel from "./prose/ProsePanel";
import { StateBackfillPanel } from "./state/StateBackfillPanel";

interface ChapterWorkspaceProps {
  mode: "create" | "edit";
  novelId?: string;
  onNavigateToMemory: () => void;
}

interface ListResponse<T> {
  data: T[];
}

export default function ChapterWorkspace({ mode, novelId, onNavigateToMemory }: ChapterWorkspaceProps) {
  const t = useTranslations("writing.chapterEditor");
  const tOutline = useTranslations("writing.outline");
  const tProse = useTranslations("writing.prose");
  // stateBackfill 是顶层命名空间（不在 writing 之下，见 T6 报告的偏离说明），
  // 必须单独取一份 translator，不能借用上面几个 writing.* 的 t()。
  const tStateBackfill = useTranslations("stateBackfill");
  const [volumes, setVolumes] = useState<VolumeSummary[]>([]);
  const [chapters, setChapters] = useState<ChapterSummary[]>([]);
  const [trash, setTrash] = useState<ChapterSummary[]>([]);
  const [selectedVolumeId, setSelectedVolumeId] = useState<string | null>(null);
  const [selectedChapterId, setSelectedChapterId] = useState<string | null>(null);
  const [draft, setDraft] = useState<ChapterDraft | null>(null);
  const [chapterOutline, setChapterOutline] = useState<StoredChapterOutline | undefined>();
  const [updatedAt, setUpdatedAt] = useState<string | undefined>();
  const [structureLoading, setStructureLoading] = useState(mode === "edit");
  const [structureError, setStructureError] = useState("");
  const [structureNotice, setStructureNotice] = useState("");
  const [chapterLoading, setChapterLoading] = useState(false);
  const [chapterLoadError, setChapterLoadError] = useState(false);
  const [saveState, setSaveState] = useState<ChapterSaveState>("idle");
  const [volumeOutlineOpen, setVolumeOutlineOpen] = useState(false);
  const [chapterOutlineOpen, setChapterOutlineOpen] = useState(false);
  const [proseOpen, setProseOpen] = useState(false);
  const [stateBackfillOpen, setStateBackfillOpen] = useState(false);
  const [stateBackfillBlocked, setStateBackfillBlocked] = useState("");
  const [batchStartScope, setBatchStartScope] = useState<"volume" | "book" | null>(null);

  const revisionRef = useRef(0);
  const selectedChapterIdRef = useRef<string | null>(null);
  const loadSequenceRef = useRef(0);
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve());

  const wordCount = useMemo(
    () => countChapterWords(draft?.content ?? ""),
    [draft?.content],
  );

  const loadStructure = useCallback(async (opts?: { silent?: boolean }) => {
    if (!novelId) return;
    if (!opts?.silent) {
      setStructureLoading(true);
      setStructureError("");
    }
    try {
      const [volumeResponse, chapterResponse, trashResponse] = await Promise.all([
        apiGet<ListResponse<VolumeSummary>>(`/api/volumes/novel/${novelId}`),
        apiGet<ListResponse<ChapterSummary>>(`/api/chapters/novel/${novelId}`),
        apiGet<ListResponse<ChapterSummary>>(`/api/chapters/novel/${novelId}/trash`),
      ]);
      const nextVolumes = [...volumeResponse.data].sort((a, b) => a.order_index - b.order_index);
      const nextChapters = [...chapterResponse.data].sort((a, b) => a.order_index - b.order_index);
      setVolumes(nextVolumes);
      setChapters(nextChapters);
      setTrash(trashResponse.data);
      setSelectedVolumeId((current) => {
        if (current && nextVolumes.some((volume) => volume._id === current)) return current;
        const selectedChapter = nextChapters.find(
          (chapter) => chapter._id === selectedChapterIdRef.current,
        );
        return selectedChapter?.volume_id ?? nextVolumes[0]?._id ?? null;
      });
      setSelectedChapterId((current) => {
        if (current && nextChapters.some((chapter) => chapter._id === current)) return current;
        const firstChapter = nextChapters[0]?._id ?? null;
        selectedChapterIdRef.current = firstChapter;
        return firstChapter;
      });
    } catch (error) {
      if (!opts?.silent) setStructureError(error instanceof Error ? error.message : t("loadFailed"));
    } finally {
      if (!opts?.silent) setStructureLoading(false);
    }
  }, [novelId, t]);

  const loadChapter = useCallback(async (chapterId: string) => {
    const sequence = ++loadSequenceRef.current;
    setChapterLoading(true);
    setChapterLoadError(false);
    try {
      const chapter = await apiGet<ChapterDetail>(`/api/chapters/${chapterId}`);
      if (sequence !== loadSequenceRef.current) return;
      const localDraft = loadNewerLocalChapterDraft(chapter);
      revisionRef.current = localDraft ? 1 : 0;
      setDraft(localDraft ?? chapterToDraft(chapter));
      setChapterOutline(chapter.outline);
      setUpdatedAt(chapter.updated_at);
      setSaveState(localDraft ? "dirty" : "idle");
      setSelectedVolumeId(chapter.volume_id);
    } catch {
      if (sequence === loadSequenceRef.current) {
        setChapterLoadError(true);
        setDraft(null);
        setChapterOutline(undefined);
      }
    } finally {
      if (sequence === loadSequenceRef.current) {
        setChapterLoading(false);
      }
    }
  }, []);

  /**
   * 接受细纲后只刷新 outline，**绝不能改走 loadChapter**。
   *
   * accept 会经 update_chapter 把 chapter.updated_at 顶到当前时刻；loadChapter 随后
   * 调 loadNewerLocalChapterDraft，它按 savedAt <= updated_at 判定本地草稿已过期，
   * 于是**删掉 localStorage 里的备份**并返回 null，正文随即被服务端副本覆盖、
   * saveState 归 idle——用户没保存的正文就这么没了，无提示无从恢复。
   *
   * 这条路径在两种能长期存在的状态下很容易走到：saveState 为 error（自动保存失败后
   * 不会重试）、以及标题为空（此时自动保存被整个跳过）。selectChapter 切章前会先
   * persistDraft 冲一次草稿，可见这个风险本就被代码承认；accept 是唯一漏掉那道保护的调用方。
   *
   * 这里不需要 loadSequenceRef 那道竞态守卫：本函数只写 chapterOutline 一个状态，
   * 与 loadChapter 争抢的 draft/saveState 都不碰；且它由用户点击"接受"触发，
   * 不像 loadChapter 那样会被选章切换连续打断。
   */
  const refreshChapterOutline = useCallback(async (chapterId: string) => {
    try {
      const chapter = await apiGet<ChapterDetail>(`/api/chapters/${chapterId}`);
      setChapterOutline(chapter.outline);
    } catch {
      // 细纲已在服务端落库，这里只是回读失败；不动正文状态，重开面板即可再读。
    }
  }, []);

  useEffect(() => {
    if (mode === "edit") void loadStructure();
  }, [loadStructure, mode]);

  useEffect(() => {
    selectedChapterIdRef.current = selectedChapterId;
    if (selectedChapterId) {
      void loadChapter(selectedChapterId);
    } else {
      setDraft(null);
      setUpdatedAt(undefined);
      setSaveState("idle");
    }
  }, [loadChapter, selectedChapterId]);

  const persistDraft = useCallback(
    (chapterId: string, snapshot: ChapterDraft, revision: number): Promise<void> => {
      const task = saveQueueRef.current
        .catch(() => undefined)
        .then(async () => {
          if (selectedChapterIdRef.current === chapterId) setSaveState("saving");
          try {
            await apiPut(`/api/chapters/${chapterId}`, snapshot);
            clearLocalChapterDraft(chapterId);
            const savedAt = new Date().toISOString();
            setChapters((current) =>
              current.map((chapter) =>
                chapter._id === chapterId
                  ? {
                      ...chapter,
                      title: snapshot.title,
                      summary: snapshot.summary,
                      status: snapshot.status,
                      word_count: countChapterWords(snapshot.content),
                      updated_at: savedAt,
                    }
                  : chapter,
              ),
            );
            if (selectedChapterIdRef.current === chapterId) {
              setUpdatedAt(savedAt);
              setSaveState(revisionRef.current === revision ? "saved" : "dirty");
            }
          } catch {
            if (selectedChapterIdRef.current === chapterId) setSaveState("error");
          }
        });
      saveQueueRef.current = task;
      return task;
    },
    [],
  );

  useEffect(() => {
    if (!selectedChapterId || !draft || !draft.title.trim() || saveState !== "dirty") return;
    const chapterId = selectedChapterId;
    const snapshot = draft;
    const revision = revisionRef.current;
    const timeoutId = window.setTimeout(() => {
      void persistDraft(chapterId, snapshot, revision);
    }, 900);
    return () => window.clearTimeout(timeoutId);
  }, [draft, persistDraft, saveState, selectedChapterId]);

  useEffect(() => {
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (saveState === "dirty" || saveState === "saving" || saveState === "error") {
        event.preventDefault();
      }
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [saveState]);

  const saveNow = useCallback(() => {
    if (!selectedChapterId || !draft || !draft.title.trim()) return;
    void persistDraft(selectedChapterId, draft, revisionRef.current);
  }, [draft, persistDraft, selectedChapterId]);

  /**
   * 显式冲一次草稿并**等待落库**，供状态回填面板在打开前调用。
   *
   * 状态回填在后端读 chapter.content（设计 §4.1），而正文可能还躺在
   * 900ms 防抖的自动保存队列里。不等这一下，AI 就会为**上一版正文**
   * 生成摘要与永久事实，且静默无感。
   *
   * 与 saveNow 的区别只在于**返回 Promise**：saveNow 是快捷键用的即发即忘。
   * 标题为空时 persistDraft 会被跳过（自动保存的既有约定），此时本函数
   * 返回 false，调用方必须据此拒绝打开面板——静默的空保存比不保存更危险。
   */
  const flushDraft = useCallback(async (): Promise<boolean> => {
    if (!selectedChapterId || !draft) return false;
    if (!draft.title.trim()) return false;
    if (saveState === "dirty" || saveState === "error") {
      await persistDraft(selectedChapterId, draft, revisionRef.current);
    }
    // saveState === "saving" 时自动保存已在途、上面的分支不会触发，但那份 PUT 仍可能
    // 未落库；无条件等一次保存队列排空，确保后端读到的正文是最新的（设计 §4.1）。
    // dirty/error 分支已 await 的 persistDraft 会把 saveQueueRef.current 指向自身任务，
    // 故这一行此时是已决议的 no-op；idle/saved 时队列本就空，同样是 no-op。
    await saveQueueRef.current;
    return true;
  }, [draft, persistDraft, saveState, selectedChapterId]);

  const openStateBackfill = useCallback(async () => {
    setStateBackfillBlocked("");
    const flushed = await flushDraft();
    if (!flushed) {
      // 标题为空 → 自动保存被跳过 → 库里的正文是旧的。如实拦住，不静默放行。
      setStateBackfillBlocked(tStateBackfill("needTitleToSave"));
      return;
    }
    setStateBackfillOpen(true);
  }, [flushDraft, tStateBackfill]);

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        saveNow();
      }
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [saveNow]);

  const changeDraft = (patch: Partial<ChapterDraft>) => {
    if (!selectedChapterId) return;
    revisionRef.current += 1;
    setDraft((current) => {
      if (!current) return current;
      const next = { ...current, ...patch };
      saveLocalChapterDraft(selectedChapterId, next);
      return next;
    });
    setSaveState("dirty");
  };

  const selectChapter = (chapterId: string) => {
    const previousChapterId = selectedChapterIdRef.current;
    if (previousChapterId === chapterId) return;
    if (
      previousChapterId &&
      draft &&
      draft.title.trim() &&
      (saveState === "dirty" || saveState === "error")
    ) {
      void persistDraft(previousChapterId, draft, revisionRef.current);
    }
    const chapter = chapters.find((item) => item._id === chapterId);
    if (chapter) setSelectedVolumeId(chapter.volume_id);
    setDraft(null);
    setChapterOutline(undefined);
    setUpdatedAt(undefined);
    setSaveState("idle");
    // 三个面板都持有 chapterId、以整容器覆盖的方式渲染：换章后若不关，
    // 面板会挂着上一章的 id 继续渲染（deleteChapter 已修过同一个坑）。
    setProseOpen(false);
    setChapterOutlineOpen(false);
    setStateBackfillOpen(false);
    setStateBackfillBlocked("");
    selectedChapterIdRef.current = chapterId;
    setSelectedChapterId(chapterId);
  };

  const createVolume = async (title: string) => {
    if (!novelId) return;
    setStructureError("");
    try {
      const response = await apiPost<{ id: string }>("/api/volumes/create", {
        novel_id: novelId,
        title,
      });
      await loadStructure();
      setSelectedVolumeId(response.id);
    } catch (error) {
      setStructureError(error instanceof Error ? error.message : t("createFailed"));
      throw error;
    }
  };

  const createChapter = async (volumeId: string) => {
    if (!novelId) return;
    setStructureError("");
    const index = chapters.filter((chapter) => chapter.volume_id === volumeId).length + 1;
    try {
      const response = await apiPost<{ id: string }>("/api/chapters/create", {
        novel_id: novelId,
        volume_id: volumeId,
        title: t("defaultChapterTitle", { index }),
        content: "",
      });
      await loadStructure();
      setSelectedVolumeId(volumeId);
      selectChapter(response.id);
    } catch (error) {
      setStructureError(error instanceof Error ? error.message : t("createFailed"));
      throw error;
    }
  };

  const restoreChapter = async (chapterId: string) => {
    setStructureError("");
    try {
      await apiPost(`/api/chapters/${chapterId}/restore`, {});
      await loadStructure();
      selectChapter(chapterId);
    } catch (error) {
      setStructureError(error instanceof Error ? error.message : t("restoreFailed"));
      throw error;
    }
  };

  const deleteChapter = async () => {
    if (!selectedChapterId) return;
    const deletedId = selectedChapterId;
    try {
      await apiDelete(`/api/chapters/${deletedId}`);
      clearLocalChapterDraft(deletedId);
      selectedChapterIdRef.current = null;
      setSelectedChapterId(null);
      // 这里绕开了 selectChapter，所以要手动补上它顺带做的面板复位：
      // 三个面板都以整容器覆盖的方式渲染，持有的 chapterId 会指向刚被删掉的章。
      setProseOpen(false);
      setChapterOutlineOpen(false);
      setStateBackfillOpen(false);
      setStateBackfillBlocked("");
      await loadStructure();
    } catch (error) {
      setStructureError(error instanceof Error ? error.message : t("deleteFailed"));
    }
  };

  const exportChapter = () => {
    if (!draft) return;
    const summary = draft.summary.trim() ? `\n${draft.summary.trim()}\n` : "";
    downloadTextFile(`${draft.title || t("untitled")}.txt`, `${draft.title}\n${summary}\n${draft.content}`);
  };

  const exportNovel = async () => {
    try {
      await apiDownload(`/api/backup/novel/${novelId}/text`, "novel.txt");
    } catch (error) {
      setStructureError(error instanceof Error ? error.message : t("exportFailed"));
    }
  };

  if (mode === "create" || !novelId) {
    return (
      <div className="flex h-full flex-col items-center justify-center bg-surface px-6 text-center">
        <svg aria-hidden="true" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" className="text-muted">
          <path d="M12 20h9" /><path d="M16.4 3.6a2.1 2.1 0 0 1 3 3L7.4 18.6l-3.8 1 1-3.8Z" />
        </svg>
        <h2 className="mt-4 text-base font-semibold text-foreground">{t("createModeTitle")}</h2>
        <p className="mt-1 max-w-md text-sm leading-6 text-muted">{t("createModeDescription")}</p>
      </div>
    );
  }

  return (
    <div className="relative flex h-full min-h-0 flex-col md:flex-row">
      <ChapterNavigator
        volumes={volumes}
        chapters={chapters}
        trash={trash}
        selectedChapterId={selectedChapterId}
        selectedVolumeId={selectedVolumeId}
        loading={structureLoading}
        onSelectChapter={selectChapter}
        onSelectVolume={setSelectedVolumeId}
        onCreateVolume={createVolume}
        onCreateChapter={createChapter}
        onRestoreChapter={restoreChapter}
        onOpenVolumeOutline={() => setVolumeOutlineOpen(true)}
        onStartVolumeJob={() => setBatchStartScope("volume")}
        onStartBookJob={() => setBatchStartScope("book")}
      />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <BatchGenerationPanel
          novelId={novelId}
          selectedVolumeId={selectedVolumeId}
          volumes={volumes}
          chapters={chapters}
          startScope={batchStartScope}
          onStartClose={() => setBatchStartScope(null)}
          onJumpToChapter={selectChapter}
          onQuietRefresh={() => void loadStructure({ silent: true })}
          onNavigateToMemory={onNavigateToMemory}
        />
        <ChapterEditorPane
          chapterId={selectedChapterId}
          draft={draft}
          wordCount={wordCount}
          updatedAt={updatedAt}
          loading={chapterLoading}
          loadError={chapterLoadError}
          saveState={saveState}
          onChange={changeDraft}
          onSave={saveNow}
          onRetryLoad={() => selectedChapterId && void loadChapter(selectedChapterId)}
          onDelete={deleteChapter}
          onExport={exportChapter}
          onExportNovel={() => void exportNovel()}
          onOpenChapterOutline={() => setChapterOutlineOpen(true)}
          onOpenProse={() => setProseOpen(true)}
          canGenerateProse={Boolean(chapterOutline)}
          onOpenStateBackfill={() => void openStateBackfill()}
          hasContent={Boolean(draft?.content?.trim())}
          stateBackfillBlocked={stateBackfillBlocked}
        />
      </div>

      {volumeOutlineOpen && novelId && (
        <VolumeOutlinePanel
          novelId={novelId}
          onClose={() => setVolumeOutlineOpen(false)}
          onAccepted={(result) => {
            // 面板接受后即关闭，成功反馈只能落在工作区里；否则用户只看到面板消失，
            // 无从确认到底建了几卷几章。
            setStructureNotice(
              tOutline("acceptSuccess", {
                volumes: result.volume_count,
                chapters: result.chapter_count,
              })
            );
            void loadStructure();
          }}
        />
      )}

      {chapterOutlineOpen && novelId && selectedChapterId && (
        <ChapterOutlinePanel
          novelId={novelId}
          chapterId={selectedChapterId}
          onClose={() => setChapterOutlineOpen(false)}
          onAccepted={() => selectedChapterId && void refreshChapterOutline(selectedChapterId)}
          existingOutline={chapterOutline}
        />
      )}

      {proseOpen && novelId && selectedChapterId && (
        <ProsePanel
          novelId={novelId}
          chapterId={selectedChapterId}
          hasExistingContent={Boolean(draft?.content?.trim())}
          onClose={() => setProseOpen(false)}
          onAccepted={(text) => {
            // **只写草稿**，落库交给既有自动保存（设计 §2）。
            // 这里绝不能像 accept 细纲那样回读服务端：那条路会把 updated_at 顶新、
            // 让 loadNewerLocalChapterDraft 判定本地草稿过期并删掉备份——
            // 整分支评审 Important #1 的原样重演。
            changeDraft({ content: text });
            setStructureNotice(tProse("acceptedNotice"));
          }}
        />
      )}

      {stateBackfillOpen && novelId && selectedChapterId && (
        <StateBackfillPanel
          novelId={novelId}
          chapterId={selectedChapterId}
          onClose={() => setStateBackfillOpen(false)}
          onAccepted={() => {
            // 摘要已由后端写库；回读章节列表让摘要与字数显示跟上。
            void loadStructure();
          }}
        />
      )}

      {structureNotice && (
        <div role="status" className="absolute bottom-4 left-1/2 z-30 flex max-w-[calc(100%-2rem)] -translate-x-1/2 items-center gap-3 rounded-lg border border-green-300 bg-green-50 px-4 py-2.5 text-sm text-green-800 shadow-lg dark:border-green-900 dark:bg-green-950 dark:text-green-200">
          <span className="min-w-0">{structureNotice}</span>
          <button type="button" onClick={() => setStructureNotice("")} className="shrink-0 text-xs font-medium underline">
            {t("dismiss")}
          </button>
        </div>
      )}

      {structureError && (
        <div role="alert" className="absolute bottom-4 left-1/2 z-30 flex max-w-[calc(100%-2rem)] -translate-x-1/2 items-center gap-3 rounded-lg border border-red-300 bg-red-50 px-4 py-2.5 text-sm text-red-800 shadow-lg dark:border-red-900 dark:bg-red-950 dark:text-red-200">
          <span className="min-w-0">{structureError}</span>
          <button type="button" onClick={() => setStructureError("")} className="shrink-0 text-xs font-medium underline">
            {t("dismiss")}
          </button>
        </div>
      )}
    </div>
  );
}
