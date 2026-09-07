import pymongo
import logging
from pymongo.asynchronous.collection import AsyncCollection
from backend.db import collections
from backend.db.mongo import get_database

logger = logging.getLogger(__name__)


async def _drop_legacy_unique_index(
    collection: AsyncCollection,
    expected_key: dict[str, int],
    new_index_name: str,
) -> None:
    """删除软删除改造前遗留的全量唯一索引。

    Args:
        collection: 需要检查的 MongoDB 集合。
        expected_key: 旧索引的 key 定义。
        new_index_name: 新 partial unique 索引名称，用于避免误删。

    Returns:
        无。
    """
    # PyMongo Async 的 list_indexes() 是协程，必须先 await 得到异步游标。
    async with await collection.list_indexes() as cursor:
        async for index in cursor:
            index_name = index.get("name")
            if index_name in {"_id_", new_index_name}:
                continue

            # 只删除 key 完全一致、unique=true 且没有 partialFilterExpression 的旧索引。
            if (
                dict(index.get("key", {})) == expected_key
                and index.get("unique") is True
                and "partialFilterExpression" not in index
            ):
                await collection.drop_index(index_name)
                logger.info("已删除旧全量唯一索引 %s.%s", collection.name, index_name)


async def _migrate_agent_runtime_readiness_ttl(
    collection: AsyncCollection,
) -> None:
    """Move readiness expiry off the authorization deadline field.

    Bound readiness envelopes must remain available as the durable reservation for
    a start_request_id, even after their original inspection deadline.  Normalize
    documents as well as the index so upgrades remain correct whether the old or
    new index already exists.
    """
    name = "agent_runtime_readiness_expiry_ttl"
    existing = (await collection.index_information()).get(name)
    if existing is not None:
        key = list(existing.get("key") or [])
        expire_after = existing.get("expireAfterSeconds")
        if key == [("expires_at", 1)] and expire_after == 0:
            await collection.drop_index(name)
            logger.info("已迁移 Agent Runtime readiness TTL 索引。")
        elif key != [("ttl_expires_at", 1)] or expire_after != 0:
            raise RuntimeError(
                "Agent Runtime readiness TTL index has unexpected options"
            )

    await collection.update_many(
        {"status": "bound", "ttl_expires_at": {"$exists": True}},
        {"$unset": {"ttl_expires_at": ""}},
    )
    await collection.update_many(
        {
            "status": {"$in": ["inspected", "expired"]},
            "ttl_expires_at": {"$exists": False},
            "expires_at": {"$type": "date"},
        },
        [{"$set": {"ttl_expires_at": "$expires_at"}}],
    )


async def init_novel_indexes():
    """初始化novels集合的索引。"""
    try:
        db = get_database()
        novels_collection = db[collections.NOVELS]
        
        logger.info("正在初始化'novels'集合的索引...")
        
        indexes = [
            pymongo.IndexModel([
                ("owner_id", pymongo.ASCENDING),
                ("is_deleted", pymongo.ASCENDING),
                ("updated_at", pymongo.DESCENDING),
            ]),
            # 单字段索引
            pymongo.IndexModel([("title", pymongo.ASCENDING)]),
            pymongo.IndexModel([("status", pymongo.ASCENDING)]),
            pymongo.IndexModel([("tags", pymongo.ASCENDING)]),
            pymongo.IndexModel([("updated_at", pymongo.DESCENDING)]),
            pymongo.IndexModel([("is_deleted", pymongo.ASCENDING)]),
            
            # 书架列表：按is_deleted过滤，按updated_at降序排序
            pymongo.IndexModel([
                ("is_deleted", pymongo.ASCENDING),
                ("updated_at", pymongo.DESCENDING)
            ]),
            
            # 标题搜索：按is_deleted过滤，按标题排序或查询
            pymongo.IndexModel([
                ("is_deleted", pymongo.ASCENDING),
                ("title", pymongo.ASCENDING)
            ]),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    (
                        "creation_provenance.card_imports.creation_id",
                        pymongo.ASCENDING,
                    ),
                ],
                unique=True,
                partialFilterExpression={
                    "creation_provenance.card_imports.creation_id": {
                        "$type": "string"
                    }
                },
                name="novels_owner_card_creation_id_unique",
            ),
        ]
        
        await novels_collection.create_indexes(indexes)
        logger.info("成功初始化'novels'集合的索引。")
    except Exception as e:
        logger.error(f"初始化novel索引失败：{e}")


async def init_identity_indexes():
    """初始化本地用户和不透明会话索引。"""
    try:
        db = get_database()
        await db[collections.USERS].create_indexes([
            pymongo.IndexModel(
                [("normalized_username", pymongo.ASCENDING)],
                unique=True,
                partialFilterExpression={"is_deleted": False},
                name="users_active_normalized_username_unique",
            ),
            pymongo.IndexModel(
                [("bootstrap_slot", pymongo.ASCENDING)],
                unique=True,
                partialFilterExpression={
                    "bootstrap_slot": "initial",
                    "is_deleted": False,
                },
                name="users_initial_admin_unique",
            ),
            pymongo.IndexModel([("role", pymongo.ASCENDING), ("status", pymongo.ASCENDING)]),
        ])
        await db[collections.AUTH_SESSIONS].create_indexes([
            pymongo.IndexModel(
                [("token_digest", pymongo.ASCENDING)],
                unique=True,
                name="auth_sessions_token_digest_unique",
            ),
            pymongo.IndexModel([("user_id", pymongo.ASCENDING), ("revoked_at", pymongo.ASCENDING)]),
            pymongo.IndexModel(
                [("expires_at", pymongo.ASCENDING)],
                expireAfterSeconds=0,
                name="auth_sessions_expiry_ttl",
            ),
        ])
        await db[collections.AUTH_LOGIN_ATTEMPTS].create_indexes([
            pymongo.IndexModel(
                [("expires_at", pymongo.ASCENDING)],
                expireAfterSeconds=0,
                name="auth_login_attempts_expiry_ttl",
            ),
        ])
        logger.info("成功初始化本地用户与认证会话索引。")
    except Exception as exc:
        logger.error("初始化认证索引失败：%s", exc)


