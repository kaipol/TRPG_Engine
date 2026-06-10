"""Persistent AI response cache for non-streaming chat completions."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Any

from . import ai_provider
from .database import safe_db
from .logger import get_logger


_log = get_logger("ai_cache")

ENV_ENABLED = "ZRIC_AI_RESPONSE_CACHE"
ENV_MAX_ENTRIES = "ZRIC_AI_RESPONSE_CACHE_MAX"
DEFAULT_MAX_ENTRIES = 512

SETTING_ENABLED = "ai_response_cache_enabled"
SETTING_MAX_ENTRIES = "ai_response_cache_max_entries"


def _env_enabled_default() -> bool:
    raw = os.environ.get(ENV_ENABLED, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _env_max_entries_default() -> int:
    raw = os.environ.get(ENV_MAX_ENTRIES, "").strip()
    if not raw:
        return DEFAULT_MAX_ENTRIES
    try:
        return max(16, min(10000, int(raw)))
    except ValueError:
        return DEFAULT_MAX_ENTRIES


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sha256_text(value: str) -> str:
    return hashlib.sha256((value or "").replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _stable_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _bool_text(value: bool) -> str:
    return "1" if value else "0"


def _read_setting(conn, key: str, default: str) -> str:
    try:
        row = conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row and row["value"] is not None else default
    except Exception:
        return default


def get_settings() -> dict[str, Any]:
    """Return persisted cache settings, falling back to environment defaults."""
    default_enabled = _bool_text(_env_enabled_default())
    default_max_entries = str(_env_max_entries_default())
    try:
        with safe_db() as conn:
            enabled_text = _read_setting(conn, SETTING_ENABLED, default_enabled)
            max_entries_text = _read_setting(conn, SETTING_MAX_ENTRIES, default_max_entries)
    except Exception as exc:
        _log.debug("AI cache settings unavailable: %s", exc)
        enabled_text = default_enabled
        max_entries_text = default_max_entries

    try:
        max_entries = max(16, min(10000, int(max_entries_text)))
    except ValueError:
        max_entries = _env_max_entries_default()

    return {
        "enabled": str(enabled_text).strip().lower() not in {"0", "false", "no", "off"},
        "max_entries": max_entries,
    }


def update_settings(*, enabled: bool | None = None, max_entries: int | None = None) -> dict[str, Any]:
    """Persist cache settings in system_state."""
    with safe_db() as conn:
        if enabled is not None:
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)",
                (SETTING_ENABLED, _bool_text(bool(enabled))),
            )
        if max_entries is not None:
            clamped = max(16, min(10000, int(max_entries)))
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)",
                (SETTING_MAX_ENTRIES, str(clamped)),
            )
        conn.commit()
    settings = get_settings()
    cleanup(settings["max_entries"])
    return settings


def make_cache_key(
    *,
    provider_id: str,
    provider_base_url: str,
    model: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    apply_token_policy: bool,
    token_policy_mode: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, dict[str, Any]]:
    """Build a stable cache key from model settings and exact prompt content."""
    system_hash = _sha256_text(system_prompt)
    user_hash = _sha256_text(user_prompt)
    provider_signature = _sha256_text(f"{provider_id}|{provider_base_url}|{model}")
    payload = {
        "provider_signature": provider_signature,
        "model": model or "",
        "temperature": round(float(temperature or 0), 3),
        "max_tokens": int(max_tokens or 0),
        "json_mode": bool(json_mode),
        "apply_token_policy": bool(apply_token_policy),
        "token_policy_mode": token_policy_mode or "",
        "system_hash": system_hash,
        "user_hash": user_hash,
    }
    cache_key = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    metadata = {
        **payload,
        "cache_key": cache_key,
        "provider_id": provider_id or "",
        "system_hash": system_hash,
        "user_hash": user_hash,
        "prompt_chars": len(system_prompt or "") + len(user_prompt or ""),
    }
    return cache_key, metadata


def key_for_request(
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    apply_token_policy: bool,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, dict[str, Any]]:
    """Build the key using the currently active provider and token policy."""
    cfg = ai_provider.get_config()
    effective_max_tokens = int(max_tokens or 1)
    token_policy_mode = ""
    if apply_token_policy:
        token_policy_mode = ai_provider.get_token_policy_mode()
        effective_max_tokens = ai_provider.clamp_output_tokens(effective_max_tokens, json_mode=json_mode)
    return make_cache_key(
        provider_id=cfg.provider_id,
        provider_base_url=cfg.base_url,
        model=model,
        temperature=temperature,
        max_tokens=effective_max_tokens,
        json_mode=json_mode,
        apply_token_policy=apply_token_policy,
        token_policy_mode=token_policy_mode,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )


def get_cached_response(cache_key: str) -> str | None:
    settings = get_settings()
    if not settings["enabled"]:
        return None
    try:
        with safe_db() as conn:
            row = conn.execute(
                "SELECT response_text FROM ai_response_cache WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE ai_response_cache SET last_used_at=?, hit_count=hit_count+1 WHERE cache_key=?",
                (_now(), cache_key),
            )
            conn.commit()
            return str(row["response_text"] or "")
    except Exception as exc:
        _log.debug("AI cache read skipped: %s", exc)
        return None


def store_response(metadata: dict[str, Any], response_text: str) -> None:
    settings = get_settings()
    if not settings["enabled"] or not response_text:
        return
    try:
        with safe_db() as conn:
            conn.execute(
                """
                INSERT INTO ai_response_cache (
                    cache_key, provider_id, model, token_policy_mode,
                    temperature, json_mode, max_tokens,
                    system_hash, user_hash, response_text,
                    prompt_chars, response_chars,
                    created_at, last_used_at, hit_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(cache_key) DO UPDATE SET
                    response_text=excluded.response_text,
                    response_chars=excluded.response_chars,
                    last_used_at=excluded.last_used_at
                """,
                (
                    metadata["cache_key"],
                    metadata.get("provider_id", ""),
                    metadata.get("model", ""),
                    metadata.get("token_policy_mode", ""),
                    float(metadata.get("temperature") or 0),
                    1 if metadata.get("json_mode") else 0,
                    int(metadata.get("max_tokens") or 0),
                    metadata.get("system_hash", ""),
                    metadata.get("user_hash", ""),
                    response_text,
                    int(metadata.get("prompt_chars") or 0),
                    len(response_text),
                    _now(),
                    _now(),
                ),
            )
            conn.commit()
        cleanup(settings["max_entries"])
    except Exception as exc:
        _log.debug("AI cache write skipped: %s", exc)


def cleanup(max_entries: int | None = None) -> int:
    """Trim least-recently-used entries beyond the configured cap."""
    cap = int(max_entries or get_settings()["max_entries"])
    if cap <= 0:
        return 0
    try:
        with safe_db() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM ai_response_cache").fetchone()
            count = int(row["n"] if row else 0)
            overflow = count - cap
            if overflow <= 0:
                return 0
            rows = conn.execute(
                "SELECT cache_key FROM ai_response_cache ORDER BY last_used_at ASC, created_at ASC LIMIT ?",
                (overflow,),
            ).fetchall()
            keys = [r["cache_key"] for r in rows]
            conn.executemany("DELETE FROM ai_response_cache WHERE cache_key=?", [(k,) for k in keys])
            conn.commit()
            return len(keys)
    except Exception as exc:
        _log.debug("AI cache cleanup skipped: %s", exc)
        return 0


def clear_cache() -> int:
    """Delete all cached AI responses."""
    try:
        with safe_db() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM ai_response_cache").fetchone()
            count = int(row["n"] if row else 0)
            conn.execute("DELETE FROM ai_response_cache")
            conn.commit()
            return count
    except Exception as exc:
        _log.debug("AI cache clear skipped: %s", exc)
        return 0


def cache_status() -> dict[str, Any]:
    settings = get_settings()
    stats = {
        "entries": 0,
        "total_hits": 0,
        "saved_prompt_chars": 0,
        "saved_response_chars": 0,
        "oldest_at": "",
        "newest_at": "",
    }
    try:
        with safe_db() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS entries,
                    COALESCE(SUM(hit_count), 0) AS total_hits,
                    COALESCE(SUM(prompt_chars * hit_count), 0) AS saved_prompt_chars,
                    COALESCE(SUM(response_chars * hit_count), 0) AS saved_response_chars,
                    COALESCE(MIN(created_at), '') AS oldest_at,
                    COALESCE(MAX(created_at), '') AS newest_at
                FROM ai_response_cache
                """
            ).fetchone()
            if row:
                stats = {key: row[key] for key in stats.keys()}
    except Exception as exc:
        _log.debug("AI cache stats unavailable: %s", exc)
    return {**settings, **stats}
