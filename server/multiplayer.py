"""
Z.R.I.C multiplayer fusion module.

This module turns the existing single-table console into a room-based web
surface while reusing the TRPG_Engine dice parser and ZRIC AI/RAG primitives.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import fastapi
from fastapi import APIRouter, File, Form, Query, Request, UploadFile, WebSocket, WebSocketDisconnect

try:
    from .logger import get_logger
except Exception:  # pragma: no cover - import fallback for direct tooling
    import logging

    def get_logger(name: str):
        return logging.getLogger(name)


_log = get_logger("multiplayer")
multiplayer_router = APIRouter(tags=["多人联机"])
from . import ai_provider
from .auth import account_player_id, get_account_by_token, require_account_from_request
from .campaign_storage import (
    campaign_asset_url,
    resolve_campaign_folder_name,
    sanitize_asset_name,
    unique_path,
)
from .local_config import get_multiplayer_settings
from .multiplayer_models import (
    AiEventRequest,
    CharacterClaimRequest,
    DiceRollRequest,
    JoinRoomRequest,
    MessageCreateRequest,
    PlayerActionRequest,
    RoomCreateRequest,
    RoomPatchRequest,
    RoomRollbackRequest,
    TokenMoveRequest,
    TokenUpsertRequest,
)

try:
    from .trpgdice.component.roll.dice import get_roll_result, parse_dice_expression
except Exception as exc:  # pragma: no cover - surfaced by /health and tests
    _log.warning("TRPG dice module unavailable: %s", exc)
    get_roll_result = None
    parse_dice_expression = None


_db_file = ""
_append_to_memory = None
_chunk_text = None
_get_embeddings = None
_refresh_vector_cache = None

_ws_clients_by_room: dict[int, set[WebSocket]] = {}
_ws_meta: dict[WebSocket, dict[str, Any]] = {}

_MULTIPLAYER_SETTINGS = get_multiplayer_settings()
MAX_SCENARIO_UPLOAD_BYTES = int(_MULTIPLAYER_SETTINGS["max_scenario_upload_bytes"])
MAX_SCENARIO_CHARS = int(_MULTIPLAYER_SETTINGS["max_scenario_chars"])
MAX_SCENARIO_CHUNKS = int(_MULTIPLAYER_SETTINGS["max_scenario_chunks"])
MAX_SCENARIO_PDF_PAGES = int(_MULTIPLAYER_SETTINGS["max_scenario_pdf_pages"])
MAX_MAP_UPLOAD_BYTES = int(_MULTIPLAYER_SETTINGS["max_map_upload_bytes"])
MAX_ROOM_PLAYERS = int(_MULTIPLAYER_SETTINGS["max_room_players"])
BGM_TRACKS: dict[str, str] = {
    "午后田园": "https://soundimage.org/wp-content/uploads/2014/08/Netherplace.mp3",
    "黄昏渡口": "https://soundimage.org/wp-content/uploads/2018/01/Romantic-Lands-Beckon.mp3",
    "钢琴小品": "https://soundimage.org/wp-content/uploads/2014/04/Ballooning.mp3",
    "浪漫舞会": "https://soundimage.org/wp-content/uploads/2014/10/Romantic-Halloween-Theme.mp3",
    "钢琴沉思": "https://soundimage.org/wp-content/uploads/2014/05/Space-for-Thought.mp3",
    "月光下的林间": "http://soundimage.org/wp-content/uploads/2014/11/Moonlit-Secrets.mp3",
    "星际穿越": "https://soundimage.org/wp-content/uploads/2025/01/Cyber-Mean-Streets.mp3",
    "幽影神秘之地": "https://cdn.pixabay.com/audio/2022/08/02/audio_884fe92c21.mp3",
    "蓝调时刻": "https://cdn.pixabay.com/audio/2022/05/27/audio_1808fbf07a.mp3",
    "赛博梦都": "https://cdn.pixabay.com/audio/2022/11/22/audio_febc508520.mp3",
    "残阳黎明": "https://cdn.pixabay.com/audio/2022/08/23/audio_d16737dc28.mp3",
}
BGM_TRACK_NAMES_TEXT = " / ".join(BGM_TRACKS)


def _trim_token_text(text: str, field: str, *, keep_tail: bool = False) -> str:
    value = str(text or "")
    get_policy = getattr(ai_provider, "get_token_policy", None)
    policy = get_policy() if callable(get_policy) else {}
    max_chars = int(policy.get(field) or 0)
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    marker = "\n...[已按省 token 策略省略部分内容]...\n"
    marker_len = len(marker)
    if max_chars <= marker_len + 40:
        return value[-max_chars:] if keep_tail else value[:max_chars]
    if keep_tail:
        return marker + value[-(max_chars - marker_len):]
    head = max(1, (max_chars - marker_len) // 2)
    tail = max_chars - marker_len - head
    return value[:head] + marker + value[-tail:]


def configure_multiplayer(
    db_file: str,
    chat_client=None,
    fn_append_to_memory=None,
    fn_chunk_text=None,
    fn_get_embeddings=None,
    fn_refresh_vector_cache=None,
):
    """Inject runtime dependencies from main.py."""
    global _db_file, _append_to_memory
    global _chunk_text, _get_embeddings, _refresh_vector_cache
    _db_file = db_file
    _append_to_memory = fn_append_to_memory
    _chunk_text = fn_chunk_text
    _get_embeddings = fn_get_embeddings
    _refresh_vector_cache = fn_refresh_vector_cache


def get_db_connection():
    if not _db_file:
        raise RuntimeError("multiplayer module is not configured")
    conn = sqlite3.connect(_db_file, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def safe_db():
    conn = get_db_connection()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_multiplayer_tables():
    """Create room/chat/VTT integration tables. Idempotent."""
    with safe_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS multiplayer_rooms (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                code               TEXT NOT NULL UNIQUE,
                name               TEXT NOT NULL,
                gm_name            TEXT NOT NULL DEFAULT '',
                campaign_path      TEXT NOT NULL DEFAULT '',
                gm_token_hash      TEXT NOT NULL DEFAULT '',
                current_scene_id   INTEGER,
                current_room_id    INTEGER,
                map_background_url TEXT NOT NULL DEFAULT '',
                settings           TEXT NOT NULL DEFAULT '{}',
                created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at         TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS multiplayer_members (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id      INTEGER NOT NULL,
                player_id    TEXT NOT NULL,
                display_name TEXT NOT NULL,
                role         TEXT NOT NULL DEFAULT 'player',
                color        TEXT NOT NULL DEFAULT '#7dd3fc',
                client_id    TEXT NOT NULL DEFAULT '',
                member_token_hash TEXT NOT NULL DEFAULT '',
                connected    INTEGER NOT NULL DEFAULT 0,
                last_seen    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(room_id, player_id),
                FOREIGN KEY(room_id) REFERENCES multiplayer_rooms(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS multiplayer_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id     INTEGER NOT NULL,
                sender_id   TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                kind        TEXT NOT NULL DEFAULT 'chat',
                content     TEXT NOT NULL DEFAULT '',
                payload     TEXT NOT NULL DEFAULT '{}',
                created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY(room_id) REFERENCES multiplayer_rooms(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS multiplayer_tokens (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id             INTEGER NOT NULL,
                token_id            TEXT NOT NULL,
                name                TEXT NOT NULL DEFAULT 'Token',
                kind                TEXT NOT NULL DEFAULT 'pc',
                owner_id            TEXT NOT NULL DEFAULT '',
                avatar_url          TEXT NOT NULL DEFAULT '',
                color               TEXT NOT NULL DEFAULT '#f59e0b',
                x                   REAL NOT NULL DEFAULT 120,
                y                   REAL NOT NULL DEFAULT 120,
                size                REAL NOT NULL DEFAULT 48,
                linked_room_id      INTEGER,
                linked_entity_id    INTEGER,
                linked_character_id INTEGER,
                notes               TEXT NOT NULL DEFAULT '',
                updated_at          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(room_id, token_id),
                FOREIGN KEY(room_id) REFERENCES multiplayer_rooms(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS multiplayer_room_documents (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id    INTEGER NOT NULL,
                rag_doc_id INTEGER,
                title      TEXT NOT NULL DEFAULT '',
                source     TEXT NOT NULL DEFAULT '',
                chunk_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY(room_id) REFERENCES multiplayer_rooms(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS multiplayer_character_claims (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id      INTEGER NOT NULL,
                character_id INTEGER NOT NULL,
                player_id    TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                claimed_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(room_id, character_id),
                FOREIGN KEY(room_id) REFERENCES multiplayer_rooms(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS multiplayer_room_checkpoints (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id        INTEGER NOT NULL,
                from_message_id INTEGER NOT NULL DEFAULT 0,
                label          TEXT NOT NULL DEFAULT '',
                snapshot       TEXT NOT NULL DEFAULT '{}',
                created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY(room_id) REFERENCES multiplayer_rooms(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mp_messages_room ON multiplayer_messages(room_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mp_tokens_room ON multiplayer_tokens(room_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mp_docs_room ON multiplayer_room_documents(room_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mp_claims_room_player ON multiplayer_character_claims(room_id, player_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mp_checkpoints_room ON multiplayer_room_checkpoints(room_id, id)")
        conn.execute("UPDATE multiplayer_members SET role='player' WHERE role='gm'")
        conn.execute("UPDATE multiplayer_members SET connected=0")
        for row in conn.execute("SELECT id FROM multiplayer_rooms").fetchall():
            _normalize_single_character_claims(conn, int(row["id"]))
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_mp_claims_one_character_per_player "
            "ON multiplayer_character_claims(room_id, player_id)"
        )
        for sql in (
            "ALTER TABLE multiplayer_rooms ADD COLUMN current_scene_id INTEGER",
            "ALTER TABLE multiplayer_rooms ADD COLUMN current_room_id INTEGER",
            "ALTER TABLE multiplayer_rooms ADD COLUMN map_background_url TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE multiplayer_rooms ADD COLUMN gm_token_hash TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE multiplayer_members ADD COLUMN client_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE multiplayer_members ADD COLUMN member_token_hash TEXT NOT NULL DEFAULT ''",
        ):
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        conn.commit()



def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_matches(token: str, token_hash: str) -> bool:
    return bool(token and token_hash and hmac.compare_digest(_hash_token(token), token_hash))


def _header_token(request: Request, name: str) -> str:
    return (request.headers.get(name) or "").strip()


def _ws_query_token(token: str | None) -> str:
    return (token or "").strip()


def _room_token_from_request(request: Request) -> str:
    return _header_token(request, "X-Room-Token")


def _member_token_from_request(request: Request) -> str:
    return _header_token(request, "X-Member-Token")


def _has_gm_access(room, room_token: str) -> bool:
    room_data = dict(room)
    return _token_matches(room_token, room_data.get("gm_token_hash", ""))


def _require_gm(room, room_token: str):
    if not dict(room).get("gm_token_hash"):
        raise fastapi.HTTPException(status_code=403, detail="此房间缺少 GM 令牌，请重新创建房间")
    if not _has_gm_access(room, room_token):
        raise fastapi.HTTPException(status_code=403, detail="需要 GM 房间令牌")


def _require_member_or_gm(conn, room, player_id: str, member_token: str, room_token: str = "") -> str:
    if _has_gm_access(room, room_token):
        return "gm"
    player_id = (player_id or "").strip()
    if not player_id:
        raise fastapi.HTTPException(status_code=403, detail="缺少玩家身份")
    member = conn.execute(
        "SELECT * FROM multiplayer_members WHERE room_id=? AND player_id=?",
        (room["id"], player_id),
    ).fetchone()
    if not member or not _token_matches(member_token, member["member_token_hash"]):
        raise fastapi.HTTPException(status_code=403, detail="成员令牌无效")
    # 真人进入多人桌时永远按玩家处理；AI-GM/管理同步只通过房间令牌走 gm 分支。
    return "player"


def _require_player_character_claims(conn, room, player_id: str, role: str) -> list[dict[str, Any]]:
    if role == "gm":
        return []
    claims = _character_claims_for_player(conn, room["id"], player_id)
    if not claims:
        raise fastapi.HTTPException(status_code=403, detail="请先确认扮演角色")
    return claims


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_json(text: str, fallback):
    try:
        return json.loads(text) if text else fallback
    except Exception:
        return fallback


def _dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _room_code() -> str:
    return secrets.token_hex(3).upper()


def _room_by_code(conn, room_code: str):
    row = conn.execute(
        "SELECT * FROM multiplayer_rooms WHERE UPPER(code)=UPPER(?)",
        (room_code.strip(),),
    ).fetchone()
    if not row:
        raise fastapi.HTTPException(status_code=404, detail="房间不存在")
    return row


def _room_campaign_folder(row) -> tuple[str, str]:
    room = dict(row)
    settings = _load_json(room.get("settings", "{}"), {})
    candidates = [
        str(settings.get("campaign_path") or "").strip(),
        str(room.get("campaign_path") or "").strip(),
    ]
    for raw_path in candidates:
        if not raw_path:
            continue
        normalized = raw_path.replace("\\", "/").strip("/")
        parts = [part for part in normalized.split("/") if part]
        if len(parts) == 2 and parts[0] == "campaigns":
            campaign_name = parts[1]
        elif len(parts) == 1:
            campaign_name = parts[0]
        else:
            raise fastapi.HTTPException(status_code=400, detail="房间绑定的剧本路径无效")
        name, folder, _campaign_json = resolve_campaign_folder_name(campaign_name)
        return name, folder
    raise fastapi.HTTPException(status_code=400, detail="房间未绑定剧本，无法保存地图到对应剧本目录")


def _serialize_room(row) -> dict[str, Any]:
    data = dict(row)
    data["settings"] = _load_json(data.get("settings", "{}"), {})
    data.pop("gm_token_hash", None)
    return data


def _serialize_message(row) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = _load_json(data.get("payload", "{}"), {})
    return data


def _serialize_token(row) -> dict[str, Any]:
    return dict(row)


def _serialize_member(row) -> dict[str, Any]:
    data = dict(row)
    data.pop("member_token_hash", None)
    data.pop("client_id", None)
    return data


def _serialize_character_claim(row) -> dict[str, Any]:
    return dict(row)


def _normalize_display_name(value: Any) -> str:
    name = re.sub(r"\s+", " ", str(value or "").strip())[:40]
    return name or "玩家"


def _display_name_key(value: Any) -> str:
    return _normalize_display_name(value).casefold()


def _normalize_client_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]", "", str(value or "").strip())[:120]


def _is_account_player_id(value: Any) -> bool:
    return str(value or "").startswith("account:")


def _join_request_for_account(req: JoinRoomRequest, account: dict[str, Any]) -> JoinRoomRequest:
    req.player_id = account_player_id(account)
    req.display_name = _normalize_display_name(account.get("display_name") or account.get("username") or req.display_name)
    return req


def _account_player_id_from_request(request: Request) -> str:
    return account_player_id(require_account_from_request(request))


def _joined_player_count(conn, room_id: int) -> int:
    rows = conn.execute(
        """
        SELECT m.player_id
        FROM multiplayer_members m
        WHERE m.room_id=?
          AND (
            m.connected=1
            OR EXISTS (
                SELECT 1
                FROM multiplayer_character_claims c
                WHERE c.room_id=m.room_id AND c.player_id=m.player_id
            )
          )
        """,
        (room_id,),
    ).fetchall()
    return len({str(row["player_id"] or "").strip() for row in rows if str(row["player_id"] or "").strip()})


def _normalize_single_character_claims(conn, room_id: int) -> None:
    rows = conn.execute(
        """
        SELECT id, player_id
        FROM multiplayer_character_claims
        WHERE room_id=?
        ORDER BY player_id, claimed_at, id
        """,
        (room_id,),
    ).fetchall()
    seen: set[str] = set()
    duplicate_ids: list[int] = []
    for row in rows:
        player_id = str(row["player_id"] or "").strip()
        if not player_id:
            duplicate_ids.append(int(row["id"]))
            continue
        if player_id in seen:
            duplicate_ids.append(int(row["id"]))
        else:
            seen.add(player_id)
    if duplicate_ids:
        placeholders = ",".join("?" for _ in duplicate_ids)
        conn.execute(f"DELETE FROM multiplayer_character_claims WHERE id IN ({placeholders})", duplicate_ids)


def _character_claims_for_room(conn, room_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT c.id, c.room_id, c.character_id, c.player_id, c.display_name, c.claimed_at,
               ch.name AS character_name, ch.role AS character_role,
               m.color AS player_color, m.connected AS player_connected
        FROM multiplayer_character_claims c
        LEFT JOIN characters ch ON ch.id=c.character_id
        LEFT JOIN multiplayer_members m ON m.room_id=c.room_id AND m.player_id=c.player_id
        WHERE c.room_id=?
        ORDER BY c.claimed_at, c.id
        """,
        (room_id,),
    ).fetchall()
    return [_serialize_character_claim(r) for r in rows]