async def init_agent_definition_indexes():
    """初始化用户 Agent 定义的唯一性、可见性与列表索引。"""
    try:
        db = get_database()
        await db[collections.AGENT_DEFINITIONS].create_indexes([
            pymongo.IndexModel(
                [("agent_id", pymongo.ASCENDING)],
                unique=True,
                name="agent_definitions_agent_id_unique",
            ),
            pymongo.IndexModel([
                ("owner_id", pymongo.ASCENDING),
                ("is_deleted", pymongo.ASCENDING),
                ("updated_at", pymongo.DESCENDING),
            ]),
            pymongo.IndexModel([
                ("visibility", pymongo.ASCENDING),
                ("enabled", pymongo.ASCENDING),
                ("capability", pymongo.ASCENDING),
            ]),
        ])
        logger.info("成功初始化 Agent 定义索引。")
    except Exception as exc:
        logger.error("初始化 Agent 定义索引失败：%s", exc)


async def init_agent_workbench_indexes():
    """初始化 Agent 运行历史与修订提案查询索引。"""
    try:
        db = get_database()
        await db[collections.AGENT_RUNS].create_indexes([
            pymongo.IndexModel([
                ("actor_id", pymongo.ASCENDING),
                ("novel_id", pymongo.ASCENDING),
                ("created_at", pymongo.DESCENDING),
            ]),
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("status", pymongo.ASCENDING),
                ("updated_at", pymongo.DESCENDING),
            ]),
        ])
        await db[collections.AGENT_REVISION_PROPOSALS].create_indexes([
            pymongo.IndexModel([
                ("actor_id", pymongo.ASCENDING),
                ("novel_id", pymongo.ASCENDING),
                ("status", pymongo.ASCENDING),
                ("created_at", pymongo.DESCENDING),
            ]),
            pymongo.IndexModel(
                [("run_id", pymongo.ASCENDING), ("source_kind", pymongo.ASCENDING)]
            ),
        ])
        logger.info("成功初始化 Agent 运行历史与修订提案索引。")
    except Exception as exc:
        logger.error("初始化 Agent 工作台索引失败：%s", exc)


async def init_agent_runtime_indexes():
    """初始化有界 Agent Runtime 的授权、运行、步骤与事件索引。"""
    try:
        db = get_database()
        await _migrate_agent_runtime_readiness_ttl(
            db[collections.AGENT_RUNTIME_READINESS]
        )
        await db[collections.AGENT_RUNTIME_READINESS].create_indexes([
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("start_request_id", pymongo.ASCENDING),
                ],
                unique=True,
                partialFilterExpression={
                    "start_request_id": {"$type": "string"},
                    "is_deleted": False,
                },
                name="agent_runtime_readiness_start_unique",
            ),
            pymongo.IndexModel([
                ("owner_id", pymongo.ASCENDING),
                ("novel_id", pymongo.ASCENDING),
                ("created_at", pymongo.DESCENDING),
            ]),
            pymongo.IndexModel(
                [("ttl_expires_at", pymongo.ASCENDING)],
                expireAfterSeconds=0,
                name="agent_runtime_readiness_expiry_ttl",
            ),
        ])
        await db[collections.AGENT_RUNTIME_RUNS].create_indexes([
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("start_request_id", pymongo.ASCENDING),
                ],
                unique=True,
                name="agent_runtime_start_idempotent",
            ),
            pymongo.IndexModel([
                ("owner_id", pymongo.ASCENDING),
                ("novel_id", pymongo.ASCENDING),
                ("created_at", pymongo.DESCENDING),
            ]),
            pymongo.IndexModel([
                ("status", pymongo.ASCENDING),
                ("lease.expires_at", pymongo.ASCENDING),
            ]),
            pymongo.IndexModel(
                [("predecessor_run_id", pymongo.ASCENDING)],
                unique=True,
                partialFilterExpression={
                    "predecessor_run_id": {"$type": "objectId"},
                    "is_deleted": False,
                },
                name="agent_runtime_predecessor_unique",
            ),
            pymongo.IndexModel([
                ("owner_id", pymongo.ASCENDING),
                ("lineage_root_run_id", pymongo.ASCENDING),
                ("created_at", pymongo.ASCENDING),
            ]),
        ])
        await db[collections.AGENT_RUNTIME_STEPS].create_indexes([
            pymongo.IndexModel(
                [
                    ("run_id", pymongo.ASCENDING),
                    ("ordinal", pymongo.ASCENDING),
                ],
                unique=True,
                name="agent_runtime_step_ordinal_unique",
            ),
            pymongo.IndexModel([
                ("owner_id", pymongo.ASCENDING),
                ("novel_id", pymongo.ASCENDING),
                ("created_at", pymongo.ASCENDING),
            ]),
        ])
        await db[collections.AGENT_RUNTIME_EVENTS].create_indexes([
            pymongo.IndexModel(
                [
                    ("run_id", pymongo.ASCENDING),
                    ("sequence", pymongo.ASCENDING),
                ],
                unique=True,
                name="agent_runtime_event_sequence_unique",
            ),
            pymongo.IndexModel(
                [
                    ("run_id", pymongo.ASCENDING),
                    ("event_key", pymongo.ASCENDING),
                ],
                unique=True,
                name="agent_runtime_event_key_unique",
            ),
        ])
        logger.info("成功初始化有界 Agent Runtime 索引。")
    except Exception as exc:
        logger.error("初始化有界 Agent Runtime 索引失败：%s", exc)
        raise


