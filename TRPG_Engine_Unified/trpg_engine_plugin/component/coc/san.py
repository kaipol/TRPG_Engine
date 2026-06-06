import random
import re
import os
import json

from ..common.output import get_output

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 恐惧
with open(PLUGIN_DIR + "/../data/phobias.json", "r", encoding="utf-8") as f:
    phobias = json.load(f)["phobias"]

# 躁狂
with open(PLUGIN_DIR + "/../data/mania.json", "r", encoding="utf-8") as f:
    manias = json.load(f)["manias"]

def parse_san_loss_formula(formula: str):
    """
    解析 SAN 损失公式，返回成功和失败时的损失表达式。
    例如 "1d6/1d10" -> ("1d6", "1d10")
    例如 "1d6+1/1d10-1" -> ("1d6+1", "1d10-1")
    """
    parts = formula.split("/")
    
    # 成功部分就是分隔符前的部分
    success_part = parts[0]
    
    # 如果包含 '/', 则失败部分是 '/' 后的部分
    # 否则失败部分和成功部分相同
    failure_part = parts[1] if len(parts) > 1 else parts[0]
    
    return success_part, failure_part

def roll_loss(loss_expr: str):
    """
    根据损失表达式计算损失值，并返回计算过程。
    支持 "XdY" 或纯数字，并支持加法和减法运算。
    例如： "1d6", "1d6+1", "1d6-2"
    """
    # 处理加法和减法
    match = re.fullmatch(r"(\d+)d(\d+)([+-]\d+)?", loss_expr)
    if match:
        num_dice, dice_size, modifier = match.groups()
        num_dice = int(num_dice)
        dice_size = int(dice_size)
        modifier = int(modifier) if modifier else 0

        # 计算骰子结果
        dice_results = [random.randint(1, dice_size) for _ in range(num_dice)]
        dice_total = sum(dice_results)

        # 总损失计算
        total_loss = dice_total + modifier

        # 确保损失不会小于0
        total_loss = max(0, total_loss)

        # 构造计算过程表达式
        if modifier != 0:
            # 处理减法时不显示 "+ -"，只显示 "-"
            if modifier > 0:
                expr = f"[{dice_total} + {modifier}] = {total_loss}"
            else:
                expr = f"[{dice_total} - {-modifier}] = {total_loss}"
        else:
            # 没有modifier时，只返回骰子的总和
            expr = f"{dice_total}"

        # 返回损失值和计算过程
        return total_loss, expr
    
    # 纯数字情况，直接返回
    elif loss_expr.isdigit():
        loss = int(loss_expr)
        return max(0, loss), f"{loss}"

def san_check(chara_data: dict, loss_formula: str):
    """
    进行一次理智检定，返回检定结果和损失值。
    chara_data: 当前人物卡数据（需包含'san'属性）
    loss_formula: 损失公式，如 "1d6/1d10"
    返回：(roll_result, san_value, result_msg, loss, new_san)
    """
    san_value = chara_data["attributes"].get("san", 0)
    roll_result = random.randint(1, 100)
    success_loss, failure_loss = parse_san_loss_formula(loss_formula)

    if roll_result <= san_value:
        loss, expr = roll_loss(success_loss)
        result_msg = get_output("san.result.success")
        succ = True
    else:
        loss, expr = roll_loss(failure_loss)
        result_msg = get_output("san.result.failure")
        succ = False

    new_san = max(0, san_value - loss)
    return roll_result, san_value, result_msg, loss, new_san, expr

def get_temporary_insanity(phobias: dict, manias: dict):
    """
    随机生成临时疯狂症状，返回症状文本。
    phobias, manias: 恐惧症和躁狂症字典
    """
    temporary_insanity = {i: get_output(f"san.temporary.{i}") for i in range(1, 11)}
    roll = random.randint(1, 10)
    result = temporary_insanity[roll].replace("1D10", str(random.randint(1, 10)))
    if roll == 9:
        fear_roll = random.randint(1, 100)
        result += "\n" + get_output("san.specific_phobia", phobia=phobias[str(fear_roll)], roll=fear_roll)
    if roll == 10:
        mania_roll = random.randint(1, 100)
        result += "\n" + get_output("san.specific_mania", mania=manias[str(mania_roll)], roll=mania_roll)
    return result

def get_long_term_insanity(phobias: dict, manias: dict):
    """
    随机生成长期疯狂症状，返回症状文本。
    phobias, manias: 恐惧症和躁狂症字典
    """
    long_term_insanity = {i: get_output(f"san.long_term.{i}") for i in range(1, 11)}
    roll = random.randint(1, 10)
    result = long_term_insanity[roll].replace("1D10", str(random.randint(1, 10)))
    if roll == 9:
        fear_roll = random.randint(1, 100)
        result += "\n" + get_output("san.specific_phobia", phobia=phobias[str(fear_roll)], roll=fear_roll)
    if roll == 10:
        mania_roll = random.randint(1, 100)
        result += "\n" + get_output("san.specific_mania", mania=manias[str(mania_roll)], roll=mania_roll)
    return result
