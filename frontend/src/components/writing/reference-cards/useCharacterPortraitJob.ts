"use client";

import type { CharacterPortraitJob } from "@/types/image";
import { useImageJob } from "@/components/image/useImageJob";

interface UseCharacterPortraitJobOptions {
  novelId: string;
  cardId: string;
}

export function useCharacterPortraitJob({
  novelId,
  cardId,
}: UseCharacterPortraitJobOptions) {
  const jobBase = `/api/reference-cards/novel/${novelId}/character/${cardId}/portrait/jobs`;
  return useImageJob<CharacterPortraitJob>({ jobBase });
}
