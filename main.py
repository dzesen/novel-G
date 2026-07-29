import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.db.mongo import connect_to_mongo, close_mongo_connection
from backend.db.indexes import init_all_indexes
from backend.api.default_routers.agent_router import router as agent_router
from backend.api.default_routers.agent_workbench_router import (
    router as agent_workbench_router,
)
from backend.api.default_routers.config_router import router as config_router
from backend.api.default_routers.novel_router import router as novel_router
from backend.api.default_routers.upload_router import router as upload_router
from backend.api.default_routers.volume_router import router as volume_router
from backend.api.default_routers.chapter_router import router as chapter_router
from backend.api.default_routers.faction_router import router as faction_router
from backend.api.default_routers.faction_relation_router import router as faction_relation_router
from backend.api.llm_routers.create_novel_router import router as create_novel_router
from backend.api.llm_routers.outline_router import router as outline_router
from backend.api.llm_routers.prose_router import router as prose_router
from backend.api.llm_routers.state_router import router as state_router
from backend.api.llm_routers.agent_tool_router import router as agent_tool_router
from backend.api.llm_routers.scene_agent_router import router as scene_agent_router
from backend.llm.prompts.prompt_selector import load_prompt_config
from backend.runtime import (
    apply_runtime_flags_from_argv,
    build_uvicorn_log_config,
    get_backend_log_level,
    is_backend_debug_enabled,
)
from backend.network_mode import get_backend_bind_host, get_cors_origins
from backend.services.backup.backup_service import create_automatic_backup_if_due
from backend.services.novel.mutation_recovery import recover_pending_mutations
from backend.api.default_routers.backup_router import router as backup_router
from backend.api.default_routers.reference_card_router import router as reference_card_router
from backend.api.default_routers.plot_thread_router import router as plot_thread_router
from backend.api.default_routers.story_health_router import router as story_health_router
from backend.api.default_routers.character_state_router import router as character_state_router
from backend.api.default_routers.generation_job_router import (
    router as generation_job_router,
    mark_running_jobs_interrupted,
)
from backend.api.default_routers.state_timeline_router import router as state_timeline_router
from backend.api.default_routers.auth_router import router as auth_router
from backend.api.default_routers.card_import_router import router as card_import_router
from backend.api.default_routers.card_export_router import router as card_export_router
from backend.api.default_routers.image_job_router import router as image_job_router

apply_runtime_flags_from_argv()

logger = logging.getLogger(__name__)


# FastAPI setup with lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI应用的生命周期管理，在启动时完成配置校验与资源连接。

    Args:
        app: 当前 FastAPI 应用实例。

    Returns:
        异步生命周期上下文生成器。
    """
    logger.info(
        "Backend startup: debug=%s bind_host=%s",
        is_backend_debug_enabled(),
        get_backend_bind_host(),
    )
    # 启动早期读取一次提示词配置，让自定义 prompt.yaml 的错误能立刻出现在控制台日志中。
    load_prompt_config(force_reload=True)
    # Setup Mongo
    await connect_to_mongo()
    # Initialize DB Indexes
    await init_all_indexes()
    recovery = await recover_pending_mutations()
    if any(recovery.values()):
        logger.info(
            "Mutation recovery: recovered=%d failed=%d unsupported=%d "
            "deferred=%d quarantined=%d conflicts=%d",
            len(recovery["recovered"]),
            len(recovery["failed"]),
            len(recovery["unsupported"]),
            len(recovery["deferred"]),
            len(recovery["quarantined"]),
            len(recovery["conflicts"]),
        )
    await create_automatic_backup_if_due()
    interrupted = await mark_running_jobs_interrupted()
    if interrupted:
        logger.info("Marked %d running generation job(s) as interrupted after restart.", interrupted)
    logger.info("Backend startup completed.")
    yield
    # Teardown
    await close_mongo_connection()
    logger.info("Backend shutdown completed.")

app = FastAPI(title="Novel Generator API", lifespan=lifespan, debug=is_backend_debug_enabled())


@app.get("/api/health", tags=["system"])
async def health() -> dict[str, str]:
    """供本地启动器辨识服务身份；不返回配置或环境细节。"""
    return {"status": "ok", "service": "novel-g-backend"}

# CORS — 允许前端开发服务器跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
import os

os.makedirs("static/covers", exist_ok=True)
app.mount("/static/covers", StaticFiles(directory="static/covers"), name="static_covers")

app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(agent_workbench_router)
app.include_router(novel_router)
app.include_router(volume_router)
app.include_router(chapter_router)
app.include_router(faction_router)
app.include_router(faction_relation_router)
app.include_router(config_router)
app.include_router(create_novel_router)
app.include_router(outline_router)
app.include_router(prose_router)
app.include_router(state_router)
app.include_router(agent_tool_router)
app.include_router(scene_agent_router)
app.include_router(upload_router)
app.include_router(backup_router)
app.include_router(reference_card_router)
app.include_router(plot_thread_router)
app.include_router(story_health_router)
app.include_router(character_state_router)
app.include_router(generation_job_router)
app.include_router(state_timeline_router)
app.include_router(card_import_router)
app.include_router(card_export_router)
app.include_router(image_job_router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=get_backend_bind_host(),
        port=8000,
        reload=False,
        log_config=build_uvicorn_log_config(),
        log_level=get_backend_log_level().lower(),
        reload_excludes=[
            "frontend/**",
            "frontend/.next/**",
            "frontend/node_modules/**",
        ],
    )
