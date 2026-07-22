export type FactKind = "death" | "injury" | "identity" | "relation" | "ability";

export interface PermanentFact {
  id: string;
  chapter_order: number;
  source_chapter_id?: string | null;
  fact: string;
  kind: FactKind;
  created_at?: string;
}

export interface CharacterState {
  _id: string;
  novel_id: string;
  card_id: string;
  current_state: string;
  as_of_chapter_order: number;
  as_of_chapter_id?: string | null;
  permanent_facts: PermanentFact[];
}
