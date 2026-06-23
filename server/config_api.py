"""Configuration routes for OpenAI-compatible provider profiles."""

from __future__ import annotations

import ipaddress
import urllib.parse
from typing import Any

import fastapi
from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import ai_cache, ai_provider
from .agent import configure_agent as _configure_agent
from .agent import reset_active_model
from .auth import account_from_request, require_account_from_request


config_router = APIRouter(tags=["Config"])

_agent_config_kwargs: dict[str, Any] = {}


class ApiKeysUpdateRequest(BaseModel):
    provider_id: str | None = None
    provider_name: str | None = None
    openai_compat_api_key: str | None = None
    openai_compat_base_url: str | None = None
    chat_model: str | None = None
    embedding_model: str | None = None
    image_model: str | None = None
    campaign_model: str | None = None
    image_size: str | None = None
    token_policy_mode: str | None = None
    make_active: bool = True


class ProviderSwitchRequest(BaseModel):
    provider_id: str


class ProviderDeleteRequest(BaseModel):
    provider_id: str


class ModelListRequest(BaseModel):
    capability: str = "chat"
    search: str = ""
    provider_id: str | None = None
    provider_name: str | None = None
    openai_compat_api_key: str | None = None
    openai_compat_base_url: str | None = None
    chat_model: str | None = None
    embedding_model: str | None = None
    image_model: str | None = None
    campaign_model: str | None = None


class TokenPolicyUpdateRequest(BaseModel):
    mode: str


class AiCacheUpdateRequest(BaseModel):
    enabled: bool | None = None
    max_entries: int | None = None


def configure_config_api(
    db_file: str,
    chat_client=None,
    persona_config: dict | None = None,
    fn_get_map_context=None,
    fn_auto_place_room=None,
    fn_process_map_actions=None,
    fn_get_current_room_id=None,
    fn_ai_extract_and_upsert_entities=None,
    fn_build_persona_instruction=None,
    fn_l1_append=None,
    fn_l1_get_working_context=None,
    fn_append_to_memory=None,
    fn_tl_append_memory=None,
    fn_get_world_entities_text=None,
    fn_rag_retrieve=None,
    fn_get_embeddings=None,
    fn_refresh_vector_cache=None,
) -> None:
    """Inject the same agent dependencies used by main.py startup wiring."""
    global _agent_config_kwargs
    _agent_config_kwargs = {
        "db_file": db_file,
        "chat_client": chat_client,
        "persona_config": persona_config,
        "fn_get_map_context": fn_get_map_context,
        "fn_auto_place_room": fn_auto_place_room,
        "fn_process_map_actions": fn_process_map_actions,
        "fn_get_current_room_id": fn_get_current_room_id,
        "fn_ai_extract_and_upsert_entities": fn_ai_extract_and_upsert_entities,
        "fn_build_persona_instruction": fn_build_persona_instruction,
        "fn_l1_append": fn_l1_append,
        "fn_l1_get_working_context": fn_l1_get_working_context,
        "fn_append_to_memory": fn_append_to_memory,
        "fn_tl_append_memory": fn_tl_append_memory,
        "fn_get_world_entities_text": fn_get_world_entities_text,
        "fn_rag_retrieve": fn_rag_retrieve,
        "fn_get_embeddings": fn_get_embeddings,
        "fn_refresh_vector_cache": fn_refresh_vector_cache,
    }


def _reconfigure_agent() -> None:
    if not _agent_config_kwargs:
        raise RuntimeError("config API has not been configured")
    _configure_agent(**_agent_config_kwargs)


def _token_policy_status() -> dict:
    active = ai_provider.get_token_policy()
    modes = []
    for mode, policy in ai_provider.TOKEN_POLICY_MODES.items():
        modes.append({
            "mode": mode,
            "label": policy.get("label") or mode,
            "rag_top_k": policy.get("rag_top_k", 0),
            "output_token_cap": policy.get("output_token_cap", 0),
            "json_output_token_cap": policy.get("json_output_token_cap", 0),
        })
    return {"mode": active["mode"], "active": active, "modes": modes}


