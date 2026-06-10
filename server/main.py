"""
Z.R.I.C 引擎 — 主系统模块 (main.py)
"""



import fastapi
from fastapi import Request, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.background import BackgroundTask
from pydantic import BaseModel
import uvicorn
import sqlite3
import json
import json_repair 
import os
import io
import glob
import re
import shutil
import zipfile
import tempfile
from datetime import datetime
import urllib.parse
import time
import webbrowser
import threading
import math
from . import ai_provider



app = fastapi.FastAPI(title="RPG 桌游控制台 API - V5")

# CORS 仅允许本地访问，部署时通过 ALLOWED_ORIGINS 环境变量配置
# 示例：ALLOWED_ORIGINS=http://localhost:8000,http://192.168.1.100:8000
_default_origins = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
]
if os.environ.get("ZRIC_ALLOW_FILE_ORIGIN", "").lower() in {"1", "true", "yes"}:
    _default_origins.append("null")
_env_origins = os.environ.get("ALLOWED_ORIGINS", "")
_allowed_origins = [o.strip() for o in _env_origins.split(",") if o.strip()] if _env_origins else _default_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Admin-Token", "X-Room-Token", "X-Member-Token"],
)

# ---------------------------------------------------------
# 【统一错误处理】：所有异常统一为 {"status":"error","message":"..."}
# 前端只需检查 response.status === "error" 即可
# ---------------------------------------------------------
@app.exception_handler(fastapi.HTTPException)
async def http_exception_handler(request, exc: fastapi.HTTPException):
    """将 HTTPException 统一为 JSON 格式，不再返回 FastAPI 默认的 {"detail":"..."}"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "message": exc.detail},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    """将参数校验错误统一为 JSON 格式"""
    errors = exc.errors()
    msg = "; ".join(f"{e.get('loc',['?'])[-1]}: {e.get('msg','未知错误')}" for e in errors)
    return JSONResponse(
        status_code=422,
        content={"status": "error", "message": f"参数校验失败: {msg}"},
    )

# ---------------------------------------------------------
# 【模块化】：挂载独立地图模块
# ---------------------------------------------------------
from .map import map_router, init_map_tables, set_db_file as map_set_db_file
from .map import get_map_context as _get_map_context
from .map import auto_place_room as _auto_place_room
from .map import export_map_data, import_map_data, clear_map_data
app.include_router(map_router)

# ---------------------------------------------------------
# 【模块化】：挂载 RAG 知识库模块
# ---------------------------------------------------------
from .rag import rag_router, configure_rag, init_rag_tables
from .rag import chunk_text as _chunk_text, get_embeddings as _get_embeddings
from .rag import rag_retrieve as _rag_retrieve, cosine_similarity as _cosine_similarity
from .rag import refresh_vector_cache as _refresh_vector_cache
app.include_router(rag_router)

# ---------------------------------------------------------
# 【模块化】：挂载 AI Agent 模块（含 SSE 流式推演）
# ---------------------------------------------------------
from .agent import agent_router, configure_agent
from .agent import build_system_context as get_system_context
app.include_router(agent_router)

# ---------------------------------------------------------
# 【模块化】：挂载记忆系统模块
# ---------------------------------------------------------
from .memory import memory_router, configure_memory
from .memory import _l1_append
from .memory import l1_get_working_context as _l1_get_working_context
from .memory import append_to_memory, _tl_append_memory
from .memory import fold_memory_with_ai as _fold_memory_with_ai
from .memory import MEMORY_FOLD_THRESHOLD, MEMORY_SUMMARY_LIMIT
app.include_router(memory_router)

# ---------------------------------------------------------
# 【模块化】：挂载触发器系统模块
# ---------------------------------------------------------
from .trigger import trigger_router, configure_trigger
app.include_router(trigger_router)

# ---------------------------------------------------------
# 【模块化】：挂载世界实体模块
# ---------------------------------------------------------
from .entity import entity_router, configure_entity
from .entity import get_world_entities_text as _get_world_entities_text
from .entity import ai_extract_and_upsert_entities as _ai_extract_and_upsert_entities
app.include_router(entity_router)

# ---------------------------------------------------------
# 【模块化】：挂载时间线模块
# ---------------------------------------------------------
from .timeline import timeline_router, configure_timeline
app.include_router(timeline_router)

# ---------------------------------------------------------
# 【模块化】：挂载 TRPG 骰子/规则服务
# ---------------------------------------------------------
from .dice import dice_router, configure_dice_service
app.include_router(dice_router)

# ---------------------------------------------------------
# 【模块化】：挂载配置管理接口
# ---------------------------------------------------------
from .config_api import config_router, configure_config_api
app.include_router(config_router)

# ---------------------------------------------------------
# 【模块化】：托管前端静态页面和资源
# ---------------------------------------------------------
from .static_files import configure_static_files, static_router
from .campaign_storage import (
    account_id as _account_id,
    account_owner_metadata as _account_owner_metadata,
    campaign_asset_url as _campaign_asset_url,
    campaign_summary as _campaign_summary,
    cleanup_managed_campaign_folder as _cleanup_managed_campaign_folder,
    configure_campaign_storage,
    ensure_save_readable as _ensure_save_readable,
    folder_size_bytes as _folder_size_bytes,
    format_mtime as _format_mtime,
    manifest_owner_id as _manifest_owner_id,
    read_save_manifest as _read_save_manifest,
    require_private_save_owner as _require_private_save_owner,
    resolve_campaign_asset as _resolve_campaign_asset,
    resolve_campaign_folder_name as _resolve_campaign_folder_name,
    sanitize_asset_name as _sanitize_asset_name,
    sanitize_campaign_name as _sanitize_campaign_name,
    save_visible_to_account as _save_visible_to_account,
    unique_path as _unique_path,
    write_save_manifest as _write_save_manifest,
)
from .campaign_import_converters import (
    ai_convert_campaign,
    decode_text_bytes,
    extract_docx_images,
    extract_docx_text,
    extract_pdf,
)

# ---------------------------------------------------------
# 【模块化】：挂载全局账号认证
# ---------------------------------------------------------
from .auth import account_from_request, auth_router, configure_auth, init_auth_tables, require_account_from_request
app.include_router(auth_router)

# ---------------------------------------------------------
# 【模块化】：挂载多人联机融合模块
# ---------------------------------------------------------
from .multiplayer import MAX_ROOM_PLAYERS, multiplayer_router, configure_multiplayer, init_multiplayer_tables
app.include_router(multiplayer_router)

# ---------------------------------------------------------
# 配置区与绝对路径锁定 (修复打包后变空白、找不到文件的问题)
# ---------------------------------------------------------
import sys
import os

# 【关键修复】：识别当前是源码运行，还是 exe 运行
_base_dir_override = os.environ.get("ZRIC_BASE_DIR", "").strip()
if _base_dir_override:
    BASE_DIR = os.path.abspath(_base_dir_override)
elif getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
configure_static_files(WEB_DIR)
CAMPAIGNS_DIR = os.path.join(BASE_DIR, "campaigns")  # 模块化剧本文件夹

# 【安全】：从 .env 文件或系统环境变量中加载 API 配置，绝不硬编码
# 安装方式：pip install python-dotenv
# 使用方式：在项目根目录创建 .env 文件，写入 OPENAI_COMPAT_* 配置；
# 也可以通过前端保存多个 OpenAI 兼容供应商到本地 openai_providers.json。
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass  # python-dotenv 未安装时，回退到纯系统环境变量

ai_provider.configure_provider_store(os.path.join(BASE_DIR, "openai_providers.json"))

DB_FILE = os.path.join(BASE_DIR, "rpg_game.db")
os.makedirs(CAMPAIGNS_DIR, exist_ok=True)
RAG_AUTO_REBUILD_EMBEDDINGS = os.environ.get("ZRIC_AUTO_REBUILD_RAG_EMBEDDINGS", "").lower() in {"1", "true", "yes", "on"}

# 【模块化】：将 DB 路径注入地图模块
map_set_db_file(DB_FILE)

# 【模块化】：配置 RAG 模块
configure_rag(DB_FILE)

# 【模块化】：配置记忆系统模块（运行时记忆归档默认不调用 embedding）
configure_memory(DB_FILE, fn_get_embeddings=None)

# 【模块化】：配置世界实体模块
configure_entity(DB_FILE)

# 【模块化】：配置时间线模块（tl_append_memory 延迟到 startup 注入）
configure_timeline(DB_FILE, fn_tl_append_memory=_tl_append_memory,
                   memory_summary_limit=MEMORY_SUMMARY_LIMIT)

# 【模块化】：配置 TRPG 骰子/规则服务
configure_dice_service(DB_FILE, fn_append_to_memory=append_to_memory)

# 【模块化】：配置多人联机模块（复用 TRPG_Engine 骰子与 ZRIC RAG/记忆）
configure_multiplayer(
    DB_FILE,
    fn_append_to_memory=append_to_memory,
    fn_chunk_text=_chunk_text,
    fn_get_embeddings=_get_embeddings,
    fn_refresh_vector_cache=_refresh_vector_cache,
)

# ---------------------------------------------------------
# 【Persona 模式】：启动时加载 persona_mode.json（若存在）
# ---------------------------------------------------------
def _load_persona_config() -> dict:
    path = os.path.join(BASE_DIR, "persona_mode.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("enabled"):
            return cfg
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}

PERSONA_CONFIG = _load_persona_config()


# ---------------------------------------------------------
# 数据库初始化
# ---------------------------------------------------------
from .logger import get_logger
from .database import (
    configure_database,
    get_db_connection,
    safe_db,
    init_db as init_core_db,
)
from .campaign_import_workflow import CampaignImportWorkflow, ImportAsset

_log = get_logger("main")

configure_database(DB_FILE)
configure_auth(DB_FILE)
init_core_db(init_map_tables=init_map_tables, init_rag_tables=init_rag_tables)
init_auth_tables()
init_multiplayer_tables()

# ---------------------------------------------------------
# 【模块化】：配置 Agent 模块（注入所有依赖函数）
# 使用 startup 事件延迟执行，确保所有函数已定义
# ---------------------------------------------------------
@app.on_event("startup")
def _startup_wire_agent():
    configure_agent(
        db_file=DB_FILE,
        persona_config=PERSONA_CONFIG,
        fn_get_map_context=_get_map_context,
        fn_auto_place_room=_auto_place_room,
        fn_process_map_actions=_process_map_actions,
        fn_get_current_room_id=_get_current_room_id,
        fn_ai_extract_and_upsert_entities=_ai_extract_and_upsert_entities,
        fn_build_persona_instruction=_build_persona_instruction,
        fn_l1_append=_l1_append,
        fn_l1_get_working_context=_l1_get_working_context,
        fn_append_to_memory=append_to_memory,
        fn_tl_append_memory=_tl_append_memory,
        fn_get_world_entities_text=_get_world_entities_text,
        fn_rag_retrieve=_rag_retrieve,
        fn_get_embeddings=None,
        fn_refresh_vector_cache=_refresh_vector_cache,
    )
    configure_config_api(
        db_file=DB_FILE,
        persona_config=PERSONA_CONFIG,
        fn_get_map_context=_get_map_context,
        fn_auto_place_room=_auto_place_room,
        fn_process_map_actions=_process_map_actions,
        fn_get_current_room_id=_get_current_room_id,
        fn_ai_extract_and_upsert_entities=_ai_extract_and_upsert_entities,
        fn_build_persona_instruction=_build_persona_instruction,
        fn_l1_append=_l1_append,
        fn_l1_get_working_context=_l1_get_working_context,
        fn_append_to_memory=append_to_memory,
        fn_tl_append_memory=_tl_append_memory,
        fn_get_world_entities_text=_get_world_entities_text,
        fn_rag_retrieve=_rag_retrieve,
        fn_get_embeddings=None,
        fn_refresh_vector_cache=_refresh_vector_cache,
    )
    # 触发器模块需要 get_system_context（来自 agent），在 agent 配置完成后注入
    configure_trigger(
        db_file=DB_FILE,
        fn_get_system_context=get_system_context,
        fn_append_to_memory=append_to_memory,
    )

# ---------------------------------------------------------
# 数据模型定义（供 API 请求体使用，CRUD 模型已迁移至各自模块）
# ---------------------------------------------------------
class AIContextRequest(BaseModel): scene_name: str = ""; content: str = ""
# DynamicActionRequest 已移至 agent.py
class CharUpdateRequest(BaseModel):
    name: str | None = None
    hp: int; san: int; inventory: str = ""; personality: str = ""
    status: str = "active"
class NodeCreateRequest(BaseModel): name: str; summary: str; content: str
class NodeUpdateRequest(BaseModel): name: str; summary: str; content: str
class OptionCreateRequest(BaseModel): node_id: int; text: str; next_node_id: int
class StringContentRequest(BaseModel): content: str
class LoadCampaignRequest(BaseModel): filename: str
class LorebookRequest(BaseModel): keywords: str; content: str
class CharCreateRequest(BaseModel):
    name: str
    role: str = "PC"
    hp: int = 100
    san: int = 80
    inventory: str = ""
    personality: str = ""
    status: str = "active"
class AutoNPCRequest(BaseModel):
    scene_name: str
    scene_content: str
    player_action: str
class ImageGenRequest(BaseModel):
    description: str = ""       # GM 补充描述（可为空，系统会自动从场景提取）
    style: str = "fantasy"      # 风格：fantasy / horror / realistic / anime / sketch
    scene_id: int | None = None # 当前场景 ID（传入后自动提取场景名+描述+地图位置）
    scene_name: str = ""        # 场景名（scene_id 未传时的手动兜底）
    scene_content: str = ""     # 场景正文（scene_id 未传时的手动兜底）
    image_model: str = ""       # 可选：请求级覆盖 OPENAI_COMPAT_IMAGE_MODEL

# 【多时间线推演】：数据模型（CRUD 模型已迁移至 timeline.py）
class TimelineDynamicRequest(BaseModel):
    timeline_id: int
    current_node_id: int
    scene_name: str = ""
    content: str = ""
    player_action: str = ""
    action_type: str = "mixed"

# 【地图系统】：数据模型已移至 map.py（通过 map_router 自动注册）

# ---------------------------------------------------------
# API 接口：剧本与存档管理（支持模块化文件夹结构）
# ---------------------------------------------------------
# 文件夹结构：
#   campaigns/
#     我的剧本/
#       campaign.json    ← 剧目（节点、角色、世界观、触发器等）
#       map.json         ← 地图（房间、通道）
#       knowledge/       ← 知识库
#         *.txt / *.md   ← RAG 文档
#   *.json               ← 兼容旧版单文件存档（平铺在 BASE_DIR 下）
# ---------------------------------------------------------

def _campaign_player_limits(path: str) -> tuple[bool, dict[str, int]]:
    """Read lightweight campaign metadata used before a campaign is loaded."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False, {}
    if not isinstance(data, dict):
        return False, {}
    if not isinstance(data.get("nodes"), list):
        return False, {}

    characters = data.get("characters", [])
    if not isinstance(characters, list):
        characters = []
    active_characters = [
        char for char in characters
        if isinstance(char, dict) and (char.get("status") or "active") != "hidden"
    ]
    character_count = len(characters)
    playable_count = len(active_characters) or character_count or 1
    max_players = max(1, min(playable_count, MAX_ROOM_PLAYERS))
    return True, {
        "character_count": character_count,
        "playable_character_count": playable_count,
        "max_players": max_players,
    }


