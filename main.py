"""Root launcher for the unified Z.R.I.C TRPG engine."""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from server.main import app, BASE_DIR, _log


def _remove_launcher_pid_file(pid_file: str | None) -> None:
    if not pid_file:
        return

    path = Path(pid_file)
    for _ in range(5):
        try:
            if path.exists():
                path.unlink()
            return
        except OSError:
            time.sleep(0.2)


def _wait_for_windows_process_exit(pid: int) -> None:
    import ctypes

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    infinite_wait = 0xFFFFFFFF

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return

    try:
        kernel32.WaitForSingleObject(handle, infinite_wait)
    finally:
        kernel32.CloseHandle(handle)


def _wait_for_posix_process_exit(pid: int) -> None:
    while True:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(1)


def _watch_launcher_parent() -> None:
    parent_pid_text = os.environ.get("TRPG_LAUNCHER_PARENT_PID")
    if not parent_pid_text:
        return

    try:
        parent_pid = int(parent_pid_text)
    except ValueError:
        return

    pid_file = os.environ.get("TRPG_LAUNCHER_PID_FILE")

    def monitor() -> None:
        if os.name == "nt":
            _wait_for_windows_process_exit(parent_pid)
        else:
            _wait_for_posix_process_exit(parent_pid)

        _log.info("启动器进程已退出，正在关闭游戏引擎以释放端口。")
        _remove_launcher_pid_file(pid_file)
        os._exit(0)

    threading.Thread(target=monitor, daemon=True).start()


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

    _watch_launcher_parent()
    threading.Thread(target=_auto_open_browser, daemon=True).start()
    uvicorn.run("server.main:app", host="127.0.0.1", port=8000, log_level="info")