def _character_claims_for_player(conn, room_id: int, player_id: str) -> list[dict[str, Any]]:
    player_id = (player_id or "").strip()
    if not player_id:
        return []
    return [c for c in _character_claims_for_room(conn, room_id) if c.get("player_id") == player_id]


def _claim_names(claims: list[dict[str, Any]]) -> list[str]:
    return [str(c.get("character_name") or f"角色{c.get('character_id')}") for c in claims]


def _single_character_claim_for_player(conn, room, player_id: str, role: str) -> dict[str, Any] | None:
    claims = _require_player_character_claims(conn, room, player_id, role)
    if len(claims) <= 1:
        return claims[0] if claims else None
    _normalize_single_character_claims(conn, int(room["id"]))
    conn.commit()
    claims = _require_player_character_claims(conn, room, player_id, role)
    if len(claims) > 1:
        raise fastapi.HTTPException(status_code=409, detail="每位玩家只能扮演一个角色")
    return claims[0] if claims else None


def _claim_actor_name(claim: dict[str, Any] | None, fallback: str = "玩家") -> str:
    if not claim:
        return (fallback or "玩家")[:40]
    return str(claim.get("character_name") or fallback or "玩家")[:40]


def _claim_actor_payload(
    actor_id: str,
    actor_name: str,
    claim: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "actor_id": (actor_id or "")[:80],
        "actor_name": (actor_name or "玩家")[:40],
    }
    if claim:
        payload.update(
            {
                "character_id": _int_or_none(claim.get("character_id")),
                "character_name": str(claim.get("character_name") or actor_name or "")[:80],
                "character_role": str(claim.get("character_role") or "")[:80],
            }
        )
    return payload


def _request_with_claim_actor(req: DiceRollRequest | PlayerActionRequest, claim: dict[str, Any] | None):
    if not claim:
        return req
    return req.copy(
        update={
            "actor_name": _claim_actor_name(claim, req.actor_name),
            "character_id": _int_or_none(claim.get("character_id")),
            "character_name": str(claim.get("character_name") or "")[:80],
        }
    )