def _is_legacy_campaign_file(path: str) -> bool:
    """Only accept root-level JSON files that look like old campaign saves."""
    valid, _limits = _campaign_player_limits(path)
    return valid


configure_campaign_storage(BASE_DIR, CAMPAIGNS_DIR, _is_legacy_campaign_file)


@app.get("/api/campaigns")
def list_campaigns(request: Request):
    """列出所有可加载的剧本（兼容旧版单文件 + 新版文件夹结构）"""
    account = account_from_request(request)
    results = []

    # 旧版：BASE_DIR 下的 *.json（向后兼容）
    for f in glob.glob(os.path.join(BASE_DIR, "*.json")):
        fname = os.path.basename(f)
        if fname.startswith("persona_mode"): continue
        valid, limits = _campaign_player_limits(f)
        if not valid: continue
        results.append({
            "name": fname,
            "type": "legacy",
            "path": fname,
            "updated_at": _format_mtime(f),
            "size_bytes": os.path.getsize(f) if os.path.exists(f) else 0,
            "download_url": "",
            "deletable": False,
            **limits,
            **_campaign_summary(f),
        })

    # 新版：campaigns/ 下的子文件夹（含 campaign.json）
    if os.path.isdir(CAMPAIGNS_DIR):
        for d in sorted(os.listdir(CAMPAIGNS_DIR)):
            folder = os.path.join(CAMPAIGNS_DIR, d)
            campaign_json = os.path.join(folder, "campaign.json")
            valid, limits = _campaign_player_limits(campaign_json)
            if os.path.isdir(folder) and valid:
                manifest = _read_save_manifest(folder)
                if not _save_visible_to_account(manifest, account):
                    continue
                owner_id = _manifest_owner_id(manifest)
                owned_by_me = owner_id is not None and owner_id == _account_id(account)
                has_map = os.path.exists(os.path.join(folder, "map.json"))
                kb_dir = os.path.join(folder, "knowledge")
                assets_dir = os.path.join(folder, "assets")
                kb_count = 0
                if os.path.isdir(kb_dir):
                    kb_count = len(
                        glob.glob(os.path.join(kb_dir, "*.txt"))
                        + glob.glob(os.path.join(kb_dir, "*.md"))
                    )
                asset_count = len(glob.glob(os.path.join(assets_dir, "*"))) if os.path.isdir(assets_dir) else 0
                results.append({
                    "name": d, "type": "folder",
                    "path": f"campaigns/{d}",
                    "scope": "private" if owner_id is not None else "public",
                    "owned_by_me": owned_by_me,
                    "owner_account_id": owner_id,
                    "owner_username": manifest.get("owner_username", ""),
                    "has_map": has_map,
                    "kb_count": kb_count,
                    "asset_count": asset_count,
                    "updated_at": _format_mtime(campaign_json),
                    "size_bytes": _folder_size_bytes(folder),
                    "download_url": f"/api/game/saves/{urllib.parse.quote(d)}/download" if owned_by_me else "",
                    "deletable": owned_by_me,
                    **limits,
                    **_campaign_summary(campaign_json),
                })

    # 向后兼容：同时返回旧版字符串数组格式（供未升级的前端使用）
    files_legacy = [r["path"] for r in results]
    return {"status": "success", "files": results, "files_legacy": files_legacy}


