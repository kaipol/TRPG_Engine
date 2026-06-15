"""AI conversion helpers for campaign import."""

from __future__ import annotations

import base64
import json
import re
from typing import Callable

import json_repair

from . import ai_provider
from .document_extraction import (
    VisionFallbackImage,
    decode_text_bytes,
    extract_document_with_mineru,
    prepare_document_vision_fallback,
)
from .local_config import get_campaign_import_settings


MAX_AI_CORPUS_CHARS = 90000
MAX_KNOWLEDGE_SECTION_CHARS = 9000
VISION_FALLBACK_MAX_IMAGES = 12
VISION_FALLBACK_BATCH_SIZE = 4
MULTIMODAL_PDF_MAX_BYTES = 48 * 1024 * 1024
CORE_PC_NAMES = {"玩家", "调查员", "探索者", "PC", "Player"}
GENERIC_ENTITY_NAMES = {
    "NPC", "PC", "角色", "人物", "姓名", "玩家", "调查员", "地点", "房间", "区域", "线索", "道具", "物品",
    "背景", "世界观", "真相", "地图", "章节", "场景", "开篇资料", "自动分段",
}


def _clean_import_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _slug_filename(value: str, fallback: str, ext: str = ".md") -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", (value or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" ._")
    return (name[:48] or fallback) + ext


def _looks_like_heading(line: str) -> bool:
    line = (line or "").strip()
    if not line or len(line) > 90:
        return False
    if re.search(r"[。！？!?；;]{2,}", line):
        return False
    if re.match(r"^(NPC|PC|角色|人物|姓名)\s*[:：]", line, re.I) and re.search(r"HP|SAN|技能|物品|属性|职业|年龄", line, re.I):
        return False
    patterns = [
        r"^#{1,4}\s+\S+",
        r"^【[^】]{2,60}】$",
        r"^\[[^\]]{2,60}\]$",
        r"^第[一二三四五六七八九十百千万\d]+[章节幕场案部篇].{0,50}$",
        r"^[一二三四五六七八九十百千万\d]+[、.．]\s*.{2,60}$",
        r"^(场景|章节|幕|地点|房间|区域|线索|人物|角色|NPC|PC|导入|背景|结局|地图|手卡|道具|真相|时间线)\s*[:：\d一二三四五六七八九十]?.{0,60}$",
        r"^(Scene|Chapter|Act)\s+\d+.{0,50}$",
    ]
    return any(re.match(pattern, line, re.I) for pattern in patterns)


def _section_kind(title: str, body: str) -> str:
    sample = f"{title}\n{body[:500]}"
    title_text = title or ""
    if re.search(r"场景|章节|第[一二三四五六七八九十百千万\d]+[章节幕场案部篇]|Scene|Chapter|Act", title_text, re.I):
        return "scene"
    if re.search(r"人物|角色|NPC|PC|调查员|玩家角色|登场", title_text, re.I):
        return "characters"
    if re.search(r"地图|地点|房间|区域|楼层|场所|路线", sample, re.I):
        return "map"
    if re.search(r"线索|证据|道具|物品|手卡|资料|情报", sample, re.I):
        return "clues"
    if re.search(r"背景|世界观|真相|设定|历史|概要", sample, re.I):
        return "lore"
    if re.search(r"结局|尾声|终章", sample, re.I):
        return "ending"
    if re.search(r"人物|角色|NPC|PC|玩家角色|登场", sample, re.I):
        return "characters"
    return "scene"


def split_campaign_sections(text: str, max_sections: int = 48) -> list[dict]:
    """Split OCR text into useful campaign sections for AI and RAG."""
    text = _clean_import_text(text)
    if not text:
        return []

    lines = text.split("\n")
    sections: list[dict] = []
    current_title = "开篇资料"
    current_lines: list[str] = []
    seen_content = False

    for raw_line in lines:
        line = raw_line.strip()
        if _looks_like_heading(line) and (seen_content or current_lines):
            body = "\n".join(current_lines).strip()
            if body:
                sections.append({"title": current_title, "text": body})
            current_title = re.sub(r"^#{1,4}\s*", "", line).strip("【】[] ")
            current_lines = []
            seen_content = False
        else:
            current_lines.append(raw_line)
            if line:
                seen_content = True

    body = "\n".join(current_lines).strip()
    if body:
        sections.append({"title": current_title, "text": body})

    if len(sections) <= 2 and len(text) > MAX_KNOWLEDGE_SECTION_CHARS:
        sections = []
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        current: list[str] = []
        current_len = 0
        for paragraph in paragraphs:
            if current and current_len + len(paragraph) > MAX_KNOWLEDGE_SECTION_CHARS:
                idx = len(sections) + 1
                sections.append({"title": f"自动分段 {idx:02d}", "text": "\n\n".join(current)})
                current = []
                current_len = 0
            current.append(paragraph)
            current_len += len(paragraph)
        if current:
            idx = len(sections) + 1
            sections.append({"title": f"自动分段 {idx:02d}", "text": "\n\n".join(current)})

    normalized: list[dict] = []
    for idx, section in enumerate(sections[:max_sections], start=1):
        title = str(section.get("title") or f"分段 {idx:02d}").strip()[:80]
        body = str(section.get("text") or "").strip()
        if not body:
            continue
        normalized.append({
            "index": idx,
            "title": title,
            "kind": _section_kind(title, body),
            "text": body,
        })
    return normalized


def build_knowledge_documents(title: str, text: str) -> list[dict[str, str]]:
    """Build multiple knowledge files instead of storing OCR text as one blob."""
    text = _clean_import_text(text)
    sections = split_campaign_sections(text, max_sections=36)
    docs: list[dict[str, str]] = []
    docs.append({
        "title": "原始剧本文档",
        "filename": "原始剧本文档.txt",
        "text": text or "原始文档未提取到文本。",
    })
    outline_lines = [
        f"# {title} 导入索引",
        "",
        f"- 原文长度：{len(text)} 字符",
        f"- 自动分段：{len(sections)} 段",
        "",
        "## 分段目录",
    ]
    for section in sections:
        outline_lines.append(f"- {section['index']:02d}. [{section['kind']}] {section['title']}（{len(section['text'])} 字）")
    docs.append({
        "title": "导入索引",
        "filename": "00_导入索引.md",
        "text": "\n".join(outline_lines).strip(),
    })
    for section in sections:
        filename = _slug_filename(section["title"], f"section_{section['index']:02d}")
        docs.append({
            "title": f"{section['index']:02d}_{section['title']}",
            "filename": f"{section['index']:02d}_{filename}",
            "text": (
                f"# {section['title']}\n\n"
                f"类型：{section['kind']}\n\n"
                f"{section['text'][:MAX_KNOWLEDGE_SECTION_CHARS]}"
            ).strip(),
        })
    return docs


