"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { apiGet, apiPost } from "@/lib/api";
import RosterPicker from "./RosterPicker";
import { Field, RowEditor, SceneContractDetails } from "./outlineUi";
import type { ChapterOutlineAuthoredFields, Scene } from "./outlineTypes";
import type { useRoster } from "./useRoster";

interface OutlineFieldsEditorProps {
  value: ChapterOutlineAuthoredFields;
  onChange: (patch: Partial<ChapterOutlineAuthoredFields>) => void;
  roster: ReturnType<typeof useRoster>;
  novelId?: string;
  chapterId?: string;
  baseScenes?: Scene[];
}

interface SceneAgentProfile {
  agent_id: string;
  label: string;
  description: string;
}

interface SceneRewriteResponse {
  scene: Scene;
  agent_id: string;
  provider_alias: string;
}

/**
 * 细纲作者字段的受控编辑器（设计 §2.6）。预览与编辑两处复用同一份，
 * 避免字段编辑器重复、约束漂移。不含 new_threads（预览自行追加）、
 * 不含 threads_planted（编辑态只读展示，见 ChapterOutlinePanel）。
 */
export default function OutlineFieldsEditor({
  value,
  onChange,
  roster,
  novelId,
  chapterId,
  baseScenes,
}: OutlineFieldsEditorProps) {
  const t = useTranslations("writing.outline");
  const [sceneAgents, setSceneAgents] = useState<SceneAgentProfile[]>([]);
  const [agentSelections, setAgentSelections] = useState<Record<number, string>>({});
  const [agentInstructions, setAgentInstructions] = useState<Record<number, string>>({});
  const [rewriteCandidates, setRewriteCandidates] = useState<Record<number, SceneRewriteResponse>>({});
  const [rewritingScene, setRewritingScene] = useState<number | null>(null);
  const [agentErrors, setAgentErrors] = useState<Record<number, string>>({});
  const sceneAgentMode = Boolean(novelId && chapterId && baseScenes);
  const sceneStructureMatches = baseScenes?.length === value.scenes.length;
  const sceneContractLocked =
    value.scene_contract_version === "scene_transition_contract.v2";

  useEffect(() => {
    if (!sceneAgentMode) return;
    let active = true;
    apiGet<{ data: SceneAgentProfile[] }>("/api/llm/scene-agents")
      .then((response) => {
        if (active) setSceneAgents(response.data);
      })
      .catch((error) => {
        if (active) {
          setAgentErrors({
            [-1]: error instanceof Error ? error.message : String(error),
          });
        }
      });
    return () => {
      active = false;
    };
  }, [sceneAgentMode]);

  const rewriteScene = async (index: number, scene: Scene) => {
    if (!novelId || !chapterId || !baseScenes?.[index]) return;
    const agentId = agentSelections[index] || sceneAgents[0]?.agent_id;
    if (!agentId) return;
    setRewritingScene(index);
    setAgentErrors((current) => ({ ...current, [index]: "" }));
    try {
      const response = await apiPost<SceneRewriteResponse>(
        "/api/llm/rewrite-chapter-scene",
        {
          novel_id: novelId,
          chapter_id: chapterId,
          scene_index: index,
          base_scene: {
            summary: baseScenes[index].summary,
            purpose: baseScenes[index].purpose,
          },
          scene: { summary: scene.summary, purpose: scene.purpose },
          agent_id: agentId,
          instruction: agentInstructions[index] || "",
        },
      );
      setRewriteCandidates((current) => ({ ...current, [index]: response }));
    } catch (error) {
      setAgentErrors((current) => ({
        ...current,
        [index]: error instanceof Error ? error.message : String(error),
      }));
    } finally {
      setRewritingScene(null);
    }
  };

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
            readOnly={sceneContractLocked}
            onChange={(e) => onChange({ target_word_count: Number(e.target.value) })}
            className="min-h-9 w-40 rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
          />
        </Field>
        {sceneContractLocked && (
          <p className="rounded-md border border-border bg-surface px-3 py-2 text-xs leading-5 text-muted">
            {t("sceneContractStructureLocked")}
          </p>
        )}
      </div>

      <RowEditor<Scene>
        title={t("fieldScenes")}
        rows={value.scenes}
        addLabel={t("addScene")}
        removeLabel={t("removeRow")}
        onChange={(scenes) => onChange({ scenes })}
        blank={{ summary: "", purpose: "" }}
        structureLocked={sceneContractLocked}
        render={(scene, update, index) => (
          <div className="grid gap-3">
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
            <SceneContractDetails scene={scene} />

            {sceneAgentMode && (
              <div className="grid gap-2 rounded-md border border-border bg-background p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs font-semibold text-foreground">{t("sceneAgentTitle")}</span>
                  <select
                    aria-label={t("sceneAgentSelect")}
                    value={agentSelections[index] || sceneAgents[0]?.agent_id || ""}
                    onChange={(event) =>
                      setAgentSelections((current) => ({
                        ...current,
                        [index]: event.target.value,
                      }))
                    }
                    className="min-h-8 min-w-44 rounded-md border border-border bg-surface px-2 py-1 text-xs text-foreground outline-none focus:border-accent"
                  >
                    {sceneAgents.map((agent) => (
                      <option key={agent.agent_id} value={agent.agent_id}>
                        {agent.label}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() => void rewriteScene(index, scene)}
                    disabled={
                      rewritingScene !== null ||
                      sceneAgents.length === 0 ||
                      !sceneStructureMatches
                    }
                    className="min-h-8 rounded-md bg-accent px-3 py-1 text-xs font-medium text-white hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {rewritingScene === index ? t("sceneAgentRewriting") : t("sceneAgentRewrite")}
                  </button>
                </div>
                <input
                  value={agentInstructions[index] || ""}
                  onChange={(event) =>
                    setAgentInstructions((current) => ({
                      ...current,
                      [index]: event.target.value,
                    }))
                  }
                  placeholder={t("sceneAgentInstructionPlaceholder")}
                  className="min-h-8 w-full rounded-md border border-border bg-surface px-2 py-1 text-xs text-foreground outline-none placeholder:text-muted focus:border-accent"
                />
                {!sceneStructureMatches && (
                  <p className="text-xs text-amber-700 dark:text-amber-300">
                    {t("sceneAgentStructureChanged")}
                  </p>
                )}
                {agentErrors[index] && (
                  <p className="text-xs text-red-600 dark:text-red-400">{agentErrors[index]}</p>
                )}
                {rewriteCandidates[index] && (
                  <div className="grid gap-2 rounded-md border border-accent/40 bg-surface p-3">
                    <p className="text-xs font-medium text-foreground">
                      {t("sceneAgentPreview", {
                        provider: rewriteCandidates[index].provider_alias,
                      })}
                    </p>
                    <p className="text-xs leading-5 text-foreground">
                      {rewriteCandidates[index].scene.summary}
                    </p>
                    <p className="text-xs leading-5 text-muted">
                      {rewriteCandidates[index].scene.purpose}
                    </p>
                    <div className="flex justify-end gap-2">
                      <button
                        type="button"
                        onClick={() =>
                          setRewriteCandidates((current) => {
                            const next = { ...current };
                            delete next[index];
                            return next;
                          })
                        }
                        className="rounded-md px-2 py-1 text-xs text-muted hover:bg-surface-secondary"
                      >
                        {t("sceneAgentDiscard")}
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          update(rewriteCandidates[index].scene);
                          setRewriteCandidates((current) => {
                            const next = { ...current };
                            delete next[index];
                            return next;
                          });
                        }}
                        className="rounded-md bg-accent px-2 py-1 text-xs font-medium text-white hover:bg-accent-hover"
                      >
                        {t("sceneAgentApply")}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}
            {agentErrors[-1] && (
              <p className="text-xs text-red-600 dark:text-red-400">
                {t("sceneAgentLoadFailed", { error: agentErrors[-1] })}
              </p>
            )}
          </div>
        )}
      />
    </>
  );
}
