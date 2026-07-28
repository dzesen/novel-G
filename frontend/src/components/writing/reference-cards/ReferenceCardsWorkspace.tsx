"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@heroui/react";
import { apiDelete, apiGet, apiPost, apiPut } from "@/lib/api";
import type {
  CharacterProfile,
  ReferenceCard,
  ReferenceCardType,
} from "@/types/novel";
import ReferenceCardCurationDialog from "./ReferenceCardCurationDialog";

interface ReferenceCardsWorkspaceProps {
  mode: "create" | "edit";
  novelId?: string;
  cardType: ReferenceCardType;
  openCurationOnMount?: boolean;
  onCurationOpened?: () => void;
}

interface CardDraft {
  name: string;
  subtitle: string;
  description: string;
  details: Record<string, string>;
  tags: string[];
  importance: "main" | "sub";
  character_profile: CharacterProfile;
}

const DETAIL_FIELDS: Record<ReferenceCardType, string[]> = {
  character: ["role", "age", "appearance", "personality", "motivation", "arc", "abilities", "relationships"],
  location: ["category", "atmosphere", "geography", "history", "story_importance", "dangers"],
  item: ["category", "appearance", "origin", "abilities", "limitations", "owner"],
  rule: ["category", "principle", "scope", "cost", "exceptions", "examples"],
  lore: ["category", "era", "background", "story_relevance", "related_entities", "uncertainties"],
};

function createDraft(card?: ReferenceCard): CardDraft {
  return {
    name: card?.name ?? "",
    subtitle: card?.subtitle ?? "",
    description: card?.description ?? "",
    details: { ...(card?.details ?? {}) },
    tags: [...(card?.tags ?? [])],
    importance: card?.importance ?? "sub",
    character_profile: {
      aliases: [...(card?.character_profile?.aliases ?? [])],
      portrayal_context: card?.character_profile?.portrayal_context ?? "",
      dialogue_examples: [
        ...(card?.character_profile?.dialogue_examples ?? []),
      ],
      scene_opening_examples: [
        ...(card?.character_profile?.scene_opening_examples ?? []),
      ],
      portrayal_notes: card?.character_profile?.portrayal_notes ?? "",
    },
  };
}

function splitTags(value: string): string[] {
  return value.split(/[,，、;；\n]/).map((tag) => tag.trim()).filter(Boolean);
}