async def init_volume_indexes():
    """初始化volumes集合的索引。"""
    try:
        db = get_database()
        volumes_collection = db[collections.VOLUMES]

        logger.info("正在初始化'volumes'集合的索引...")

        await _drop_legacy_unique_index(
            volumes_collection,
            {"novel_id": 1, "order_index": 1},
            "volumes_active_novel_order_unique",
        )

        indexes = [
            # 单字段索引：按小说过滤拉取全书卷列表
            pymongo.IndexModel([("novel_id", pymongo.ASCENDING)]),

            # 只约束未软删除卷，允许回收站里保留历史序号。
            pymongo.IndexModel(
                [("novel_id", pymongo.ASCENDING), ("order_index", pymongo.ASCENDING)],
                unique=True,
                partialFilterExpression={"is_deleted": False},
                name="volumes_active_novel_order_unique",
            ),

            # 读优化组合索引：未删除卷按序号排列的常用查询
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("is_deleted", pymongo.ASCENDING),
                ("order_index", pymongo.ASCENDING)
            ]),

            # 按最近更新时间检索
            pymongo.IndexModel([("updated_at", pymongo.DESCENDING)]),
        ]

        await volumes_collection.create_indexes(indexes)
        logger.info("成功初始化'volumes'集合的索引。")
    except Exception as e:
        logger.error(f"初始化volume索引失败：{e}")


async def init_chapter_indexes():
    """初始化 chapters 集合的顺序、列表和更新时间索引。"""
    try:
        db = get_database()
        chapters_collection = db[collections.CHAPTERS]

        await _drop_legacy_unique_index(
            chapters_collection,
            {"volume_id": 1, "order_index": 1},
            "chapters_active_volume_order_unique",
        )

        indexes = [
            pymongo.IndexModel([("novel_id", pymongo.ASCENDING)]),
            pymongo.IndexModel([("volume_id", pymongo.ASCENDING)]),
            pymongo.IndexModel(
                [("volume_id", pymongo.ASCENDING), ("order_index", pymongo.ASCENDING)],
                unique=True,
                partialFilterExpression={"is_deleted": False},
                name="chapters_active_volume_order_unique",
            ),
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("is_deleted", pymongo.ASCENDING),
                ("volume_id", pymongo.ASCENDING),
                ("order_index", pymongo.ASCENDING),
            ]),
            pymongo.IndexModel([("updated_at", pymongo.DESCENDING)]),
        ]
        await chapters_collection.create_indexes(indexes)
        logger.info("成功初始化'chapters'集合的索引。")
    except Exception as exc:
        logger.error("初始化 chapter 索引失败：%s", exc)


async def init_faction_indexes():
    """初始化factions集合的索引。"""
    try:
        db = get_database()
        factions_collection = db[collections.FACTIONS]

        logger.info("正在初始化'factions'集合的索引...")

        await _drop_legacy_unique_index(
            factions_collection,
            {"novel_id": 1, "faction_id": 1},
            "factions_active_novel_faction_unique",
        )

        indexes = [
            # 只约束未软删除阵营，允许回收站保留历史业务ID。
            pymongo.IndexModel(
                [("novel_id", pymongo.ASCENDING), ("faction_id", pymongo.ASCENDING)],
                unique=True,
                partialFilterExpression={"is_deleted": False},
                name="factions_active_novel_faction_unique",
            ),

            # 按层级类型过滤（用于按 core / major_volume 等召回）
            pymongo.IndexModel(
                [("novel_id", pymongo.ASCENDING), ("level_type", pymongo.ASCENDING)],
            ),

            # 按父级阵营查子阵营
            pymongo.IndexModel(
                [("novel_id", pymongo.ASCENDING), ("parent_faction_id", pymongo.ASCENDING)],
            ),

            # 按名称检索阵营
            pymongo.IndexModel(
                [("novel_id", pymongo.ASCENDING), ("name", pymongo.ASCENDING)],
            ),

            # 读优化：未删除阵营按排序权重排列
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("is_deleted", pymongo.ASCENDING),
                ("sort_order", pymongo.ASCENDING)
            ]),

            # 按最近更新时间检索
            pymongo.IndexModel([("updated_at", pymongo.DESCENDING)]),
        ]

        await factions_collection.create_indexes(indexes)
        logger.info("成功初始化'factions'集合的索引。")
    except Exception as e:
        logger.error(f"初始化faction索引失败：{e}")


async def init_reference_card_indexes():
    """Initialize list/search indexes for character and world-building cards."""
    try:
        db = get_database()
        for collection_name in (collections.CHARACTERS, collections.WORLDBOOK):
            collection = db[collection_name]
            await collection.create_indexes([
                pymongo.IndexModel([
                    ("novel_id", pymongo.ASCENDING),
                    ("card_type", pymongo.ASCENDING),
                    ("is_deleted", pymongo.ASCENDING),
                    ("sort_order", pymongo.ASCENDING),
                ]),
                pymongo.IndexModel([
                    ("novel_id", pymongo.ASCENDING),
                    ("card_type", pymongo.ASCENDING),
                    ("name", pymongo.ASCENDING),
                ]),
                pymongo.IndexModel([("tags", pymongo.ASCENDING)]),
                pymongo.IndexModel(
                    [
                        ("novel_id", pymongo.ASCENDING),
                        ("interop.source.source_hash", pymongo.ASCENDING),
                    ],
                    name=f"{collection_name}_novel_interop_source_hash",
                ),
                pymongo.IndexModel([("updated_at", pymongo.DESCENDING)]),
            ])
        logger.info("成功初始化人物与世界资料卡索引。")
    except Exception as exc:
        logger.error("初始化 reference card 索引失败：%s", exc)


