export type StoryHealthDueState =
  | "unscheduled"
  | "upcoming"
  | "due"
  | "overdue"
  | "unmapped"
  | "not_started";

export type StoryHealthDeviationState =
  | "under"
  | "within_target"
  | "over"
  | "no_content"
  | "no_chapters";

export interface StoryHealthChapterPosition {
  chapter_id: string;
  volume_id: string;
  volume_order: number;
  chapter_order: number;
  book_ordinal: number;
  volume_title: string;
  chapter_title: string;
}

export interface PlotThreadHealth {
  thread_id: string;
  name: string;
  status: string;
  importance: string;
  planted_at: StoryHealthChapterPosition | null;
  due_target: Record<string, unknown> | null;
  due_at: StoryHealthChapterPosition | null;
  due_book_ordinal: number | null;
  age_in_chapters: number | null;
  due_state: StoryHealthDueState;
  chapters_until_due: number | null;
  overdue_by_chapters: number | null;
  attention_required: boolean;
  unavailable_reason: string | null;
}

export interface CharacterAbsenceHealth {
  card_id: string;
  name: string;
  importance: string;
  last_present_at: StoryHealthChapterPosition | null;
  consecutive_absent_chapters: number;
  observed_outline_chapters: number;
  never_present: boolean;
  currently_absent: boolean;
}

export interface ChapterWordCountHealth {
  chapter_id: string;
  volume_id: string;
  volume_order: number;
  chapter_order: number;
  book_ordinal: number;
  chapter_title: string;
  actual_word_count: number;
  target_word_count: number;
  target_source: "chapter_outline" | "novel_default" | "system_default";
  delta_word_count: number;
  completion_ratio: number;
  deviation_ratio: number;
  deviation_state: StoryHealthDeviationState;
  attention_required: boolean;
}

export interface VolumeWordCountHealth {
  volume_id: string;
  volume_order: number;
  volume_title: string;
  chapter_count: number;
  chapters_with_content: number;
  actual_word_count: number;
  target_word_count: number;
  delta_word_count: number;
  completion_ratio: number | null;
  deviation_ratio: number | null;
  deviation_state: StoryHealthDeviationState;
  attention_required: boolean;
}

export interface StoryHealthReport {
  schema_version: "story_health.v1";
  novel_id: string;
  scope: {
    kind: "book" | "volume";
    volume_id: string | null;
  };
  as_of: StoryHealthChapterPosition | null;
  policies: {
    timeline_ordering: string;
    progress_basis: string;
    character_presence_basis: string;
    chapter_target_precedence: string;
    word_deviation_attention_ratio: number;
  };
  observation: {
    active_chapter_count: number;
    progress_chapter_count: number;
    outlined_chapter_count: number;
    chapters_with_content: number;
  };
  summary: {
    active_plot_thread_count: number;
    due_plot_thread_count: number;
    overdue_plot_thread_count: number;
    unmapped_plot_thread_count: number;
    character_count: number;
    currently_absent_character_count: number;
    chapter_word_deviation_count: number;
    volume_word_deviation_count: number;
  };
  plot_threads: PlotThreadHealth[];
  character_absences: CharacterAbsenceHealth[];
  word_counts: {
    volumes: VolumeWordCountHealth[];
    chapters: ChapterWordCountHealth[];
  };
}
