"""TRPG dice and rules service exposed as native Z.R.I.C APIs."""

from __future__ import annotations

import random
import re
import sqlite3
from datetime import datetime
from typing import Any

import fastapi
from pydantic import BaseModel, Field

from .trpgdice.component.coc.checks import prepare_skill_check
from .trpgdice.component.coc.rules import configure_coc_rules, modify_coc_great_sf_rule_command
from .trpgdice.component.coc.san import (
    get_long_term_insanity,
    get_temporary_insanity,
    manias,
    phobias,
    san_check,
)
from .trpgdice.component.combat.init import InitiativeItem, InitiativeManager
from .trpgdice.component.roll.dice import (
    choose_option,
    fireball,
    get_roll_result,
    parse_dice_expression,
    roll_RP,
    roll_d66,
)
from .trpgdice.component.spells import query_spell
from .auth import require_account_from_request


dice_router = fastapi.APIRouter(prefix="/api/dice", tags=["骰子规则服务"])

_db_file = ""
_append_to_memory = None
_initiative = InitiativeManager()


def configure_dice_service(db_file: str, fn_append_to_memory=None):
    """Inject host engine dependencies without depending on plugin runtime."""
    global _db_file, _append_to_memory
    _db_file = db_file
    _append_to_memory = fn_append_to_memory
    configure_coc_rules(db_file)


