import type { GenerationDiagnostic, GenerationJob, ReadinessIssue } from "./batchTypes";

export type PauseDestination = "baseline" | "candidates" | "curation" | "facts" | "blueprint" | "readiness" | "chapter" | "settings";

// A chapter ID locates evidence. It does not determine the kind of review needed.
export function pauseDestinations(job: GenerationJob, diagnostic?: GenerationDiagnostic | null): PauseDestination[] {
  if (job.pause_reason_detail === "world_baseline_confirmation_required") return ["baseline"];
  if (["reference_card_review", "reference_card_repair_exhausted"].includes(job.pause_reason ?? "")) return ["candidates"];
  const codes = new Set([
    job.pause_reason_detail ?? job.pause_reason,
    diagnostic?.code,
    ...(diagnostic?.action_codes ?? []),
  ]);
  const has = (...values: string[]) => values.some((value) => codes.has(value));
  const result: PauseDestination[] = [];
  if (has("world_baseline_confirmation_required", "open_world_baseline")) result.push("baseline");
  if (has("reference_card_review", "reference_card_repair_exhausted", "open_reference_card_candidates")
    || (job.pause_reason === "uncertain_attempt" && job.error?.auto_creation)) result.push("candidates");
  if (has("open_reference_card_curation")) result.push("curation");
  if (has("conflict", "candidate_state_repair_exhausted", "repair_budget_exhausted_state_reextraction")) result.push("facts");
  if (has("open_book_blueprint", "blueprint_structure_incomplete")) result.push("blueprint");
  if (has("open_provider_settings", "review_provider_output_limit", "provider_authentication_failed", "provider_schema_unsupported")) result.push("settings");
  if (!result.length && has("authorization_scope_increased", "readiness_confirmation_required", "cost_cap", "attempt_capacity", "refresh_generation_readiness", "review_generation_authorization")) result.push("readiness");
  if (!result.some((item) => ["baseline", "candidates", "curation", "blueprint", "settings"].includes(item))
    && has("open_affected_chapter", "open_incomplete_prose") && diagnostic?.chapter_id
    && typeof diagnostic.details.prose_run_id === "string") result.push("chapter");
  if (!result.length && (diagnostic?.chapter_id || job.error?.chapter_id || job.current_chapter_id)) result.push("chapter");
  return result;
}

export function pauseDestinationSearch(destination: PauseDestination, job: GenerationJob, diagnostic?: GenerationDiagnostic | null) {
  const chapter = diagnostic?.chapter_id || job.error?.chapter_id || job.current_chapter_id;
  if (destination === "chapter") {
    const query = new URLSearchParams({ area: "writing", view: "chapter" });
    if (chapter) query.set("chapter", chapter);
    if (typeof diagnostic?.details.prose_run_id === "string") query.set("run", diagnostic.details.prose_run_id);
    return query.toString();
  }
  if (["baseline", "candidates", "curation"].includes(destination)) return `area=world&view=${destination}`;
  if (destination === "facts") return "area=continuity&view=facts";
  if (destination === "blueprint") return "area=blueprint&view=overview";
  return `area=auto-book&view=runs&job=${encodeURIComponent(job._id)}`;
}


export function readinessDestinations(issue: ReadinessIssue): PauseDestination[] {
  const actions = new Set(issue.action_codes);
  const has = (...codes: string[]) => codes.some((code) => actions.has(code));
  const result: PauseDestination[] = [];
  if (has("open_world_baseline")) result.push("baseline");
  if (has("review_reference_card_candidates", "open_reference_card_candidates")) result.push("candidates");
  if (has("curate_reference_cards", "review_reference_card_proposal", "reject_reference_card_proposal", "create_character_card")) result.push("curation");
  if (has("review_novel_blueprint", "review_book_structure", "review_book_structure_trash", "review_book_structure_initialization")) result.push("blueprint");
  if (has("review_provider_settings", "open_provider_settings")) result.push("settings");
  if (has("complete_prose_manually", "review_chapter_outline", "regenerate_chapter_outline", "return_to_chapters")) result.push("chapter");
  if (has("review_generation_context")) result.push("facts");
  return result;
}
