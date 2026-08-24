"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { apiPost } from "@/lib/api";
import { useOutlineStream } from "../outline/useOutlineStream";
import { useRoster } from "../outline/useRoster";
import {
  ContextNotices,
  Field,
  Notice,
  ReferenceCleanupNotice,
  ReferenceRemapNotice,
} from "../outline/outlineUi";
import type {
  ChapterStateAcceptResponse,
  ChapterStateResult,
  PermanentFactProposal,
  StateFactDropReason,
} from "./stateTypes";

interface StateBackfillPanelProps {
  novelId: string;
  chapterId: string;
  chapterLabel?: string;
  closeLabel?: string;
  onClose: () => void;
  onAccepted: () => void;
}

/** 事实的渲染键；正式选择与丢弃原因始终使用服务端 selection_id。 */
const factKey = (cardId: string, index: number, fact: PermanentFactProposal) =>
  `${cardId}::${index}::${fact.fact}`;

const toggleSetValue = (current: Set<string>, value: string) => {
  const next = new Set(current);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
};

/**
 * 后端在正文为空时返回的原始 400 错误文案（见 state_router.py 的
 * `extract_chapter_state_by_ai`）。该文案不经 i18n，是后端固定吐出的中文串；
 * 命中时用本地化的 needContent 顶替，避免英文界面下直接漏出后端原文。
 */
const NEED_CONTENT_BACKEND_MESSAGE = "本章还没有已保存的正文，请先写好并保存正文";

/**
 * AI 状态回填面板：预览 → 逐项勾选 → 接受。
 *
 * 复用 outline 链路的 `useOutlineStream`（帧形状一致，只是 path/payload/stepKey
 * 不同）与 `useRoster`（id → 名称），不新写一套事件机——`useOutlineStream` 内的
 * `runIdRef` 陈旧守卫是修过 Critical 的既有正确性保障。
 *
 * 本组件由连续性与状态模块持有；章节编辑器只负责保存当前草稿并导航到这里。
 */
