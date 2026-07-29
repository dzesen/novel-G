"""脱敏、带 revision 的配置生命周期 API。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from backend.api.default_routers.auth_router import require_admin_request

from backend.config import config as config_module
from backend.config.capability_cache import FileCapabilityCacheStore
from backend.config.lifecycle import (
    ConfigChangePreview,
    ConfigConflictError,
    ConfigLifecycle,
    ConfigPatch,
    ConfigView,
    FileSecretVersionStore,
    YamlConfigStore,
)
from backend.config.workflow_catalog import WorkflowDefinition, get_workflow_catalog
from backend.db.mongo import connect_to_mongo
from backend.services.llm.provider_test_service import (
    ProviderTestRequest,
    ProviderTestResponse,
    resolve_provider_test_request,
    test_llm_provider_capabilities,
)
from backend.services.image.provider_test_service import (
    ImageProviderTestRequest,
    ImageProviderTestResponse,
    test_image_provider_connection,
)

router = APIRouter(
    prefix="/api/config",
    tags=["config"],
    dependencies=[Depends(require_admin_request)],
)
logger = logging.getLogger(__name__)

_lifecycle_cache: tuple[Path, ConfigLifecycle] | None = None
_capability_cache: tuple[Path, FileCapabilityCacheStore] | None = None


def _get_lifecycle() -> ConfigLifecycle:
    """按当前 CONFIG_PATH 构造生命周期，兼容测试和本地自定义路径。"""
    global _lifecycle_cache
    config_path = Path(config_module.CONFIG_PATH).resolve()
    if _lifecycle_cache is not None and _lifecycle_cache[0] == config_path:
        return _lifecycle_cache[1]

    secret_store = FileSecretVersionStore(
        config_path.with_name(".config-secret-versions.json")
    )
    lifecycle = ConfigLifecycle(
        store=YamlConfigStore(
            config_path,
            default_path=Path(config_module.DEFAULT_CONFIG_PATH),
        ),
        secret_store=secret_store,
        confirmation_key=secret_store.derive_key("config-delete-confirmation"),
    )
    _lifecycle_cache = (config_path, lifecycle)
    return lifecycle


def _get_capability_cache() -> FileCapabilityCacheStore:
    global _capability_cache
    config_path = Path(config_module.CONFIG_PATH).resolve()
    if _capability_cache is not None and _capability_cache[0] == config_path:
        return _capability_cache[1]
    secret_store = FileSecretVersionStore(
        config_path.with_name(".config-secret-versions.json")
    )
    store = FileCapabilityCacheStore(
        config_path.with_name(".provider-capabilities.json"),
        key=secret_store.derive_key("provider-capability-cache"),
    )
    _capability_cache = (config_path, store)
    return store


async def _apply_runtime_config() -> None:
    """刷新进程内配置并验证新的 MongoDB 连接。"""
    config_module.load_config(force_reload=True)
    await connect_to_mongo()


async def _restore_runtime_config() -> None:
    """磁盘回滚后恢复进程内配置和旧 MongoDB 连接。"""
    config_module.load_config(force_reload=True)
    await connect_to_mongo()


@router.get("", response_model=ConfigView)
async def get_configurations() -> ConfigView:
    """返回可编辑配置视图；API Key 永不出现在响应中。"""
    try:
        return _get_lifecycle().get_view()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/workflows", response_model=list[WorkflowDefinition])
async def get_workflows() -> tuple[WorkflowDefinition, ...]:
    """返回设置页使用的只读工作流目录。"""
    return get_workflow_catalog()


@router.post("/preview", response_model=ConfigChangePreview)
async def preview_configuration(request: ConfigPatch) -> ConfigChangePreview:
    """预览引用变化，并为破坏性操作签发短时确认令牌。"""
    try:
        return _get_lifecycle().preview(request)
    except ConfigConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("", response_model=ConfigView)
async def patch_configurations(request: ConfigPatch) -> ConfigView:
    """按 revision 原子应用补丁，并把运行时重载纳入同一回滚单元。"""
    try:
        return await _get_lifecycle().patch_and_apply(
            request,
            runtime_apply=_apply_runtime_config,
            runtime_restore=_restore_runtime_config,
        )
    except ConfigConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to apply configuration; previous state was restored")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("")
async def retired_full_config_update() -> None:
    """旧整对象写接口不安全，明确返回 Gone，防止静默清空密钥。"""
    raise HTTPException(
        status_code=410,
        detail="Full config PUT has been retired; reload ConfigView and use PATCH",
    )


@router.post("/llm-providers/test", response_model=ProviderTestResponse)
async def test_llm_provider(request: ProviderTestRequest) -> ProviderTestResponse:
    """测试当前表单中的 LLM Provider 接口能力，不写入配置文件。"""
    try:
        resolved = resolve_provider_test_request(
            request,
            _get_lifecycle().get_raw_config(),
        )
        response = await test_llm_provider_capabilities(resolved)
        if resolved.persist_capabilities:
            saved_provider = (
                _get_lifecycle().get_raw_config()
                .get("llm", {})
                .get("providers", {})
                .get(resolved.alias)
            )
            if isinstance(saved_provider, dict):
                _get_capability_cache().put(
                    resolved.alias,
                    saved_provider,
                    response.recommendations.model_dump(),
                )
        return response
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "/image-providers/test",
    response_model=ImageProviderTestResponse,
)
async def test_image_provider(
    request: ImageProviderTestRequest,
) -> ImageProviderTestResponse:
    """测试已保存的 ComfyUI 配置；本片不伪造 OpenAI-compatible 结果。"""

    try:
        return await test_image_provider_connection(
            request,
            _get_lifecycle().get_raw_config(),
            template_root=Path(config_module.CONFIG_PATH).resolve().parent,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Image Provider connectivity test failed")
        raise HTTPException(
            status_code=500,
            detail="图像 Provider 连通性测试失败",
        ) from exc