def _resolve_campaign_load_target(filename: str) -> tuple[str, bool, str, str | None, str | None]:
    requested = (filename or "").strip().replace("\\", "/")
    if not requested or os.path.isabs(requested) or "\x00" in requested:
        raise fastapi.HTTPException(status_code=400, detail="非法剧本路径")
    parts = [part for part in requested.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise fastapi.HTTPException(status_code=400, detail="非法剧本路径")

    if parts[:1] == ["campaigns"]:
        if len(parts) != 2:
            raise fastapi.HTTPException(status_code=400, detail="仅支持 campaigns/<剧本名> 格式")
        root = os.path.realpath(CAMPAIGNS_DIR)
        target = os.path.realpath(os.path.join(root, parts[1]))
        try:
            if os.path.commonpath([root, target]) != root:
                raise ValueError
        except ValueError:
            raise fastapi.HTTPException(status_code=400, detail="非法剧本路径") from None
        campaign_path = os.path.join(target, "campaign.json")
        return target, True, campaign_path, os.path.join(target, "map.json"), os.path.join(target, "knowledge")

    if len(parts) != 1 or os.path.splitext(parts[0])[1].lower() != ".json":
        raise fastapi.HTTPException(status_code=400, detail="仅支持根目录旧版 JSON 存档或 campaigns/<剧本名>")
    root = os.path.realpath(BASE_DIR)
    target = os.path.realpath(os.path.join(root, parts[0]))
    if os.path.dirname(target) != root:
        raise fastapi.HTTPException(status_code=400, detail="非法剧本路径")
    if os.path.exists(target) and not _is_legacy_campaign_file(target):
        raise fastapi.HTTPException(status_code=400, detail="文件不是有效剧本存档")
    return target, False, target, None, None






_campaign_import_workflow = CampaignImportWorkflow(
    campaigns_dir=CAMPAIGNS_DIR,
    sanitize_campaign_name=_sanitize_campaign_name,
    sanitize_asset_name=_sanitize_asset_name,
    unique_path=_unique_path,
    campaign_asset_url=_campaign_asset_url,
    decode_text_bytes=decode_text_bytes,
    extract_docx_text=extract_docx_text,
    extract_docx_images=extract_docx_images,
    extract_pdf=extract_pdf,
    ai_convert_campaign=ai_convert_campaign,
    logger=_log,
)
_campaign_import_job_owners: dict[str, dict] = {}


@app.get("/api/campaigns/import/formats")
def campaign_import_formats():
    return {
        "status": "success",
        "formats": [
            {"ext": ".pdf", "label": "PDF", "notes": "支持文字型 PDF；扫描版暂不做 OCR"},
            {"ext": ".docx", "label": "Word DOCX", "notes": "支持正文、表格文本与内嵌图片"},
            {"ext": ".txt", "label": "TXT", "notes": "UTF-8 优先，GBK fallback"},
            {"ext": ".md", "label": "Markdown", "notes": "按纯文本导入"},
        ],
        "asset_formats": [".png", ".jpg", ".jpeg", ".webp", ".gif"],
    }


@app.post("/api/campaigns/import")
async def import_campaign_from_document(
    request: Request,
    name: str = Form(""),
    main_file: UploadFile = File(...),
    assets: list[UploadFile] | None = File(None),
):
    account = require_account_from_request(request)
    filename = main_file.filename or "scenario.txt"
    suffix = os.path.splitext(filename)[1].lower()
    if suffix == ".doc":
        raise fastapi.HTTPException(status_code=400, detail="暂不支持旧式 .doc，请在 Word 中另存为 .docx 后导入")
    if suffix not in {".pdf", ".docx", ".txt", ".md", ".markdown"}:
        raise fastapi.HTTPException(status_code=400, detail="仅支持 PDF、DOCX、TXT、Markdown 剧本文件")

    raw = await main_file.read()
    if not raw:
        raise fastapi.HTTPException(status_code=400, detail="文件内容为空")

    buffered_assets = [
        ImportAsset(filename=asset.filename or "", data=await asset.read())
        for asset in (assets or [])
    ]
    job = _campaign_import_workflow.create_job(
        requested_name=name,
        filename=filename,
        suffix=suffix,
        raw=raw,
        assets=buffered_assets,
        metadata=_account_owner_metadata(account),
    )
    _campaign_import_job_owners[job.id] = _account_owner_metadata(account)
    return {"status": "accepted", "job_id": job.id, "job": job.to_dict()}


@app.get("/api/campaigns/import/{job_id}")
def get_campaign_import_job(job_id: str, request: Request):
    account = require_account_from_request(request)
    owner = _campaign_import_job_owners.get(job_id)
    if owner and owner.get("owner_account_id") != _account_id(account):
        raise fastapi.HTTPException(status_code=403, detail="无权查看其他账号的导入任务")
    job = _campaign_import_workflow.get_job(job_id)
    if not job:
        raise fastapi.HTTPException(status_code=404, detail="导入任务不存在或已过期")
    return {"status": "success", "job": job}

@app.post("/api/game/load")
def load_campaign(req: LoadCampaignRequest, request: Request):
    """
    加载剧本。支持两种格式：
    1. 旧版单文件：req.filename = "save_xxx.json"
    2. 新版文件夹：req.filename = "campaigns/我的剧本"
       自动加载 campaign.json + map.json + knowledge/*.txt
    """
    # 判断是文件夹还是单文件，并限制到可加载剧本白名单路径形态。
    target, is_folder, campaign_path, map_path, kb_dir = _resolve_campaign_load_target(req.filename)
    if is_folder:
        _ensure_save_readable(target, account_from_request(request))

    if not os.path.exists(campaign_path):
        raise fastapi.HTTPException(status_code=404, detail=f"文件不存在: {campaign_path}")

    try:
        with open(campaign_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        if not isinstance(config, dict) or not isinstance(config.get("nodes"), list):
            raise fastapi.HTTPException(status_code=400, detail="文件不是有效剧本存档")

        conn = get_db_connection()
        cursor = conn.cursor()

        # ── 清空所有业务数据表（含 RAG 知识库）──────────────────────
        for tbl in ("nodes", "options", "characters", "system_state",
                    "lorebook", "triggers", "timelines", "world_entities",
                    "map_rooms", "map_edges",
                    "rag_documents", "rag_chunks",
                    "memory_l1", "pending_effects", "npc_chat_logs"):
            try:
                cursor.execute(f"DELETE FROM {tbl}")
            except sqlite3.OperationalError:
                pass   # 表不存在时静默跳过（旧存档兼容）
        for sql in (
            "DELETE FROM multiplayer_character_claims",
            "DELETE FROM multiplayer_tokens WHERE kind='pc' OR linked_character_id IS NOT NULL",
            "UPDATE multiplayer_rooms SET current_scene_id=NULL, current_room_id=NULL",
        ):
            try:
                cursor.execute(sql)
            except sqlite3.OperationalError:
                pass   # 多人房间表尚未创建时兼容启动

        # 正确重置自增序列：只更新已存在行，不删整张表
        for tbl in ("nodes", "options", "characters", "lorebook",
                    "triggers", "timelines", "world_entities",
                    "map_rooms", "map_edges",
                    "rag_documents", "rag_chunks",
                    "memory_l1", "pending_effects", "npc_chat_logs",
                    "multiplayer_character_claims"):
            try:
                cursor.execute(
                    "UPDATE sqlite_sequence SET seq=0 WHERE name=?", (tbl,)
                )
            except sqlite3.OperationalError:
                pass

        # ── 写入系统状态 ───────────────────────────────────────────
        worldview      = config.get("worldview", "【默认世界观】")
        session_memory = config.get("session_memory", "【跑团记忆日志已初始化】\n")
        cursor.execute("INSERT INTO system_state (key, value) VALUES ('worldview', ?)",       (worldview,))
        cursor.execute("INSERT INTO system_state (key, value) VALUES ('session_memory', ?)",  (session_memory,))
        cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_current_scene_id', '')")
        cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_scene_image', '')")
        cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_scene_prompt', '')")
        cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_scene_ai_text', '')")
        cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_bgm_url', '')")
        cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_bgm_name', '')")

        # ── 还原业务数据 ───────────────────────────────────────────
        for char in config.get("characters", []):
            cursor.execute(
                "INSERT INTO characters (name, role, hp, san, inventory, personality, status) VALUES (?,?,?,?,?,?,?)",
                (char.get("name"), char.get("role"),
                 char.get("hp"), char.get("san"), char.get("inventory", ""),
                 char.get("personality", ""), char.get("status", "active"))
            )
        for node in config.get("nodes", []):
            cursor.execute(
                "INSERT INTO nodes (id, name, summary, content, expanded_content, scene_image) VALUES (?,?,?,?,?,?)",
                (node.get("id"), node.get("name"),
                 node.get("summary"), node.get("content"),
                 node.get("expanded_content", ""),
                 node.get("scene_image", ""))
            )
        for opt in config.get("options", []):
            cursor.execute(
                "INSERT INTO options (node_id, text, next_node_id) VALUES (?,?,?)",
                (opt.get("node_id"), opt.get("text"), opt.get("next_node_id"))
            )
        explicit_lore_keywords: set[str] = set()
        for lore in config.get("lorebook", []):
            keywords = str(lore.get("keywords") or "").strip()
            if keywords:
                explicit_lore_keywords.add(keywords.lower())
            cursor.execute(
                "INSERT INTO lorebook (keywords, content) VALUES (?,?)",
                (keywords, lore.get("content"))
            )
        for t in config.get("triggers", []):
            # 兼容旧存档：可能只有 cond_type/cond_value，没有 conditions
            conditions_raw = t.get("conditions", [])
            # 导出时 conditions 列是 DB TEXT 字段，写入 JSON 后变成字符串 —— 需先反序列化
            if isinstance(conditions_raw, str):
                try:
                    conditions_raw = json.loads(conditions_raw)
                except (json.JSONDecodeError, TypeError):
                    conditions_raw = []
            if not conditions_raw:
                conditions_raw = []
            if not conditions_raw and t.get("cond_type") and t.get("cond_value"):
                conditions_raw = [{"type": t["cond_type"], "value": t["cond_value"]}]
            cond_type = t.get("cond_type", "")
            cond_value = t.get("cond_value", "")
            # 旧存档 cond_type 可能是 None；兼容 list 和 dict（树）两种格式
            if not cond_type:
                if isinstance(conditions_raw, list) and conditions_raw:
                    cond_type = conditions_raw[0].get("type", "")
                    cond_value = conditions_raw[0].get("value", "")
                elif isinstance(conditions_raw, dict):
                    first = (conditions_raw.get("children") or [{}])[0]
                    cond_type = first.get("type", "")
                    cond_value = first.get("value", "")
            cursor.execute(
                "INSERT INTO triggers (label, target_node_id, mode, cond_type, cond_value, conditions, fired) "
                "VALUES (?,?,?,?,?,?,0)",
                (t.get("label", ""), t.get("target_node_id", 0),
                 t.get("mode", "soft"), cond_type or "", cond_value or "",
                 json.dumps(conditions_raw, ensure_ascii=False))
            )
        for tl in config.get("timelines", []):
            cursor.execute(
                "INSERT INTO timelines (label, color, current_node_id, memory, char_ids, status, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (tl.get("label", "时间线"), tl.get("color", "#5b9cf5"),
                 tl.get("current_node_id"), tl.get("memory", ""),
                 tl.get("char_ids", ""), tl.get("status", "active"),
                 tl.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            )
        # 地图数据：优先从独立 map.json 加载，兼容旧版内嵌格式
        clear_map_data(conn)
        if is_folder and map_path and os.path.exists(map_path):
            with open(map_path, "r", encoding="utf-8") as mf:
                map_data = json.load(mf)
            import_map_data(conn, map_data)
        elif config.get("map_rooms"):
            import_map_data(conn, {
                "map_rooms": config.get("map_rooms", []),
                "map_edges": config.get("map_edges", [])
            })

        # ── 还原世界实体 ───────────────────────────────────────
        for we in config.get("world_entities", []):
            cursor.execute(
                "INSERT INTO world_entities "
                "(entity_type, name, location, status, last_seen_by, state_desc, updated_at, room_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (we.get("entity_type", "npc"), we.get("name", ""),
                 we.get("location", ""), we.get("status", "active"),
                 we.get("last_seen_by", ""), we.get("state_desc", ""),
                 we.get("updated_at", ""), we.get("room_id"))
            )

        # ── 还原 memory_l1 ─────────────────────────────────────────
        for ml in config.get("memory_l1", []):
            cursor.execute(
                "INSERT INTO memory_l1 (scene_name, player_action, ai_summary, "
                "thought_process, entity_updates, timeline_id) VALUES (?,?,?,?,?,?)",
                (ml.get("scene_name", ""), ml.get("player_action", ""),
                 ml.get("ai_summary", ""), ml.get("thought_process", ""),
                 ml.get("entity_updates", ""), ml.get("timeline_id"))
            )

        # ── 还原 pending_effects ────────────────────────────────────
        for pe in config.get("pending_effects", []):
            cursor.execute(
                "INSERT INTO pending_effects (node_id, payload) VALUES (?,?)",
                (pe.get("node_id"), pe.get("payload", "{}"))
            )

        # ── 还原 npc_chat_logs ──────────────────────────────────────
        for log in config.get("npc_chat_logs", []):
            cursor.execute(
                "INSERT INTO npc_chat_logs (npc_name, sender, message, created_at) VALUES (?,?,?,?)",
                (log.get("npc_name", ""), log.get("sender", "player"),
                 log.get("message", ""), log.get("created_at", ""))
            )

        # ── knowledge/ 资料：无论 RAG 是否预计算，都先灌入百科库 ───────
        raw_library = []
        if is_folder and kb_dir and os.path.isdir(kb_dir):
            for kb_file in sorted(glob.glob(os.path.join(kb_dir, "*.txt")) +
                                  glob.glob(os.path.join(kb_dir, "*.md"))):
                try:
                    with open(kb_file, "r", encoding="utf-8") as kf:
                        kb_text = kf.read().strip()
                    if kb_text:
                        raw_library.append({
                            "title": os.path.splitext(os.path.basename(kb_file))[0],
                            "source": f"knowledge/{os.path.basename(kb_file)}",
                            "text": kb_text
                        })
                except Exception as e:
                    _log.warning("知识库文件读取失败: %s — %s", kb_file, e)

        auto_lore_count = 0
        for raw_item in raw_library:
            title = str(raw_item.get("title") or "").strip()
            text = str(raw_item.get("text") or "").strip()
            if not title or not text or title.lower() in explicit_lore_keywords:
                continue
            cursor.execute(
                "INSERT INTO lorebook (keywords, content) VALUES (?,?)",
                (title, text[:12000])
            )
            explicit_lore_keywords.add(title.lower())
            auto_lore_count += 1

        # ── 还原 RAG 知识库 ────────────────────────────────────
        rag_library = config.get("rag_library", [])
        load_type = "文件夹" if is_folder else "单文件"

        # 判断是否为含预计算 embedding 的导出存档（items 有 chunks 字段而非 text 字段）
        has_precomputed = bool(rag_library) and all("chunks" in item for item in rag_library)

        if has_precomputed:
            # 直接恢复预计算数据，跳过 embedding API，毫秒级完成
            for doc_item in rag_library:
                cur_doc = cursor.execute(
                    "INSERT INTO rag_documents (title, source, chunk_size) VALUES (?,?,?)",
                    (doc_item["title"][:100], doc_item.get("source", "")[:200],
                     len(doc_item.get("chunks", [])))
                )
                doc_id = cur_doc.lastrowid
                for chunk in doc_item.get("chunks", []):
                    cursor.execute(
                        "INSERT INTO rag_chunks (doc_id, chunk_index, chunk_text, embedding) "
                        "VALUES (?,?,?,?)",
                        (doc_id, chunk["index"], chunk["text"], chunk.get("embedding", "[]"))
                    )
            conn.commit()
            conn.close()
            _refresh_vector_cache()
            _log.info("RAG 知识库从存档直接恢复，共 %d 个文档，跳过 embedding 重建", len(rag_library))
            return {
                "status": "success",
                "message": (
                    f"成功加载 {req.filename}（{load_type}，含 {len(rag_library)} 个RAG文档"
                    f"，自动百科 {auto_lore_count} 条）"
                ),
                "auto_setup": {
                    "rag_documents": len(rag_library),
                    "auto_lore": auto_lore_count,
                    "explicit_lore": len(explicit_lore_keywords) - auto_lore_count,
                    "has_map": bool(map_path and os.path.exists(map_path)) or bool(config.get("map_rooms")),
                },
            }

        for rag_item in raw_library:
            title  = rag_item.get("title", "未命名")
            source = rag_item.get("source", "")
            text   = rag_item.get("text", "")
            if not text.strip():
                continue
            chunks = _chunk_text(text)
            cur_doc = cursor.execute(
                "INSERT INTO rag_documents (title, source, chunk_size) VALUES (?,?,?)",
                (title[:100], source[:200], len(chunks))
            )
            doc_id = cur_doc.lastrowid
            for idx, chunk in enumerate(chunks):
                cursor.execute(
                    "INSERT INTO rag_chunks (doc_id, chunk_index, chunk_text, embedding) "
                    "VALUES (?,?,?,?)",
                    (doc_id, idx, chunk, "[]")
                )

        conn.commit()

        # ── 可选异步重建 RAG embedding（默认关闭，避免载入剧本后批量请求 embedding）────
        if raw_library and RAG_AUTO_REBUILD_EMBEDDINGS:
            import threading
            def _rebuild_embeddings():
                try:
                    _conn = get_db_connection()
                    rows = _conn.execute(
                        "SELECT id, chunk_text FROM rag_chunks WHERE embedding='[]' ORDER BY id"
                    ).fetchall()
                    if not rows:
                        _conn.close()
                        return
                    _log.info("后台 RAG embedding 重建开始，共 %d 个 chunks", len(rows))
                    texts = [r["chunk_text"] for r in rows]
                    ids   = [r["id"] for r in rows]
                    BATCH = 16
                    embedded_count = 0
                    for i in range(0, len(texts), BATCH):
                        batch_texts = texts[i:i+BATCH]
                        batch_ids   = ids[i:i+BATCH]
                        try:
                            vecs = _get_embeddings(batch_texts)
                            for rid, vec in zip(batch_ids, vecs):
                                _conn.execute(
                                    "UPDATE rag_chunks SET embedding=? WHERE id=?",
                                    (json.dumps(vec), rid)
                                )
                            _conn.commit()
                            embedded_count += len(batch_texts)
                        except Exception as e:
                            _log.warning("RAG embedding 批次写入失败 (batch %d): %s", i // BATCH, e)
                    _conn.close()
                    _log.info("后台 RAG embedding 重建完成，成功 %d/%d", embedded_count, len(rows))
                    _refresh_vector_cache()  # 重建完成后刷新内存缓存
                except Exception as e:
                    _log.error("后台 RAG embedding 重建线程异常: %s", e, exc_info=True)
            threading.Thread(target=_rebuild_embeddings, daemon=True).start()
        elif raw_library:
            _log.info(
                "跳过后台 RAG embedding 重建；运行时使用关键词检索。"
                "如需重建，设置 ZRIC_AUTO_REBUILD_RAG_EMBEDDINGS=1 后重新载入剧本。"
            )

        conn.close()
        # 加载完成后立即刷新向量缓存（embedding 为空的 chunks 会在后台线程重建后再次刷新）
        _refresh_vector_cache()
        return {
            "status": "success",
            "message": (
                f"成功加载 {req.filename}（{load_type}，含 {len(raw_library)} 个RAG文档"
                f"，自动百科 {auto_lore_count} 条）"
            ),
            "auto_setup": {
                "rag_documents": len(raw_library),
                "auto_lore": auto_lore_count,
                "explicit_lore": len(explicit_lore_keywords) - auto_lore_count,
                "has_map": bool(map_path and os.path.exists(map_path)) or bool(config.get("map_rooms")),
            },
        }

    except Exception as e:
        _log.error("load_campaign 异常: %s", e, exc_info=True)
        raise fastapi.HTTPException(status_code=500, detail=f"加载失败: {str(e)}")

class ExportSaveRequest(BaseModel):
    save_name: str = ""

@app.post("/api/game/export")
def export_campaign(request: Request, req: ExportSaveRequest = ExportSaveRequest()):
    """导出存档到 campaigns/ 文件夹，支持自定义名。"""
    account = require_account_from_request(request)
    conn = get_db_connection()
    nodes = [dict(row) for row in conn.execute("SELECT * FROM nodes").fetchall()]
    options = [dict(row) for row in conn.execute("SELECT * FROM options").fetchall()]
    characters = [dict(row) for row in conn.execute("SELECT * FROM characters").fetchall()]
    lorebook = [dict(row) for row in conn.execute("SELECT * FROM lorebook").fetchall()]
    wv_row = conn.execute("SELECT value FROM system_state WHERE key = 'worldview'").fetchone()
    mem_row = conn.execute("SELECT value FROM system_state WHERE key = 'session_memory'").fetchone()
    triggers = [dict(row) for row in conn.execute("SELECT * FROM triggers").fetchall()]
    timelines = [dict(row) for row in conn.execute("SELECT * FROM timelines").fetchall()]
    world_entities = [dict(row) for row in conn.execute("SELECT * FROM world_entities").fetchall()]
    map_data = export_map_data(conn)

    # RAG 知识库：导出含 embedding
    rag_export = []
    rag_docs = conn.execute("SELECT * FROM rag_documents ORDER BY id").fetchall()
    for doc in rag_docs:
        chunks = conn.execute(
            "SELECT chunk_index, chunk_text, embedding FROM rag_chunks WHERE doc_id=? ORDER BY chunk_index", (doc["id"],)
        ).fetchall()
        rag_export.append({
            "title": doc["title"], "source": doc["source"],
            "chunks": [{"index": c["chunk_index"], "text": c["chunk_text"], "embedding": c["embedding"]} for c in chunks]
        })
    memory_l1 = [dict(row) for row in conn.execute("SELECT * FROM memory_l1").fetchall()]
    pending_effects = [dict(row) for row in conn.execute("SELECT * FROM pending_effects").fetchall()]
    npc_chat_logs = [dict(row) for row in conn.execute("SELECT npc_name, sender, message, created_at FROM npc_chat_logs ORDER BY id").fetchall()]
    conn.close()

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    staged_assets_dir = ""
    try:
        if req.save_name.strip():
            safe_name = _sanitize_campaign_name(req.save_name.strip())
            folder_name = safe_name or f"save_{timestamp}"
        else:
            folder_name = f"save_{timestamp}"
        folder_path = os.path.join(CAMPAIGNS_DIR, folder_name)
        staged_assets_dir = tempfile.mkdtemp(prefix="zric_export_assets_")
        staged_assets: dict[str, str] = {}
        for node in nodes:
            scene_image = (node.get("scene_image") or "").strip()
            prefix = "/api/campaign-assets/"
            if not scene_image.startswith(prefix):
                continue
            rel = scene_image[len(prefix):]
            parts = rel.split("/", 1)
            if len(parts) != 2:
                continue
            src_campaign = urllib.parse.unquote(parts[0])
            src_asset = urllib.parse.unquote(parts[1])
            dst_name = _sanitize_asset_name(os.path.basename(src_asset))
            if dst_name in staged_assets:
                continue
            try:
                src_path = _resolve_campaign_asset(src_campaign, src_asset)
                staged_path = os.path.join(staged_assets_dir, dst_name)
                shutil.copy2(src_path, staged_path)
                staged_assets[dst_name] = staged_path
            except (fastapi.HTTPException, OSError):
                continue
        if os.path.isdir(folder_path):
            existing_campaign = os.path.join(folder_path, "campaign.json")
            if not os.path.exists(existing_campaign) or not _is_legacy_campaign_file(existing_campaign):
                raise fastapi.HTTPException(status_code=400, detail="目标存档格式无效，已拒绝覆盖")
            _require_private_save_owner(folder_path, account)
            _cleanup_managed_campaign_folder(folder_path)
        os.makedirs(folder_path, exist_ok=True)
        export_nodes = [dict(n) for n in nodes]
        for node in export_nodes:
            scene_image = (node.get("scene_image") or "").strip()
            prefix = "/api/campaign-assets/"
            if not scene_image.startswith(prefix):
                continue
            rel = scene_image[len(prefix):]
            parts = rel.split("/", 1)
            if len(parts) != 2:
                continue
            asset_name = _sanitize_asset_name(os.path.basename(urllib.parse.unquote(parts[1])))
            node["scene_image"] = _campaign_asset_url(folder_name, asset_name)

        campaign_data = {
            "worldview": wv_row["value"] if wv_row else "",
            "session_memory": mem_row["value"] if mem_row else "",
            "characters": characters, "nodes": export_nodes, "options": options,
            "lorebook": lorebook, "triggers": triggers,
            "timelines": timelines, "world_entities": world_entities,
            "rag_library": rag_export,
            "memory_l1": memory_l1,
            "pending_effects": pending_effects,
            "npc_chat_logs": npc_chat_logs,
        }
        with open(os.path.join(folder_path, "campaign.json"), "w", encoding="utf-8") as f:
            json.dump(campaign_data, f, ensure_ascii=False, indent=4)
        with open(os.path.join(folder_path, "map.json"), "w", encoding="utf-8") as f:
            json.dump(map_data, f, ensure_ascii=False, indent=4)

        copied_assets = set()
        assets_dir = os.path.join(folder_path, "assets")
        for dst_name, src_path in staged_assets.items():
            os.makedirs(assets_dir, exist_ok=True)
            dst_path = os.path.join(assets_dir, dst_name)
            if os.path.realpath(src_path) != os.path.realpath(dst_path):
                shutil.copy2(src_path, dst_path)
            copied_assets.add(dst_name)

        if rag_export:
            kb_dir = os.path.join(folder_path, "knowledge")
            os.makedirs(kb_dir, exist_ok=True)
            for i, rag_item in enumerate(rag_export):
                safe_title = "".join(c for c in rag_item["title"] if c.isalnum() or c in " _-")[:40] or f"doc_{i}"
                full_text = "\n".join(ch["text"] for ch in rag_item.get("chunks", []))
                with open(os.path.join(kb_dir, f"{safe_title}.txt"), "w", encoding="utf-8") as f:
                    f.write(full_text)

        manifest = {
            "name": folder_name,
            "path": f"campaigns/{folder_name}",
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            **_account_owner_metadata(account),
            "node_count": len(nodes),
            "character_count": len(characters),
            "lore_count": len(lorebook),
            "rag_count": len(rag_export),
            "map_room_count": len(map_data.get("map_rooms") or []),
            "asset_count": len(copied_assets),
        }
        with open(os.path.join(folder_path, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        return {
            "status": "success",
            "folder": folder_name,
            "path": f"campaigns/{folder_name}",
            "download_url": f"/api/game/saves/{urllib.parse.quote(folder_name)}/download",
            "updated_at": _format_mtime(os.path.join(folder_path, "campaign.json")),
            "summary": manifest,
            "message": f"已导出到 campaigns/{folder_name}/"
        }
    except Exception as e:
        if isinstance(e, fastapi.HTTPException):
            raise
        raise fastapi.HTTPException(status_code=500, detail=str(e))
    finally:
        if staged_assets_dir and os.path.isdir(staged_assets_dir):
            shutil.rmtree(staged_assets_dir, ignore_errors=True)


@app.get("/api/game/saves/{campaign_name}/download")
def download_save_archive(campaign_name: str, request: Request):
    """将文件夹存档打包为 ZIP 下载。"""
    account = require_account_from_request(request)
    name, folder, _campaign_json = _resolve_campaign_folder_name(campaign_name)
    _require_private_save_owner(folder, account)
    tmp = tempfile.NamedTemporaryFile(prefix=f"zric_{_sanitize_asset_name(name, '.zip')}_", suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(folder):
                for filename in files:
                    path = os.path.join(root, filename)
                    rel = os.path.relpath(path, folder).replace("\\", "/")
                    zf.write(path, arcname=f"{name}/{rel}")
    except Exception as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise fastapi.HTTPException(status_code=500, detail=f"打包存档失败：{exc}") from exc
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=f"{name}.zip",
        background=BackgroundTask(lambda p: os.path.exists(p) and os.remove(p), tmp_path),
    )


@app.delete("/api/game/saves/{campaign_name}")
def delete_save_folder(campaign_name: str, request: Request):
    """删除 campaigns/<name> 文件夹存档。旧版根目录 JSON 不允许通过此接口删除。"""
    account = require_account_from_request(request)
    name, folder, _campaign_json = _resolve_campaign_folder_name(campaign_name)
    _require_private_save_owner(folder, account)
    try:
        shutil.rmtree(folder)
    except Exception as exc:
        raise fastapi.HTTPException(status_code=500, detail=f"删除存档失败：{exc}") from exc
    return {"status": "success", "name": name, "message": f"已删除存档：{name}"}

# ---------------------------------------------------------
# 【记忆系统】：已迁移至 memory.py（通过 app.include_router(memory_router) 自动注册）
# append_to_memory, _l1_append, _l1_get_working_context, _tl_append_memory 通过顶部 import 引入
# ---------------------------------------------------------

@app.get("/api/game/lorebook")
def get_lorebook():
    with safe_db() as conn:
        r = [dict(row) for row in conn.execute("SELECT * FROM lorebook").fetchall()]
    return {"status": "success", "lorebook": r}

@app.post("/api/game/lorebook")
def create_lore(req: LorebookRequest):
    with safe_db() as conn:
        conn.execute("INSERT INTO lorebook (keywords, content) VALUES (?, ?)", (req.keywords, req.content))
        conn.commit()
    return {"status": "success"}

@app.delete("/api/game/lorebook/{lore_id}")
def delete_lore(lore_id: int):
    with safe_db() as conn:
        conn.execute("DELETE FROM lorebook WHERE id = ?", (lore_id,))
        conn.commit()
    return {"status": "success"}

# ---------------------------------------------------------
# API 接口：词条库、世界观、节点 CRUD (原样保留)
# ---------------------------------------------------------
@app.get("/api/game/worldview")
def get_worldview():
    with safe_db() as conn:
        r = conn.execute("SELECT value FROM system_state WHERE key = 'worldview'").fetchone()
    return {"status": "success", "content": r["value"] if r else ""}

@app.put("/api/game/worldview")
def update_worldview(req: StringContentRequest):
    with safe_db() as conn:
        conn.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES ('worldview', ?)", (req.content,))
        conn.commit()
    return {"status": "success"}

@app.get("/api/game/stat-labels")
def get_stat_labels():
    """获取 HP/SAN 的自定义显示名。"""
    with safe_db() as conn:
        hp_row = conn.execute("SELECT value FROM system_state WHERE key='hp_label'").fetchone()
        san_row = conn.execute("SELECT value FROM system_state WHERE key='san_label'").fetchone()
    return {"hp_label": hp_row["value"] if hp_row else "HP", "san_label": san_row["value"] if san_row else "SAN"}

class StatLabelsRequest(BaseModel):
    hp_label: str = "HP"
    san_label: str = "SAN"

@app.put("/api/game/stat-labels")
def update_stat_labels(req: StatLabelsRequest):
    """更新 HP/SAN 显示名。"""
    with safe_db() as conn:
        conn.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES ('hp_label', ?)", (req.hp_label[:20],))
        conn.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES ('san_label', ?)", (req.san_label[:20],))
        conn.commit()
    return {"status": "success"}

@app.get("/api/game/state")
def get_game_state():
    with safe_db() as conn:
        n = [dict(row) for row in conn.execute("SELECT * FROM nodes").fetchall()]
        o = [dict(row) for row in conn.execute("SELECT * FROM options").fetchall()]
        c = [dict(row) for row in conn.execute("SELECT * FROM characters").fetchall()]
        wv_row = conn.execute("SELECT value FROM system_state WHERE key='worldview'").fetchone()
        worldview = wv_row["value"] if wv_row else ""
    for node in n:
        node["options"] = [opt for opt in o if opt["node_id"] == node["id"]]
    active_characters = [char for char in c if (char.get("status") or "active") != "hidden"]
    playable_characters = active_characters or c
    return {
        "status": "success",
        "nodes": n,
        "characters": c,
        "playable_characters": playable_characters,
        "all_characters": c,
        "worldview": worldview,
    }

@app.post("/api/game/character/{char_id}")
def update_character(char_id: int, req: CharUpdateRequest):
    with safe_db() as conn:
        if req.name is not None and req.name.strip():
            conn.execute("UPDATE characters SET name=?, hp=?, san=?, inventory=?, personality=?, status=? WHERE id=?",
                         (req.name.strip()[:50], req.hp, req.san, req.inventory, req.personality, req.status, char_id))
        else:
            conn.execute("UPDATE characters SET hp=?, san=?, inventory=?, personality=?, status=? WHERE id=?",
                         (req.hp, req.san, req.inventory, req.personality, req.status, char_id))
        conn.commit()
    return {"status": "success"}

@app.post("/api/game/node")
def create_node(req: NodeCreateRequest):
    with safe_db() as conn:
        c = conn.execute("INSERT INTO nodes (name, summary, content) VALUES (?,?,?)", (req.name, req.summary, req.content))
        i = c.lastrowid
        conn.commit()
    return {"status": "success", "id": i}

@app.put("/api/game/node/{node_id}")
def update_node(node_id: int, req: NodeUpdateRequest):
    with safe_db() as conn:
        conn.execute("UPDATE nodes SET name=?, summary=?, content=? WHERE id=?", (req.name, req.summary, req.content, node_id))
        conn.commit()
    return {"status": "success"}

@app.delete("/api/game/node/{node_id}")
def delete_node(node_id: int):
    with safe_db() as conn:
        conn.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        conn.execute("DELETE FROM options WHERE node_id=? OR next_node_id=?", (node_id, node_id))
        conn.commit()
    return {"status": "success"}

@app.post("/api/game/option")
def create_option(req: OptionCreateRequest):
    with safe_db() as conn:
        c = conn.execute("INSERT INTO options (node_id, text, next_node_id) VALUES (?,?,?)", (req.node_id, req.text, req.next_node_id))
        i = c.lastrowid
        conn.commit()
    return {"status": "success", "id": i}

@app.delete("/api/game/option/{option_id}")
def delete_option(option_id: int):
    with safe_db() as conn:
        conn.execute("DELETE FROM options WHERE id=?", (option_id,))
        conn.commit()
    return {"status": "success"}

# ---------------------------------------------------------
# 【新增核心】：打包提取四大维度的全知上下文
# ---------------------------------------------------------

def _build_persona_instruction() -> str:
    """
    将 persona_mode.json 的核心规则、MBTI 池、癖好池组装为可注入 system prompt 的文本块。
    若 persona_mode 未启用则返回空字符串，对已有逻辑零影响。
    """
    if not PERSONA_CONFIG:
        return ""
    lines = [PERSONA_CONFIG.get("core_rule", "")]
    npc_rule = PERSONA_CONFIG.get("npc_generation_rule", "")
    if npc_rule:
        lines.append(npc_rule)
    mbti_pool = PERSONA_CONFIG.get("mbti_pool", [])
    if mbti_pool:
        lines.append("【可用MBTI类型池】" + " | ".join(mbti_pool))
    quirk_pool = PERSONA_CONFIG.get("quirk_pool", [])
    if quirk_pool:
        lines.append("【可用行为癖好池】" + " | ".join(quirk_pool))
    return "\n".join(filter(None, lines))


# ---------------------------------------------------------
# 【世界实体】：已迁移至 entity.py
# _get_world_entities_text, _ai_extract_and_upsert_entities 通过顶部 import 引入
# ---------------------------------------------------------


# =============================================================
# 【RAG 知识库引擎】：已迁移至 rag.py
# =============================================================


# _get_map_context 和 _auto_place_room 已移至 map.py
# 通过顶部 from map import get_map_context as _get_map_context 引入
# ---------------------------------------------------------
# 【地图-AI 联动引擎】：处理 AI 返回的 map_actions
# ---------------------------------------------------------
def _process_map_actions(conn, parsed: dict, current_room_id: int | None,
                         timeline_id: int | None = None) -> dict:
    """
    处理 AI 推演返回的 map_actions 字段，执行地图状态变更。
    返回一个 map_result 字典，包含所有执行结果供前端渲染。

    处理顺序：unlock_edge → movement → new_room
    所有操作均有硬校验，AI 的幻觉不会破坏地图一致性。
    """
    result = {
        "moved_to": None,          # {"room_id": int, "label": str}
        "new_room": None,          # {"room_id": int, "label": str}
        "unlocked_edge": None,     # {"edge_id": int, "from": str, "to": str}
        "errors": [],              # 校验失败的信息（GM 可看到）
    }

    map_actions = parsed.get("map_actions") if isinstance(parsed, dict) else None
    if not map_actions or not isinstance(map_actions, dict):
        return result

    MAP_ID = 1  # 当前只支持单地图

    # ── 1. 解锁通道 ──────────────────────────────────────
    unlock = map_actions.get("unlock_edge")
    if unlock and isinstance(unlock, dict):
        from_label = str(unlock.get("from_label", "")).strip()
        to_label   = str(unlock.get("to_label", "")).strip()
        key_used   = str(unlock.get("key_used", "")).strip()

        if from_label and to_label:
            # 按标签模糊匹配房间对
            from_room = conn.execute(
                "SELECT id FROM map_rooms WHERE label LIKE ? LIMIT 1", (f"%{from_label[:20]}%",)
            ).fetchone()
            to_room = conn.execute(
                "SELECT id FROM map_rooms WHERE label LIKE ? LIMIT 1", (f"%{to_label[:20]}%",)
            ).fetchone()

            if from_room and to_room:
                edge = conn.execute(
                    "SELECT * FROM map_edges WHERE "
                    "((from_id=? AND to_id=?) OR (from_id=? AND to_id=?)) AND locked=1",
                    (from_room["id"], to_room["id"], to_room["id"], from_room["id"])
                ).fetchone()

                if edge:
                    # 硬校验：检查玩家背包是否有钥匙
                    if key_used:
                        all_inv = " ".join(
                            (c["inventory"] or "").lower()
                            for c in conn.execute("SELECT inventory FROM characters").fetchall()
                        )
                        if key_used.lower() in all_inv:
                            conn.execute("UPDATE map_edges SET locked=0 WHERE id=?", (edge["id"],))
                            result["unlocked_edge"] = {
                                "edge_id": edge["id"],
                                "from": from_label, "to": to_label,
                                "key_used": key_used
                            }
                            # 从背包移除钥匙
                            chars = conn.execute("SELECT * FROM characters").fetchall()
                            for c in chars:
                                inv = c["inventory"] or ""
                                if key_used.lower() in inv.lower():
                                    new_inv = ", ".join(
                                        p.strip() for p in inv.split(",")
                                        if key_used.lower() not in p.lower()
                                    ).strip(", ")
                                    conn.execute(
                                        "UPDATE characters SET inventory=? WHERE id=?",
                                        (new_inv, c["id"])
                                    )
                                    break
                        else:
                            result["errors"].append(f"解锁失败：背包中没有「{key_used}」")
                    else:
                        # 无需钥匙，直接解锁
                        conn.execute("UPDATE map_edges SET locked=0 WHERE id=?", (edge["id"],))
                        result["unlocked_edge"] = {
                            "edge_id": edge["id"],
                            "from": from_label, "to": to_label, "key_used": ""
                        }
                else:
                    result["errors"].append(f"解锁失败：{from_label}↔{to_label} 之间没有上锁的通道")

    # ── 2. 空间移动 ──────────────────────────────────────
    movement = map_actions.get("movement")
    if movement and isinstance(movement, dict) and current_room_id:
        target_label = str(movement.get("target_room_label", "")).strip()

        if target_label:
            # 按标签模糊匹配目标房间
            target_room = conn.execute(
                "SELECT id, label FROM map_rooms WHERE label LIKE ? LIMIT 1",
                (f"%{target_label[:20]}%",)
            ).fetchone()

            if target_room:
                # 硬校验：目标房间是否与当前房间相邻？
                is_adjacent = conn.execute(
                    "SELECT id, locked, key_item FROM map_edges WHERE "
                    "((from_id=? AND to_id=?) OR (from_id=? AND to_id=?))",
                    (current_room_id, target_room["id"],
                     target_room["id"], current_room_id)
                ).fetchone()

                if is_adjacent:
                    if is_adjacent["locked"]:
                        result["errors"].append(
                            f"移动失败：通往「{target_room['label']}」的通道被锁住"
                            f"（需要：{is_adjacent['key_item'] or '钥匙'}）"
                        )
                    else:
                        # 执行移动
                        conn.execute(
                            "UPDATE map_rooms SET state='explored' WHERE id=?",
                            (target_room["id"],)
                        )
                        if timeline_id:
                            conn.execute(
                                "UPDATE timelines SET current_room_id=? WHERE id=?",
                                (target_room["id"], timeline_id)
                            )
                        else:
                            conn.execute(
                                "INSERT OR REPLACE INTO system_state (key,value) "
                                "VALUES ('current_room_id',?)",
                                (str(target_room["id"]),)
                            )
                        result["moved_to"] = {
                            "room_id": target_room["id"],
                            "label": target_room["label"]
                        }
                else:
                    result["errors"].append(
                        f"移动失败：「{target_room['label']}」与当前房间不相邻"
                    )
            else:
                result["errors"].append(f"移动失败：找不到名为「{target_label}」的房间")

    # ── 3. 发现新房间（自动生长）──────────────────────────
    new_room_data = map_actions.get("new_room")
    if new_room_data and isinstance(new_room_data, dict) and current_room_id:
        nr_label = str(new_room_data.get("label", "")).strip()[:30]
        nr_desc  = str(new_room_data.get("description", "")).strip()[:200]

        if nr_label:
            # 检查是否已有同名房间
            existing = conn.execute(
                "SELECT id, label FROM map_rooms WHERE label LIKE ? LIMIT 1",
                (f"%{nr_label[:20]}%",)
            ).fetchone()

            if existing:
                result["errors"].append(f"新房间「{nr_label}」已存在（ID:{existing['id']}），跳过创建")
            else:
                # 调用 auto_place_room 在当前房间旁自动放置
                parent_room_id = current_room_id
                # 如果刚刚移动过，以移动后的房间为基点
                if result["moved_to"]:
                    parent_room_id = result["moved_to"]["room_id"]

                new_id = _auto_place_room(conn, MAP_ID, parent_room_id, nr_label, 0, nr_desc)
                if new_id:
                    result["new_room"] = {"room_id": new_id, "label": nr_label}
                else:
                    result["errors"].append(f"新房间「{nr_label}」生成失败：四周无空位")

    conn.commit()
    return result


def _get_current_room_id(conn, timeline_id: int | None = None) -> int | None:
    """获取当前房间 ID（支持时间线模式）"""
    if timeline_id:
        tl = conn.execute(
            "SELECT current_room_id FROM timelines WHERE id=?", (timeline_id,)
        ).fetchone()
        return tl["current_room_id"] if tl and tl["current_room_id"] else None
    else:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key='current_room_id'"
        ).fetchone()
        return int(row["value"]) if row and row["value"] else None



# get_system_context, expand_scene_text, generate_dynamic_options
# 已迁移至 agent.py（通过 app.include_router(agent_router) 自动注册）

# ---------------------------------------------------------
# API 接口：角色管理 (新增/删除)
# ---------------------------------------------------------
@app.post("/api/game/character")
def create_character(req: CharCreateRequest):
    with safe_db() as conn:
        _existing_char = conn.execute(
            "SELECT id FROM characters WHERE name=?", (req.name[:50],)
        ).fetchone()
        if _existing_char:
            conn.execute(
                "UPDATE characters SET role=?, hp=?, san=?, inventory=?, personality=?, status=? WHERE id=?",
                (req.role[:10], req.hp, req.san, req.inventory[:200], req.personality[:500], req.status, _existing_char["id"])
            )
            new_id = _existing_char["id"]
        else:
            new_id = conn.execute(
                "INSERT INTO characters (name, role, hp, san, inventory, personality, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (req.name[:50], req.role[:10], req.hp, req.san, req.inventory[:200], req.personality[:500], req.status)
            ).lastrowid
        # NPC 自动创建世界实体条目（使情绪状态机可用）
        if req.role.upper() == 'NPC':
            existing = conn.execute("SELECT id FROM world_entities WHERE name=?", (req.name[:50],)).fetchone()
            if not existing:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                initial_sd = json.dumps({
                    "desc": req.inventory[:200] if req.inventory else "",
                    "emotion": {"trust": 0, "fear": 0, "irritation": 0},
                    "breakpoint": {"threshold": 70, "trigger_field": "irritation", "reaction": ""},
                    "memory": []
                }, ensure_ascii=False)
                conn.execute(
                    "INSERT INTO world_entities (entity_type, name, location, status, state_desc, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    ("npc", req.name[:50], "", "active", initial_sd, now_str)
                )
        conn.commit()
    return {"status": "success", "id": new_id}

@app.delete("/api/game/character/{char_id}")
def delete_character(char_id: int):
    with safe_db() as conn:
        result = conn.execute("SELECT id FROM characters WHERE id = ?", (char_id,)).fetchone()
        if not result:
            raise fastapi.HTTPException(status_code=404, detail="角色不存在")
        conn.execute("DELETE FROM characters WHERE id = ?", (char_id,))
        conn.commit()
    return {"status": "success"}

@app.post("/api/ai/generate-npc")
def generate_npc(request: AutoNPCRequest):
    """根据当前剧情场景，AI自动生成一个合适的NPC并写入数据库"""
    conn = get_db_connection()
    worldview, party_status, relevant_lore, session_memory, l1_context, world_entities_text, rag_context, map_context = get_system_context(
        conn, request.scene_name, request.scene_content, request.player_action
    )
    conn.close()

    system_prompt = f"""你是一个跑团GM，需要根据当前剧情创建一个新NPC角色。
【全局世界观】\n{worldview}
【近期记忆】\n{session_memory}
【相关设定】\n{relevant_lore}
{f"【知识库检索结果（语义最相关的背景设定）】{chr(10)}{rag_context}" if rag_context else ""}
{f"【地图空间感知——AI必须遵守此空间结构推演】{chr(10)}{map_context}" if map_context else ""}
【当前队伍】\n{party_status}

请根据当前场景和玩家行动，生成一个符合剧情的NPC。
必须严格返回JSON格式：
{{"name": "NPC姓名（10字以内）", "role": "NPC", "hp": 50, "san": 60, "inventory": "持有物品或特征描述（30字以内）", "backstory": "简短背景描述（50字以内）"}}"""

    user_prompt = f"当前场景：{request.scene_name}\n场景内容：{request.scene_content}\n玩家行动：{request.player_action}\n请生成一个合适的NPC。"

    try:
        response = ai_provider.chat_completion(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.9,
            max_tokens=300,
            json_mode=True,
        )
        ai_result = response.choices[0].message.content.strip()
        npc_data = json_repair.loads(ai_result)

        name = npc_data.get("name", "神秘人")[:50]
        hp   = int(npc_data.get("hp", 50))
        san  = int(npc_data.get("san", 60))
        inv  = npc_data.get("inventory", "")[:200]
        backstory = npc_data.get("backstory", "")

        conn2 = get_db_connection()
        _existing = conn2.execute("SELECT id FROM characters WHERE name=?", (name,)).fetchone()
        if _existing:
            conn2.execute(
                "UPDATE characters SET hp=?, san=?, inventory=? WHERE id=?",
                (hp, san, inv, _existing["id"]))
            new_id = _existing["id"]
        else:
            new_id = conn2.execute(
                "INSERT INTO characters (name, role, hp, san, inventory) VALUES (?, 'NPC', ?, ?, ?)",
                (name, hp, san, inv)).lastrowid
        append_to_memory(conn2, f"NPC [{name}] 登场于 [{request.scene_name}]。背景：{backstory}")
        conn2.commit()
        conn2.close()

        return {"status": "success", "npc": {"id": new_id, "name": name, "role": "NPC", "hp": hp, "san": san, "inventory": inv, "backstory": backstory}}
    except Exception as e:
        return {"status": "error", "message": f"NPC生成失败: {str(e)}"}

# ---------------------------------------------------------
# API 接口：跳转场景时自动检测是否需要生成 NPC
# ---------------------------------------------------------
class CheckNPCRequest(BaseModel):
    scene_name: str
    scene_content: str

@app.post("/api/ai/check-npc")
def check_npc_on_enter(request: CheckNPCRequest):
    """
    跳转到新场景时调用。
    AI 轻量判断：该场景是否暗示一个新NPC应当出现？
    为节省 token，使用较小的 max_tokens，且加入防刷限制（同名NPC不重复创建）。
    """
    conn = get_db_connection()
    worldview, party_status, relevant_lore, session_memory, l1_context, world_entities_text, rag_context, map_context = get_system_context(
        conn, request.scene_name, request.scene_content
    )

    # 防重复：若当前角色表中已有同名NPC则跳过
    existing_names = [r["name"] for r in conn.execute("SELECT name FROM characters WHERE role='NPC'").fetchall()]

    system_prompt = f"""你是跑团GM助手。根据玩家刚进入的场景，判断是否需要立即生成一个新NPC登场。
【世界观】{worldview}
【近期记忆】{session_memory}
【相关设定】{relevant_lore}
【已有角色】{', '.join(existing_names) or '无'}

判断规则：
- 场景描述中明确提到某个人物、守卫、商人、敌人等具体角色 → 生成
- 场景只是环境描写（森林、废墟等）→ 不生成，返回 null
- 已有同名角色 → 不生成，返回 null

必须返回 JSON：
{{"npc": {{"name": "姓名", "role": "NPC", "hp": 50, "san": 60, "inventory": "特征", "backstory": "背景"}} | null}}"""

    user_prompt = f"场景名：{request.scene_name}\n场景内容：{request.scene_content[:300]}"

    try:
        response = ai_provider.chat_completion(
            [{"role": "system", "content": system_prompt},
             {"role": "user",   "content": user_prompt}],
            temperature=0.7,
            max_tokens=250,
            json_mode=True,
        )
        parsed = json_repair.loads(response.choices[0].message.content.strip())
        npc_data = parsed.get("npc")

        if not npc_data or not npc_data.get("name"):
            conn.close()
            return {"status": "success", "spawned_npc": None}

        npc_name = npc_data.get("name", "神秘人")[:50]

        # 二次防重复检查
        if npc_name in existing_names:
            conn.close()
            return {"status": "success", "spawned_npc": None}

        npc_hp   = int(npc_data.get("hp", 50))
        npc_san  = int(npc_data.get("san", 60))
        npc_inv  = npc_data.get("inventory", "")[:200]
        npc_back = npc_data.get("backstory", "")

        cur = conn.execute(
            "INSERT INTO characters (name, role, hp, san, inventory) VALUES (?, 'NPC', ?, ?, ?)",
            (npc_name, npc_hp, npc_san, npc_inv)
        )
        spawned_npc = {"id": cur.lastrowid, "name": npc_name, "role": "NPC",
                       "hp": npc_hp, "san": npc_san, "inventory": npc_inv, "backstory": npc_back}
        append_to_memory(conn, f"NPC [{npc_name}] 登场于新场景 [{request.scene_name}]。{npc_back}")
        conn.commit()
        conn.close()
        return {"status": "success", "spawned_npc": spawned_npc}

    except Exception as e:
        conn.close()
        return {"status": "success", "spawned_npc": None}  # 静默失败，不影响主流程


# ---------------------------------------------------------
# 【触发器系统】：已迁移至 trigger.py（通过 app.include_router(trigger_router) 自动注册）
# ---------------------------------------------------------

# ---------------------------------------------------------
# 【核心增强】：AI 图片生成（场景感知 + OpenAI 兼容端点）
# ---------------------------------------------------------
@app.post("/api/ai/generate-image")
def generate_image(request: ImageGenRequest):
    """
    图片生成（场景感知版）：
      Step 1 - 自动从当前场景提取上下文（场景名、正文、世界观、地图位置）
      Step 2 - 当前聊天模型将上下文压缩为一段精炼的画面描述（含构图、光影、氛围）
      Step 3 - prompt + 风格锚点发给 OPENAI_COMPAT_IMAGE_MODEL
    GM 可以不填 description，系统自动从场景生图；也可以填补充描述来引导画面重点。
    """
    # ── 风格方向锚点 ──
    style_map = {
        "fantasy":   "奇幻RPG插画，数字绘画",
        "horror":    "黑暗恐怖风格，哥特式",
        "realistic": "照片级写实，电影画面",
        "anime":     "日系动漫风格，吉卜力风",
        "sketch":    "铅笔素描风格，黑白线稿",
        "none":      "",
    }
    style_anchor = style_map.get(request.style, style_map["fantasy"])

    # ── Step 1：收集场景上下文 ──
    scene_name = request.scene_name
    scene_content = request.scene_content
    worldview_snippet = ""
    map_location = ""

    if request.scene_id:
        try:
            with safe_db() as conn:
                node = conn.execute("SELECT name, content FROM nodes WHERE id=?", (request.scene_id,)).fetchone()
                if node:
                    scene_name = scene_name or node["name"] or ""
                    scene_content = scene_content or node["content"] or ""

                # 世界观摘要（取前 150 字）
                wv = conn.execute("SELECT value FROM system_state WHERE key='worldview'").fetchone()
                if wv and wv["value"]:
                    worldview_snippet = wv["value"][:150]

                # 地图位置
                room_row = conn.execute("SELECT value FROM system_state WHERE key='current_room_id'").fetchone()
                if room_row and room_row["value"]:
                    room = conn.execute("SELECT label, description FROM map_rooms WHERE id=?",
                                        (int(room_row["value"]),)).fetchone()
                    if room:
                        map_location = f"{room['label']}（{room['description'] or ''}）".strip("（）")
        except Exception as e:
            _log.debug("生图场景上下文提取失败: %s", e)

    context_parts = []
    if scene_name:
        context_parts.append(f"场景名：{scene_name}")
    if scene_content:
        context_parts.append(f"场景描述：{scene_content[:500]}")
    if worldview_snippet:
        context_parts.append(f"世界观：{worldview_snippet}")
    if map_location:
        context_parts.append(f"地点：{map_location}")
    if request.description:
        context_parts.append(f"GM 补充指导：{request.description}")
    if not context_parts:
        context_parts.append(request.description or "一个神秘的奇幻场景")

    context_text = "\n".join(context_parts)
    try:
        resp = ai_provider.chat_completion(
            [
                {"role": "system", "content": (
                    "你是一个专业的 AI 视觉提示词工程师。"
                    "请将跑团场景描述扩写为一段高质量、细节丰富的画面描述，字数控制在 150-220 字以内。"
                    "强调人物、空间、材质、构图、光影和氛围。"
                    "不要输出解释、前缀、markdown 或引号内对话。"
                )},
                {"role": "user", "content": context_text},
            ],
            temperature=0.7,
            max_tokens=420,
        )
        scene_prompt = resp.choices[0].message.content.strip().strip('"').replace("\n", "，")
    except Exception as e:
        _log.warning("生图 prompt 生成失败，使用场景文本兜底: %s", e)
        scene_prompt = "，".join(
            p for p in [
                scene_name,
                scene_content[:300].replace("\n", "，") if scene_content else "",
                request.description,
            ] if p
        ) or "神秘的奇幻场景，戏剧性光影"

    full_prompt = f"{scene_prompt}，{style_anchor}，电影级光影，高质量" if style_anchor else scene_prompt
    model_override = (request.image_model or "").strip()
    if model_override == "default":
        model_override = ""

    try:
        resp = ai_provider.image_generate(full_prompt, model=model_override)
        img_item = resp.data[0] if getattr(resp, "data", None) else None

        if not img_item:
            raise ValueError("响应中未找到图片数据")
        if getattr(img_item, "url", None):
            image_url = img_item.url
        elif getattr(img_item, "b64_json", None):
            b64 = img_item.b64_json
            image_url = f"data:image/png;base64,{b64}"
        else:
            raise ValueError("响应中未找到图片数据")

        return {
            "status":    "success",
            "image_url": image_url,
            "prompt_used": full_prompt,
        }

    except Exception as e:
        return {
            "status":  "error",
            "message": f"OpenAI 兼容图像生成失败：{e}",
            "image_url": "",
            "prompt_used": full_prompt,
        }


# ---------------------------------------------------------
# API 接口：战报/小说导出
# ---------------------------------------------------------
@app.get("/api/debug/battle-report-state")
def debug_battle_report_state():
    conn = get_db_connection()
    pos_row   = conn.execute("SELECT value FROM system_state WHERE key='last_chronicle_position'").fetchone()
    pend_row  = conn.execute("SELECT value FROM system_state WHERE key='pending_regenerate'").fetchone()
    count_row = conn.execute("SELECT COUNT(*) as cnt, MIN(id) as min_id, MAX(id) as max_id FROM chronicle_log").fetchone()
    conn.close()
    return {
        "last_chronicle_position": pos_row["value"] if pos_row else None,
        "pending_regenerate":      pend_row["value"] if pend_row else None,
        "chronicle_log_count":     count_row["cnt"],
        "chronicle_log_id_range":  f"{count_row['min_id']} ~ {count_row['max_id']}",
    }

# ---------------------------------------------------------
@app.post("/api/ai/export-battle-report")
def export_battle_report():
    """
    增量式战报生成：
      - 输入源不再是 session_memory，而是 chronicle_log 中 id > last_chronicle_position 的所有新场景快照
        （每条包含场景名、content、深入调查后的 expanded_content、玩家动作）
      - 生成完成后将 last_chronicle_position 推进到本次最大 id
      - 每次另存为一个新的 .md 文件，不重复生成之前已写过的部分
    """
    conn = get_db_connection()
    pos_row = conn.execute(
        "SELECT value FROM system_state WHERE key='last_chronicle_position'"
    ).fetchone()
    last_pos = int(pos_row["value"]) if pos_row and pos_row["value"].isdigit() else 0

    rows = conn.execute(
        "SELECT * FROM chronicle_log WHERE id > ? ORDER BY id ASC", (last_pos,)
    ).fetchall()

    wv_row = conn.execute("SELECT value FROM system_state WHERE key='worldview'").fetchone()
    chars  = conn.execute("SELECT name, role FROM characters").fetchall()

    if not rows:
        regen_row = conn.execute(
            "SELECT value FROM system_state WHERE key='pending_regenerate'"
        ).fetchone()
        pending = regen_row and regen_row["value"] == "1"

        if not pending:
            # 第一次无新场景：告知用户，并立 flag 等待下次重新生成
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES ('pending_regenerate', '1')"
            )
            conn.commit()
            conn.close()
            return {
                "status": "success",
                "report": "（自上次生成以来暂无新场景，无需续写。）",
                "filename": "",
            }

        # 第二次（用户点重新生成后触发）：拉取上一章全部场景重新生成
        if last_pos > 0:
            rows = conn.execute(
                "SELECT * FROM chronicle_log WHERE id <= ? ORDER BY id ASC", (last_pos,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM chronicle_log ORDER BY id ASC"
            ).fetchall()
        if not rows:
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES ('pending_regenerate', '0')"
            )
            conn.commit()
            conn.close()
            return {
                "status": "success",
                "report": "（暂无可生成的场景记录。）",
                "filename": "",
            }
        is_regenerate = True
    else:
        is_regenerate = False
        # 有新场景时清除 flag
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, value) VALUES ('pending_regenerate', '0')"
        )

    worldview = wv_row["value"] if wv_row else ""
    char_list = "、".join([c["name"] for c in chars]) or "未知角色"

    # 拼接结构化输入：按时间顺序逐场景列出
    parts = []
    for i, r in enumerate(rows, 1):
        parts.append(f"## 场景 {i}：{r['scene_name'] or '未命名场景'}")
        if r["player_action"]:
            parts.append(f"**玩家动作**：{r['player_action']}")
        if r["scene_content"]:
            parts.append(f"**场景描写**：\n{r['scene_content']}")
        if r["expanded_content"]:
            parts.append(f"**深入调查**：\n{r['expanded_content']}")
        parts.append("")  # 空行分隔
    structured_input = "\n".join(parts)

    is_continuation = last_pos > 0 and not is_regenerate
    system_prompt = (
        "你是一位才华横溢的奇幻小说作者。下面提供按时间顺序排列的若干场景快照，"
        "每个场景包含玩家动作、场景描写、（可选的）深入调查细节。"
        "请将其润色为一段排版精美、文笔优美的奇幻战报/小说章节。要求："
        "- 使用 Markdown 格式，包含标题、段落分节，长度根据场景数量自然延伸，不要强行截断"
        "- 严格按场景给定的时间顺序展开，保留所有人名、地点、关键事件，不得改变剧情走向"
        "- 充分利用「深入调查」中的细节进行环境与氛围描写"
        "- 文笔生动、有代入感"
        + (
            "- 这是续章，请用一个承接上一章的简短开篇过渡几句，不要重复之前章节中已经叙述过的剧情"
            if is_continuation else
            "- 这是开篇第一章"
        )
        + "- 结尾加一句「（本章完）」"
        + f"【世界观背景】{worldview}"
        + f"【主要角色】{char_list}"
    )

    try:
        resp = ai_provider.chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": f"以下是本次需要润色的场景快照：\n\n{structured_input}"}
            ],
            temperature=0.8,
            max_tokens=8000,
        )
        report = resp.choices[0].message.content.strip()

        # 写入新文件
        report_dir = os.path.join(BASE_DIR, "battle_report")
        os.makedirs(report_dir, exist_ok=True)
        filename = f"battle_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        filepath = os.path.join(report_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report)

        # 生成成功后推进位置（重新生成时不推进，保留原位置便于再次重试）
        if not is_regenerate:
            new_pos = rows[-1]["id"]
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES ('last_chronicle_position', ?)",
                (str(new_pos),)
            )
        # 无论正向还是重新生成，成功后都清除 flag
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, value) VALUES ('pending_regenerate', '0')"
        )
        conn.commit()
        conn.close()
        return {"status": "success", "report": report, "filename": filename}
    except Exception as e:
        conn.close()
        return {"status": "error", "message": str(e), "report": ""}


