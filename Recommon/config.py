"""Small JSON-backed config + per-user data directory helpers."""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP_NAME = "lan-remote"


#: 进程内 timeBeginPeriod 的引用计数。见 enable_high_resolution_timers。
_TIMER_LOCK = threading.Lock()
_TIMER_USERS = 0


def enable_high_resolution_timers() -> None:
    """Ask Windows for 1 ms timer resolution.

    The OS default is 15.6 ms, so a render pump scheduled at 33 ms gets quantised
    to ~46 ms (~21 fps) and ``time.sleep`` pacing drifts the same way.

    调用是**成对**的：``timeBeginPeriod(1)`` 一直有效到有人 ``timeEndPeriod(1)``，
    只调不还等于整个进程生命周期都钉在 1 ms，系统因此不会进入低功耗状态。
    嵌套调用安全：谁申请谁归还。
    """
    global _TIMER_USERS
    if sys.platform != "win32":
        return
    try:
        import ctypes

        with _TIMER_LOCK:
            ctypes.windll.winmm.timeBeginPeriod(1)
            _TIMER_USERS += 1
    except Exception:  # noqa: BLE001 - not fatal, just less smooth
        log.debug("无法设置高精度定时器", exc_info=True)


def disable_high_resolution_timers() -> None:
    """Give the 1 ms timer resolution back (see enable_*).

    Calls are counted, so the last user leaving is what actually calls
    ``timeEndPeriod`` - a host that also wants 1 ms pacing keeps it.
    """
    global _TIMER_USERS
    if sys.platform != "win32":
        return
    with _TIMER_LOCK:
        if _TIMER_USERS <= 0:
            return
        _TIMER_USERS -= 1
        if _TIMER_USERS:
            return
        try:
            import ctypes

            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:  # noqa: BLE001
            log.debug("无法恢复定时器分辨率", exc_info=True)
        log.info("已归还 1ms 定时器分辨率")


def data_dir(role: str) -> Path:
    """每个角色（agent / viewer / security）的存储目录：配置、密钥、证书。

    跟随 ``settings.data_root()``，与验证码、连接历史放在一起，路径可在包根
    ``__init__.py`` 顶部统一改。
    """
    from . import settings

    root = settings.data_root() / role
    root.mkdir(parents=True, exist_ok=True)
    return root


class Config:
    """A dict that persists itself to ``<data_dir>/config.json``."""

    def __init__(self, role: str, defaults: dict[str, Any] | None = None) -> None:
        self.path = data_dir(role) / "config.json"
        self._lock = threading.Lock()
        self._data: dict[str, Any] = dict(defaults or {})
        if self.path.exists():
            try:
                self._data.update(json.loads(self.path.read_text("utf-8")))
            except (OSError, ValueError):
                pass

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def __setitem__(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._save_unlocked()

    def update(self, mapping: dict[str, Any]) -> None:
        with self._lock:
            self._data.update(mapping)
            self._save_unlocked()

    def pop(self, key: str, default: Any = None) -> Any:
        """删除并返回键值，立即落盘（键不存在时返回 *default*）。"""
        with self._lock:
            value = self._data.pop(key, default)
            self._save_unlocked()
            return value

    def _save_unlocked(self) -> None:
        # 临时文件名必须唯一：固定的 config.tmp 在两个线程同时写时会互相交错
        # 字节，os.replace 把损坏的 JSON 推上盘。同一份 Config 会被会话线程
        # （写 monitor）和 UI 线程（写 allow_control）同时用。
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(self._data, ensure_ascii=False, indent=2))
            os.replace(tmp, self.path)
        except OSError:
            # Windows 上目标文件被别的句柄打开时 os.replace 会抛 sharing
            # violation。这里不能让异常从 __setitem__ 冒到调用方。
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()


def local_ips() -> list[str]:
    """All IPv4 addresses of this machine on active interfaces."""
    found: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    # A UDP trick picks up the primary LAN address even when DNS is unhelpful.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            found.add(s.getsockname()[0])
    except OSError:
        pass
    found.discard("0.0.0.0")
    return sorted(found)


#: 本线程已选定的 COM 公寓模型；见 init_com()。
_COM = threading.local()


#: COINIT_APARTMENTTHREADED = 2 - comtypes（dxcam）的模型。
COINIT_STA = 2
#: COINIT_MULTITHREADED = 0 - Media Foundation（h264_mf）的模型。
COINIT_MTA = 0


def _pin_com(model: int) -> None:
    """给**当前线程**选一次 COM 公寓模型，此后不再改。

    公寓模型是每线程的，而 dxcam（comtypes）要 STA、Media Foundation
    （h264_mf）要 MTA：同一个线程上两者互斥（实测 STA 线程上 h264_mf 的
    avcodec_open2 必然 EINVAL，反之 dxcam create 报 RPC_E_CHANGED_MODE）。
    跨线程互不影响，所以规则是「采集侧线程用 init_com，编码侧线程用
    init_com_mta」，而不是整个进程钉死一个模型。

    每线程只做一次：反复调 CoInitializeEx 会多一份永不归还的 COM 引用
    （我们从不 CoUninitialize），中途换模型则直接 RPC_E_CHANGED_MODE。
    """
    if sys.platform != "win32":
        return
    if getattr(_COM, "model", None) is not None:
        return
    _COM.model = model
    try:
        import ctypes

        # S_OK/S_FALSE mean already initialised.
        hr = ctypes.windll.ole32.CoInitializeEx(None, model) & 0xFFFFFFFF
        if hr not in (0, 1):
            log.debug("CoInitializeEx(%d) 返回 %#x", model, hr)
    except Exception:  # noqa: BLE001 - never fatal, the backends cope
        log.debug("COM 初始化失败", exc_info=True)


def init_com() -> None:
    """采集侧：把当前线程钉在 STA（comtypes/dxcam 要的模型）。

    采集创建必须与编码探测**不在同一个线程**，否则两者在该线程上抢公寓模型。
    """
    _pin_com(COINIT_STA)


def init_com_mta() -> None:
    """编码侧：把当前线程钉在 MTA（Media Foundation/h264_mf 要的模型）。

    编码上下文在哪条线程上 open，就要求哪条线程是 MTA（或干脆未初始化，
    让 MF 自己选 MTA）：探测线程、会话的发送线程。
    """
    _pin_com(COINIT_MTA)


def log_dir() -> Path:
    """日志目录，跟随主程序位置（见 ``Recommon.settings``）。"""
    from . import settings

    return settings.log_dir()


def fix_stdio() -> None:
    """无控制台构建下把 stdout/stderr 接到文件。见 ``Recommon.settings``。"""
    from . import settings

    settings.fix_stdio()
