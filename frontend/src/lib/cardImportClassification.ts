const REASON_KEYS = {
  explicit_metadata: "referenceCardClassification.explicitMetadata",
  explicit_title: "referenceCardClassification.explicitTitle",
  structured_fields: "referenceCardClassification.structuredFields",
  conflicting_markers: "referenceCardClassification.conflictingMarkers",
  unclassified: "referenceCardClassification.unclassified",
} as const;

export function cardImportClassificationKey(classification: {
  schema_version: string;
  reason_code: string;
} | undefined) {
  if (classification?.schema_version !== "worldbook_classification.v1") return null;
  return Object.hasOwn(REASON_KEYS, classification.reason_code)
    ? REASON_KEYS[classification.reason_code as keyof typeof REASON_KEYS]
    : "referenceCardClassification.unclassified";
}
