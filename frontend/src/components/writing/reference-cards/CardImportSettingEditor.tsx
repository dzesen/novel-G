"use client";

import { useId } from "react";
import { useTranslations } from "next-intl";
import type { CardImportCandidate, CardImportDecision, CardImportProposal } from "@/types/novel";

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

function originalText(proposal: CardImportProposal, candidate: CardImportCandidate): string {
  const raw = object(proposal.raw_payload);
  const data = object(raw.data);
  if (candidate.candidate_id === "character:0") {
    const fields = Object.keys(data).length ? data : raw;
    return ["name", "description", "personality", "scenario", "first_mes", "mes_example"]
      .filter((key) => typeof fields[key] === "string" && fields[key])
      .map((key) => `${key}\n${fields[key]}`).join("\n\n");
  }
  const locator = candidate.interop_preview?.source_locator ?? "";
  const entries = proposal.source_format === "worldbook_standalone"
    ? raw.entries : object(data.character_book).entries;
  const entry = Array.isArray(entries) ? object(entries[Number(locator)]) : object(object(entries)[locator]);
  return typeof entry.content === "string" ? entry.content : "";
}

const NOTICE_KEYS: Record<string, string> = {
  narrative_roles: "roles", conditions_preserved: "conditions",
  structured_data_readable: "structured", presentation_removed: "presentation",
  runtime_code_isolated: "code", unknown_macro: "unknown", dynamic_output: "unknown",
  unresolved_state_variable: "state", damaged_template: "unsupported",
  template_complexity: "unsupported", unsupported_condition: "unsupported",
  unsupported_template_code: "unsupported", invalid_literal: "unsupported",
  unsupported_variable: "unsupported", damaged_code_block: "unsupported",
};

export default function CardImportSettingEditor({ proposal, candidate, decision, disabled, onDecision }: {
  proposal: CardImportProposal;
  candidate: CardImportCandidate;
  decision: CardImportDecision;
  disabled: boolean;
  onDecision: (next: CardImportDecision) => void;
}) {
  const t = useTranslations("cardImportAdaptation");
  const metadataT = useTranslations("writing.generationMetadata.referenceCardFields");
  const id = useId();
  const adaptation = candidate.adaptation;
  const needsReview = adaptation && adaptation.status !== "ready";
  const source = originalText(proposal, candidate);
  const description = typeof decision.overrides?.description === "string"
    ? decision.overrides.description : candidate.fields.description ?? "";
  const name = typeof decision.overrides?.name === "string" ? decision.overrides.name : candidate.fields.name;
  const profile = object(decision.overrides?.character_profile ?? candidate.fields.character_profile);
  const notices = Array.from(new Set(Object.values(adaptation?.fields ?? {}).flatMap(
    (field) => field.notices.map((notice) => NOTICE_KEYS[notice] ?? "unsupported"),
  )));
  const update = (field: string, value: unknown) => onDecision({
    ...decision, overrides: { ...decision.overrides, [field]: value },
  });

  return (
    <details className="mt-3 border-t border-border pt-3" open={Boolean(needsReview)}>
      <summary className="cursor-pointer text-sm font-medium text-foreground focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-accent">
        {t("open")}
        {adaptation && notices.length > 0 && (
          <span className="ml-2 text-xs font-normal text-muted">{t(`status.${adaptation.status}`)}</span>
        )}
      </summary>
      <div className="mt-3 space-y-3">
        {notices.length > 0 && (
          <ul className="list-disc space-y-1 pl-5 text-xs leading-5 text-muted">
            {notices.map((notice) => <li key={notice}>{t(`notices.${notice}`)}</li>)}
          </ul>
        )}
        <div>
          <label className="block text-xs font-medium text-foreground" htmlFor={`${id}-name`}>{t("name")}</label>
          <input id={`${id}-name`} value={name} maxLength={120} disabled={disabled}
            onChange={(event) => update("name", event.target.value)}
            className="mt-1 block min-h-10 w-full min-w-0 rounded-lg border border-border bg-background px-3 text-sm focus:border-accent focus:outline-none disabled:opacity-60" />
        </div>
        <div>
          <label className="block text-xs font-medium text-foreground" htmlFor={`${id}-description`}>{t("description")}</label>
          <textarea id={`${id}-description`} value={description} rows={8} disabled={disabled}
            onChange={(event) => update("description", event.target.value)}
            className="mt-1 block max-h-96 min-h-40 w-full min-w-0 resize-y rounded-lg border border-border bg-background p-3 text-sm leading-6 focus:border-accent focus:outline-none disabled:opacity-60" />
        </div>
        {typeof profile.portrayal_context === "string" && (
          <div>
            <label className="block text-xs font-medium text-foreground" htmlFor={`${id}-portrayal`}>
              {metadataT("portrayal_context")}
            </label>
            <textarea id={`${id}-portrayal`} value={profile.portrayal_context} rows={4} disabled={disabled}
              onChange={(event) => update("character_profile", { ...profile, portrayal_context: event.target.value })}
              className="mt-1 block max-h-64 min-h-24 w-full min-w-0 resize-y rounded-lg border border-border bg-background p-3 text-sm leading-6 focus:border-accent focus:outline-none disabled:opacity-60" />
          </div>
        )}
        {needsReview && (
          <label className="flex items-start gap-2 text-xs leading-5 text-foreground">
            <input type="checkbox" className="mt-1 size-4 shrink-0 accent-accent" disabled={disabled || !description.trim()}
              checked={typeof decision.overrides?.description === "string" && Boolean(description.trim())}
              onChange={(event) => {
                if (event.target.checked) update("description", description);
                else {
                  const overrides = { ...decision.overrides };
                  delete overrides.description;
                  onDecision({ ...decision, overrides });
                }
              }} />
            <span>{t("confirm")}</span>
          </label>
        )}
        <p className="text-xs leading-5 text-muted">{t("editingHint")}</p>
        {source && (
          <details className="border-t border-border pt-2">
            <summary className="cursor-pointer text-xs font-medium text-muted">{t("source")}</summary>
            <p className="mt-2 text-xs leading-5 text-muted">{t("sourceHint")}</p>
            <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-surface-secondary p-3 text-xs leading-5 [overflow-wrap:anywhere]">{source}</pre>
          </details>
        )}
      </div>
    </details>
  );
}