async def init_reference_card_proposal_indexes():
    """Initialize persisted proposal lookup and retention indexes."""
    try:
        collection = get_database()[collections.REFERENCE_CARD_PROPOSALS]
        await collection.create_indexes([
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("status", pymongo.ASCENDING),
                ("updated_at", pymongo.DESCENDING),
            ]),
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("source_digest", pymongo.ASCENDING),
                ("card_set_digest", pymongo.ASCENDING),
            ]),
            pymongo.IndexModel(
                [("novel_id", pymongo.ASCENDING)],
                unique=True,
                partialFilterExpression={"status": "generating"},
                name="reference_card_proposals_single_generation_lease",
            ),
            pymongo.IndexModel(
                [("purge_after", pymongo.ASCENDING)],
                expireAfterSeconds=0,
                name="reference_card_proposals_retention_ttl",
            ),
        ])
        logger.info("Initialized reference-card proposal indexes.")
    except Exception as exc:
        logger.error("Failed to initialize reference-card proposal indexes: %s", exc)


async def init_emergent_reference_card_candidate_indexes():
    """Initialize queue, blocker, and exact-name suggestion indexes."""
    try:
        collection = get_database()[
            collections.EMERGENT_REFERENCE_CARD_CANDIDATES
        ]
        await collection.create_indexes([
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("status", pymongo.ASCENDING),
                ("updated_at", pymongo.DESCENDING),
            ]),
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("requires_review_before_next_chapter", pymongo.ASCENDING),
                ("status", pymongo.ASCENDING),
            ]),
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("card_type", pymongo.ASCENDING),
                ("normalized_name", pymongo.ASCENDING),
                ("status", pymongo.ASCENDING),
            ]),
            pymongo.IndexModel([
                ("chapter_id", pymongo.ASCENDING),
                ("source_mutation_id", pymongo.ASCENDING),
            ]),
            pymongo.IndexModel(
                [
                    ("novel_id", pymongo.ASCENDING),
                    ("auto_creation.counted", pymongo.ASCENDING),
                    ("chapter_id", pymongo.ASCENDING),
                ],
                name="emergent_reference_cards_auto_creation_count",
            ),
        ])
        logger.info("Initialized emergent reference-card candidate indexes.")
    except Exception as exc:
        logger.error(
            "Failed to initialize emergent reference-card candidate indexes: %s",
            exc,
        )


async def init_card_import_proposal_indexes():
    """Initialize owner-scoped import lookup, duplicate, and retention indexes."""
    try:
        collection = get_database()[collections.CARD_IMPORT_PROPOSALS]
        await collection.create_indexes([
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("status", pymongo.ASCENDING),
                    ("updated_at", pymongo.DESCENDING),
                ],
                name="card_import_proposals_owner_novel_status",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("source_hash", pymongo.ASCENDING),
                    ("imported_at", pymongo.DESCENDING),
                ],
                name="card_import_proposals_owner_source_hash",
            ),
            pymongo.IndexModel(
                [("purge_after", pymongo.ASCENDING)],
                expireAfterSeconds=0,
                name="card_import_proposals_retention_ttl",
            ),
        ])
        logger.info("Initialized card-import proposal indexes.")
    except Exception as exc:
        logger.error("Failed to initialize card-import proposal indexes: %s", exc)


async def init_faction_relation_indexes():
    """初始化faction_relations集合的索引。"""
    try:
        db = get_database()
        relations_collection = db[collections.FACTION_RELATIONS]

        logger.info("正在初始化'faction_relations'集合的索引...")

        indexes = [
            # 只约束未软删除关系，方便未来保留历史关系记录。
            pymongo.IndexModel(
                [("novel_id", pymongo.ASCENDING), ("relation_id", pymongo.ASCENDING)],
                unique=True,
                partialFilterExpression={"is_deleted": False},
                name="faction_relations_active_novel_relation_unique",
            ),

            # 按来源阵营召回关系
            pymongo.IndexModel(
                [("novel_id", pymongo.ASCENDING), ("source_faction_id", pymongo.ASCENDING)],
            ),

            # 按目标阵营召回关系
            pymongo.IndexModel(
                [("novel_id", pymongo.ASCENDING), ("target_faction_id", pymongo.ASCENDING)],
            ),

            # 常用列表读取：只读未删除、启用关系，并按强度排序
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("is_deleted", pymongo.ASCENDING),
                ("is_active", pymongo.ASCENDING),
                ("intensity", pymongo.DESCENDING),
            ]),

            # 按最近更新时间检索
            pymongo.IndexModel([("updated_at", pymongo.DESCENDING)]),
        ]

        await relations_collection.create_indexes(indexes)
        logger.info("成功初始化'faction_relations'集合的索引。")
    except Exception as e:
        logger.error(f"初始化faction_relations索引失败：{e}")


async def init_plot_thread_indexes():
    """初始化 plot_threads 集合的索引。"""
    try:
        db = get_database()
        threads_collection = db[collections.PLOT_THREADS]

        indexes = [
            # 按状态召回活跃伏笔，装配器每章都要查。
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("status", pymongo.ASCENDING),
            ]),
            # 按期望回收章序范围查询。截断优先级已改在 Python 端排序
            # （见 plot_thread_repository.list_threads 的说明），此索引不再
            # 驱动截断；保留是为阶段 2 的欠账提醒（按 due_chapter_order 找
            # 逾期未回收的伏笔）服务。
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("due_chapter_order", pymongo.ASCENDING),
            ]),
            pymongo.IndexModel([("updated_at", pymongo.DESCENDING)]),
        ]
        await threads_collection.create_indexes(indexes)
        logger.info("成功初始化'plot_threads'集合的索引。")
    except Exception as exc:
        logger.error("初始化 plot_threads 索引失败：%s", exc)


