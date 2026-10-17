"""Remote screen widget: paints decoded frames and captures local input.

Rendering is a plain ``QImage`` blit inside ``paintEvent`` - no ``QLabel``,
no ``QPixmap`` round trip, both of which cost far more than the blit itself.

Input is expressed as ratios of the remote frame (0..1), never pixels, so a
1080p agent and a 1440p viewer stay aligned without any scaling maths.
"""

from __future__ import annotations

import logging
from typing import Protocol

import numpy as np
from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QPaintEvent, QPainter, QWheelEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..Recommon import protocol as P

log = logging.getLogger(__name__)

# Held locally: never forwarded, it hands keyboard focus back to the user.
RELEASE_GRAB = (Qt.Key.Key_L, Qt.ControlModifier | Qt.ShiftModifier)

# Coalesce mouse-move bursts.  输入包只有几十字节，所以这是包数量而不是带宽
# 的问题 —— 但它同样是纯增加的延迟，所以要远小于帧间隔，否则指针会觉得脱手。
_MOVE_INTERVAL_MS = 8

# Windows 用 E0 前缀发送这些键；不显式告诉 SendInput，方向键在对端就变成小键盘
# 8/2/4/6，远端每个编辑器都会坏掉。
_EXTENDED_KEYS = {
    Qt.Key.Key_Up,
    Qt.Key.Key_Down,
    Qt.Key.Key_Left,
    Qt.Key.Key_Right,
    Qt.Key.Key_Insert,
    Qt.Key.Key_Delete,
    Qt.Key.Key_Home,
    Qt.Key.Key_End,
    Qt.Key.Key_PageUp,
    Qt.Key.Key_PageDown,
    # 不含 Key_NumLock / Key_Clear：它们的扫描码是 0x45 / 0x4C，没有 E0 前缀。
    # 放进本表的话，注入的 E0 45 / E0 4C 是死键 —— 远端 NumLock 永远切不动，
    # 小键盘 5（NumLock 关）也按不出来。
    Qt.Key.Key_division,
    Qt.Key.Key_Enter,  # numpad Enter
    Qt.Key.Key_Super_L,
    Qt.Key.Key_Super_R,
    Qt.Key.Key_Menu,
}


class RemoteSink(Protocol):
    def send_mouse(self, action: int, button: int, x_ratio: float, y_ratio: float) -> None: ...
    def send_wheel(self, delta: int, horizontal: bool = False) -> None: ...
    def send_key(self, action: int, vk: int, scan: int, ext: bool = False) -> None: ...


def to_qimage(rgb: np.ndarray) -> QImage:
    """numpy RGB -> QImage. Runs on the reader thread, never the GUI thread."""
    arr = np.ascontiguousarray(rgb)
    h, w = arr.shape[:2]
    img = QImage(arr.data, w, h, arr.strides[0], QImage.Format.Format_RGB888)
    # .copy() detaches from the numpy buffer, which is about to be recycled
    return img.copy()


_BUTTON_MAP = {
    Qt.MouseButton.LeftButton: P.MouseBtn.LEFT,
    Qt.MouseButton.RightButton: P.MouseBtn.RIGHT,
    Qt.MouseButton.MiddleButton: P.MouseBtn.MIDDLE,
    Qt.MouseButton.XButton1: P.MouseBtn.X1,
    Qt.MouseButton.XButton2: P.MouseBtn.X2,
}