# =============================================================
# 【多时间线 CRUD】：已迁移至 timeline.py
# _tl_append_memory 通过顶部 import 引入
# =============================================================

@app.post("/api/timelines/{tl_id}/dynamic-options")
def timeline_dynamic_options(tl_id: int, req: TimelineDynamicRequest):
    """
    时间线分支推演——委托给 agent.py 的统一推演引擎。
    时间线专属上下文在此构建，AI 调用和后处理由 agent 模块完成。
    """
    from .agent import _call_ai, _build_dynamic_system_prompt, _post_process_dynamic_result, _trim_prompt_context

    conn = get_db_connection()
    tl = conn.execute("SELECT * FROM timelines WHERE id=?", (tl_id,)).fetchone()
    if not tl:
        conn.close()
        return {"status": "error", "message": "时间线不存在"}

    tl_memory = tl["memory"] or ""
    tl_char_ids = [int(x) for x in tl["char_ids"].split(",") if x.strip().isdigit()]

    worldview_row = conn.execute("SELECT value FROM system_state WHERE key='worldview'").fetchone()
    worldview = worldview_row["value"] if worldview_row else ""

    combined = f"{req.scene_name} {req.content} {req.player_action}".lower()
    lores = conn.execute("SELECT keywords, content FROM lorebook").fetchall()
    injected = [f"[{l['keywords']}]: {l['content']}" for l in lores
                if any(k.strip().lower() in combined for k in l["keywords"].split(","))]
    relevant_lore = "\n".join(injected) or "无"

    tl_chars = conn.execute(
        f"SELECT * FROM characters WHERE id IN ({','.join('?'*len(tl_char_ids)) if tl_char_ids else '0'})",
        tl_char_ids if tl_char_ids else []
    ).fetchall()
    party_status = "\n".join(
        [f"- {c['name']} (HP:{c['hp']}, SAN:{c['san']}) | {c['inventory'] or '无'}" for c in tl_chars]
    ) or "（本时间线暂无绑定角色）"

    world_entities_text = _get_world_entities_text(conn, req.scene_name, req.content, req.player_action)
    token_policy = ai_provider.get_token_policy()
    rag_top_k = int(token_policy.get("rag_top_k") or 0)
    if rag_top_k > 0:
        rag_context = _rag_retrieve(conn, f"{req.scene_name} {req.content} {req.player_action}", top_k=rag_top_k)
    else:
        rag_context = _rag_retrieve(conn, f"{req.scene_name} {req.content} {req.player_action}")
    l1_context = _l1_get_working_context(conn, tl_id)

    try:
        tl_room_id = tl["current_room_id"] if tl["current_room_id"] else None
        map_context = _get_map_context(conn, tl_room_id)
    except Exception:
        map_context = ""

    worldview, party_status, relevant_lore, tl_memory, l1_context, world_entities_text, rag_context, map_context = _trim_prompt_context(
        worldview, party_status, relevant_lore, tl_memory,
        l1_context, world_entities_text, rag_context, map_context,
    )

    system_prompt = _build_dynamic_system_prompt(
        worldview, party_status, relevant_lore, tl_memory,
        l1_context, world_entities_text, rag_context, map_context,
        is_timeline=True, tl_label=tl["label"],
        action_type=req.action_type,
        gm_correction=""
    )
    user_prompt = f"场景：{req.scene_name}\n内容：{req.content}\n玩家动作：{req.player_action}\n先思考，再生成分支。"

    try:
        ai_result = _call_ai(system_prompt, user_prompt, temperature=0.8, max_tokens=2500, json_mode=True)
        if ai_result.startswith("```"):
            ai_result = ai_result.split("```")[1]
            if ai_result.startswith("json"): ai_result = ai_result[4:]
        parsed = json_repair.loads(ai_result.strip())

        new_options, spawned_npc, applied_changes, map_result, thought_process, entity_updates_text = \
            _post_process_dynamic_result(
                conn, parsed, req.scene_name, req.player_action,
                req.current_node_id, timeline_id=tl_id, tl_id_for_memory=tl_id,
                action_type=req.action_type
            )
        conn.close()
        return {"status": "success", "new_options": new_options,
                "spawned_npc": spawned_npc, "stat_changes": applied_changes,
                "thought_process": thought_process,
                "entity_updates": entity_updates_text,
                "map_result": map_result}
    except Exception as e:
        conn.close()
        return {"status": "error", "message": str(e), "new_options": []}


