import type { useTranslations } from "next-intl";
import type { ReadinessIssue } from "./batchTypes.ts";
import {
  REFERENCE_CARD_TYPES,
  type ReferenceCardType,
} from "./referenceCardAutoCreation.ts";
import { referenceCardTypeTranslationKey } from "./referenceCardAutoCreationPresentation.ts";

type BatchTranslator = ReturnType<typeof useTranslations>;

export function readinessIssueCopy(
  issue: ReadinessIssue,
  t: BatchTranslator,
  continuationCount: number,
) {
  switch (issue.code) {
    case "novel_scale_invalid":
      return {
        title: t("readinessIssueScaleInvalidTitle"),
        body: t("readinessIssueScaleInvalidBody"),
      };
    case "book_structure_initialization_required":
      return {
        title: t("readinessIssueStructureRequiredTitle"),
        body: t("readinessIssueStructureRequiredBody", {
          count: Number(issue.details.target_chapter_count ?? 0),
        }),
      };
    case "book_structure_partial":
      return {
        title: t("readinessIssueStructurePartialTitle"),
        body: t("readinessIssueStructurePartialBody"),
      };
    case "book_structure_in_trash":
      return {
        title: t("readinessIssueStructureTrashTitle"),
        body: t("readinessIssueStructureTrashBody", {
          volumes: Number(issue.details.deleted_volume_count ?? 0),
          chapters: Number(issue.details.deleted_chapter_count ?? 0),
        }),
      };
    case "book_structure_target_invalid":
      return {
        title: t("readinessIssueStructureTargetTitle"),
        body: t("readinessIssueStructureTargetBody"),
      };
    case "book_structure_budget_not_covered":
      return {
        title: t("readinessIssueStructureBudgetTitle"),
        body: t("readinessIssueStructureBudgetBody", {
          maximum: Number(issue.details.maximum_tokens_total ?? 0),
          budget: Number(issue.details.token_budget ?? 0),
        }),
      };
    case "world_baseline_confirmation_required":
      return {
        title: t("readinessIssueWorldBaselineTitle"),
        body: t("readinessIssueWorldBaselineBody"),
      };
    case "empty_world_auto_supplement_requires_confirmation":
      return {
        title: t("readinessIssueEmptyWorldSupplementTitle"),
        body: t("readinessIssueEmptyWorldSupplementBody"),
      };
    case "character_cards_missing":
      return {
        title: t("readinessIssueCharacterCardsMissingTitle"),
        body: t("readinessIssueCharacterCardsMissingBody"),
      };
    case "world_cards_missing":
      return {
        title: t("readinessIssueWorldCardsMissingTitle"),
        body: t("readinessIssueWorldCardsMissingBody"),
      };
    case "reference_card_proposal_pending":
      return {
        title: t("readinessIssueProposalPendingTitle"),
        body: t("readinessIssueProposalPendingBody"),
      };
    case "provider_plan_invalid":
      if (issue.details.reason === "output_token_limit_missing") {
        return {
          title: t("readinessIssueOutputLimitMissingTitle"),
          body: t("readinessIssueOutputLimitMissingBody"),
        };
      }
      return {
        title: t("readinessIssueProviderInvalidTitle"),
        body: t("readinessIssueProviderInvalidBody"),
      };
    case "generation_context_too_large":
      return {
        title: t("readinessIssueContextTooLargeTitle"),
        body: t("readinessIssueContextTooLargeBody"),
      };
    case "existing_prose_without_outline_requires_manual_review":
      return {
        title: t("readinessIssueExistingProseOutlineTitle"),
        body: t("readinessIssueExistingProseOutlineBody", {
          count: Number(issue.details.chapter_count ?? 0),
        }),
      };
    case "legacy_outline_requires_v2_regeneration":
      return {
        title: t("readinessIssueLegacyOutlineTitle"),
        body: t("readinessIssueLegacyOutlineBody", {
          count: Number(issue.details.chapter_count ?? 0),
        }),
      };
    case "unknown_outline_contract_requires_manual_review":
      return {
        title: t("readinessIssueUnknownOutlineContractTitle"),
        body: t("readinessIssueUnknownOutlineContractBody", {
          count: Number(issue.details.chapter_count ?? 0),
        }),
      };
    case "no_generation_work":
      return {
        title: t("readinessIssueNoWorkTitle"),
        body: t("readinessIssueNoWorkBody"),
      };
    case "prose_scene_segmentation_planned":
      return {
        title: t("readinessIssueProseSegmentsTitle"),
        body: t("readinessIssueProseSegmentsBody", {
          segmented: Number(issue.details.scene_segment_chapters ?? 0),
          unknown: Number(issue.details.unknown_outline_chapters ?? 0),
          calls: Number(issue.details.maximum_prose_calls ?? 0),
        }),
      };
    case "prose_output_risk_requires_ack":
      return {
        title: t("readinessIssueOutputRiskTitle"),
        body: t("readinessIssueOutputRiskBody", {
          count: Number(issue.details.chapter_count ?? 0),
          target: Number(issue.details.maximum_target_words ?? 0),
          safe: Number(issue.details.safe_output_words ?? 0),
          calls: Number(issue.details.maximum_prose_calls ?? 0),
          source: issue.details.output_limit_known
            ? t("readinessCapabilityKnown")
            : t("readinessCapabilityConservative"),
        }),
      };
    case "partial_prose_requires_manual_completion":
      return {
        title: t("readinessIssuePartialProseTitle"),
        body: t("readinessIssuePartialProseBody", {
          count: Number(issue.details.chapter_count ?? 0),
        }),
      };
    case "prose_scene_divergence_protection":
      return {
        title: t("readinessIssueDivergenceProtectionTitle"),
        body: t("readinessIssueDivergenceProtectionBody", {
          factor: Number(issue.details.stop_factor ?? 0),
        }),
      };
    case "automatic_continuations_require_confirmation":
      return {
        title: t("readinessIssueAutomaticConfirmationTitle"),
        body: t("readinessIssueAutomaticConfirmationBody", {
          count: continuationCount,
        }),
      };
    case "automatic_continuations_require_token_budget":
      return {
        title: t("readinessIssueAutomaticBudgetTitle"),
        body: t("readinessIssueAutomaticBudgetBody"),
      };
    case "prose_token_bound_unproven":
      return {
        title: t("readinessIssueTokenBoundTitle"),
        body: t("readinessIssueTokenBoundBody"),
      };
    case "batch_generation_requires_token_budget":
      return {
        title: t("readinessIssueBatchBudgetRequiredTitle"),
        body: t("readinessIssueBatchBudgetRequiredBody", {
          maximum: Number(issue.details.maximum_tokens_total ?? 0),
        }),
      };
    case "automatic_token_budget_requires_confirmation":
      return {
        title: t("readinessIssueAutomaticTokenBudgetTitle"),
        body: t("readinessIssueAutomaticTokenBudgetBody", {
          maximum: Number(issue.details.maximum_tokens_total ?? 0),
        }),
      };
    case "batch_generation_budget_may_pause":
      return {
        title: t("readinessIssueBatchBudgetShortTitle"),
        body: t("readinessIssueBatchBudgetShortBody", {
          maximum: Number(issue.details.maximum_tokens_total ?? 0),
          budget: Number(issue.details.token_budget ?? 0),
        }),
      };
    case "reference_card_repair_budget_not_covered":
      return {
        title: t("readinessIssueRepairBudgetShortTitle"),
        body: t("readinessIssueRepairBudgetShortBody", {
          maximum: Number(issue.details.maximum_tokens_total ?? 0),
          budget: Number(issue.details.token_budget ?? 0),
        }),
      };
    case "batch_generation_token_bound_unproven":
      return {
        title: t("readinessIssueBatchBoundUnknownTitle"),
        body: t("readinessIssueBatchBoundUnknownBody"),
      };
    case "automatic_reference_card_creation_requires_confirmation": {
      const allowedTypes = Array.isArray(issue.details.allowed_card_types)
        ? issue.details.allowed_card_types.filter(
            (item): item is ReferenceCardType =>
              typeof item === "string"
              && REFERENCE_CARD_TYPES.includes(item as ReferenceCardType),
          )
        : [];
      return {
        title: t("readinessIssueAutoCardsTitle"),
        body: t("readinessIssueAutoCardsBody", {
          types: allowedTypes
            .map((cardType) => t(referenceCardTypeTranslationKey(cardType)))
            .join(t("referenceCardNameSeparator"))
            || t("readinessNone"),
          perChapter: Number(issue.details.max_auto_creates_per_chapter ?? 0),
          perBook: Number(issue.details.max_auto_creates_per_book ?? 0),
          repair: Number(
            issue.details.max_candidate_repair_cycles_per_chapter ?? 0,
          ),
          attempts: Number(
            issue.details.maximum_repair_provider_attempts_total ?? 0,
          ),
          tokens: Number(issue.details.maximum_repair_tokens_total ?? 0),
        }),
      };
    }
    default:
      return {
        title: t("readinessIssueUnknownTitle"),
        body: t("readinessIssueUnknownBody"),
      };
  }
}
