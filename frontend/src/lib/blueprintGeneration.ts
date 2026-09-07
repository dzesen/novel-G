import { isBlueprintStrategy, normalizeBlueprintExecution, normalizeBlueprintParams } from "./blueprintRunClient.ts";
import type { CreativeDirectionSelection } from "@/types/agent";
import { normalizeAuthorConstraints } from "./authorInput.ts";
import type {
  AICreateRequest,
  AICreateResponse,
  BlueprintGenerationSource,
  CardImportDirectionReference,
  WritingDraft,
} from "@/types/novel";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown, maxLength = 100_000): value is string {
  return (
    typeof value === "string" &&
    value.trim().length > 0 &&
    value.length <= maxLength
  );
}

function normalizeCardImports(
  value: unknown,
): CardImportDirectionReference[] | null {
  if (!Array.isArray(value) || value.length > 100) return null;
  const normalized: CardImportDirectionReference[] = [];
  for (const item of value) {
    if (
      !isRecord(item) ||
      !isNonEmptyString(item.proposal_id, 200) ||
      typeof item.digest !== "string" ||
      !/^[0-9a-f]{64}$/.test(item.digest)
    ) {
      return null;
    }
    normalized.push({
      proposal_id: item.proposal_id.trim(),
      digest: item.digest,
    });
  }
  return normalized;
}

function normalizeCreativeDirection(
  value: unknown,
): CreativeDirectionSelection | null | undefined {
  if (value === null) return null;
  if (!isRecord(value) || !isRecord(value.direction)) return undefined;
  const direction = value.direction;
  const stringFields = [
    "title",
    "pitch",
    "core_conflict",
    "protagonist_arc",
    "story_engine",
    "world_hook",
    "tone_and_style",
  ] as const;
  if (
    !isNonEmptyString(value.agent_id, 200) ||
    typeof value.agent_version !== "number" ||
    !Number.isInteger(value.agent_version) ||
    value.agent_version < 1 ||
    !stringFields.every((field) => isNonEmptyString(direction[field])) ||
    !Array.isArray(direction.must_keep) ||
    !direction.must_keep.every((item) => typeof item === "string") ||
    !Array.isArray(direction.risks) ||
    !direction.risks.every((item) => typeof item === "string") ||
    (value.provider_alias !== null &&
      typeof value.provider_alias !== "string") ||
    typeof value.user_adjustments !== "string" ||
    (value.card_context_digest !== undefined &&
      value.card_context_digest !== null &&
      (typeof value.card_context_digest !== "string" ||
        !/^[0-9a-f]{64}$/.test(value.card_context_digest)))
  ) {
    return undefined;
  }
  return {
    agent_id: value.agent_id.trim(),
    agent_version: value.agent_version,
    provider_alias: value.provider_alias || null,
    direction: {
      title: direction.title as string,
      pitch: direction.pitch as string,
      core_conflict: direction.core_conflict as string,
      protagonist_arc: direction.protagonist_arc as string,
      story_engine: direction.story_engine as string,
      world_hook: direction.world_hook as string,
      tone_and_style: direction.tone_and_style as string,
      must_keep: [...direction.must_keep] as string[],
      risks: [...direction.risks] as string[],
    },
    user_adjustments: value.user_adjustments,
    ...(value.card_context_digest && {
      card_context_digest: value.card_context_digest,
    }),
  };
}

/** 浏览器草稿是不可信输入；只有完整且仍绑定原来源的快照才能付费重跑。 */
export function normalizeBlueprintGenerationSource(
  value: unknown,
): BlueprintGenerationSource | null {
  if (
    !isRecord(value) ||
    value.schema_version !== "blueprint_generation_source.v1" ||
    !isNonEmptyString(value.user_idea) ||
    typeof value.number_of_chapters !== "number" ||
    !Number.isInteger(value.number_of_chapters) ||
    value.number_of_chapters < 1 ||
    value.number_of_chapters > 10_000 ||
    typeof value.words_per_chapter !== "number" ||
    !Number.isInteger(value.words_per_chapter) ||
    value.words_per_chapter < 500 ||
    value.words_per_chapter > 50_000
  ) {
    return null;
  }
  const creativeDirection = normalizeCreativeDirection(
    value.creative_direction,
  );
  const cardImports = normalizeCardImports(value.card_imports);
  if (value.strategy !== undefined && !isBlueprintStrategy(value.strategy)) return null;
  if (value.generation_params !== undefined && !normalizeBlueprintParams(value.generation_params)) return null;
  const constraints = normalizeAuthorConstraints(value.author_constraints);
  if (constraints === null) return null;
  if (creativeDirection === undefined || cardImports === null) return null;
  return {
    schema_version: "blueprint_generation_source.v1",
    ...(isBlueprintStrategy(value.strategy) && { strategy: value.strategy }),
    ...(normalizeBlueprintExecution(value.execution) && { execution: normalizeBlueprintExecution(value.execution) }),
    ...(normalizeBlueprintParams(value.generation_params) && { generation_params: normalizeBlueprintParams(value.generation_params) }),
    user_idea: value.user_idea.trim(),
    number_of_chapters: value.number_of_chapters,
    words_per_chapter: value.words_per_chapter,
    creative_direction: creativeDirection,
    card_imports: cardImports,
    ...(value.author_constraints !== undefined && { author_constraints: constraints }),
  };
}