def build_structured_knowledge_documents(title: str, campaign: dict, map_data: dict) -> list[dict[str, str]]:
    """Build RAG-ready documents from generated campaign structure."""
    docs: list[dict[str, str]] = []
    model_docs = [d for d in (campaign.get("knowledge_documents") or []) if isinstance(d, dict)]
    characters = [c for c in (campaign.get("characters") or []) if isinstance(c, dict)]
    nodes = [n for n in (campaign.get("nodes") or []) if isinstance(n, dict)]
    lorebook = [l for l in (campaign.get("lorebook") or []) if isinstance(l, dict)]
    entities = [e for e in (campaign.get("world_entities") or []) if isinstance(e, dict)]
    rooms = [r for r in (map_data.get("map_rooms") or []) if isinstance(r, dict)]
    edges = [e for e in (map_data.get("map_edges") or []) if isinstance(e, dict)]

    for idx, item in enumerate(model_docs[:48], start=1):
        doc_title = str(item.get("title") or item.get("name") or f"模型知识分段 {idx:02d}").strip()[:80]
        body = str(item.get("text") or item.get("content") or "").strip()
        if not body:
            continue
        docs.append({
            "title": doc_title,
            "filename": f"模型_{idx:02d}_{_slug_filename(doc_title, f'model_doc_{idx:02d}')}",
            "text": f"# {doc_title}\n\n{body[:MAX_KNOWLEDGE_SECTION_CHARS]}",
        })

    if characters:
        lines = [f"# {title} 角色档案", ""]
        for char in characters:
            lines.extend([
                f"## {char.get('name') or '未命名角色'}",
                f"- 类型：{char.get('role') or 'NPC'}",
                f"- HP：{char.get('hp', 100)}",
                f"- SAN：{char.get('san', 80)}",
                f"- 状态：{char.get('status') or 'active'}",
                f"- 物品/属性：{char.get('inventory') or '无'}",
                "",
                str(char.get("personality") or "无角色描述"),
                "",
            ])
        docs.append({"title": "角色档案", "filename": "角色档案.md", "text": "\n".join(lines).strip()})

    if rooms:
        room_by_id = {room.get("id"): room for room in rooms}
        lines = [f"# {title} 地图与地点", ""]
        for room in rooms:
            connected = []
            for edge in edges:
                if edge.get("from_id") == room.get("id"):
                    target = room_by_id.get(edge.get("to_id"))
                    if target:
                        connected.append(str(target.get("label") or target.get("id")))
                elif edge.get("to_id") == room.get("id"):
                    target = room_by_id.get(edge.get("from_id"))
                    if target:
                        connected.append(str(target.get("label") or target.get("id")))
            lines.extend([
                f"## {room.get('label') or '未命名地点'}",
                f"- 关联场景：{room.get('node_id') or '未绑定'}",
                f"- 状态：{room.get('state') or 'unknown'}",
                f"- 连接：{', '.join(connected) if connected else '无明确连接'}",
                "",
                str(room.get("description") or "无地点描述"),
                "",
            ])
        docs.append({"title": "地图与地点", "filename": "地图与地点.md", "text": "\n".join(lines).strip()})

    if lorebook:
        lines = [f"# {title} 百科与线索", ""]
        for lore in lorebook:
            lines.extend([
                f"## {lore.get('keywords') or '未命名条目'}",
                str(lore.get("content") or ""),
                "",
            ])
        docs.append({"title": "百科与线索", "filename": "百科与线索.md", "text": "\n".join(lines).strip()})

    if entities:
        lines = [f"# {title} 世界实体", ""]
        for entity in entities:
            lines.extend([
                f"## {entity.get('name') or '未命名实体'}",
                f"- 类型：{entity.get('entity_type') or 'entity'}",
                f"- 位置：{entity.get('location') or '未知'}",
                f"- 状态：{entity.get('status') or 'active'}",
                "",
                str(entity.get("state_desc") or ""),
                "",
            ])
        docs.append({"title": "世界实体", "filename": "世界实体.md", "text": "\n".join(lines).strip()})

    if nodes:
        lines = [f"# {title} 场景索引", ""]
        for node in nodes:
            lines.extend([
                f"## {node.get('id')}. {node.get('name') or '未命名场景'}",
                f"- 摘要：{node.get('summary') or ''}",
                f"- 场景图：{node.get('scene_image') or '无'}",
                "",
                str(node.get("content") or "")[:1800],
                "",
            ])
        docs.append({"title": "场景索引", "filename": "场景索引.md", "text": "\n".join(lines).strip()})

    return docs


def _pick_lines(text: str, pattern: str, limit: int = 30) -> list[str]:
    matches: list[str] = []
    for line in text.splitlines():
        clean = line.strip()
        if not clean or len(clean) > 180:
            continue
        if re.search(pattern, clean, re.I):
            matches.append(clean)
        if len(matches) >= limit:
            break
    return matches


def _shorten(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


def _extract_number(text: str, labels: tuple[str, ...], default: int) -> int:
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*[:：=]?\s*(\d{{1,3}})", text, re.I)
        if match:
            try:
                return max(0, min(999, int(match.group(1))))
            except ValueError:
                pass
    return default


def _extract_character_stats(text: str) -> dict[str, int | str]:
    hp = _extract_number(text, ("HP", "体力", "生命", "耐久"), 100)
    san = _extract_number(text, ("SAN", "理智", "理智值"), 80)
    attrs = []
    for label in ("STR", "CON", "DEX", "APP", "INT", "POW", "SIZ", "EDU", "力量", "体质", "敏捷", "外貌", "智力", "意志", "体型", "教育"):
        match = re.search(rf"{re.escape(label)}\s*[:：=]?\s*([\d%/]+)", text, re.I)
        if match:
            attrs.append(f"{label}:{match.group(1)}")
    skill_match = re.search(r"(?:技能|能力|专长|特长)\s*[:：]\s*([^\n。；;]{2,180})", text, re.I)
    item_match = re.search(r"(?:物品|装备|携带|道具|库存)\s*[:：]\s*([^\n。；;]{2,180})", text, re.I)
    return {
        "hp": hp,
        "san": san,
        "attrs": "，".join(attrs),
        "skills": _shorten(skill_match.group(1), 180) if skill_match else "",
        "items": _shorten(item_match.group(1), 180) if item_match else "",
    }


def _infer_role(text: str, fallback: str = "NPC") -> str:
    if re.search(r"\bPC\b|玩家角色|调查员|探索者|预设角色|可扮演", text, re.I):
        return "PC"
    if re.search(r"\bNPC\b|非玩家|敌人|怪物|守卫|店主|管家|医生|警察|教授", text, re.I):
        return "NPC"
    return fallback


def _normalize_name(value: str) -> str:
    name = re.sub(r"^[#*\-·\s\d一二三四五六七八九十、.．:：]+", "", str(value or "")).strip()
    name = re.split(r"[:：,，;；（()\[\]【】\-—\s]", name, maxsplit=1)[0].strip()
    return name[:24]


def _extract_named_entities_from_line(line: str) -> list[str]:
    clean = line.strip(" -·\t")
    if not clean or len(clean) > 160:
        return []
    names: list[str] = []
    patterns = [
        r"(?:NPC|PC|角色|人物|姓名|调查员|探索者)\s*[:：]\s*([A-Za-z0-9_\-\u4e00-\u9fff]{2,24})",
        r"^([A-Za-z0-9_\-\u4e00-\u9fff]{2,16})\s*[:：]\s*(?:HP|SAN|年龄|职业|身份|技能|物品|性格|背景|目标)",
        r"^([A-Za-z0-9_\-\u4e00-\u9fff]{2,16})[（(](?:NPC|PC|调查员|探索者|角色|人物)[）)]",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, clean, re.I):
            name = _normalize_name(match.group(1))
            if name and name not in GENERIC_ENTITY_NAMES:
                names.append(name)
    return names


def _extract_characters_from_sections(sections: list[dict]) -> list[dict]:
    candidates: dict[str, dict] = {}
    search_sections = [s for s in sections if s["kind"] == "characters"] + sections[:12]
    for section in search_sections:
        for line in [section.get("title", ""), *section["text"].splitlines()]:
            clean = line.strip(" -·\t")
            for name in _extract_named_entities_from_line(clean):
                stats = _extract_character_stats(clean)
                role = _infer_role(clean, "NPC")
                current = candidates.get(name, {})
                candidates[name] = {
                    "name": name,
                    "role": current.get("role") or role,
                    "hp": current.get("hp") or stats["hp"],
                    "san": current.get("san") or stats["san"],
                    "inventory": current.get("inventory") or stats["items"] or (f"属性：{stats['attrs']}" if stats["attrs"] else ""),
                    "personality": current.get("personality") or _shorten(clean, 500),
                    "status": current.get("status") or "active",
                }
                if stats["skills"] and stats["skills"] not in candidates[name]["personality"]:
                    candidates[name]["personality"] = _shorten(candidates[name]["personality"] + f"\n技能：{stats['skills']}", 800)

    characters = list(candidates.values())[:32]
    if not any(char.get("role") == "PC" for char in characters):
        characters.insert(0, {
            "name": "调查员",
            "role": "PC",
            "hp": 100,
            "san": 80,
            "inventory": "",
            "personality": "默认玩家角色。导入文本未明确给出可扮演角色，可在角色面板继续细化属性。",
            "status": "active",
        })
    return characters[:40]


def _section_summary(section: dict, limit: int = 420) -> str:
    text = str(section.get("text") or "")
    first_paragraph = re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0]
    return _shorten(first_paragraph or text, limit)