async def init_character_state_indexes():
    """初始化 character_states 集合的索引。"""
    try:
        db = get_database()
        states_collection = db[collections.CHARACTER_STATES]

        indexes = [
            # 每角色一条状态：两条必然互相矛盾，故在库层面就禁掉。
            pymongo.IndexModel(
                [("novel_id", pymongo.ASCENDING), ("card_id", pymongo.ASCENDING)],
                unique=True,
                name="character_states_novel_card_unique",
            ),
            pymongo.IndexModel([("updated_at", pymongo.DESCENDING)]),
        ]
        await states_collection.create_indexes(indexes)
        logger.info("成功初始化'character_states'集合的索引。")
    except Exception as exc:
        logger.error("初始化 character_states 索引失败：%s", exc)


async def init_generation_job_indexes():
    """初始化 generation_jobs 集合的索引。"""
    try:
        db = get_database()
        jobs_collection = db[collections.GENERATION_JOBS]
        indexes = [
            # 活跃作业查找 + 列表（全局单作业守卫按 status 查在跑作业）
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("status", pymongo.ASCENDING),
            ]),
            # 历史列表按创建时间
            pymongo.IndexModel([
                ("novel_id", pymongo.ASCENDING),
                ("created_at", pymongo.DESCENDING),
            ]),
            pymongo.IndexModel([("updated_at", pymongo.DESCENDING)]),
            pymongo.IndexModel(
                [
                    ("novel_id", pymongo.ASCENDING),
                    ("is_deleted", pymongo.ASCENDING),
                    ("required_book_successor_parent_job_id", pymongo.ASCENDING),
                    ("created_at", pymongo.DESCENDING),
                    ("_id", pymongo.DESCENDING),
                ],
                name="generation_root_history_cursor",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("job_kind", pymongo.ASCENDING),
                    ("interactive_source_key", pymongo.ASCENDING),
                    ("authorization_revision", pymongo.DESCENDING),
                    ("created_at", pymongo.DESCENDING),
                ],
                partialFilterExpression={
                    "job_kind": "interactive_chapter_completion",
                    "is_deleted": False,
                },
                name="interactive_completion_source_history",
            ),
            pymongo.IndexModel(
                [("active_slot", pymongo.ASCENDING)],
                unique=True,
                partialFilterExpression={"active_slot": "global", "is_deleted": False},
                name="single_active_generation_job",
            ),
            pymongo.IndexModel(
                [
                    (
                        "required_book_successor_action.action_digest",
                        pymongo.ASCENDING,
                    )
                ],
                unique=True,
                partialFilterExpression={
                    "required_book_successor_action.action_digest": {
                        "$type": "string"
                    },
                    "is_deleted": False,
                },
                name="required_book_successor_child_action",
            ),
        ]
        await jobs_collection.create_indexes(indexes)
        logger.info("成功初始化'generation_jobs'集合的索引。")
    except Exception as exc:
        logger.error("初始化 generation_jobs 索引失败：%s", exc)


async def init_prose_run_indexes():
    """Initialize resumable prose-run ownership, lookup, and retention indexes."""
    try:
        collection = get_database()[collections.PROSE_RUNS]
        await collection.create_indexes([
            pymongo.IndexModel([
                ("owner_id", 1),
                ("chapter_id", 1),
                ("status", 1),
                ("updated_at", -1),
            ]),
            pymongo.IndexModel([("novel_id", 1), ("updated_at", -1)]),
            pymongo.IndexModel([
                ("owner_id", 1),
                ("novel_id", 1),
                ("generation_job_id", 1),
                ("updated_at", -1),
            ]),
            pymongo.IndexModel([("lease.expires_at", 1)]),
            # Status is included in the partial filter so legacy runs created
            # before this index are protected without a schema backfill.
            pymongo.IndexModel(
                [("owner_id", 1), ("chapter_id", 1)],
                unique=True,
                partialFilterExpression={
                    "status": {
                        "$in": ["active", "incomplete", "complete"],
                    },
                    "is_deleted": False,
                },
                name="prose_runs_single_current",
            ),
        ])
        logger.info("Initialized prose_runs indexes.")
    except Exception as exc:
        logger.error("Failed to initialize prose_runs indexes: %s", exc)
        # Run replacement relies on this unique constraint to close the
        # CAS-to-insert race. Continuing without it can duplicate paid work.
        raise


async def init_prose_remediation_receipt_indexes():
    """Initialize the durable idempotency ledger for paid prose rewrites."""
    try:
        collection = get_database()[collections.PROSE_REMEDIATION_RECEIPTS]
        await collection.create_indexes([
            pymongo.IndexModel(
                [
                    ("owner_id", 1),
                    ("novel_id", 1),
                    ("prose_run_id", 1),
                    ("idempotency_key", 1),
                ],
                unique=True,
                name="prose_remediation_receipt_identity",
            ),
            pymongo.IndexModel([
                ("prose_run_id", 1),
                ("state", 1),
                ("updated_at", -1),
            ]),
            pymongo.IndexModel([("claim_expires_at", 1)]),
        ])
        logger.info("Initialized prose_remediation_receipts indexes.")
    except Exception as exc:
        logger.error(
            "Failed to initialize prose_remediation_receipts indexes: %s",
            exc,
        )
        # Paid idempotency depends on the exact unique identity. Starting
        # without it can duplicate Provider calls, so this index is fail-closed.
        raise