def normalize_dice_expression(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^[./!！。]\s*", "", text)
    if text.lower().startswith("roll "):
        text = text[4:].strip()
    elif text.lower().startswith("r ") or re.match(r"^r\d", text, re.I):
        text = text[1:].strip()
    return text or "1d100"


def single_line(text: str) -> str:
    return re.sub(r"\s*[\r\n]+\s*", " ; ", str(text or "")).strip()


def coc_rank_label(text: str) -> str:
    if "大成功" in text:
        return "critical_success"
    if "极难" in text:
        return "extreme_success"
    if "困难" in text:
        return "hard_success"
    if "大失败" in text:
        return "fumble"
    if "成功" in text:
        return "success"
    return "failure"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _record_to_memory(enabled: bool, text: str, request: fastapi.Request):
    if not enabled or not _append_to_memory or not _db_file:
        return
    account = require_account_from_request(request)
    conn = sqlite3.connect(_db_file, timeout=10)
    try:
        _append_to_memory(conn, f"[骰子服务] {text}", owner_account_id=int(account["id"]))
        conn.commit()
    finally:
        conn.close()


def _load_character_tools():
    try:
        from .trpgdice.component.common.utils import (
            format_character,
            format_dnd_character,
            generate_names,
            roll_character,
            roll_dnd_character,
        )
    except ModuleNotFoundError as exc:
        if exc.name == "faker":
            raise fastapi.HTTPException(
                status_code=503,
                detail="姓名和角色生成需要安装 faker，请先执行 pip install -r requirements.txt。",
            ) from exc
        raise
    return generate_names, roll_character, format_character, roll_dnd_character, format_dnd_character


def _roll_coc_d100(bonus_dice: int = 0, penalty_dice: int = 0) -> tuple[int, str]:
    bonus_dice = max(0, min(int(bonus_dice or 0), 9))
    penalty_dice = max(0, min(int(penalty_dice or 0), 9))
    ones = random.randint(0, 9)
    tens_rolls = [random.randint(0, 9) for _ in range(1 + max(bonus_dice, penalty_dice))]
    if bonus_dice:
        final_tens = min(tens_rolls)
        mode = f"{bonus_dice}B"
    elif penalty_dice:
        final_tens = max(tens_rolls)
        mode = f"{penalty_dice}P"
    else:
        final_tens = tens_rolls[0]
        mode = "D100"
    value = final_tens * 10 + ones
    if value == 0:
        value = 100
    detail = f"{mode} 十位[{', '.join(str(x) for x in tens_rolls)}] 个位[{ones}] -> {value}"
    return value, detail


class RollRequest(BaseModel):
    expression: str = Field(default="1d100", max_length=120)
    actor_name: str = Field(default="GM", max_length=80)
    reason: str = Field(default="", max_length=200)
    skill_name: str = Field(default="", max_length=80)
    skill_value: int | None = Field(default=None, ge=0, le=999)
    target_number: int | None = Field(default=None, ge=-9999, le=9999)
    group_id: str = Field(default="main", max_length=80)
    record_to_memory: bool = False


class CocCheckRequest(BaseModel):
    actor_name: str = Field(default="GM", max_length=80)
    skill_name: str = Field(default="侦查", max_length=80)
    skill_value: int = Field(default=60, ge=0, le=999)
    roll_times: int = Field(default=1, ge=1, le=20)
    bonus_dice: int = Field(default=0, ge=0, le=9)
    penalty_dice: int = Field(default=0, ge=0, le=9)
    group_id: str = Field(default="main", max_length=80)
    reason: str = Field(default="", max_length=200)
    record_to_memory: bool = False


class VersusRequest(BaseModel):
    left_name: str = Field(default="发起方", max_length=80)
    left_value: int = Field(default=60, ge=0, le=999)
    right_name: str = Field(default="对抗方", max_length=80)
    right_value: int = Field(default=60, ge=0, le=999)
    group_id: str = Field(default="main", max_length=80)
    record_to_memory: bool = False


class SanCheckRequest(BaseModel):
    actor_name: str = Field(default="调查员", max_length=80)
    san: int = Field(default=60, ge=0, le=999)
    loss_formula: str = Field(default="1/1d6", max_length=40)
    record_to_memory: bool = False


class ChoiceRequest(BaseModel):
    options: str = Field(default="", max_length=1000)
    record_to_memory: bool = False


class InitiativeAddRequest(BaseModel):
    group_id: str = Field(default="main", max_length=80)
    name: str = Field(default="角色", max_length=80)
    expression: str = Field(default="", max_length=40)
    player_id: int = 0


@dice_router.post("/roll")
def roll_expression(req: RollRequest, request: fastapi.Request):
    expression = normalize_dice_expression(req.expression)
    total, detail = parse_dice_expression(expression)
    if total is None:
        raise fastapi.HTTPException(status_code=400, detail=f"骰子表达式错误：{detail}")

    numeric_total = int(total) if isinstance(total, (int, float)) and float(total).is_integer() else total
    outcome = ""
    rank = None
    if req.skill_value is not None and isinstance(numeric_total, int):
        outcome = get_roll_result(int(numeric_total), int(req.skill_value), req.group_id)
        rank = coc_rank_label(outcome)
    elif req.target_number is not None and isinstance(numeric_total, (int, float)):
        outcome = "成功" if numeric_total >= req.target_number else "失败"
        rank = "success" if numeric_total >= req.target_number else "failure"

    summary = f"{req.actor_name} 掷骰 {expression} = {numeric_total}"
    if req.skill_name:
        summary += f"；{req.skill_name}"
    if req.skill_value is not None:
        summary += f" 目标 {req.skill_value}"
    if req.target_number is not None:
        summary += f" DC {req.target_number}"
    if outcome:
        summary += f"：{outcome}"
    if req.reason:
        summary += f"；{req.reason}"

    result = {
        "status": "success",
        "expression": expression,
        "total": numeric_total,
        "detail": single_line(detail),
        "outcome": outcome,
        "rank": rank,
        "summary": summary,
        "created_at": _now(),
    }
    _record_to_memory(req.record_to_memory, summary, request)
    return result


@dice_router.post("/coc/check")
def coc_check(req: CocCheckRequest, request: fastapi.Request):
    display_skill, target_value = prepare_skill_check(req.skill_name, req.skill_value)
    results: list[dict[str, Any]] = []
    for _ in range(req.roll_times):
        roll, detail = _roll_coc_d100(req.bonus_dice, req.penalty_dice)
        outcome = get_roll_result(roll, target_value, req.group_id)
        results.append(
            {
                "roll": roll,
                "target": target_value,
                "detail": detail,
                "outcome": outcome,
                "rank": coc_rank_label(outcome),
            }
        )
    result_text = "；".join(f"{item['roll']}/{item['target']} {item['outcome']}" for item in results)
    summary = f"{req.actor_name} 进行 {display_skill} 检定：{result_text}"
    if req.reason:
        summary += f"；{req.reason}"
    _record_to_memory(req.record_to_memory, summary, request)
    return {
        "status": "success",
        "actor_name": req.actor_name,
        "skill_name": display_skill,
        "skill_value": req.skill_value,
        "target_value": target_value,
        "results": results,
        "summary": summary,
        "created_at": _now(),
    }


@dice_router.post("/coc/versus")
def coc_versus(req: VersusRequest, request: fastapi.Request):
    left_roll, left_detail = _roll_coc_d100()
    right_roll, right_detail = _roll_coc_d100()
    left_outcome = get_roll_result(left_roll, req.left_value, req.group_id)
    right_outcome = get_roll_result(right_roll, req.right_value, req.group_id)

    rank_order = {
        "critical_success": 5,
        "extreme_success": 4,
        "hard_success": 3,
        "success": 2,
        "failure": 1,
        "fumble": 0,
    }
    left_rank = rank_order[coc_rank_label(left_outcome)]
    right_rank = rank_order[coc_rank_label(right_outcome)]
    if left_rank <= 1 and right_rank <= 1:
        winner = "双方均失败"
    elif left_rank > right_rank:
        winner = req.left_name
    elif right_rank > left_rank:
        winner = req.right_name
    elif req.left_value > req.right_value:
        winner = req.left_name
    elif req.right_value > req.left_value:
        winner = req.right_name
    else:
        winner = "平局"

    summary = (
        f"对抗检定：{req.left_name} {left_roll}/{req.left_value} {left_outcome}；"
        f"{req.right_name} {right_roll}/{req.right_value} {right_outcome}；胜者：{winner}"
    )
    _record_to_memory(req.record_to_memory, summary, request)
    return {
        "status": "success",
        "left": {"name": req.left_name, "roll": left_roll, "target": req.left_value, "detail": left_detail, "outcome": left_outcome},
        "right": {"name": req.right_name, "roll": right_roll, "target": req.right_value, "detail": right_detail, "outcome": right_outcome},
        "winner": winner,
        "summary": summary,
    }


@dice_router.post("/coc/san")
def coc_san(req: SanCheckRequest, request: fastapi.Request):
    chara_data = {"attributes": {"san": req.san}}
    roll, san_value, result_msg, loss, new_san, expr = san_check(chara_data, req.loss_formula)
    summary = f"{req.actor_name} SAN Check：{roll}/{san_value} {result_msg}，损失 {loss}（{expr}），剩余 {new_san}"
    _record_to_memory(req.record_to_memory, summary, request)
    return {
        "status": "success",
        "roll": roll,
        "san": san_value,
        "result": result_msg,
        "loss": loss,
        "loss_detail": expr,
        "new_san": new_san,
        "summary": summary,
    }


@dice_router.get("/coc/insanity")
def coc_insanity(kind: str = "temporary"):
    if kind in {"long", "long_term", "长期"}:
        result = get_long_term_insanity(phobias, manias)
    else:
        result = get_temporary_insanity(phobias, manias)
    return {"status": "success", "kind": kind, "result": result}


@dice_router.post("/coc/rule")
def set_coc_rule(request: fastapi.Request, group_id: str = "main", command: str = " "):
    require_account_from_request(request)
    return {"status": "success", "result": modify_coc_great_sf_rule_command(group_id, command or " ")}


@dice_router.get("/tool/d66")
def tool_d66(count: int = 1):
    return {"status": "success", "result": roll_d66(count)}


@dice_router.get("/tool/jrrp")
def tool_jrrp(actor_id: str = "main"):
    return {"status": "success", "result": roll_RP(actor_id)}


@dice_router.get("/tool/fireball")
def tool_fireball(ring: int = 3):
    return {"status": "success", "result": fireball(ring)}


@dice_router.post("/tool/choose")
def tool_choose(req: ChoiceRequest, request: fastapi.Request):
    result = choose_option(req.options)
    _record_to_memory(req.record_to_memory, f"随机选择：{result}", request)
    return {"status": "success", "result": result}


@dice_router.get("/tool/name")
def tool_name(language: str = "cn", count: int = 5, sex: str | None = None):
    generate_names, *_ = _load_character_tools()
    names = generate_names(language, max(1, min(int(count), 20)), sex)
    return {"status": "success", "names": names, "result": "、".join(names)}


@dice_router.get("/tool/character/coc")
def tool_coc_character(count: int = 1):
    _, roll_character, format_character, _, _ = _load_character_tools()
    count = max(1, min(int(count), 10))
    characters = [format_character(roll_character(), i + 1) for i in range(count)]
    return {"status": "success", "result": "\n\n".join(characters), "characters": characters}


@dice_router.get("/tool/character/dnd")
def tool_dnd_character(count: int = 1):
    _, _, _, roll_dnd_character, format_dnd_character = _load_character_tools()
    count = max(1, min(int(count), 10))
    characters = [format_dnd_character(roll_dnd_character(), i + 1) for i in range(count)]
    return {"status": "success", "result": "\n\n".join(characters), "characters": characters}


@dice_router.get("/spell")
def spell_lookup(q: str = ""):
    return {"status": "success", "query": q, "result": query_spell(q)}


@dice_router.post("/initiative/add")
def initiative_add(req: InitiativeAddRequest, request: fastapi.Request):
    require_account_from_request(request)
    value, name = _initiative.parse_roll(req.expression, req.name)
    item = InitiativeItem(name=name or req.name, init_value=value, player_id=req.player_id)
    _initiative.add_item(req.group_id, item)
    return {"status": "success", "item": item.__dict__, "list": _initiative.format_list(req.group_id)}


@dice_router.post("/initiative/next")
def initiative_next(request: fastapi.Request, group_id: str = "main"):
    require_account_from_request(request)
    item = _initiative.next_turn(group_id)
    return {"status": "success", "current": item.__dict__ if item else None, "list": _initiative.format_list(group_id)}


@dice_router.post("/initiative/clear")
def initiative_clear(request: fastapi.Request, group_id: str = "main"):
    require_account_from_request(request)
    _initiative.clear(group_id)
    return {"status": "success", "list": _initiative.format_list(group_id)}