# =============================================================
# 【世界实体】：已迁移至 entity.py（通过 app.include_router(entity_router) 自动注册）
# =============================================================

# =============================================================
# 【时间线 CRUD/合并】：已迁移至 timeline.py（通过 app.include_router(timeline_router) 自动注册）
# =============================================================



# =============================================================
# 【RAG 知识库】：已迁移至 rag.py（通过 app.include_router(rag_router) 自动注册）
# =============================================================

# =============================================================
# 【地图系统】：已迁移至 map.py（通过 app.include_router(map_router) 自动注册）
# =============================================================

# ---------------------------------------------------------
# 【地图-AI 联动】：节点→房间绑定查询（供前端 jumpToNode 调用）
# ---------------------------------------------------------
@app.get("/api/map/room-by-node/{node_id}")
def get_room_by_node(node_id: int):
    """查询某个剧情节点绑定的地图房间。jumpToNode 时自动调用。"""
    with safe_db() as conn:
        room = conn.execute(
            "SELECT id, label FROM map_rooms WHERE node_id=? LIMIT 1", (node_id,)
        ).fetchone()
    if room:
        return {"status": "success", "room_id": room["id"], "label": room["label"]}
    return {"status": "success", "room_id": None}
# =============================================================

# ---------------------------------------------------------
# API 接口：投屏端同步（REST 轮询 + WebSocket 实时推送）
# ---------------------------------------------------------

