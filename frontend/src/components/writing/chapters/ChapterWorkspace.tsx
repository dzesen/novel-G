"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import {
  ApiError,
  apiDelete,
  apiDownload,
  apiGet,
  apiPost,
  apiPut,
} from "@/lib/api";
import type {
  ChapterDetail,
  ChapterDraft,
  ChapterSummary,
  VolumeSummary,
} from "@/types/novel";
import ChapterEditorPane, { type ChapterSaveState } from "./ChapterEditorPane";
import ChapterNavigator from "./ChapterNavigator";
import ChapterAssistantPanel from "./ChapterAssistantPanel";
import ChapterContextInspector from "./ChapterContextInspector";
import ChapterWorkspaceLayout, { type ChapterWorkspaceLayoutControls } from "./ChapterWorkspaceLayout";
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
import type {
  AcceptVolumeOutlineResponse,
  StoredChapterOutline,
} from "./outline/outlineTypes";
import ProsePanel from "./prose/ProsePanel";
import JudgeReviewRecordsPanel from "./prose/JudgeReviewRecordsPanel";
import type { ProseRunSnapshot } from "./prose/useProseStream";
import SceneIllustrationPanel from "./SceneIllustrationPanel";
import type { LeftoverProseRun } from "./batch/batchTypes";
import BlueprintCompletionDialog from "./BlueprintCompletionDialog";

interface ChapterWorkspaceProps {
  mode: "create" | "edit";
  novelId?: string;
  onNavigateToReferenceCards: () => void;
  initialVolumeId?: string;
  initialChapterId?: string;
  initialSceneIndex?: number;
  initialRunId?: string;
  onChapterTargetChange: (chapterId: string) => void;
  onVolumeTargetValidation: (volumeId: string, valid: boolean) => void;
  onChapterTargetValidation: (chapterId: string, valid: boolean) => void;
  onSceneTargetValidation: (sceneIndex: number, valid: boolean) => void;
  onRunTargetValidation: (runId: string, valid: boolean) => void;
  onRunTargetChange: (runId?: string) => void;
  onOpenRunAudit: (chapterId: string, runId: string) => void;
  onOpenStateProposal: (chapterId: string) => void;
  onStructureAccepted: (
    nextRoute: AcceptVolumeOutlineResponse["next_route"],
  ) => void;
  onStartAutoBook: (
    scope: "volume" | "book",
    volumeId?: string,
    preferWorldAutoSupplement?: boolean,
  ) => void;
  proseOpenRequest?: ProseOpenRequest | null;
  onProseOpenRequestConsumed: () => void;
}

export interface ProseOpenRequest {
  requestId: number;
  chapterId: string;
  run: ProseRunSnapshot | null;
}

interface ListResponse<T> {
  data: T[];
}

interface ProseRunLocator {
  run_id: string;
  novel_id: string;
  chapter_id: string;
  status: string;
}

