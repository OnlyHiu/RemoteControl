"""The remote session window.

Always a window of its own - the main panel never embeds the stream, which
keeps the assistant widget usable while someone is being helped.

Frame delivery is a single-slot mailbox: the reader thread signals
``_frames_ready`` with **no payload**, and the GUI thread's ``_drain()`` then
pulls the newest frame out of the mailbox.  A queued signal *carrying* a frame
on every frame is unbounded: the moment the GUI thread falls behind (occluded
window, modal dialog, slow box) the event queue accumulates ~6 MB QImage per
frame and takes the process down.  The mailbox caps that at one frame no matter
how far behind we get.
"""

from __future__ import annotations

import logging
import os
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QStatusBar, QVBoxLayout, QWidget

from qfluentwidgets import BodyLabel, CaptionLabel, MessageBoxBase, PillPushButton, PushButton, SubtitleLabel

from ..Recommon.framebus import FrameSlot
from ..Recommon.protocol import DEFAULT_AGENT_PORT
from .client import (
    RemoteCodeChanged,
    RemoteFingerprintChanged,
    ViewerClient,
    forget_pinned_fingerprint,
)
from .video_widget import VideoWidget, to_qimage

log = logging.getLogger(__name__)

STATS_INTERVAL_MS = 1000
#: 收到对端剪贴板后多久之内不回发（时间窗防回声，见 _on_clipboard_changed）。
CLIP_ECHO_WINDOW_S = 2.0
#: 重连退避基数。指数退避只对【连接失败】爬升（连接成功会清零），所以"连上又被
#: 外部拆掉"的场景恒等于这个值 —— 在勉强的链路上赶紧回来比任何客户端加固都更能
#: 改善体感。怀疑拆流频率与建连速率相关时，可用 LRDP_RECONNECT_BASE_S=2.0
#: 跑一轮对照实验。
RECONNECT_BASE_S = float(os.environ.get("LRDP_RECONNECT_BASE_S", "0.2"))
RECONNECT_MAX_S = 3.0


