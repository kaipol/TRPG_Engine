"""Campaign save, asset, and ownership helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import urllib.parse
from collections.abc import Callable
from datetime import datetime

import fastapi


_BASE_DIR = ""
_CAMPAIGNS_DIR = ""
_LEGACY_CAMPAIGN_VALIDATOR: Callable[[str], bool] | None = None


def configure_campaign_storage(
    base_dir: str,
    campaigns_dir: str,
    legacy_campaign_validator: Callable[[str], bool],
) -> None:
    global _BASE_DIR, _CAMPAIGNS_DIR, _LEGACY_CAMPAIGN_VALIDATOR
    _BASE_DIR = os.path.realpath(base_dir)
    _CAMPAIGNS_DIR = os.path.realpath(campaigns_dir)
    _LEGACY_CAMPAIGN_VALIDATOR = legacy_campaign_validator


def _require_configured() -> tuple[str, str, Callable[[str], bool]]:
    if not _BASE_DIR or not _CAMPAIGNS_DIR or not _LEGACY_CAMPAIGN_VALIDATOR:
        raise RuntimeError("campaign storage has not been configured")
    return _BASE_DIR, _CAMPAIGNS_DIR, _LEGACY_CAMPAIGN_VALIDATOR


def format_mtime(path: str) -> str:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return ""


def folder_size_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for filename in files:
            try:
                total += os.path.getsize(os.path.join(root, filename))
            except OSError:
                pass
    return total


def campaign_summary(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "node_count": len(data.get("nodes") or []),
        "option_count": len(data.get("options") or []),
        "lore_count": len(data.get("lorebook") or []),
        "trigger_count": len(data.get("triggers") or []),
        "timeline_count": len(data.get("timelines") or []),
        "entity_count": len(data.get("world_entities") or []),
        "rag_count": len(data.get("rag_library") or []),
    }


def save_manifest_path(folder_path: str) -> str:
    return os.path.join(folder_path, "manifest.json")


def read_save_manifest(folder_path: str) -> dict:
    try:
        with open(save_manifest_path(folder_path), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_save_manifest(folder_path: str, updates: dict) -> dict:
    manifest = read_save_manifest(folder_path)
    manifest.update({k: v for k, v in updates.items() if v is not None})
    with open(save_manifest_path(folder_path), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def account_id(account: dict | None) -> int | None:
    try:
        return int(account["id"]) if account else None
    except (KeyError, TypeError, ValueError):
        return None


def manifest_owner_id(manifest: dict) -> int | None:
    try:
        owner = manifest.get("owner_account_id")
        return int(owner) if owner not in (None, "") else None
    except (TypeError, ValueError):
        return None


def account_owner_metadata(account: dict) -> dict:
    return {
        "owner_account_id": account_id(account),
        "owner_username": account.get("username", ""),
        "owner_display_name": account.get("display_name", "") or account.get("username", ""),
    }


def save_visible_to_account(manifest: dict, account: dict | None) -> bool:
    owner_id = manifest_owner_id(manifest)
    if owner_id is None:
        return True
    return owner_id == account_id(account)


def require_private_save_owner(folder_path: str, account: dict) -> dict:
    manifest = read_save_manifest(folder_path)
    owner_id = manifest_owner_id(manifest)
    if owner_id is None:
        raise fastapi.HTTPException(status_code=403, detail="该存档尚未绑定账号，不能作为账号存档修改")
    if owner_id != account_id(account):
        raise fastapi.HTTPException(status_code=403, detail="无权访问其他账号的存档")
    return manifest


def ensure_save_readable(folder_path: str, account: dict | None) -> dict:
    manifest = read_save_manifest(folder_path)
    if not save_visible_to_account(manifest, account):
        raise fastapi.HTTPException(status_code=403, detail="无权访问其他账号的存档")
    return manifest


def resolve_campaign_folder_name(campaign_name: str) -> tuple[str, str, str]:
    _base_dir, campaigns_dir, legacy_campaign_validator = _require_configured()
    name = urllib.parse.unquote((campaign_name or "").strip())
    if not name or os.path.isabs(name) or "\x00" in name or "/" in name or "\\" in name or name in {".", ".."}:
        raise fastapi.HTTPException(status_code=400, detail="非法存档名称")
    folder = os.path.realpath(os.path.join(campaigns_dir, name))
    try:
        if os.path.commonpath([campaigns_dir, folder]) != campaigns_dir:
            raise ValueError
    except ValueError:
        raise fastapi.HTTPException(status_code=400, detail="非法存档路径") from None
    campaign_json = os.path.join(folder, "campaign.json")
    if not os.path.isdir(folder) or not legacy_campaign_validator(campaign_json):
        raise fastapi.HTTPException(status_code=404, detail="存档不存在或格式无效")
    return name, folder, campaign_json


def cleanup_managed_campaign_folder(folder_path: str) -> None:
    for filename in ("campaign.json", "map.json", "manifest.json"):
        path = os.path.join(folder_path, filename)
        if os.path.isfile(path):
            os.remove(path)
    if os.path.isdir(folder_path):
        for filename in os.listdir(folder_path):
            if not filename.startswith("mp_map_"):
                continue
            path = os.path.join(folder_path, filename)
            if os.path.isfile(path):
                os.remove(path)
    for dirname in ("assets", "knowledge"):
        path = os.path.join(folder_path, dirname)
        if os.path.isdir(path):
            shutil.rmtree(path)


def sanitize_campaign_name(name: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:60] or f"导入剧本_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def sanitize_asset_name(name: str, default_ext: str = "") -> str:
    stem, ext = os.path.splitext(name or "")
    stem = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", stem).strip(" ._")
    ext = (ext or default_ext or "").lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return (stem[:48] or "asset") + ext[:12]


def unique_path(folder: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(folder, filename)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base}_{i}{ext}")
        i += 1
    return candidate


def campaign_asset_url(campaign_name: str, asset_name: str) -> str:
    return (
        f"/api/campaign-assets/{urllib.parse.quote(campaign_name)}/"
        f"{urllib.parse.quote(asset_name)}"
    )


def resolve_campaign_asset(campaign_name: str, asset_name: str) -> str:
    _base_dir, campaigns_dir, _legacy_campaign_validator = _require_configured()
    if not campaign_name or "/" in campaign_name or "\\" in campaign_name:
        raise fastapi.HTTPException(status_code=400, detail="非法剧本名")
    campaign_root = os.path.realpath(os.path.join(campaigns_dir, campaign_name))
    try:
        if os.path.commonpath([campaigns_dir, campaign_root]) != campaigns_dir:
            raise ValueError
    except ValueError:
        raise fastapi.HTTPException(status_code=403, detail="禁止访问") from None

    clean_asset_name = urllib.parse.unquote(asset_name or "").strip()
    if (
        not clean_asset_name
        or os.path.isabs(clean_asset_name)
        or "\x00" in clean_asset_name
        or "/" in clean_asset_name
        or "\\" in clean_asset_name
        or clean_asset_name in {".", ".."}
    ):
        raise fastapi.HTTPException(status_code=400, detail="非法资源名")

    direct_target = os.path.realpath(os.path.join(campaign_root, clean_asset_name))
    try:
        if os.path.commonpath([campaign_root, direct_target]) != campaign_root:
            raise ValueError
    except ValueError:
        raise fastapi.HTTPException(status_code=403, detail="禁止访问") from None
    if os.path.isfile(direct_target):
        return direct_target

    legacy_assets_root = os.path.realpath(os.path.join(campaign_root, "assets"))
    legacy_target = os.path.realpath(os.path.join(legacy_assets_root, clean_asset_name))
    try:
        if os.path.commonpath([legacy_assets_root, legacy_target]) != legacy_assets_root:
            raise ValueError
    except ValueError:
        raise fastapi.HTTPException(status_code=403, detail="禁止访问") from None
    if os.path.isfile(legacy_target):
        return legacy_target

    raise fastapi.HTTPException(status_code=404, detail="资源不存在")
