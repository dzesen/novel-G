export type StoryHealthIssueTarget = {
  issueId: string;
  kind: "thread" | "character" | "chapter" | "volume";
  ownerId: string;
};

interface StoryHealthIssueSource {
  plot_threads: Array<{ thread_id: string; attention_required: boolean }>;
  character_absences: Array<{ card_id: string; currently_absent: boolean }>;
  word_counts: {
    chapters: Array<{ chapter_id: string; attention_required: boolean }>;
    volumes: Array<{ volume_id: string; attention_required: boolean }>;
  };
}

export function storyHealthIssueTargets(
  report: StoryHealthIssueSource,
): StoryHealthIssueTarget[] {
  return [
    ...report.plot_threads
      .filter((thread) => thread.attention_required)
      .map((thread) => ({
        issueId: `story-health:thread:${thread.thread_id}`,
        kind: "thread" as const,
        ownerId: thread.thread_id,
      })),
    ...report.character_absences
      .filter((character) => character.currently_absent)
      .map((character) => ({
        issueId: `story-health:character:${character.card_id}`,
        kind: "character" as const,
        ownerId: character.card_id,
      })),
    ...report.word_counts.chapters
      .filter((chapter) => chapter.attention_required)
      .map((chapter) => ({
        issueId: `story-health:chapter:${chapter.chapter_id}`,
        kind: "chapter" as const,
        ownerId: chapter.chapter_id,
      })),
    ...report.word_counts.volumes
      .filter((volume) => volume.attention_required)
      .map((volume) => ({
        issueId: `story-health:volume:${volume.volume_id}`,
        kind: "volume" as const,
        ownerId: volume.volume_id,
      })),
  ];
}