#: Qt 特殊键（key() >= 0x100）→ Windows 虚拟键码。字符键走 VkKeyScanW，
#: 这张表只管 F1~F24、方向、导航这类"没有字符"的键。值都是 Win32 VK_* 常量。
_QT_SPECIAL_VK: dict[int, int] = {
    int(Qt.Key.Key_Escape): 0x1B,
    int(Qt.Key.Key_Tab): 0x09,
    int(Qt.Key.Key_Backtab): 0x09,
    int(Qt.Key.Key_Backspace): 0x08,
    int(Qt.Key.Key_Return): 0x0D,
    int(Qt.Key.Key_Enter): 0x0D,
    int(Qt.Key.Key_Insert): 0x2D,
    int(Qt.Key.Key_Delete): 0x2E,
    int(Qt.Key.Key_Pause): 0x13,
    int(Qt.Key.Key_Print): 0x2C,
    int(Qt.Key.Key_SysReq): 0x2C,
    int(Qt.Key.Key_Clear): 0x0C,
    int(Qt.Key.Key_Home): 0x24,
    int(Qt.Key.Key_End): 0x23,
    int(Qt.Key.Key_Left): 0x25,
    int(Qt.Key.Key_Up): 0x26,
    int(Qt.Key.Key_Right): 0x27,
    int(Qt.Key.Key_Down): 0x28,
    int(Qt.Key.Key_PageUp): 0x21,
    int(Qt.Key.Key_PageDown): 0x22,
    int(Qt.Key.Key_Shift): 0x10,
    int(Qt.Key.Key_Control): 0x11,
    int(Qt.Key.Key_Meta): 0x5B,
    int(Qt.Key.Key_Alt): 0x12,
    int(Qt.Key.Key_AltGr): 0xA5,
    int(Qt.Key.Key_CapsLock): 0x14,
    int(Qt.Key.Key_NumLock): 0x90,
    int(Qt.Key.Key_ScrollLock): 0x91,
    int(Qt.Key.Key_Super_L): 0x5B,
    int(Qt.Key.Key_Super_R): 0x5C,
    int(Qt.Key.Key_Menu): 0x5D,
    int(Qt.Key.Key_Sleep): 0x5F,
}
# F1..F24 在 Qt 里连号、在 Win32 里也连号（VK_F1 = 0x70）
for _i in range(24):
    _QT_SPECIAL_VK[int(Qt.Key.Key_F1) + _i] = 0x70 + _i


def _qt_key_to_vk(qt_key: int) -> int:
    """Qt key code -> Windows virtual key code.

    ``nativeVirtualKey()`` 对任何不是来自平台层的事件都是空的（合成事件、部分
    输入法、某些远程桌面前端）。丢掉这类按键是远程控制工具唯一不能有的失败，
    所以回退到 Qt 自己的键码。

    字符键问当前键盘布局要真实的 VK（VkKeyScanW：'a' -> 'A'、'+' -> OEM_PLUS；
    字母数字的 Qt 键码本身就是 VK，但标点不是），特殊键查 ``_QT_SPECIAL_VK``。
    注意**不能**用 MapVirtualKeyW(qt_key, 0) —— 那是 VK->扫描码的翻译，返回的
    是扫描码：'A' 会得到 0x1E，注入出去是另一个键。
    """
    if not qt_key:
        return 0
    if qt_key >= 0x100:
        return _QT_SPECIAL_VK.get(int(qt_key), 0)
    try:
        import ctypes

        code = ctypes.windll.user32.VkKeyScanW(ctypes.c_wchar(chr(qt_key))) & 0xFFFF
        if code != 0xFFFF and code & 0xFF:
            return code & 0xFF
    except Exception:  # noqa: BLE001 - best effort
        pass
    return int(qt_key)


