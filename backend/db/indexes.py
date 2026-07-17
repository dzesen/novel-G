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


async def init_novel_indexes():
    """初始化novels集合的索引。"""
    try:
        db = get_database()
        novels_collection = db[collections.NOVELS]
        
        logger.info("正在初始化'novels'集合的索引...")
        
        indexes = [
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
        ]
        
        await novels_collection.create_indexes(indexes)
        logger.info("成功初始化'novels'集合的索引。")
    except Exception as e:
        logger.error(f"初始化novel索引失败：{e}")


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
                pymongo.IndexModel([("updated_at", pymongo.DESCENDING)]),
            ])
        logger.info("成功初始化人物与世界资料卡索引。")
    except Exception as exc:
        logger.error("初始化 reference card 索引失败：%s", exc)


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


async def init_all_indexes():
    """初始化所有数据库索引。"""
    await init_novel_indexes()
    await init_volume_indexes()
    await init_chapter_indexes()
    await init_reference_card_indexes()
    await init_faction_indexes()
    await init_faction_relation_indexes()
    await init_plot_thread_indexes()
    await init_character_state_indexes()
    # 在这里添加其他集合的索引初始化
