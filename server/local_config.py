"""Local runtime configuration stored beside provider profiles."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


CONFIG_FILE_NAME = "config.json"
LEGACY_CONFIG_FILE_NAME = "openai_providers.json"
DEFAULT_CONFIG_VERSION = 1
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 8000
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_IMAGE_SIZE = "1024x1024"
DEFAULT_PROVIDER_ID = "default"
DEFAULT_PROVIDER_NAME = "Default endpoint"
DEFAULT_TOKEN_POLICY_MODE = "full"
DEFAULT_AI_CACHE_ENABLED = True
DEFAULT_AI_CACHE_MAX_ENTRIES = 512

DEFAULT_PROVIDER_RECORD = {
    "id": DEFAULT_PROVIDER_ID,
    "name": DEFAULT_PROVIDER_NAME,
    "api_key": "",
    "base_url": DEFAULT_BASE_URL,
    "chat_model": "",
    "embedding_model": "",
    "image_model": "",
    "image_size": DEFAULT_IMAGE_SIZE,
}

DEFAULT_LOCAL_CONFIG = {
    "config_version": DEFAULT_CONFIG_VERSION,
    "server": {
        "host": DEFAULT_SERVER_HOST,
        "port": DEFAULT_SERVER_PORT,
    },
    "active_provider": DEFAULT_PROVIDER_ID,
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


def legacy_provider_store_path(base_dir: str | None = None) -> Path:
    return Path(base_dir or resolve_base_dir()) / LEGACY_CONFIG_FILE_NAME


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
    record.update({
        "id": str(raw.get("id") or fallback_id).strip() or fallback_id,
        "name": str(raw.get("name") or raw.get("id") or fallback_id).strip() or fallback_id,
        "api_key": str(raw.get("api_key") or "").strip(),
        "base_url": str(raw.get("base_url") or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
        "chat_model": str(raw.get("chat_model") or "").strip(),
        "embedding_model": str(raw.get("embedding_model") or "").strip(),
        "image_model": str(raw.get("image_model") or "").strip(),
        "image_size": str(raw.get("image_size") or DEFAULT_IMAGE_SIZE).strip() or DEFAULT_IMAGE_SIZE,
    })
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


def normalize_local_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    cfg = _clone(DEFAULT_LOCAL_CONFIG)
    for key, value in raw.items():
        if key not in cfg:
            cfg[key] = value

    server = raw.get("server") if isinstance(raw.get("server"), dict) else {}
    cfg["server"] = {
        "host": str(server.get("host") or DEFAULT_SERVER_HOST).strip() or DEFAULT_SERVER_HOST,
        "port": normalize_server_port(server.get("port")),
    }
    cfg["config_version"] = raw.get("config_version") or DEFAULT_CONFIG_VERSION
    cfg["active_provider"] = str(raw.get("active_provider") or DEFAULT_PROVIDER_ID).strip() or DEFAULT_PROVIDER_ID
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
    providers = json.dumps(cfg["providers"], ensure_ascii=False, indent=4).replace("\n", "\n  ")
    return (
        "{\n"
        "  // Z.R.I.C 本地配置。这里可能包含 API Key；不要提交到 Git。\n"
        f"  \"config_version\": {json.dumps(cfg['config_version'], ensure_ascii=False)},\n\n"
        "  // 本地 FastAPI 服务监听配置；修改 host/port 后需要重新启动。\n"
        f"  \"server\": {_json_block(cfg['server'])},\n\n"
        "  // 当前使用的 OpenAI 兼容供应商 id，必须对应 providers[].id。\n"
        f"  \"active_provider\": {json.dumps(cfg['active_provider'], ensure_ascii=False)},\n\n"
        "  // 游玩过程的 Token 策略：full / balanced / frugal。\n"
        f"  \"token_policy_mode\": {json.dumps(cfg['token_policy_mode'], ensure_ascii=False)},\n\n"
        "  // 非流式 AI 响应缓存设置；max_entries 范围 16-10000。\n"
        f"  \"ai_cache\": {_json_block(cfg['ai_cache'])},\n\n"
        "  // OpenAI 兼容供应商列表。api_key 留空时该供应商不可用；base_url 需包含 /v1。\n"
        "  // chat_model / embedding_model / image_model 留空时需在前端选择或手动填入。\n"
        f"  \"providers\": {providers}\n"
        "}\n"
    )


def read_local_config(base_dir: str | None = None) -> dict[str, Any]:
    path = provider_store_path(base_dir)
    legacy_path = legacy_provider_store_path(base_dir)
    try:
        if path.exists():
            return normalize_local_config(_read_config_file(path))
        if legacy_path.exists():
            data = normalize_local_config(_read_config_file(legacy_path))
            write_local_config(data, base_dir)
            return data
    except Exception:
        return normalize_local_config()
    return normalize_local_config()


def write_local_config(data: dict[str, Any], base_dir: str | None = None) -> None:
    path = provider_store_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_config_jsonc_text(data), encoding="utf-8")


def ensure_local_config(base_dir: str | None = None) -> Path:
    path = provider_store_path(base_dir)
    if not path.exists():
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
    env_host = os.environ.get("ZRIC_HOST", "").strip()
    if env_host:
        return env_host
    data = read_local_config(base_dir)
    server = data.get("server") if isinstance(data.get("server"), dict) else {}
    return str(server.get("host") or DEFAULT_SERVER_HOST).strip() or DEFAULT_SERVER_HOST


def get_server_port(base_dir: str | None = None) -> int:
    env_port = os.environ.get("ZRIC_PORT", "").strip()
    if env_port:
        return normalize_server_port(env_port)
    return get_saved_server_port(base_dir)


def get_saved_server_port(base_dir: str | None = None) -> int:
    data = read_local_config(base_dir)
    server = data.get("server") if isinstance(data.get("server"), dict) else {}
    return normalize_server_port(server.get("port"))


def get_server_bind(base_dir: str | None = None) -> tuple[str, int]:
    return get_server_host(base_dir), get_server_port(base_dir)


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
