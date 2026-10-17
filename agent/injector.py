"""Mouse / keyboard injection on Windows via ``user32.SendInput``.

``SendInput`` is used rather than ``SetCursorPos`` + ``keybd_event`` because it
is the only API that produces genuinely *injected* events (needed for games,
UAC-adjacent windows and anything doing input sniffing), and it carries the
scan code so non-US layouts keep working.

Caveat (documented in README): UIPI blocks injection into windows owned by a
higher-integrity process.  To drive an elevated window the agent itself must
run elevated.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from ..Recommon.protocol import KeyAction, MouseAction, MouseBtn

log = logging.getLogger(__name__)

if sys.platform != "win32":  # pragma: no cover - Windows only project
    raise ImportError("agent.injector requires Windows")

ULONG_PTR = ctypes.c_size_t
user32 = ctypes.windll.user32

# --- constants ------------------------------------------------------------
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

#: 走虚拟键码注入的小键盘 VK：VK_NUMPAD0~9、VK_MULTIPLY/ADD/SEPARATOR/
#: SUBTRACT/DECIMAL/DIVIDE（0x60~0x6F）和 VK_CLEAR（NumLock 关的小键盘 5）。
#: 原因见 InputInjector.key()。
_NUMPAD_VKS = frozenset(range(0x60, 0x70)) | {0x0C}

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

_BTN_DOWN = {
    MouseBtn.LEFT: MOUSEEVENTF_LEFTDOWN,
    MouseBtn.RIGHT: MOUSEEVENTF_RIGHTDOWN,
    MouseBtn.MIDDLE: MOUSEEVENTF_MIDDLEDOWN,
    MouseBtn.X1: MOUSEEVENTF_XDOWN,
    MouseBtn.X2: MOUSEEVENTF_XDOWN,
}
_BTN_UP = {
    MouseBtn.LEFT: MOUSEEVENTF_LEFTUP,
    MouseBtn.RIGHT: MOUSEEVENTF_RIGHTUP,
    MouseBtn.MIDDLE: MOUSEEVENTF_MIDDLEUP,
    MouseBtn.X1: MOUSEEVENTF_XUP,
    MouseBtn.X2: MOUSEEVENTF_XUP,
}
_BTN_DATA = {MouseBtn.X1: XBUTTON1, MouseBtn.X2: XBUTTON2}


# --- structs --------------------------------------------------------------
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def _send(*inputs: INPUT) -> None:
    if not inputs:
        return
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        log.debug("SendInput 实际注入 %d/%d 个事件", sent, len(inputs))


def _mouse(flags: int, x: int = 0, y: int = 0, data: int = 0) -> INPUT:
    return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(x, y, data, flags, 0, 0))


def _key(vk: int, scan: int, flags: int) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, scan, flags, 0, 0))


class InputInjector:
    """Turns normalised viewer events back into local input.

    Viewer coordinates are ratios of the *captured region*, so injection has to
    map them back onto that same rect rather than the whole desktop - otherwise
    clicks drift on multi-monitor hosts.
    """

    def __init__(self, region: tuple[int, int, int, int] | None = None) -> None:
        # 记账：哪些键/按钮被按住还没释放。reset() 只释放这些 —— 无差别发
        # "抬起"会把孤立的 MOUSEEVENTF_RIGHTUP 落到桌面上，某些应用会把它
        # 当成一次右键单击的后半段。
        self._held_buttons: set[MouseBtn] = set()
        self._held_keys: set[int] = set()
        self.set_region(region)

    # -- geometry ----------------------------------------------------------
    def set_region(self, region: tuple[int, int, int, int] | None) -> None:
        if region is None:
            region = (
                user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
                user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
                max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
                max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
            )
        # 打包成一个 tuple 一次性赋值：发送线程每帧都会调这里，而会话线程可能
        # 正在读这四个属性。分成四次赋值的话，切显示器那一瞬间会读到"新 x +
        # 旧 w"这种撕裂组合，那一次点击就落在错误位置。
        self._region = (
            region[0],
            region[1],
            max(1, region[2]),
            max(1, region[3]),
        )

    def _to_absolute(self, x_ratio: float, y_ratio: float) -> tuple[int, int]:
        x_ratio = min(1.0, max(0.0, x_ratio))
        y_ratio = min(1.0, max(0.0, y_ratio))
        rx, ry, rw, rh = self._region
        px = rx + round(x_ratio * (rw - 1))
        py = ry + round(y_ratio * (rh - 1))
        # MOUSEEVENTF_ABSOLUTE 要的是虚拟桌面上的 0..65535。分母用 max(2, ...)：
        # 用 max(1, ...) 时 v 恰好等于 1，后面除以 (v - 1) 就是 ZeroDivisionError。
        vw = max(2, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        vh = max(2, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        nx = round((px - vx) * 65535 / (vw - 1))
        ny = round((py - vy) * 65535 / (vh - 1))
        return nx, ny

    # -- events ------------------------------------------------------------
    def mouse(self, action: int, button: int, x_ratio: float, y_ratio: float) -> None:
        # action/button 是**线上来的**裸整数，不是本地枚举：越界的 button 会让
        # MouseBtn(button) 抛 ValueError，怪异的 action 会被当成"抬起"。都在门口
        # 挡掉，别让一个坏包变成一次意外注入。
        try:
            btn = MouseBtn(button)
        except ValueError:
            log.debug("忽略未知鼠标按钮 %s", button)
            return
        if action not in (MouseAction.MOVE, MouseAction.DOWN, MouseAction.UP):
            log.debug("忽略未知鼠标动作 %s", action)
            return
        nx, ny = self._to_absolute(x_ratio, y_ratio)
        move = _mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)
        if action == MouseAction.MOVE or btn == MouseBtn.NONE:
            _send(move)
            return
        data = _BTN_DATA.get(btn, 0)
        if action == MouseAction.DOWN:
            self._held_buttons.add(btn)
            extra = _mouse(_BTN_DOWN[btn], 0, 0, data)
        else:
            self._held_buttons.discard(btn)
            extra = _mouse(_BTN_UP[btn], 0, 0, data)
        _send(move, extra)

    def wheel(self, delta: int, horizontal: bool = False) -> None:
        flags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
        _send(_mouse(flags, 0, 0, delta))

    def key(self, action: int, vk: int, scan: int, ext: bool = False) -> None:
        # 同 mouse()：action 来自线上，别把怪异的值当成"按下"。
        if action not in (KeyAction.DOWN, KeyAction.UP):
            log.debug("忽略未知按键动作 %s", action)
            return
        # 小键盘键（VK_NUMPAD0~9、VK_MULTIPLY~VK_DIVIDE、VK_CLEAR）不按扫描码
        # 注入：扫描码的含义由**被控端** NumLock 决定（同一颗键 0x50，NumLock
        # 开是"2"、关是"↓"）。而两端 NumLock 的偏差修不回来 —— viewer 侧按一下
        # NumLock，本机和被控端一起翻转，偏差永远保持。改用虚拟键码后
        # ToUnicode 对 VK_NUMPAD* / VK_DECIMAL 总是出数字，与 NumLock 无关。
        if vk in _NUMPAD_VKS:
            scan, ext = 0, False
        # scan 为 0 时不能用 KEYEVENTF_SCANCODE —— 那等于发出一个无效按键。
        # 这种情况回退到虚拟键码。
        flags = KEYEVENTF_SCANCODE if scan else 0
        if ext:
            flags |= KEYEVENTF_EXTENDEDKEY
        if action == KeyAction.UP:
            flags |= KEYEVENTF_KEYUP
            self._held_keys.discard(vk)
        else:
            self._held_keys.add(vk)
        _send(_key(vk, scan, flags))

    def reset(self) -> None:
        """Release what a dead session may have left held - and only that.

        会话断在半途时，远端按住的键/按钮必须补抬起（否则拖拽卡住、修饰键卡住）。
        但**只能**补记账中真正按住的：无差别给所有按钮发"抬起"，孤立的
        MOUSEEVENTF_RIGHTUP 落到某些应用上会被当成一次右键单击的后半段。

        键盘的修饰键仍无差别释放：孤立的按键抬起无害，而漏记账的修饰键会让
        接下来每一次输入都变成组合键。
        """
        VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN = 0x11, 0x12, 0x10, 0x5B, 0x5C
        for vk in (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN):
            _send(_key(vk, 0, KEYEVENTF_KEYUP))
        for vk in sorted(self._held_keys - {VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN}):
            _send(_key(vk, 0, KEYEVENTF_KEYUP))
        self._held_keys.clear()
        # 鼠标只释放真正按着的（见 docstring：孤立抬起会弹右键菜单）。
        for btn in list(self._held_buttons):
            _send(_mouse(_BTN_UP[btn], 0, 0, _BTN_DATA.get(btn, 0)))
        self._held_buttons.clear()
