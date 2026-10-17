"""Screen capture backends.

Both backends hand back the same thing: an ``(H, W, 3) uint8`` RGB numpy
array, straight from the OS - no colour conversion round trips through PIL.

* ``DxcamCapture``  - DXGI Desktop Duplication. Near zero-copy, reaches 30+
  fps at 1080p. Windows only and fails over RDP sessions.
* ``MssCapture``   - GDI BitBlt. Portable and always available, but every
  frame costs a CPU-side BGRA->RGB copy (~5-8 ms at 1080p).
"""

from __future__ import annotations

import logging
import re
import sys
import time
from abc import ABC, abstractmethod

import numpy as np

log = logging.getLogger(__name__)


def enable_dpi_awareness() -> None:
    """Must run before any capture, otherwise coordinates disagree with pixels."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        except OSError:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception as exc:  # noqa: BLE001
        log.warning("无法启用 DPI 感知：%s", exc)


class ScreenCapture(ABC):
    width: int = 0
    height: int = 0
    # (left, top, width, height) of what we actually capture, in virtual-desktop
    # coordinates.  Input injection maps its normalised ratios onto this rect,
    # which is what keeps clicks landing correctly on multi-monitor setups.
    region: tuple[int, int, int, int] = (0, 0, 1, 1)

    def open(self) -> None:  # noqa: B027 - optional hook
        """Acquire OS resources. Default: nothing to do."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release OS resources. Default: nothing to do."""

    def pause(self) -> None:  # noqa: B027 - optional hook
        """Stop the backend's own capture thread while nobody is watching.

        dxcam 的 ``start()`` 之后采集线程会一直满速跑，没人看的时候纯属烧核。
        """

    def resume(self) -> None:  # noqa: B027 - optional hook
        """Restart capture after ``pause()``. Default: nothing to do."""

    @abstractmethod
    def grab(self) -> np.ndarray | None:
        """Return the newest frame as an ``(H, W, 3) uint8`` RGB array.

        The returned array is owned by the caller and will never be written to
        again by the capture backend - the encode thread may hold it while the
        capture thread grabs the next one.
        """