# 【WebSocket 投屏】：连接管理 + 广播
import asyncio

_ws_clients: set[WebSocket] = set()

@app.websocket("/ws/player")
async def ws_player_endpoint(websocket: WebSocket):
    """
    投屏端 WebSocket 连接。
    连接建立后立即推送一次完整状态，之后 GM 每次 push_player_state 时自动广播。
    客户端只需监听 onmessage，不需要主动发消息。
    """
    await websocket.accept()
    _ws_clients.add(websocket)
    _log.info("投屏 WebSocket 连接建立（当前 %d 个客户端）", len(_ws_clients))
    try:
        # 连接建立后立即推送当前状态
        state = _build_player_state_snapshot()
        await websocket.send_json(state)
        # 保持连接，等待客户端断开（客户端不需要发数据，但 WebSocket 需要 recv 循环保持心跳）
        while True:
            # 等待客户端消息（通常不会收到，但需要这个循环来检测断开）
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # 心跳：每 30 秒发一个 ping（客户端无需处理）
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        _log.debug("投屏 WebSocket 异常断开: %s", e)
    finally:
        _ws_clients.discard(websocket)
        _log.info("投屏 WebSocket 连接断开（剩余 %d 个客户端）", len(_ws_clients))


def _build_player_state_snapshot() -> dict:
    """构建投屏端需要的完整状态快照（场景+角色+BGM）。"""
    with safe_db() as conn:
        def _get(k):
            r = conn.execute("SELECT value FROM system_state WHERE key=?", (k,)).fetchone()
            return r["value"] if r else ""

        scene_id_raw = _get("player_current_scene_id")
        scene_id = int(scene_id_raw) if scene_id_raw and scene_id_raw.isdigit() else None

        # 如果有当前场景，附带场景详情
        current_scene = None
        if scene_id:
            node = conn.execute("SELECT * FROM nodes WHERE id=?", (scene_id,)).fetchone()
            if node:
                options = [dict(o) for o in conn.execute(
                    "SELECT * FROM options WHERE node_id=?", (scene_id,)
                ).fetchall()]
                current_scene = {**dict(node), "options": options}

        characters = [dict(c) for c in conn.execute("SELECT * FROM characters").fetchall()]

    return {
        "type": "state_update",
        "current_scene_id": scene_id,
        "current_scene": current_scene,
        "scene_image":   _get("player_scene_image") if scene_id else "",
        "scene_prompt":  _get("player_scene_prompt") if scene_id else "",
        "scene_ai_text": _get("player_scene_ai_text") if scene_id else "",
        "bgm_url": _get("player_bgm_url") if scene_id else "",
        "bgm_name": _get("player_bgm_name") if scene_id else "",
        "characters": characters,
    }