def _extract_lore_entries(sections: list[dict]) -> list[dict]:
    entries: list[dict] = []
    for section in sections[:80]:
        title = str(section.get("title") or "").strip() or f"资料 {section.get('index', len(entries) + 1)}"
        kind = section.get("kind") or "scene"
        if kind == "scene":
            keywords = f"场景：{title}"
        elif kind == "characters":
            keywords = f"人物：{title}"
        elif kind == "map":
            keywords = f"地点：{title}"
        elif kind == "clues":
            keywords = f"线索：{title}"
        else:
            keywords = title
        entries.append({
            "keywords": keywords[:100],
            "content": str(section.get("text") or "")[:5000],
        })
    return entries


def _extract_location_names(sections: list[dict], nodes: list[dict]) -> list[dict]:
    locations: list[dict] = []
    seen: set[str] = set()
    for section in sections:
        if section["kind"] in {"map", "scene", "ending"}:
            title = _shorten(section["title"], 40)
            if title and title not in seen and title not in GENERIC_ENTITY_NAMES:
                seen.add(title)
                locations.append({"name": title, "description": _section_summary(section, 600)})
        if len(locations) >= 48:
            break
    for node in nodes:
        title = _shorten(node.get("name"), 40)
        if title and title not in seen and title not in GENERIC_ENTITY_NAMES:
            seen.add(title)
            locations.append({"name": title, "description": _shorten(node.get("summary") or node.get("content"), 600)})
        if len(locations) >= 48:
            break
    return locations


def _extract_clue_entities(sections: list[dict]) -> list[dict]:
    entities: list[dict] = []
    seen: set[str] = set()
    for section in sections:
        if section["kind"] not in {"clues", "lore"}:
            continue
        for line in section["text"].splitlines():
            clean = line.strip(" -·\t")
            if not clean or len(clean) > 160:
                continue
            match = re.match(r"(?:线索|证据|道具|物品|手卡|资料|情报)\s*[:：]?\s*([^：:，,。；;\n]{2,40})", clean)
            if not match:
                continue
            name = _normalize_name(match.group(1))
            if not name or name in seen or name in GENERIC_ENTITY_NAMES:
                continue
            seen.add(name)
            entities.append({
                "entity_type": "item",
                "name": name,
                "location": section["title"],
                "status": "hidden",
                "state_desc": _shorten(clean, 500),
            })
            if len(entities) >= 40:
                return entities
    return entities


def _dedupe_entities(entities: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = _normalize_name(entity.get("name") or "")
        if not name or name in GENERIC_ENTITY_NAMES:
            continue
        key = f"{entity.get('entity_type') or 'entity'}|{name}".lower()
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "entity_type": str(entity.get("entity_type") or "entity")[:32],
            "name": name,
            "location": str(entity.get("location") or "")[:120],
            "status": str(entity.get("status") or "active")[:32],
            "last_seen_by": str(entity.get("last_seen_by") or ""),
            "state_desc": str(entity.get("state_desc") or entity.get("description") or "")[:2500],
            "room_id": entity.get("room_id"),
        })
    return result


def _normalize_character(raw: dict, fallback_name: str = "角色") -> dict:
    name = _normalize_name(raw.get("name") or fallback_name) or fallback_name
    blob = json.dumps(raw, ensure_ascii=False)
    stats = _extract_character_stats(blob)
    hp = raw.get("hp", stats["hp"])
    san = raw.get("san", stats["san"])
    try:
        hp = max(0, min(999, int(hp)))
    except (TypeError, ValueError):
        hp = 100
    try:
        san = max(0, min(999, int(san)))
    except (TypeError, ValueError):
        san = 80
    inventory_parts = [
        str(raw.get("inventory") or raw.get("items") or "").strip(),
        f"属性：{stats['attrs']}" if stats["attrs"] else "",
    ]
    personality_parts = [
        str(raw.get("personality") or raw.get("description") or raw.get("background") or "").strip(),
        f"技能：{stats['skills']}" if stats["skills"] else "",
        f"动机：{raw.get('motivation')}" if raw.get("motivation") else "",
        f"秘密：{raw.get('secret')}" if raw.get("secret") else "",
    ]
    role = str(raw.get("role") or _infer_role(blob, "NPC")).upper()
    if role not in {"PC", "NPC"}:
        role = "PC" if "玩家" in role or "调查" in role else "NPC"
    return {
        "name": name,
        "role": role,
        "hp": hp,
        "san": san,
        "inventory": _shorten("；".join(part for part in inventory_parts if part), 800),
        "personality": _shorten("\n".join(part for part in personality_parts if part), 1600),
        "status": str(raw.get("status") or "active")[:32],
    }


