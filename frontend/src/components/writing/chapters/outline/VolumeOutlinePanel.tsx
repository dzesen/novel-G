"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { apiPost } from "@/lib/api";
import { useOutlineStream } from "./useOutlineStream";
import OutlineGenerationParams, {
  EMPTY_GENERATION_PARAMS,
  toRequestParams,
  type GenerationParams,
} from "./OutlineGenerationParams";
import { ContextNotices, Field, Notice } from "./outlineUi";
import type {
  AcceptVolumeOutlineResponse,
  VolumeOutlineItem,
  VolumeOutlineResult,
} from "./outlineTypes";

interface VolumeOutlinePanelProps {
  novelId: string;
  onClose: () => void;
  onAccepted: () => void;
}

export default function VolumeOutlinePanel({ novelId, onClose, onAccepted }: VolumeOutlinePanelProps) {
  const t = useTranslations("writing.outline");
  const stream = useOutlineStream<VolumeOutlineResult>({
    path: "/api/llm/create-volume-outline-by-ai",
    stepKey: "volume_outline",
  });
  const [params, setParams] = useState<GenerationParams>(EMPTY_GENERATION_PARAMS);
  const [accepting, setAccepting] = useState(false);
  const [acceptError, setAcceptError] = useState("");

  const volumes = stream.result?.volumes ?? null;

  const updateVolume = (index: number, patch: Partial<VolumeOutlineItem>) => {
    stream.setResult((current) => {
      if (!current) return current;
      const next = [...current.volumes];
      next[index] = { ...next[index], ...patch };
      return { volumes: next };
    });
  };

  const accept = async () => {
    if (!volumes) return;
    setAccepting(true);
    setAcceptError("");
    try {
      await apiPost<AcceptVolumeOutlineResponse>(
        `/api/volumes/novel/${novelId}/accept-outline`,
        { volumes }
      );
      onAccepted();
      onClose();
    } catch (err) {
      // 409 的文案里含恢复路径（"请先清空卷（可在垃圾桶恢复）后重试"），
      // 原样展示；前端另写一句概括会把那条路径丢掉（设计 §6.1）。
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
            <h3 className="text-base font-semibold text-foreground">{t("volumeTitle")}</h3>
            <p className="mt-1 text-xs leading-5 text-muted">{t("volumeDescription")}</p>
          </div>
          <div className="flex shrink-0 gap-2">
            {stream.status === "running" ? (
              <Button variant="outline" size="sm" onPress={stream.cancel}>
                {t("cancel")}
              </Button>
            ) : (
              <Button
                variant="primary"
                size="sm"
                className="bg-accent text-white hover:bg-accent-hover"
                onPress={() => void stream.start({ novel_id: novelId, ...toRequestParams(params) })}
                isDisabled={busy}
              >
                {volumes ? t("regenerate") : t("generate")}
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

          <ContextNotices report={stream.contextReport} />
          {stream.error && <Notice tone="error">{stream.error}</Notice>}
          {acceptError && <Notice tone="error">{acceptError}</Notice>}

          {stream.status === "running" && !volumes && (
            <p className="py-10 text-center text-sm text-muted">{t("generating")}</p>
          )}

          {!volumes && stream.status !== "running" && (
            <p className="py-10 text-center text-sm text-muted">{t("emptyPreview")}</p>
          )}

          {volumes && (
            <>
              <div className="mb-3 flex items-center justify-between">
                <span className="rounded-md border border-border bg-background px-2 py-1 text-xs text-muted">
                  {t("volumeCount", { count: volumes.length })}
                </span>
              </div>
              <p className="mb-4 text-xs leading-5 text-muted">{t("rangeHint")}</p>
              <div className="grid gap-4">
                {volumes.map((volume, index) => (
                  <div key={index} className="rounded-md border border-border bg-background p-4">
                    <div className="grid gap-3 md:grid-cols-2">
                      <Field label={t("fieldTitle")}>
                        <input
                          value={volume.title}
                          onChange={(e) => updateVolume(index, { title: e.target.value })}
                          className="min-h-9 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
                        />
                      </Field>
                      <div className="grid grid-cols-2 gap-3">
                        <Field label={t("fieldRangeStart")}>
                          <input
                            type="number"
                            value={volume.chapter_range.start}
                            onChange={(e) =>
                              updateVolume(index, {
                                chapter_range: { ...volume.chapter_range, start: Number(e.target.value) },
                              })
                            }
                            className="min-h-9 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
                          />
                        </Field>
                        <Field label={t("fieldRangeEnd")}>
                          <input
                            type="number"
                            value={volume.chapter_range.end}
                            onChange={(e) =>
                              updateVolume(index, {
                                chapter_range: { ...volume.chapter_range, end: Number(e.target.value) },
                              })
                            }
                            className="min-h-9 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none focus:border-accent"
                          />
                        </Field>
                      </div>
                    </div>
                    <div className="mt-3 grid gap-3">
                      <Field label={t("fieldSummary")}>
                        <textarea
                          value={volume.summary}
                          rows={3}
                          onChange={(e) => updateVolume(index, { summary: e.target.value })}
                          className="w-full resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm leading-5 text-foreground outline-none focus:border-accent"
                        />
                      </Field>
                      <Field label={t("fieldArc")}>
                        <textarea
                          value={volume.arc}
                          rows={2}
                          onChange={(e) => updateVolume(index, { arc: e.target.value })}
                          className="w-full resize-y rounded-md border border-border bg-surface px-3 py-2 text-sm leading-5 text-foreground outline-none focus:border-accent"
                        />
                      </Field>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>

        <footer className="flex justify-end gap-2 border-t border-border px-5 py-3">
          <Button variant="ghost" size="sm" onPress={stream.reset} isDisabled={!volumes || busy}>
            {t("discard")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            className="bg-accent text-white hover:bg-accent-hover"
            onPress={() => void accept()}
            isDisabled={!volumes || busy}
          >
            {accepting ? t("accepting") : t("accept")}
          </Button>
        </footer>
      </div>
    </div>
  );
}