def _system_value(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def _valid_bgm_name(value: Any) -> str:
    name = str(value or "").strip()[:40]
    return name if name in BGM_TRACKS else ""


def _apply_bgm_recommendation(conn, adjudication: dict[str, Any]) -> str:
    name = _valid_bgm_name(adjudication.get("bgm_name"))
    if not name:
        return ""
    conn.execute(
        "INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_bgm_url',?)",
        (BGM_TRACKS[name],),
    )
    conn.execute(
        "INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_bgm_name',?)",
        (name,),
    )
    conn.commit()
    return name


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _playable_character_count(conn) -> int:
    active = conn.execute(
        "SELECT COUNT(*) AS count FROM characters WHERE COALESCE(status,'active')<>'hidden'"
    ).fetchone()
    total = conn.execute("SELECT COUNT(*) AS count FROM characters").fetchone()
    return int((active and active["count"]) or (total and total["count"]) or 1)


def _normalize_room_settings(conn, raw_settings: Any) -> dict[str, Any]:
    settings = dict(raw_settings) if isinstance(raw_settings, dict) else {}
    playable_count = _playable_character_count(conn)
    upper = max(1, min(playable_count, MAX_ROOM_PLAYERS))
    requested = _int_or_none(settings.get("max_players")) or upper
    settings["max_players"] = max(1, min(requested, upper))
    return settings


def _claimed_player_count(claims: list[dict[str, Any]]) -> int:
    return len({str(c.get("player_id") or "").strip() for c in claims if str(c.get("player_id") or "").strip()})


def _claimed_character_count(claims: list[dict[str, Any]]) -> int:
    return len({int(c["character_id"]) for c in claims if c.get("character_id") is not None})


def _room_seat_info(conn, room_row, claims: list[dict[str, Any]] | None = None) -> dict[str, int]:
    claims = claims if claims is not None else _character_claims_for_room(conn, int(room_row["id"]))
    settings = _normalize_room_settings(conn, _load_json(room_row["settings"], {}))
    joined_players = _joined_player_count(conn, int(room_row["id"]))
    claimed_players = _claimed_player_count(claims)
    claimed_characters = _claimed_character_count(claims)
    max_players = int(settings["max_players"])
    return {
        "max_players": max_players,
        "joined_player_count": joined_players,
        "claimed_player_count": claimed_players,
        "claimed_character_count": claimed_characters,
        "player_slots_remaining": max(0, max_players - joined_players),
        "character_slots_remaining": max(0, max_players - claimed_characters),
        "playable_character_count": _playable_character_count(conn),
    }


def _serialize_room_with_limits(conn, row, claims: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    data = _serialize_room(row)
    settings = _normalize_room_settings(conn, data.get("settings", {}))
    data["settings"] = settings
    data.update(_room_seat_info(conn, row, claims))
    data["settings"]["max_players"] = data["max_players"]
    return data


def _serialize_scene_for_players(conn, scene_id: int | None) -> dict[str, Any] | None:
    if not scene_id:
        return None
    node = conn.execute("SELECT * FROM nodes WHERE id=?", (scene_id,)).fetchone()
    if not node:
        return None
    options = [
        dict(r)
        for r in conn.execute(
            "SELECT id, text, next_node_id FROM options WHERE node_id=? ORDER BY id",
            (scene_id,),
        ).fetchall()
    ]
    scene = dict(node)
    scene["options"] = options
    return scene


def _first_scene_id(conn) -> int | None:
    row = conn.execute("SELECT id FROM nodes ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def _current_scene_for_room(conn, room) -> dict[str, Any] | None:
    scene_id = _int_or_none(room["current_scene_id"]) or _int_or_none(_system_value(conn, "player_current_scene_id")) or _first_scene_id(conn)
    return _serialize_scene_for_players(conn, scene_id)


def _current_scene_id_for_room(conn, room) -> int | None:
    return _int_or_none(room["current_scene_id"]) or _int_or_none(_system_value(conn, "player_current_scene_id")) or _first_scene_id(conn)


def _map_room_for_scene(conn, node_id: int) -> sqlite3.Row | None:
    try:
        return conn.execute("SELECT id, label FROM map_rooms WHERE node_id=? LIMIT 1", (node_id,)).fetchone()
    except sqlite3.OperationalError:
        return None


def _resolve_action_scene_advance(conn, room, req: PlayerActionRequest) -> tuple[sqlite3.Row, sqlite3.Row] | None:
    current_scene_id = _current_scene_id_for_room(conn, room)
    if not current_scene_id:
        return None

    option = None
    if req.option_id:
        option = conn.execute(
            "SELECT id, text, node_id, next_node_id FROM options WHERE id=? AND node_id=?",
            (req.option_id, current_scene_id),
        ).fetchone()
    if not option and req.next_node_id:
        option = conn.execute(
            """
            SELECT id, text, node_id, next_node_id
            FROM options
            WHERE node_id=? AND next_node_id=?
            ORDER BY id
            LIMIT 1
            """,
            (current_scene_id, req.next_node_id),
        ).fetchone()
    if not option:
        return None

    next_node_id = _int_or_none(option["next_node_id"])
    if not next_node_id:
        return None
    next_node = conn.execute("SELECT * FROM nodes WHERE id=?", (next_node_id,)).fetchone()
    if not next_node:
        return None
    return option, next_node


def _apply_action_scene_advance(conn, room, req: PlayerActionRequest, actor_payload: dict[str, Any]) -> dict[str, Any] | None:
    resolved = _resolve_action_scene_advance(conn, room, req)
    if not resolved:
        return None
    option, next_node = resolved
    next_node_id = int(next_node["id"])
    option_text = (req.option_text or option["text"] or req.action or "继续").strip()[:300]
    map_room = _map_room_for_scene(conn, next_node_id)
    current_room_id = int(map_room["id"]) if map_room else None
    if map_room:
        conn.execute("UPDATE map_rooms SET state='explored' WHERE id=?", (current_room_id,))
    conn.execute(
        """
        UPDATE multiplayer_rooms
        SET current_scene_id=?,
            current_room_id=COALESCE(?, current_room_id),
            updated_at=?
        WHERE id=?
        """,
        (next_node_id, current_room_id, _now(), room["id"]),
    )
    scene_name = str(next_node["name"] or f"场景 #{next_node_id}").strip()
    content = f"场景推进：{req.actor_name}选择「{option_text}」→ {scene_name}"
    scene_payload = {
        "source": "scene_advance",
        **actor_payload,
        "option_id": int(option["id"]),
        "option_text": option_text,
        "from_node_id": _current_scene_id_for_room(conn, room),
        "next_node_id": next_node_id,
        "scene_name": scene_name,
        "map_room_id": current_room_id,
        "map_room_label": str(map_room["label"] or "") if map_room else "",
    }
    scene_msg = _save_message(conn, room["id"], "ai-kp", "AI-KP", "state", content, scene_payload)
    _remember_room_event(conn, room["code"], content)
    return scene_msg


def _character_opening_text(conn, room, character: sqlite3.Row | dict[str, Any]) -> str:
    char = dict(character)
    scene = _current_scene_for_room(conn, room) or {}
    scene_name = str(scene.get("name") or "开场").strip()
    scene_text = str(scene.get("expanded_content") or scene.get("content") or "").strip()
    scene_excerpt = _single_line(scene_text)[:420] or "主持端尚未写下更多场景细节，你先从自己的视角观察当下。"
    name = str(char.get("name") or "角色").strip()
    role = str(char.get("role") or "调查员").strip()
    inventory = str(char.get("inventory") or "").strip()
    personality = str(char.get("personality") or "").strip()
    status = str(char.get("status") or "active").strip()
    seed = hashlib.sha256(f"{room['code']}|{char.get('id')}|{name}".encode("utf-8")).hexdigest()
    focus_options = [
        "你首先注意到环境里最不合常理的细节。",
        "你下意识检查随身物与退路，确认自己还能掌控什么。",
        "你把同伴的动静放在余光里，独自判断这里是否安全。",
        "你从自己的经历出发，意识到眼前的线索可能并不简单。",
    ]
    focus = focus_options[int(seed[:2], 16) % len(focus_options)]
    details = [f"{name}，你以「{role}」的身份进入「{scene_name}」。"]
    if personality:
        details.append(f"你的性格/背景提示是：{_single_line(personality)[:160]}。")
    if inventory:
        details.append(f"你当前随身/状态记录：{_single_line(inventory)[:160]}。")
    elif status and status != "active":
        details.append(f"你当前状态为：{status}。")
    details.append(focus)
    details.append(f"从你的视角看，开场是这样的：{scene_excerpt}")
    details.append("你可以先用自己的角色口吻描述反应，或直接提交一次行动交给 AI-GM 单独裁定。")
    return "\n".join(details)[:1800]


def _stable_stat_seed(name: str) -> random.Random:
    digest = hashlib.sha256((name or "玩家").encode("utf-8")).hexdigest()
    return random.Random(int(digest[:12], 16))


def _normalize_player_characters(conn) -> None:
    chars = conn.execute("SELECT id, name, role, hp, san FROM characters ORDER BY id").fetchall()
    if not chars:
        rng = _stable_stat_seed("默认调查员")
        conn.execute(
            "INSERT INTO characters (name, role, hp, san, inventory, personality, status) VALUES (?,?,?,?,?,?,?)",
            ("调查员", "PC", rng.randint(70, 100), rng.randint(55, 85), "", "", "active"),
        )
        return
    for char in chars:
        hp = char["hp"]
        san = char["san"]
        updates: dict[str, int] = {}
        rng = _stable_stat_seed(char["name"] or f"角色{char['id']}")
        if hp is None:
            updates["hp"] = rng.randint(60, 100)
        if san is None:
            updates["san"] = rng.randint(45, 90)
        if updates:
            conn.execute(
                "UPDATE characters SET hp=COALESCE(?, hp), san=COALESCE(?, san) WHERE id=?",
                (updates.get("hp"), updates.get("san"), char["id"]),
            )


def _ensure_room_opening(conn, room) -> sqlite3.Row:
    _normalize_player_characters(conn)
    scene_id = _int_or_none(room["current_scene_id"]) or _int_or_none(_system_value(conn, "player_current_scene_id")) or _first_scene_id(conn)
    if scene_id and not _int_or_none(room["current_scene_id"]):
        conn.execute(
            "UPDATE multiplayer_rooms SET current_scene_id=?, updated_at=? WHERE id=?",
            (scene_id, _now(), room["id"]),
        )
        room = conn.execute("SELECT * FROM multiplayer_rooms WHERE id=?", (room["id"],)).fetchone()
    conn.commit()
    return room


def _serialize_map_room(conn, room_id: int | None) -> dict[str, Any] | None:
    if not room_id:
        return None
    try:
        room = conn.execute("SELECT * FROM map_rooms WHERE id=?", (room_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(room) if room else None


def _build_game_state(conn, room_row=None) -> dict[str, Any]:
    if room_row:
        room_row = _ensure_room_opening(conn, room_row)
    room_id = int(room_row["id"]) if room_row else None
    room_scene_id = _int_or_none(room_row["current_scene_id"]) if room_row else None
    room_map_id = _int_or_none(room_row["current_room_id"]) if room_row else None
    player_scene_id = _int_or_none(_system_value(conn, "player_current_scene_id"))
    current_room_id = room_map_id or _int_or_none(_system_value(conn, "current_room_id"))
    current_scene_id = room_scene_id or player_scene_id
    current_scene = _serialize_scene_for_players(conn, current_scene_id)

    claims = _character_claims_for_room(conn, room_id) if room_id else []
    room_limits = _room_seat_info(conn, room_row, claims) if room_row else {
        "max_players": 1,
        "claimed_player_count": 0,
        "claimed_character_count": 0,
        "player_slots_remaining": 1,
        "character_slots_remaining": 1,
        "playable_character_count": 1,
    }
    claims_by_character = {int(c["character_id"]): c for c in claims if c.get("character_id") is not None}
    characters = [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, role, hp, san, inventory, personality, status FROM characters ORDER BY role, name"
        ).fetchall()
    ]
    for char in characters:
        claim = claims_by_character.get(int(char["id"]))
        if claim:
            char["claim_locked"] = True
            char["claimed_by_player_id"] = claim.get("player_id", "")
            char["claimed_by_name"] = claim.get("display_name", "")
            char["claimed_at"] = claim.get("claimed_at", "")
        else:
            char["claim_locked"] = False
            char["claimed_by_player_id"] = ""
            char["claimed_by_name"] = ""
            char["claimed_at"] = ""
    active_characters = [c for c in characters if (c.get("status") or "active") != "hidden"]
    playable_characters = active_characters or characters
    map_room = _serialize_map_room(conn, current_room_id)
    return {
        "current_scene_id": current_scene_id,
        "current_scene": current_scene,
        "scene_image": _system_value(conn, "player_scene_image") or (current_scene or {}).get("scene_image", ""),
        "scene_prompt": _system_value(conn, "player_scene_prompt"),
        "scene_ai_text": _system_value(conn, "player_scene_ai_text"),
        "bgm_url": _system_value(conn, "player_bgm_url"),
        "bgm_name": _system_value(conn, "player_bgm_name"),
        "characters": active_characters,
        "playable_characters": playable_characters,
        "all_characters": characters,
        "character_claims": claims,
        "room_limits": room_limits,
        "current_room_id": current_room_id,
        "current_room": map_room,
        "updated_at": _now(),
    }


def _snapshot(conn, room_id: int, message_limit: int = 80) -> dict[str, Any]:
    room = conn.execute("SELECT * FROM multiplayer_rooms WHERE id=?", (room_id,)).fetchone()
    if not room:
        raise fastapi.HTTPException(status_code=404, detail="房间不存在")
    members = [
        _serialize_member(r)
        for r in conn.execute(
            "SELECT * FROM multiplayer_members WHERE room_id=? ORDER BY display_name",
            (room_id,),
        ).fetchall()
    ]
    messages = [
        _serialize_message(r)
        for r in conn.execute(
            """
            SELECT * FROM multiplayer_messages
            WHERE room_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (room_id, message_limit),
        ).fetchall()
    ]
    messages.reverse()
    tokens = [
        _serialize_token(r)
        for r in conn.execute(
            "SELECT * FROM multiplayer_tokens WHERE room_id=? ORDER BY kind, name",
            (room_id,),
        ).fetchall()
    ]
    docs = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM multiplayer_room_documents WHERE room_id=? ORDER BY id DESC",
            (room_id,),
        ).fetchall()
    ]
    claims = _character_claims_for_room(conn, room_id)
    checkpoint_count = conn.execute(
        "SELECT COUNT(*) FROM multiplayer_room_checkpoints WHERE room_id=?",
        (room_id,),
    ).fetchone()[0]
    return {
        "type": "snapshot",
        "room": _serialize_room_with_limits(conn, room, claims),
        "game_state": _build_game_state(conn, room),
        "members": members,
        "messages": messages,
        "tokens": tokens,
        "documents": docs,
        "character_claims": claims,
        "checkpoint_count": checkpoint_count,
    }


def _room_state_checkpoint(conn, room, label: str = "") -> int:
    """Save the room-local play state before an adjudicated turn mutates it."""
    room_id = int(room["id"])
    state_keys = [
        "player_current_scene_id",
        "player_scene_image",
        "player_scene_prompt",
        "player_scene_ai_text",
        "player_bgm_url",
        "player_bgm_name",
        "current_room_id",
    ]

    def rows(sql: str, *args):
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    system_state = {
        key: (row["value"] if row else "")
        for key in state_keys
        for row in [conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()]
    }
    from_message_id = conn.execute(
        "SELECT COALESCE(MAX(id),0) FROM multiplayer_messages WHERE room_id=?",
        (room_id,),
    ).fetchone()[0]
    snap = {
        "room": {
            "name": room["name"],
            "current_scene_id": room["current_scene_id"],
            "current_room_id": room["current_room_id"],
            "map_background_url": room["map_background_url"],
            "settings": room["settings"],
        },
        "characters": rows("SELECT * FROM characters"),
        "tokens": rows("SELECT * FROM multiplayer_tokens WHERE room_id=?", room_id),
        "system_state": system_state,
        "map_rooms_state": {
            str(r["id"]): r["state"]
            for r in rows("SELECT id, state FROM map_rooms")
        },
        "from_message_id": from_message_id,
    }
    cur = conn.execute(
        """
        INSERT INTO multiplayer_room_checkpoints (room_id, from_message_id, label, snapshot)
        VALUES (?,?,?,?)
        """,
        (room_id, from_message_id, label[:80], _dump_json(snap)),
    )
    conn.execute(
        """
        DELETE FROM multiplayer_room_checkpoints
        WHERE room_id=? AND id NOT IN (
            SELECT id FROM multiplayer_room_checkpoints
            WHERE room_id=?
            ORDER BY id DESC
            LIMIT 10
        )
        """,
        (room_id, room_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def _rollback_room_checkpoint(conn, room) -> dict[str, Any]:
    room_id = int(room["id"])
    cp = conn.execute(
        """
        SELECT id, from_message_id, snapshot
        FROM multiplayer_room_checkpoints
        WHERE room_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (room_id,),
    ).fetchone()
    if not cp:
        raise fastapi.HTTPException(status_code=400, detail="没有可返回的上一回合")

    snap = _load_json(cp["snapshot"], {})
    room_state = snap.get("room") or {}
    conn.execute(
        """
        UPDATE multiplayer_rooms
        SET name=COALESCE(?, name),
            current_scene_id=?,
            current_room_id=?,
            map_background_url=?,
            settings=COALESCE(?, settings),
            updated_at=?
        WHERE id=?
        """,
        (
            room_state.get("name"),
            room_state.get("current_scene_id"),
            room_state.get("current_room_id"),
            room_state.get("map_background_url", ""),
            room_state.get("settings"),
            _now(),
            room_id,
        ),
    )

    conn.execute("DELETE FROM characters")
    for c in snap.get("characters") or []:
        conn.execute(
            """
            INSERT INTO characters (id,name,role,hp,san,inventory,personality,status)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                c.get("id"),
                c.get("name", ""),
                c.get("role", ""),
                c.get("hp"),
                c.get("san"),
                c.get("inventory", ""),
                c.get("personality", ""),
                c.get("status", "active"),
            ),
        )

    conn.execute("DELETE FROM multiplayer_tokens WHERE room_id=?", (room_id,))
    for t in snap.get("tokens") or []:
        conn.execute(
            """
            INSERT INTO multiplayer_tokens
            (id, room_id, token_id, name, kind, owner_id, avatar_url, color, x, y, size,
             linked_room_id, linked_entity_id, linked_character_id, notes, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                t.get("id"),
                room_id,
                t.get("token_id", ""),
                t.get("name", "Token"),
                t.get("kind", "pc"),
                t.get("owner_id", ""),
                t.get("avatar_url", ""),
                t.get("color", "#f59e0b"),
                t.get("x", 120),
                t.get("y", 120),
                t.get("size", 48),
                t.get("linked_room_id"),
                t.get("linked_entity_id"),
                t.get("linked_character_id"),
                t.get("notes", ""),
                t.get("updated_at") or _now(),
            ),
        )

    for key, value in (snap.get("system_state") or {}).items():
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key,value) VALUES (?,?)",
            (key, value or ""),
        )

    for map_room_id, state in (snap.get("map_rooms_state") or {}).items():
        conn.execute(
            "UPDATE map_rooms SET state=? WHERE id=?",
            (state or "unknown", int(map_room_id)),
        )

    from_message_id = int(snap.get("from_message_id") or cp["from_message_id"] or 0)
    conn.execute(
        "DELETE FROM multiplayer_messages WHERE room_id=? AND id>?",
        (room_id, from_message_id),
    )
    conn.execute("DELETE FROM multiplayer_room_checkpoints WHERE id=?", (cp["id"],))
    conn.commit()
    return _snapshot(conn, room_id)


def _save_message(
    conn,
    room_id: int,
    sender_id: str,
    sender_name: str,
    kind: str,
    content: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cur = conn.execute(
        """
        INSERT INTO multiplayer_messages
        (room_id, sender_id, sender_name, kind, content, payload)
        VALUES (?,?,?,?,?,?)
        """,
        (
            room_id,
            (sender_id or "")[:80],
            (sender_name or "")[:40],
            (kind or "chat")[:20],
            (content or "")[:4000],
            _dump_json(payload or {}),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM multiplayer_messages WHERE id=?", (cur.lastrowid,)).fetchone()
    return _serialize_message(row)


def _remember_room_event(conn, room_code: str, text: str):
    if not _append_to_memory:
        return
    try:
        _append_to_memory(conn, f"[多人房间 {room_code}] {text}")
    except Exception as exc:
        _log.debug("room memory append skipped: %s", exc)


async def _broadcast(room_id: int, event: dict[str, Any]):
    dead: set[WebSocket] = set()
    for ws in list(_ws_clients_by_room.get(room_id, set())):
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _ws_clients_by_room.get(room_id, set()).discard(ws)
        _ws_meta.pop(ws, None)


async def _broadcast_game_state(conn, room):
    await _broadcast(room["id"], {"type": "game.state", "game_state": _build_game_state(conn, room)})


def _ensure_member(
    conn,
    room_id: int,
    req: JoinRoomRequest,
    member_token: str = "",
    room=None,
    room_token: str = "",
) -> tuple[dict[str, Any], str]:
    player_id = (req.player_id or secrets.token_hex(8))[:80]
    display_name = _normalize_display_name(req.display_name)
    color = req.color or _pick_color(player_id)
    client_id = _normalize_client_id(req.client_id)
    account_bound = _is_account_player_id(player_id)
    existing = conn.execute(
        "SELECT * FROM multiplayer_members WHERE room_id=? AND player_id=?",
        (room_id, player_id),
    ).fetchone()
    restored_by_account = bool(existing and account_bound)
    restored_by_client = False
    if existing and client_id:
        existing_client_id = _normalize_client_id(existing["client_id"])
        same_display_name = _display_name_key(existing["display_name"]) == _display_name_key(display_name)
        if existing_client_id == client_id or (not existing_client_id and same_display_name):
            restored_by_client = True
    if client_id and not account_bound:
        client_existing = conn.execute(
            """
            SELECT * FROM multiplayer_members
            WHERE room_id=? AND client_id=? AND player_id<>?
            ORDER BY id
            LIMIT 1
            """,
            (room_id, client_id, player_id),
        ).fetchone()
        if client_existing:
            existing = client_existing
            player_id = client_existing["player_id"]
            display_name = _normalize_display_name(client_existing["display_name"])
            color = client_existing["color"] or color
            restored_by_client = True
    if existing and _display_name_key(existing["display_name"]) != _display_name_key(display_name):
        existing_claim = conn.execute(
            "SELECT id FROM multiplayer_character_claims WHERE room_id=? AND player_id=? LIMIT 1",
            (room_id, player_id),
        ).fetchone()
        if existing_claim:
            raise fastapi.HTTPException(status_code=409, detail="角色已锁定后不能更改昵称")
    # 多人桌不再有真人 GM 席位；房间令牌仅作为后台同步/导入等管理权限使用。
    role = "player"
    if existing and existing["member_token_hash"]:
        if not _token_matches(member_token, existing["member_token_hash"]):
            if not (restored_by_client or restored_by_account):
                raise fastapi.HTTPException(status_code=403, detail="成员令牌无效，请重新以新身份加入")
            token_to_return = _new_token()
            member_token_hash = _hash_token(token_to_return)
        else:
            token_to_return = member_token
            member_token_hash = existing["member_token_hash"]
    else:
        if not account_bound:
            name_conflict = conn.execute(
                """
                SELECT player_id FROM multiplayer_members
                WHERE room_id=? AND lower(display_name)=lower(?) AND player_id<>?
                LIMIT 1
                """,
                (room_id, display_name, player_id),
            ).fetchone()
            if name_conflict:
                raise fastapi.HTTPException(status_code=409, detail="这个昵称已经在房间中，请使用不同昵称")
        if not existing and room is not None:
            seat_info = _room_seat_info(conn, room)
            if seat_info["joined_player_count"] >= seat_info["max_players"]:
                raise fastapi.HTTPException(
                    status_code=409,
                    detail=f"房间人数已满（{seat_info['joined_player_count']}/{seat_info['max_players']}）",
                )
        token_to_return = member_token or _new_token()
        member_token_hash = _hash_token(token_to_return)
    if existing and not account_bound:
        name_conflict = conn.execute(
            """
            SELECT player_id FROM multiplayer_members
            WHERE room_id=? AND lower(display_name)=lower(?) AND player_id<>?
            LIMIT 1
            """,
            (room_id, display_name, player_id),
        ).fetchone()
        if name_conflict:
            raise fastapi.HTTPException(status_code=409, detail="这个昵称已经在房间中，请使用不同昵称")
    conn.execute(
        """
        INSERT INTO multiplayer_members
        (room_id, player_id, display_name, role, color, client_id, member_token_hash, connected, last_seen)
        VALUES (?,?,?,?,?,?,?,1,?)
        ON CONFLICT(room_id, player_id) DO UPDATE SET
            display_name=excluded.display_name,
            role=excluded.role,
            color=excluded.color,
            client_id=CASE
                WHEN excluded.client_id<>'' THEN excluded.client_id
                ELSE multiplayer_members.client_id
            END,
            member_token_hash=excluded.member_token_hash,
            connected=1,
            last_seen=excluded.last_seen
        """,
        (room_id, player_id, display_name, role, color[:20], client_id, member_token_hash, _now()),
    )
    conn.execute(
        "UPDATE multiplayer_character_claims SET display_name=? WHERE room_id=? AND player_id=?",
        (display_name, room_id, player_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM multiplayer_members WHERE room_id=? AND player_id=?",
        (room_id, player_id),
    ).fetchone()
    return _serialize_member(row), token_to_return


def _pick_color(seed: str) -> str:
    palette = ["#7dd3fc", "#f59e0b", "#a7f3d0", "#fda4af", "#c4b5fd", "#fde68a", "#93c5fd"]
    return palette[sum(ord(ch) for ch in seed or "player") % len(palette)]


def _normalize_dice_expression(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^[./!！。]\s*", "", text)
    if text.lower().startswith("r "):
        text = text[1:].strip()
    elif text.lower().startswith("roll "):
        text = text[4:].strip()
    elif text.lower().startswith("r") and re.match(r"^r\d", text, re.I):
        text = text[1:].strip()
    return text or "1d100"


def _single_line(text: str) -> str:
    return re.sub(r"\s*[\r\n]+\s*", " ; ", str(text or "")).strip()


def _roll_structured(req: DiceRollRequest, room_code: str) -> dict[str, Any]:
    if not parse_dice_expression:
        raise fastapi.HTTPException(status_code=500, detail="TRPG 骰子模块不可用")
    expression = _normalize_dice_expression(req.expression)
    total, detail = parse_dice_expression(expression)
    if total is None:
        raise fastapi.HTTPException(status_code=400, detail=f"骰子表达式错误：{detail}")

    numeric_total = int(total) if isinstance(total, (int, float)) and float(total).is_integer() else total
    outcome = ""
    rank = None
    if req.skill_value is not None and isinstance(numeric_total, int) and get_roll_result:
        outcome = get_roll_result(int(numeric_total), int(req.skill_value), room_code)
        rank = _coc_rank_label(outcome)
    elif req.target_number is not None and isinstance(numeric_total, (int, float)):
        outcome = "成功" if numeric_total >= req.target_number else "失败"
        rank = "success" if numeric_total >= req.target_number else "failure"

    reason = req.reason.strip()
    actor = req.actor_name.strip() or "玩家"
    skill = req.skill_name.strip()
    summary_bits = [f"{actor} 掷骰 {expression}", f"结果 {numeric_total}"]
    if skill:
        summary_bits.append(f"检定 {skill}")
    if req.skill_value is not None:
        summary_bits.append(f"目标 {req.skill_value}")
    if req.target_number is not None:
        summary_bits.append(f"DC {req.target_number}")
    if outcome:
        summary_bits.append(outcome)
    if reason:
        summary_bits.append(f"原因：{reason}")

    return {
        "expression": expression,
        "total": numeric_total,
        "detail": _single_line(detail),
        "actor_id": req.actor_id,
        "actor_name": actor,
        "character_id": req.character_id,
        "character_name": req.character_name or actor,
        "reason": reason,
        "skill_name": skill,
        "skill_value": req.skill_value,
        "target_number": req.target_number,
        "outcome": outcome,
        "rank": rank,
        "summary": " | ".join(str(x) for x in summary_bits if x),
        "created_at": _now(),
    }


def _coc_rank_label(text: str) -> str:
    if "大成功" in text:
        return "critical_success"
    if "极难" in text:
        return "extreme_success"
    if "困难" in text:
        return "hard_success"
    if "成功" in text and "大成功" not in text:
        return "success"
    if "大失败" in text:
        return "fumble"
    return "failure"


def _deterministic_dice_feedback(room_name: str, roll: dict[str, Any]) -> str:
    outcome = roll.get("outcome") or "等待主持人裁定"
    reason = f"（{roll['reason']}）" if roll.get("reason") else ""
    if roll.get("skill_name"):
        return (
            f"{roll['actor_name']}进行{roll['skill_name']}检定{reason}，"
            f"骰点为 {roll['total']}，判定：{outcome}。"
        )
    return f"{roll['actor_name']}在「{room_name}」掷出 {roll['expression']} = {roll['total']}。{outcome}"


def _strip_json_fence(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        if len(parts) >= 2:
            cleaned = parts[1].strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
    return cleaned


def _apply_character_updates(conn, updates: Any) -> list[dict[str, Any]]:
    if not isinstance(updates, list):
        return []
    allowed_statuses = {"active", "hidden", "benched", "dead"}
    applied: list[dict[str, Any]] = []
    for item in updates[:12]:
        if not isinstance(item, dict):
            continue
        char_id = _int_or_none(item.get("id") or item.get("character_id"))
        name = str(item.get("name") or "").strip()
        row = None
        if char_id:
            row = conn.execute("SELECT * FROM characters WHERE id=?", (char_id,)).fetchone()
        if not row and name:
            row = conn.execute("SELECT * FROM characters WHERE name=?", (name[:50],)).fetchone()
        if not row:
            continue

        before = dict(row)
        hp = before.get("hp")
        san = before.get("san")
        status = before.get("status") or "active"
        inventory = before.get("inventory") or ""

        try:
            if item.get("hp") is not None:
                hp = max(0, min(999, int(item.get("hp"))))
            if item.get("san") is not None:
                san = max(0, min(999, int(item.get("san"))))
        except (TypeError, ValueError):
            continue
        if item.get("status"):
            next_status = str(item.get("status")).strip().lower()
            if next_status in allowed_statuses:
                status = next_status
        if item.get("inventory") is not None:
            inventory = str(item.get("inventory") or "")[:500]

        if hp == before.get("hp") and san == before.get("san") and status == (before.get("status") or "active") and inventory == (before.get("inventory") or ""):
            continue

        conn.execute(
            "UPDATE characters SET hp=?, san=?, inventory=?, status=? WHERE id=?",
            (hp, san, inventory, status, before["id"]),
        )
        applied.append(
            {
                "id": before["id"],
                "name": before["name"],
                "before": {
                    "hp": before.get("hp"),
                    "san": before.get("san"),
                    "inventory": before.get("inventory") or "",
                    "status": before.get("status") or "active",
                },
                "after": {"hp": hp, "san": san, "inventory": inventory, "status": status},
                "reason": str(item.get("reason") or "")[:240],
            }
        )
    if applied:
        conn.commit()
    return applied


def _format_character_changes(changes: list[dict[str, Any]]) -> str:
    lines = []
    for change in changes:
        before = change["before"]
        after = change["after"]
        bits = []
        for key, label in (("hp", "HP"), ("san", "SAN"), ("status", "状态"), ("inventory", "物品")):
            if before.get(key) != after.get(key):
                bits.append(f"{label}: {before.get(key)} -> {after.get(key)}")
        if bits:
            reason = f"（{change['reason']}）" if change.get("reason") else ""
            lines.append(f"{change['name']}：" + "，".join(bits) + reason)
    return "\n".join(lines)


def _ai_adjudicate_json(system_prompt: str, user_payload: dict[str, Any], fallback: str) -> dict[str, Any]:
    if not ai_provider.is_configured():
        return {"narration": fallback, "character_updates": []}
    try:
        resp = ai_provider.chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "请只返回 json 对象。\n" + json.dumps(user_payload, ensure_ascii=False)},
            ],
            temperature=0.55,
            max_tokens=520,
            json_mode=True,
        )
        raw = _strip_json_fence(resp.choices[0].message.content or "")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("AI did not return an object")
        data["narration"] = str(data.get("narration") or fallback).strip()[:1800]
        if not isinstance(data.get("character_updates"), list):
            data["character_updates"] = []
        data["bgm_name"] = _valid_bgm_name(data.get("bgm_name"))
        return data
    except Exception as exc:
        _log.warning("AI-KP adjudication failed: %s", exc)
        return {"narration": fallback, "character_updates": []}


async def _ai_adjudicate_json_async(system_prompt: str, user_payload: dict[str, Any], fallback: str) -> dict[str, Any]:
    if not ai_provider.is_configured():
        return {"narration": fallback, "character_updates": []}
    return await asyncio.to_thread(_ai_adjudicate_json, system_prompt, user_payload, fallback)


def _roll_adjudication_request(conn, room, roll: dict[str, Any], context: str) -> tuple[str, dict[str, Any], str]:
    room_name = room["name"]
    fallback = _deterministic_dice_feedback(room_name, roll)
    state = _build_game_state(conn, room)
    system_prompt = (
        "你是多人联机 TRPG 的 AI-KP。主持人职责是描述世界、扮演 NPC、裁定规则、"
        "根据骰点和玩家行动给出公平后果。严格返回 JSON / json："
        "{\"narration\":\"1-3句给所有玩家看的叙事裁定\","
        "\"character_updates\":[{\"id\":角色ID或省略,\"name\":\"角色名\","
        "\"hp\":新的HP可省略,\"san\":新的SAN可省略,\"status\":\"active|hidden|benched|dead\"可省略,"
        "\"inventory\":\"新的物品/状态文本可省略\",\"reason\":\"改动原因\"}],"
        f"\"bgm_name\":\"可选，必须从这些曲名中选择：{BGM_TRACK_NAMES_TEXT}\"}}。"
        "默认只裁定 roll.character_id 对应角色的直接后果；只有行动明确影响其他角色时才写入其他角色。"
        "只能改已有角色；没有明确后果时 character_updates 返回空数组；不要替玩家继续行动。"
        "bgm_name 可省略；若输出，必须选择最贴近当前氛围的一首，不要自造曲名。"
    )
    user_payload = {
        "room": room_name,
        "roll": roll,
        "context": _trim_token_text(context, "multiplayer_context_chars", keep_tail=True),
        "game_state": state,
    }
    return system_prompt, user_payload, fallback


def _ai_roll_adjudication(conn, room, roll: dict[str, Any], context: str) -> dict[str, Any]:
    system_prompt, user_payload, fallback = _roll_adjudication_request(conn, room, roll, context)
    return _ai_adjudicate_json(system_prompt, user_payload, fallback)


async def _ai_roll_adjudication_async(conn, room, roll: dict[str, Any], context: str) -> dict[str, Any]:
    system_prompt, user_payload, fallback = _roll_adjudication_request(conn, room, roll, context)
    return await _ai_adjudicate_json_async(system_prompt, user_payload, fallback)


def _player_action_adjudication_request(conn, room, req: PlayerActionRequest) -> tuple[str, dict[str, Any], str]:
    room_name = room["name"]
    action = req.action.strip()
    fallback = f"AI-KP 记录了{req.actor_name}的行动：{action}。等待下一步裁定。"
    state = _build_game_state(conn, room)
    system_prompt = (
        "你是多人联机 TRPG 的 AI-KP。玩家只控制自己的角色；你负责建立场景、扮演 NPC/环境、"
        "判断行动是否可行、是否需要掷骰，并叙述当前即时后果。严格返回 JSON / json："
        "{\"narration\":\"给所有玩家看的主持人回应\","
        "\"needs_roll\":false,\"suggested_roll\":\"可选骰式或检定\","
        "\"character_updates\":[{\"id\":角色ID或省略,\"name\":\"角色名\",\"hp\":新的HP可省略,"
        "\"san\":新的SAN可省略,\"status\":\"active|hidden|benched|dead\"可省略,"
        "\"inventory\":\"新的物品/状态文本可省略\",\"reason\":\"改动原因\"}],"
        f"\"bgm_name\":\"可选，必须从这些曲名中选择：{BGM_TRACK_NAMES_TEXT}\"}}。"
        "默认只裁定 actor.character_id 对应角色的即时后果，不要把其他玩家的开场、行动或剧情推进混在一起。"
        "没有明确规则后果时不要改角色；需要检定时提出检定而不是代替玩家宣告成功。"
        "bgm_name 可省略；若输出，必须选择最贴近当前氛围的一首，不要自造曲名。"
    )
    user_payload = {
        "room": room_name,
        "actor": {
            "id": req.actor_id,
            "name": req.actor_name,
            "character_id": req.character_id,
            "character_name": req.character_name,
        },
        "action_type": req.action_type,
        "action": action,
        "context": _trim_token_text(req.context, "multiplayer_context_chars", keep_tail=True),
        "game_state": state,
    }
    return system_prompt, user_payload, fallback


def _ai_player_action_adjudication(conn, room, req: PlayerActionRequest) -> dict[str, Any]:
    system_prompt, user_payload, fallback = _player_action_adjudication_request(conn, room, req)
    return _ai_adjudicate_json(system_prompt, user_payload, fallback)


async def _ai_player_action_adjudication_async(conn, room, req: PlayerActionRequest) -> dict[str, Any]:
    system_prompt, user_payload, fallback = _player_action_adjudication_request(conn, room, req)
    return await _ai_adjudicate_json_async(system_prompt, user_payload, fallback)


def _ai_dice_feedback(room_name: str, roll: dict[str, Any], context: str) -> str:
    if not ai_provider.is_configured():
        return _deterministic_dice_feedback(room_name, roll)
    prompt = (
        "你是多人联机 TRPG 的 AI-KP。请根据骰点生成 1-2 句短叙事反馈，"
        "承认骰点和成败，不要改写规则结果，不要继续替玩家行动。"
    )
    user = {
        "room": room_name,
        "roll": roll,
        "context": _trim_token_text(context, "multiplayer_context_chars", keep_tail=True),
    }
    try:
        resp = ai_provider.chat_completion(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            temperature=0.55,
            max_tokens=220,
        )
        return (resp.choices[0].message.content or "").strip() or _deterministic_dice_feedback(room_name, roll)
    except Exception as exc:
        _log.warning("AI dice feedback failed: %s", exc)
        return _deterministic_dice_feedback(room_name, roll)


async def _roll_and_record(conn, room, req: DiceRollRequest) -> dict[str, Any]:
    roll = _roll_structured(req, room["code"])
    actor_payload = {
        "actor_id": roll.get("actor_id", ""),
        "actor_name": roll.get("actor_name", ""),
        "character_id": roll.get("character_id"),
        "character_name": roll.get("character_name", ""),
    }
    dice_msg = _save_message(
        conn,
        room["id"],
        roll["actor_id"],
        roll["actor_name"],
        "dice",
        roll["summary"],
        {"source": "dice", **actor_payload, **roll},
    )
    adjudication = await _ai_roll_adjudication_async(conn, room, roll, req.context) if req.ask_ai else {
        "narration": _deterministic_dice_feedback(room["name"], roll),
        "character_updates": [],
    }
    changes = _apply_character_updates(conn, adjudication.get("character_updates"))
    feedback = adjudication.get("narration") or _deterministic_dice_feedback(room["name"], roll)
    bgm_name = _apply_bgm_recommendation(conn, adjudication)
    ai_msg = _save_message(
        conn,
        room["id"],
        "ai-kp",
        "AI-KP",
        "ai",
        feedback,
        {"source": "dice", **actor_payload, "roll": roll, "character_updates": changes, "bgm_name": bgm_name},
    )
    change_msg = None
    if changes:
        change_msg = _save_message(
            conn,
            room["id"],
            "ai-kp",
            "AI-KP",
            "state",
            _format_character_changes(changes),
            {"source": "character_updates", **actor_payload, "changes": changes},
        )
    _remember_room_event(conn, room["code"], f"{roll['summary']}；AI反馈：{feedback}")
    await _broadcast(room["id"], {"type": "message.created", "message": dice_msg})
    await _broadcast(room["id"], {"type": "message.created", "message": ai_msg})
    if change_msg:
        await _broadcast(room["id"], {"type": "message.created", "message": change_msg})
    await _broadcast_game_state(conn, room)
    return {
        "status": "success",
        "roll": roll,
        "message": dice_msg,
        "ai_message": ai_msg,
        "state_message": change_msg,
        "character_updates": changes,
        "game_state": _build_game_state(conn, room),
    }


async def _adjudicate_player_action(conn, room, req: PlayerActionRequest) -> dict[str, Any]:
    action = req.action.strip()
    if not action:
        raise fastapi.HTTPException(status_code=400, detail="行动不能为空")
    member = conn.execute(
        "SELECT role FROM multiplayer_members WHERE room_id=? AND player_id=?",
        (room["id"], req.actor_id),
    ).fetchone()
    claim = None
    if not member or member["role"] != "gm":
        claim = _single_character_claim_for_player(conn, room, req.actor_id, "player")
        req = _request_with_claim_actor(req, claim)
    actor_payload = _claim_actor_payload(req.actor_id, req.actor_name, claim)
    _room_state_checkpoint(conn, room, "action")
    player_msg = _save_message(
        conn,
        room["id"],
        req.actor_id,
        req.actor_name,
        "action",
        action,
        {
            "source": "player_action",
            **actor_payload,
            "action_type": req.action_type,
            "context": req.context,
            "option_id": req.option_id,
            "next_node_id": req.next_node_id,
            "option_text": req.option_text,
        },
    )
    await _broadcast(room["id"], {"type": "message.created", "message": player_msg})
    adjudication = await _ai_player_action_adjudication_async(conn, room, req)
    changes = _apply_character_updates(conn, adjudication.get("character_updates"))
    narration = adjudication.get("narration") or f"AI-KP 记录了{req.actor_name}的行动：{action}。"
    bgm_name = _apply_bgm_recommendation(conn, adjudication)
    payload = {
        "source": "player_action",
        **actor_payload,
        "action": action,
        "action_type": req.action_type,
        "character_updates": changes,
        "bgm_name": bgm_name,
    }
    if adjudication.get("needs_roll"):
        payload["needs_roll"] = True
        payload["suggested_roll"] = str(adjudication.get("suggested_roll") or "")[:120]
    ai_msg = _save_message(conn, room["id"], "ai-kp", "AI-KP", "ai", narration, payload)
    change_msg = None
    if changes:
        change_msg = _save_message(
            conn,
            room["id"],
            "ai-kp",
            "AI-KP",
            "state",
            _format_character_changes(changes),
            {"source": "character_updates", **actor_payload, "changes": changes},
        )
    room = conn.execute("SELECT * FROM multiplayer_rooms WHERE id=?", (room["id"],)).fetchone() or room
    scene_msg = _apply_action_scene_advance(conn, room, req, actor_payload)
    if scene_msg:
        room = conn.execute("SELECT * FROM multiplayer_rooms WHERE id=?", (room["id"],)).fetchone()
    _remember_room_event(conn, room["code"], f"{req.actor_name}行动：{action[:300]}；AI反馈：{narration[:300]}")
    await _broadcast(room["id"], {"type": "message.created", "message": ai_msg})
    if change_msg:
        await _broadcast(room["id"], {"type": "message.created", "message": change_msg})
    if scene_msg:
        await _broadcast(room["id"], {"type": "message.created", "message": scene_msg})
    await _broadcast_game_state(conn, room)
    return {
        "status": "success",
        "message": player_msg,
        "ai_message": ai_msg,
        "state_message": change_msg,
        "scene_message": scene_msg,
        "character_updates": changes,
        "game_state": _build_game_state(conn, room),
    }


async def _extract_upload_text(file: UploadFile) -> tuple[str, str]:
    filename = file.filename or "scenario.txt"
    raw = await file.read()
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        try:
            import pypdf
        except ImportError as exc:
            raise fastapi.HTTPException(status_code=500, detail="PDF 解析需要安装 pypdf") from exc
        try:
            import io

            reader = pypdf.PdfReader(io.BytesIO(raw))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:
            raise fastapi.HTTPException(status_code=400, detail=f"PDF 解析失败：{exc}") from exc
    elif suffix in {".txt", ".md", ".markdown", ""}:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gbk")
            except Exception as exc:
                raise fastapi.HTTPException(status_code=400, detail="文件编码无法识别") from exc
    else:
        raise fastapi.HTTPException(status_code=400, detail="仅支持 TXT、Markdown、PDF 剧本")
    text = text.strip()
    if not text:
        raise fastapi.HTTPException(status_code=400, detail="文件内容为空")
    return filename, text


@multiplayer_router.get("/api/multiplayer/health")
def multiplayer_health():
    return {
        "status": "success",
        "dice_available": bool(parse_dice_expression),
        "db_configured": bool(_db_file),
    }


@multiplayer_router.get("/api/multiplayer/rooms")
def list_rooms():
    with safe_db() as conn:
        rooms = [
            _serialize_room_with_limits(conn, r)
            for r in conn.execute("SELECT * FROM multiplayer_rooms ORDER BY updated_at DESC, id DESC").fetchall()
        ]
    return {"status": "success", "rooms": rooms}


@multiplayer_router.post("/api/multiplayer/rooms")
def create_room(req: RoomCreateRequest):
    with safe_db() as conn:
        code = _room_code()
        while conn.execute("SELECT id FROM multiplayer_rooms WHERE code=?", (code,)).fetchone():
            code = _room_code()
        room_token = _new_token()
        settings = _normalize_room_settings(conn, req.settings)
        cur = conn.execute(
            """
            INSERT INTO multiplayer_rooms (code, name, gm_name, campaign_path, settings, gm_token_hash)
            VALUES (?,?,?,?,?,?)
            """,
            (
                code,
                req.name[:80],
                req.gm_name[:40],
                req.campaign_path[:240],
                _dump_json(settings),
                _hash_token(room_token),
            ),
        )
        room_id = cur.lastrowid
        conn.commit()
        return {"status": "success", "room_token": room_token, **_snapshot(conn, room_id)}


@multiplayer_router.get("/api/multiplayer/rooms/{room_code}")
def get_room(room_code: str):
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        return {"status": "success", **_snapshot(conn, room["id"])}


@multiplayer_router.patch("/api/multiplayer/rooms/{room_code}")
async def patch_room(room_code: str, req: RoomPatchRequest, request: Request):
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        _require_gm(room, _room_token_from_request(request))
        settings = _load_json(room["settings"], {})
        if req.settings:
            settings.update(req.settings)
        settings = _normalize_room_settings(conn, settings)
        campaign_path = str(settings.get("campaign_path") or "").strip()[:240] or None
        conn.execute(
            """
            UPDATE multiplayer_rooms
            SET name=COALESCE(?, name),
                campaign_path=COALESCE(?, campaign_path),
                current_scene_id=COALESCE(?, current_scene_id),
                current_room_id=COALESCE(?, current_room_id),
                settings=?,
                updated_at=?
            WHERE id=?
            """,
            (
                req.name,
                campaign_path,
                req.current_scene_id,
                req.current_room_id,
                _dump_json(settings),
                _now(),
                room["id"],
            ),
        )
        conn.commit()
        snap = _snapshot(conn, room["id"])
    await _broadcast(room["id"], {"type": "room.updated", "snapshot": snap})
    return {"status": "success", **snap}


@multiplayer_router.post("/api/multiplayer/rooms/{room_code}/join")
async def join_room(room_code: str, req: JoinRoomRequest, request: Request):
    account = require_account_from_request(request)
    req = _join_request_for_account(req, account)
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        member, member_token = _ensure_member(
            conn,
            room["id"],
            req,
            _member_token_from_request(request),
            room,
            _room_token_from_request(request),
        )
        snap = _snapshot(conn, room["id"])
    await _broadcast(room["id"], {"type": "member.updated", "member": member})
    return {"status": "success", "member": member, "member_token": member_token, **snap}


@multiplayer_router.get("/api/multiplayer/rooms/{room_code}/messages")
def get_messages(room_code: str, limit: int = Query(default=120, ge=1, le=500)):
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        rows = conn.execute(
            "SELECT * FROM multiplayer_messages WHERE room_id=? ORDER BY id DESC LIMIT ?",
            (room["id"], limit),
        ).fetchall()
        messages = [_serialize_message(r) for r in rows]
        messages.reverse()
    return {"status": "success", "messages": messages}


@multiplayer_router.post("/api/multiplayer/rooms/{room_code}/messages")
async def post_message(room_code: str, req: MessageCreateRequest, request: Request):
    content = (req.content or "").strip()
    if not content:
        raise fastapi.HTTPException(status_code=400, detail="消息不能为空")
    room_token = _room_token_from_request(request)
    if not room_token:
        account = require_account_from_request(request)
        req.sender_id = account_player_id(account)
        req.sender_name = _normalize_display_name(account.get("display_name") or account.get("username") or req.sender_name)
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        role = _require_member_or_gm(
            conn,
            room,
            req.sender_id,
            _member_token_from_request(request),
            room_token,
        )
        claim = _single_character_claim_for_player(conn, room, req.sender_id, role)
        sender_name = _claim_actor_name(claim, req.sender_name)
        actor_payload = _claim_actor_payload(req.sender_id, sender_name, claim)
        if re.match(r"^[./!！。]\s*r(?:oll)?\b|^[./!！。]\s*r\d", content, re.I):
            dice_req = DiceRollRequest(
                expression=content,
                actor_id=req.sender_id,
                actor_name=sender_name,
                character_id=actor_payload.get("character_id"),
                character_name=actor_payload.get("character_name", ""),
                reason=req.payload.get("reason", ""),
                skill_name=req.payload.get("skill_name", ""),
                skill_value=req.payload.get("skill_value"),
                target_number=req.payload.get("target_number"),
                ask_ai=bool(req.payload.get("ask_ai", True)),
                context=req.payload.get("context", ""),
            )
            _room_state_checkpoint(conn, room, "dice")
            return await _roll_and_record(conn, room, dice_req)
        msg_payload = {**(req.payload or {}), **actor_payload}
        msg = _save_message(conn, room["id"], req.sender_id, sender_name, req.kind, content, msg_payload)
        _remember_room_event(conn, room["code"], f"{sender_name}: {content[:300]}")
    await _broadcast(room["id"], {"type": "message.created", "message": msg})
    return {"status": "success", "message": msg}


@multiplayer_router.post("/api/multiplayer/rooms/{room_code}/dice")
async def roll_dice(room_code: str, req: DiceRollRequest, request: Request):
    room_token = _room_token_from_request(request)
    if not room_token:
        req.actor_id = _account_player_id_from_request(request)
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        role = _require_member_or_gm(
            conn,
            room,
            req.actor_id,
            _member_token_from_request(request),
            room_token,
        )
        claim = _single_character_claim_for_player(conn, room, req.actor_id, role)
        req = _request_with_claim_actor(req, claim)
        _room_state_checkpoint(conn, room, "dice")
        return await _roll_and_record(conn, room, req)


@multiplayer_router.post("/api/multiplayer/rooms/{room_code}/action")
async def submit_player_action(room_code: str, req: PlayerActionRequest, request: Request):
    room_token = _room_token_from_request(request)
    if not room_token:
        req.actor_id = _account_player_id_from_request(request)
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        _require_member_or_gm(
            conn,
            room,
            req.actor_id,
            _member_token_from_request(request),
            room_token,
        )
        return await _adjudicate_player_action(conn, room, req)


@multiplayer_router.get("/api/multiplayer/rooms/{room_code}/state")
def get_room_game_state(room_code: str):
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        return {"status": "success", "game_state": _build_game_state(conn, room)}


@multiplayer_router.post("/api/multiplayer/rooms/{room_code}/rollback")
async def rollback_room_turn(room_code: str, req: RoomRollbackRequest, request: Request):
    room_token = _room_token_from_request(request)
    if not room_token:
        req.player_id = _account_player_id_from_request(request)
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        _require_member_or_gm(
            conn,
            room,
            req.player_id,
            _member_token_from_request(request),
            room_token,
        )
        snap = _rollback_room_checkpoint(conn, room)
    await _broadcast(room["id"], {"type": "room.updated", "snapshot": snap})
    return {"status": "success", **snap}


@multiplayer_router.post("/api/multiplayer/rooms/{room_code}/characters/claim")
async def claim_room_characters(room_code: str, req: CharacterClaimRequest, request: Request):
    character_ids: list[int] = []
    try:
        for cid in req.character_ids:
            normalized_id = int(cid)
            if normalized_id > 0 and normalized_id not in character_ids:
                character_ids.append(normalized_id)
            if len(character_ids) >= 6:
                break
    except (TypeError, ValueError):
        raise fastapi.HTTPException(status_code=400, detail="角色 ID 无效") from None
    if not character_ids:
        raise fastapi.HTTPException(status_code=400, detail="请至少选择一个角色")
    if len(character_ids) != 1:
        raise fastapi.HTTPException(status_code=400, detail="每位玩家只能选择一个角色")
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        room_token = _room_token_from_request(request)
        player_id = (req.player_id or "").strip()[:80]
        if not room_token:
            player_id = _account_player_id_from_request(request)
        if not player_id:
            raise fastapi.HTTPException(status_code=400, detail="缺少玩家身份")
        _require_member_or_gm(
            conn,
            room,
            player_id,
            _member_token_from_request(request),
            room_token,
        )
        member = conn.execute(
            "SELECT * FROM multiplayer_members WHERE room_id=? AND player_id=?",
            (room["id"], player_id),
        ).fetchone()
        display_name = _normalize_display_name(member["display_name"] if member else player_id)
        _normalize_single_character_claims(conn, int(room["id"]))

        placeholders = ",".join("?" for _ in character_ids)
        rows = conn.execute(
            f"SELECT id, name, role, hp, san, inventory, personality, status FROM characters WHERE id IN ({placeholders})",
            character_ids,
        ).fetchall()
        row_by_id = {int(r["id"]): r for r in rows}
        missing_ids = [cid for cid in character_ids if cid not in row_by_id]
        if missing_ids:
            raise fastapi.HTTPException(status_code=404, detail=f"角色不存在：{missing_ids[0]}")

        existing_claims = _character_claims_for_player(conn, room["id"], player_id)
        existing_ids = [int(c["character_id"]) for c in existing_claims]
        if not _is_account_player_id(player_id):
            existing_name_claims = [
                claim
                for claim in _character_claims_for_room(conn, room["id"])
                if _display_name_key(claim.get("display_name")) == _display_name_key(display_name)
            ]
            existing_name_ids = [int(c["character_id"]) for c in existing_name_claims if c.get("character_id") is not None]
            if existing_name_ids and set(existing_name_ids) != set(character_ids):
                raise fastapi.HTTPException(status_code=409, detail="该昵称已经锁定角色，不能重新选择")
        if existing_ids and set(existing_ids) != set(character_ids):
            raise fastapi.HTTPException(status_code=409, detail="角色已锁定，不能重新选择")
        if not existing_ids:
            seat_info = _room_seat_info(conn, room)
            if seat_info["claimed_character_count"] + len(character_ids) > seat_info["max_players"]:
                raise fastapi.HTTPException(
                    status_code=409,
                    detail=f"角色席位不足（已确认 {seat_info['claimed_character_count']}/{seat_info['max_players']}，本次选择 {len(character_ids)} 个）",
                )

        conflict_rows = conn.execute(
            f"""
            SELECT c.character_id, c.player_id, c.display_name, ch.name AS character_name
            FROM multiplayer_character_claims c
            LEFT JOIN characters ch ON ch.id=c.character_id
            WHERE c.room_id=? AND c.character_id IN ({placeholders}) AND c.player_id<>?
            """,
            [room["id"], *character_ids, player_id],
        ).fetchall()
        if conflict_rows:
            conflict_text = "、".join(
                f"{r['character_name'] or r['character_id']}（{r['display_name'] or r['player_id']}）"
                for r in conflict_rows
            )
            raise fastapi.HTTPException(status_code=409, detail=f"角色已被选择：{conflict_text}")

        rows_in_order = [row_by_id[cid] for cid in character_ids]
        names = [r["name"] for r in rows_in_order]
        first_claim = not existing_ids
        if first_claim:
            for cid in character_ids:
                conn.execute(
                    """
                    INSERT INTO multiplayer_character_claims
                    (room_id, character_id, player_id, display_name, claimed_at)
                    VALUES (?,?,?,?,?)
                    """,
                    (room["id"], cid, player_id, display_name, _now()),
                )

        character_id = int(rows_in_order[0]["id"])
        character_name = str(rows_in_order[0]["name"] or f"角色{character_id}")
        notes = json.dumps(
            {
                "character_id": character_id,
                "character_name": character_name,
                "character_ids": character_ids,
                "character_names": names,
                "locked": True,
            },
            ensure_ascii=False,
        )
        token_id = f"pc_{player_id}"
        conn.execute(
            """
            INSERT INTO multiplayer_tokens
            (room_id, token_id, name, kind, owner_id, avatar_url, color, x, y, size,
             linked_room_id, linked_entity_id, linked_character_id, notes, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(room_id, token_id) DO UPDATE SET
                name=excluded.name,
                kind=excluded.kind,
                owner_id=excluded.owner_id,
                linked_character_id=excluded.linked_character_id,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (
                room["id"],
                token_id,
                character_name[:80],
                "pc",
                player_id,
                "",
                (member["color"] if member else "#7dd3fc")[:20],
                160,
                160,
                48,
                _int_or_none(room["current_room_id"]),
                None,
                character_id,
                notes,
                _now(),
            ),
        )
        msg = None
        if first_claim:
            opening_text = _character_opening_text(conn, room, rows_in_order[0])
            opening_scene_id = _current_scene_id_for_room(conn, room)
            msg = _save_message(
                conn,
                room["id"],
                player_id,
                character_name[:40],
                "system",
                opening_text,
                {
                    "source": "character_opening",
                    "actor_id": player_id,
                    "actor_name": character_name[:40],
                    "character_id": character_id,
                    "character_name": character_name,
                    "scene_id": opening_scene_id,
                    "opening": opening_text,
                    "locked": True,
                },
            )
        conn.commit()
        token = _serialize_token(conn.execute("SELECT * FROM multiplayer_tokens WHERE room_id=? AND token_id=?", (room["id"], token_id)).fetchone())
        snap = _snapshot(conn, room["id"])
    await _broadcast(room["id"], {"type": "token.updated", "token": token})
    if msg:
        await _broadcast(room["id"], {"type": "message.created", "message": msg})
    await _broadcast(room["id"], {"type": "room.updated", "snapshot": snap})
    return {"status": "success", "message": msg, "token": token, **snap}


@multiplayer_router.post("/api/multiplayer/rooms/{room_code}/events")
async def post_ai_kp_event(room_code: str, req: AiEventRequest, request: Request):
    content = (req.content or "").strip()
    if not content:
        raise fastapi.HTTPException(status_code=400, detail="事件内容不能为空")
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        _require_gm(room, _room_token_from_request(request))
        _room_state_checkpoint(conn, room, "gm-event")
        kind = req.kind if req.kind in {"ai", "state", "system", "dice"} else "ai"
        msg = _save_message(
            conn,
            room["id"],
            "ai-kp",
            req.sender_name or "AI-KP",
            kind,
            content,
            req.payload,
        )
        _remember_room_event(conn, room["code"], f"AI-KP事件：{content[:300]}")
        game_state = _build_game_state(conn, room)
    await _broadcast(room["id"], {"type": "message.created", "message": msg})
    if req.broadcast_state:
        await _broadcast(room["id"], {"type": "game.state", "game_state": game_state})
    return {"status": "success", "message": msg, "game_state": game_state}


@multiplayer_router.get("/api/multiplayer/rooms/{room_code}/tokens")
def get_tokens(room_code: str):
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        rows = conn.execute(
            "SELECT * FROM multiplayer_tokens WHERE room_id=? ORDER BY kind, name",
            (room["id"],),
        ).fetchall()
    return {"status": "success", "tokens": [_serialize_token(r) for r in rows]}


@multiplayer_router.post("/api/multiplayer/rooms/{room_code}/tokens")
async def upsert_token(room_code: str, req: TokenUpsertRequest, request: Request):
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        _require_member_or_gm(
            conn,
            room,
            req.owner_id,
            _member_token_from_request(request),
            _room_token_from_request(request),
        )
        token_id = req.token_id or secrets.token_hex(6)
        conn.execute(
            """
            INSERT INTO multiplayer_tokens
            (room_id, token_id, name, kind, owner_id, avatar_url, color, x, y, size,
             linked_room_id, linked_entity_id, linked_character_id, notes, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(room_id, token_id) DO UPDATE SET
                name=excluded.name,
                kind=excluded.kind,
                owner_id=excluded.owner_id,
                avatar_url=excluded.avatar_url,
                color=excluded.color,
                x=excluded.x,
                y=excluded.y,
                size=excluded.size,
                linked_room_id=excluded.linked_room_id,
                linked_entity_id=excluded.linked_entity_id,
                linked_character_id=excluded.linked_character_id,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (
                room["id"],
                token_id,
                req.name[:80],
                req.kind[:20],
                req.owner_id[:80],
                req.avatar_url[:500],
                req.color[:20],
                req.x,
                req.y,
                req.size,
                req.linked_room_id,
                req.linked_entity_id,
                req.linked_character_id,
                req.notes[:1000],
                _now(),
            ),
        )
        conn.commit()
        token = _serialize_token(
            conn.execute(
                "SELECT * FROM multiplayer_tokens WHERE room_id=? AND token_id=?",
                (room["id"], token_id),
            ).fetchone()
        )
    await _broadcast(room["id"], {"type": "token.updated", "token": token})
    return {"status": "success", "token": token}


@multiplayer_router.put("/api/multiplayer/rooms/{room_code}/tokens/{token_id}/move")
async def move_token(room_code: str, token_id: str, req: TokenMoveRequest, request: Request):
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        row = conn.execute(
            "SELECT * FROM multiplayer_tokens WHERE room_id=? AND token_id=?",
            (room["id"], token_id),
        ).fetchone()
        if not row:
            raise fastapi.HTTPException(status_code=404, detail="Token 不存在")
        _require_member_or_gm(
            conn,
            room,
            row["owner_id"],
            _member_token_from_request(request),
            _room_token_from_request(request),
        )
        conn.execute(
            """
            UPDATE multiplayer_tokens
            SET x=?, y=?, linked_room_id=COALESCE(?, linked_room_id), updated_at=?
            WHERE room_id=? AND token_id=?
            """,
            (req.x, req.y, req.linked_room_id, _now(), room["id"], token_id),
        )
        if req.linked_room_id:
            conn.execute("UPDATE multiplayer_rooms SET current_room_id=?, updated_at=? WHERE id=?", (req.linked_room_id, _now(), room["id"]))
            try:
                conn.execute("UPDATE map_rooms SET state='explored' WHERE id=?", (req.linked_room_id,))
            except Exception:
                pass
        conn.commit()
        token = _serialize_token(
            conn.execute(
                "SELECT * FROM multiplayer_tokens WHERE room_id=? AND token_id=?",
                (room["id"], token_id),
            ).fetchone()
        )
        _remember_room_event(conn, room["code"], f"{token['name']} 移动到坐标 ({round(req.x)}, {round(req.y)})")
    await _broadcast(room["id"], {"type": "token.updated", "token": token})
    return {"status": "success", "token": token}


@multiplayer_router.post("/api/multiplayer/rooms/{room_code}/scenario/upload")
async def upload_scenario(
    room_code: str,
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(""),
    source: str = Form(""),
    chunk_size: int = Form(600),
    chunk_overlap: int = Form(80),
    hidden: int = Form(0),
):
    filename, text = await _extract_upload_text(file)
    if not _chunk_text:
        raise fastapi.HTTPException(status_code=500, detail="RAG 模块未注入")
    chunks = _chunk_text(text, max_size=max(200, min(chunk_size, 2000)), overlap=max(0, min(chunk_overlap, 300)))
    if not chunks:
        raise fastapi.HTTPException(status_code=400, detail="文本切片失败")
    embeddings = _get_embeddings(chunks) if _get_embeddings else []

    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        _require_gm(room, _room_token_from_request(request))
        doc_title = (title.strip() or filename)[:100]
        doc_source = f"room:{room['code']}:{(source.strip() or filename)[:180]}"
        cur = conn.execute(
            "INSERT INTO rag_documents (title, source, chunk_size, hidden) VALUES (?,?,?,?)",
            (f"[{room['code']}] {doc_title}"[:100], doc_source[:200], len(chunks), 1 if hidden else 0),
        )
        doc_id = cur.lastrowid
        for idx, chunk in enumerate(chunks):
            emb = embeddings[idx] if idx < len(embeddings) else []
            conn.execute(
                "INSERT INTO rag_chunks (doc_id, chunk_index, chunk_text, embedding) VALUES (?,?,?,?)",
                (doc_id, idx, chunk, _dump_json(emb)),
            )
        conn.execute(
            """
            INSERT INTO multiplayer_room_documents
            (room_id, rag_doc_id, title, source, chunk_count)
            VALUES (?,?,?,?,?)
            """,
            (room["id"], doc_id, doc_title, doc_source, len(chunks)),
        )
        msg = _save_message(
            conn,
            room["id"],
            "system",
            "System",
            "system",
            f"剧本已导入：{doc_title}（{len(chunks)} chunks）",
            {"rag_doc_id": doc_id, "filename": filename, "char_count": len(text), "chunk_count": len(chunks)},
        )
        _remember_room_event(conn, room["code"], f"导入剧本文档 {doc_title}，共 {len(chunks)} 个片段")
    if _refresh_vector_cache:
        try:
            _refresh_vector_cache()
        except Exception as exc:
            _log.warning("refresh vector cache failed: %s", exc)
    await _broadcast(room["id"], {"type": "message.created", "message": msg})
    return {
        "status": "success",
        "doc_id": doc_id,
        "filename": filename,
        "char_count": len(text),
        "chunk_count": len(chunks),
        "embedded": sum(1 for item in embeddings if item),
        "message": msg,
    }


@multiplayer_router.post("/api/multiplayer/rooms/{room_code}/map/background")
async def upload_map_background(room_code: str, request: Request, file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        raise fastapi.HTTPException(status_code=400, detail="仅支持常见图片格式")
    with safe_db() as conn:
        room = _room_by_code(conn, room_code)
        _require_gm(room, _room_token_from_request(request))
        campaign_name, campaign_folder = _room_campaign_folder(room)
        safe_room_code = re.sub(r"[^A-Za-z0-9_-]", "_", str(room["code"]))
        safe_name = sanitize_asset_name(
            f"mp_map_{safe_room_code}_{int(time.time())}_{secrets.token_hex(3)}{suffix}",
            default_ext=suffix,
        )
        target = unique_path(campaign_folder, safe_name)
        raw = await file.read()
        if len(raw) > MAX_MAP_UPLOAD_BYTES:
            max_mb = MAX_MAP_UPLOAD_BYTES // (1024 * 1024)
            raise fastapi.HTTPException(status_code=400, detail=f"地图图片不能超过 {max_mb}MB")
        with open(target, "wb") as f:
            f.write(raw)
        asset_name = os.path.basename(target)
        url = campaign_asset_url(campaign_name, asset_name)
        conn.execute(
            "UPDATE multiplayer_rooms SET map_background_url=?, updated_at=? WHERE id=?",
            (url, _now(), room["id"]),
        )
        conn.commit()
        snap = _snapshot(conn, room["id"])
    await _broadcast(room["id"], {"type": "room.updated", "snapshot": snap})
    return {"status": "success", "url": url, **snap}


@multiplayer_router.websocket("/ws/rooms/{room_code}")
async def room_websocket(
    websocket: WebSocket,
    room_code: str,
    player_id: str = Query(default=""),
    name: str = Query(default="玩家"),
    role: str = Query(default="player"),
    member_token: str = Query(default=""),
    client_id: str = Query(default=""),
    auth_token: str = Query(default=""),
):
    await websocket.accept()
    try:
        account = get_account_by_token(auth_token)
        if not account:
            await websocket.send_json({"type": "error", "message": "请先登录账号"})
            await websocket.close(code=4401)
            return
        player_id = account_player_id(account)
        name = _normalize_display_name(account.get("display_name") or account.get("username") or name)
        with safe_db() as conn:
            room = _room_by_code(conn, room_code)
            member, new_member_token = _ensure_member(
                conn,
                room["id"],
                JoinRoomRequest(player_id=player_id, display_name=name, role=role, client_id=client_id),
                _ws_query_token(member_token),
                room,
            )
            room_id = room["id"]
            snapshot = _snapshot(conn, room_id)
        _ws_clients_by_room.setdefault(room_id, set()).add(websocket)
        _ws_meta[websocket] = {
            "room_id": room_id,
            "player_id": member["player_id"],
            "name": member["display_name"],
            "role": member["role"],
            "member_token": new_member_token,
        }
        await websocket.send_json({**snapshot, "member": member, "member_token": new_member_token})
        await _broadcast(room_id, {"type": "member.updated", "member": member})

        while True:
            incoming = await websocket.receive_json()
            msg_type = incoming.get("type")
            with safe_db() as conn:
                room = conn.execute("SELECT * FROM multiplayer_rooms WHERE id=?", (room_id,)).fetchone()
                if not room:
                    await websocket.send_json({"type": "error", "message": "房间已不存在"})
                    break
                meta = _ws_meta.get(websocket, {})
                if msg_type == "chat.send":
                    content = str(incoming.get("content", "")).strip()
                    if not content:
                        continue
                    req = MessageCreateRequest(
                        sender_id=meta.get("player_id", ""),
                        sender_name=meta.get("name", name),
                        kind="chat",
                        content=content,
                        payload=incoming.get("payload") or {},
                    )
                    claim = _single_character_claim_for_player(conn, room, req.sender_id, meta.get("role", "player"))
                    sender_name = _claim_actor_name(claim, req.sender_name)
                    actor_payload = _claim_actor_payload(req.sender_id, sender_name, claim)
                    if re.match(r"^[./!！。]\s*r", content, re.I):
                        dice_req = DiceRollRequest(
                            expression=content,
                            actor_id=req.sender_id,
                            actor_name=sender_name,
                            character_id=actor_payload.get("character_id"),
                            character_name=actor_payload.get("character_name", ""),
                            ask_ai=bool(req.payload.get("ask_ai", True)),
                            context=req.payload.get("context", ""),
                        )
                        _room_state_checkpoint(conn, room, "dice")
                        await _roll_and_record(conn, room, dice_req)
                    else:
                        saved = _save_message(
                            conn,
                            room_id,
                            req.sender_id,
                            sender_name,
                            "chat",
                            content,
                            {**(req.payload or {}), **actor_payload},
                        )
                        _remember_room_event(conn, room["code"], f"{sender_name}: {content[:300]}")
                        await _broadcast(room_id, {"type": "message.created", "message": saved})
                elif msg_type == "dice.roll":
                    payload = incoming.get("payload") or {}
                    actor_id = meta.get("player_id", "")
                    claim = _single_character_claim_for_player(conn, room, actor_id, meta.get("role", "player"))
                    actor_name = _claim_actor_name(claim, payload.get("actor_name") or meta.get("name", name))
                    actor_payload = _claim_actor_payload(actor_id, actor_name, claim)
                    dice_req = DiceRollRequest(
                        expression=payload.get("expression", "1d100"),
                        actor_id=actor_id,
                        actor_name=actor_name,
                        character_id=actor_payload.get("character_id"),
                        character_name=actor_payload.get("character_name", ""),
                        reason=payload.get("reason", ""),
                        skill_name=payload.get("skill_name", ""),
                        skill_value=payload.get("skill_value"),
                        target_number=payload.get("target_number"),
                        ask_ai=bool(payload.get("ask_ai", False)),
                        context=payload.get("context", ""),
                    )
                    _room_state_checkpoint(conn, room, "dice")
                    await _roll_and_record(conn, room, dice_req)
                elif msg_type == "player.action":
                    payload = incoming.get("payload") or {}
                    action_req = PlayerActionRequest(
                        actor_id=meta.get("player_id", ""),
                        actor_name=payload.get("actor_name") or meta.get("name", name),
                        action=payload.get("action", ""),
                        action_type=payload.get("action_type", "mixed"),
                        context=payload.get("context", ""),
                        option_id=payload.get("option_id"),
                        next_node_id=payload.get("next_node_id"),
                        option_text=payload.get("option_text", ""),
                    )
                    await _adjudicate_player_action(conn, room, action_req)
                elif msg_type == "room.rollback":
                    meta = _ws_meta.get(websocket, {})
                    _require_member_or_gm(
                        conn,
                        room,
                        meta.get("player_id", ""),
                        _ws_query_token(meta.get("member_token", "")),
                    )
                    snap = _rollback_room_checkpoint(conn, room)
                    await _broadcast(room_id, {"type": "room.updated", "snapshot": snap})
                elif msg_type == "token.move":
                    payload = incoming.get("payload") or {}
                    token_id = str(payload.get("token_id", ""))
                    if not token_id:
                        continue
                    req = TokenMoveRequest(
                        token_id=token_id,
                        x=float(payload.get("x", 0)),
                        y=float(payload.get("y", 0)),
                        linked_room_id=payload.get("linked_room_id"),
                    )
                    row = conn.execute(
                        "SELECT * FROM multiplayer_tokens WHERE room_id=? AND token_id=?",
                        (room_id, token_id),
                    ).fetchone()
                    if row:
                        conn.execute(
                            "UPDATE multiplayer_tokens SET x=?, y=?, linked_room_id=COALESCE(?, linked_room_id), updated_at=? WHERE room_id=? AND token_id=?",
                            (req.x, req.y, req.linked_room_id, _now(), room_id, token_id),
                        )
                        conn.commit()
                        token = _serialize_token(
                            conn.execute(
                                "SELECT * FROM multiplayer_tokens WHERE room_id=? AND token_id=?",
                                (room_id, token_id),
                            ).fetchone()
                        )
                        await _broadcast(room_id, {"type": "token.updated", "token": token})
                elif msg_type == "token.upsert":
                    payload = incoming.get("payload") or {}
                    req = TokenUpsertRequest(**payload)
                    token_id = req.token_id or secrets.token_hex(6)
                    conn.execute(
                        """
                        INSERT INTO multiplayer_tokens
                        (room_id, token_id, name, kind, owner_id, avatar_url, color, x, y, size,
                         linked_room_id, linked_entity_id, linked_character_id, notes, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(room_id, token_id) DO UPDATE SET
                            name=excluded.name, kind=excluded.kind, owner_id=excluded.owner_id,
                            avatar_url=excluded.avatar_url, color=excluded.color,
                            x=excluded.x, y=excluded.y, size=excluded.size,
                            linked_room_id=excluded.linked_room_id,
                            linked_entity_id=excluded.linked_entity_id,
                            linked_character_id=excluded.linked_character_id,
                            notes=excluded.notes,
                            updated_at=excluded.updated_at
                        """,
                        (
                            room_id,
                            token_id,
                            req.name,
                            req.kind,
                            req.owner_id,
                            req.avatar_url,
                            req.color,
                            req.x,
                            req.y,
                            req.size,
                            req.linked_room_id,
                            req.linked_entity_id,
                            req.linked_character_id,
                            req.notes,
                            _now(),
                        ),
                    )
                    conn.commit()
                    token = _serialize_token(
                        conn.execute(
                            "SELECT * FROM multiplayer_tokens WHERE room_id=? AND token_id=?",
                            (room_id, token_id),
                        ).fetchone()
                    )
                    await _broadcast(room_id, {"type": "token.updated", "token": token})
                elif msg_type == "ping":
                    await websocket.send_json({"type": "pong", "at": _now()})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
        _log.debug("room websocket closed with error: %s", exc)
    finally:
        meta = _ws_meta.pop(websocket, None)
        if meta:
            room_id = meta["room_id"]
            _ws_clients_by_room.get(room_id, set()).discard(websocket)
            with safe_db() as conn:
                conn.execute(
                    "UPDATE multiplayer_members SET connected=0,last_seen=? WHERE room_id=? AND player_id=?",
                    (_now(), room_id, meta["player_id"]),
                )
                conn.commit()
            await _broadcast(room_id, {"type": "member.left", "player_id": meta["player_id"]})