class RetrustDialog(MessageBoxBase):
    """「重新信任」确认框：证书指纹不匹配时由人裁决。

    不能没有恢复路径 —— 对端重装/换运行目录就会换证书，普通用户没法手改 JSON，
    等于永远连不上。安全边界就是「操作者确认对端是本人」这个动作本身：不确认就
    拒绝，确认就作废旧指纹、重新记录。
    """

    def __init__(self, host: str, detail: str, parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel("证书指纹与首次连接记录不一致")
        self.viewLayout.addWidget(self.titleLabel)
        body = BodyLabel(
            f"「{host}」出示的证书和第一次连接时记录的不一样。\n"
            "常见原因是对端重装或更换了运行目录（自签证书每个安装各一份）；\n"
            "也可能是有人在中间冒充。\n\n"
            f"详情：{detail}\n\n"
            "只有确认对端就是要连的那台机器时，才应重新信任。"
        )
        body.setWordWrap(True)
        self.viewLayout.addWidget(body)
        self.widget.setMinimumWidth(520)
        self.yesButton.setText("重新信任并连接")
        self.cancelButton.setText("取消")


class RemoteWindow(QMainWindow):
    """One live remote session."""

    # emitted from worker/reader threads, handled on the GUI thread
    _result = Signal(object)
    _stream = Signal(str, object)
    # No payload on purpose: the GUI thread pulls the newest frame out of the
    # mailbox instead of carrying one per signal.  Queued signals then cost
    # nothing even when the GUI falls behind, and a frame is painted the moment
    # it arrives rather than on the next timer tick.
    _frames_ready = Signal()

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_AGENT_PORT,
        *,
        name: str = "",
        code: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # 关掉远程窗口绝不能把宿主程序一起带走：Qt 默认的 quitOnLastWindowClosed
        # 会在"最后一个可见窗口"关闭时 quit 整个 QApplication。嵌入宿主后，宿主
        # 主窗口最小化到托盘（hide，仍算开着）时本窗口就是最后一个可见窗口 ——
        # 不摘掉这个属性，点一下关闭宿主程序就整个退出了。
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.host = host
        self.port = port
        self.code = code
        self.setWindowTitle(f"远程协助 - {name or host}")
        self.resize(1180, 740)

        # 图标路径在 __init__.py 的配置区块里设；未设置则用系统默认。
        from ..Recommon import settings

        icon_path = settings.window_icon_path()
        if icon_path is not None and icon_path.exists():
            from PySide6.QtGui import QIcon

            self.setWindowIcon(QIcon(str(icon_path)))

        self._client: ViewerClient | None = None
        self._closing = False
        self._reconnects = 0
        self._reconnect_total = 0  # 本窗口累计断开次数（不清零）
        self._recv = 0
        self._rendered = 0
        self._painted = False  # 本会话画过第一帧没有（见 _drain）
        self._drop_mark = 0
        self._stat_t = time.monotonic()
        self._agent_stats = ""
        self._rtt_ms: float | None = None  # 最近一次 PING/PONG 实测 RTT
        # 剪断本机对端桌面显示：对方关掉程序或取消控制时就别继续展示。瞬时抖动会在
        # 这个窗口内重连回来，所以只有会话真的结束才清屏。
        self._blank_timer = QTimer(self)
        self._blank_timer.setSingleShot(True)
        self._blank_timer.setInterval(5000)
        self._blank_timer.timeout.connect(self._blank_after_gap)

        # 重连定时器必须持有引用：QTimer.singleShot 那个静态重载没有对象可停，
        # 关窗之后它照样会触发 _connect，建出一个没人管的活会话（被控端会一直推流
        # 并接受键鼠注入）。改成成员 QTimer，closeEvent 里可以 cancel。
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._connect)

        # Single-slot mailbox: newest frame wins, older ones are dropped.
        self._frames: FrameSlot = FrameSlot()
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(STATS_INTERVAL_MS)
        self._stats_timer.timeout.connect(self._report_stats)

        self.video = VideoWidget()
        self.video.set_placeholder("正在连接…")  # first paint only; see _connect()
        self._build_toolbar()  # 顶部栏 + 画面一起装进中央容器

        self.remote_label = BodyLabel(f"{name or host}  ({host})")
        self.stats = CaptionLabel("")

        status = QStatusBar()
        status.addWidget(self.remote_label)
        status.addPermanentWidget(self.stats)
        self.setStatusBar(status)

        self._result.connect(self._on_result)
        self._stream.connect(self._on_stream)
        self._frames_ready.connect(self._drain)
        self.video.status.connect(lambda m: self.statusBar().showMessage(m, 4000))
        self._stats_timer.start()
        self._connect()
        # 剪贴板双向同步：本机复制 → 推给对端
        QGuiApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)

    # -- toolbar -----------------------------------------------------------
    def _build_toolbar(self) -> None:
        """顶部操作栏 + 画面的中央布局。全部 qfluentwidgets 控件。"""
        top = QWidget()
        bar = QHBoxLayout(top)
        bar.setContentsMargins(10, 6, 10, 2)
        bar.setSpacing(8)

        self.disconnect_btn = PushButton("断开")
        self.disconnect_btn.setToolTip("Ctrl+Shift+D 断开连接")
        self.disconnect_btn.clicked.connect(lambda _on=False: self._manual_disconnect())
        bar.addWidget(self.disconnect_btn)
        QShortcut(QKeySequence("Ctrl+Shift+D"), self, self._manual_disconnect)

        self.fullscreen_btn = PillPushButton("全屏")
        self.fullscreen_btn.setToolTip("F11 进入全屏 / 退出全屏")
        self.fullscreen_btn.clicked.connect(self._toggle_fullscreen)
        bar.addWidget(self.fullscreen_btn)
        QShortcut(QKeySequence("F11"), self, self._toggle_fullscreen)

        bar.addSpacing(8)

        # 显示器切换按钮区：被控端有几块屏就放几个按钮（1、2、3…），
        # 连接后才知道数量，所以先占位、后填充。
        self.monitor_label = CaptionLabel("显示器")
        self.monitor_label.setTextColor(QColor(120, 120, 130), QColor(170, 170, 180))
        bar.addWidget(self.monitor_label)
        self.monitor_box = QWidget()
        self.monitor_layout = QHBoxLayout(self.monitor_box)
        self.monitor_layout.setContentsMargins(0, 0, 0, 0)
        self.monitor_layout.setSpacing(4)
        bar.addWidget(self.monitor_box)
        self.monitor_label.hide()
        self.monitor_box.hide()
        self._monitor_buttons: list[PillPushButton] = []

        # 没有渲染定时器：帧到即画（见 ``_drain``），所以没有固定帧率可配。
        self.fps_readout = CaptionLabel("")
        bar.addStretch(1)
        bar.addWidget(self.fps_readout)

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(top)
        outer.addWidget(self.video, 1)
        self.setCentralWidget(container)

    def _toggle_fullscreen(self, checked: bool = False) -> None:
        """F11 / 按钮共用的全屏开关。快捷键走过来时要手动同步按钮选中态。"""
        if self.isFullScreen():
            self.showNormal()
            self.fullscreen_btn.setChecked(False)
        else:
            self.showFullScreen()
            self.fullscreen_btn.setChecked(True)

    # -- connection --------------------------------------------------------
    def _connect(self) -> None:
        # 每次尝试都留一条。失败那侧以前只进状态栏，于是磁盘上只有"连上了/断了"，
        # 看不到试了几次、每次结果如何。成功时由 ("connected") 补上另一半。
        log.info("开始连接 %s:%d（第 %d 次尝试）", self.host, self.port, self._reconnects + 1)
        self.statusBar().showMessage(f"正在连接 {self.host} …")
        # 绝不在这里碰画面。到这一步帧槽已经被渲染泵排空了，一测就是"没画面"，
        # 于是在完好的最后一帧上刷一层深色占位符 —— 那道闪就是这么来的。占位符在
        # 构造时设一次；重连时画面只是冻住，然后接着走。
        import threading

        threading.Thread(
            target=self._connect_worker, name="lrdp-connect", daemon=True
        ).start()

    def _connect_worker(self) -> None:
        # 关窗竞态：closeEvent 只是把 _closing 置位，而排下来的重连取消不掉。于是
        # 关窗后那一次仍会真的建连，线程起来、self._client 被赋值，而
        # WA_DeleteOnClose 已经把 C++ 对象销毁了 —— 结果是没人看、也没人关的活
        # 会话。每一处碰 self 的地方都要先问一句 _closing。
        if self._closing:
            return
        try:
            client = ViewerClient(
                self.host,
                self.port,
                code=self.code,
                on_frame=self._on_frame,
                on_event=self._on_event,
            )
            client.connect()
        except RemoteCodeChanged as exc:
            if not self._closing:
                self._result.emit(("code_changed", str(exc)))
            return
        except RemoteFingerprintChanged as exc:
            # 与验证码变更同理：盲重试永远过不了，交给操作者裁决。
            if not self._closing:
                self._result.emit(("fingerprint_changed", str(exc)))
            return
        except Exception as exc:  # noqa: BLE001 - surfaced below
            if not self._closing:
                self._result.emit(
                    ("failed", f"{type(exc).__name__}: {exc}", bool(getattr(exc, "permanent", False)))
                )
            return
        # 建连期间可能被关掉了：不能把会话挂在已经销毁的窗口上。
        if self._closing:
            client.close("窗口已关闭")
            return
        self._client = client
        # 上面的检查到赋值之间仍可能被关掉：closeEvent 看到的还是 None，
        # _teardown 什么也不会关 —— 留下一个没人看、也没人关的活会话。补一次复查兜住这个窗口。
        if self._closing:
            self._client = None
            client.close("窗口已关闭")
            return
        self.video.set_sink(client)
        self._result.emit(
            ("connected", client.screen_w, client.screen_h, client.remote_fps,
             client.monitors, client.monitor_index)
        )

    def _manual_disconnect(self) -> None:
        self._closing = True
        self._teardown("已断开", blank=True)

    # -- 多屏切换 -----------------------------------------------------------
    def _set_monitors(self, monitors: list, current: int) -> None:
        """按被控端的显示器数量生成按钮：1、2、3…，当前那块高亮。"""
        for b in self._monitor_buttons:
            b.setParent(None)
        self._monitor_buttons = []
        count = max(1, len(monitors))
        # 只有一块屏时不显示按钮区，免得占地方
        self.monitor_label.setVisible(count > 1)
        self.monitor_box.setVisible(count > 1)
        for i in range(count):
            # PillPushButton：选中=主题色填充、未选中=灰底描边，当前推流的那块屏
            # 一眼可辨（PushButton 自绘背景，checked 没有任何视觉变化）。
            btn = PillPushButton(str(i + 1))
            btn.setCheckable(True)
            btn.setChecked(i == current)
            btn.setToolTip(f"切换到第 {i + 1} 块显示器")
            btn.setFixedWidth(44)
            btn.clicked.connect(lambda _on=False, idx=i: self._switch_monitor(idx))
            self.monitor_layout.addWidget(btn)
            self._monitor_buttons.append(btn)

    def _switch_monitor(self, index: int) -> None:
        client = self._client
        if client is None or not client.connected:
            return
        self._mark_monitor(index)
        client.set_monitor(index)
        self.statusBar().showMessage(f"正在切换到第 {index + 1} 块显示器…", 5000)

    def _mark_monitor(self, index: int) -> None:
        """高亮第 index 块（0 起）。"""
        for i, b in enumerate(self._monitor_buttons):
            b.setChecked(i == index)

    def _on_monitor_ack(self, data: dict) -> None:
        """被控端确认（或拒绝了）切屏请求。

        按钮是先点亮、再发请求的，所以回执到达前那个高亮是乐观的。被拒时必须按
        对端回传的 index 拨回它当前那块 —— 没有回执的话按钮就永远停在刚点亮的
        那块上，而画面其实还在原来那块屏。
        """
        try:
            index = int(data.get("index", 0))
        except (TypeError, ValueError):
            index = 0
        monitors = data.get("monitors") or []
        if monitors:
            # 对端报的显示器列表可能和建连时不同（中间插拔过扩展屏），重建按钮。
            self._set_monitors(monitors, index)
        else:
            self._mark_monitor(index)
        if data.get("ok", True):
            self.statusBar().showMessage(f"已切换到第 {index + 1} 块显示器", 4000)
        else:
            self._mark_monitor(index)
            self.statusBar().showMessage(
                f"切换失败：{data.get('error') or '对方拒绝了该请求'}", 6000
            )

    def _on_clipboard_changed(self) -> None:
        """本机复制 → 推给被控端，实现「这边复制、那边粘贴」。

        ``_clip_local`` 记着我们自己写进去的内容，避免被控端回传后又发回去形成
        回声循环。文本比较只是第一道（Windows 会规范化剪贴板内容，往返一圈文本
        就对不上），``_clip_recv_at`` 时间窗是第二道。
        """
        client = self._client
        if client is None or not client.connected:
            log.info("剪贴板变化但未连接，忽略")
            return
        text = QGuiApplication.clipboard().text()
        log.info("本机剪贴板变化：%d 字符", len(text))
        if not text or text == client._clip_local:
            return
        if time.monotonic() - getattr(client, "_clip_recv_at", 0.0) < CLIP_ECHO_WINDOW_S:
            return
        # Windows 上同一个应用会连着把同一段文本 set 剪贴板两三次。内容与上次
        # 发送的完全相同、又在几秒之内 —— 只算一次复制，不必再发。
        now = time.monotonic()
        if (
            text == getattr(self, "_clip_last_sent", None)
            and now - getattr(self, "_clip_last_at", 0.0) < 3.0
        ):
            return
        self._clip_last_sent = text
        self._clip_last_at = now
        client.send_clipboard(text)

    def _blank_after_gap(self) -> None:
        """No session for 5 s: this is a real end, not a blip."""
        self.video.set_placeholder("对方已结束远程控制")
        self.statusBar().showMessage("对方已结束远程控制")

    def _teardown(self, message: str, blank: bool = False) -> None:
        """Drop the client.  *blank* clears the picture; only user-initiated
        disconnects do that.

        掉线时**故意**保留最后一帧：画面马上就要回来，先刷一层深色占位符读起来
        就是"屏幕黑了一下又闪"。
        """
        client, self._client = self._client, None
        self._rtt_ms = None  # 会话没了，别把上一次的 RTT 挂在状态栏上
        if getattr(self.video, "_sink", None) is client:
            # 只清"我们正要关掉的这个会话"的输入口：无差别 set_sink(None) 会把
            # 刚接上的新会话也弄成画面正常、键鼠全失效。
            self.video.set_sink(None)
        if blank:
            self.video.set_placeholder(message or "未连接")
        if client is not None:
            import threading

            threading.Thread(target=client.close, args=(message or "断开",), name="lrdp-close", daemon=True).start()
        if blank:
            self._blank_timer.stop()
        else:
            # Give it 3 s to come back; only then is this a real end.
            self._blank_timer.start()
        if message:
            self.statusBar().showMessage(message, 4000)

    # -- cross-thread hooks ------------------------------------------------
    def _on_frame(self, rgb, ts_us: int) -> None:  # noqa: ANN001
        # 跑在读线程上：在这里转换，绝不在 GUI 线程做。
        self._frames.put(to_qimage(rgb))
        self._recv += 1
        self._frames_ready.emit()

    def _on_event(self, name: str, data: dict) -> None:
        self._stream.emit(name, data)

    def _drain(self) -> None:
        """Paint whatever is newest. Cheap no-op when nothing has arrived."""
        image = self._frames.take(timeout=0)
        if image is None:
            return
        self.video.set_image(image)
        self._rendered += 1
        if not self._painted:
            # 一次会话只说一次。_rendered 每个统计周期都清零，拿它当"首帧"的
            # 判断会每秒刷一条，把真正的重连信号淹进噪音。
            self._painted = True
            log.info("首帧已绘制 %dx%d", image.width(), image.height())

    def _report_stats(self) -> None:
        """Report rates, not running totals.

        最新优先的投递本来就故意丢旧帧，累计计数器只增不减、看着像 bug。把接收与
        渲染分开还能告诉你瓶颈在哪一端：接收低说明被控端或网络慢，渲染低说明
        GUI 慢。
        """
        now = time.monotonic()
        elapsed = now - self._stat_t
        if elapsed <= 0:
            return
        dropped = self._frames.dropped - self._drop_mark
        parts = [
            f"接收 {self._recv / elapsed:.0f}",
            f"渲染 {self._rendered / elapsed:.0f} fps",
        ]
        # RTT 排最前：判断"卡"是网络抖动还是本地慢，先看这个数。
        if self._rtt_ms is not None:
            parts.insert(0, f"RTT {self._rtt_ms:.0f}ms")
        if dropped:
            parts.append(f"丢弃 {dropped / elapsed:.0f}/s")
        if self._agent_stats:
            parts.append(self._agent_stats)
        self.stats.setText(" · ".join(parts))
        # Mirror the live rate next to the frame-rate selector at the top.
        self.fps_readout.setText(f"实际 {self._rendered / elapsed:.0f} fps")
        self._recv = 0
        self._rendered = 0
        self._drop_mark = self._frames.dropped
        self._stat_t = now

    # -- GUI-thread handling ----------------------------------------------
    def _on_result(self, payload: object) -> None:
        kind = payload[0]
        if kind == "connected":
            self._reconnects = 0
            self._closing = False
            self._painted = False
            self._blank_timer.stop()  # a session is up again - keep the picture
            # 多屏信息是后加的字段，兼容旧的 4 元组调用方；这一段绝不能让窗口
            # 构造失败。
            try:
                self._set_monitors(
                    payload[4] if len(payload) > 4 else [[0, 0, payload[1], payload[2]]],
                    payload[5] if len(payload) > 5 else 0,
                )
            except Exception:  # noqa: BLE001
                log.exception("生成显示器按钮失败")
            self.remote_label.setText(
                f"{self.host}  ({payload[1]}×{payload[2]})"
            )
            msg = "已连接，点击画面以捕获键鼠"
            if self._reconnect_total:
                msg += f"（本次窗口已断 {self._reconnect_total} 次）"
            self.statusBar().showMessage(msg, 5000)
        elif kind == "code_changed":
            self._stop_retrying()
            self._teardown("验证码已变更", blank=True)
            self._result.emit(("notify_error", payload[1]))
        elif kind == "fingerprint_changed":
            self._stop_retrying()
            self._teardown("证书指纹已改变", blank=True)
            self._ask_retrust(str(payload[1]))
        elif kind == "failed":
            message = str(payload[1])
            # 失败也要落盘：BUSY（会话槽还没释放）/ TLS 失败 / 超时 / 被拒，四种
            # 原因在日志里长得一模一样，断线风暴期间无从判断是哪一种。这条与
            # _connect() 的"开始连接"配对出现。
            log.info(
                "连接失败（第 %d 次尝试，%s）：%s",
                self._reconnects + 1,
                "永久" if payload[2] else "可重试",
                message,
            )
            if payload[2]:
                # 对端明确拒绝了 —— 别再问了。_teardown() 会结束客户端，客户端又
                # 发 session_ended，session_ended 又调 _teardown() 然后
                # _schedule_reconnect()。不先置 _closing 就会排出下一次尝试，
                # 变成"拒绝 → 重试 → 拒绝"的自我振荡。
                self._stop_retrying()
                self._teardown(message, blank=True)
                self.statusBar().showMessage(f"{message}，窗口即将关闭")
                QTimer.singleShot(1500, self.close)
            else:
                self._teardown(message)
                self._schedule_reconnect()
        elif kind == "notify_error":
            self.video.set_placeholder(str(payload[1]))

    def _ask_retrust(self, message: str) -> None:
        """指纹不匹配的恢复路径：确认对端是本人 → 作废旧指纹 → 重连。

        确认框的按钮文案必须把后果说清楚（"重新信任"而不是"确定"），否则一次
        点击就把中间人检查给关了。
        """
        dialog = RetrustDialog(self.host, message, parent=self)
        if dialog.exec():
            forget_pinned_fingerprint(self.host, self.port)
            self._closing = False  # _stop_retrying() 置过位，重连前必须复位
            self.statusBar().showMessage("已重新信任，正在重连…")
            self._connect()
        else:
            self.statusBar().showMessage("已拒绝连接：证书指纹不一致", 6000)
            QTimer.singleShot(1500, self.close)

    def _on_stream(self, name: str, data: dict) -> None:
        if name == "stats":
            # 被控端遥测：q 说明它在自适应，耗时/预算说明瓶颈在哪一侧。
            # 不显示 scale：分辨率刻意钉死在 100%（AdaptiveQuality，SCALES=(1.0,)），
            # 那一列永远是 100%，像是自适应分辨率其实没有。kb 要标清口径 ——
            # 它是最近一两秒的平均帧大小（见 _Session._emit_stats）。
            # q 是控制器的计算值，q_eff 是编码器真正生效的值（H.264 的码率只在
            # 开流时定，会话内两者会分道扬镳）；不标清楚，一个根本没生效的控制器
            # 会"看起来在工作"。
            q, q_eff = data.get("q"), data.get("q_eff")
            q_text = f"q={q}" if q_eff in (None, q) else f"q={q}→生效{q_eff}"
            self._agent_stats = (
                f"被控端 {q_text} "
                f"{data.get('cost_ms')}/{data.get('budget_ms')}ms "
                f"{data.get('kb')}KB/帧（最近均值）"
            )
            return
        if name == "pong":
            # RTT 由 PING/PONG 回显测得（见 ViewerClient._dispatch_inner）。
            self._rtt_ms = data.get("rtt_ms")
            # 落盘的理由不只是"看抖动"：两机时钟不同步，判断"对端看到的 EOF 是
            # 链路先发的、还是它收到 BYE 之后才关的"只能靠 RTT 校准偏移。
            if self._rtt_ms is None:
                log.info("RTT 无回执")
            else:
                log.info("RTT %.1f ms", self._rtt_ms)
            return
        if name == "monitor":
            self._on_monitor_ack(data)
            return
        if name == "session_ended":
            if self._closing:
                return
            sender = data.get("client")
            if sender is not None and sender is not self._client:
                # 迟到的旧会话事件：新会话已经接上了，不能被它拖下水 —— 照单全收
                # 会把**新会话**的输入 sink 清掉，画面照常流动、键鼠却全部失效。
                log.info("忽略已结束会话的迟到事件：%s", data.get("reason"))
                return
            self._teardown(str(data.get("reason") or "连接已断开"))
            self._schedule_reconnect()

    def _stop_retrying(self) -> None:
        """End the reconnect loop for good.

        必须在 _teardown() **之前**调：teardown 关闭客户端 → 客户端发
        session_ended → 走到 _schedule_reconnect()。先置 _closing 才能让那次调用
        变成空操作，而不是再多排一次尝试。
        """
        self._closing = True
        self._blank_timer.stop()
        self._reconnect_timer.stop()

    def _schedule_reconnect(self) -> None:
        if self._closing or self._client is not None:
            return
        self._reconnects += 1
        self._reconnect_total += 1
        delay = min(RECONNECT_MAX_S, RECONNECT_BASE_S * (2 ** min(self._reconnects - 1, 5)))
        # 只更新状态栏，画面原样留着。累计次数必须显示：被外部反复拆流时画面一顿
        # 一顿，操作者需要知道"已经断了 N 次"，而不是看着画面冻结毫无解释。
        self.statusBar().showMessage(
            f"连接中断，{delay:.1f} 秒后自动重连…（本次窗口已断 {self._reconnect_total} 次）"
        )
        self._reconnect_timer.start(int(delay * 1000))

    # -- lifecycle ---------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        self._stats_timer.start()

    def hideEvent(self, event) -> None:  # noqa: ANN001
        # 没东西可画；别在隐藏的窗口上烧 GUI 时间。
        self._stats_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._closing = True
        self._reconnect_timer.stop()
        self._stats_timer.stop()
        self._teardown("", blank=True)
        self._frames.close()
        # 剪贴板是单例，它的信号会一直持有 self，窗口关掉后就是一个泄漏。
        try:
            QGuiApplication.clipboard().dataChanged.disconnect(self._on_clipboard_changed)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)