async def init_state_candidate_repair_receipt_indexes():
    """Initialize the durable idempotency ledger for state repair calls."""
    try:
        collection = get_database()[
            collections.STATE_CANDIDATE_REPAIR_RECEIPTS
        ]
        await collection.create_indexes([
            pymongo.IndexModel(
                [
                    ("owner_id", 1),
                    ("novel_id", 1),
                    ("chapter_id", 1),
                    ("execution_id", 1),
                    ("cycle", 1),
                ],
                unique=True,
                name="state_candidate_repair_receipt_identity",
            ),
            pymongo.IndexModel([
                ("novel_id", 1),
                ("state", 1),
                ("updated_at", -1),
            ]),
            pymongo.IndexModel([("claim_expires_at", 1)]),
        ])
        logger.info("Initialized state_candidate_repair_receipts indexes.")
    except Exception as exc:
        logger.error(
            "Failed to initialize state_candidate_repair_receipts indexes: %s",
            exc,
        )
        raise


async def init_reference_card_repair_receipt_indexes():
    """Initialize the durable idempotency ledger for dependency repairs."""
    try:
        collection = get_database()[
            collections.REFERENCE_CARD_REPAIR_RECEIPTS
        ]
        await collection.create_indexes([
            pymongo.IndexModel(
                [
                    ("owner_id", 1),
                    ("novel_id", 1),
                    ("job_id", 1),
                    ("chapter_id", 1),
                    ("cycle", 1),
                    ("authorization_digest", 1),
                ],
                unique=True,
                name="reference_card_repair_receipt_identity",
            ),
            pymongo.IndexModel([
                ("novel_id", 1),
                ("state", 1),
                ("updated_at", -1),
            ]),
            pymongo.IndexModel([("claim_expires_at", 1)]),
        ])
        logger.info("Initialized reference_card_repair_receipts indexes.")
    except Exception as exc:
        logger.error(
            "Failed to initialize reference_card_repair_receipts indexes: %s",
            exc,
        )
        raise


async def init_image_asset_indexes():
    """Initialize owner isolation, idempotency, and listing indexes."""
    try:
        collection = get_database()[collections.IMAGE_ASSETS]
        await collection.create_indexes([
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("created_at", pymongo.DESCENDING),
                ],
                name="image_assets_owner_novel_created",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("relative_path", pymongo.ASCENDING),
                ],
                name="image_assets_owner_relative_path",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("content_hash", pymongo.ASCENDING),
                ],
                name="image_assets_owner_novel_content",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("subject_kind", pymongo.ASCENDING),
                    ("subject_id", pymongo.ASCENDING),
                    ("created_at", pymongo.DESCENDING),
                ],
                name="image_assets_owner_novel_subject",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("illustration_brief_id", pymongo.ASCENDING),
                    ("illustration_run_id", pymongo.ASCENDING),
                    ("pipeline_stage", pymongo.ASCENDING),
                    ("candidate_state", pymongo.ASCENDING),
                    ("created_at", pymongo.DESCENDING),
                ],
                partialFilterExpression={
                    "illustration_brief_id": {"$type": "objectId"},
                    "illustration_run_id": {"$type": "objectId"},
                    "is_deleted": False,
                },
                name="image_assets_owner_brief_candidates",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("metadata_fingerprint", pymongo.ASCENDING),
                ],
                unique=True,
                partialFilterExpression={"is_deleted": False},
                name="image_assets_active_metadata_idempotent",
            ),
        ])
        logger.info("Initialized image_assets indexes.")
    except Exception as exc:
        logger.error("Failed to initialize image_assets indexes: %s", exc)
        # Idempotent metadata registration relies on this constraint to close
        # concurrent upsert races. Continuing without it can duplicate records.
        raise


async def init_character_visual_profile_indexes():
    """Initialize the visual-profile identity and card-cascade indexes."""

    try:
        collection = get_database()[collections.CHARACTER_VISUAL_PROFILES]
        await collection.create_indexes([
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("character_card_id", pymongo.ASCENDING),
                ],
                unique=True,
                name="character_visual_profiles_owner_novel_card",
            ),
            pymongo.IndexModel(
                [
                    ("novel_id", pymongo.ASCENDING),
                    ("character_card_id", pymongo.ASCENDING),
                ],
                name="character_visual_profiles_novel_card",
            ),
        ])
        logger.info("Initialized character_visual_profiles indexes.")
    except Exception as exc:
        logger.error(
            "Failed to initialize character_visual_profiles indexes: %s",
            exc,
        )
        # Concurrent first writes rely on this unique constraint. Starting
        # without it would allow two active profiles for one formal card.
        raise


async def init_illustration_brief_indexes():
    """Initialize chapter illustration brief scope and ordering indexes."""

    try:
        collection = get_database()[collections.ILLUSTRATION_BRIEFS]
        await collection.create_indexes([
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("chapter_id", pymongo.ASCENDING),
                    ("status", pymongo.ASCENDING),
                    ("sort_order", pymongo.ASCENDING),
                    ("_id", pymongo.ASCENDING),
                ],
                name="illustration_briefs_owner_novel_chapter_status_sort",
            ),
            pymongo.IndexModel(
                [
                    ("novel_id", pymongo.ASCENDING),
                    ("chapter_id", pymongo.ASCENDING),
                ],
                name="illustration_briefs_novel_chapter",
            ),
        ])
        logger.info("Initialized illustration_briefs indexes.")
    except Exception as exc:
        logger.error("Failed to initialize illustration_briefs indexes: %s", exc)
        raise