export default function ReferenceCardsWorkspace({
  mode,
  novelId,
  cardType,
  openCurationOnMount = false,
  onCurationOpened,
}: ReferenceCardsWorkspaceProps) {
  const t = useTranslations("writing.referenceCards");
  const [cards, setCards] = useState<ReferenceCard[]>([]);
  const [trash, setTrash] = useState<ReferenceCard[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<CardDraft>(() => createDraft());
  const [creating, setCreating] = useState(false);
  const [showTrash, setShowTrash] = useState(false);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showCuration, setShowCuration] = useState(false);

  const selectedCard = useMemo(
    () => cards.find((card) => card._id === selectedId) ?? null,
    [cards, selectedId],
  );

  const filteredCards = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    if (!needle) return cards;
    return cards.filter((card) =>
      [
        card.name,
        card.subtitle,
        card.description,
        ...card.tags,
        ...(card.character_profile?.aliases ?? []),
      ]
        .join(" ")
        .toLocaleLowerCase()
        .includes(needle),
    );
  }, [cards, search]);

  const loadCards = useCallback(async () => {
    if (!novelId || mode !== "edit") return;
    setLoading(true);
    setError(null);
    try {
      const [activeResponse, trashResponse] = await Promise.all([
        apiGet<{ data: ReferenceCard[] }>(`/api/reference-cards/novel/${novelId}/${cardType}`),
        apiGet<{ data: ReferenceCard[] }>(`/api/reference-cards/novel/${novelId}/${cardType}/trash`),
      ]);
      setCards(activeResponse.data);
      setTrash(trashResponse.data);
      setSelectedId((current) => {
        if (current && activeResponse.data.some((card) => card._id === current)) return current;
        return activeResponse.data[0]?._id ?? null;
      });
      if (!activeResponse.data.length) setDraft(createDraft());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [cardType, mode, novelId, t]);

  useEffect(() => {
    void loadCards();
  }, [loadCards]);

  useEffect(() => {
    if (mode === "edit" && novelId && openCurationOnMount) {
      setShowCuration(true);
      onCurationOpened?.();
    }
  }, [mode, novelId, onCurationOpened, openCurationOnMount]);

  useEffect(() => {
    if (selectedCard && !creating) setDraft(createDraft(selectedCard));
  }, [creating, selectedCard]);

  const selectCard = (card: ReferenceCard) => {
    setCreating(false);
    setSelectedId(card._id);
    setDraft(createDraft(card));
    setError(null);
  };

  const startCreating = () => {
    setShowTrash(false);
    setCreating(true);
    setSelectedId(null);
    setDraft(createDraft());
    setError(null);
  };

  const saveCard = async () => {
    if (!novelId || !draft.name.trim()) {
      setError(t("nameRequired"));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const payload = {
        name: draft.name.trim(),
        subtitle: draft.subtitle.trim(),
        description: draft.description,
        details: draft.details,
        tags: draft.tags,
        importance: draft.importance,
        ...(cardType === "character"
          ? { character_profile: draft.character_profile }
          : {}),
      };
      const saved = creating
        ? await apiPost<ReferenceCard>(`/api/reference-cards/novel/${novelId}/${cardType}`, payload)
        : await apiPut<ReferenceCard>(`/api/reference-cards/novel/${novelId}/${cardType}/${selectedId}`, payload);
      setCards((current) => {
        const exists = current.some((card) => card._id === saved._id);
        return exists ? current.map((card) => card._id === saved._id ? saved : card) : [...current, saved];
      });
      setCreating(false);
      setSelectedId(saved._id);
      setDraft(createDraft(saved));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const moveToTrash = async () => {
    if (!novelId || !selectedCard || !window.confirm(t("deleteConfirm", { name: selectedCard.name }))) return;
    try {
      await apiDelete(`/api/reference-cards/novel/${novelId}/${cardType}/${selectedCard._id}`);
      const remaining = cards.filter((card) => card._id !== selectedCard._id);
      setCards(remaining);
      setTrash((current) => [{ ...selectedCard, is_deleted: true }, ...current]);
      setSelectedId(remaining[0]?._id ?? null);
      setCreating(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("deleteFailed"));
    }
  };

  const restoreCard = async (card: ReferenceCard) => {
    if (!novelId) return;
    try {
      const restored = await apiPost<ReferenceCard>(
        `/api/reference-cards/novel/${novelId}/${cardType}/${card._id}/restore`,
        {},
      );
      setTrash((current) => current.filter((item) => item._id !== card._id));
      setCards((current) => [...current, restored]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("restoreFailed"));
    }
  };

  const hardDeleteCard = async (card: ReferenceCard) => {
    if (!novelId || !window.confirm(t("hardDeleteConfirm", { name: card.name }))) return;
    try {
      await apiDelete(`/api/reference-cards/novel/${novelId}/${cardType}/${card._id}/hard`);
      setTrash((current) => current.filter((item) => item._id !== card._id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("deleteFailed"));
    }
  };

  if (mode !== "edit" || !novelId) {
    return (
      <div className="flex h-full items-center justify-center bg-background px-6 text-center">
        <div className="max-w-md">
          <h2 className="text-xl font-semibold text-foreground">{t(`types.${cardType}`)}</h2>
          <p className="mt-2 text-sm leading-6 text-muted">{t("createModeUnavailable")}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-background lg:flex-row">
      <aside className="flex max-h-[45vh] w-full shrink-0 flex-col border-b border-border bg-surface lg:max-h-none lg:w-80 lg:border-b-0 lg:border-r">
        <div className="border-b border-border px-4 py-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h1 className="text-lg font-semibold text-foreground">{t(`types.${cardType}`)}</h1>
              <p className="mt-1 text-xs text-muted">{t(`typeDescriptions.${cardType}`)}</p>
            </div>
            <div className="flex shrink-0 gap-2">
              <button
                type="button"
                onClick={() => setShowCuration(true)}
                className="inline-flex h-9 items-center justify-center rounded-lg border border-accent/30 bg-accent/10 px-3 text-xs font-semibold text-accent transition-colors hover:bg-accent/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                {t("aiCuration")}
              </button>
              <button
                type="button"
                onClick={startCreating}
                className="inline-flex h-9 w-9 items-center justify-center rounded-lg bg-accent text-lg text-white transition-colors hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                aria-label={t("newCard")}
              >
                +
              </button>
            </div>
          </div>
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t("search")}
            className="mt-4 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground outline-none placeholder:text-muted focus:border-accent focus:ring-2 focus:ring-accent/15"
          />
          <div className="mt-3 flex gap-1 rounded-lg bg-surface-secondary p-1 text-xs">
            <button type="button" onClick={() => setShowTrash(false)} className={`flex-1 rounded-md px-2 py-1.5 ${!showTrash ? "bg-surface font-medium text-foreground" : "text-muted"}`}>
              {t("active", { count: cards.length })}
            </button>
            <button type="button" onClick={() => setShowTrash(true)} className={`flex-1 rounded-md px-2 py-1.5 ${showTrash ? "bg-surface font-medium text-foreground" : "text-muted"}`}>
              {t("trash", { count: trash.length })}
            </button>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {loading ? (
            <p className="px-3 py-8 text-center text-sm text-muted">{t("loading")}</p>
          ) : showTrash ? (
            trash.length ? trash.map((card) => (
              <div key={card._id} className="mb-2 rounded-lg border border-border bg-background p-3">
                <p className="font-medium text-foreground">{card.name}</p>
                <p className="mt-1 line-clamp-2 text-xs text-muted">{card.subtitle || card.description || t("noDescription")}</p>
                <div className="mt-3 flex gap-2">
                  <button type="button" onClick={() => void restoreCard(card)} className="text-xs font-medium text-accent hover:underline">{t("restore")}</button>
                  <button type="button" onClick={() => void hardDeleteCard(card)} className="text-xs font-medium text-red-600 hover:underline">{t("hardDelete")}</button>
                </div>
              </div>
            )) : <p className="px-3 py-8 text-center text-sm text-muted">{t("trashEmpty")}</p>
          ) : filteredCards.length ? filteredCards.map((card) => (
            <button
              key={card._id}
              type="button"
              onClick={() => selectCard(card)}
              className={`mb-1 w-full rounded-lg px-3 py-3 text-left transition-colors ${selectedId === card._id && !creating ? "bg-accent/10" : "hover:bg-surface-secondary"}`}
            >
              <div className="flex items-center gap-3">
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-accent/10 text-sm font-semibold text-accent">{card.name.slice(0, 1).toLocaleUpperCase()}</span>
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium text-foreground">{card.name}</span>
                  <span className="mt-0.5 block truncate text-xs text-muted">{card.subtitle || card.description || t("noDescription")}</span>
                  {card.interop?.writing_participation?.status === "not_participating" && (
                    <span className="mt-1.5 inline-flex rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-800 dark:bg-amber-950 dark:text-amber-200">
                      {t("importedNotParticipating")}
                    </span>
                  )}
                </span>
              </div>
            </button>
          )) : <p className="px-3 py-8 text-center text-sm text-muted">{search ? t("noSearchResults") : t("empty")}</p>}
        </div>
      </aside>

      <main className="min-h-0 flex-1 overflow-y-auto">
        {showTrash ? (
          <div className="flex min-h-full items-center justify-center px-6 text-center text-sm text-muted">{t("trashHint")}</div>
        ) : creating || selectedCard ? (
          <div className="mx-auto max-w-4xl px-5 py-6 sm:px-8 sm:py-8">
            <div className="flex flex-wrap items-start justify-between gap-4 border-b border-border pb-5">
              <div>
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-accent">{creating ? t("newCard") : t("editing")}</p>
                <h2 className="mt-1 text-2xl font-semibold text-foreground">{draft.name || t("untitled")}</h2>
              </div>
              <div className="flex gap-2">
                {!creating && <Button variant="ghost" className="text-red-600" onPress={() => void moveToTrash()}>{t("delete")}</Button>}
                <Button variant="primary" className="bg-accent text-white hover:bg-accent-hover" isDisabled={saving} onPress={() => void saveCard()}>{saving ? t("saving") : t("save")}</Button>
              </div>
            </div>

            {error && <div role="alert" className="mt-5 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">{error}</div>}
            {!creating && selectedCard?.interop?.writing_participation?.status === "not_participating" && (
              <div role="status" className="mt-5 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100">
                <p className="font-semibold">{t("importedNotParticipating")}</p>
                <p className="mt-1 leading-6 text-amber-800 dark:text-amber-200">
                  {t("importedNotParticipatingHint")}
                </p>
              </div>
            )}

            <div className="mt-6 grid gap-5 md:grid-cols-2">
              <Field label={t("fields.name")} value={draft.name} onChange={(value) => setDraft((current) => ({ ...current, name: value }))} />
              <Field label={t("fields.subtitle")} value={draft.subtitle} onChange={(value) => setDraft((current) => ({ ...current, subtitle: value }))} />
              <label className="block">
                <span className="mb-2 block text-sm font-medium text-foreground">{t("fields.cardImportance")}</span>
                <select
                  value={draft.importance}
                  onChange={(event) => setDraft((current) => ({ ...current, importance: event.target.value as "main" | "sub" }))}
                  className="w-full rounded-lg border border-border bg-surface px-3 py-2.5 text-sm text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
                >
                  <option value="main">{t("importanceMainGeneric")}</option>
                  <option value="sub">{t("importanceSubGeneric")}</option>
                </select>
              </label>
              <TextArea className="md:col-span-2" label={t("fields.description")} value={draft.description} onChange={(value) => setDraft((current) => ({ ...current, description: value }))} />
              {DETAIL_FIELDS[cardType].map((field) => (
                <TextArea
                  key={field}
                  label={t(`fields.${field}`)}
                  value={draft.details[field] ?? ""}
                  onChange={(value) => setDraft((current) => ({ ...current, details: { ...current.details, [field]: value } }))}
                />
              ))}
              {cardType === "character" && (
                <section className="border-t border-border pt-6 md:col-span-2">
                  <div className="max-w-2xl">
                    <h3 className="text-base font-semibold text-foreground">
                      {t("profileTitle")}
                    </h3>
                    <p className="mt-1 text-sm leading-6 text-muted">
                      {t("profileDescription")}
                    </p>
                  </div>
                  <div className="mt-5 grid gap-5 md:grid-cols-2">
                    <Field
                      label={t("fields.aliases")}
                      value={draft.character_profile.aliases.join("，")}
                      onChange={(value) =>
                        setDraft((current) => ({
                          ...current,
                          character_profile: {
                            ...current.character_profile,
                            aliases: splitTags(value),
                          },
                        }))
                      }
                    />
                    <TextArea
                      label={t("fields.portrayalNotes")}
                      value={draft.character_profile.portrayal_notes}
                      onChange={(value) =>
                        setDraft((current) => ({
                          ...current,
                          character_profile: {
                            ...current.character_profile,
                            portrayal_notes: value,
                          },
                        }))
                      }
                      hint={t("profilePromptHint")}
                    />
                    <TextArea
                      className="md:col-span-2"
                      label={t("fields.portrayalContext")}
                      value={draft.character_profile.portrayal_context}
                      onChange={(value) =>
                        setDraft((current) => ({
                          ...current,
                          character_profile: {
                            ...current.character_profile,
                            portrayal_context: value,
                          },
                        }))
                      }
                      hint={t("profileContextHint")}
                    />
                    <ExampleListEditor
                      className="md:col-span-2"
                      label={t("fields.dialogueExamples")}
                      values={draft.character_profile.dialogue_examples}
                      maxItems={12}
                      addLabel={t("addDialogueExample")}
                      removeLabel={t("removeExample")}
                      hint={t("dialogueExamplesHint")}
                      onChange={(dialogueExamples) =>
                        setDraft((current) => ({
                          ...current,
                          character_profile: {
                            ...current.character_profile,
                            dialogue_examples: dialogueExamples,
                          },
                        }))
                      }
                    />
                    <ExampleListEditor
                      className="md:col-span-2"
                      label={t("fields.sceneOpeningExamples")}
                      values={draft.character_profile.scene_opening_examples}
                      maxItems={8}
                      addLabel={t("addOpeningExample")}
                      removeLabel={t("removeExample")}
                      hint={t("sceneOpeningHint")}
                      onChange={(sceneOpeningExamples) =>
                        setDraft((current) => ({
                          ...current,
                          character_profile: {
                            ...current.character_profile,
                            scene_opening_examples: sceneOpeningExamples,
                          },
                        }))
                      }
                    />
                  </div>
                </section>
              )}
              <TextArea className="md:col-span-2" label={t("fields.tags")} value={draft.tags.join("，")} onChange={(value) => setDraft((current) => ({ ...current, tags: splitTags(value) }))} hint={t("tagsHint")} />
            </div>
          </div>
        ) : (
          <div className="flex min-h-full items-center justify-center px-6 text-center">
            <div>
              <p className="text-lg font-medium text-foreground">{t("emptyTitle")}</p>
              <p className="mt-2 text-sm text-muted">{t("emptyDescription")}</p>
              <Button className="mt-5 bg-accent text-white hover:bg-accent-hover" variant="primary" onPress={startCreating}>{t("newCard")}</Button>
            </div>
          </div>
        )}
      </main>
      <ReferenceCardCurationDialog
        novelId={novelId}
        isOpen={showCuration}
        onClose={() => setShowCuration(false)}
        onApplied={loadCards}
      />
    </div>
  );
}

function Field({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <label className="block">
      <span className="mb-2 block text-sm font-medium text-foreground">{label}</span>
      <input value={value} onChange={(event) => onChange(event.target.value)} className="w-full rounded-lg border border-border bg-surface px-3 py-2.5 text-sm text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15" />
    </label>
  );
}

function TextArea({ label, value, onChange, hint, className = "" }: { label: string; value: string; onChange: (value: string) => void; hint?: string; className?: string }) {
  return (
    <label className={`block ${className}`}>
      <span className="mb-2 block text-sm font-medium text-foreground">{label}</span>
      <textarea value={value} onChange={(event) => onChange(event.target.value)} rows={4} className="w-full resize-y rounded-lg border border-border bg-surface px-3 py-2.5 text-sm leading-6 text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15" />
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </label>
  );
}

function ExampleListEditor({
  label,
  values,
  maxItems,
  addLabel,
  removeLabel,
  hint,
  onChange,
  className = "",
}: {
  label: string;
  values: string[];
  maxItems: number;
  addLabel: string;
  removeLabel: string;
  hint: string;
  onChange: (values: string[]) => void;
  className?: string;
}) {
  return (
    <fieldset className={className}>
      <legend className="text-sm font-medium text-foreground">{label}</legend>
      <p className="mt-1 text-xs leading-5 text-muted">{hint}</p>
      <div className="mt-3 space-y-3">
        {values.map((value, index) => (
          <div
            // Entries are append/remove only; the index is stable while text is edited.
            key={index}
            className="rounded-lg border border-border bg-surface-secondary p-3"
          >
            <textarea
              value={value}
              onChange={(event) =>
                onChange(
                  values.map((item, itemIndex) =>
                    itemIndex === index ? event.target.value : item,
                  ),
                )
              }
              rows={3}
              className="w-full resize-y bg-transparent text-sm leading-6 text-foreground outline-none placeholder:text-muted"
            />
            <button
              type="button"
              onClick={() =>
                onChange(values.filter((_, itemIndex) => itemIndex !== index))
              }
              className="mt-2 text-xs font-medium text-red-600 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              {removeLabel}
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        disabled={values.length >= maxItems}
        onClick={() => onChange([...values, ""])}
        className="mt-3 text-sm font-semibold text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40"
      >
        + {addLabel}
      </button>
    </fieldset>
  );
}