export default function ChapterWorkspace({
  mode,
  novelId,
  onNavigateToReferenceCards,
  initialVolumeId,
  initialChapterId,
  initialSceneIndex,
  initialRunId,
  onChapterTargetChange,
  onVolumeTargetValidation,
  onChapterTargetValidation,
  onSceneTargetValidation,
  onRunTargetValidation,
  onRunTargetChange,
  onOpenRunAudit,
  onOpenStateProposal,
  onStructureAccepted,
  onStartAutoBook,
  proseOpenRequest,
  onProseOpenRequestConsumed,
}: ChapterWorkspaceProps) {
  const t = useTranslations("writing.chapterEditor");
  const tOutline = useTranslations("writing.outline");
  const tProse = useTranslations("writing.prose");
  // stateBackfill 是顶层命名空间（不在 writing 之下，见 T6 报告的偏离说明），
  // 必须单独取一份 translator，不能借用上面几个 writing.* 的 t()。
  const tStateBackfill = useTranslations("stateBackfill");
  const [volumes, setVolumes] = useState<VolumeSummary[]>([]);
  const [volumeTrash, setVolumeTrash] = useState<VolumeSummary[]>([]);
  const [chapters, setChapters] = useState<ChapterSummary[]>([]);
  const [trash, setTrash] = useState<ChapterSummary[]>([]);
  const [selectedVolumeId, setSelectedVolumeId] = useState<string | null>(null);
  const [selectedChapterId, setSelectedChapterId] = useState<string | null>(
    initialChapterId ?? null,
  );
  const [draft, setDraft] = useState<ChapterDraft | null>(null);
  const [chapterOutline, setChapterOutline] = useState<StoredChapterOutline | undefined>();
  const [updatedAt, setUpdatedAt] = useState<string | undefined>();
  const [structureLoading, setStructureLoading] = useState(mode === "edit");
  const [structureLoadedNovelId, setStructureLoadedNovelId] =
    useState<string | null>(null);
  const [structureError, setStructureError] = useState("");
  const [structureNotice, setStructureNotice] = useState("");
  const [chapterLoading, setChapterLoading] = useState(false);
  const [chapterLoadError, setChapterLoadError] = useState(false);
  const [loadedChapterId, setLoadedChapterId] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<ChapterSaveState>("idle");
  const [volumeOutlineOpen, setVolumeOutlineOpen] = useState(false);
  const [blueprintCompletion, setBlueprintCompletion] =
    useState<AcceptVolumeOutlineResponse | null>(null);
  const [chapterOutlineOpen, setChapterOutlineOpen] = useState(false);
  const [proseOpen, setProseOpen] = useState(false);
  const [judgeReviewChapterId, setJudgeReviewChapterId] = useState<string | null>(null);
  const [sceneIllustrationOpen, setSceneIllustrationOpen] =
    useState(false);
  const [initialProseRun, setInitialProseRun] =
    useState<ProseRunSnapshot | null>(null);
  const [pendingProseOpen, setPendingProseOpen] = useState<{
    chapterId: string;
    run: ProseRunSnapshot | null;
  } | null>(null);
  const [stateBackfillBlocked, setStateBackfillBlocked] = useState("");
  const [runTargetAudit, setRunTargetAudit] =
    useState<ProseRunLocator | null>(null);
  const [runTargetLoadError, setRunTargetLoadError] = useState<{
    chapterId: string;
    runId: string;
    message: string;
  } | null>(null);
  const [runLookupRevision, setRunLookupRevision] = useState(0);

  const revisionRef = useRef(0);
  const selectedChapterIdRef = useRef<string | null>(initialChapterId ?? null);
  const initialVolumeIdRef = useRef(initialVolumeId);
  const initialChapterIdRef = useRef(initialChapterId);
  const structureRequestRef = useRef(0);
  const loadSequenceRef = useRef(0);
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const handledProseOpenRequestRef = useRef<number | null>(null);
  const handledInitialRunKeyRef = useRef<string | null>(null);
  const selectChapterRef = useRef<
    (chapterId: string, updateRoute?: boolean) => void
  >(() => undefined);

  initialVolumeIdRef.current = initialVolumeId;
  initialChapterIdRef.current = initialChapterId;

  const wordCount = useMemo(
    () => countChapterWords(draft?.content ?? ""),
    [draft?.content],
  );

  const loadStructure = useCallback(async (opts?: { silent?: boolean }) => {
    if (!novelId) return;
    const requestId = ++structureRequestRef.current;
    if (!opts?.silent) {
      setStructureLoading(true);
      setStructureError("");
    }
    try {
      const [volumeResponse, volumeTrashResponse, chapterResponse, trashResponse] = await Promise.all([
        apiGet<ListResponse<VolumeSummary>>(`/api/volumes/novel/${novelId}`),
        apiGet<ListResponse<VolumeSummary>>(`/api/volumes/novel/${novelId}/trash`),
        apiGet<ListResponse<ChapterSummary>>(`/api/chapters/novel/${novelId}`),
        apiGet<ListResponse<ChapterSummary>>(`/api/chapters/novel/${novelId}/trash`),
      ]);
      if (requestId !== structureRequestRef.current) return;
      const nextVolumes = [...volumeResponse.data].sort((a, b) => a.order_index - b.order_index);
      const nextChapters = [...chapterResponse.data].sort((a, b) => a.order_index - b.order_index);
      setVolumes(nextVolumes);
      setStructureLoadedNovelId(novelId);
      setVolumeTrash(volumeTrashResponse.data);
      setChapters(nextChapters);
      setTrash(trashResponse.data);
      const requestedVolumeId = initialVolumeIdRef.current;
      const requestedVolume = requestedVolumeId
        ? nextVolumes.find((volume) => volume._id === requestedVolumeId)
        : undefined;
      const requestedChapterId = initialChapterIdRef.current;
      const requestedChapter = requestedChapterId
        ? nextChapters.find((chapter) => chapter._id === requestedChapterId)
        : undefined;
      const requestedVolumeValid = Boolean(
        requestedVolume
        && (!requestedChapter || requestedChapter.volume_id === requestedVolumeId),
      );
      if (requestedVolumeId) {
        onVolumeTargetValidation(requestedVolumeId, requestedVolumeValid);
      }
      if (requestedChapterId) {
        onChapterTargetValidation(requestedChapterId, Boolean(requestedChapter));
      }
      const currentChapter = nextChapters.find(
        (chapter) => chapter._id === selectedChapterIdRef.current,
      );
      const nextSelectedChapter = requestedChapterId
        ? requestedChapter ?? null
        : requestedVolumeId
          ? requestedVolumeValid
            ? nextChapters.find(
                (chapter) => chapter.volume_id === requestedVolumeId,
              ) ?? null
            : null
          : currentChapter ?? nextChapters[0] ?? null;
      selectedChapterIdRef.current = nextSelectedChapter?._id ?? null;
      setSelectedChapterId(nextSelectedChapter?._id ?? null);
      setSelectedVolumeId(
        requestedVolumeValid
          ? requestedVolumeId ?? null
          : nextSelectedChapter?.volume_id ?? nextVolumes[0]?._id ?? null,
      );
    } catch (error) {
      if (requestId === structureRequestRef.current && !opts?.silent) {
        setStructureError(error instanceof Error ? error.message : t("loadFailed"));
      }
    } finally {
      if (requestId === structureRequestRef.current) {
        setStructureLoading(false);
      }
    }
  }, [
    novelId,
    onChapterTargetValidation,
    onVolumeTargetValidation,
    t,
  ]);

  const loadChapter = useCallback(async (chapterId: string) => {
    const sequence = ++loadSequenceRef.current;
    setChapterLoading(true);
    setChapterLoadError(false);
    setLoadedChapterId(null);
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
      setLoadedChapterId(chapterId);
    } catch {
      if (sequence === loadSequenceRef.current) {
        setChapterLoadError(true);
        setDraft(null);
        setChapterOutline(undefined);
        setLoadedChapterId(null);
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
    if (structureLoading || structureLoadedNovelId !== novelId) return;
    const requestedVolume = initialVolumeId
      ? volumes.find((volume) => volume._id === initialVolumeId)
      : undefined;
    const requestedChapter = initialChapterId
      ? chapters.find((chapter) => chapter._id === initialChapterId)
      : undefined;
    if (initialVolumeId) {
      const valid = Boolean(
        requestedVolume
        && (!requestedChapter || requestedChapter.volume_id === initialVolumeId),
      );
      onVolumeTargetValidation(initialVolumeId, valid);
      if (!valid) return;
    }
    if (initialChapterId) {
      const exists = Boolean(requestedChapter);
      onChapterTargetValidation(initialChapterId, exists);
      if (exists && selectedChapterIdRef.current !== initialChapterId) {
        selectChapterRef.current(initialChapterId, false);
      }
      return;
    }
    const firstChapterId = initialVolumeId
      ? chapters.find((chapter) => chapter.volume_id === initialVolumeId)?._id
      : chapters[0]?._id;
    if (initialVolumeId) setSelectedVolumeId(initialVolumeId);
    if (firstChapterId && selectedChapterIdRef.current !== firstChapterId) {
      selectChapterRef.current(firstChapterId, false);
    } else if (!firstChapterId && selectedChapterIdRef.current !== null) {
      selectedChapterIdRef.current = null;
      setSelectedChapterId(null);
    }
  }, [
    chapters,
    initialChapterId,
    initialVolumeId,
    onChapterTargetValidation,
    onVolumeTargetValidation,
    novelId,
    structureLoadedNovelId,
    structureLoading,
    volumes,
  ]);

  useEffect(() => {
    if (!initialRunId) {
      handledInitialRunKeyRef.current = null;
      return;
    }
    const runLookupKey = `${initialChapterId ?? ""}:${initialRunId}`;
    if (
      !initialChapterId ||
      !novelId ||
      structureLoadedNovelId !== novelId ||
      handledInitialRunKeyRef.current === runLookupKey
    ) {
      return;
    }
    setRunTargetAudit((current) =>
      current?.chapter_id === initialChapterId
      && current.run_id === initialRunId
        ? null
        : current,
    );
    setRunTargetLoadError((current) =>
      current?.chapterId === initialChapterId
      && current.runId === initialRunId
        ? null
        : current,
    );
    const suppliedRun = proseOpenRequest?.run;
    const suppliedRunId = suppliedRun?.run_id ?? suppliedRun?._id;
    if (
      suppliedRun &&
      suppliedRunId === initialRunId &&
      proseOpenRequest?.chapterId === initialChapterId
    ) {
      handledInitialRunKeyRef.current = runLookupKey;
      onRunTargetValidation(initialRunId, true);
      return;
    }

    let cancelled = false;
    void (async () => {
      let locator: ProseRunLocator;
      try {
        locator = await apiGet<ProseRunLocator>(
          `/api/llm/prose-runs/${encodeURIComponent(initialRunId)}/telemetry`,
        );
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && [400, 404].includes(error.status)) {
          handledInitialRunKeyRef.current = runLookupKey;
          onRunTargetValidation(initialRunId, false);
          return;
        }
        setRunTargetLoadError({
          chapterId: initialChapterId,
          runId: initialRunId,
          message: error instanceof Error ? error.message : t("loadFailed"),
        });
        return;
      }
      if (cancelled) return;
      const valid = locator.novel_id === novelId
        && locator.chapter_id === initialChapterId
        && locator.run_id === initialRunId;
      onRunTargetValidation(initialRunId, valid);
      if (!valid) {
        handledInitialRunKeyRef.current = runLookupKey;
        return;
      }

      if (["active", "complete"].includes(locator.status)) {
        let currentRun: ProseRunSnapshot | null;
        try {
          currentRun = await apiGet<ProseRunSnapshot | null>(
            `/api/llm/prose-runs/chapter/${encodeURIComponent(initialChapterId)}`,
          );
        } catch (error) {
          if (cancelled) return;
          setRunTargetLoadError({
            chapterId: initialChapterId,
            runId: initialRunId,
            message: error instanceof Error ? error.message : t("loadFailed"),
          });
          return;
        }
        if (cancelled) return;
        handledInitialRunKeyRef.current = runLookupKey;
        const currentRunId = currentRun?.run_id ?? currentRun?._id;
        if (currentRun && currentRunId === initialRunId) {
          setPendingProseOpen({ chapterId: initialChapterId, run: currentRun });
        } else {
          // 精确记录在两次读取之间离开 current 集合，仍保留为可审计对象。
          setRunTargetAudit(locator);
        }
        return;
      }

      if (!["incomplete", "superseded", "stale"].includes(locator.status)) {
        handledInitialRunKeyRef.current = runLookupKey;
        setRunTargetAudit(locator);
        return;
      }

      let leftovers: LeftoverProseRun[];
      try {
        leftovers = await apiGet<LeftoverProseRun[]>(
          `/api/llm/prose-runs/novel/${novelId}/leftovers`,
        );
      } catch (error) {
        if (cancelled) return;
        setRunTargetLoadError({
          chapterId: initialChapterId,
          runId: initialRunId,
          message: error instanceof Error ? error.message : t("loadFailed"),
        });
        return;
      }
      if (cancelled) return;
      handledInitialRunKeyRef.current = runLookupKey;
      const run = leftovers.find(
        (item) =>
          (item.run_id === initialRunId || item._id === initialRunId)
          && item.chapter_id === initialChapterId,
      );
      if (run) {
        setPendingProseOpen({ chapterId: initialChapterId, run });
      } else {
        // 状态可能在两次读取之间结束；精确记录仍保留为可见审计对象。
        setRunTargetAudit(locator);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    initialChapterId,
    initialRunId,
    novelId,
    onRunTargetValidation,
    proseOpenRequest,
    runLookupRevision,
    structureLoadedNovelId,
    t,
  ]);

  useEffect(() => {
    if (initialSceneIndex == null || !initialChapterId) return;
    if (loadedChapterId !== initialChapterId) return;
    const valid = Boolean(chapterOutline?.scenes?.[initialSceneIndex]);
    onSceneTargetValidation(initialSceneIndex, valid);
    if (!valid) return;
    setChapterOutlineOpen(true);
    setStructureNotice(
      t("evidenceSceneLocated", { scene: initialSceneIndex + 1 }),
    );
  }, [
    chapterOutline,
    initialChapterId,
    initialSceneIndex,
    loadedChapterId,
    onSceneTargetValidation,
    t,
  ]);

  useEffect(() => {
    selectedChapterIdRef.current = selectedChapterId;
    if (selectedChapterId) {
      void loadChapter(selectedChapterId);
    } else {
      setDraft(null);
      setLoadedChapterId(null);
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
    if (selectedChapterId) onOpenStateProposal(selectedChapterId);
  }, [flushDraft, onOpenStateProposal, selectedChapterId, tStateBackfill]);

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

  const selectChapter = (chapterId: string, updateRoute = true) => {
    const chapter = chapters.find((item) => item._id === chapterId);
    if (!chapter) {
      onChapterTargetValidation(chapterId, false);
      return;
    }
    onChapterTargetValidation(chapterId, true);
    if (updateRoute) onChapterTargetChange(chapterId);
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
    setSelectedVolumeId(chapter.volume_id);
    setDraft(null);
    setChapterOutline(undefined);
    setUpdatedAt(undefined);
    setSaveState("idle");
    // 这些章节面板都持有 chapterId、以整容器覆盖的方式渲染：换章后若不关，
    // 面板会挂着上一章的 id 继续渲染（deleteChapter 已修过同一个坑）。
    setProseOpen(false);
    setInitialProseRun(null);
    setPendingProseOpen(null);
    setChapterOutlineOpen(false);
    setJudgeReviewChapterId(null);
    setSceneIllustrationOpen(false);
    setStateBackfillBlocked("");
    selectedChapterIdRef.current = chapterId;
    setSelectedChapterId(chapterId);
  };
  selectChapterRef.current = selectChapter;

  useEffect(() => {
    if (
      !proseOpenRequest
      || handledProseOpenRequestRef.current === proseOpenRequest.requestId
    ) {
      return;
    }
    const timer = window.setTimeout(() => {
      if (handledProseOpenRequestRef.current === proseOpenRequest.requestId) {
        return;
      }
      handledProseOpenRequestRef.current = proseOpenRequest.requestId;
      if (selectedChapterIdRef.current !== proseOpenRequest.chapterId) {
        selectChapterRef.current(proseOpenRequest.chapterId);
      }
      setPendingProseOpen({
        chapterId: proseOpenRequest.chapterId,
        run: proseOpenRequest.run,
      });
      onProseOpenRequestConsumed();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [
    onProseOpenRequestConsumed,
    proseOpenRequest,
  ]);

  useEffect(() => {
    if (
      !pendingProseOpen
      || pendingProseOpen.chapterId !== selectedChapterId
      || chapterLoading
      || !draft
    ) {
      return;
    }
    setInitialProseRun(pendingProseOpen.run);
    setProseOpen(true);
    setPendingProseOpen(null);
  }, [chapterLoading, draft, pendingProseOpen, selectedChapterId]);

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

  const resetChapterPanels = () => {
    setProseOpen(false);
    setJudgeReviewChapterId(null);
    setChapterOutlineOpen(false);
    setSceneIllustrationOpen(false);
    setStateBackfillBlocked("");
  };

  const deleteVolume = async (volumeId: string) => {
    setStructureError("");
    try {
      const deletedChapterIds = chapters
        .filter((chapter) => chapter.volume_id === volumeId)
        .map((chapter) => chapter._id);
      await apiDelete(`/api/volumes/${volumeId}`);
      deletedChapterIds.forEach(clearLocalChapterDraft);
      if (selectedChapterId && deletedChapterIds.includes(selectedChapterId)) {
        selectedChapterIdRef.current = null;
        setSelectedChapterId(null);
        setDraft(null);
        resetChapterPanels();
      }
      await loadStructure();
    } catch (error) {
      setStructureError(error instanceof Error ? error.message : t("deleteVolumeFailed"));
      throw error;
    }
  };

  const restoreVolume = async (volumeId: string) => {
    setStructureError("");
    try {
      await apiPost(`/api/volumes/${volumeId}/restore`, {});
      await loadStructure();
      setSelectedVolumeId(volumeId);
    } catch (error) {
      setStructureError(error instanceof Error ? error.message : t("restoreVolumeFailed"));
      throw error;
    }
  };

  const hardDeleteVolume = async (volumeId: string) => {
    setStructureError("");
    try {
      await apiDelete(`/api/volumes/${volumeId}/hard`);
      await loadStructure();
    } catch (error) {
      setStructureError(error instanceof Error ? error.message : t("hardDeleteVolumeFailed"));
      throw error;
    }
  };

  const bulkDeleteChapters = async (chapterIds: string[]) => {
    if (!novelId || chapterIds.length === 0) return;
    setStructureError("");
    try {
      const result = await apiPost<{
        requested: number;
        deleted: string[];
        failed: Array<{ chapter_id: string; detail: string }>;
      }>("/api/chapters/bulk-delete", {
        novel_id: novelId,
        chapter_ids: chapterIds,
      });
      result.deleted.forEach(clearLocalChapterDraft);
      if (selectedChapterId && result.deleted.includes(selectedChapterId)) {
        selectedChapterIdRef.current = null;
        setSelectedChapterId(null);
        setDraft(null);
        resetChapterPanels();
      }
      await loadStructure();
      if (result.failed.length > 0) {
        throw new Error(
          t("bulkDeletePartialFailed", {
            deleted: result.deleted.length,
            failed: result.failed.length,
          }),
        );
      }
    } catch (error) {
      setStructureError(error instanceof Error ? error.message : t("bulkDeleteFailed"));
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
      resetChapterPanels();
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

  const renderAssistant = (controls: ChapterWorkspaceLayoutControls, compact = false) => (
    <ChapterAssistantPanel
      compact={compact}
      hasChapter={Boolean(selectedChapterId && draft && !chapterLoading && !chapterLoadError)}
      onOpenChapterOutline={() => {
        controls.closeDrawer();
        setChapterOutlineOpen(true);
      }}
      onOpenProse={() => {
        controls.closeDrawer();
        setPendingProseOpen(null);
        setInitialProseRun(null);
        setProseOpen(true);
      }}
      canGenerateProse={Boolean(chapterOutline)}
      onOpenSceneIllustration={() => {
        controls.closeDrawer();
        setSceneIllustrationOpen(true);
      }}
      canGenerateSceneIllustration={Boolean(chapterOutline)}
      onOpenStateBackfill={() => {
        controls.closeDrawer();
        void openStateBackfill();
      }}
      hasContent={Boolean(draft?.content?.trim())}
      onOpenJudgeReviews={() => {
        controls.closeDrawer();
        document.getElementById(controls.assistantTriggerId)?.focus();
        setJudgeReviewChapterId(selectedChapterId);
      }}
    />
  );

  const visibleRunTargetAudit =
    runTargetAudit?.chapter_id === initialChapterId
    && runTargetAudit?.run_id === initialRunId
      ? runTargetAudit
      : null;
  const visibleRunTargetLoadError =
    runTargetLoadError?.chapterId === initialChapterId
    && runTargetLoadError?.runId === initialRunId
      ? runTargetLoadError
      : null;

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      {visibleRunTargetAudit && initialChapterId && (
        <div
          role="status"
          className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-sky-300 bg-sky-50 px-4 py-3 text-sm text-sky-950 dark:border-sky-900 dark:bg-sky-950/40 dark:text-sky-100 sm:px-6"
        >
          <div className="min-w-0">
            <p className="font-semibold">{t("runAuditTitle")}</p>
            <p className="mt-0.5 break-words text-xs leading-5">
              {t("runAuditBody")}
            </p>
            <code className="mt-1 block max-w-full overflow-x-auto text-[11px]">
              {visibleRunTargetAudit.run_id}
            </code>
          </div>
          <button
            type="button"
            onClick={() =>
              onOpenRunAudit(initialChapterId, visibleRunTargetAudit.run_id)
            }
            className="min-h-9 shrink-0 rounded-md border border-sky-400 bg-white/70 px-3 text-xs font-semibold hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent dark:border-sky-800 dark:bg-sky-950"
          >
            {t("openRunAudit")}
          </button>
        </div>
      )}

      {visibleRunTargetLoadError && (
        <div
          role="alert"
          className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200 sm:px-6"
        >
          <span className="min-w-0 break-words">
            {t("runLookupFailed", {
              run: visibleRunTargetLoadError.runId,
              error: visibleRunTargetLoadError.message,
            })}
          </span>
          <button
            type="button"
            onClick={() => {
              handledInitialRunKeyRef.current = null;
              setRunTargetLoadError(null);
              setRunLookupRevision((current) => current + 1);
            }}
            className="min-h-9 shrink-0 rounded-md border border-red-300 px-3 text-xs font-semibold hover:bg-red-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent dark:border-red-800 dark:hover:bg-red-950"
          >
            {t("retry")}
          </button>
        </div>
      )}

      <div className="min-h-0 flex-1">
        <ChapterWorkspaceLayout
          renderDirectory={(controls) => (
            <ChapterNavigator
              volumes={volumes}
              deletedVolumes={volumeTrash}
              chapters={chapters}
              trash={trash}
              selectedChapterId={selectedChapterId}
              selectedVolumeId={selectedVolumeId}
              loading={structureLoading}
              onSelectChapter={(chapterId) => {
                selectChapter(chapterId);
                controls.closeDrawer();
              }}
              onSelectVolume={setSelectedVolumeId}
              onCreateVolume={createVolume}
              onCreateChapter={async (volumeId) => {
                await createChapter(volumeId);
                controls.closeDrawer();
              }}
              onRestoreChapter={async (chapterId) => {
                await restoreChapter(chapterId);
                controls.closeDrawer();
              }}
              onDeleteVolume={deleteVolume}
              onRestoreVolume={restoreVolume}
              onHardDeleteVolume={hardDeleteVolume}
              onBulkDeleteChapters={bulkDeleteChapters}
              onOpenVolumeOutline={() => {
                controls.closeDrawer();
                setVolumeOutlineOpen(true);
              }}
              onStartVolumeJob={() => {
                controls.closeDrawer();
                onStartAutoBook("volume", selectedVolumeId ?? undefined);
              }}
              onStartBookJob={() => {
                controls.closeDrawer();
                onStartAutoBook("book");
              }}
            />
          )}
          renderEditor={(controls) => (
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
              stateBackfillBlocked={stateBackfillBlocked}
              workspaceControls={controls}
              assistantActions={renderAssistant(controls, true)}
            />
          )}
          renderContext={() => (
            <ChapterContextInspector
              chapterId={selectedChapterId}
              draft={draft}
              outline={chapterOutline}
              wordCount={wordCount}
              saveState={saveState}
              updatedAt={updatedAt}
            />
          )}
          renderAssistant={renderAssistant}
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
            setBlueprintCompletion(result);
          }}
        />
      )}

      {blueprintCompletion && (
        <BlueprintCompletionDialog
          volumeCount={blueprintCompletion.volume_count}
          chapterCount={blueprintCompletion.chapter_count}
          onManual={() => setBlueprintCompletion(null)}
          onVolume={() => {
            const firstVolumeId = blueprintCompletion.volume_ids[0];
            setBlueprintCompletion(null);
            onStartAutoBook("volume", firstVolumeId, true);
          }}
          onBook={() => {
            setBlueprintCompletion(null);
            onStartAutoBook("book", undefined, true);
          }}
          onReviewWorld={() => {
            const nextRoute = blueprintCompletion.next_route;
            setBlueprintCompletion(null);
            onStructureAccepted(nextRoute);
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

      {judgeReviewChapterId && judgeReviewChapterId === selectedChapterId && draft && (
        <JudgeReviewRecordsPanel
          key={judgeReviewChapterId}
          chapterId={judgeReviewChapterId}
          chapterTitle={draft.title}
          onClose={() => setJudgeReviewChapterId(null)}
        />
      )}

      {proseOpen && novelId && selectedChapterId && (
        <ProsePanel
          novelId={novelId}
          chapterId={selectedChapterId}
          initialRun={initialProseRun}
          hasExistingContent={Boolean(draft?.content?.trim())}
          onClose={() => {
            setProseOpen(false);
            setPendingProseOpen(null);
            setInitialProseRun(null);
            if (initialRunId) onRunTargetChange(undefined);
          }}
          onRunStateChanged={() => onRunTargetChange(undefined)}
          onAccepted={(text, acceptanceState) => {
            // ProseRun accept 已通过可恢复的 mutation journal 写入正式正文；
            // 这里同步当前编辑器草稿，后续自动保存只会幂等写回同一份内容。不能立刻用 loadChapter
            // 回读：它会把 updated_at 顶新，并可能把未保存的其他编辑器字段误判
            // 为过期本地草稿后删除。
            changeDraft({
              content: text,
              ...(acceptanceState === "partial_manual_required" || acceptanceState === "author_confirmed"
                ? { status: "writing" as const }
                : {}),
            });
            setStructureNotice(tProse(
              acceptanceState === "partial_manual_required"
                ? "acceptedPartialNotice"
                : acceptanceState === "author_confirmed"
                  ? "acceptedAuthorNotice"
                : "acceptedCompleteNotice",
            ));
          }}
        />
      )}

      {sceneIllustrationOpen &&
        novelId &&
        selectedChapterId &&
        chapterOutline &&
        draft && (
          <SceneIllustrationPanel
            novelId={novelId}
            chapterId={selectedChapterId}
            chapterTitle={draft.title}
            chapterOutline={chapterOutline}
            onClose={() => setSceneIllustrationOpen(false)}
            onOpenCharacterCards={onNavigateToReferenceCards}
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
