"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { apiPost } from "@/lib/api";
import { useOutlineStream } from "./useOutlineStream";
import { useRoster } from "./useRoster";
import RosterPicker from "./RosterPicker";
import OutlineGenerationParams, {
  EMPTY_GENERATION_PARAMS,
  toRequestParams,
  type GenerationParams,
} from "./OutlineGenerationParams";
import { ContextNotices, Field, Notice } from "./outlineUi";
import type {
  AcceptChapterOutlineResponse,
  ChapterOutlineResult,
  NewThread,
  Scene,
  StoredChapterOutline,
} from "./outlineTypes";

interface ChapterOutlinePanelProps {
  novelId: string;
  chapterId: string;
  onClose: () => void;
  onAccepted: () => void;
  existingOutline?: StoredChapterOutline;
}

export default function ChapterOutlinePanel({
  novelId,
  chapterId,
  onClose,
  onAccepted,
  existingOutline,
}: ChapterOutlinePanelProps) {
  const t = useTranslations("writing.outline");
  const roster = useRoster(novelId);
  const stream = useOutlineStream<ChapterOutlineResult>({
    path: "/api/llm/create-chapter-outline-by-ai",
    stepKey: "chapter_outline",
  });
  const [params, setParams] = useState<GenerationParams>(EMPTY_GENERATION_PARAMS);
  const [accepting, setAccepting] = useState(false);
  const [acceptError, setAcceptError] = useState("");
  const [orphanThreadIds, setOrphanThreadIds] = useState<string[]>([]);
  // 设计 §5.1：用户动过任何字段即置 true，不做深比较。
  const [dirty, setDirty] = useState(false);
  // 已接受的细纲不可编辑后直接回贴（见文件顶部 accept 的 schema 约束），重新生成
  // 会整份覆盖它并让上次创建的伏笔成为孤儿，所以用二次确认挡一下误点。
  const [regenerateArmed, setRegenerateArmed] = useState(false);

  const outline = stream.result;

  const patch = (next: Partial<ChapterOutlineResult>) => {
    setDirty(true);
    stream.setResult((current) => (current ? { ...current, ...next } : current));
  };

  // dirty 描述的是"**当前这份**细纲有没有被人动过"，所以只在真有新细纲顶替旧的那一刻
  // 清掉它——即 resultVersion 前进的那一刻，而不是"点了生成"的那一刻。
  //
  // 两个方向都会错，两个都踩过：
  // - 永不清零 → "改一处 → 重新生成 → 直接接受"把人类从未看过的 AI 产物标成已人工编辑；
  // - 点击时就清零 → 生成被取消或失败时屏幕上留着的仍是那份改过的旧细纲（start 刻意
  //   不清 result），却已被标成未编辑，接受时反过来漏报。
  useEffect(() => {
    setDirty(false);
  }, [stream.resultVersion]);

  const startGeneration = () => {
    void stream.start({
      novel_id: novelId,
      chapter_id: chapterId,
      ...toRequestParams(params),
    });
  };

  const discardPreview = () => {
    setDirty(false);
    setRegenerateArmed(false);
    stream.reset();
  };

  // 二次确认必须"一次只放行一次生成"。丢弃预览、生成失败、生成中途取消——三条路
  // 都会让面板退回到"只显示已接受的细纲"那个状态，此时若 armed 还留着 true，
  // 下一次就变成一键直接重新生成，保险栓等于白装。故凡是退回只读态都重新上栓。
  //
  // 取消走的是 idle 不是 error（见 useOutlineStream 的 AbortError 分支：主动取消
  // 不算失败），只判 error 会漏掉它——而取消按钮就摆在生成过程中的表头上，是最容易走到的一条。
  // 上栓本身不改 stream.status，所以本effect不会把刚点下的 armed 又抹掉。
  useEffect(() => {
    if (stream.status === "error" || stream.status === "idle") setRegenerateArmed(false);
  }, [stream.status]);

  const accept = async () => {
    if (!outline) return;
    setAccepting(true);
    setAcceptError("");
    try {
      const response = await apiPost<AcceptChapterOutlineResponse>(
        `/api/chapters/${chapterId}/accept-outline`,
        { outline, edited_by_human: dirty }
      );
      // 设计 §7.4：孤儿伏笔必须显示，后端已如实返回，前端吞掉等于让
      // "不掩盖"止步于 API 边界。
      if (response.previous_thread_ids.length > 0) {
        setOrphanThreadIds(response.previous_thread_ids);
        void roster.reload();
      } else {
        onClose();
      }
      onAccepted();
    } catch (err) {
      setAcceptError(err instanceof Error ? err.message : String(err));
    } finally {
      setAccepting(false);
    }
  };

  const busy = stream.status === "running" || accepting;

  return (
    <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/25 px-4 py-6">
      <div className="flex max-h-full w-full max-w-5xl flex-col rounded-md border border-border bg-surface shadow-lg">
        <header className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h3 className="text-base font-semibold text-foreground">{t("chapterTitle")}</h3>
            <p className="mt-1 text-xs leading-5 text-muted">{t("chapterDescription")}</p>
          </div>
          <div className="flex shrink-0 gap-2">
            {stream.status === "running" ? (
              <Button variant="outline" size="sm" onPress={stream.cancel}>
                {t("cancel")}
              </Button>
            ) : existingOutline && !outline && !regenerateArmed ? (
              <Button
                variant="outline"
                size="sm"
                onPress={() => setRegenerateArmed(true)}
                isDisabled={busy}
              >
                {t("regenerate")}
              </Button>
            ) : (
              <Button
                variant="primary"
                size="sm"
                className="bg-accent text-white hover:bg-accent-hover"
                onPress={startGeneration}
                isDisabled={busy}
              >
                {existingOutline && !outline ? t("confirmRegenerate") : outline ? t("regenerate") : t("generate")}
              </Button>
            )}
            <Button variant="ghost" size="sm" onPress={onClose} isDisabled={accepting}>
              {t("close")}
            </Button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <div className="mb-4">
            <OutlineGenerationParams value={params} onChange={setParams} />
          </div>

          {roster.error && <Notice tone="warning">{t("rosterLoadFailed", { error: roster.error })}</Notice>}

          {existingOutline && !outline && (
            <section className="mb-4 rounded-md border border-border bg-background p-4">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <h4 className="text-sm font-semibold text-foreground">{t("existingTitle")}</h4>
                <div className="flex gap-2 text-xs text-muted">
                  {existingOutline.generated_at && (
                    <span className="rounded-md border border-border px-2 py-0.5">
                      {t("existingGeneratedAt", { time: new Date(existingOutline.generated_at).toLocaleString() })}
                    </span>
                  )}
                  <span className="rounded-md border border-border px-2 py-0.5">
                    {existingOutline.edited_by_human ? t("existingEditedByHuman") : t("existingAiOnly")}
                  </span>
                </div>
              </div>
              <p className="mb-3 text-xs leading-5 text-amber-700 dark:text-amber-300">{t("existingHint")}</p>
              {regenerateArmed && (
                <p className="mb-3 text-xs leading-5 text-amber-700 dark:text-amber-300">{t("regenerateWarning")}</p>
              )}
              <dl className="grid gap-2 text-sm">
                <ReadOnlyIds label={t("fieldPov")} ids={existingOutline.pov_character_card_id ? [existingOutline.pov_character_card_id] : []} nameById={roster.nameById} />
                <ReadOnlyIds label={t("fieldPresent")} ids={existingOutline.present_character_card_ids} nameById={roster.nameById} />
                <ReadOnlyIds label={t("fieldMentioned")} ids={existingOutline.mentioned_character_card_ids} nameById={roster.nameById} />
                <ReadOnlyIds label={t("fieldWorldbook")} ids={existingOutline.referenced_worldbook_card_ids} nameById={roster.nameById} />
                <ReadOnlyIds label={t("threadsPlanted")} ids={existingOutline.threads_planted} nameById={roster.nameById} />
                <ReadOnlyIds label={t("fieldThreadsResolved")} ids={existingOutline.threads_resolved} nameById={roster.nameById} />
                <ReadOnlyText label={t("fieldCoreConflict")} value={existingOutline.core_conflict} />
                <ReadOnlyText label={t("fieldEndingHook")} value={existingOutline.ending_hook} />
                <ReadOnlyText label={t("fieldTargetWords")} value={String(existingOutline.target_word_count)} />
              </dl>
              <div className="mt-3 grid gap-2">
                <span className="text-xs font-medium text-muted">{t("fieldScenes")}</span>
                {existingOutline.scenes.map((scene, index) => (
                  <div key={index} className="rounded-md border border-border bg-surface px-3 py-2 text-xs leading-5 text-foreground">
                    <p>{scene.summary}</p>
                    <p className="mt-1 text-muted">{scene.purpose}</p>
                  </div>
                ))}
              </div>
              <p className="mt-3 rounded-md border border-border bg-surface px-3 py-2 text-xs leading-5 text-muted">
                {t("threadsResolvedNotice")}
              </p>
            </section>
          )}

          <ContextNotices report={stream.contextReport} />
          {stream.droppedIds && Object.keys(stream.droppedIds).length > 0 && (
            <Notice tone="warning">
              {t("droppedIdsWarning", {
                count: Object.values(stream.droppedIds).reduce((sum, ids) => sum + ids.length, 0),
                detail: Object.entries(stream.droppedIds)
                  .map(([field, ids]) => `${field}: ${ids.join(", ")}`)
                  .join("；"),
              })}
            </Notice>
          )}
          {orphanThreadIds.length > 0 && (
            <Notice tone="warning">
              {t("orphanThreadsNotice", {
                count: orphanThreadIds.length,
                names: orphanThreadIds.map((id) => roster.nameById[id] ?? id).join("、"),
              })}
            </Notice>
          )}
          {stream.error && <Notice tone="error">{stream.error}</Notice>}
          {acceptError && <Notice tone="error">{acceptError}</Notice>}

          {stream.status === "running" && !outline && (
            <p className="py-10 text-center text-sm text-muted">{t("generating")}</p>
          )}
          {!outline && stream.status !== "running" && (
            <p className="py-10 text-center text-sm text-muted">{t("emptyPreview")}</p>
          )}

          {outline && (
            <div className="grid gap-4">
              <div className="grid gap-3 rounded-md border border-border bg-background p-4">
                <RosterPicker
                  mode="single"
                  label={t("fieldPov")}
                  options={roster.characters}
                  value={outline.pov_character_card_id}
                  onChange={(next) => patch({ pov_character_card_id: next as string | null })}
                  emptyText={t("rosterEmpty")}
                />
                <RosterPicker
                  mode="multi"
                  label={t("fieldPresent")}
                  options={roster.characters}
                  value={outline.present_character_card_ids}
                  onChange={(next) => patch({ present_character_card_ids: next as string[] })}
                  emptyText={t("rosterEmpty")}
                />
                <RosterPicker
                  mode="multi"
                  label={t("fieldMentioned")}
                  options={roster.characters}
                  value={outline.mentioned_character_card_ids}
                  onChange={(next) => patch({ mentioned_character_card_ids: next as string[] })}
                  emptyText={t("rosterEmpty")}
                />
                <RosterPicker
                  mode="multi"
                  label={t("fieldWorldbook")}
                  options={roster.worldbook}
                  value={outline.referenced_worldbook_card_ids}
                  onChange={(next) => patch({ referenced_worldbook_card_ids: next as string[] })}
                  emptyText={t("rosterEmpty")}
                />
                <RosterPicker
                  mode="multi"
                  label={t("fieldThreadsResolved")}
                  options={roster.threads}
                  value={outline.threads_resolved}
                  onChange={(next) => patch({ threads_resolved: next as string[] })}
                  emptyText={t("rosterEmpty")}
                />
                {/* 设计 §7.3：这个行为极易被误认成 bug——接受了细纲却发现伏笔没消失。 */}
                <p className="rounded-md border border-border bg-surface px-3 py-2 text-xs leading-5 text-muted">
                  {t("threadsResolvedNotice")}
                </p>
              </div>

              <div className="grid gap-3 rounded-md border border-border bg-background p-4">
                <Field label={t("fieldCoreConflict")}>
                  <textarea
                    value={outline.core_conflict}
                    rows={2}
                    onChange={(e) => patch({ core_conflict: e.target.value })}
                    className="w-full resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm leading-5 text-foreground outline-none focus:border-accent"
                  />
                </Field>
                <Field label={t("fieldEndingHook")}>
                  <textarea
                    value={outline.ending_hook}
                    rows={2}
                    onChange={(e) => patch({ ending_hook: e.target.value })}
                    className="w-full resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm leading-5 text-foreground outline-none focus:border-accent"
                  />
                </Field>
                <Field label={t("fieldTargetWords")}>
                  <input
                    type="number"
                    value={outline.target_word_count}
                    onChange={(e) => patch({ target_word_count: Number(e.target.value) })}
                    className="min-h-9 w-40 rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
                  />
                </Field>
              </div>

              <RowEditor<Scene>
                title={t("fieldScenes")}
                rows={outline.scenes}
                addLabel={t("addScene")}
                removeLabel={t("removeRow")}
                onChange={(scenes) => patch({ scenes })}
                blank={{ summary: "", purpose: "" }}
                render={(scene, update) => (
                  <div className="grid gap-3 md:grid-cols-2">
                    <Field label={t("fieldSceneSummary")}>
                      <textarea
                        value={scene.summary}
                        rows={2}
                        onChange={(e) => update({ summary: e.target.value })}
                        className="w-full resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm leading-5 text-foreground outline-none focus:border-accent"
                      />
                    </Field>
                    <Field label={t("fieldScenePurpose")}>
                      <textarea
                        value={scene.purpose}
                        rows={2}
                        onChange={(e) => update({ purpose: e.target.value })}
                        className="w-full resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm leading-5 text-foreground outline-none focus:border-accent"
                      />
                    </Field>
                  </div>
                )}
              />

              <RowEditor<NewThread>
                title={t("fieldNewThreads")}
                rows={outline.new_threads}
                addLabel={t("addThread")}
                removeLabel={t("removeRow")}
                onChange={(new_threads) => patch({ new_threads })}
                blank={{ name: "", description: "", due_chapter_order: null, importance: "sub" }}
                render={(thread, update) => (
                  <div className="grid gap-3 md:grid-cols-2">
                    <Field label={t("fieldThreadName")}>
                      <input
                        value={thread.name}
                        onChange={(e) => update({ name: e.target.value })}
                        className="min-h-9 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
                      />
                    </Field>
                    <Field label={t("fieldThreadDue")}>
                      <input
                        type="number"
                        value={thread.due_chapter_order ?? ""}
                        onChange={(e) =>
                          update({ due_chapter_order: e.target.value ? Number(e.target.value) : null })
                        }
                        className="min-h-9 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
                      />
                    </Field>
                    <Field label={t("fieldThreadDescription")}>
                      <textarea
                        value={thread.description}
                        rows={2}
                        onChange={(e) => update({ description: e.target.value })}
                        className="w-full resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm leading-5 text-foreground outline-none focus:border-accent"
                      />
                    </Field>
                    <Field label={t("fieldThreadImportance")}>
                      <select
                        value={thread.importance}
                        onChange={(e) => update({ importance: e.target.value as NewThread["importance"] })}
                        className="min-h-9 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
                      >
                        <option value="main">{t("importanceMain")}</option>
                        <option value="sub">{t("importanceSub")}</option>
                      </select>
                    </Field>
                  </div>
                )}
              />
            </div>
          )}
        </div>

        <footer className="flex justify-end gap-2 border-t border-border px-5 py-3">
          <Button variant="ghost" size="sm" onPress={discardPreview} isDisabled={!outline || busy}>
            {t("discard")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            className="bg-accent text-white hover:bg-accent-hover"
            onPress={() => void accept()}
            isDisabled={!outline || busy}
          >
            {accepting ? t("accepting") : t("accept")}
          </Button>
        </footer>
      </div>
    </div>
  );
}

function RowEditor<T>({
  title,
  rows,
  addLabel,
  removeLabel,
  blank,
  onChange,
  render,
}: {
  title: string;
  rows: T[];
  addLabel: string;
  removeLabel: string;
  blank: T;
  onChange: (rows: T[]) => void;
  render: (row: T, update: (patch: Partial<T>) => void) => React.ReactNode;
}) {
  return (
    <section className="rounded-md border border-border bg-background p-4">
      <div className="mb-3 flex items-center justify-between">
        <h4 className="text-sm font-semibold text-foreground">{title}</h4>
        <button
          type="button"
          onClick={() => onChange([...rows, blank])}
          className="rounded-md border border-border px-2 py-1 text-xs text-muted hover:bg-surface-secondary hover:text-foreground"
        >
          {addLabel}
        </button>
      </div>
      <div className="grid gap-3">
        {rows.map((row, index) => (
          <div key={index} className="rounded-md border border-border bg-surface p-3">
            {render(row, (rowPatch) => {
              const next = [...rows];
              next[index] = { ...next[index], ...rowPatch };
              onChange(next);
            })}
            <div className="mt-2 flex justify-end">
              <button
                type="button"
                onClick={() => onChange(rows.filter((_, i) => i !== index))}
                className="text-xs text-red-600 hover:underline dark:text-red-400"
              >
                {removeLabel}
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function ReadOnlyIds({
  label,
  ids,
  nameById,
}: {
  label: string;
  ids: string[];
  nameById: Record<string, string>;
}) {
  if (ids.length === 0) return null;
  return (
    <div className="grid gap-1">
      <dt className="text-xs font-medium text-muted">{label}</dt>
      <dd className="flex flex-wrap gap-1.5">
        {ids.map((id) => (
          <span key={id} className="rounded-md border border-border px-2 py-0.5 text-xs text-foreground">
            {nameById[id] ?? id}
          </span>
        ))}
      </dd>
    </div>
  );
}

function ReadOnlyText({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div className="grid gap-1">
      <dt className="text-xs font-medium text-muted">{label}</dt>
      <dd className="text-sm leading-5 text-foreground">{value}</dd>
    </div>
  );
}
