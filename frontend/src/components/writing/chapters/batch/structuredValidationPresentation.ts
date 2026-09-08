const ERROR_KEYS = {
  missing: "diagnosticsValidationMissing",
  json_invalid: "diagnosticsValidationJson",
  json_decode_error: "diagnosticsValidationJson",
  literal_error: "diagnosticsValidationChoice",
  enum: "diagnosticsValidationChoice",
  string_too_long: "diagnosticsValidationTextTooLong",
  string_too_short: "diagnosticsValidationTextTooShort",
  too_long: "diagnosticsValidationTooLong",
  too_short: "diagnosticsValidationTooShort",
  list_type: "diagnosticsValidationList",
  dict_type: "diagnosticsValidationObject",
  model_type: "diagnosticsValidationObject",
  model_attributes_type: "diagnosticsValidationObject",
  string_type: "diagnosticsValidationText",
  int_type: "diagnosticsValidationInteger",
  int_parsing: "diagnosticsValidationInteger",
  greater_than: "diagnosticsValidationNumberRange",
  greater_than_equal: "diagnosticsValidationNumberRange",
  less_than: "diagnosticsValidationNumberRange",
  less_than_equal: "diagnosticsValidationNumberRange",
  extra_forbidden: "diagnosticsValidationExtra",
} as const;

const FIELD_KEYS = {
  title: "diagnosticsFieldTitle",
  scenes: "diagnosticsFieldScenes",
  present_character_card_ids: "diagnosticsFieldCharacterIds",
  reference_card_ids: "diagnosticsFieldReferenceIds",
  character_card_id: "diagnosticsFieldCharacterId",
  card_id: "diagnosticsFieldCardId",
  target_word_count: "diagnosticsFieldWordTarget",
  word_count: "diagnosticsFieldWordCount",
  narrative_delta: "diagnosticsFieldNarrativeDelta",
  dimension: "diagnosticsFieldDimension",
  content: "diagnosticsFieldContent",
  scene_id: "diagnosticsFieldSceneId",
  summary: "diagnosticsFieldSummary",
} as const;

export function structuredValidationErrorKey(errorType: string) {
  return Object.hasOwn(ERROR_KEYS, errorType)
    ? ERROR_KEYS[errorType as keyof typeof ERROR_KEYS]
    : "diagnosticsValidationConstraintUnknown";
}

export function structuredValidationFieldKey(path: string) {
  if (path === "$") return "diagnosticsFieldResponse";
  const field = path.replace(/\[\d+\]/g, "").split(".").at(-1) ?? "";
  return Object.hasOwn(FIELD_KEYS, field)
    ? FIELD_KEYS[field as keyof typeof FIELD_KEYS]
    : "diagnosticsFieldOther";
}

export function structuredValidationSceneNumber(path: string) {
  const match = /^scenes\[(\d+)\](?:\.|$)/.exec(path);
  return match ? Number(match[1]) + 1 : null;
}
