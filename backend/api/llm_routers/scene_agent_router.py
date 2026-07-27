"""Agent catalog and scene-level rewrite preview endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.api.default_routers.agent_router import get_agent_catalog
from backend.api.default_routers.auth_router import require_authenticated_request
from backend.api.llm_routers._common import (
    GenerationParamsMixin,
    build_gen_kwargs,
    build_runtime_kwargs,
)
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.utils import to_object_id
from backend.services.auth.identity_service import Actor
from backend.services.auth.novel_access_service import (
    NovelAccessService,
    get_novel_access_service,
)
from backend.services.llm.agent_catalog import AgentCatalog
from backend.services.llm.agent_orchestrator import (
    AgentOrchestrator,
    SceneRewriteResult,
)
from backend.services.llm.context_builder import (
    ContextBudgetError,
    assemble_context,
    fetch_context_inputs,
)
from backend.services.llm.generation_runtime import (
    PromptPlan,
    WorkflowStepTarget,
    create_generation_runtime,
)

router = APIRouter(
    prefix="/api/llm",
    tags=["llm-agents"],
    dependencies=[Depends(require_authenticated_request)],
)

SCENE_AGENT_WORKFLOW = "rewrite_chapter_scene_by_agent"
SCENE_AGENT_STEP = "scene_rewrite"


class SceneSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1200)
    purpose: str = Field(min_length=1, max_length=600)


class RewriteChapterSceneRequest(GenerationParamsMixin):
    model_config = ConfigDict(extra="forbid")

    novel_id: str = Field(min_length=1)
    chapter_id: str = Field(min_length=1)
    scene_index: int = Field(ge=0)
    base_scene: SceneSnapshot
    scene: SceneSnapshot
    agent_id: str = Field(default="scene_balanced", min_length=1)
    instruction: str = Field(default="", max_length=1200)


def _scene_prompt(
    *,
    context: str,
    chapter: dict[str, Any],
    scenes: list[dict[str, Any]],
    scene_index: int,
    instruction: str,
    json_only: bool,
) -> str:
    previous_scene = scenes[scene_index - 1] if scene_index > 0 else None
    next_scene = scenes[scene_index + 1] if scene_index + 1 < len(scenes) else None
    suffix = (
        '只输出 JSON：{"summary":"...","purpose":"..."}，不要输出其他内容。'
        if json_only
        else "严格按提供的 JSON Schema 输出。"
    )
    return f"""请只改写第 {scene_index + 1} 个场景，返回预览，不改写其他场景。

【章节】
第 {chapter.get("order_index", 0)} 章《{chapter.get("title", "")}》

【全局与本卷上下文】
{context}

【相邻场景】
前一场：{previous_scene or "无"}
当前场：{scenes[scene_index]}
后一场：{next_scene or "无"}

【用户补充要求】
{instruction or "无"}

约束：
- 保留当前场景在整章中的既定因果结果和 purpose；可以把 purpose 写得更准确，但不得删除其功能。
- 服从本章细纲、当前卷大纲、人物卡和永久事实。
- 不新增会改变后续场景前提的重大人物、设定或转折。
- summary 要能直接指导正文写作，purpose 要说明它对人物、冲突或全局结构的作用。

{suffix}""".strip()


@router.get("/scene-agents")
async def list_scene_agents(
    actor: Actor = Depends(require_authenticated_request),
    catalog: AgentCatalog = Depends(get_agent_catalog),
):
    profiles = await catalog.list_profiles(
        actor,
        capability="scene_rewrite",
    )
    return {
        "data": [profile.public_view() for profile in profiles]
    }


@router.post("/rewrite-chapter-scene")
async def rewrite_chapter_scene(
    req: RewriteChapterSceneRequest,
    actor: Actor = Depends(require_authenticated_request),
    access: NovelAccessService = Depends(get_novel_access_service),
    catalog: AgentCatalog = Depends(get_agent_catalog),
):
    try:
        await access.require_owned_novel(actor, req.novel_id)
        profile = await catalog.resolve_profile(
            actor,
            agent_id=req.agent_id,
            capability="scene_rewrite",
        )

        chapter = await chapter_repo.get_chapter_by_id(req.chapter_id)
        if chapter.get("novel_id") != to_object_id(req.novel_id):
            raise HTTPException(status_code=400, detail="该章节不属于指定小说")
        outline = chapter.get("outline") or {}
        scenes = list(outline.get("scenes") or [])
        if req.scene_index >= len(scenes):
            raise HTTPException(status_code=400, detail="场景序号超出当前细纲范围")

        current_scene = SceneSnapshot.model_validate(scenes[req.scene_index])
        if current_scene.model_dump() != req.base_scene.model_dump():
            raise HTTPException(
                status_code=409,
                detail="场景已被其他操作修改，请刷新细纲后重试",
            )

        inputs = await fetch_context_inputs(req.novel_id, req.chapter_id)
        context = assemble_context(inputs)
        prompt_scenes = [dict(scene) for scene in scenes]
        prompt_scenes[req.scene_index] = req.scene.model_dump()
        native_prompt = _scene_prompt(
            context=context.to_prompt_text(),
            chapter=chapter,
            scenes=prompt_scenes,
            scene_index=req.scene_index,
            instruction=req.instruction.strip(),
            json_only=False,
        )
        json_prompt = _scene_prompt(
            context=context.to_prompt_text(),
            chapter=chapter,
            scenes=prompt_scenes,
            scene_index=req.scene_index,
            instruction=req.instruction.strip(),
            json_only=True,
        )

        orchestrator = AgentOrchestrator(
            create_generation_runtime(**build_runtime_kwargs(req))
        )
        generated = await orchestrator.generate_structured(
            profile=profile,
            target=WorkflowStepTarget(SCENE_AGENT_WORKFLOW, SCENE_AGENT_STEP),
            schema=SceneRewriteResult,
            prompts=PromptPlan(
                native_schema_prompt=native_prompt,
                prompt_json_prompt=json_prompt,
            ),
            **build_gen_kwargs(req),
        )
        return {
            "scene": generated.value.model_dump(),
            "agent_id": req.agent_id,
            "provider_alias": generated.plan.provider_alias,
            "usage": generated.usage.model_dump(),
            "context_report": {
                "truncated_sections": context.truncated_sections,
                "dropped_item_counts": context.dropped_item_counts,
            },
        }
    except HTTPException:
        raise
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError, ContextBudgetError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