class DxcamCapture(ScreenCapture):
    name = "dxcam"

    def __init__(self, region: tuple[int, int, int, int], target_fps: int = 60) -> None:
        self._target = tuple(region)
        self._target_fps = max(1, target_fps)
        self._cam = None
        self._running = False
        self._region_mismatch_logged = False

    def open(self) -> None:
        import dxcam

        # 按**桌面矩形**选输出，绝不按下标。``output_idx`` 是 DXGI 的枚举顺序，
        # 与 mss 的顺序、Windows 显示设置里的编号都无关 —— 它取决于哪块屏是主
        # 屏、每块屏在哪一侧、由哪块 GPU 驱动。两块同尺寸屏是最常见的情况，
        # 形状检查根本抓不住；只有矩形是 Win32 / mss / DXGI 三方都认的句柄。
        found = _find_dxcam_output(self._target)
        if found is None:
            raise RuntimeError(f"no DXGI output covers {self._target}")
        device_idx, output_idx, devicename = found
        self._output_index = output_idx
        self._cam = dxcam.create(
            device_idx=device_idx,
            output_idx=output_idx,
            output_color="RGB",
            max_buffer_len=2,
            # "numpy" is dxcam's compiled Cython swizzle; the default "cv2"
            # backend imports OpenCV and then dies on its capture thread.
            processor_backend="numpy",
        )
        # The rect we asked for is the rect we capture, so picture and pointer
        # can no longer disagree.
        self.region = self._target
        self._cam.start(target_fps=self._target_fps, video_mode=True)
        self._running = True
        first = self._cam.get_latest_frame()
        if first is not None:
            self.height, self.width = first.shape[:2]
        log.info("dxcam %s (dev %d out %d) -> %s", devicename, device_idx, output_idx, self.region)

    def grab(self) -> np.ndarray | None:
        assert self._cam is not None
        frame = self._cam.get_latest_frame()
        if frame is None:
            return None
        self.height, self.width = frame.shape[:2]
        # region 和上报的帧必须是同一块屏：region 来自 mss 的显示器枚举序，
        # dxcam 的 output_idx 是 DXGI 的输出序，多显卡/扩展坞上两套编号不保证
        # 一致。对不上时点击会整体偏到另一块屏上，而日志里什么都看不出来 ——
        # 至少让它喊一声（只喊一次）。
        if not self._region_mismatch_logged and (self.width, self.height) != (
            self.region[2],
            self.region[3],
        ):
            self._region_mismatch_logged = True
            log.warning(
                "采集帧 %dx%d 与上报区域 %dx%d 不一致：dxcam 的输出序与 mss 的显示器序"
                "可能对不上，鼠标坐标会落到别的显示器上（当前输出下标 %d）",
                self.width,
                self.height,
                self.region[2],
                self.region[3],
                self._output_index,
            )
        # dxcam hands out a view into a ring buffer its own thread keeps
        # overwriting, so we must take a copy before letting go of it.
        return frame.copy()

    def pause(self) -> None:
        # 幂等：dxcam 对重复 stop()/start() 会抛异常，而调用方不必关心当前状态。
        if self._cam is not None and self._running:
            try:
                self._cam.stop()
            except Exception:  # noqa: BLE001
                pass
            self._running = False

    def resume(self) -> None:
        if self._cam is not None and not self._running:
            try:
                self._cam.start(target_fps=self._target_fps, video_mode=True)
                self._running = True
            except Exception:  # noqa: BLE001
                log.exception("恢复 dxcam 采集线程失败")

    def close(self) -> None:
        if self._cam is not None:
            try:
                self._cam.stop()
            except Exception:  # noqa: BLE001
                pass
            # 必须 release()，不能只 stop()：dxcam 的 create() 有一个
            # WeakValueDictionary 实例缓存，只有 is_released 的旧实例才会被丢弃
            # 重建。旧实例被自己的采集线程引用着不会被回收 —— 不 release 的话，
            # 切回同一块屏时 create() 会把这只"已 stop 的僵尸"原样返回，再对它
            # start() 就是 use-after-free：native 段错误、被控端当场闪退。
            try:
                self._cam.release()
            except Exception:  # noqa: BLE001
                log.exception("释放 dxcam 实例失败")
            self._cam = None


class MssCapture(ScreenCapture):
    name = "mss"

    def __init__(self, region: tuple[int, int, int, int], target_fps: int = 60) -> None:
        # 收的是矩形而不是下标：``mss.monitors`` 的顺序来自 EnumDisplayMonitors，
        # 与 Windows 显示设置里的编号无关。按下标取就是"选 1 却操作了 2"的另
        # 一半；按矩形取才能保证画面与注入坐标指的是同一块物理屏。
        self._target = tuple(region)
        self._sct = None

    def open(self) -> None:
        import mss

        # `with_context` was removed from newer mss releases; it only controls
        # whether a context manager is used, so try both spellings.
        try:
            self._sct = mss.mss(with_context=True)
        except TypeError:
            self._sct = mss.mss()
        left, top, width, height = self._target
        # A degenerate rect means the caller could not resolve a display at all;
        # fall back to mss's first real monitor rather than grabbing 0x0.
        if width <= 0 or height <= 0:
            fallback = self._sct.monitors[1]
            left, top = fallback["left"], fallback["top"]
            width, height = fallback["width"], fallback["height"]
        self._mon = {"left": left, "top": top, "width": width, "height": height}
        self.width, self.height = width, height
        self.region = (left, top, width, height)

    def grab(self) -> np.ndarray | None:
        assert self._sct is not None
        shot = self._sct.grab(self._mon)
        # mss yields BGRA in a reusable buffer: slice the channels in reverse
        # (BGR -> RGB) and copy once, which does the swizzle and the ownership
        # handover in a single pass.
        return np.asarray(shot)[:, :, 2::-1].copy()

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None


