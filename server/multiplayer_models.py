"""Pydantic request models for the multiplayer room API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RoomCreateRequest(BaseModel):
    name: str = Field(default="新的跑团房间", max_length=80)
    gm_name: str = Field(default="GM", max_length=40)
    campaign_path: str = Field(default="", max_length=240)
    restore_code: str = Field(default="", max_length=80)
    settings: dict[str, Any] = Field(default_factory=dict)


class RoomPatchRequest(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    current_scene_id: int | None = None
    current_room_id: int | None = None
    settings: dict[str, Any] | None = None


class JoinRoomRequest(BaseModel):
    player_id: str = Field(default="", max_length=80)
    display_name: str = Field(default="玩家", max_length=40)
    role: str = Field(default="player", max_length=20)
    color: str = Field(default="", max_length=20)
    client_id: str = Field(default="", max_length=120)


class CharacterClaimRequest(BaseModel):
    player_id: str = Field(default="", max_length=80)
    character_ids: list[int] = Field(default_factory=list, max_length=6)


class MessageCreateRequest(BaseModel):
    sender_id: str = Field(default="", max_length=80)
    sender_name: str = Field(default="玩家", max_length=40)
    kind: str = Field(default="chat", max_length=20)
    content: str = Field(default="", max_length=4000)
    payload: dict[str, Any] = Field(default_factory=dict)


class DiceRollRequest(BaseModel):
    expression: str = Field(default="1d100", max_length=120)
    actor_id: str = Field(default="", max_length=80)
    actor_name: str = Field(default="玩家", max_length=40)
    character_id: int | None = None
    character_name: str = Field(default="", max_length=80)
    reason: str = Field(default="", max_length=200)
    skill_name: str = Field(default="", max_length=80)
    skill_value: int | None = Field(default=None, ge=0, le=999)
    target_number: int | None = Field(default=None, ge=-9999, le=9999)
    ask_ai: bool = False
    context: str = Field(default="", max_length=1200)


class PlayerActionRequest(BaseModel):
    actor_id: str = Field(default="", max_length=80)
    actor_name: str = Field(default="玩家", max_length=40)
    character_id: int | None = None
    character_name: str = Field(default="", max_length=80)
    action: str = Field(default="", max_length=1200)
    action_type: str = Field(default="mixed", max_length=20)
    context: str = Field(default="", max_length=1200)
    option_id: int | None = None
    next_node_id: int | None = None
    option_text: str = Field(default="", max_length=300)


class RoomRollbackRequest(BaseModel):
    player_id: str = Field(default="", max_length=80)


class AiEventRequest(BaseModel):
    kind: str = Field(default="ai", max_length=20)
    content: str = Field(default="", max_length=4000)
    payload: dict[str, Any] = Field(default_factory=dict)
    sender_name: str = Field(default="AI-KP", max_length=40)
    broadcast_state: bool = True


class TokenUpsertRequest(BaseModel):
    token_id: str = Field(default="", max_length=80)
    name: str = Field(default="Token", max_length=80)
    kind: str = Field(default="pc", max_length=20)
    owner_id: str = Field(default="", max_length=80)
    avatar_url: str = Field(default="", max_length=500)
    color: str = Field(default="#f59e0b", max_length=20)
    x: float = 120
    y: float = 120
    size: float = Field(default=48, ge=20, le=160)
    linked_room_id: int | None = None
    linked_entity_id: int | None = None
    linked_character_id: int | None = None
    notes: str = Field(default="", max_length=1000)


class TokenMoveRequest(BaseModel):
    token_id: str = Field(max_length=80)
    x: float
    y: float
    linked_room_id: int | None = None
