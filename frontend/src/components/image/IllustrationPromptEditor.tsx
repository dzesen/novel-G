"use client";

import {
  illustrationPromptCharacterCount,
} from "@/lib/illustrationPrompt";
import type { IllustrationPromptResult } from "@/types/agent";

const PROMPT_FIELDS: Array<keyof IllustrationPromptResult> = [
  "subject",
  "appearance",
  "scene",
  "style",
  "negative",
];

interface IllustrationPromptEditorProps {
  prompt: IllustrationPromptResult;
  labels: Record<keyof IllustrationPromptResult, string>;
  totalLabel: (count: number) => string;
  onChange: (
    field: keyof IllustrationPromptResult,
    value: string,
  ) => void;
}

export default function IllustrationPromptEditor({
  prompt,
  labels,
  totalLabel,
  onChange,
}: IllustrationPromptEditorProps) {
  return (
    <div className="mt-5 grid min-w-0 gap-4 md:grid-cols-2">
      {PROMPT_FIELDS.map((field) => (
        <label
          key={field}
          className={`min-w-0 text-sm ${
            field === "scene" ? "md:col-span-2" : ""
          }`}
        >
          <span className="mb-1.5 block font-medium text-foreground">
            {labels[field]}
          </span>
          <textarea
            aria-label={labels[field]}
            data-testid={`illustration-prompt-${field}`}
            value={prompt[field]}
            onChange={(event) => onChange(field, event.target.value)}
            rows={field === "scene" ? 4 : 3}
            className="w-full min-w-0 resize-y rounded-lg border border-border bg-surface px-3 py-2.5 text-sm leading-6 text-foreground outline-none focus:border-accent focus:ring-2 focus:ring-accent/15"
          />
        </label>
      ))}
      <p className="text-xs text-muted md:col-span-2">
        {totalLabel(illustrationPromptCharacterCount(prompt))}
      </p>
    </div>
  );
}
