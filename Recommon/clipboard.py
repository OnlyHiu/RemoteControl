"""剪贴板文本读写（被控端不依赖 Qt）。

远程协助最常见的一个动作是「我这边复制、你那边粘贴」。被控端是无界面的
引擎，不能依赖 Qt，所以走 Win32 剪贴板 API；装了 Qt 的一侧（控制端）也
可以直接用这个，行为一致。

只处理文本 —— 文件/图片的剪贴板同步是另一个量级的复杂度。
"""

from __future__ import annotations

import logging
import sys

from .protocol import MAX_CLIPBOARD_CHARS

log = logging.getLogger(__name__)

#: 一次最多读写多少字符。协议层是同一个上限，直接共用，免得两处各写一份
#: 然后慢慢漂开。
MAX_CHARS = MAX_CLIPBOARD_CHARS

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _win32():
    """Win32 剪贴板 API，带完整原型声明。

    必须声明 argtypes/restype：不声明时 ctypes 按 32 位传参，64 位进程里
    HGLOBAL/HANDLE 会被截断，`SetClipboardData` 就静默失败。
    """
    import ctypes
    from ctypes import wintypes

    u32 = ctypes.windll.user32
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.OpenClipboard.restype = wintypes.BOOL
    u32.CloseClipboard.argtypes = []
    u32.CloseClipboard.restype = wintypes.BOOL
    u32.EmptyClipboard.argtypes = []
    u32.EmptyClipboard.restype = wintypes.BOOL
    u32.GetClipboardData.argtypes = [wintypes.UINT]
    u32.GetClipboardData.restype = wintypes.HANDLE
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE

    k32 = ctypes.windll.kernel32
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HANDLE
    k32.GlobalLock.argtypes = [wintypes.HANDLE]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    k32.GlobalUnlock.restype = wintypes.BOOL
    k32.GlobalFree.argtypes = [wintypes.HANDLE]
    k32.GlobalFree.restype = wintypes.HANDLE
    # 读取剪贴板要知道这块内存有多大，才可能不读过界 —— 见 _win32_get。
    k32.GlobalSize.argtypes = [wintypes.HANDLE]
    k32.GlobalSize.restype = ctypes.c_size_t
    return u32, k32


def _win32_get() -> str | None:
    import ctypes

    u32, k32 = _win32()
    if not u32.OpenClipboard(None):
        return None
    try:
        handle = u32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = k32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            # 无参 wstring_at 会一路读到 NUL 为止。写这块内存的程序若没写结尾
            # NUL（别的进程崩了、被杀、格式本身怪），就会冲出这块全局内存 ——
            # 那是一次访问违例，整个进程（含被控端）当场崩掉。所以按 GlobalSize
            # 给出的实际大小卡一个上界，并且不超过剪贴板同步的长度上限。
            units = k32.GlobalSize(handle) // ctypes.sizeof(ctypes.c_wchar)
            if units < 1:
                # GlobalSize 对非 GlobalAlloc 出来的句柄返回 0。这种来源的
                # CF_UNICODETEXT 按规范不该存在，而猜错长度就是访问违例 ——
                # 宁可这一次读不到，也不要冒崩掉整个被控端的风险。
                return None
            # 带 size 的 wstring_at 读的是**恰好** size 个字符，而 GlobalSize 是
            # 整块分配的大小 —— 写入方常分配得比实际文本大。所以必须在**第一个**
            # NUL 处截断，否则读出来的是 "hi\x00<未初始化内存>"，被当成新内容
            # 推给对端写进对方剪贴板，跟"对端上次发来的文本"也永远比不上。
            text = ctypes.wstring_at(ptr, min(units, MAX_CHARS + 1))
            return text.split("\x00", 1)[0]
        finally:
            k32.GlobalUnlock(handle)
    finally:
        u32.CloseClipboard()


def _win32_set(text: str) -> bool:
    import ctypes

    u32, k32 = _win32()
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)

    handle = k32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        return False
    ptr = k32.GlobalLock(handle)
    if not ptr:
        k32.GlobalFree(handle)
        return False
    ctypes.memmove(ptr, data, size)
    k32.GlobalUnlock(handle)

    if not u32.OpenClipboard(None):
        k32.GlobalFree(handle)
        return False
    try:
        u32.EmptyClipboard()
        if not u32.SetClipboardData(CF_UNICODETEXT, handle):
            k32.GlobalFree(handle)
            return False
    finally:
        u32.CloseClipboard()
    return True


def available() -> bool:
    return sys.platform == "win32"


def get_text() -> str | None:
    """读取当前剪贴板文本；读不到返回 None。最多返回 ``MAX_CHARS`` 个字符。"""
    if not available():
        return None
    try:
        return _win32_get()
    except Exception:  # noqa: BLE001 - 剪贴板被别的程序占用等情况
        log.debug("剪贴板读取失败", exc_info=True)
        return None


def set_text(text: str) -> bool:
    """写入剪贴板文本。成功返回 True。"""
    if not available():
        return False
    text = text[:MAX_CHARS]
    try:
        return _win32_set(text)
    except Exception:  # noqa: BLE001
        log.debug("剪贴板写入失败", exc_info=True)
        return False


def try_set_text(text: str) -> bool:
    """写入失败时重试一次。

    剪贴板常被别的程序短暂占用（Office、输入法、截图工具都在抢），
    失败一次不代表失败。
    """
    for _ in range(2):
        if set_text(text):
            return True
    log.warning("无法写入剪贴板（%d 字符），可能被其他程序占用", len(text))
    return False