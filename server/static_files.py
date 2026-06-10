"""Static web file serving for the bundled Z.R.I.C frontend."""

from __future__ import annotations

import mimetypes
import os

import fastapi
from fastapi import APIRouter
from fastapi.responses import FileResponse


static_router = APIRouter()

_WEB_DIR = ""
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}
_ALLOWED_STATIC_EXTENSIONS = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def configure_static_files(web_dir: str) -> None:
    global _WEB_DIR
    _WEB_DIR = os.path.realpath(web_dir)


def _require_web_dir() -> str:
    if not _WEB_DIR:
        raise RuntimeError("static web directory has not been configured")
    return _WEB_DIR


def _response_for(path: str, media_type: str | None = None) -> FileResponse:
    return FileResponse(path, media_type=media_type, headers=_NO_STORE_HEADERS)


def _resolve_web_file(relative_path: str, allowed_extensions: set[str] | None = None) -> tuple[str, str]:
    if not relative_path or relative_path.startswith(("/", "\\")) or "\x00" in relative_path:
        raise fastapi.HTTPException(status_code=400, detail="非法文件名")
    if ".." in relative_path.replace("\\", "/").split("/"):
        raise fastapi.HTTPException(status_code=400, detail="非法文件名")

    ext = os.path.splitext(relative_path)[1].lower()
    allowed = allowed_extensions or set(_ALLOWED_STATIC_EXTENSIONS)
    if ext not in allowed:
        raise fastapi.HTTPException(status_code=404, detail="文件不存在")

    web_dir = _require_web_dir()
    path = os.path.realpath(os.path.join(web_dir, relative_path))
    try:
        if os.path.commonpath([web_dir, path]) != web_dir:
            raise ValueError
    except ValueError:
        raise fastapi.HTTPException(status_code=403, detail="禁止访问") from None
    if not os.path.isfile(path):
        raise fastapi.HTTPException(status_code=404, detail="文件不存在")
    return path, ext


@static_router.get("/")
def serve_index():
    path, _ext = _resolve_web_file("index.html", {".html"})
    return _response_for(path, "text/html; charset=utf-8")


@static_router.get("/assets/{asset_path:path}")
def serve_asset(asset_path: str):
    path, ext = _resolve_web_file(os.path.join("assets", asset_path))
    media_type = _ALLOWED_STATIC_EXTENSIONS.get(ext) or mimetypes.guess_type(path)[0]
    return _response_for(path, media_type)


@static_router.get("/{filename}")
def serve_static_page(filename: str):
    if "/" in filename or "\\" in filename:
        raise fastapi.HTTPException(status_code=400, detail="非法文件名")
    path, _ext = _resolve_web_file(filename, {".html"})
    return _response_for(path, "text/html; charset=utf-8")