async def init_illustration_run_indexes():
    """Initialize staged-run scope, active uniqueness, and lineage indexes."""

    try:
        collection = get_database()[collections.ILLUSTRATION_RUNS]
        await collection.create_indexes([
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("illustration_brief_id", pymongo.ASCENDING),
                ],
                unique=True,
                partialFilterExpression={
                    "status": "active",
                    "is_deleted": False,
                },
                name="illustration_runs_owner_novel_brief_active",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("chapter_id", pymongo.ASCENDING),
                    ("illustration_brief_id", pymongo.ASCENDING),
                    ("created_at", pymongo.DESCENDING),
                ],
                name="illustration_runs_owner_novel_chapter_brief_created",
            ),
            pymongo.IndexModel(
                [
                    ("novel_id", pymongo.ASCENDING),
                    ("parent_run_id", pymongo.ASCENDING),
                ],
                partialFilterExpression={
                    "parent_run_id": {"$type": "objectId"}
                },
                name="illustration_runs_novel_parent",
            ),
        ])
        logger.info("Initialized illustration_runs indexes.")
    except Exception as exc:
        logger.error("Failed to initialize illustration_runs indexes: %s", exc)
        # Concurrent first writes rely on the partial unique index. Starting
        # without it would allow two active runs for one formal brief.
        raise



async def init_image_job_indexes():
    """Initialize durable image-job ownership, resume, and idempotency indexes."""
    try:
        collection = get_database()[collections.IMAGE_JOBS]
        existing = await collection.index_information()
        desired_idempotency_filter = {
            "idempotency_key": {"$type": "string"},
            "is_terminal": False,
            "is_deleted": False,
        }
        desired_active_filter = {
            "usage": "character_portrait",
            "is_terminal": False,
            "is_deleted": False,
        }
        desired_subject_active_filter = {
            "subject_id": {"$type": "objectId"},
            "is_terminal": False,
            "is_deleted": False,
        }
        desired_subject_cleanup_filter = {
            "subject_id": {"$type": "objectId"},
            "cleanup_pending": True,
            "is_deleted": False,
        }
        active_index = existing.get("image_jobs_owner_character_active")
        desired_active_key = [
            ("owner_id", pymongo.ASCENDING),
            ("novel_id", pymongo.ASCENDING),
            ("character_card_id", pymongo.ASCENDING),
            ("usage", pymongo.ASCENDING),
        ]
        if active_index is not None and (
            active_index.get("key") != desired_active_key
            or active_index.get("unique") is not True
            or active_index.get("partialFilterExpression")
            != desired_active_filter
        ):
            await collection.drop_index("image_jobs_owner_character_active")
        idempotency_index = existing.get("image_jobs_owner_idempotency")
        if (
            idempotency_index is not None
            and idempotency_index.get("partialFilterExpression")
            != desired_idempotency_filter
        ):
            await collection.drop_index("image_jobs_owner_idempotency")
        provider_prompt_index = existing.get("image_jobs_provider_prompt")
        desired_provider_prompt_key = [
            ("provider_alias", pymongo.ASCENDING),
            ("handle.prompt_id", pymongo.ASCENDING),
        ]
        if (
            provider_prompt_index is not None
            and provider_prompt_index.get("key") != desired_provider_prompt_key
        ):
            await collection.drop_index("image_jobs_provider_prompt")
        await collection.create_indexes([
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("created_at", pymongo.DESCENDING),
                ],
                name="image_jobs_owner_novel_created",
            ),
            pymongo.IndexModel(
                desired_active_key,
                unique=True,
                partialFilterExpression=desired_active_filter,
                name="image_jobs_owner_character_active",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("usage", pymongo.ASCENDING),
                    ("subject_id", pymongo.ASCENDING),
                ],
                unique=True,
                partialFilterExpression=desired_subject_active_filter,
                name="image_jobs_owner_subject_active",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("idempotency_key", pymongo.ASCENDING),
                ],
                unique=True,
                partialFilterExpression=desired_idempotency_filter,
                name="image_jobs_owner_idempotency",
            ),
            pymongo.IndexModel(
                desired_provider_prompt_key,
                unique=True,
                partialFilterExpression={
                    "handle.prompt_id": {"$type": "string"},
                    "is_deleted": False,
                },
                name="image_jobs_provider_prompt",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("character_card_id", pymongo.ASCENDING),
                    ("created_at", pymongo.DESCENDING),
                ],
                partialFilterExpression={
                    "cleanup_pending": True,
                    "is_deleted": False,
                },
                name="image_jobs_owner_character_cleanup",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("usage", pymongo.ASCENDING),
                    ("subject_id", pymongo.ASCENDING),
                    ("created_at", pymongo.DESCENDING),
                ],
                partialFilterExpression=desired_subject_cleanup_filter,
                name="image_jobs_owner_subject_cleanup",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("usage", pymongo.ASCENDING),
                    ("appearance_anchor_card_ids", pymongo.ASCENDING),
                    ("created_at", pymongo.DESCENDING),
                ],
                partialFilterExpression={
                    "usage": "scene_illustration",
                    "is_deleted": False,
                },
                name="image_jobs_owner_anchor_dependencies",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("illustration_run_id", pymongo.ASCENDING),
                    ("pipeline_stage", pymongo.ASCENDING),
                    ("created_at", pymongo.DESCENDING),
                ],
                partialFilterExpression={
                    "illustration_run_id": {"$type": "objectId"},
                    "is_deleted": False,
                },
                name="image_jobs_owner_run_stage_created",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("portrait_batch_id", pymongo.ASCENDING),
                    ("subject_id", pymongo.ASCENDING),
                    ("created_at", pymongo.DESCENDING),
                ],
                partialFilterExpression={
                    "portrait_batch_id": {"$type": "objectId"},
                    "is_deleted": False,
                },
                name="image_jobs_owner_portrait_batch_card_created",
            ),
            pymongo.IndexModel(
                [
                    ("is_terminal", pymongo.ASCENDING),
                    ("status", pymongo.ASCENDING),
                    ("updated_at", pymongo.ASCENDING),
                ],
                name="image_jobs_status_updated",
            ),
        ])
        logger.info("Initialized image_jobs indexes.")
    except Exception as exc:
        logger.error("Failed to initialize image_jobs indexes: %s", exc)
        # Idempotent submit/resume depends on the two unique indexes above.
        raise


