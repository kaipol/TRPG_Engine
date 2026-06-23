"""Local runtime configuration stored beside provider profiles."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


CONFIG_FILE_NAME = "config.json"
DEFAULT_CONFIG_VERSION = 1
DEFAULT_SERVER_HOST = "0.0.0.0"
DEFAULT_SERVER_PORT = 8000
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_IMAGE_SIZE = "1024x1024"
DEFAULT_PROVIDER_ID = "default"
DEFAULT_PROVIDER_NAME = "Default endpoint"
DEFAULT_TOKEN_POLICY_MODE = "full"
DEFAULT_AI_CACHE_ENABLED = True
DEFAULT_AI_CACHE_MAX_ENTRIES = 512
DEFAULT_AUTO_OPEN_BROWSER = True
DEFAULT_ALLOW_FILE_ORIGIN = False
DEFAULT_CAMPAIGN_IMPORT_AI_TIMEOUT_SECONDS = 45.0
DEFAULT_CAMPAIGN_IMPORT_MINERU_COMMAND = "mineru-open-api"
DEFAULT_CAMPAIGN_IMPORT_MINERU_TIMEOUT_SECONDS = 900.0
DEFAULT_CAMPAIGN_IMPORT_MINERU_MODEL = ""
DEFAULT_CAMPAIGN_IMPORT_MINERU_LANGUAGE = "ch"
DEFAULT_CAMPAIGN_IMPORT_MINERU_PAGES = ""
DEFAULT_CAMPAIGN_IMPORT_MINERU_USE_EXTRACT = True
DEFAULT_CAMPAIGN_IMPORT_MINERU_TOKEN = ""
DEFAULT_CAMPAIGN_IMPORT_MINERU_BASE_URL = ""
DEFAULT_CAMPAIGN_IMPORT_MINERU_SOURCE = ""
DEFAULT_CAMPAIGN_IMPORT_MINERU_VERBOSE = False
DEFAULT_RAG_AUTO_REBUILD_EMBEDDINGS = False
DEFAULT_MAX_SCENARIO_UPLOAD_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_SCENARIO_CHARS = 400000
DEFAULT_MAX_SCENARIO_CHUNKS = 800
DEFAULT_MAX_MAP_UPLOAD_BYTES = 12 * 1024 * 1024
DEFAULT_MAX_ROOM_PLAYERS = 24
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

DEFAULT_PROVIDER_RECORD = {
    "id": DEFAULT_PROVIDER_ID,
    "name": DEFAULT_PROVIDER_NAME,
    "api_key": "",
    "base_url": DEFAULT_BASE_URL,
    "chat_model": "",
    "embedding_model": "",
    "image_model": "",
    "campaign_model": "",
    "image_size": DEFAULT_IMAGE_SIZE,
}

DEFAULT_LOCAL_CONFIG = {
    "config_version": DEFAULT_CONFIG_VERSION,
    "server": {
        "host": DEFAULT_SERVER_HOST,
        "port": DEFAULT_SERVER_PORT,
        "auto_open_browser": DEFAULT_AUTO_OPEN_BROWSER,
        "allowed_origins": [],
        "allow_file_origin": DEFAULT_ALLOW_FILE_ORIGIN,
    },
    "campaign_import": {
        "ai_timeout_seconds": DEFAULT_CAMPAIGN_IMPORT_AI_TIMEOUT_SECONDS,
        "mineru_command": DEFAULT_CAMPAIGN_IMPORT_MINERU_COMMAND,
        "mineru_timeout_seconds": DEFAULT_CAMPAIGN_IMPORT_MINERU_TIMEOUT_SECONDS,
        "mineru_model": DEFAULT_CAMPAIGN_IMPORT_MINERU_MODEL,
        "mineru_language": DEFAULT_CAMPAIGN_IMPORT_MINERU_LANGUAGE,
        "mineru_pages": DEFAULT_CAMPAIGN_IMPORT_MINERU_PAGES,
        "mineru_use_extract": DEFAULT_CAMPAIGN_IMPORT_MINERU_USE_EXTRACT,
        "mineru_token": DEFAULT_CAMPAIGN_IMPORT_MINERU_TOKEN,
        "mineru_base_url": DEFAULT_CAMPAIGN_IMPORT_MINERU_BASE_URL,
        "mineru_source": DEFAULT_CAMPAIGN_IMPORT_MINERU_SOURCE,
        "mineru_verbose": DEFAULT_CAMPAIGN_IMPORT_MINERU_VERBOSE,
    },
    "rag": {
        "auto_rebuild_embeddings": DEFAULT_RAG_AUTO_REBUILD_EMBEDDINGS,
    },
    "multiplayer": {
        "max_scenario_upload_bytes": DEFAULT_MAX_SCENARIO_UPLOAD_BYTES,
        "max_scenario_chars": DEFAULT_MAX_SCENARIO_CHARS,
        "max_scenario_chunks": DEFAULT_MAX_SCENARIO_CHUNKS,
        "max_map_upload_bytes": DEFAULT_MAX_MAP_UPLOAD_BYTES,
        "max_room_players": DEFAULT_MAX_ROOM_PLAYERS,
    },
    "active_provider": DEFAULT_PROVIDER_ID,
    "active_provider_by_account": {},
    "token_policy_mode": DEFAULT_TOKEN_POLICY_MODE,
    "ai_cache": {
        "enabled": DEFAULT_AI_CACHE_ENABLED,
        "max_entries": DEFAULT_AI_CACHE_MAX_ENTRIES,
    },
    "providers": [DEFAULT_PROVIDER_RECORD],
}


def resolve_base_dir() -> str:
    override = os.environ.get("ZRIC_BASE_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def provider_store_path(base_dir: str | None = None) -> Path:
    return Path(base_dir or resolve_base_dir()) / CONFIG_FILE_NAME


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _strip_json_comments(text: str) -> str:
    result: list[str] = []
    in_string = False
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            result.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            result.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] in "\r\n":
                    result.append(text[i])
                i += 1
            i += 2
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def _read_config_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    data = json.loads(_strip_json_comments(text))
    return data if isinstance(data, dict) else {}


def _normalize_provider_record(raw: Any, fallback_id: str = DEFAULT_PROVIDER_ID) -> dict[str, Any]:
    record = _clone(DEFAULT_PROVIDER_RECORD)
    if not isinstance(raw, dict):
        raw = {}
    owner_account_id = raw.get("owner_account_id")
    try:
        owner_account_id = int(owner_account_id) if owner_account_id not in (None, "") else None
    except (TypeError, ValueError):
        owner_account_id = None
    record.update({
        "id": str(raw.get("id") or fallback_id).strip() or fallback_id,
        "name": str(raw.get("name") or raw.get("id") or fallback_id).strip() or fallback_id,
        "api_key": str(raw.get("api_key") or "").strip(),
        "base_url": str(raw.get("base_url") or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
        "chat_model": str(raw.get("chat_model") or "").strip(),
        "embedding_model": str(raw.get("embedding_model") or "").strip(),
        "image_model": str(raw.get("image_model") or "").strip(),
        "campaign_model": str(raw.get("campaign_model") or "").strip(),
        "image_size": str(raw.get("image_size") or DEFAULT_IMAGE_SIZE).strip() or DEFAULT_IMAGE_SIZE,
    })
    if owner_account_id is not None:
        record["owner_account_id"] = owner_account_id
        record["owner_username"] = str(raw.get("owner_username") or "").strip()
        record["owner_display_name"] = str(raw.get("owner_display_name") or "").strip()
    return record


def normalize_ai_cache_settings(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    try:
        max_entries = int(data.get("max_entries", DEFAULT_AI_CACHE_MAX_ENTRIES))
    except (TypeError, ValueError):
        max_entries = DEFAULT_AI_CACHE_MAX_ENTRIES
    return {
        "enabled": data.get("enabled", DEFAULT_AI_CACHE_ENABLED) is not False,
        "max_entries": max(16, min(10000, max_entries)),
    }


def normalize_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


def normalize_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def normalize_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def normalize_origin_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_values = value
    elif isinstance(value, str):
        raw_values = value.split(",")
    else:
        raw_values = []
    origins: list[str] = []
    for item in raw_values:
        origin = str(item or "").strip()
        if origin and origin not in origins:
            origins.append(origin)
    return origins


def normalize_server_settings(raw: Any) -> dict[str, Any]:
    server = raw if isinstance(raw, dict) else {}
    return {
        "host": str(server.get("host") or DEFAULT_SERVER_HOST).strip() or DEFAULT_SERVER_HOST,
        "port": normalize_server_port(server.get("port")),
        "auto_open_browser": normalize_bool(server.get("auto_open_browser"), DEFAULT_AUTO_OPEN_BROWSER),
        "allowed_origins": normalize_origin_list(server.get("allowed_origins")),
        "allow_file_origin": normalize_bool(server.get("allow_file_origin"), DEFAULT_ALLOW_FILE_ORIGIN),
    }


def normalize_campaign_import_settings(raw: Any) -> dict[str, Any]:
    settings = raw if isinstance(raw, dict) else {}
    return {
        "ai_timeout_seconds": normalize_float(
            settings.get("ai_timeout_seconds"),
            DEFAULT_CAMPAIGN_IMPORT_AI_TIMEOUT_SECONDS,
            minimum=5.0,
            maximum=300.0,
        ),
        "mineru_command": str(settings.get("mineru_command") or DEFAULT_CAMPAIGN_IMPORT_MINERU_COMMAND).strip()
        or DEFAULT_CAMPAIGN_IMPORT_MINERU_COMMAND,
        "mineru_timeout_seconds": normalize_float(
            settings.get("mineru_timeout_seconds"),
            DEFAULT_CAMPAIGN_IMPORT_MINERU_TIMEOUT_SECONDS,
            minimum=30.0,
            maximum=1200.0,
        ),
        "mineru_model": str(settings.get("mineru_model") or DEFAULT_CAMPAIGN_IMPORT_MINERU_MODEL).strip(),
        "mineru_language": str(settings.get("mineru_language") or DEFAULT_CAMPAIGN_IMPORT_MINERU_LANGUAGE).strip()
        or DEFAULT_CAMPAIGN_IMPORT_MINERU_LANGUAGE,
        "mineru_pages": str(settings.get("mineru_pages") or DEFAULT_CAMPAIGN_IMPORT_MINERU_PAGES).strip(),
        "mineru_use_extract": normalize_bool(
            settings.get("mineru_use_extract"),
            DEFAULT_CAMPAIGN_IMPORT_MINERU_USE_EXTRACT,
        ),
        "mineru_token": str(settings.get("mineru_token") or DEFAULT_CAMPAIGN_IMPORT_MINERU_TOKEN).strip(),
        "mineru_base_url": str(settings.get("mineru_base_url") or DEFAULT_CAMPAIGN_IMPORT_MINERU_BASE_URL).strip(),
        "mineru_source": str(settings.get("mineru_source") or DEFAULT_CAMPAIGN_IMPORT_MINERU_SOURCE).strip(),
        "mineru_verbose": normalize_bool(
            settings.get("mineru_verbose"),
            DEFAULT_CAMPAIGN_IMPORT_MINERU_VERBOSE,
        ),
    }


def normalize_rag_settings(raw: Any) -> dict[str, Any]:
    settings = raw if isinstance(raw, dict) else {}
    return {
        "auto_rebuild_embeddings": normalize_bool(
            settings.get("auto_rebuild_embeddings"),
            DEFAULT_RAG_AUTO_REBUILD_EMBEDDINGS,
        ),
    }


def normalize_multiplayer_settings(raw: Any) -> dict[str, Any]:
    settings = raw if isinstance(raw, dict) else {}
    return {
        "max_scenario_upload_bytes": normalize_int(
            settings.get("max_scenario_upload_bytes"),
            DEFAULT_MAX_SCENARIO_UPLOAD_BYTES,
            minimum=1024,
            maximum=100 * 1024 * 1024,
        ),
        "max_scenario_chars": normalize_int(
            settings.get("max_scenario_chars"),
            DEFAULT_MAX_SCENARIO_CHARS,
            minimum=1000,
            maximum=5_000_000,
        ),
        "max_scenario_chunks": normalize_int(
            settings.get("max_scenario_chunks"),
            DEFAULT_MAX_SCENARIO_CHUNKS,
            minimum=1,
            maximum=10000,
        ),
        "max_map_upload_bytes": normalize_int(
            settings.get("max_map_upload_bytes"),
            DEFAULT_MAX_MAP_UPLOAD_BYTES,
            minimum=1024,
            maximum=100 * 1024 * 1024,
        ),
        "max_room_players": normalize_int(
            settings.get("max_room_players"),
            DEFAULT_MAX_ROOM_PLAYERS,
            minimum=1,
            maximum=200,
        ),
    }


def normalize_local_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    cfg = _clone(DEFAULT_LOCAL_CONFIG)
    for key, value in raw.items():
        if key == "security":
            continue
        if key not in cfg:
            cfg[key] = value

    cfg["server"] = normalize_server_settings(raw.get("server"))
    cfg["campaign_import"] = normalize_campaign_import_settings(raw.get("campaign_import"))
    cfg["rag"] = normalize_rag_settings(raw.get("rag"))
    cfg["multiplayer"] = normalize_multiplayer_settings(raw.get("multiplayer"))
    cfg["config_version"] = raw.get("config_version") or DEFAULT_CONFIG_VERSION
    cfg["active_provider"] = str(raw.get("active_provider") or DEFAULT_PROVIDER_ID).strip() or DEFAULT_PROVIDER_ID
    active_by_account = raw.get("active_provider_by_account")
    cfg["active_provider_by_account"] = active_by_account if isinstance(active_by_account, dict) else {}
    cfg["token_policy_mode"] = str(raw.get("token_policy_mode") or DEFAULT_TOKEN_POLICY_MODE).strip() or DEFAULT_TOKEN_POLICY_MODE
    cfg["ai_cache"] = normalize_ai_cache_settings(raw.get("ai_cache"))

    providers = raw.get("providers") if isinstance(raw.get("providers"), list) else []
    normalized_providers = [
        _normalize_provider_record(item, f"provider-{idx + 1}")
        for idx, item in enumerate(providers)
        if isinstance(item, dict)
    ]
    cfg["providers"] = normalized_providers or [_clone(DEFAULT_PROVIDER_RECORD)]
    if not any(p.get("id") == cfg["active_provider"] for p in cfg["providers"]):
        cfg["active_provider"] = str(cfg["providers"][0].get("id") or DEFAULT_PROVIDER_ID)
    return cfg


def _json_block(value: Any, indent: int = 2) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=indent)
    return text.replace("\n", "\n" + " " * indent)


def _config_jsonc_text(data: dict[str, Any]) -> str:
    cfg = normalize_local_config(data)
    campaign_import = _campaign_import_config_for_write(cfg["campaign_import"])
    providers = json.dumps(cfg["providers"], ensure_ascii=False, indent=4).replace("\n", "\n  ")
    return (
        "{\n"
        "  // Z.R.I.C 本地配置。这里可能包含 API Key；不要提交到 Git。\n"
        f"  \"config_version\": {json.dumps(cfg['config_version'], ensure_ascii=False)},\n\n"
        "  // FastAPI 服务运行配置；修改后需要重新启动。\n"
        "  // allowed_origins 为空时自动允许当前端口的 localhost / 127.0.0.1。\n"
        f"  \"server\": {_json_block(cfg['server'])},\n\n"
        "  // 剧本导入配置。MinerU 会先 OCR/提取图片，然后调用剧本解析模型转换为可玩剧本。\n"
        "  // mineru_model 留空时使用官方 CLI 自动模型选择；mineru_token/base_url/source/verbose 仅在显式配置时写入。\n"
        "  // mineru_token 属于敏感凭据，优先建议用 mineru-open-api auth 或 MINERU_TOKEN；如填写到此文件，不要提交到 Git。\n"
        f"  \"campaign_import\": {_json_block(campaign_import)},\n\n"
        "  // RAG 运行配置。auto_rebuild_embeddings=true 会在载入剧本时后台补建 embedding。\n"
        f"  \"rag\": {_json_block(cfg['rag'])},\n\n"
        "  // 多人联机运行限制，单位为字节/字符/数量。\n"
        f"  \"multiplayer\": {_json_block(cfg['multiplayer'])},\n\n"
        "  // 以下字段由网页配置面板维护，通常不需要手动修改。\n"
        "  // 当前使用的 OpenAI 兼容供应商 id，必须对应 providers[].id。\n"
        f"  \"active_provider\": {json.dumps(cfg['active_provider'], ensure_ascii=False)},\n\n"
        "  // 各登录账号自己的活动供应商 id。普通账号只能看到和修改自己创建的供应商。\n"
        f"  \"active_provider_by_account\": {_json_block(cfg['active_provider_by_account'])},\n\n"
        "  // 游玩过程的 Token 策略：full / balanced / frugal。\n"
        f"  \"token_policy_mode\": {json.dumps(cfg['token_policy_mode'], ensure_ascii=False)},\n\n"
        "  // 非流式 AI 响应缓存设置；max_entries 范围 16-10000。\n"
        f"  \"ai_cache\": {_json_block(cfg['ai_cache'])},\n\n"
        "  // OpenAI 兼容供应商列表。api_key 留空时该供应商不可用；base_url 需包含 /v1。\n"
        "  // chat_model / embedding_model / image_model / campaign_model 留空时需在前端选择或手动填入。\n"
        f"  \"providers\": {providers}\n"
        "}\n"
    )


def read_local_config(base_dir: str | None = None) -> dict[str, Any]:
    path = provider_store_path(base_dir)
    try:
        if path.exists():
            return normalize_local_config(_read_config_file(path))
    except Exception:
        return normalize_local_config()
    return normalize_local_config()


def write_local_config(data: dict[str, Any], base_dir: str | None = None) -> None:
    path = provider_store_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_config_jsonc_text(data), encoding="utf-8")


def _campaign_import_config_for_write(settings: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ai_timeout_seconds": settings["ai_timeout_seconds"],
        "mineru_command": settings["mineru_command"],
        "mineru_timeout_seconds": settings["mineru_timeout_seconds"],
        "mineru_model": settings["mineru_model"],
        "mineru_language": settings["mineru_language"],
        "mineru_pages": settings["mineru_pages"],
    }

    token = str(settings.get("mineru_token") or "").strip()
    if token:
        result["mineru_token"] = token

    base_url = str(settings.get("mineru_base_url") or "").strip()
    if base_url:
        result["mineru_base_url"] = base_url

    source = str(settings.get("mineru_source") or "").strip()
    if source:
        result["mineru_source"] = source

    if settings.get("mineru_verbose"):
        result["mineru_verbose"] = True

    if not settings.get("mineru_use_extract", DEFAULT_CAMPAIGN_IMPORT_MINERU_USE_EXTRACT):
        result["mineru_use_extract"] = False

    return result


def _campaign_import_needs_rewrite(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return True

    required_keys = (
        "ai_timeout_seconds",
        "mineru_command",
        "mineru_timeout_seconds",
        "mineru_model",
        "mineru_language",
        "mineru_pages",
    )
    if any(key not in raw for key in required_keys):
        return True

    if "use_ai_conversion" in raw:
        return True

    if "mineru_token" in raw and not str(raw.get("mineru_token") or "").strip():
        return True
    if "mineru_base_url" in raw and not str(raw.get("mineru_base_url") or "").strip():
        return True
    if "mineru_source" in raw and not str(raw.get("mineru_source") or "").strip():
        return True
    if "mineru_verbose" in raw and not normalize_bool(raw.get("mineru_verbose"), DEFAULT_CAMPAIGN_IMPORT_MINERU_VERBOSE):
        return True
    if "mineru_use_extract" in raw and normalize_bool(
        raw.get("mineru_use_extract"),
        DEFAULT_CAMPAIGN_IMPORT_MINERU_USE_EXTRACT,
    ) == DEFAULT_CAMPAIGN_IMPORT_MINERU_USE_EXTRACT:
        return True

    return False


def ensure_local_config(base_dir: str | None = None) -> Path:
    path = provider_store_path(base_dir)
    if not path.exists():
        write_local_config(read_local_config(base_dir), base_dir)
    else:
        try:
            raw = _read_config_file(path)
            required_sections = ("server", "campaign_import", "rag", "multiplayer")
            missing_section = any(not isinstance(raw.get(section), dict) for section in required_sections)
            has_security_section = "security" in raw
            missing_server_keys = (
                isinstance(raw.get("server"), dict)
                and any(key not in raw["server"] for key in ("auto_open_browser", "allowed_origins", "allow_file_origin"))
            )
            rewrite_campaign_import = _campaign_import_needs_rewrite(raw.get("campaign_import"))
            providers = raw.get("providers") if isinstance(raw.get("providers"), list) else []
            missing_provider_keys = any(isinstance(p, dict) and "campaign_model" not in p for p in providers)
            if missing_section or has_security_section or missing_server_keys or rewrite_campaign_import or missing_provider_keys:
                write_local_config(normalize_local_config(raw), base_dir)
        except Exception:
            write_local_config(read_local_config(base_dir), base_dir)
    return path


def normalize_server_port(value: Any, default: int = DEFAULT_SERVER_PORT) -> int:
    try:
        return validate_server_port(value)
    except ValueError:
        return default


def validate_server_port(value: Any) -> int:
    raw = str(value).strip()
    if not raw:
        raise ValueError("端口必须是 1-65535 的整数")
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("端口必须是 1-65535 的整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("端口必须是 1-65535 的整数")
    return port


def get_server_host(base_dir: str | None = None) -> str:
    saved_host = get_server_settings(base_dir)["host"]
    if os.name != "nt" and saved_host in {"127.0.0.1", "localhost"}:
        return DEFAULT_SERVER_HOST
    return saved_host


def get_server_port(base_dir: str | None = None) -> int:
    return get_server_settings(base_dir)["port"]


def get_saved_server_port(base_dir: str | None = None) -> int:
    data = read_local_config(base_dir)
    server = data.get("server") if isinstance(data.get("server"), dict) else {}
    return normalize_server_port(server.get("port"))


def get_server_bind(base_dir: str | None = None) -> tuple[str, int]:
    return get_server_host(base_dir), get_server_port(base_dir)


def get_server_settings(base_dir: str | None = None) -> dict[str, Any]:
    data = read_local_config(base_dir)
    settings = normalize_server_settings(data.get("server"))

    env_host = os.environ.get("ZRIC_HOST", "").strip()
    if env_host:
        settings["host"] = env_host

    env_port = os.environ.get("ZRIC_PORT", "").strip()
    if env_port:
        settings["port"] = normalize_server_port(env_port)

    env_no_browser = os.environ.get("ZRIC_NO_BROWSER", "").strip()
    if env_no_browser:
        settings["auto_open_browser"] = not normalize_bool(env_no_browser, False)

    env_origins = os.environ.get("ALLOWED_ORIGINS", "").strip()
    if env_origins:
        settings["allowed_origins"] = normalize_origin_list(env_origins)

    env_allow_file_origin = os.environ.get("ZRIC_ALLOW_FILE_ORIGIN", "").strip()
    if env_allow_file_origin:
        settings["allow_file_origin"] = normalize_bool(env_allow_file_origin, settings["allow_file_origin"])

    return settings


def get_allowed_origins(base_dir: str | None = None, server_port: int | None = None) -> list[str]:
    settings = get_server_settings(base_dir)
    origins = normalize_origin_list(settings.get("allowed_origins"))
    if not origins:
        port = server_port or int(settings.get("port") or DEFAULT_SERVER_PORT)
        origins = [
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        ]
    if settings.get("allow_file_origin") and "null" not in origins:
        origins.append("null")
    return origins


def get_auto_open_browser(base_dir: str | None = None) -> bool:
    return bool(get_server_settings(base_dir).get("auto_open_browser"))


def get_campaign_import_settings(base_dir: str | None = None) -> dict[str, Any]:
    data = read_local_config(base_dir)
    settings = normalize_campaign_import_settings(data.get("campaign_import"))

    env_timeout = os.environ.get("ZRIC_CAMPAIGN_IMPORT_AI_TIMEOUT", "").strip()
    if env_timeout:
        settings["ai_timeout_seconds"] = normalize_float(
            env_timeout,
            settings["ai_timeout_seconds"],
            minimum=5.0,
            maximum=300.0,
        )
    env_mineru_command = os.environ.get("ZRIC_MINERU_COMMAND", "").strip()
    if env_mineru_command:
        settings["mineru_command"] = env_mineru_command

    env_mineru_timeout = os.environ.get("ZRIC_MINERU_TIMEOUT", "").strip()
    if env_mineru_timeout:
        settings["mineru_timeout_seconds"] = normalize_float(
            env_mineru_timeout,
            settings["mineru_timeout_seconds"],
            minimum=30.0,
            maximum=1200.0,
        )

    env_mineru_model = os.environ.get("ZRIC_MINERU_MODEL", "").strip()
    if env_mineru_model:
        settings["mineru_model"] = env_mineru_model

    env_mineru_language = os.environ.get("ZRIC_MINERU_LANGUAGE", "").strip()
    if env_mineru_language:
        settings["mineru_language"] = env_mineru_language

    env_mineru_pages = os.environ.get("ZRIC_MINERU_PAGES", "").strip()
    if env_mineru_pages:
        settings["mineru_pages"] = env_mineru_pages

    env_mineru_use_extract = os.environ.get("ZRIC_MINERU_USE_EXTRACT", "").strip()
    if env_mineru_use_extract:
        settings["mineru_use_extract"] = normalize_bool(env_mineru_use_extract, settings["mineru_use_extract"])

    env_mineru_token = os.environ.get("ZRIC_MINERU_TOKEN", "").strip()
    if env_mineru_token:
        settings["mineru_token"] = env_mineru_token

    env_mineru_base_url = os.environ.get("ZRIC_MINERU_BASE_URL", "").strip()
    if env_mineru_base_url:
        settings["mineru_base_url"] = env_mineru_base_url

    env_mineru_source = os.environ.get("ZRIC_MINERU_SOURCE", "").strip()
    if env_mineru_source:
        settings["mineru_source"] = env_mineru_source

    env_mineru_verbose = os.environ.get("ZRIC_MINERU_VERBOSE", "").strip()
    if env_mineru_verbose:
        settings["mineru_verbose"] = normalize_bool(env_mineru_verbose, settings["mineru_verbose"])
    return settings


def get_rag_settings(base_dir: str | None = None) -> dict[str, Any]:
    data = read_local_config(base_dir)
    settings = normalize_rag_settings(data.get("rag"))
    env_auto_rebuild = os.environ.get("ZRIC_AUTO_REBUILD_RAG_EMBEDDINGS", "").strip()
    if env_auto_rebuild:
        settings["auto_rebuild_embeddings"] = normalize_bool(
            env_auto_rebuild,
            settings["auto_rebuild_embeddings"],
        )
    return settings


def get_multiplayer_settings(base_dir: str | None = None) -> dict[str, Any]:
    data = read_local_config(base_dir)
    settings = normalize_multiplayer_settings(data.get("multiplayer"))
    env_overrides = {
        "max_scenario_upload_bytes": ("ZRIC_MAX_SCENARIO_UPLOAD_BYTES", 1024, 100 * 1024 * 1024),
        "max_scenario_chars": ("ZRIC_MAX_SCENARIO_CHARS", 1000, 5_000_000),
        "max_scenario_chunks": ("ZRIC_MAX_SCENARIO_CHUNKS", 1, 10000),
        "max_map_upload_bytes": ("ZRIC_MAX_MAP_UPLOAD_BYTES", 1024, 100 * 1024 * 1024),
        "max_room_players": ("ZRIC_MAX_ROOM_PLAYERS", 1, 200),
    }
    for key, (env_name, minimum, maximum) in env_overrides.items():
        raw = os.environ.get(env_name, "").strip()
        if raw:
            settings[key] = normalize_int(raw, settings[key], minimum=minimum, maximum=maximum)
    return settings


def set_server_port(port: Any, base_dir: str | None = None) -> int:
    normalized = validate_server_port(port)
    data = read_local_config(base_dir)
    server = data.get("server") if isinstance(data.get("server"), dict) else {}
    server["host"] = str(server.get("host") or DEFAULT_SERVER_HOST).strip() or DEFAULT_SERVER_HOST
    server["port"] = normalized
    data["server"] = server
    write_local_config(data, base_dir)
    return normalized


def get_ai_cache_settings(base_dir: str | None = None) -> dict[str, Any]:
    data = read_local_config(base_dir)
    return normalize_ai_cache_settings(data.get("ai_cache"))


def set_ai_cache_settings(
    *,
    enabled: bool | None = None,
    max_entries: int | None = None,
    base_dir: str | None = None,
) -> dict[str, Any]:
    data = read_local_config(base_dir)
    settings = normalize_ai_cache_settings(data.get("ai_cache"))
    if enabled is not None:
        settings["enabled"] = bool(enabled)
    if max_entries is not None:
        settings["max_entries"] = max(16, min(10000, int(max_entries)))
    data["ai_cache"] = settings
    write_local_config(data, base_dir)
    return settings
