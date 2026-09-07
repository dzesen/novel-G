import type {
  AICreateResponse,
  BlueprintGenerationSource,
  CardImportCreationSelection,
  WritingDraft,
} from "@/types/novel";
import { normalizeAuthorConstraints } from "./authorInput.ts";

export const WRITING_DRAFT_SCHEMA_VERSION = 1 as const;

export type NovelCreationOrigin = NonNullable<
  WritingDraft["_creationOrigin"]
>;

interface GeneratedDraftOptions {
  result: AICreateResponse;
  generationSource: BlueprintGenerationSource;
  origin: Exclude<NovelCreationOrigin, "blank">;
  cardCreationId?: string;
  cardImports?: CardImportCreationSelection[];
  cardAvatarProposalIds?: string[];
}

/** 创建三种建书入口共同使用的空白蓝图草稿。 */
export function createBlankWritingDraft(): WritingDraft {
  return {
    _draftSchemaVersion: WRITING_DRAFT_SCHEMA_VERSION,
    _creationOrigin: "blank",
    title: "",
    tags: [],
    creation_mode: "manual",
  };
}

/** 把 AI 创意或卡驱动结果归一为同一份可编辑蓝图草稿。 */
export function createGeneratedWritingDraft({
  result,
  generationSource,
  origin,
  cardCreationId,
  cardImports,
  cardAvatarProposalIds,
}: GeneratedDraftOptions): WritingDraft {
  const meta = result.novel_meta;
  const plot = result.expand_idea?.plot ?? result.extract_idea.plot ?? "";
  const constraints = normalizeAuthorConstraints(generationSource.author_constraints);
  if (constraints === null) throw new Error("Invalid author constraints");

  return {
    _fromAI: true,
    _draftSchemaVersion: WRITING_DRAFT_SCHEMA_VERSION,
    _creationOrigin: origin,
    title: meta.title,
    subtitle: meta.subtitle,
    genre: result.extract_idea.genre,
    tags: meta.tags,
    introduction: meta.introduction,
    summary: meta.summary,
    core_seed: result.core_seed.core_seed,
    worldview: meta.worldview,
    writing_style: meta.writing_style,
    narrative_pov: meta.narrative_pov,
    era_background: meta.era_background,
    plot,
    tone: result.extract_idea.tone,
    target_audience: result.extract_idea.target_audience,
    core_idea: result.extract_idea.core_idea,
    number_of_chapters: generationSource.number_of_chapters,
    words_per_chapter: generationSource.words_per_chapter,
    creation_mode: "ai",
    author_input: {
      original_idea: generationSource.user_idea,
      creative_direction: generationSource.creative_direction,
      constraints,
    },
    _generationSource: generationSource,
    ...(generationSource.creative_direction && {
      creative_direction: generationSource.creative_direction,
    }),
    ...(cardCreationId && { card_creation_id: cardCreationId }),
    ...(cardImports && { card_imports: cardImports }),
    ...(cardAvatarProposalIds && {
      card_avatar_proposal_ids: cardAvatarProposalIds,
    }),
  };
}

/**
 * 浏览器存储属于不可信兼容输入。这里只验证创建页恢复所需的最小闭集，
 * 旧版未带 schema/origin 的合法草稿继续可恢复。
 */
export function isWritingDraft(value: unknown): value is WritingDraft {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }

  const draft = value as Record<string, unknown>;
  if (typeof draft.title !== "string") {
    return false;
  }
  if (
    draft._draftSchemaVersion !== undefined &&
    draft._draftSchemaVersion !== WRITING_DRAFT_SCHEMA_VERSION
  ) {
    return false;
  }
  if (
    draft._creationOrigin !== undefined &&
    !["blank", "ai_idea", "tavern_cards"].includes(
      String(draft._creationOrigin),
    )
  ) {
    return false;
  }
  return (
    draft.creation_mode === undefined ||
    draft.creation_mode === "manual" ||
    draft.creation_mode === "ai"
  );
}
