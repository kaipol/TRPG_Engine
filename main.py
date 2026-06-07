"""Root launcher for the unified Z.R.I.C TRPG engine."""

from __future__ import annotations

import multiprocessing
import threading
import time
import webbrowser

import uvicorn

from server.main import app, BASE_DIR, _log


def _auto_open_browser() -> None:
    time.sleep(2)
    _log.info("正在自动为您打开浏览器...")
    webbrowser.open("http://127.0.0.1:8000")


if __name__ == "__main__":
    multiprocessing.freeze_support()

    _log.info("=====================================================")
    _log.info("-Z.R.I.C 零界核心- 正在启动...")
    _log.info("当前工作目录: %s", BASE_DIR)
    _log.info("=====================================================")
    _log.info("请不要关闭此窗口！关闭窗口将停止游戏引擎。")

    threading.Thread(target=_auto_open_browser, daemon=True).start()
    uvicorn.run("server.main:app", host="127.0.0.1", port=8000, log_level="info")