async def _broadcast_player_state():
    """向所有已连接的投屏端 WebSocket 客户端广播当前状态。"""
    if not _ws_clients:
        return
    state = _build_player_state_snapshot()
    dead_clients = set()
    for ws in _ws_clients.copy():
        try:
            await ws.send_json(state)
        except Exception:
            dead_clients.add(ws)
    # 清理已断开的连接
    _ws_clients.difference_update(dead_clients)


# ---------------------------------------------------------
# 场景跳转日志：记录玩家通过预设选项进入节点的行为
# ---------------------------------------------------------
class SceneVisitRequest(BaseModel):
    node_id:      int
    node_name:    str
    option_text:  str = ""   # 玩家点击的选项文本，为空时表示直接跳转

@app.post("/api/game/log-scene-visit")
def log_scene_visit(req: SceneVisitRequest):
    # 只有通过预设选项跳转才记录，直接跳转不写记忆流
    if not req.option_text.strip():
        return {"status": "skipped"}
    conn = get_db_connection()
    log = f"玩家选择「{req.option_text}」→ 进入场景【{req.node_name}】"
    append_to_memory(conn, log)

    # 同步写入战报史册：抓取当前场景 content/expanded_content 作为快照
    try:
        node = conn.execute(
            "SELECT content, expanded_content FROM nodes WHERE id=?", (req.node_id,)
        ).fetchone()
        scene_content   = (node["content"] if node else "") or ""
        expanded_snap   = (node["expanded_content"] if node else "") or ""
        conn.execute(
            "INSERT INTO chronicle_log (scene_id, scene_name, scene_content, "
            "expanded_content, player_action) VALUES (?,?,?,?,?)",
            (req.node_id, req.node_name, scene_content, expanded_snap, req.option_text)
        )
        conn.commit()
    except Exception as e:
        _log.warning("写入 chronicle_log 失败: %s", e)

    conn.close()
    return {"status": "success"}


