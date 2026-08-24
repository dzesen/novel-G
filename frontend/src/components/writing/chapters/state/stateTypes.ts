/**
 * 对齐后端 schema 的状态回填类型。
 *
 * 与细纲同一区分（2a 设计 §3.2）：**LLM 输出 schema ≠ accept 入参**。
 * ChapterStateResult 是 AI 吐出的形状（带 evidence 与 consistency_issues）；
 * accept 只发**勾上的**项，字段名不同（accepted_permanent_facts /
 * accepted_thread_updates），且不含 evidence 与 consistency_issues——
 * 后端用 extra="forbid" 校验，多发字段会 400。
 */

export type FactKind = "death" | "injury" | "identity" | "relation" | "ability";
export type StateFactDropReason =
  | "duplicate_existing_fact"
  | "duplicate_proposal"
  | "legal_no_op"
  | "unsupported_by_prose";

export interface PermanentFactProposal {
  fact: string;
  kind: FactKind;
  selection_id?: string;
}

export interface CharacterStateUpdate {
  card_id: string;
  current_state: string;
  new_permanent_facts: PermanentFactProposal[];
  selection_id?: string;
}

export interface ThreadStatusUpdate {
  thread_id: string;
  status: "developing" | "resolved";
  evidence: string;
  selection_id?: string;
}

export interface ConsistencyIssue {
  card_id: string | null;
  fact: string;
  conflict: string;
}

/** AI 输出的形状。 */
export interface ChapterStateResult {
  summary: string;
  character_updates: CharacterStateUpdate[];
  thread_updates: ThreadStatusUpdate[];
  consistency_issues: ConsistencyIssue[];
  fact_evidence: {
    evidence_schema_version: "chapter_state_fact_evidence.v1";
    extraction_status: "complete" | "complete_no_change" | "unknown";
    evidence_digest: string;
    invalid_internal_references: number;
    dangling_references: number;
  };
  proposal_id?: string;
  acceptance_token?: string;
  proposal_expires_at?: string;
}

/** accept 端点的返回。skipped_duplicate_facts 必须显示——去重不得静默（设计 §5.3）。 */
export interface ChapterStateAcceptResponse {
  chapter_id: string;
  states_updated: number;
  facts_appended: number;
  threads_updated: number;
  skipped_duplicate_facts: string[];
  timeline_revision?: number;
}