export type BlueprintRegenerationInspection =
  | { allowed: true; source: BlueprintGenerationSource; blocked_code: null }
  | {
      allowed: false;
      source: null;
      blocked_code:
        | "blueprint_regeneration_not_available"
        | "blueprint_source_binding_stale"
        | "card_import_binding_stale";
    };

/** 在发起请求前重验创建来源和卡审核绑定，阻断不可信浏览器草稿。 */
export function inspectBlueprintRegeneration(
  draft: WritingDraft,
): BlueprintRegenerationInspection {
  if (
    draft._creationOrigin !== "ai_idea" &&
    draft._creationOrigin !== "tavern_cards"
  ) {
    return {
      allowed: false,
      source: null,
      blocked_code: "blueprint_regeneration_not_available",
    };
  }
  let source = normalizeBlueprintGenerationSource(
    draft._generationSource,
  );
  if (!source) {
    return {
      allowed: false,
      source: null,
      blocked_code: "blueprint_source_binding_stale",
    };
  }
  // Explicitly edited requirements are authoritative input for a new preview.
  // Generated summary/plot edits never reconstruct or replace that input.
  if (draft.author_input) {
    const updated = normalizeBlueprintGenerationSource({
      ...source,
      user_idea: draft.author_input.original_idea,
      creative_direction: draft.author_input.creative_direction,
      author_constraints: draft.author_input.constraints,
      number_of_chapters: draft.number_of_chapters ?? source.number_of_chapters,
      words_per_chapter: draft.words_per_chapter ?? source.words_per_chapter,
    });
    if (!updated || draft.author_input.original_idea.length > 8000 || JSON.stringify(draft.author_input).length > 24000) {
      return { allowed: false, source: null, blocked_code: "blueprint_source_binding_stale" };
    }
    source = updated;
  }
  if (draft._creationOrigin === "tavern_cards") {
    const expected = source.card_imports.map(({ proposal_id, digest }) => ({
      proposal_id,
      digest,
    }));
    const current = (draft.card_imports ?? []).map(
      ({ proposal_id, digest }) => ({ proposal_id, digest }),
    );
    if (
      !source.creative_direction?.card_context_digest ||
      JSON.stringify(current) !== JSON.stringify(expected)
    ) {
      return {
        allowed: false,
        source: null,
        blocked_code: "card_import_binding_stale",
      };
    }
  }
  return { allowed: true, source, blocked_code: null };
}

/** 整版重跑使用明确的创作输入，不携带旧步骤或从派生剧情反推约束。 */
export function buildBlueprintRegenerationRequest(
  source: BlueprintGenerationSource,
): AICreateRequest {
  return {
    ...source.generation_params,
    ...(source.strategy && { strategy: source.strategy }),
    user_idea: source.user_idea,
    number_of_chapters: source.number_of_chapters,
    words_per_chapter: source.words_per_chapter,
    ...(source.author_constraints && { author_constraints: source.author_constraints }),
    ...(source.creative_direction && {
      creative_direction: source.creative_direction,
    }),
  };
}

/** 把已确认的新候选作为一个整体写回，同时保留来源绑定与非生成设置。 */
export function applyRegeneratedBlueprint(
  draft: WritingDraft,
  result: AICreateResponse,
  source?: BlueprintGenerationSource,
): WritingDraft {
  const meta = result.novel_meta;
  return {
    ...draft,
    ...(source && { _generationSource: source }),
    _fromAI: true,
    title: meta.title,
    subtitle: meta.subtitle,
    genre: result.extract_idea.genre,
    tags: [...meta.tags],
    introduction: meta.introduction,
    summary: meta.summary,
    core_seed: result.core_seed.core_seed,
    worldview: meta.worldview,
    writing_style: meta.writing_style,
    narrative_pov: meta.narrative_pov,
    era_background: meta.era_background,
    plot: result.expand_idea?.plot ?? result.extract_idea.plot ?? "",
    tone: result.extract_idea.tone,
    target_audience: result.extract_idea.target_audience,
    core_idea: result.extract_idea.core_idea,
  };
}
