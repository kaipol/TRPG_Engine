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
    owner_account_id: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url)


def configure_provider_store(path: str) -> None:
    global _provider_store_base_dir
    config_path = Path(path)
    if config_path.name == local_config.CONFIG_FILE_NAME:
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
    owner_account_id = raw.get("owner_account_id")
    try:
        owner_account_id = int(owner_account_id) if owner_account_id not in (None, "") else None
    except (TypeError, ValueError):
        owner_account_id = None
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
        "owner_account_id": owner_account_id,
        "owner_username": str(raw.get("owner_username") or "").strip(),
        "owner_display_name": str(raw.get("owner_display_name") or "").strip(),
    }


def _owner_key(owner_account_id: int | None) -> str:
    return str(int(owner_account_id)) if owner_account_id is not None else ""


def _record_owner_id(record: dict) -> int | None:
    try:
        owner = record.get("owner_account_id")
        return int(owner) if owner not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _record_visible_to_owner(record: dict, owner_account_id: int | None, include_unowned: bool) -> bool:
    if owner_account_id is None:
        return include_unowned or _record_owner_id(record) is None
    owner_id = _record_owner_id(record)
    if owner_id == owner_account_id:
        return True
    return include_unowned and owner_id is None


def _provider_records(
    include_default: bool = True,
    *,
    owner_account_id: int | None = None,
    include_unowned: bool = True,
) -> tuple[str, list[dict]]:
    store = _read_store()
    records = []
    seen = set()
    for idx, item in enumerate(store.get("providers") or []):
        if not isinstance(item, dict):
            continue
        record = _normalize_record(item, f"provider-{idx + 1}")
        seen_key = (_record_owner_id(record), record["id"])
        if seen_key in seen:
            continue
        seen.add(seen_key)
        if not _record_visible_to_owner(record, owner_account_id, include_unowned):
            continue
        records.append(record)

    if include_default and not records:
        records.append(_normalize_record(_default_record(), DEFAULT_PROVIDER_ID))

    if owner_account_id is not None:
        active_map = store.get("active_provider_by_account") if isinstance(store.get("active_provider_by_account"), dict) else {}
        active_id = _slug(str(active_map.get(_owner_key(owner_account_id)) or ""))
    else:
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
        owner_account_id=_record_owner_id(record),
    )


def get_config(provider_id: str | None = None, *, owner_account_id: int | None = None) -> OpenAICompatibleConfig:
    """Read current runtime configuration from provider profiles or environment variables."""
    active_id, records = _provider_records(
        include_default=owner_account_id is None,
        owner_account_id=owner_account_id,
        include_unowned=owner_account_id is None,
    )
    selected_id = _slug(provider_id or active_id)
    for record in records:
        if record["id"] == selected_id:
            return _config_from_record(record)
    if records:
        return _config_from_record(records[0])
    return _config_from_record(_default_record())


def is_configured(provider_id: str | None = None, *, owner_account_id: int | None = None) -> bool:
    return get_config(provider_id, owner_account_id=owner_account_id).configured


def list_provider_profiles(*, owner_account_id: int | None = None, include_unowned: bool = True) -> list[dict]:
    active_id, records = _provider_records(
        include_default=owner_account_id is None,
        owner_account_id=owner_account_id,
        include_unowned=include_unowned,
    )
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
            "owner_account_id": cfg.owner_account_id,
        })
    return profiles


def get_active_provider_id(*, owner_account_id: int | None = None) -> str:
    active_id, _ = _provider_records(
        include_default=owner_account_id is None,
        owner_account_id=owner_account_id,
        include_unowned=owner_account_id is None,
    )
    return active_id


def _store_from_records(active_id: str, records: list[dict], *, owner_account_id: int | None = None) -> dict:
    store = _read_store()
    if owner_account_id is None:
        store["active_provider"] = active_id
    else:
        active_map = store.get("active_provider_by_account")
        if not isinstance(active_map, dict):
            active_map = {}
        key = _owner_key(owner_account_id)
        if active_id:
            active_map[key] = active_id
        else:
            active_map.pop(key, None)
        store["active_provider_by_account"] = active_map
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
    owner_account_id: int | None = None,
    owner_username: str = "",
    owner_display_name: str = "",
) -> OpenAICompatibleConfig:
    active_id, records = _provider_records(include_default=False, include_unowned=True)
    target_id = _slug(provider_id or provider_name or DEFAULT_PROVIDER_ID)

    existing = None
    for record in records:
        if record["id"] == target_id and _record_owner_id(record) == owner_account_id:
            existing = record
            break
    if existing is None:
        base_id = target_id
        suffix = 1
        used = {record["id"] for record in records}
        while target_id in used:
            suffix += 1
            target_id = _slug(f"{base_id}-{suffix}")
        existing = _normalize_record({"id": target_id, "name": provider_name or target_id}, target_id)
        records.append(existing)

    def pick(value: str | None, old: str) -> str:
        return value.strip() if value is not None and value.strip() else old

    existing["name"] = pick(provider_name, existing["name"])
    existing["owner_account_id"] = owner_account_id
    existing["owner_username"] = (owner_username or "").strip()
    existing["owner_display_name"] = (owner_display_name or "").strip()
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

    _write_store(_store_from_records(active_id or existing["id"], records, owner_account_id=owner_account_id))
    return _config_from_record(existing)


