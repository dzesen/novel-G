"use client";

import { useTranslations } from "next-intl";
import RosterPicker from "./RosterPicker";
import { Field, RowEditor } from "./outlineUi";
import type { ChapterOutlineAuthoredFields, Scene } from "./outlineTypes";
import type { useRoster } from "./useRoster";

interface OutlineFieldsEditorProps {
  value: ChapterOutlineAuthoredFields;
  onChange: (patch: Partial<ChapterOutlineAuthoredFields>) => void;
  roster: ReturnType<typeof useRoster>;
}

/**
 * 细纲作者字段的受控编辑器（设计 §2.6）。预览与编辑两处复用同一份，
 * 避免字段编辑器重复、约束漂移。不含 new_threads（预览自行追加）、
 * 不含 threads_planted（编辑态只读展示，见 ChapterOutlinePanel）。
 */
export default function OutlineFieldsEditor({ value, onChange, roster }: OutlineFieldsEditorProps) {
  const t = useTranslations("writing.outline");
  return (
    <>
      <div className="grid gap-3 rounded-md border border-border bg-background p-4">
        <RosterPicker
          mode="single"
          label={t("fieldPov")}
          options={roster.characters}
          value={value.pov_character_card_id}
          onChange={(next) => onChange({ pov_character_card_id: next as string | null })}
          emptyText={t("rosterEmpty")}
        />
        <RosterPicker
          mode="multi"
          label={t("fieldPresent")}
          options={roster.characters}
          value={value.present_character_card_ids}
          onChange={(next) => onChange({ present_character_card_ids: next as string[] })}
          emptyText={t("rosterEmpty")}
        />
        <RosterPicker
          mode="multi"
          label={t("fieldMentioned")}
          options={roster.characters}
          value={value.mentioned_character_card_ids}
          onChange={(next) => onChange({ mentioned_character_card_ids: next as string[] })}
          emptyText={t("rosterEmpty")}
        />
        <RosterPicker
          mode="multi"
          label={t("fieldWorldbook")}
          options={roster.worldbook}
          value={value.referenced_worldbook_card_ids}
          onChange={(next) => onChange({ referenced_worldbook_card_ids: next as string[] })}
          emptyText={t("rosterEmpty")}
        />
        <RosterPicker
          mode="multi"
          label={t("fieldThreadsResolved")}
          options={roster.threads}
          value={value.threads_resolved}
          onChange={(next) => onChange({ threads_resolved: next as string[] })}
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
            value={value.core_conflict}
            rows={2}
            onChange={(e) => onChange({ core_conflict: e.target.value })}
            className="w-full resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm leading-5 text-foreground outline-none focus:border-accent"
          />
        </Field>
        <Field label={t("fieldEndingHook")}>
          <textarea
            value={value.ending_hook}
            rows={2}
            onChange={(e) => onChange({ ending_hook: e.target.value })}
            className="w-full resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm leading-5 text-foreground outline-none focus:border-accent"
          />
        </Field>
        <Field label={t("fieldTargetWords")}>
          <input
            type="number"
            value={value.target_word_count}
            onChange={(e) => onChange({ target_word_count: Number(e.target.value) })}
            className="min-h-9 w-40 rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
          />
        </Field>
      </div>

      <RowEditor<Scene>
        title={t("fieldScenes")}
        rows={value.scenes}
        addLabel={t("addScene")}
        removeLabel={t("removeRow")}
        onChange={(scenes) => onChange({ scenes })}
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
    </>
  );
}
