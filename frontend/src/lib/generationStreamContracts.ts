import type { SSECompletionContract } from "./api";

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function structuredGenerationStream(
  resultKeys: readonly string[],
): SSECompletionContract {
  return {
    terminalEvent: "done",
    validateTerminal: (data) => data.success === false || (
      data.success === true
      && isRecord(data.result)
      && resultKeys.every((key) => isRecord((data.result as Record<string, unknown>)[key]))
    ),
  };
}

export const blueprintGenerationStream = structuredGenerationStream([
  "extract_idea", "core_seed", "novel_meta",
]);

export const proseGenerationStream: SSECompletionContract = {
  terminalEvent: "done",
  validateTerminal: (data) => data.success === false || (
    data.success === true && typeof data.text === "string"
  ),
};