async def init_image_batch_indexes():
    """Initialize owner isolation and the one-live-batch invariant."""
    try:
        collection = get_database()[collections.IMAGE_BATCHES]
        await collection.create_indexes([
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("created_at", pymongo.DESCENDING),
                ],
                name="image_batches_owner_novel_created",
            ),
            pymongo.IndexModel(
                [
                    ("owner_id", pymongo.ASCENDING),
                    ("novel_id", pymongo.ASCENDING),
                    ("kind", pymongo.ASCENDING),
                ],
                unique=True,
                partialFilterExpression={
                    "is_terminal": False,
                    "is_deleted": False,
                },
                name="image_batches_owner_novel_active",
            ),
            pymongo.IndexModel(
                [
                    ("is_terminal", pymongo.ASCENDING),
                    ("status", pymongo.ASCENDING),
                    ("updated_at", pymongo.ASCENDING),
                ],
                name="image_batches_status_updated",
            ),
        ])
        logger.info("Initialized image_batches indexes.")
    except Exception as exc:
        logger.error("Failed to initialize image_batches indexes: %s", exc)
        # Without the partial unique index two tabs could each launch a batch.
        raise


async def init_state_timeline_indexes():
    """初始化可回放状态时间线、预览与 standalone journal 索引。"""
    try:
        db = get_database()
        await db[collections.CHAPTER_STATE_DELTAS].create_indexes([
            pymongo.IndexModel(
                [("novel_id", 1), ("chapter_id", 1)],
                unique=True,
                name="chapter_state_delta_unique",
            ),
            pymongo.IndexModel([("novel_id", 1), ("stale", 1)]),
        ])
        await db[collections.CHARACTER_STATE_SNAPSHOTS].create_indexes([
            pymongo.IndexModel(
                [("novel_id", 1), ("chapter_id", 1), ("card_id", 1)],
                unique=True,
                name="character_snapshot_unique",
            ),
            pymongo.IndexModel([("novel_id", 1), ("card_id", 1), ("stale", 1)]),
        ])
        await db[collections.PLOT_THREAD_EVENTS].create_indexes([
            pymongo.IndexModel(
                [("novel_id", 1), ("idempotency_key", 1)],
                unique=True,
                name="plot_thread_event_idempotent",
            ),
            pymongo.IndexModel([("novel_id", 1), ("chapter_id", 1)]),
        ])
        await db[collections.MANUAL_CORRECTIONS].create_indexes([
            pymongo.IndexModel(
                [("novel_id", 1), ("idempotency_key", 1)],
                unique=True,
                name="manual_correction_idempotent",
            ),
        ])
        await db[collections.STATE_PREVIEWS].create_indexes([
            pymongo.IndexModel([("expires_at", 1)], expireAfterSeconds=0),
            pymongo.IndexModel([("novel_id", 1), ("chapter_id", 1)]),
            pymongo.IndexModel([("status", 1), ("updated_at", 1)]),
            pymongo.IndexModel(
                [("job_mutation_key", 1)],
                unique=True,
                partialFilterExpression={
                    "job_mutation_key": {"$type": "string"},
                    "is_deleted": False,
                },
                name="state_preview_job_mutation_unique",
            ),
        ])
        await db[collections.MUTATION_JOURNALS].create_indexes([
            pymongo.IndexModel(
                [("novel_id", 1), ("idempotency_key", 1)],
                unique=True,
                name="mutation_journal_idempotent",
            ),
            pymongo.IndexModel([("status", 1), ("updated_at", 1)]),
        ])
        logger.info("成功初始化状态时间线与 mutation journal 索引。")
    except Exception as exc:
        logger.error("初始化状态时间线索引失败：%s", exc)


async def init_blueprint_run_indexes():
    await get_database()[collections.BLUEPRINT_RUNS].create_indexes([
        pymongo.IndexModel(
            [("owner_id", 1), ("draft_id", 1), ("is_deleted", 1), ("created_at", -1), ("_id", -1)],
            name="blueprint_owner_draft_history",
        ),
        pymongo.IndexModel(
            [("owner_id", 1), ("is_deleted", 1), ("created_at", -1), ("_id", -1)],
            name="blueprint_owner_history",
        ),
    ])


async def init_all_indexes():
    """初始化所有数据库索引。"""
    await init_identity_indexes()
    await init_agent_definition_indexes()
    await init_agent_workbench_indexes()
    await init_agent_runtime_indexes()
    await init_novel_indexes()
    await init_volume_indexes()
    await init_chapter_indexes()
    await init_reference_card_indexes()
    await init_reference_card_proposal_indexes()
    await init_emergent_reference_card_candidate_indexes()
    await init_card_import_proposal_indexes()
    await init_faction_indexes()
    await init_faction_relation_indexes()
    await init_plot_thread_indexes()
    await init_character_state_indexes()
    await init_generation_job_indexes()
    await init_prose_run_indexes()
    await get_database()[collections.JUDGE_REVIEW_RECORDS].create_indexes([
        pymongo.IndexModel([("owner_id", 1), ("chapter_id", 1), ("is_deleted", 1), ("_id", -1)], name="judge_chapter_history"),
        pymongo.IndexModel([("novel_id", 1)], name="judge_novel_delete"),
        pymongo.IndexModel([("job_id", 1)], name="judge_job_history"),
    ])
    await init_blueprint_run_indexes()
    await init_prose_remediation_receipt_indexes()
    await init_state_candidate_repair_receipt_indexes()
    await init_reference_card_repair_receipt_indexes()
    await init_image_asset_indexes()
    await init_character_visual_profile_indexes()
    await init_illustration_brief_indexes()
    await init_illustration_run_indexes()
    await init_image_job_indexes()
    await init_image_batch_indexes()
    await init_state_timeline_indexes()
    # 在这里添加其他集合的索引初始化
