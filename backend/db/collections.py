"""MongoDB 集合名注册表：全部集合名的单一事实源。

新增集合必须先在此登记，再由仓储、索引与 backup_service 引用常量，不要散落字符串字面量。

`tests/test_backup_service.py` 断言 `BACKUP_COLLECTIONS` 恰好覆盖 `ALL_COLLECTIONS`，
于是"加了集合忘了加备份"会当场测试失败，而不是在某次恢复之后表现为数据错配——
后者是静默的：恢复只回滚备份列表里的集合，漏掉的那个原封不动，与全库对不上。
"""

from __future__ import annotations

# 在用集合：有仓储、有写入方。
NOVELS = "novels"
VOLUMES = "volumes"
CHAPTERS = "chapters"
CHARACTERS = "characters"
WORLDBOOK = "worldbook"
FACTIONS = "factions"
FACTION_RELATIONS = "faction_relations"
PLOT_THREADS = "plot_threads"
CHARACTER_STATES = "character_states"

ACTIVE_COLLECTIONS = frozenset({
    NOVELS,
    VOLUMES,
    CHAPTERS,
    CHARACTERS,
    WORLDBOOK,
    FACTIONS,
    FACTION_RELATIONS,
    PLOT_THREADS,
    CHARACTER_STATES,
})

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