def set_active_provider(provider_id: str, *, owner_account_id: int | None = None) -> OpenAICompatibleConfig:
    _active_id, records = _provider_records(
        include_default=owner_account_id is None,
        owner_account_id=owner_account_id,
        include_unowned=owner_account_id is None,
    )
    target_id = _slug(provider_id)
    for record in records:
        if record["id"] == target_id:
            all_records = _provider_records(include_default=False, include_unowned=True)[1]
            _write_store(_store_from_records(target_id, all_records, owner_account_id=owner_account_id))
            _active_models.clear()
            return _config_from_record(record)
    raise ValueError("provider not found")


def delete_provider_profile(provider_id: str, *, owner_account_id: int | None = None) -> str:
    active_id, visible = _provider_records(
        include_default=False,
        owner_account_id=owner_account_id,
        include_unowned=owner_account_id is None,
    )
    _global_active_id, records = _provider_records(include_default=False, include_unowned=True)
    target_id = _slug(provider_id)
    target_record = next((record for record in visible if record["id"] == target_id), None)
    if not target_record:
        raise ValueError("provider not found")
    target_owner = _record_owner_id(target_record)
    remaining = [
        record for record in records
        if not (record["id"] == target_id and _record_owner_id(record) == target_owner)
    ]
    if len(remaining) == len(records):
        raise ValueError("provider not found")
    if active_id == target_id:
        replacement = [record for record in remaining if _record_visible_to_owner(record, owner_account_id, owner_account_id is None)]
        active_id = replacement[0]["id"] if replacement else ""
        _active_models.clear()
    _write_store(_store_from_records(active_id, remaining, owner_account_id=owner_account_id))
    return active_id


def get_client(
    timeout: float | None = None,
    provider_id: str | None = None,
    *,
    owner_account_id: int | None = None,
) -> OpenAI:
    cfg = get_config(provider_id, owner_account_id=owner_account_id)
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


def _active_model_key(capability: str, owner_account_id: int | None) -> str:
    return f"{_owner_key(owner_account_id) or 'global'}:{capability}"


def get_active_model(capability: str = "chat", *, owner_account_id: int | None = None) -> str:
    key = _active_model_key(capability, owner_account_id)
    if key in _active_models and _active_models[key]:
        return _active_models[key]
    cfg = get_config(owner_account_id=owner_account_id)
    defaults = {
        "chat": cfg.chat_model,
        "embedding": cfg.embedding_model,
        "image": cfg.image_model,
        "campaign": cfg.campaign_model,
    }
    return defaults.get(capability, "")


def _persist_active_model(capability: str, model: str, *, owner_account_id: int | None = None) -> None:
    field = MODEL_CAPABILITY_FIELDS.get(capability)
    if not field:
        return
    active_id, records = _provider_records(
        include_default=owner_account_id is None,
        owner_account_id=owner_account_id,
        include_unowned=owner_account_id is None,
    )
    _global_active_id, all_records = _provider_records(include_default=False, include_unowned=True)
    target_records = all_records or records
    for record in records:
        if record["id"] == active_id:
            owner_id = _record_owner_id(record)
            for target in target_records:
                if target["id"] == record["id"] and _record_owner_id(target) == owner_id:
                    target[field] = model
                    break
            _write_store(_store_from_records(active_id, target_records, owner_account_id=owner_account_id))
            return


def set_active_model(model: str, capability: str = "chat", *, owner_account_id: int | None = None) -> str:
    model = (model or "").strip()
    if not model:
        raise ValueError("model is required")
    _active_models[_active_model_key(capability, owner_account_id)] = model
    _persist_active_model(capability, model, owner_account_id=owner_account_id)
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


def list_remote_models(
    search: str = "",
    provider_id: str | None = None,
    *,
    owner_account_id: int | None = None,
) -> list[dict]:
    """Fetch /models from a saved compatible endpoint."""
    cfg = get_config(provider_id, owner_account_id=owner_account_id)
    if not cfg.configured:
        return []
    client = get_client(timeout=30, provider_id=cfg.provider_id, owner_account_id=owner_account_id)
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
    owner_account_id: int | None = None,
):
    model_id = (model or get_active_model("chat", owner_account_id=owner_account_id)).strip()
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
    client = get_client(timeout=timeout, owner_account_id=owner_account_id)
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


def embedding_create(texts: list[str], *, owner_account_id: int | None = None):
    model_id = get_active_model("embedding", owner_account_id=owner_account_id).strip()
    if not model_id:
        raise RuntimeError("No OpenAI-compatible embedding model is configured.")
    return get_client(timeout=90, owner_account_id=owner_account_id).embeddings.create(model=model_id, input=texts)


def image_generate(prompt: str, *, model: str = "", size: str = "", owner_account_id: int | None = None):
    cfg = get_config(owner_account_id=owner_account_id)
    model_id = (model or get_active_model("image", owner_account_id=owner_account_id)).strip()
    if not model_id:
        raise RuntimeError("No OpenAI-compatible image model is configured.")
    client = get_client(timeout=120, owner_account_id=owner_account_id)
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