class PlayerStateRequest(BaseModel):
    current_scene_id: int = 0
    scene_image:      str = ""
    scene_prompt:     str = ""
    scene_ai_text:    str = ""
    bgm_url:          str = ""
    bgm_name:         str = ""

class GmControlEventRequest(BaseModel):
    content: str
    kind: str = "ai"
    sender_name: str = "GM"
    current_scene_id: int = 0
    sync_player_state: bool = True
    record_to_memory: bool = True

class CheckpointRequest(BaseModel):
    from_node_id: int
    label: str = ""

@app.post("/api/game/checkpoint")
def create_checkpoint(req: CheckpointRequest):
    """保存当前游戏状态快照，用于返回上一回合。"""
    with safe_db() as conn:
        def rows(sql, *args):
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        def max_id(table):
            try:
                return conn.execute(f"SELECT COALESCE(MAX(id),0) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                return 0
        state_keys = [
            "session_memory",
            "player_current_scene_id", "player_scene_image", "player_scene_prompt",
            "player_scene_ai_text", "player_bgm_url", "player_bgm_name",
            "current_room_id",
        ]
        system_state = {
            key: (row["value"] if row else "")
            for key in state_keys
            for row in [conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()]
        }

        snap = {
            "characters":     rows("SELECT * FROM characters"),
            "world_entities": rows("SELECT * FROM world_entities"),
            "memory_l1_max_id": (conn.execute("SELECT COALESCE(MAX(id),0) FROM memory_l1").fetchone()[0]),
            "node_max_id": max_id("nodes"),
            "option_max_id": max_id("options"),
            "chronicle_log_max_id": max_id("chronicle_log"),
            "timelines":      rows("SELECT * FROM timelines"),
            "node_expanded":  {str(r["id"]): r["expanded_content"]
                               for r in conn.execute("SELECT id,expanded_content FROM nodes").fetchall()},
            "triggers":       rows("SELECT id,fired,fire_count,last_fired_at FROM triggers"),
            "pending_effects": rows("SELECT * FROM pending_effects"),
            "session_memory": system_state.get("session_memory", ""),
            "system_state": system_state,
            "map_rooms_state": {str(r["id"]): r["state"]
                                for r in conn.execute("SELECT id,state FROM map_rooms").fetchall()},
        }

        conn.execute(
            "INSERT INTO game_checkpoints (from_node_id, snapshot, label) VALUES (?,?,?)",
            (req.from_node_id, json.dumps(snap, ensure_ascii=False), req.label[:80])
        )
        # 保留最近10条，自动淘汰最旧的
        conn.execute("""
            DELETE FROM game_checkpoints
            WHERE id NOT IN (SELECT id FROM game_checkpoints ORDER BY id DESC LIMIT 10)
        """)
        remaining = conn.execute("SELECT COUNT(*) FROM game_checkpoints").fetchone()[0]
        conn.commit()
    return {"status": "success", "remaining": remaining}

@app.get("/api/game/checkpoints")
def list_checkpoints():
    with safe_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM game_checkpoints").fetchone()[0]
    return {"status": "success", "count": count}


@app.post("/api/game/rollback")
def rollback_checkpoint():
    """恢复最近一次快照，返回应导航到的节点ID。快照消费后自动删除。"""
    with safe_db() as conn:
        row = conn.execute(
            "SELECT id, from_node_id, snapshot FROM game_checkpoints ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"status": "error", "message": "没有可用的快照"}

        cp_id       = row["id"]
        from_node   = row["from_node_id"]
        snap        = json.loads(row["snapshot"])
        node_max_id = int(snap.get("node_max_id") or 0)
        option_max_id = int(snap.get("option_max_id") or 0)
        chronicle_log_max_id = int(snap.get("chronicle_log_max_id") or 0)

        if node_max_id:
            conn.execute("DELETE FROM options WHERE node_id > ? OR next_node_id > ?",
                         (node_max_id, node_max_id))
            conn.execute("DELETE FROM pending_effects WHERE node_id > ?", (node_max_id,))
            conn.execute("DELETE FROM nodes WHERE id > ?", (node_max_id,))
        if option_max_id:
            conn.execute("DELETE FROM options WHERE id > ?", (option_max_id,))
        if chronicle_log_max_id:
            conn.execute("DELETE FROM chronicle_log WHERE id > ?", (chronicle_log_max_id,))

        # ── characters ──────────────────────────────────────────
        conn.execute("DELETE FROM characters")
        for c in snap["characters"]:
            conn.execute(
                "INSERT INTO characters (id,name,role,hp,san,inventory,personality,status) VALUES (?,?,?,?,?,?,?,?)",
                (c["id"], c["name"], c["role"], c["hp"], c["san"],
                 c.get("inventory",""), c.get("personality",""), c.get("status","active"))
            )

        # ── world_entities ───────────────────────────────────────
        conn.execute("DELETE FROM world_entities")
        for e in snap["world_entities"]:
            conn.execute(
                "INSERT INTO world_entities (id,entity_type,name,location,status,last_seen_by,state_desc,updated_at,room_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (e["id"], e["entity_type"], e["name"], e["location"],
                 e["status"], e["last_seen_by"], e["state_desc"],
                 e["updated_at"], e.get("room_id"))
            )

        # ── memory_l1（删除快照之后新增的条目）──────────────────
        conn.execute("DELETE FROM memory_l1 WHERE id > ?", (snap["memory_l1_max_id"],))

        # ── timelines ────────────────────────────────────────────
        conn.execute("DELETE FROM timelines")
        for tl in snap["timelines"]:
            conn.execute(
                "INSERT INTO timelines (id,label,color,current_node_id,current_room_id,memory,char_ids,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tl["id"], tl["label"], tl["color"],
                 tl["current_node_id"], tl.get("current_room_id"),
                 tl["memory"], tl["char_ids"], tl["status"], tl["created_at"])
            )

        # ── nodes.expanded_content ───────────────────────────────
        for node_id_str, content in snap["node_expanded"].items():
            conn.execute("UPDATE nodes SET expanded_content=? WHERE id=?",
                         (content, int(node_id_str)))

        # ── triggers 状态字段 ─────────────────────────────────────
        for tr in snap["triggers"]:
            conn.execute(
                "UPDATE triggers SET fired=?,fire_count=?,last_fired_at=? WHERE id=?",
                (tr["fired"], tr.get("fire_count",0), tr.get("last_fired_at"), tr["id"])
            )

        # ── pending_effects ──────────────────────────────────────
        conn.execute("DELETE FROM pending_effects")
        for pe in snap["pending_effects"]:
            conn.execute(
                "INSERT INTO pending_effects (id,node_id,payload,created_at) VALUES (?,?,?,?)",
                (pe["id"], pe["node_id"], pe["payload"], pe["created_at"])
            )

        # ── session_memory ───────────────────────────────────────
        system_state = snap.get("system_state") or {"session_memory": snap.get("session_memory", "")}
        for key, value in system_state.items():
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key,value) VALUES (?,?)",
                (key, value or "")
            )

        # ── map_rooms.state ──────────────────────────────────────
        for room_id_str, state in snap["map_rooms_state"].items():
            conn.execute("UPDATE map_rooms SET state=? WHERE id=?",
                         (state, int(room_id_str)))

        # 快照消费后删除
        conn.execute("DELETE FROM game_checkpoints WHERE id=?", (cp_id,))
        # 返回剩余快照数量，供前端更新按钮状态
        remaining = conn.execute("SELECT COUNT(*) FROM game_checkpoints").fetchone()[0]
        conn.commit()

    return {"status": "success", "restored_node_id": from_node, "remaining": remaining}

@app.get("/api/player/state")
def get_player_state():
    """REST 轮询接口（向后兼容，投屏端已升级为 WebSocket 则不再需要此接口）。"""
    with safe_db() as conn:
        def _get(k):
            r = conn.execute("SELECT value FROM system_state WHERE key=?", (k,)).fetchone()
            return r["value"] if r else ""
        scene_id_raw = _get("player_current_scene_id")
        result = {
            "current_scene_id": int(scene_id_raw) if scene_id_raw and scene_id_raw.isdigit() else None,
            "scene_image":      _get("player_scene_image"),
            "scene_prompt":     _get("player_scene_prompt"),
            "scene_ai_text":    _get("player_scene_ai_text"),
            "bgm_url":          _get("player_bgm_url"),
            "bgm_name":         _get("player_bgm_name"),
        }
    return result

@app.post("/api/player/state")
async def push_player_state(req: PlayerStateRequest):
    """GM 推送投屏状态。写入数据库后立即通过 WebSocket 广播给所有投屏端。"""
    with safe_db() as conn:
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_current_scene_id',?)", (str(req.current_scene_id),))
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_scene_image',?)",      (req.scene_image,))
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_scene_prompt',?)",     (req.scene_prompt,))
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_scene_ai_text',?)",    (req.scene_ai_text,))
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_bgm_url',?)",          (req.bgm_url,))
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_bgm_name',?)",         (req.bgm_name,))
        conn.commit()
    # WebSocket 广播（异步，不阻塞响应）
    await _broadcast_player_state()
    return {"status": "success"}

@app.post("/api/game/gm-event")
async def publish_gm_control_event(req: GmControlEventRequest):
    """GM 手动接管：把旁白/裁定同步到当前玩家视图，并写入主线记忆。"""
    content = (req.content or "").strip()
    if not content:
        raise fastapi.HTTPException(status_code=400, detail="GM 事件内容不能为空")
    kind = req.kind if req.kind in {"ai", "state", "system", "dice"} else "ai"
    sender = (req.sender_name or "GM").strip()[:40]
    with safe_db() as conn:
        def _get(k):
            row = conn.execute("SELECT value FROM system_state WHERE key=?", (k,)).fetchone()
            return row["value"] if row else ""

        scene_id = req.current_scene_id or 0
        if not scene_id:
            raw_scene_id = _get("player_current_scene_id")
            scene_id = int(raw_scene_id) if raw_scene_id and raw_scene_id.isdigit() else 0
        if req.sync_player_state:
            scene_image = _get("player_scene_image")
            if scene_id and not scene_image:
                node = conn.execute("SELECT scene_image FROM nodes WHERE id=?", (scene_id,)).fetchone()
                scene_image = (node["scene_image"] if node else "") or ""
            conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_current_scene_id',?)", (str(scene_id),))
            conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_scene_image',?)", (scene_image,))
            conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_scene_ai_text',?)", (content,))
        if req.record_to_memory:
            append_to_memory(conn, f"[{sender}手动接管/{kind}] {content[:1000]}")
        else:
            conn.commit()
    if req.sync_player_state:
        await _broadcast_player_state()
    return {"status": "success", "event": {"kind": kind, "sender_name": sender, "content": content}}

@app.get("/api/campaign-assets/{campaign_name}/{asset_name:path}")
def serve_campaign_asset(campaign_name: str, asset_name: str):
    """安全访问导入剧本的图片资源。"""
    if not campaign_name or "/" in campaign_name or "\\" in campaign_name:
        raise fastapi.HTTPException(status_code=400, detail="非法剧本名")
    ext = os.path.splitext(asset_name)[1].lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        raise fastapi.HTTPException(status_code=404, detail="资源不存在")
    path = _resolve_campaign_asset(campaign_name, asset_name)
    return FileResponse(path)

# ---------------------------------------------------------
# 【模块化】：静态前端路由必须最后挂载，避免捕获 API 路径
# ---------------------------------------------------------
app.include_router(static_router)

# ---------------------------------------------------------
# 程序入口
# ---------------------------------------------------------
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    
    _log.info("=====================================================")
    _log.info("-Z.R.I.C 零界核心- 正在启动...")
    _log.info("当前工作目录: %s", BASE_DIR)
    _log.info("=====================================================")
    _log.info("请不要关闭此窗口！关闭窗口将停止游戏引擎。")
    
    def auto_open_browser():
        time.sleep(2)
        _log.info("正在自动为您打开浏览器...")
        webbrowser.open("http://127.0.0.1:8000")

    threading.Thread(target=auto_open_browser, daemon=True).start()

    # 启动服务器 (使用 127.0.0.1 避免某些网络策略拦截)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
