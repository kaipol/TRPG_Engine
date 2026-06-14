"""Root launcher for the unified Z.R.I.C TRPG engine."""

from __future__ import annotations

import multiprocessing
import os
import argparse
import threading
import time
import webbrowser

import uvicorn

from server.local_config import get_auto_open_browser, get_server_bind, resolve_base_dir, set_server_port


_log = None


def _auto_open_browser(host: str, port: int) -> None:
    time.sleep(2)
    _log.info("正在自动为您打开浏览器...")
    open_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    webbrowser.open(f"http://{open_host}:{port}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the Z.R.I.C TRPG engine.")
    parser.add_argument("--port", type=int, help="Use this port for this launch and persist it to config.json.")
    parser.add_argument("--host", default="", help="Override bind host for this launch. Defaults to local config.")
    parser.add_argument("--no-browser", action="store_true", help="Do not try to open a local browser after startup.")
    return parser.parse_args()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    args = _parse_args()
    base_dir = resolve_base_dir()
    host, port = get_server_bind(base_dir)
    if args.port is not None:
        try:
            port = set_server_port(args.port, base_dir)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if args.host:
        host = args.host
    os.environ["ZRIC_HOST"] = host
    os.environ["ZRIC_PORT"] = str(port)

    from server.main import ASSETS_DIR, BASE_DIR, WEB_DIR, _log as server_log
    _log = server_log

    _log.info("=====================================================")
    _log.info("-Z.R.I.C 零界核心- 正在启动...")
    _log.info("当前工作目录: %s", BASE_DIR)
    _log.info("前端静态目录: %s", WEB_DIR)
    _log.info("前端资源目录: %s", ASSETS_DIR)
    _log.info("监听地址: http://%s:%s", host, port)
    _log.info("=====================================================")
    _log.info("请不要关闭此窗口！关闭窗口将停止游戏引擎。")

    no_browser = (
        args.no_browser
        or os.name != "nt"
        or not get_auto_open_browser(base_dir)
    )
    if not no_browser:
        threading.Thread(target=_auto_open_browser, args=(host, port), daemon=True).start()
    uvicorn.run("server.main:app", host=host, port=port, log_level="info")
