"""MongoDB 集合名注册表：全部集合名的单一事实源。

新增集合必须先在此登记，再由仓储、索引与 backup_service 引用常量，不要散落字符串字面量。

`tests/test_backup_service.py` 断言 `BACKUP_COLLECTIONS` 恰好覆盖 `ALL_COLLECTIONS`，
于是"加了集合忘了加备份"会当场测试失败，而不是在某次恢复之后表现为数据错配——
后者是静默的：恢复只回滚备份列表里的集合，漏掉的那个原封不动，与全库对不上。
"""

from __future__ import annotations

# 在用集合：有仓储、有写入方。
NOVELS = "novels"
USERS = "users"
VOLUMES = "volumes"
CHAPTERS = "chapters"
CHARACTERS = "characters"
WORLDBOOK = "worldbook"
FACTIONS = "factions"
FACTION_RELATIONS = "faction_relations"
PLOT_THREADS = "plot_threads"
CHARACTER_STATES = "character_states"
AGENT_DEFINITIONS = "agent_definitions"
AGENT_RUNS = "agent_runs"
AGENT_REVISION_PROPOSALS = "agent_revision_proposals"

# 批量生成作业（阶段 3）。注意与遗留幽灵 GENERATION_TASKS 撞名但**不同集合**：
# 本集合有写入方（作业引擎），generation_tasks 无。级联 stats key 用规则名
# generation_jobs_deleted，勿沿用 generation_tasks 那条不规则的 tasks_deleted。
GENERATION_JOBS = "generation_jobs"
PROSE_RUNS = "prose_runs"
CHAPTER_STATE_DELTAS = "chapter_state_deltas"
CHARACTER_STATE_SNAPSHOTS = "character_state_snapshots"
PLOT_THREAD_EVENTS = "plot_thread_events"
MANUAL_CORRECTIONS = "manual_corrections"
STATE_PREVIEWS = "state_previews"
REFERENCE_CARD_PROPOSALS = "reference_card_proposals"
CARD_IMPORT_PROPOSALS = "card_import_proposals"
MUTATION_JOURNALS = "mutation_journals"
IMAGE_ASSETS = "image_assets"
IMAGE_JOBS = "image_jobs"
CHARACTER_VISUAL_PROFILES = "character_visual_profiles"
ILLUSTRATION_BRIEFS = "illustration_briefs"

ACTIVE_COLLECTIONS = frozenset({
    NOVELS,
    USERS,
    VOLUMES,
    CHAPTERS,
    CHARACTERS,
    WORLDBOOK,
    FACTIONS,
    FACTION_RELATIONS,
    PLOT_THREADS,
    CHARACTER_STATES,
    AGENT_DEFINITIONS,
    AGENT_RUNS,
    AGENT_REVISION_PROPOSALS,
    GENERATION_JOBS,
    PROSE_RUNS,
    CHAPTER_STATE_DELTAS,
    CHARACTER_STATE_SNAPSHOTS,
    PLOT_THREAD_EVENTS,
    MANUAL_CORRECTIONS,
    STATE_PREVIEWS,
    REFERENCE_CARD_PROPOSALS,
    CARD_IMPORT_PROPOSALS,
    MUTATION_JOURNALS,
    IMAGE_ASSETS,
    IMAGE_JOBS,
    CHARACTER_VISUAL_PROFILES,
    ILLUSTRATION_BRIEFS,
})

# 运行时集合不会进入备份。会话在恢复后必须重新建立，避免把可用凭据材料
# 搬进快照，也避免恢复旧用户状态后意外复活旧会话。
AUTH_SESSIONS = "auth_sessions"
AUTH_LOGIN_ATTEMPTS = "auth_login_attempts"
EPHEMERAL_COLLECTIONS = frozenset({AUTH_SESSIONS, AUTH_LOGIN_ATTEMPTS})

# 遗留集合：仓储模块已删除，当前无任何写入方。
# 仍保留的原因：novel_service.hard_delete_novel 仍按 novel_id 做防御性清理，
# 备份也仍收录，以免历史库中的残留数据在恢复时被静默丢弃。
#
# 曾经的第四个幽灵 arcs 已于 2026-07-17 按 §4.2 方案 A 彻底清除：审计确认它在全部
# 224 个提交里从无一处 insert，那套级联是为一个从未存在过数据的集合写的防御。
# 余下三个未同等处置——它们的历史仓储真的写过库，老用户可能存有数据。
OUTLINES = "outlines"
GENERATION_TASKS = "generation_tasks"
MEMORY_FRAGMENTS = "memory_fragments"

LEGACY_COLLECTIONS = frozenset({
    OUTLINES,
    GENERATION_TASKS,
    MEMORY_FRAGMENTS,
})

ALL_COLLECTIONS = ACTIVE_COLLECTIONS | LEGACY_COLLECTIONS
REGISTERED_COLLECTIONS = ALL_COLLECTIONS | EPHEMERAL_COLLECTIONS

# 小说作用域集合：除 NOVELS（被删除的根记录，按 _id 删）外，其余集合
# 的记录都带 novel_id。唯一允许为空的是建书前的 CARD_IMPORT_PROPOSALS；
# hard_delete_novel 只清理其中已绑定当前小说的记录，不能误删 null 提案。
# 级联清理仍必须覆盖这里的每一个集合，包括上面三个遗留幽灵集合。
# tests/test_novel_service.py 拿它核对级联的完整性，用法与
# BACKUP_COLLECTIONS 的覆盖测试同源。
NOVEL_SCOPED_COLLECTIONS = ALL_COLLECTIONS - {NOVELS, USERS, AGENT_DEFINITIONS}