export function StateBackfillPanel({
  novelId,
  chapterId,
  chapterLabel,
  closeLabel,
  onClose,
  onAccepted,
}: StateBackfillPanelProps) {
  const t = useTranslations("stateBackfill");
  const roster = useRoster(novelId);
  const stream = useOutlineStream<ChapterStateResult>({
    path: "/api/llm/extract-chapter-state-by-ai",
    stepKey: "chapter_state",
  });

  const [checkedFacts, setCheckedFacts] = useState<Set<string>>(new Set());
  const [checkedCharacters, setCheckedCharacters] = useState<Set<string>>(new Set());
  const [checkedThreads, setCheckedThreads] = useState<Set<string>>(new Set());
  const [dropReasons, setDropReasons] = useState<Record<string, StateFactDropReason>>({});
  const [accepting, setAccepting] = useState(false);
  const [acceptError, setAcceptError] = useState("");
  const [acceptResult, setAcceptResult] = useState<ChapterStateAcceptResponse | null>(null);

  // 每当**流**送来一份新结果就重置勾选：永久事实回到全不勾（不可逆，强制 opt-in），
  // 伏笔回到全勾（可改，opt-out）。用 resultVersion 而不是 status——生成可能被
  // 取消或失败，那时屏幕上留着的仍是旧那一份，不该被重置。
  useEffect(() => {
    if (!stream.result) return;
    setCheckedFacts(new Set());
    setCheckedCharacters(new Set(
      stream.result.character_updates
        .map((item) => item.selection_id)
        .filter((id): id is string => Boolean(id))
    ));
    setCheckedThreads(new Set(
      stream.result.thread_updates
        .map((item) => item.selection_id)
        .filter((id): id is string => Boolean(id))
    ));
    setDropReasons({});
    setAcceptResult(null);
    setAcceptError("");
    // 只在**流**送来新结果时重置勾选（由 resultVersion 追踪）；刻意不把 stream.result
    // 放进依赖，避免同一候选的其他本地状态变化清空勾选与跳过原因。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream.resultVersion]);

  // 六条约束之二：busy 只由生成状态与接受状态派生，consistency_issues 从不参与——
  // 一致性冲突只是标红提示，不得阻断接受（设计 §6.3）。
  const busy = stream.status === "running" || accepting;

  const startGeneration = () => {
    void stream.start({ novel_id: novelId, chapter_id: chapterId });
  };

  const clearDropReason = (selectionId: string) => {
    setDropReasons((current) => {
      const next = { ...current };
      delete next[selectionId];
      return next;
    });
  };

  const toggleFact = (selectionId: string) => {
    clearDropReason(selectionId);
    setCheckedFacts((current) => toggleSetValue(current, selectionId));
  };

  const toggleCharacter = (selectionId: string) => {
    clearDropReason(selectionId);
    setCheckedCharacters((current) => toggleSetValue(current, selectionId));
  };

  const toggleThread = (selectionId: string) => {
    clearDropReason(selectionId);
    setCheckedThreads((current) => toggleSetValue(current, selectionId));
  };

  const setDropReason = (selectionId: string, value: string) => {
    setDropReasons((current) => {
      const next = { ...current };
      if (value) next[selectionId] = value as StateFactDropReason;
      else delete next[selectionId];
      return next;
    });
  };

  const dropReasonField = (selectionId: string) => (
    <label className="mt-2 block min-w-0 text-xs text-muted">
      <span className="mb-1 block font-medium text-foreground">{t("dropReasonLabel")}</span>
      <select
        value={dropReasons[selectionId] ?? ""}
        onChange={(event) => setDropReason(selectionId, event.target.value)}
        className="w-full min-w-0 rounded-md border border-border bg-background px-3 py-2 text-base text-foreground outline-none focus:border-accent sm:text-sm"
      >
        <option value="">{t("dropReasonPlaceholder")}</option>
        <option value="unsupported_by_prose">{t("dropReasonUnsupported")}</option>
        <option value="duplicate_existing_fact">{t("dropReasonExistingDuplicate")}</option>
        <option value="duplicate_proposal">{t("dropReasonProposalDuplicate")}</option>
        <option value="legal_no_op">{t("dropReasonNoOp")}</option>
      </select>
    </label>
  );

  const factKindLabel = (kind: PermanentFactProposal["kind"]) => {
    switch (kind) {
      case "death":
        return t("factKindDeath");
      case "injury":
        return t("factKindInjury");
      case "identity":
        return t("factKindIdentity");
      case "relation":
        return t("factKindRelation");
      case "ability":
        return t("factKindAbility");
      default:
        return kind;
    }
  };

  // 角色/伏笔一律按名称显示；roster 里查不到时标注为不可识别对象，
  // 但不把只能由数据库或开发工具定位的内部 id 暴露给作者。
  const characterName = (cardId: string | null) => {
    if (!cardId) return t("unknownCharacter");
    return roster.nameById[cardId] ?? t("unknownCharacter");
  };

  const threadName = (threadId: string) =>
    roster.nameById[threadId] ?? t("unknownThread");

  const result = stream.result;
  const selectionIds = result
    ? [
        ...result.character_updates.map((item) => item.selection_id),
        ...result.character_updates.flatMap((item) =>
          item.new_permanent_facts.map((fact) => fact.selection_id)
        ),
        ...result.thread_updates.map((item) => item.selection_id),
      ]
    : [];
  const hasMissingSelectionIds = selectionIds.some((id) => !id);
  const hasDroppedIds = Object.values(stream.droppedIds ?? {}).some(
    (ids) => ids.length > 0
  );
  const hasFactEvidenceBlocker = Boolean(
    result
      && (
        hasDroppedIds
        || result.fact_evidence.extraction_status === "unknown"
        || result.fact_evidence.invalid_internal_references > 0
        || result.fact_evidence.dangling_references > 0
      )
  );
  const unselectedIds = result
    ? [
        ...result.character_updates
          .map((item) => item.selection_id)
          .filter(
            (id): id is string =>
              typeof id === "string" && id.length > 0 && !checkedCharacters.has(id)
          ),
        ...result.character_updates.flatMap((item) =>
          item.new_permanent_facts
            .map((fact) => fact.selection_id)
            .filter(
              (id): id is string =>
                typeof id === "string" && id.length > 0 && !checkedFacts.has(id)
            )
        ),
        ...result.thread_updates
          .map((item) => item.selection_id)
          .filter(
            (id): id is string =>
              typeof id === "string" && id.length > 0 && !checkedThreads.has(id)
          ),
      ]
    : [];
  const hasUnexplainedDrops = unselectedIds.some((id) => !dropReasons[id]);

  const accept = async () => {
    if (!stream.result) return;
    if (!stream.result.proposal_id || !stream.result.acceptance_token) {
      setAcceptError(t("stalePreview"));
      return;
    }
    setAccepting(true);
    setAcceptError("");
    try {
      const response = await apiPost<ChapterStateAcceptResponse>(
        "/api/llm/accept-chapter-state",
        {
          chapter_id: chapterId,
          proposal_id: stream.result.proposal_id,
          acceptance_token: stream.result.acceptance_token,
          selected_character_ids: stream.result.character_updates
            .filter((item) => item.selection_id && checkedCharacters.has(item.selection_id))
            .map((item) => item.selection_id)
            .filter((id): id is string => Boolean(id)),
          selected_fact_ids: stream.result.character_updates.flatMap((update) =>
            update.new_permanent_facts
              .filter((fact) => fact.selection_id && checkedFacts.has(fact.selection_id))
              .map((fact) => fact.selection_id)
              .filter((id): id is string => Boolean(id))
          ),
          selected_thread_ids: stream.result.thread_updates
            .filter((item) => item.selection_id && checkedThreads.has(item.selection_id))
            .map((item) => item.selection_id)
            .filter((id): id is string => Boolean(id)),
          drop_reasons: dropReasons,
        }
      );
      setAcceptResult(response);
      onAccepted();
      // 有跳过项时**不关面板**：去重不得静默，而关掉的面板等于静默（设计 §5.3）。
      if (response.skipped_duplicate_facts.length === 0) onClose();
    } catch (err) {
      setAcceptError(err instanceof Error ? err.message : String(err));
    } finally {
      setAccepting(false);
    }
  };

  const noStateChanges = result
    ? result.character_updates.length === 0 && result.thread_updates.length === 0
    : false;

  return (
    <div
      data-testid="state-proposal-workspace"
      className="flex h-full min-h-0 min-w-0 flex-col bg-background"
    >
        <header className="flex flex-col gap-3 border-b border-border bg-surface px-4 py-4 sm:flex-row sm:items-start sm:justify-between sm:px-6">
          <div className="min-w-0">
            <h3 className="text-base font-semibold text-foreground">{t("title")}</h3>
            {chapterLabel && (
              <p className="mt-1 truncate text-xs text-muted">{chapterLabel}</p>
            )}
          </div>
          <div className="flex shrink-0 flex-wrap gap-2">
            {stream.status === "running" ? (
              <Button variant="outline" size="sm" onPress={stream.cancel}>
                {t("cancel")}
              </Button>
            ) : (
              <Button
                variant="primary"
                size="sm"
                className="bg-accent text-white hover:bg-accent-hover"
                onPress={startGeneration}
                isDisabled={busy}
              >
                {result ? t("regenerate") : t("generate")}
              </Button>
            )}
            <Button variant="ghost" size="sm" onPress={onClose} isDisabled={accepting}>
              {closeLabel ?? t("close")}
            </Button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
          {roster.error && <Notice tone="warning">{roster.error}</Notice>}

          {/* context / id_remapping / id_validation 都先于结果帧到达并即时可见。 */}
          <ContextNotices report={stream.contextReport} />
          <ReferenceRemapNotice
            remappedReferences={stream.remappedReferences}
            nameById={roster.nameById}
          />
          <ReferenceCleanupNotice droppedIds={stream.droppedIds} />

          {stream.error && (
            <Notice tone="error">
              {stream.error === NEED_CONTENT_BACKEND_MESSAGE ? t("needContent") : stream.error}
            </Notice>
          )}
          {acceptError && <Notice tone="error">{acceptError}</Notice>}

          {/* 六条约束之四：接受成功后显示 acceptSuccess；skipped_duplicate_facts
              非空时额外显示 skippedDuplicates（是否关面板已在 accept() 里处理）。 */}
          {acceptResult && (
            <div className="mb-3 rounded-md border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-700 dark:border-green-900/60 dark:bg-green-950/40 dark:text-green-300">
              <p>
                {t("acceptSuccess", {
                  states: acceptResult.states_updated,
                  facts: acceptResult.facts_appended,
                  threads: acceptResult.threads_updated,
                })}
              </p>
              {acceptResult.skipped_duplicate_facts.length > 0 && (
                <p className="mt-1">
                  {t("skippedDuplicates", {
                    count: acceptResult.skipped_duplicate_facts.length,
                    facts: acceptResult.skipped_duplicate_facts.join(", "),
                  })}
                </p>
              )}
            </div>
          )}

          {stream.status === "running" && !result && (
            <p className="py-10 text-center text-sm text-muted">{t("running")}</p>
          )}

          {stream.status !== "running" && !result && !stream.error && (
            <div className="flex min-h-48 flex-col items-center justify-center px-4 text-center">
              <p className="text-sm font-medium text-foreground">
                {t("emptyTitle")}
              </p>
              <p className="mt-2 max-w-[60ch] text-xs leading-5 text-muted">
                {t("emptyDescription")}
              </p>
            </div>
          )}

          {result && (
            <div className="grid gap-4">
              <p className="rounded-md border border-border bg-surface px-3 py-2 text-xs leading-5 text-muted">
                {t("evidenceLockedHint")}
              </p>
              <div className="rounded-md border border-border bg-background p-4">
                <Field label={t("summaryLabel")}>
                  <textarea
                    value={result.summary}
                    rows={3}
                    readOnly
                    className="w-full resize-y rounded-md border border-border bg-surface px-3 py-2 text-base leading-6 text-foreground sm:text-sm sm:leading-5"
                  />
                </Field>
              </div>

              {noStateChanges && (
                <p className="rounded-md border border-border bg-background px-3 py-2 text-sm text-muted">
                  {t("emptyResult")}
                </p>
              )}

              {result.character_updates.length > 0 && (
                <section className="grid gap-3 rounded-md border border-border bg-background p-4">
                  <h4 className="text-sm font-semibold text-foreground">{t("charactersLabel")}</h4>
                  <div className="grid gap-3">
                    {result.character_updates.map((update) => (
                      <div
                        key={update.card_id}
                        className="rounded-md border border-border bg-surface p-3"
                      >
                        <p className="mb-2 text-sm font-medium text-foreground">
                          {characterName(update.card_id)}
                        </p>
                        <div className="grid gap-1">
                          <label className="flex min-w-0 items-start gap-2 text-xs font-medium text-muted">
                            <input
                              type="checkbox"
                              checked={Boolean(
                                update.selection_id
                                && checkedCharacters.has(update.selection_id)
                              )}
                              disabled={!update.selection_id}
                              onChange={() => {
                                if (update.selection_id) toggleCharacter(update.selection_id);
                              }}
                              className="mt-0.5 h-4 w-4 shrink-0 accent-[var(--color-accent)]"
                            />
                            <span className="min-w-0">{t("currentStateLabel")}</span>
                          </label>
                          <textarea
                            value={update.current_state}
                            rows={2}
                            readOnly
                            disabled={Boolean(
                              update.selection_id
                              && !checkedCharacters.has(update.selection_id)
                            )}
                            aria-label={t("currentStateLabel")}
                            className="w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-base leading-6 text-foreground disabled:cursor-not-allowed disabled:opacity-60 sm:text-sm sm:leading-5"
                          />
                          {update.selection_id
                            && !checkedCharacters.has(update.selection_id)
                            && dropReasonField(update.selection_id)}
                        </div>
                        {update.new_permanent_facts.length > 0 && (
                          <div className="mt-2 grid gap-1.5">
                            <span className="text-xs font-medium text-muted">
                              {t("factsLabel")}
                            </span>
                            {update.new_permanent_facts.map((fact, factIndex) => {
                              const key = factKey(update.card_id, factIndex, fact);
                              const selectionId = fact.selection_id ?? "";
                              const selected = Boolean(
                                selectionId && checkedFacts.has(selectionId)
                              );
                              return (
                                <div
                                  key={key}
                                  className="rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground"
                                >
                                  <label className="flex min-w-0 items-start gap-2">
                                    <input
                                      type="checkbox"
                                      checked={selected}
                                      disabled={!selectionId}
                                      onChange={() => {
                                        if (selectionId) toggleFact(selectionId);
                                      }}
                                      className="mt-0.5 h-4 w-4 shrink-0 accent-[var(--color-accent)]"
                                    />
                                    <span className="min-w-0 break-words">
                                      <span className="mr-1.5 rounded-md border border-border px-1.5 py-0.5 text-xs text-muted">
                                        {factKindLabel(fact.kind)}
                                      </span>
                                      {fact.fact}
                                    </span>
                                  </label>
                                  {selectionId && !selected && dropReasonField(selectionId)}
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </section>
              )}

              {result.thread_updates.length > 0 && (
                <section className="grid gap-3 rounded-md border border-border bg-background p-4">
                  <h4 className="text-sm font-semibold text-foreground">{t("threadsLabel")}</h4>
                  <div className="grid gap-2">
                    {result.thread_updates.map((item) => {
                      const selectionId = item.selection_id ?? "";
                      const selected = Boolean(
                        selectionId && checkedThreads.has(selectionId)
                      );
                      return (
                        <div
                          key={item.thread_id}
                          className="rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground"
                        >
                          <label className="flex min-w-0 items-start gap-2">
                            <input
                              type="checkbox"
                              checked={selected}
                              disabled={!selectionId}
                              onChange={() => {
                                if (selectionId) toggleThread(selectionId);
                              }}
                              className="mt-0.5 h-4 w-4 shrink-0 accent-[var(--color-accent)]"
                            />
                            <span className="min-w-0 flex-1 break-words">
                              <span className="mr-1.5 font-medium">{threadName(item.thread_id)}</span>
                              <span className="mr-1.5 rounded-md border border-border px-1.5 py-0.5 text-xs text-muted">
                                {item.status === "resolved"
                                  ? t("threadResolved")
                                  : t("threadDeveloping")}
                              </span>
                              {item.evidence && (
                                <span className="mt-1 block text-xs text-muted">
                                  <span className="font-medium">{t("evidenceLabel")}: </span>
                                  {item.evidence}
                                </span>
                              )}
                            </span>
                          </label>
                          {selectionId && !selected && dropReasonField(selectionId)}
                        </div>
                      );
                    })}
                  </div>
                </section>
              )}

              {/* 六条约束之二：一致性冲突标红只读展示，不出现在任何禁用判据里。 */}
              <section className="grid gap-2 rounded-md border border-border bg-background p-4">
                <h4 className="text-sm font-semibold text-foreground">{t("issuesLabel")}</h4>
                {result.consistency_issues.length === 0 ? (
                  <p className="text-xs text-muted">{t("issuesEmpty")}</p>
                ) : (
                  result.consistency_issues.map((issue, index) => (
                    <Notice key={index} tone="error">
                      <p className="font-medium">{characterName(issue.card_id)}</p>
                      <p className="mt-1">{issue.fact}</p>
                      <p className="mt-1 text-xs">
                        <span className="font-medium">{t("conflictLabel")}: </span>
                        {issue.conflict}
                      </p>
                    </Notice>
                  ))
                )}
              </section>
            </div>
          )}
        </div>

        <footer className="flex flex-col gap-2 border-t border-border bg-surface px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-6">
          <p
            role={
              hasMissingSelectionIds || hasFactEvidenceBlocker || hasUnexplainedDrops
                ? "alert"
                : undefined
            }
            className="min-w-0 text-xs leading-5 text-muted"
          >
            {hasMissingSelectionIds
              ? t("stalePreview")
              : hasFactEvidenceBlocker
                ? t("factAccountingBlocked")
                : hasUnexplainedDrops
                  ? t("dropReasonRequired")
                  : result
                    ? t("factAccountingReady")
                    : ""}
          </p>
          <Button
            variant="primary"
            size="sm"
            className="bg-accent text-white hover:bg-accent-hover"
            onPress={() => void accept()}
            isDisabled={
              !result
              || busy
              || hasMissingSelectionIds
              || hasFactEvidenceBlocker
              || hasUnexplainedDrops
            }
          >
            {accepting ? t("accepting") : t("accept")}
          </Button>
        </footer>
    </div>
  );
}
