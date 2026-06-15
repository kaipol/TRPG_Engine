"""
Unified OpenAI-compatible AI provider helpers.

All AI capabilities in the app route through the active provider profile.
Profiles are stored locally so the user can switch between multiple
OpenAI-compatible vendors without rewriting application code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

from . import local_config


DEFAULT_BASE_URL = local_config.DEFAULT_BASE_URL
DEFAULT_IMAGE_SIZE = local_config.DEFAULT_IMAGE_SIZE
DEFAULT_PROVIDER_ID = local_config.DEFAULT_PROVIDER_ID
DEFAULT_PROVIDER_NAME = local_config.DEFAULT_PROVIDER_NAME
DEFAULT_TOKEN_POLICY_MODE = local_config.DEFAULT_TOKEN_POLICY_MODE
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

MODEL_CAPABILITY_FIELDS = {
    "chat": "chat_model",
    "embedding": "embedding_model",
    "image": "image_model",
    "campaign": "campaign_model",
}

TOKEN_POLICY_MODES = {
    "full": {
        "label": "完整",
        "worldview_chars": 0,
        "party_status_chars": 0,
        "relevant_lore_chars": 0,
        "session_memory_chars": 0,
        "l1_context_chars": 0,
        "world_entities_chars": 0,
        "rag_context_chars": 0,
        "rag_top_k": 0,
        "map_context_chars": 0,
        "npc_persona_chars": 0,
        "npc_history_chars": 0,
        "multiplayer_context_chars": 0,
        "output_token_cap": 0,
        "json_output_token_cap": 0,
    },
    "balanced": {
        "label": "平衡",
        "worldview_chars": 5200,
        "party_status_chars": 2400,
        "relevant_lore_chars": 2200,
        "session_memory_chars": 3600,
        "l1_context_chars": 1800,
        "world_entities_chars": 2600,
        "rag_context_chars": 3200,
        "rag_top_k": 4,
        "map_context_chars": 2600,
        "npc_persona_chars": 1600,
        "npc_history_chars": 900,
        "multiplayer_context_chars": 800,
        "output_token_cap": 1000,
        "json_output_token_cap": 1200,
    },
    "frugal": {
        "label": "极省",
        "worldview_chars": 3000,
        "party_status_chars": 1600,
        "relevant_lore_chars": 1200,
        "session_memory_chars": 1800,
        "l1_context_chars": 900,
        "world_entities_chars": 1400,
        "rag_context_chars": 1800,
        "rag_top_k": 2,
        "map_context_chars": 1400,
        "npc_persona_chars": 900,
        "npc_history_chars": 500,
        "multiplayer_context_chars": 500,
        "output_token_cap": 700,
        "json_output_token_cap": 950,
    },
}

_provider_store_base_dir: str | None = None
_active_models: dict[str, str] = {}


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    provider_id: str
    provider_name: str
    api_key: str
    base_url: str
    chat_model: str
    embedding_model: str
    image_model: str
    campaign_model: str
    image_size: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url)


def configure_provider_store(path: str) -> None:
    global _provider_store_base_dir
    config_path = Path(path)
    if config_path.name in {local_config.CONFIG_FILE_NAME, local_config.LEGACY_CONFIG_FILE_NAME}:
        _provider_store_base_dir = str(config_path.parent)
    else:
        _provider_store_base_dir = str(config_path)
    local_config.ensure_local_config(_provider_store_base_dir)


def _default_record() -> dict:
    return {
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


def _read_store() -> dict:
    return local_config.read_local_config(_provider_store_base_dir)


def _write_store(data: dict) -> None:
    local_config.write_local_config(data, _provider_store_base_dir)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "").strip().lower()).strip("-")
    return slug[:48] or DEFAULT_PROVIDER_ID


def _normalize_record(raw: dict, fallback_id: str = DEFAULT_PROVIDER_ID) -> dict:
    provider_id = _slug(str(raw.get("id") or fallback_id))
    provider_name = str(raw.get("name") or provider_id or DEFAULT_PROVIDER_NAME).strip()
    return {
        "id": provider_id,
        "name": provider_name,
        "api_key": str(raw.get("api_key") or "").strip(),
        "base_url": str(raw.get("base_url") or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
        "chat_model": str(raw.get("chat_model") or "").strip(),
        "embedding_model": str(raw.get("embedding_model") or "").strip(),
        "image_model": str(raw.get("image_model") or "").strip(),
        "campaign_model": str(raw.get("campaign_model") or "").strip(),
        "image_size": str(raw.get("image_size") or DEFAULT_IMAGE_SIZE).strip() or DEFAULT_IMAGE_SIZE,
    }


def _provider_records(include_default: bool = True) -> tuple[str, list[dict]]:
    store = _read_store()
    records = []
    seen = set()
    for idx, item in enumerate(store.get("providers") or []):
        if not isinstance(item, dict):
            continue
        record = _normalize_record(item, f"provider-{idx + 1}")
        if record["id"] in seen:
            continue
        seen.add(record["id"])
        records.append(record)

    if include_default and not records:
        records.append(_normalize_record(_default_record(), DEFAULT_PROVIDER_ID))

    active_id = _slug(str(store.get("active_provider") or ""))
    if not active_id and records:
        active_id = records[0]["id"]
    if active_id and records and not any(p["id"] == active_id for p in records):
        active_id = records[0]["id"]
    return active_id, records


def _config_from_record(record: dict | None) -> OpenAICompatibleConfig:
    record = _normalize_record(record or _default_record(), DEFAULT_PROVIDER_ID)
    return OpenAICompatibleConfig(
        provider_id=record["id"],
        provider_name=record["name"],
        api_key=record["api_key"],
        base_url=record["base_url"],
        chat_model=record["chat_model"],
        embedding_model=record["embedding_model"],
        image_model=record["image_model"],
        campaign_model=record["campaign_model"],
        image_size=record["image_size"],
    )


def get_config(provider_id: str | None = None) -> OpenAICompatibleConfig:
    """Read current runtime configuration from provider profiles or environment variables."""
    active_id, records = _provider_records(include_default=True)
    selected_id = _slug(provider_id or active_id)
    for record in records:
        if record["id"] == selected_id:
            return _config_from_record(record)
    if records:
        return _config_from_record(records[0])
    return _config_from_record(_default_record())


def is_configured(provider_id: str | None = None) -> bool:
    return get_config(provider_id).configured


def list_provider_profiles() -> list[dict]:
    active_id, records = _provider_records(include_default=True)
    profiles = []
    for record in records:
        cfg = _config_from_record(record)
        profiles.append({
            "id": cfg.provider_id,
            "name": cfg.provider_name,
            "base_url": cfg.base_url,
            "configured": cfg.configured,
            "active": cfg.provider_id == active_id,
            "chat_model": cfg.chat_model,
            "embedding_model": cfg.embedding_model,
            "image_model": cfg.image_model,
            "campaign_model": cfg.campaign_model,
            "image_size": cfg.image_size,
        })
    return profiles


def get_active_provider_id() -> str:
    active_id, _ = _provider_records(include_default=True)
    return active_id


def _store_from_records(active_id: str, records: list[dict]) -> dict:
    store = _read_store()
    store["active_provider"] = active_id
    store["providers"] = records
    return store


def normalize_token_policy_mode(mode: str | None) -> str:
    mode_key = (mode or "").strip().lower()
    return mode_key if mode_key in TOKEN_POLICY_MODES else DEFAULT_TOKEN_POLICY_MODE


def get_token_policy_mode() -> str:
    store = _read_store()
    stored = store.get("token_policy_mode") if isinstance(store, dict) else ""
    return normalize_token_policy_mode(stored or DEFAULT_TOKEN_POLICY_MODE)


def set_token_policy_mode(mode: str) -> str:
    normalized = normalize_token_policy_mode(mode)
    store = _read_store()
    store["token_policy_mode"] = normalized
    _write_store(store)
    return normalized


def get_token_policy(mode: str | None = None) -> dict:
    mode_key = normalize_token_policy_mode(mode or get_token_policy_mode())
    policy = dict(TOKEN_POLICY_MODES[mode_key])
    policy["mode"] = mode_key
    return policy


def clamp_output_tokens(max_tokens: int, *, json_mode: bool = False) -> int:
    policy = get_token_policy()
    cap_key = "json_output_token_cap" if json_mode else "output_token_cap"
    cap = int(policy.get(cap_key) or 0)
    requested = max(1, int(max_tokens or 1))
    return min(requested, cap) if cap > 0 else requested


def upsert_provider_profile(
    *,
    provider_id: str = "",
    provider_name: str = "",
    api_key: str | None = None,
    base_url: str | None = None,
    chat_model: str | None = None,
    embedding_model: str | None = None,
    image_model: str | None = None,
    campaign_model: str | None = None,
    image_size: str | None = None,
    make_active: bool = True,
) -> OpenAICompatibleConfig:
    active_id, records = _provider_records(include_default=True)
    target_id = _slug(provider_id or provider_name or DEFAULT_PROVIDER_ID)

    existing = None
    for record in records:
        if record["id"] == target_id:
            existing = record
            break
    if existing is None:
        existing = _normalize_record({"id": target_id, "name": provider_name or target_id}, target_id)
        records.append(existing)

    def pick(value: str | None, old: str) -> str:
        return value.strip() if value is not None and value.strip() else old

    existing["name"] = pick(provider_name, existing["name"])
    existing["api_key"] = pick(api_key, existing["api_key"])
    existing["base_url"] = pick(base_url, existing["base_url"])
    existing["chat_model"] = pick(chat_model, existing["chat_model"])
    existing["embedding_model"] = pick(embedding_model, existing["embedding_model"])
    existing["image_model"] = pick(image_model, existing["image_model"])
    existing["campaign_model"] = pick(campaign_model, existing["campaign_model"])
    existing["image_size"] = pick(image_size, existing["image_size"] or DEFAULT_IMAGE_SIZE)

    if make_active:
        active_id = existing["id"]
        _active_models.clear()

    _write_store(_store_from_records(active_id or existing["id"], records))
    return _config_from_record(existing)


def set_active_provider(provider_id: str) -> OpenAICompatibleConfig:
    active_id, records = _provider_records(include_default=True)
    target_id = _slug(provider_id)
    for record in records:
        if record["id"] == target_id:
            _write_store(_store_from_records(target_id, records))
            _active_models.clear()
            return _config_from_record(record)
    raise ValueError("provider not found")


def delete_provider_profile(provider_id: str) -> str:
    active_id, records = _provider_records(include_default=False)
    target_id = _slug(provider_id)
    remaining = [record for record in records if record["id"] != target_id]
    if len(remaining) == len(records):
        raise ValueError("provider not found")
    if active_id == target_id:
        active_id = remaining[0]["id"] if remaining else ""
        _active_models.clear()
    _write_store(_store_from_records(active_id, remaining))
    return active_id


def get_client(timeout: float | None = None, provider_id: str | None = None) -> OpenAI:
    cfg = get_config(provider_id)
    if not cfg.configured:
        raise RuntimeError("OpenAI-compatible endpoint is not configured.")
    kwargs = {
        "api_key": cfg.api_key,
        "base_url": cfg.base_url,
        "default_headers": {"User-Agent": BROWSER_USER_AGENT},
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    return OpenAI(**kwargs)


def get_active_model(capability: str = "chat") -> str:
    if capability in _active_models and _active_models[capability]:
        return _active_models[capability]
    cfg = get_config()
    defaults = {
        "chat": cfg.chat_model,
        "embedding": cfg.embedding_model,
        "image": cfg.image_model,
        "campaign": cfg.campaign_model,
    }
    return defaults.get(capability, "")


def _persist_active_model(capability: str, model: str) -> None:
    field = MODEL_CAPABILITY_FIELDS.get(capability)
    if not field:
        return
    active_id, records = _provider_records(include_default=True)
    for record in records:
        if record["id"] == active_id:
            record[field] = model
            _write_store(_store_from_records(active_id, records))
            return


def set_active_model(model: str, capability: str = "chat") -> str:
    model = (model or "").strip()
    if not model:
        raise ValueError("model is required")
    _active_models[capability] = model
    _persist_active_model(capability, model)
    return model


def _list_models_with_client(client: OpenAI, *, search: str, provider_name: str, provider_id: str) -> list[dict]:
    needle = search.strip().lower()
    response = client.models.list()
    models = []
    for item in getattr(response, "data", []) or []:
        model_id = getattr(item, "id", "") or ""
        if not model_id:
            continue
        if needle and needle not in model_id.lower():
            continue
        models.append({
            "key": model_id,
            "model_id": model_id,
            "label": model_id,
            "available": True,
            "provider": provider_name,
            "provider_id": provider_id,
        })
    models.sort(key=lambda m: m["model_id"].lower())
    return models


def list_remote_models(search: str = "", provider_id: str | None = None) -> list[dict]:
    """Fetch /models from a saved compatible endpoint."""
    cfg = get_config(provider_id)
    if not cfg.configured:
        return []
    client = get_client(timeout=30, provider_id=cfg.provider_id)
    return _list_models_with_client(
        client,
        search=search,
        provider_name=cfg.provider_name,
        provider_id=cfg.provider_id,
    )


def list_remote_models_for_endpoint(
    *,
    api_key: str,
    base_url: str,
    search: str = "",
    provider_name: str = "",
    provider_id: str = "",
) -> list[dict]:
    """Fetch /models from a draft endpoint without persisting the credentials."""
    key = (api_key or "").strip()
    url = (base_url or "").strip()
    if not key or not url:
        return []
    client = OpenAI(
        api_key=key,
        base_url=url,
        default_headers={"User-Agent": BROWSER_USER_AGENT},
        timeout=30,
    )
    return _list_models_with_client(
        client,
        search=search,
        provider_name=(provider_name or "未保存供应商").strip(),
        provider_id=(provider_id or "draft").strip(),
    )


def chat_completion(
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float = 0.8,
    max_tokens: int = 2000,
    json_mode: bool = False,
    response_format: dict | None = None,
    stream: bool = False,
    timeout: float = 120,
    apply_token_policy: bool = False,
):
    model_id = (model or get_active_model("chat")).strip()
    if not model_id:
        raise RuntimeError("No OpenAI-compatible chat model is selected.")
    json_mode = json_mode or (isinstance(response_format, dict) and response_format.get("type") == "json_object")
    if apply_token_policy:
        max_tokens = clamp_output_tokens(max_tokens, json_mode=json_mode)
    normalized_messages = list(messages or [])
    if json_mode and not _messages_contain_json(normalized_messages):
        normalized_messages = _with_json_instruction(normalized_messages)
    kwargs = {
        "model": model_id,
        "messages": normalized_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
        "timeout": timeout,
    }
    if json_mode:
        kwargs["response_format"] = response_format or {"type": "json_object"}
    client = get_client(timeout=timeout)
    try:
        return client.chat.completions.create(**kwargs)
    except Exception as exc:
        if not json_mode or "response_format" not in str(exc):
            raise
        kwargs.pop("response_format", None)
        return client.chat.completions.create(**kwargs)


def _messages_contain_json(messages: list[dict]) -> bool:
    for message in messages:
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, str) and "json" in content.lower():
            return True
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "json" in str(part.get("text", "")).lower():
                    return True
                if isinstance(part, str) and "json" in part.lower():
                    return True
    return False


def _with_json_instruction(messages: list[dict]) -> list[dict]:
    instruction = "\n\nReturn only a valid JSON object. Do not include markdown or explanatory text."
    if not messages:
        return [{"role": "system", "content": instruction.strip()}]
    updated = []
    inserted = False
    for message in messages:
        if not inserted and isinstance(message, dict) and message.get("role") == "system":
            updated.append({**message, "content": f"{message.get('content', '')}{instruction}"})
            inserted = True
        else:
            updated.append(message)
    if not inserted:
        updated.insert(0, {"role": "system", "content": instruction.strip()})
    return updated


def embedding_create(texts: list[str]):
    model_id = get_active_model("embedding").strip()
    if not model_id:
        raise RuntimeError("No OpenAI-compatible embedding model is configured.")
    return get_client(timeout=90).embeddings.create(model=model_id, input=texts)


def image_generate(prompt: str, *, model: str = "", size: str = ""):
    cfg = get_config()
    model_id = (model or get_active_model("image")).strip()
    if not model_id:
        raise RuntimeError("No OpenAI-compatible image model is configured.")
    client = get_client(timeout=120)
    kwargs = {
        "model": model_id,
        "prompt": prompt,
        "size": size or cfg.image_size,
        "n": 1,
        "response_format": "url",
    }
    try:
        return client.images.generate(**kwargs)
    except Exception as exc:
        if "response_format" not in str(exc):
            raise
        kwargs.pop("response_format", None)
        return client.images.generate(**kwargs)
