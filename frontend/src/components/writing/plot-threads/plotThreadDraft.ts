import type { ChapterSummary } from "../../../types/novel";
import type {
  PlotThread,
  ThreadImportance,
  ThreadStatus,
} from "../chapters/outline/outlineTypes";

export type ThreadDraft = {
  name: string;
  description: string;
  status: ThreadStatus;
  importance: ThreadImportance;
  planted_chapter_id: string;
  legacy_planted_chapter_order: number | null;
  due_kind: "none" | "chapter" | "planned_ordinal";
  due_value: string;
  resolved_chapter_id: string;
  legacy_resolved_chapter_order: number | null;
  notes: string;
};

type ChapterReference = Pick<ChapterSummary, "_id" | "order_index">;

export function plotThreadToDraft(thread: PlotThread): ThreadDraft {
  return {
    name: thread.name,
    description: thread.description ?? "",
    status: thread.status,
    importance: thread.importance,
    planted_chapter_id: thread.planted_chapter_id ?? "",
    legacy_planted_chapter_order: thread.planted_chapter_id
      ? null
      : thread.planted_chapter_order ?? null,
    due_kind: thread.due_target?.kind
      ?? (thread.due_chapter_order != null ? "planned_ordinal" : "none"),
    due_value: thread.due_target?.kind === "chapter"
      ? thread.due_target.chapter_id
      : String(
        thread.due_target?.kind === "planned_ordinal"
          ? thread.due_target.ordinal
          : thread.due_chapter_order ?? "",
      ),
    resolved_chapter_id: thread.resolved_chapter_id ?? "",
    legacy_resolved_chapter_order: thread.resolved_chapter_id
      ? null
      : thread.resolved_chapter_order ?? null,
    notes: thread.notes ?? "",
  };
}

export function plotThreadDraftToPayload(
  draft: ThreadDraft,
  chapters: ChapterReference[],
) {
  const planted = chapters.find(
    (chapter) => chapter._id === draft.planted_chapter_id,
  );
  const resolved = chapters.find(
    (chapter) => chapter._id === draft.resolved_chapter_id,
  );
  const dueTarget = draft.due_kind === "chapter" && draft.due_value
    ? { kind: "chapter" as const, chapter_id: draft.due_value }
    : draft.due_kind === "planned_ordinal" && draft.due_value
      ? { kind: "planned_ordinal" as const, ordinal: Number(draft.due_value) }
      : null;
  return {
    name: draft.name,
    description: draft.description,
    status: draft.status,
    importance: draft.importance,
    planted_chapter_id: planted?._id ?? null,
    planted_chapter_order: planted?.order_index
      ?? draft.legacy_planted_chapter_order,
    due_target: dueTarget,
    resolved_chapter_id: resolved?._id ?? null,
    resolved_chapter_order: resolved?.order_index
      ?? draft.legacy_resolved_chapter_order,
    notes: draft.notes,
  };
}
