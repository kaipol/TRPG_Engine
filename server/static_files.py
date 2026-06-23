"""Static web file serving for the bundled Z.R.I.C frontend."""

from __future__ import annotations

import os

import fastapi
from fastapi import APIRouter
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles


static_router = APIRouter()

_WEB_DIR = ""
_ASSETS_DIR = ""
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
_REQUIRED_FRONTEND_FILES = (
    "index.html",
    "multiplayer.html",
    "player.html",
    "phone.html",
)
_REQUIRED_ASSET_FILES = (
    "index.css",
    "index-app.js",
    "multiplayer.css",
    "multiplayer-app.js",
    "tailwind-lite.css",
)
class NoStoreStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(_NO_STORE_HEADERS)
        return response


def configure_static_files(web_dir: str, assets_dir: str) -> None:
    global _WEB_DIR, _ASSETS_DIR
    _WEB_DIR = os.path.realpath(web_dir)
    _ASSETS_DIR = os.path.realpath(assets_dir)
    _validate_frontend_files()


def mount_asset_files(app: fastapi.FastAPI, web_dir: str, assets_dir: str) -> None:
    configure_static_files(web_dir, assets_dir)
    app.mount(
        "/assets",
        NoStoreStaticFiles(directory=_ASSETS_DIR, check_dir=True),
        name="assets",
    )


def _require_web_dir() -> str:
    if not _WEB_DIR:
        raise RuntimeError("static web directory has not been configured")
    return _WEB_DIR


def _require_assets_dir() -> str:
    if not _ASSETS_DIR:
        raise RuntimeError("static assets directory has not been configured")
    return _ASSETS_DIR


def _validate_frontend_files() -> None:
    web_dir = _require_web_dir()
    assets_dir = _require_assets_dir()
    missing_web = [
        rel for rel in _REQUIRED_FRONTEND_FILES
        if not os.path.isfile(os.path.join(web_dir, rel))
    ]
    missing_assets = [
        os.path.join("assets", rel).replace("\\", "/")
        for rel in _REQUIRED_ASSET_FILES
        if not os.path.isfile(os.path.join(assets_dir, rel))
    ]
    missing = missing_web + missing_assets
    if missing:
        missing_text = ", ".join(missing)
        raise RuntimeError(
            f"前端静态资源缺失：{missing_text}；当前 WEB_DIR={web_dir}，ASSETS_DIR={assets_dir}。"
            "请确认上传服务器时包含项目根目录 assets/。"
        )


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


def static_assets_status() -> dict:
    web_dir = _require_web_dir()
    assets_dir = _require_assets_dir()
    files = {}
    for rel in _REQUIRED_FRONTEND_FILES:
        path = os.path.join(web_dir, rel)
        display_rel = rel.replace("\\", "/")
        files[f"web/{display_rel}"] = {
            "exists": os.path.isfile(path),
            "size_bytes": os.path.getsize(path) if os.path.isfile(path) else 0,
        }
    for rel in _REQUIRED_ASSET_FILES:
        path = os.path.join(assets_dir, rel)
        files[f"assets/{rel}"] = {
            "exists": os.path.isfile(path),
            "size_bytes": os.path.getsize(path) if os.path.isfile(path) else 0,
        }
    return {
        "status": "success",
        "web_dir": web_dir,
        "web_dir_exists": os.path.isdir(web_dir),
        "assets_dir": assets_dir,
        "assets_dir_exists": os.path.isdir(assets_dir),
        "files": files,
    }


@static_router.get("/")
def serve_index():
    path, _ext = _resolve_web_file("index.html", {".html"})
    return _response_for(path, "text/html; charset=utf-8")


@static_router.get("/favicon.ico", include_in_schema=False)
def serve_favicon():
    web_dir = _require_web_dir()
    path = os.path.realpath(os.path.join(web_dir, "favicon.ico"))
    if os.path.isfile(path):
        return _response_for(path, "image/x-icon")
    return Response(status_code=204)


@static_router.get("/api/debug/static-assets")
def debug_static_assets():
    return static_assets_status()


@static_router.get("/{filename}")
def serve_static_page(filename: str):
    if "/" in filename or "\\" in filename:
        raise fastapi.HTTPException(status_code=400, detail="非法文件名")
    path, _ext = _resolve_web_file(filename, {".html"})
    return _response_for(path, "text/html; charset=utf-8")