class SolidColorCapture(ScreenCapture):
    """Diagnostic backend used by the capture smoke test."""

    name = "synthetic"

    def __init__(self, width: int = 1280, height: int = 720) -> None:
        self.width, self.height = width, height
        self.region = (0, 0, width, height)
        self._n = 0

    def grab(self) -> np.ndarray:
        self._n += 1
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[:, :, 0] = (self._n * 3) % 256
        return frame


def _open_mss():
    """mss 的入口，兼容新旧版本（新版删掉了 ``with_context`` 参数）。"""
    import mss

    try:
        return mss.mss(with_context=True)
    except TypeError:
        return mss.mss()


def monitor_region(index: int, fallback_w: int, fallback_h: int) -> tuple[int, int, int, int]:
    """Bounding rect of display *index* in virtual-desktop coordinates.

    与 ``list_monitors()`` 同源：采集与注入必须指向同一块屏，第二次枚举就多
    一次两者分歧的机会。
    """
    monitors = list_monitors()
    if 0 <= index < len(monitors):
        return monitors[index]
    return (0, 0, fallback_w, fallback_h)


def region_looks_bright(region: tuple[int, int, int, int]) -> bool | None:
    """用 mss 抓一小口 *region*，回答「这块屏幕现在是不是真有内容」。

    桌面复制丢了句柄时 dxcam 会继续交出一张全黑的旧缓冲 —— 那和「屏幕本来
    就是全黑的」长得一模一样，只看帧根本分不出来。mss 走 GDI，与 DXGI 的句柄
    无关，谁在说谎一眼可辨。

    返回 ``True``=有明显亮像素、``False``=确实全黑、``None``=这次没测出来。
    ``None`` 不等于黑：调用方要按「无法判断」走兜底，别拿它当重建的证据。
    """
    x, y, w, h = region
    if w <= 0 or h <= 0:
        return None
    try:
        with _open_mss() as sct:
            shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
        arr = np.asarray(shot)
    except Exception:  # noqa: BLE001 - 这是旁路探测，坏了不能影响主路径
        log.debug("mss 交叉探测失败", exc_info=True)
        return None
    if arr.size == 0:
        return None
    # 与 _capture_loop 的黑帧判定同一把尺子（每 8 个像素取一）。mss 给的是
    # BGRA，取 max 是跨通道的，彩色像素总有通道是亮的，结论一致。
    return bool(int(arr[::8, ::8].max(initial=0)) > 2)


def _device_number(device: str) -> int:
    """DISPLAY2 -> 2.  Sort key matching Windows' own display number."""
    match = re.search(r"(\d+)$", device or "")
    return int(match.group(1)) if match else 1 << 30


def _enum_monitors() -> list[tuple[str, tuple[int, int, int, int]]]:
    """(device name, rect) for every attached display, ordered like Windows.

    顺序取设备名里的数字（DISPLAY1 在前），因为那正是 Windows 显示设置里
    用户看到的编号；mss 的列表来自 EnumDisplayMonitors，顺序无意义。

    坐标是物理像素，所以必须先开 DPI 感知，否则矩形与 DXGI 报的对不上。
    """
    import ctypes
    from ctypes import wintypes

    enable_dpi_awareness()

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    proc_t = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )
    found: list[tuple[str, tuple[int, int, int, int]]] = []

    def _callback(hmon, _hdc, _rect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(
                (info.szDevice, (r.left, r.top, r.right - r.left, r.bottom - r.top))
            )
        return True

    callback = proc_t(_callback)  # keep the ref alive for the duration of the call
    ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0)
    return found


def _find_dxcam_output(rect: tuple[int, int, int, int]) -> tuple[int, int, str] | None:
    """(device_idx, output_idx, device name) of the DXGI output covering *rect*.

    dxcam numbers outputs in DXGI order; we need the one that is *this* screen.
    The desktop rect is the only handle all three of Windows, mss and DXGI share.
    """
    import dxcam

    want = tuple(rect)
    for device_idx, outputs in enumerate(dxcam.DXFactory().outputs):
        for output_idx, output in enumerate(outputs):
            try:
                if not output.attached_to_desktop:
                    continue
                desc = output.desc.DesktopCoordinates
            except Exception:  # noqa: BLE001 - a stale/undescribed output
                continue
            got = (desc.left, desc.top, desc.right - desc.left, desc.bottom - desc.top)
            if got == want:
                return device_idx, output_idx, output.devicename
    return None