def _normalize_lore_entries(values: list[dict], fallback: list[dict]) -> list[dict]:
    entries: list[dict] = []
    for idx, item in enumerate(values[:160], start=1):
        if not isinstance(item, dict):
            continue
        keywords = str(item.get("keywords") or item.get("title") or item.get("name") or f"百科 {idx}").strip()
        content = str(item.get("content") or item.get("description") or item.get("text") or "").strip()
        if not keywords or not content:
            continue
        entries.append({"keywords": keywords[:100], "content": content[:6000]})
    if len(entries) < max(8, min(24, len(fallback) // 2)):
        existing = {entry["keywords"].lower() for entry in entries}
        for item in fallback:
            key = str(item.get("keywords") or "").lower()
            if key and key not in existing:
                entries.append(item)
                existing.add(key)
            if len(entries) >= 120:
                break
    return entries[:120]


def _normalize_world_entities(values: list[dict], fallback: list[dict]) -> list[dict]:
    entities = _dedupe_entities(values)
    if len(entities) < max(8, min(30, len(fallback) // 2)):
        entities = _dedupe_entities([*entities, *fallback])
    return entities[:120]


def _conversion_corpus(title: str, text: str, sections: list[dict], image_urls: list[str]) -> str:
    text = _clean_import_text(text)
    toc = "\n".join(
        f"{s['index']:02d}. [{s['kind']}] {s['title']} - {len(s['text'])}字"
        for s in sections[:36]
    )
    section_briefs = []
    for section in sections[:24]:
        excerpt = section["text"][:1400].strip()
        section_briefs.append(
            f"## {section['index']:02d}. {section['title']} [{section['kind']}]\n{excerpt}"
        )
    first = text[:12000]
    middle = text[max(0, len(text) // 2 - 6000): len(text) // 2 + 6000] if len(text) > 26000 else ""
    ending = text[-12000:] if len(text) > 14000 else ""
    candidates = {
        "characters": _pick_lines(text, r"人物|角色|NPC|PC|调查员|姓名|登场", 36),
        "locations": _pick_lines(text, r"地图|地点|房间|区域|楼层|场所|路线|入口|大厅|走廊|室", 36),
        "clues": _pick_lines(text, r"线索|证据|道具|物品|手卡|资料|情报|秘密|判定", 36),
    }
    parts = [
        f"剧本：{title}",
        f"全文长度：{len(text)} 字符；分段数：{len(sections)}；图片数：{len(image_urls)}",
        "可用图片 URL：" + json.dumps(image_urls, ensure_ascii=False),
        "\n# 自动分段目录\n" + (toc or "无"),
        "\n# 候选信息\n" + json.dumps(candidates, ensure_ascii=False, indent=2),
        "\n# 分段摘录\n" + "\n\n".join(section_briefs),
        "\n# 原文开头\n" + first,
    ]
    if middle:
        parts.append("\n# 原文中段\n" + middle)
    if ending:
        parts.append("\n# 原文结尾\n" + ending)
    corpus = "\n\n".join(parts)
    return corpus[:MAX_AI_CORPUS_CHARS]


def extract_campaign_document(
    raw: bytes,
    filename: str,
    assets_dir: str,
    ocr_enabled: bool = True,
    progress_callback: Callable[[int, str, list[str]], None] | None = None,
) -> tuple[str, list[str], list[str]]:
    result = extract_document_with_mineru(
        raw,
        filename,
        assets_dir,
        ocr_enabled=ocr_enabled,
        progress_callback=progress_callback,
    )
    return result.text, result.assets, result.warnings


def _campaign_structure_prompt(filename: str, *, source_hint: str = "") -> str:
    prompt = f"""
文件名：{filename}

任务：识别并分析剧本文档，将结果直接整理成 Z.R.I.C 可导入的结构化 JSON。

工作流程必须按以下顺序执行：
阶段 1 - 文档识别：
- 按页阅读 PDF/图片/辅助文本，先还原正文阅读顺序。
- 保留标题层级、表格字段、角色卡、手卡、判定规则、房间说明、线索列表、结局条件。
- 识别页眉页脚、页码、目录重复、广告水印、断行噪声并从结构化结果里剔除。

阶段 2 - 信息抽取：
- 列出主要章节、场景、地点、NPC、PC、组织、怪物/威胁、物品、线索、规则、时间线、真相和结局。
- 对每个场景判断：入口条件、地点、在场人物、可见信息、隐藏信息、可调查线索、判定、失败/成功后果、可前往地点。
- 对角色判断：PC/NPC、公开信息、秘密、动机、关系、属性/技能/HP/SAN/物品。

阶段 3 - 可玩结构设计：
- 把章节和地点拆成 nodes。节点不是摘要，必须能让 GM 直接主持。
- 把调查路径、空间移动、剧情推进、关键选择整理为 options；保证从节点 1 可以到达主要节点。
- 把地点整理为 map_rooms，并用 node_id 绑定最相关节点；map_edges 表示空间路径、剧情推进或调查关联。
- 把可检索信息拆成 lorebook 和 knowledge_documents，粒度适合 RAG 检索。
- 把所有重要人物、地点、组织、道具、线索和威胁写入 world_entities。

阶段 4 - 一致性校验：
- 检查 node_id、next_node_id、map room node_id、map edge from_id/to_id 都指向存在的对象。
- 检查角色、百科、实体不要空泛重复；重要原文信息不能只出现在 source_markdown 而没有进入结构化字段。
- 若原文缺失 HP/SAN，用 PC/NPC 默认 100/80；不要编造姓名、真相、结局和关键线索。

必须输出单个 JSON 对象，字段如下：
- source_markdown: string，按原文顺序整理出的 Markdown 正文，用于写入知识库；保留章节、表格要点、手卡、规则、线索和剧情流程。
- worldview: string，概括但不要丢失核心世界观、背景、真相和跑团基调。
- session_memory: string，初始化跑团记忆日志。
- characters: array，元素包含 name, role, hp, san, inventory, personality, status；role 只能是 PC 或 NPC。
- nodes: array，元素包含 id, name, summary, content, scene_image；id 从 1 开始。
- options: array，元素包含 node_id, text, next_node_id。
- lorebook: array，元素包含 keywords, content。
- triggers: array，可为空。
- world_entities: array，元素包含 entity_type, name, location, status, state_desc。
- map_rooms: array，元素包含 id, map_id, label, x, y, w, h, description, state, color, node_id, floor。
- map_edges: array，元素包含 id, map_id, from_id, to_id, label, locked, key_item, edge_type。
- knowledge_documents: array，可选；元素包含 title, text，用来补充 source_markdown 中的可检索知识分段。

结构化要求：
1. 这不是摘要任务。请把 PDF/图片里的剧本内容拆成可主持、可游玩的结构。
2. nodes 至少覆盖主要剧情场景、调查地点、关键冲突、结局或分支；每个节点 content 写清「场景描述 / 可见信息 / 隐藏信息 / 线索 / NPC 状态 / 判定 / 后果」。
3. options 形成可走的主线和关键分支，不能只有空数组；每条 option 的 text 用玩家可理解的行动短句。
4. characters 要区分 PC/NPC；预设调查员或玩家角色列为 PC；技能、属性、物品、秘密、动机写进 inventory/personality。
5. lorebook 拆分世界观、真相、时间线、规则、线索、道具、手卡、重要地点和组织；不要把全文塞进单条。
6. world_entities 记录 NPC、地点、组织、物品、威胁、隐藏线索和当前状态。
7. map_rooms 从场景地点生成，并尽量绑定 node_id；没有明确空间关系时按剧情/调查顺序生成 map_edges。
8. OCR/视觉识别可能有噪声；忽略页眉页脚、页码、目录重复、断行和明显乱码。
9. 无法识别的局部用「[无法识别]」标记，不要编造原文没有的关键事实。
10. 输出前自检 JSON 可解析性：双引号、逗号、数组和对象必须合法；只输出 JSON，不要 Markdown 代码块，不要解释。
"""
    if source_hint:
        prompt += f"\n本地抽取到的辅助文本如下，可用于校对视觉识别，不要只复述它：\n{source_hint[:16000]}\n"
    return prompt.strip()


def _flatten_structured_payload(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    flattened = dict(data)
    campaign = data.get("campaign")
    if isinstance(campaign, dict):
        for key, value in campaign.items():
            flattened.setdefault(key, value)
    for map_key in ("map", "map_data"):
        map_data = data.get(map_key)
        if isinstance(map_data, dict):
            for key in ("map_rooms", "map_edges"):
                if key in map_data and key not in flattened:
                    flattened[key] = map_data[key]
    return flattened


def _parse_structured_campaign_response(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    parsed = json_repair.loads(text.strip())
    if not isinstance(parsed, dict):
        raise ValueError("模型返回的根结构不是 JSON 对象")
    return _flatten_structured_payload(parsed)


def _structured_payload_source_text(payload: dict, title: str) -> str:
    payload = _flatten_structured_payload(payload)
    preferred = ""
    for key in ("source_markdown", "recognized_markdown", "recognized_text", "ocr_text", "full_text"):
        value = str(payload.get(key) or "").strip()
        if len(value) > len(preferred):
            preferred = value
    parts: list[str] = []
    if preferred:
        parts.append(preferred)
    worldview = str(payload.get("worldview") or "").strip()
    if worldview:
        parts.append(f"# 世界观\n\n{worldview}")
    characters = payload.get("characters") if isinstance(payload.get("characters"), list) else []
    if characters:
        lines = ["# 角色"]
        for item in characters[:120]:
            if not isinstance(item, dict):
                continue
            lines.append(
                "## {name}\nrole: {role}\nhp: {hp}\nsan: {san}\n{inventory}\n{personality}".format(
                    name=str(item.get("name") or "未命名角色"),
                    role=str(item.get("role") or ""),
                    hp=str(item.get("hp") or ""),
                    san=str(item.get("san") or ""),
                    inventory=str(item.get("inventory") or ""),
                    personality=str(item.get("personality") or ""),
                ).strip()
            )
        parts.append("\n\n".join(lines))
    lorebook = payload.get("lorebook") if isinstance(payload.get("lorebook"), list) else []
    if lorebook:
        lines = ["# 百科与线索"]
        for item in lorebook[:160]:
            if isinstance(item, dict):
                lines.append(f"## {item.get('keywords') or item.get('title') or '条目'}\n{item.get('content') or item.get('text') or ''}".strip())
        parts.append("\n\n".join(lines))
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
    if nodes:
        lines = ["# 场景节点"]
        for item in nodes[:120]:
            if isinstance(item, dict):
                lines.append(
                    f"## {item.get('name') or '场景'}\n"
                    f"{item.get('summary') or ''}\n\n{item.get('content') or ''}".strip()
                )
        parts.append("\n\n".join(lines))
    knowledge_docs = payload.get("knowledge_documents") if isinstance(payload.get("knowledge_documents"), list) else []
    if knowledge_docs:
        lines = ["# 知识库分段"]
        for item in knowledge_docs[:80]:
            if isinstance(item, dict):
                lines.append(f"## {item.get('title') or '知识'}\n{item.get('text') or item.get('content') or ''}".strip())
        parts.append("\n\n".join(lines))
    text = _clean_import_text("\n\n".join(part for part in parts if part.strip()))
    return text or f"# {title}\n\n多模态模型已返回结构化剧本，但未提供可写入知识库的 source_markdown。"


def convert_structured_campaign_payload(
    title: str,
    text: str,
    image_urls: list[str],
    payload: dict,
) -> tuple[dict, dict, list[str]]:
    warnings: list[str] = []
    flattened = _flatten_structured_payload(payload)
    source_text = _clean_import_text(text) or _structured_payload_source_text(flattened, title)
    campaign, map_data = normalize_imported_campaign(flattened, title, source_text, image_urls)
    if len(campaign.get("nodes", [])) <= 1:
        warnings.append("多模态模型返回的场景节点过少，已混合本地启发式结果生成可玩结构。")
    return campaign, map_data, warnings


def multimodal_extract_campaign_document(
    raw: bytes,
    filename: str,
    assets_dir: str,
    progress_callback: Callable[[int, str, list[str]], None] | None = None,
) -> tuple[str, list[str], list[str], dict | None]:
    """Use the configured campaign model as a PDF/vision fallback when MinerU returns no text."""
    warnings: list[str] = []

    def notify(progress: int, message: str) -> None:
        if progress_callback:
            progress_callback(progress, message, list(warnings))

    suffix = re.sub(r".*(\.[^.]+)$", r"\1", filename or "").lower()
    notify(39, "MinerU 未提取到正文，正在准备多模态剧本解析兜底")

    if not ai_provider.is_configured():
        warnings.append("多模态 OCR 兜底不可用：OpenAI 兼容端点尚未配置。")
        payload = prepare_document_vision_fallback(raw, filename, assets_dir, max_images=VISION_FALLBACK_MAX_IMAGES)
        warnings.extend(item for item in payload.warnings if item and item not in warnings)
        return _clean_import_text(payload.text), payload.assets, warnings, None

    campaign_model = ai_provider.get_active_model("campaign").strip()
    if not campaign_model:
        campaign_model = ai_provider.get_active_model("chat").strip()
        warnings.append("未配置剧本解析模型，多模态 OCR 兜底已回退使用 Chat Model。")
    if not campaign_model:
        warnings.append("多模态 OCR 兜底不可用：未选择剧本解析模型或 Chat Model。")
        payload = prepare_document_vision_fallback(raw, filename, assets_dir, max_images=VISION_FALLBACK_MAX_IMAGES)
        warnings.extend(item for item in payload.warnings if item and item not in warnings)
        return _clean_import_text(payload.text), payload.assets, warnings, None

    import_settings = get_campaign_import_settings()
    timeout_seconds = max(45.0, float(import_settings["ai_timeout_seconds"]))

    if suffix == ".pdf" and len(raw) <= MULTIMODAL_PDF_MAX_BYTES:
        notify(41, "正在将 PDF 直接发送给剧本解析模型")
        try:
            parsed = _call_multimodal_pdf_campaign(
                raw,
                filename,
                campaign_model,
                timeout_seconds=timeout_seconds,
            )
            source_text = _structured_payload_source_text(parsed, filename)
            warnings.append("MinerU 未返回正文，已由剧本解析模型直接识别 PDF 并输出结构化剧本。")
            assets: list[str] = []
            try:
                notify(48, "PDF 结构化完成，正在保存可迁移图片资源")
                payload_for_assets = prepare_document_vision_fallback(raw, filename, assets_dir, max_images=VISION_FALLBACK_MAX_IMAGES)
                assets = payload_for_assets.assets
                for item in payload_for_assets.warnings:
                    if item and item not in warnings:
                        warnings.append(item)
            except Exception as exc:
                warnings.append(f"PDF 已结构化，但本地图片保存失败：{type(exc).__name__}: {exc}")
            notify(50, "PDF 多模态结构化完成，继续写入剧本")
            return source_text, assets, warnings, parsed
        except Exception as exc:
            warnings.append(f"PDF 直发多模态模型失败，改用本地文本/图片兜底：{type(exc).__name__}: {exc}")
    elif suffix == ".pdf":
        warnings.append(
            f"PDF 文件超过多模态直发上限 {MULTIMODAL_PDF_MAX_BYTES // 1024 // 1024}MB，改用本地文本/图片兜底。"
        )

    notify(42, "正在准备本地文本与图片兜底")
    payload = prepare_document_vision_fallback(
        raw,
        filename,
        assets_dir,
        max_images=VISION_FALLBACK_MAX_IMAGES,
    )
    for item in payload.warnings:
        if item and item not in warnings:
            warnings.append(item)

    local_text = _clean_import_text(payload.text)
    if local_text and len(local_text) >= 1200 and not payload.images:
        warnings.append("MinerU 未返回正文，已使用本地文本抽取结果继续结构化剧本。")
        notify(48, "已取得本地文本，继续转换为可玩剧本")
        return local_text, payload.assets, warnings, None

    if not payload.images:
        if local_text:
            warnings.append("多模态 OCR 未获得页面图片，已使用本地可抽取文本继续导入。")
        else:
            warnings.append("多模态 OCR 兜底无法启动：没有可识别的页面图片。")
        return local_text, payload.assets, warnings, None

    notify(44, "正在调用剧本解析模型结构化本地文本与图片")
    try:
        parsed = _call_multimodal_images_campaign(
            filename,
            payload.images,
            campaign_model,
            local_text,
            timeout_seconds=timeout_seconds,
        )
        source_text = _structured_payload_source_text(parsed, filename)
        warnings.append("MinerU 未返回正文，已由剧本解析模型识别本地文本/图片并输出结构化剧本。")
        notify(50, "多模态结构化完成，继续写入剧本")
        return source_text, payload.assets, warnings, parsed
    except Exception as exc:
        warnings.append(f"多模态结构化剧本失败，改用逐批 OCR 文本兜底：{type(exc).__name__}: {exc}")

    notify(42, "正在调用剧本解析模型识别本地文本与文档图片")
    recognized_parts: list[str] = []
    image_batches = [
        payload.images[idx: idx + VISION_FALLBACK_BATCH_SIZE]
        for idx in range(0, len(payload.images), VISION_FALLBACK_BATCH_SIZE)
    ]

    for batch_index, batch in enumerate(image_batches, start=1):
        start_no = (batch_index - 1) * VISION_FALLBACK_BATCH_SIZE + 1
        end_no = start_no + len(batch) - 1
        notify(
            min(49, 42 + batch_index),
            f"多模态识别第 {start_no}-{end_no} 张页面/图片",
        )
        try:
            recognized = _call_multimodal_ocr_batch(
                filename,
                batch,
                campaign_model,
                local_text if batch_index == 1 else "",
                timeout_seconds=timeout_seconds,
            )
            if recognized.strip():
                recognized_parts.append(
                    f"# 多模态识别片段 {batch_index}\n\n{recognized.strip()}"
                )
        except Exception as exc:
            warnings.append(f"多模态 OCR 批次 {batch_index} 失败：{type(exc).__name__}: {exc}")

    recognized_text = _clean_import_text("\n\n".join(recognized_parts))
    if recognized_text:
        warnings.append("MinerU 未返回正文，已改用剧本解析模型进行多模态识别并继续结构化导入。")
        notify(50, "多模态识别完成，继续转换为可玩剧本")
        merged_text = "\n\n".join(part for part in [local_text, recognized_text] if part).strip()
        return merged_text, payload.assets, warnings, None

    if local_text:
        warnings.append("多模态 OCR 未返回可用正文，已使用本地可抽取文本继续导入。")
        return local_text, payload.assets, warnings, None

    warnings.append("多模态 OCR 未返回可用正文。")
    return "", payload.assets, warnings, None


def _call_multimodal_pdf_campaign(
    raw: bytes,
    filename: str,
    model: str,
    *,
    timeout_seconds: float,
) -> dict:
    from . import ai_provider as provider

    system_prompt = (
        "你是中文 TRPG PDF 剧本识别与结构化导入器。"
        "你会直接阅读 PDF 附件，识别文字、表格、版式、手卡、地图和图片中的剧本信息，"
        "并输出可被程序解析的 JSON 对象。"
    )
    pdf_b64 = base64.b64encode(raw).decode("ascii")
    variants = [
        {"type": "file", "file": {"filename": filename or "campaign.pdf", "file_data": f"data:application/pdf;base64,{pdf_b64}"}},
        {"type": "file", "file": {"filename": filename or "campaign.pdf", "file_data": pdf_b64}},
    ]
    last_error: Exception | None = None
    for file_part in variants:
        content = [
            {"type": "text", "text": _campaign_structure_prompt(filename)},
            file_part,
        ]
        try:
            response = provider.chat_completion(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
                model=model,
                temperature=0.1,
                max_tokens=12000,
                json_mode=True,
                timeout=timeout_seconds,
                apply_token_policy=False,
            )
            result = response.choices[0].message.content
            if not result:
                raise ValueError("模型返回了空内容")
            return _parse_structured_campaign_response(result)
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("PDF 多模态结构化失败")


def _call_multimodal_images_campaign(
    filename: str,
    images: list[VisionFallbackImage],
    model: str,
    local_text: str = "",
    *,
    timeout_seconds: float = 45.0,
) -> dict:
    from . import ai_provider as provider

    system_prompt = (
        "你是中文 TRPG 剧本文档视觉识别与结构化导入器。"
        "你会结合本地抽取文本和页面图片，输出可被程序解析的 JSON 对象。"
    )
    text_prompt = _campaign_structure_prompt(filename, source_hint=local_text)
    content: list[dict] = [{"type": "text", "text": text_prompt}]
    for image in images:
        content.append({"type": "text", "text": f"页面/图片：{image.label}（{image.width}x{image.height}）"})
        content.append({"type": "image_url", "image_url": {"url": image.data_url}})
    response = provider.chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        model=model,
        temperature=0.1,
        max_tokens=12000,
        json_mode=True,
        timeout=timeout_seconds,
        apply_token_policy=False,
    )
    result = response.choices[0].message.content
    if not result:
        raise ValueError("模型返回了空内容")
    return _parse_structured_campaign_response(result)


def _call_multimodal_ocr_batch(
    filename: str,
    images: list[VisionFallbackImage],
    model: str,
    local_text: str = "",
    *,
    timeout_seconds: float = 45.0,
) -> str:
    from . import ai_provider as provider

    system_prompt = (
        "你是中文 TRPG 剧本文档 OCR 与整理器。"
        "请根据图片逐页识别正文，保留章节、人物、地点、线索、道具、规则、手卡、判定和剧情流程。"
        "不要写摘要，不要编造无法看清的内容；无法识别处用「[无法识别]」标注。"
        "输出 Markdown 正文即可。"
    )
    text_prompt = (
        f"文件名：{filename}\n"
        f"本批图片数：{len(images)}\n\n"
        "任务：按图片顺序 OCR 并整理为可继续转换为 Z.R.I.C 剧本的 Markdown 正文。"
        "如果图片是整页扫描，请尽量保留页面里的标题层级和表格/列表信息。"
    )
    if local_text:
        text_prompt += (
            "\n\n本地抽取到的少量文本如下，可用于校对图片 OCR，但不要只复述它：\n"
            f"{local_text[:12000]}"
        )
    content: list[dict] = [{"type": "text", "text": text_prompt}]
    for image in images:
        content.append({"type": "text", "text": f"图片：{image.label}（{image.width}x{image.height}）"})
        content.append({"type": "image_url", "image_url": {"url": image.data_url}})
    response = provider.chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        model=model,
        temperature=0.1,
        max_tokens=7000,
        json_mode=False,
        timeout=timeout_seconds,
        apply_token_policy=False,
    )
    result = response.choices[0].message.content
    if not result:
        raise ValueError("模型返回了空内容")
    return result.strip()


def fallback_campaign(title: str, text: str, first_image_url: str = "") -> dict:
    opening = text.strip()[:1800] or "原始文档未能提取出可用正文。请在导入后手动补充开场场景。"
    return {
        "worldview": f"【{title}】\n\n由原始剧本文档导入生成。AI 转换不可用时创建了保底剧本，请在客户端继续整理节点、地图与触发器。",
        "session_memory": "【跑团记忆日志已初始化】\n",
        "characters": [
            {"name": "玩家", "role": "PC", "hp": 100, "san": 80, "inventory": "", "status": "active"}
        ],
        "nodes": [
            {
                "id": 1,
                "name": "开场",
                "summary": "由导入文档生成的起始场景",
                "content": opening,
                "expanded_content": "",
                "scene_image": first_image_url,
            }
        ],
        "options": [],
        "lorebook": [],
        "triggers": [],
        "world_entities": [],
        "timelines": [],
        "rag_library": [],
        "memory_l1": [],
        "pending_effects": [],
        "npc_chat_logs": [],
    }


def heuristic_campaign_from_text(title: str, text: str, image_urls: list[str]) -> tuple[dict, dict]:
    sections = split_campaign_sections(text, max_sections=36)
    campaign = fallback_campaign(title, text, image_urls[0] if image_urls else "")
    if not sections:
        return campaign, {"map_rooms": [], "map_edges": []}

    characters = _extract_characters_from_sections(sections) or campaign["characters"]
    campaign["characters"] = characters

    scene_sections = [s for s in sections if s["kind"] in {"scene", "ending"}]
    if len(scene_sections) < 3:
        scene_sections = sections[: min(len(sections), 24)]
    nodes = []
    for idx, section in enumerate(scene_sections[:36], start=1):
        image = image_urls[(idx - 1) % len(image_urls)] if image_urls and idx <= len(image_urls) else ""
        nodes.append({
            "id": idx,
            "name": section["title"][:80] or f"场景 {idx}",
            "summary": section["text"][:240].replace("\n", " "),
            "content": section["text"][:6000],
            "expanded_content": "",
            "scene_image": image,
        })
    if nodes:
        campaign["nodes"] = nodes
        campaign["options"] = [
            {"node_id": idx, "text": f"前往：{nodes[idx]['name']}", "next_node_id": idx + 1}
            for idx in range(1, len(nodes))
        ]

    campaign["lorebook"] = _extract_lore_entries(sections)
    campaign["world_entities"] = [
        {
            "entity_type": "npc" if char.get("role") != "PC" else "pc",
            "name": char.get("name", ""),
            "location": "",
            "status": char.get("status", "active"),
            "state_desc": "\n".join(
                part for part in [
                    str(char.get("personality") or ""),
                    f"HP:{char.get('hp', 100)} SAN:{char.get('san', 80)}",
                    f"物品/属性：{char.get('inventory')}" if char.get("inventory") else "",
                ]
                if part
            ),
        }
        for char in characters
        if char.get("name")
    ]
    for location in _extract_location_names(sections, campaign["nodes"]):
        campaign["world_entities"].append({
            "entity_type": "location",
            "name": location["name"],
            "location": location["name"],
            "status": "active",
            "state_desc": location["description"],
        })
    campaign["world_entities"].extend(_extract_clue_entities(sections))
    campaign["world_entities"] = _dedupe_entities(campaign["world_entities"])[:120]

    rooms = []
    locations = _extract_location_names(sections, campaign["nodes"])
    room_sources = locations[:48] or [
        {"name": node["name"], "description": node.get("summary") or node.get("content", "")}
        for node in campaign["nodes"][:36]
    ]
    for idx, source in enumerate(room_sources[:48], start=1):
        col = (idx - 1) % 4
        row = (idx - 1) // 4
        linked_node = campaign["nodes"][idx - 1]["id"] if idx - 1 < len(campaign["nodes"]) else None
        rooms.append({
            "id": idx,
            "map_id": 1,
            "label": source["name"][:40],
            "x": 80 + col * 170,
            "y": 80 + row * 120,
            "w": 130,
            "h": 76,
            "description": source["description"],
            "state": "unknown",
            "color": "#1e3a2f",
            "node_id": linked_node,
            "floor": 1,
        })
    edges = [
        {
            "id": idx,
            "map_id": 1,
            "from_id": idx,
            "to_id": idx + 1,
            "label": "剧情推进",
            "locked": 0,
            "key_item": "",
            "edge_type": "normal",
        }
        for idx in range(1, len(rooms))
    ]
    return campaign, {"map_rooms": rooms, "map_edges": edges}


def normalize_imported_campaign(raw: dict, title: str, text: str, image_urls: list[str]) -> tuple[dict, dict]:
    heuristic_campaign, heuristic_map = heuristic_campaign_from_text(title, text, image_urls)
    campaign = heuristic_campaign
    if not isinstance(raw, dict):
        return campaign, heuristic_map

    campaign["worldview"] = str(raw.get("worldview") or campaign["worldview"])
    campaign["session_memory"] = str(raw.get("session_memory") or campaign["session_memory"])

    nodes = raw.get("nodes") if isinstance(raw.get("nodes"), list) else []
    normalized_nodes = []
    old_to_new = {}
    for idx, node in enumerate(nodes[:80], start=1):
        if not isinstance(node, dict):
            continue
        old_id = node.get("id", idx)
        old_to_new[str(old_id)] = len(normalized_nodes) + 1
        normalized_nodes.append({
            "id": len(normalized_nodes) + 1,
            "name": str(node.get("name") or f"场景 {idx}")[:80],
            "summary": str(node.get("summary") or "")[:300],
            "content": str(node.get("content") or "")[:6000],
            "expanded_content": str(node.get("expanded_content") or ""),
            "scene_image": str(node.get("scene_image") or ""),
        })
    if normalized_nodes:
        campaign["nodes"] = normalized_nodes
    if image_urls and not any(n.get("scene_image") for n in campaign["nodes"]):
        campaign["nodes"][0]["scene_image"] = image_urls[0]

    valid_node_ids = {n["id"] for n in campaign["nodes"]}
    options = []
    for opt in (raw.get("options") if isinstance(raw.get("options"), list) else [])[:160]:
        if not isinstance(opt, dict):
            continue
        node_id = old_to_new.get(str(opt.get("node_id")), opt.get("node_id"))
        next_id = old_to_new.get(str(opt.get("next_node_id")), opt.get("next_node_id"))
        try:
            node_id = int(node_id)
            next_id = int(next_id)
        except (TypeError, ValueError):
            continue
        if node_id in valid_node_ids and next_id in valid_node_ids:
            options.append({"node_id": node_id, "text": str(opt.get("text") or "继续")[:200], "next_node_id": next_id})
    if options:
        campaign["options"] = options
    elif not campaign.get("options") and len(campaign.get("nodes", [])) > 1:
        campaign["options"] = [
            {
                "node_id": campaign["nodes"][idx - 1]["id"],
                "text": f"前往：{campaign['nodes'][idx]['name']}",
                "next_node_id": campaign["nodes"][idx]["id"],
            }
            for idx in range(1, len(campaign["nodes"]))
        ]

    def list_of_dicts(key: str, limit: int) -> list[dict]:
        values = raw.get(key) if isinstance(raw.get(key), list) else []
        return [v for v in values[:limit] if isinstance(v, dict)]

    raw_characters = list_of_dicts("characters", 40)
    if raw_characters:
        campaign["characters"] = [
            _normalize_character(item, f"角色 {idx}")
            for idx, item in enumerate(raw_characters, start=1)
        ]
        if not any(char.get("role") == "PC" for char in campaign["characters"]):
            campaign["characters"].insert(0, _normalize_character({"name": "调查员", "role": "PC"}, "调查员"))
    else:
        campaign["characters"] = [
            _normalize_character(item, f"角色 {idx}")
            for idx, item in enumerate(campaign.get("characters", []), start=1)
        ]
    campaign["lorebook"] = _normalize_lore_entries(list_of_dicts("lorebook", 160), heuristic_campaign.get("lorebook", []))
    knowledge_documents = []
    for idx, item in enumerate(list_of_dicts("knowledge_documents", 80), start=1):
        doc_title = str(item.get("title") or item.get("name") or f"知识分段 {idx:02d}").strip()[:80]
        body = str(item.get("text") or item.get("content") or "").strip()
        if doc_title and body:
            knowledge_documents.append({"title": doc_title, "text": body[:MAX_KNOWLEDGE_SECTION_CHARS]})
    if knowledge_documents:
        campaign["knowledge_documents"] = knowledge_documents
    campaign["triggers"] = list_of_dicts("triggers", 60)
    character_entities = [
        {
            "entity_type": "pc" if char.get("role") == "PC" else "npc",
            "name": char.get("name", ""),
            "location": "",
            "status": char.get("status", "active"),
            "state_desc": "\n".join(
                part for part in [
                    str(char.get("personality") or ""),
                    f"HP:{char.get('hp', 100)} SAN:{char.get('san', 80)}",
                    f"物品/属性：{char.get('inventory')}" if char.get("inventory") else "",
                ]
                if part
            ),
        }
        for char in campaign.get("characters", [])
        if char.get("name")
    ]
    campaign["world_entities"] = _normalize_world_entities(
        [*list_of_dicts("world_entities", 160), *character_entities],
        heuristic_campaign.get("world_entities", []),
    )
    campaign["timelines"] = list_of_dicts("timelines", 20)

    map_rooms = list_of_dicts("map_rooms", 120)
    map_edges = list_of_dicts("map_edges", 240)
    if not map_rooms and isinstance(raw.get("map"), dict):
        map_rooms = [v for v in raw["map"].get("map_rooms", []) if isinstance(v, dict)]
        map_edges = [v for v in raw["map"].get("map_edges", []) if isinstance(v, dict)]

    if len(campaign["nodes"]) < max(3, min(12, len(heuristic_campaign.get("nodes", [])) // 2)):
        ai_worldview = campaign.get("worldview")
        ai_memory = campaign.get("session_memory")
        ai_characters = campaign.get("characters") or []
        ai_lorebook = campaign.get("lorebook") or []
        ai_entities = campaign.get("world_entities") or []
        ai_knowledge_documents = campaign.get("knowledge_documents") or []
        campaign = heuristic_campaign
        campaign["worldview"] = ai_worldview or campaign["worldview"]
        campaign["session_memory"] = ai_memory or campaign["session_memory"]
        if ai_characters:
            campaign["characters"] = ai_characters
        if ai_lorebook:
            campaign["lorebook"] = _normalize_lore_entries(ai_lorebook, heuristic_campaign.get("lorebook", []))
        if ai_entities:
            campaign["world_entities"] = _normalize_world_entities(ai_entities, heuristic_campaign.get("world_entities", []))
        if ai_knowledge_documents:
            campaign["knowledge_documents"] = ai_knowledge_documents

    valid_node_ids = {int(n.get("id")) for n in campaign.get("nodes", []) if str(n.get("id", "")).isdigit()}
    normalized_rooms = []
    old_room_to_new = {}
    for idx, room in enumerate((map_rooms or heuristic_map.get("map_rooms", []))[:120], start=1):
        if not isinstance(room, dict):
            continue
        old_id = room.get("id", idx)
        old_room_to_new[str(old_id)] = len(normalized_rooms) + 1
        node_id = room.get("node_id")
        try:
            node_id = int(node_id) if node_id not in (None, "") else None
        except (TypeError, ValueError):
            node_id = None
        if node_id not in valid_node_ids:
            node_id = None
        normalized_rooms.append({
            "id": len(normalized_rooms) + 1,
            "map_id": int(room.get("map_id") or 1),
            "label": str(room.get("label") or room.get("name") or f"房间 {idx}")[:40],
            "x": int(room.get("x") or (80 + ((idx - 1) % 4) * 170)),
            "y": int(room.get("y") or (80 + ((idx - 1) // 4) * 120)),
            "w": int(room.get("w") or 130),
            "h": int(room.get("h") or 76),
            "description": str(room.get("description") or "")[:1000],
            "state": str(room.get("state") or "unknown"),
            "color": str(room.get("color") or "#1e3a2f"),
            "node_id": node_id,
            "floor": int(room.get("floor") or 1),
        })
    valid_room_ids = {room["id"] for room in normalized_rooms}
    normalized_edges = []
    for idx, edge in enumerate((map_edges or heuristic_map.get("map_edges", []))[:240], start=1):
        if not isinstance(edge, dict):
            continue
        from_id = old_room_to_new.get(str(edge.get("from_id")), edge.get("from_id"))
        to_id = old_room_to_new.get(str(edge.get("to_id")), edge.get("to_id"))
        try:
            from_id = int(from_id)
            to_id = int(to_id)
        except (TypeError, ValueError):
            continue
        if from_id not in valid_room_ids or to_id not in valid_room_ids:
            continue
        normalized_edges.append({
            "id": len(normalized_edges) + 1,
            "map_id": int(edge.get("map_id") or 1),
            "from_id": from_id,
            "to_id": to_id,
            "label": str(edge.get("label") or "")[:80],
            "locked": int(edge.get("locked") or 0),
            "key_item": str(edge.get("key_item") or "")[:120],
            "edge_type": str(edge.get("edge_type") or "normal")[:40],
        })
    map_data = {"map_rooms": normalized_rooms, "map_edges": normalized_edges}
    return campaign, map_data


def ai_convert_campaign(title: str, text: str, image_urls: list[str]) -> tuple[dict, dict, list[str]]:
    warnings = []
    import_settings = get_campaign_import_settings()
    if not ai_provider.is_configured():
        warnings.append("OpenAI 兼容端点尚未配置，已生成保底剧本。")
        campaign, map_data = heuristic_campaign_from_text(title, text, image_urls)
        return campaign, map_data, warnings

    campaign_model = ai_provider.get_active_model("campaign").strip()
    if not campaign_model:
        campaign_model = ai_provider.get_active_model("chat").strip()
        warnings.append("未配置剧本解析模型，已回退使用 Chat Model。")

    clean_text = _clean_import_text(text)
    sections = split_campaign_sections(clean_text, max_sections=48)
    corpus = _conversion_corpus(title, clean_text, sections, image_urls)
    target_node_count = max(6, min(36, len([s for s in sections if s["kind"] in {"scene", "ending"}]) or len(sections) or 8))
    system_prompt = (
        "你是资深 TRPG 剧本结构化转换器和跑团模组编辑。"
        "你的任务不是复述 OCR 文本，而是把剧本文档加工成 Z.R.I.C 引擎可直接导入和游玩的结构："
        "世界观、PC/NPC、场景节点、选项图、百科/线索、世界实体、地图房间与连接。"
        "你必须先理解剧本运行方式，再输出严格 JSON。所有内容用中文。"
    )
    user_prompt = f"""
剧本名称：{title}

请按以下工作流程处理输入：

阶段 1：阅读与清洗
- 阅读自动分段目录、候选信息、分段摘录、原文开头/中段/结尾。
- 还原文档本来的章节顺序，忽略 OCR 造成的页码、页眉页脚、目录重复、换行断裂和明显乱码。
- 保留 GM 真正需要的信息：场景说明、NPC 反应、线索、判定、规则、手卡、地图、结局条件。

阶段 2：抽取剧本资产
- 抽取 PC/NPC、组织、地点、物品、线索、怪物/威胁、规则、时间线、背景真相、结局。
- 对每个场景识别：地点、进入条件、可见信息、隐藏信息、可互动对象、判定、成功/失败后果、可转向节点。
- 对每个角色识别：身份、公开描述、秘密/动机、关系、属性/技能、HP/SAN、装备/物品。

阶段 3：设计可玩结构
- nodes 是 GM 现场主持用的节点，不是章节摘要。每个 content 必须包含足够主持的信息。
- options 是从当前节点出发的玩家行动或剧情推进，必须让主要节点可达。
- lorebook 是检索百科，按世界观、线索、道具、规则、手卡、重要地点、真相、时间线拆条。
- world_entities 是运行时实体表，记录 NPC、地点、组织、物品、线索和威胁的状态。
- map_rooms 从地点和场景生成；map_edges 表示空间路径、调查关联或剧情顺序。

阶段 4：校验输出
- 所有 node_id/next_node_id 必须指向存在节点；map edge 的 from_id/to_id 必须指向存在房间。
- 重要角色、地点、线索、真相不能只留在 worldview 或原文摘录里，必须进入对应结构字段。
- 不要凭空创造关键事实；原文没有 HP/SAN 时用 100/80。
- 输出必须是合法 JSON 对象，不能有 Markdown 代码块、注释或解释文本。

请输出字段：
- worldview: string
- session_memory: string
- characters: array，元素包含 name, role, hp, san, inventory, personality, status；inventory/personality 中必须保留原文里的属性、技能、物品、动机、秘密
- nodes: array，元素包含 id, name, summary, content, scene_image；id 从 1 开始
- options: array，元素包含 node_id, text, next_node_id
- lorebook: array，元素包含 keywords, content
- triggers: array，可为空
- world_entities: array，元素包含 entity_type, name, location, status, state_desc
- map_rooms: array，元素包含 id, map_id, label, x, y, w, h, description, state, color, node_id, floor
- map_edges: array，元素包含 id, map_id, from_id, to_id, label, locked, key_item, edge_type

要求：
1. 至少生成 {target_node_count} 个关键场景节点，除非原文确实更短；不能只生成“开场”或章节摘要。
2. 每个节点写清楚 GM 可直接主持的场景信息：地点、可见信息、隐藏信息、NPC状态、线索、判定、成功/失败后果、可能冲突。
3. options 必须形成可走的主线图；关键调查/分支可以用多个选项连接到不同节点。
4. characters 要区分 PC/NPC；如果原文有预设调查员/玩家角色，优先列为 PC。没有 HP/SAN 时用 100/80；发现 STR/DEX/技能/物品/秘密等信息时写入 inventory/personality。
5. lorebook 必须生成可检索百科：世界观、真相、时间线、线索、道具、规则、手卡、重要地点都要拆成独立条目；每条 content 应能独立回答一次检索问题，不要把整篇原文塞进单条 lore。
6. world_entities 必须记录重要 NPC、组织、地点、物品、威胁和隐藏线索，state_desc 写当前状态、秘密、关系或用途。
7. map_rooms 必须从场景地点生成，并尽量绑定相关 node_id；map_edges 表示路径、剧情推进或调查关系，没有空间关系时按剧情顺序连接。
8. 图片 URL 只在适合的节点 scene_image 中使用，不要改写 URL。
9. OCR 可能有噪声；忽略页眉页脚、页码、目录重复、断行和明显乱码。
10. 输出必须是单个 JSON 对象；不要只输出摘要，不要省略 characters/lorebook/world_entities/map_rooms。

结构化 OCR 输入：
{corpus}
"""
    try:
        from .agent import _call_ai
        raw = _call_ai(
            system_prompt,
            user_prompt,
            temperature=0.25,
            max_tokens=9000,
            json_mode=True,
            model_override=campaign_model,
            apply_token_policy=False,
            cacheable=False,
            timeout=float(import_settings["ai_timeout_seconds"]),
        )
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json_repair.loads(raw.strip())
        campaign, map_data = normalize_imported_campaign(parsed, title, text, image_urls)
        if len(campaign.get("nodes", [])) <= 1:
            warnings.append("AI 返回的场景节点过少，已使用本地分段兜底生成可玩结构。")
        return campaign, map_data, warnings
    except Exception as exc:
        warnings.append(f"AI 转换失败，已生成保底剧本：{type(exc).__name__}: {exc}")
        campaign, map_data = heuristic_campaign_from_text(title, text, image_urls)
        return campaign, map_data, warnings