class VideoWidget(QWidget):
    status = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._placeholder = "未连接"
        self._sink: RemoteSink | None = None
        self._view_rect = QRect()
        self._pressed: set[int] = set()
        #: Currently held keys as ``vk -> extended``.  Needed because losing
        #: focus means we never see the matching keyReleaseEvent - see
        #: _release_held_input().
        self._keys_down: dict[int, bool] = {}
        self._grab_keyboard = False
        self._pending_move: tuple[float, float] | None = None

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 200)
        self.setStyleSheet("background-color: #101014;")

        self._move_timer = QTimer(self)
        self._move_timer.setInterval(_MOVE_INTERVAL_MS)
        self._move_timer.timeout.connect(self._flush_move)

    # -- api ---------------------------------------------------------------
    def set_sink(self, sink: RemoteSink | None) -> None:
        if sink is None:
            # 对端已经走了：我们按住的任何东西都不可能再被释放。忘掉它，
            # 好让下一次抓取从干净状态开始。
            self._keys_down.clear()
            self._pressed.clear()
            self._move_timer.stop()
            self._pending_move = None
        self._sink = sink

    def clear(self) -> None:
        self._image = None
        self._placeholder = "未连接"
        self._grab_keyboard = False
        self._release_held_input()
        self._move_timer.stop()
        self._pending_move = None
        self.update()

    def set_image(self, image: QImage) -> None:
        """Show a frame. Call from the GUI thread (via RemoteWindow._drain)."""
        self._image = image
        self._placeholder = ""
        self.update()

    def set_placeholder(self, text: str) -> None:
        """Show a message instead of the stream (errors, waiting states)."""
        self._image = None
        self._placeholder = text
        self.update()

    # -- painting ----------------------------------------------------------
    def _compute_view_rect(self) -> QRect:
        if self._image is None or self._image.width() == 0:
            return QRect()
        iw, ih = self._image.width(), self._image.height()
        ww, wh = self.width(), self.height()
        scale = min(ww / iw, wh / ih)
        dw, dh = int(iw * scale), int(ih * scale)
        return QRect((ww - dw) // 2, (wh - dh) // 2, dw, dh)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101014"))
        if self._image is None:
            painter.setPen(QColor("#8a8a94"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                self._placeholder or "未连接",
            )
            painter.end()
            return
        self._view_rect = self._compute_view_rect()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(self._view_rect, self._image)
        if not self._grab_keyboard:
            painter.setPen(QColor(255, 255, 255, 140))
            painter.drawText(
                self.rect().adjusted(8, 8, -8, -8),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                "点击画面以捕获键鼠 · Ctrl+Shift+L 释放",
            )
        painter.end()

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        super().resizeEvent(event)
        self.update()

    def _on_frame(self, image: QImage) -> None:
        self.set_image(image)

    # -- input mapping -----------------------------------------------------
    def _to_ratio(self, pos) -> tuple[float, float] | None:  # noqa: ANN001
        rect = self._view_rect
        if rect.isEmpty() or self._image is None:
            return None
        x = (pos.x() - rect.x()) / max(1, rect.width())
        y = (pos.y() - rect.y()) / max(1, rect.height())
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            return None
        return x, y

    # -- input events ------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._grab_keyboard = True
        self.status.emit("已捕获键鼠，可直接输入（Ctrl+Shift+L 释放）")
        btn = _BUTTON_MAP.get(event.button())
        if btn is None or self._sink is None:
            return
        ratio = self._to_ratio(event.position())
        if ratio is None:
            return
        self._pressed.add(btn)
        self._sink.send_mouse(P.MouseAction.DOWN, int(btn), *ratio)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        btn = _BUTTON_MAP.get(event.button())
        if btn is None or self._sink is None:
            return
        if btn not in self._pressed:
            # 没发过 DOWN 就不发 UP：press 在画面外时不产生 DOWN（坐标无法映射），
            # 此时的 release 若照发就是一个孤立的按钮抬起 —— 对端某些应用会把它
            # 当成一次点击的后半段（右键菜单）。失焦补发 _release_held_input
            # 已经把该发的 UP 发过了。
            return
        ratio = self._to_ratio(event.position()) or (0.5, 0.5)
        self._pressed.discard(btn)
        self._sink.send_mouse(P.MouseAction.UP, int(btn), *ratio)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._sink is None:
            return
        ratio = self._to_ratio(event.position())
        if ratio is None:
            return
        # 合并：一次快速划过产生的 move 事件远多于线路需要的，全发出去就是输入
        # 延迟的来源。
        self._pending_move = ratio
        if not self._move_timer.isActive():
            self._move_timer.start()

    def _flush_move(self) -> None:
        ratio, self._pending_move = self._pending_move, None
        if ratio is None:
            self._move_timer.stop()
            return
        if self._sink is not None:
            self._sink.send_mouse(P.MouseAction.MOVE, 0, *ratio)

    def _release_held_input(self) -> None:
        """Send the "up" for everything still held on the far side.

        失焦（或松开键盘抓取）意味着我们再也不会收到对应的抬起事件 —— Qt 只在
        widget 有焦点时才送这些。不补发的话**对端**那台机器上的键就一直按着：
        Ctrl 按住会让每次击键都变成 Ctrl+xxx，Alt 按住会留着菜单栏并废掉快捷键，
        鼠标按钮按住则是一个拖拽或右键菜单挂在那里。

        被控端也有兜底 reset()，但它只在整个会话结束时跑 —— 这里管的是逐次失焦。
        """
        keys, buttons = self._keys_down, self._pressed
        self._keys_down, self._pressed = {}, set()
        sink = self._sink
        if sink is None:
            return
        # scan=0 让注入器回退到虚拟键码，这对一次抬起正合适：我们已经不确定
        # 扫描码了。
        for vk, ext in keys.items():
            try:
                sink.send_key(P.KeyAction.UP, int(vk), 0, ext=bool(ext))
            except Exception:  # noqa: BLE001 - never raise out of a Qt handler
                log.debug("补发按键抬起失败 vk=%s", vk, exc_info=True)
        for btn in buttons:
            try:
                sink.send_mouse(P.MouseAction.UP, int(btn), 0.5, 0.5)
            except Exception:  # noqa: BLE001
                log.debug("补发鼠标抬起失败 btn=%s", btn, exc_info=True)
        if keys or buttons:
            log.info("失焦，已补发 %d 个按键 / %d 个鼠标键的抬起", len(keys), len(buttons))

    def wheelEvent(self, event: QWheelEvent) -> None:
        if self._sink is None:
            return
        delta = event.angleDelta()
        if delta.y():
            self._sink.send_wheel(delta.y(), False)
        elif delta.x():
            self._sink.send_wheel(delta.x(), True)

    def _on_release_grab(self) -> None:
        """Give the keyboard back to the user (Ctrl+Shift+L), releasing what is held."""
        self._grab_keyboard = False
        # 松开抓取时不能漏掉还按着的键 —— 之后不会再收到它们的抬起事件。
        self._release_held_input()
        self.status.emit("已释放键鼠捕获")
        self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # 日志放在最前面：放在 isAutoRepeat 之后就看不到"按键被当成自动重复丢弃
        # 了"这类情况。
        log.info(
            "键盘按下 key=%s vk=%s scan=%s 自动重复=%s 捕获=%s sink=%s",
            event.key(),
            event.nativeVirtualKey(),
            event.nativeScanCode(),
            event.isAutoRepeat(),
            self._grab_keyboard,
            self._sink is not None,
        )
        try:
            self._handle_key_press(event)
        except Exception:  # noqa: BLE001
            # Qt 会把槽里的异常吞掉、只留一行 stderr —— 必须自己记下来。
            log.exception("keyPressEvent 处理异常")

    def _handle_key_press(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            return  # the injected key-down already produces native repeats
        key, mods = event.key(), event.modifiers()
        if (key, mods & (Qt.ControlModifier | Qt.ShiftModifier | Qt.AltModifier)) == RELEASE_GRAB:
            self._on_release_grab()
            event.accept()
            return
        if not self._grab_keyboard or self._sink is None:
            return
        vk = int(event.nativeVirtualKey() or 0)
        scan = int(event.nativeScanCode() or 0)
        if vk == 0:
            vk = _qt_key_to_vk(key)
        if vk == 0 and scan == 0:
            # 没有键码的事件没法在线上表示（会变成 VK_NULL）。宁可不发，
            # 也不给对面注入一个无效按键。
            return
        ext = self._is_extended(event)
        self._sink.send_key(
            P.KeyAction.DOWN,
            vk,
            scan,
            ext=ext,
        )
        if vk:
            self._keys_down[vk] = ext
        event.accept()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        log.info(
            "键盘抬起 key=%s vk=%s scan=%s 自动重复=%s 捕获=%s",
            event.key(),
            event.nativeVirtualKey(),
            event.nativeScanCode(),
            event.isAutoRepeat(),
            self._grab_keyboard,
        )
        if event.isAutoRepeat() or not self._grab_keyboard or self._sink is None:
            return
        vk = int(event.nativeVirtualKey() or 0)
        scan = int(event.nativeScanCode() or 0)
        if vk == 0:
            vk = _qt_key_to_vk(event.key())
        if vk == 0 and scan == 0:
            return
        self._keys_down.pop(vk, None)
        self._sink.send_key(
            P.KeyAction.UP,
            vk,
            scan,
            ext=self._is_extended(event),
        )
        event.accept()

    @staticmethod
    def _is_extended(event: QKeyEvent) -> bool:
        if event.key() in _EXTENDED_KEYS:
            return True
        # 右手 Ctrl / Alt 带 E0 前缀却与左手共用同一个 Qt 键码，只有扫描码能分辨
        # —— MapVirtualKey 返回裸码，低位带 E0。
        scan = int(event.nativeScanCode() or 0)
        # 不能用 KeypadModifier 兜底：小键盘的数字/运算键全带 KeypadModifier，
        # 但它们是**非**扩展键（扫描码 0x47~0x53，无 E0 前缀）。按它置 ext 的话，
        # 被控端会把小键盘 7 注成 E0 47 = Home、1 = End、2 = ↓、0 = Ins……
        # 真正带 E0 的小键盘键只有 Enter（E0 1C）和除号（E0 35），以及 NumLock
        # 关掉时小键盘变出的方向/导航键 —— 都已在 _EXTENDED_KEYS 里。
        return bool(scan & 0xE000)

    def focusOutEvent(self, event) -> None:  # noqa: ANN001 - Qt signature
        self._grab_keyboard = False
        # Qt 之后不会再把这个 widget 的 keyReleaseEvent 送给我们，所以还按着的
        # 键必须在这里补发抬起 —— 否则对面的修饰键会一直卡在按下状态。
        self._release_held_input()
        super().focusOutEvent(event)
        self.update()