def _account_owner_kwargs(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "owner_account_id": int(account["id"]),
        "owner_username": str(account.get("username") or ""),
        "owner_display_name": str(account.get("display_name") or account.get("username") or ""),
    }


def _request_owner_id(request: Request) -> int | None:
    account = account_from_request(request)
    if not account:
        return None
    try:
        return int(account["id"])
    except (KeyError, TypeError, ValueError):
        return None


def _validate_user_provider_base_url(base_url: str | None) -> None:
    if not base_url:
        return
    parsed = urllib.parse.urlparse(str(base_url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise fastapi.HTTPException(status_code=400, detail="Base URL 必须是 http(s) 地址")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise fastapi.HTTPException(status_code=400, detail="Base URL 缺少主机名")
    if host in {"localhost"} or host.endswith(".localhost") or host.endswith(".local"):
        raise fastapi.HTTPException(status_code=400, detail="账号不能配置本机或内网模型地址")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise fastapi.HTTPException(status_code=400, detail="账号不能配置本机或内网模型地址")


@config_router.get("/api/config/keys")
def get_api_keys_status(request: Request):
    """返回 AI 端点配置状态（不返回明文密钥）。"""
    owner_id = _request_owner_id(request)
    cfg = ai_provider.get_config(owner_account_id=owner_id if owner_id is not None else -1)
    providers = ai_provider.list_provider_profiles(owner_account_id=owner_id, include_unowned=False) if owner_id else []
    return {
        "status": "success",
        "keys": {
            "openai_compatible": {
                "configured": cfg.configured,
                "label": "OpenAI 兼容端点",
                "required": True,
            },
        },
        "providers": providers,
        "active_provider": ai_provider.get_active_provider_id(owner_account_id=owner_id) if owner_id else "",
        "token_policy": _token_policy_status(),
        "ai_cache": ai_cache.cache_status(),
        "config": {
            "provider_id": cfg.provider_id,
            "provider_name": cfg.provider_name,
            "base_url": cfg.base_url,
            "chat_model": ai_provider.get_active_model("chat", owner_account_id=owner_id) or cfg.chat_model,
            "embedding_model": ai_provider.get_active_model("embedding", owner_account_id=owner_id) or cfg.embedding_model,
            "image_model": ai_provider.get_active_model("image", owner_account_id=owner_id) or cfg.image_model,
            "campaign_model": ai_provider.get_active_model("campaign", owner_account_id=owner_id) or cfg.campaign_model,
            "image_size": cfg.image_size,
        },
    }


@config_router.post("/api/config/keys")
def update_api_keys(req: ApiKeysUpdateRequest, request: Request):
    """
    将用户填写的 OpenAI 兼容端点配置写入本地 provider profile 并热重载。
    只更新有值的字段，不清空已有配置。
    """
    account = require_account_from_request(request)
    owner_id = int(account["id"])
    _validate_user_provider_base_url(req.openai_compat_base_url)

    try:
        cfg = ai_provider.upsert_provider_profile(
            provider_id=req.provider_id or "",
            provider_name=req.provider_name or "",
            api_key=req.openai_compat_api_key,
            base_url=req.openai_compat_base_url,
            chat_model=req.chat_model,
            embedding_model=req.embedding_model,
            image_model=req.image_model,
            campaign_model=req.campaign_model,
            image_size=req.image_size,
            make_active=req.make_active,
            **_account_owner_kwargs(account),
        )
    except Exception as e:
        return {"status": "error", "message": f"保存供应商配置失败：{e}"}

    if req.chat_model and req.make_active:
        ai_provider.set_active_model(req.chat_model.strip(), "chat", owner_account_id=owner_id)
    if req.embedding_model and req.make_active:
        ai_provider.set_active_model(req.embedding_model.strip(), "embedding", owner_account_id=owner_id)
    if req.image_model and req.make_active:
        ai_provider.set_active_model(req.image_model.strip(), "image", owner_account_id=owner_id)
    if req.campaign_model and req.make_active:
        ai_provider.set_active_model(req.campaign_model.strip(), "campaign", owner_account_id=owner_id)
    if req.token_policy_mode:
        ai_provider.set_token_policy_mode(req.token_policy_mode)

    _reconfigure_agent()

    return {
        "status": "success",
        "message": f"已保存供应商：{cfg.provider_name}",
        "keys": {
            "openai_compatible": {"configured": ai_provider.is_configured(owner_account_id=owner_id)},
        },
        "providers": ai_provider.list_provider_profiles(owner_account_id=owner_id, include_unowned=False),
        "active_provider": ai_provider.get_active_provider_id(owner_account_id=owner_id),
        "token_policy": _token_policy_status(),
        "ai_cache": ai_cache.cache_status(),
        "config": get_api_keys_status(request)["config"],
    }


@config_router.get("/api/config/token-policy")
def get_token_policy_status():
    """返回当前省 token 策略。"""
    return {"status": "success", "token_policy": _token_policy_status()}


@config_router.post("/api/config/token-policy")
def update_token_policy(req: TokenPolicyUpdateRequest, request: Request):
    """更新游玩过程中的 prompt 上下文预算与输出上限策略。"""
    require_account_from_request(request)
    mode = ai_provider.normalize_token_policy_mode(req.mode)
    ai_provider.set_token_policy_mode(mode)
    return {"status": "success", "token_policy": _token_policy_status()}


@config_router.get("/api/config/ai-cache")
def get_ai_cache_status():
    """返回 AI 响应缓存状态与统计。"""
    return {"status": "success", "ai_cache": ai_cache.cache_status()}


@config_router.post("/api/config/ai-cache")
def update_ai_cache(req: AiCacheUpdateRequest, request: Request):
    """更新 AI 响应缓存开关与容量。"""
    require_account_from_request(request)
    settings = ai_cache.update_settings(enabled=req.enabled, max_entries=req.max_entries)
    return {"status": "success", "ai_cache": {**ai_cache.cache_status(), **settings}}


@config_router.post("/api/config/ai-cache/clear")
def clear_ai_cache(request: Request):
    """清空已缓存的非流式 AI 响应。"""
    require_account_from_request(request)
    deleted = ai_cache.clear_cache()
    return {"status": "success", "deleted": deleted, "ai_cache": ai_cache.cache_status()}


@config_router.post("/api/config/providers/switch")
def switch_provider(req: ProviderSwitchRequest, request: Request):
    """切换当前 OpenAI 兼容供应商 profile。"""
    account = require_account_from_request(request)
    owner_id = int(account["id"])
    try:
        cfg = ai_provider.set_active_provider(req.provider_id, owner_account_id=owner_id)
        reset_active_model()
    except Exception as e:
        return {"status": "error", "message": str(e)}
    return {
        "status": "success",
        "active_provider": cfg.provider_id,
        "token_policy": _token_policy_status(),
        "ai_cache": ai_cache.cache_status(),
        "config": get_api_keys_status(request)["config"],
        "providers": ai_provider.list_provider_profiles(owner_account_id=owner_id, include_unowned=False),
    }


@config_router.post("/api/config/providers/delete")
def delete_provider(req: ProviderDeleteRequest, request: Request):
    """删除本地 OpenAI 兼容供应商 profile。"""
    account = require_account_from_request(request)
    owner_id = int(account["id"])
    try:
        active_provider = ai_provider.delete_provider_profile(req.provider_id, owner_account_id=owner_id)
        reset_active_model()
    except Exception as e:
        return {"status": "error", "message": str(e)}
    return {
        "status": "success",
        "active_provider": active_provider,
        "token_policy": _token_policy_status(),
        "ai_cache": ai_cache.cache_status(),
        "config": get_api_keys_status(request)["config"],
        "providers": ai_provider.list_provider_profiles(owner_account_id=owner_id, include_unowned=False),
    }


def _config_model_response(
    req: ModelListRequest,
    *,
    owner_account_id: int | None = None,
    allow_draft_endpoint: bool = False,
):
    capability = (req.capability or "chat").strip() or "chat"
    search = (req.search or "").strip()
    field = ai_provider.MODEL_CAPABILITY_FIELDS.get(capability)
    if not field:
        return {
            "status": "error",
            "message": f"不支持的模型能力：{capability}",
            "capability": capability,
            "active": "",
            "models": [],
        }

    provider_id = (req.provider_id or "").strip()
    draft_key = (req.openai_compat_api_key or "").strip()
    draft_base_url = (req.openai_compat_base_url or "").strip()
    provider_name = (req.provider_name or "").strip()
    draft_model_values = {
        "chat": req.chat_model or "",
        "embedding": req.embedding_model or "",
        "image": req.image_model or "",
        "campaign": req.campaign_model or "",
    }

    if allow_draft_endpoint and (draft_key or not provider_id):
        _validate_user_provider_base_url(draft_base_url)
        if not draft_key or not draft_base_url:
            return {
                "status": "error",
                "message": "请先填写当前供应商的 API Key 和 Base URL，再获取模型。",
                "capability": capability,
                "active": draft_model_values.get(capability, ""),
                "models": [],
                "provider": {
                    "id": provider_id or "draft",
                    "name": provider_name or "未保存供应商",
                    "base_url": draft_base_url,
                },
                "draft": True,
            }
        try:
            models = ai_provider.list_remote_models_for_endpoint(
                api_key=draft_key,
                base_url=draft_base_url,
                search=search,
                provider_name=provider_name or "未保存供应商",
                provider_id=provider_id or "draft",
            )
            error = ""
        except Exception as e:
            models = []
            error = f"{type(e).__name__}: {e}"
        active = draft_model_values.get(capability, "")
        provider = {
            "id": provider_id or "draft",
            "name": provider_name or "未保存供应商",
            "base_url": draft_base_url,
        }
        configured = bool(draft_key and draft_base_url)
        draft = True
    else:
        cfg = ai_provider.get_config(provider_id or None, owner_account_id=owner_account_id)
        try:
            models = ai_provider.list_remote_models(search, provider_id or None, owner_account_id=owner_account_id)
            error = ""
        except Exception as e:
            models = []
            error = f"{type(e).__name__}: {e}"
        active_runtime = (
            ai_provider.get_active_model(capability, owner_account_id=owner_account_id)
            if cfg.provider_id == ai_provider.get_active_provider_id(owner_account_id=owner_account_id)
            else ""
        )
        active = active_runtime or getattr(cfg, field, "")
        provider = {"id": cfg.provider_id, "name": cfg.provider_name, "base_url": cfg.base_url}
        configured = cfg.configured
        draft = False

    if active and not any(m["key"] == active for m in models):
        needle = search.lower()
        if not needle or needle in active.lower():
            models.insert(0, {
                "key": active,
                "model_id": active,
                "label": active,
                "available": configured,
                "provider": provider["name"],
                "provider_id": provider["id"],
            })
    return {
        "status": "success",
        "capability": capability,
        "active": active,
        "models": models,
        "provider": provider,
        "draft": draft,
        "error": error,
    }


@config_router.get("/api/config/models")
def list_config_models(request: Request, capability: str = "chat", search: str = ""):
    """为配置面板获取已保存供应商的模型列表，并按能力返回当前选中模型。"""
    account = require_account_from_request(request)
    return _config_model_response(ModelListRequest(capability=capability, search=search), owner_account_id=int(account["id"]))


@config_router.post("/api/config/models")
def list_config_models_for_current_form(req: ModelListRequest, request: Request):
    """按配置面板当前表单草稿获取模型，不隐式回退到旧 active 供应商。"""
    account = require_account_from_request(request)
    return _config_model_response(
        req,
        owner_account_id=int(account["id"]),
        allow_draft_endpoint=True,
    )