def list_monitors() -> list[tuple[int, int, int, int]]:
    """Rects of every display, in virtual-desktop coordinates.

    按 Windows 自己的显示器编号排序，下标 0 就是 Windows 里的「显示器 1」。
    """
    try:
        # Sorted here rather than in _enum_monitors() so the ordering policy is
        # testable without stubbing the Win32 call.
        ordered = sorted(_enum_monitors(), key=lambda item: _device_number(item[0]))
        rects = [rect for _device, rect in ordered]
        if rects:
            return rects
    except Exception:  # noqa: BLE001 - not Windows, or the API refused us
        log.debug("Win32 显示器枚举失败，退回 mss", exc_info=True)
    try:
        with _open_mss() as sct:
            rects = [
                (m["left"], m["top"], m["width"], m["height"]) for m in sct.monitors[1:]
            ]
            if rects:
                return rects
    except Exception:  # noqa: BLE001
        pass
    return [(0, 0, 0, 0)]


def _verified(cap: ScreenCapture, name: str) -> ScreenCapture:
    """Acquire OS resources and fail loudly if no frames actually arrive.

    dxcam 的采集线程把自己吞掉的错误只写成一行日志，之后 ``grab()`` 就永远
    返回 ``None``。不先探一下，我们会挑中 dxcam 却什么都推不出去，然后去怪网络。
    """
    cap.open()
    for _ in range(60):  # ~1.5 s: long enough for the first Desktop Duplication frame
        try:
            frame = cap.grab()
        except Exception as exc:  # noqa: BLE001
            cap.close()
            raise RuntimeError(f"{name} grab raised: {exc}") from exc
        if frame is not None:
            return cap
        time.sleep(0.025)
    cap.close()
    raise RuntimeError(f"{name} produced no frames")


def create_capture(monitor_index: int = 0, target_fps: int = 30) -> ScreenCapture:
    """Prefer DXGI; fall back to mss. Either way, prove it works before use.

    两个后端收的都是同一个*矩形*而不是显示器下标：下标只在某一个后端自己的
    编号里有意义，而 Windows 显示设置、mss、DXGI 这三套编号互不吻合。提前
    把矩形定死一次，才保证推出去的画面与注入的点击指的是同一块物理屏。
    """
    from ..Recommon.config import init_com

    init_com()
    enable_dpi_awareness()

    monitors = list_monitors()
    if not 0 <= monitor_index < len(monitors):
        log.warning("显示器下标 %d 越界（共 %d 块），改用第 1 块", monitor_index + 1, len(monitors))
        monitor_index = 0
    rect = monitors[monitor_index]
    log.info("目标显示器 %d：%s", monitor_index + 1, rect)

    if sys.platform == "win32":
        try:
            # 让 DXGI 重复线程比我们采样得更快，这样取到的帧最坏也只是半个间隔
            # 前的，而不是"上一整个采样周期"。上限收到 120 只是别让 dxcam 的
            # swizzle 线程白烧 CPU。
            backend = DxcamCapture(
                region=rect, target_fps=min(120, max(2, target_fps * 2))
            )
            cap = _verified(backend, "dxcam")
            log.info("采集：dxcam %dx%d 区域=%s", cap.width, cap.height, cap.region)
            return cap
        except Exception as exc:  # noqa: BLE001 - RDP, headless, missing DX, no cv2
            log.warning("dxcam 不可用（%s），回退到 mss", exc)
    cap = _verified(MssCapture(region=rect), "mss")
    log.info("采集：mss %dx%d 区域=%s", cap.width, cap.height, cap.region)
    return cap
