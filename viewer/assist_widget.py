"""The main panel: a self-contained "remote assistance" control.

A ``QFrame`` carrying its own ``objectName``, so it drops into a host application
as one more interface rather than demanding a window of its own.

Layout (single page, three groups, no navigation rail):

    本设备信息    my IP + code, copy, set code, allow-control switch, who is
                  controlling me right now
    远程伙伴设备  their IP + code + connect
    连接历史      bare IPs; click to copy, double-click to fill the form

The stream itself never lands here - every session opens in its own
``RemoteWindow``.
"""

from __future__ import annotations

import logging
import threading
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QListWidgetItem,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    HeaderCardWidget,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    ListWidget,
    MessageBoxBase,
    PrimaryPushButton,
    RoundMenu,
    SubtitleLabel,
    SwitchButton,
    ToolButton,
)

from ..agent.server import AgentService
from ..Recommon import protocol as P
from ..Recommon.config import local_ips
from ..Recommon.pairing import normalise_code
from .client import RemoteFingerprintChanged, forget_pinned_fingerprint
from .history import PartnerHistory
from .remote_window import RemoteWindow, RetrustDialog

log = logging.getLogger(__name__)

HINT_IDLE = "把 IP 和验证码告诉要控制本机的人，验证码长期有效，可随时修改。"
STATUS_IDLE = "未被控制"
HISTORY_HINT = "单击复制 IP · 双击填入「伙伴 IP」"


# --------------------------------------------------------------------------
# dialogs
# --------------------------------------------------------------------------


class CodeDialog(MessageBoxBase):
    """Set the verification code this machine shows to others."""

    def __init__(self, current: str, parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel("设置验证码")
        self.edit = LineEdit()
        self.edit.setPlaceholderText("4-6 位数字或字母")
        self.edit.setText(current)
        self.edit.setMaxLength(6)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(CaptionLabel("对方需要输入此验证码才能控制本机："))
        self.viewLayout.addWidget(self.edit)
        self.widget.setMinimumWidth(500)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")

    def accept(self) -> None:
        from ..Recommon.pairing import normalise_code

        try:
            self.value = normalise_code(self.edit.text())
        except ValueError as exc:
            InfoBar.warning(
                title=str(exc),
                content="",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )
            return
        super().accept()


class ConfirmDialog(MessageBoxBase):
    """Generic yes/no prompt with a question as its body."""

    def __init__(self, title: str, body: str, parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(title)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(BodyLabel(body))
        self.widget.setMinimumWidth(500)
        self.yesButton.setText("确定")
        self.cancelButton.setText("取消")


# --------------------------------------------------------------------------
# main widget
# --------------------------------------------------------------------------


class RemoteAssistWidget(QFrame):
    # worker-thread -> GUI-thread marshalling
    _event = Signal(str, object)

    def __init__(
        self, name: str, parent=None, history_role: str = "viewer", target_fps: int = 30
    ):
        super().__init__(parent=parent)
        self.setObjectName(name)
        # 同 RemoteWindow：本组件的任何窗口都不负责退出宿主进程。宿主最小化到
        # 托盘后，若面板/会话窗口是最后一个可见窗口，Qt 会把"关它"当成"退出程序"。
        # 嵌入（有 parent）时无关紧要；独立窗口运行时它就是保命开关。
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)

        # history_role 可注入，测试就不会写进操作者真实的连接历史。
        self.history = PartnerHistory(history_role)
        self.windows: list[RemoteWindow] = []
        self.service: AgentService | None = None
        self._target_fps = target_fps
        self._connecting = False

        self._build_ui()
        self._event.connect(self._on_event)
        self._start_agent()
        self._refresh_history()

    # -- ui ---------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addWidget(self._build_mine_card())
        layout.addWidget(self._build_partner_card())
        layout.addWidget(self._build_history_card())
        layout.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll)

    # -- group 1: this device ---------------------------------------------
    def _build_mine_card(self) -> HeaderCardWidget:
        card = HeaderCardWidget(self)
        card.setTitle("本设备信息")
        body = QVBoxLayout()
        body.setSpacing(8)

        self.ip_edit = LineEdit()
        self.ip_edit.setReadOnly(True)
        self.ip_edit.setPlaceholderText("未检测到网络地址")
        body.addLayout(
            self._field_row(
                "本机 IP", self.ip_edit, self._make_tool(FluentIcon.COPY, "复制信息", self._copy_info)
            )
        )

        self.code_edit = LineEdit()
        self.code_edit.setReadOnly(True)
        self.code_edit.setPlaceholderText("未设置")
        body.addLayout(
            self._field_row(
                "验证码", self.code_edit, self._make_tool(FluentIcon.SETTING, "设置验证码", self._set_code)
            )
        )

        # R3a: who is controlling me right now
        self.status_label = BodyLabel(STATUS_IDLE)
        self.status_label.setWordWrap(True)
        body.addLayout(self._field_row("状态", self.status_label))

        # R5: accept incoming control at all
        self.allow_switch = SwitchButton(self)
        self.allow_switch.setChecked(True)
        self.allow_switch.checkedChanged.connect(self._on_allow_toggled)
        body.addLayout(self._field_row("允许被控", self.allow_switch))

        self.mine_hint = CaptionLabel(HINT_IDLE)
        self.mine_hint.setWordWrap(True)
        body.addWidget(self.mine_hint)

        card.viewLayout.addLayout(body)
        self.mine_card = card
        return card

    # -- group 2: remote partner ------------------------------------------
    def _build_partner_card(self) -> HeaderCardWidget:
        card = HeaderCardWidget(self)
        card.setTitle("远程伙伴设备")
        body = QVBoxLayout()
        body.setSpacing(8)

        self.partner_ip = LineEdit()
        self.partner_ip.setPlaceholderText("例如 192.168.1.10")
        self.partner_ip.returnPressed.connect(self._on_connect_clicked)
        body.addLayout(self._field_row("伙伴 IP", self.partner_ip))

        self.partner_code = LineEdit()
        self.partner_code.setPlaceholderText("对方屏幕上显示的验证码")
        self.partner_code.setMaxLength(6)
        self.partner_code.returnPressed.connect(self._on_connect_clicked)
        body.addLayout(self._field_row("验证码", self.partner_code))

        self.connect_btn = PrimaryPushButton("连接")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        body.addWidget(self.connect_btn, 0, Qt.AlignmentFlag.AlignRight)

        card.viewLayout.addLayout(body)
        self.partner_card = card
        return card

    # -- group 3: connection history (R2) ---------------------------------
    def _build_history_card(self) -> HeaderCardWidget:
        card = HeaderCardWidget(self)
        card.setTitle("连接历史")
        body = QVBoxLayout()
        body.setSpacing(8)

        self.history_list = ListWidget(self)
        # 固定高度 + 自带滚动条：卡片里的 Expanding 列表会撑满滚动区给的高度，
        # 第三组就统治整个面板了。
        self.history_list.setFixedHeight(150)
        self.history_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history_list.customContextMenuRequested.connect(self._on_history_menu)
        self.history_list.itemDoubleClicked.connect(self._on_history_double_clicked)
        body.addWidget(self.history_list)

        body.addWidget(CaptionLabel("双击填入 IP 和验证码 · 右键连接或删除"))
        card.viewLayout.addLayout(body)
        self.history_card = card
        return card

    @staticmethod
    def _field_row(label: str, widget: QWidget, trailing: QWidget | None = None) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        caption = CaptionLabel(label)
        caption.setFixedWidth(64)
        row.addWidget(caption)
        row.addWidget(widget, 1)
        if trailing is not None:
            row.addWidget(trailing)
        return row

    @staticmethod
    def _make_tool(icon, tooltip: str, slot) -> ToolButton:  # noqa: ANN001
        button = ToolButton(icon)
        button.setToolTip(tooltip)
        button.clicked.connect(slot)
        return button

    # -- agent -------------------------------------------------------------
    def _start_agent(self) -> None:
        def boot() -> None:
            try:
                service = AgentService(on_event=self._safe_emit, target_fps=self._target_fps)
                service.start()
            except Exception:  # noqa: BLE001
                log.exception("被控服务启动失败")
                self._safe_emit("agent_error", {"message": "被控服务启动失败，请检查端口占用"})
                return
            self._safe_emit("agent_ready", {"service": service})

        threading.Thread(target=boot, name="lrdp-agent-boot", daemon=True).start()

    def _safe_emit(self, name: str, data: dict) -> None:
        """Called from agent threads, possibly after this widget is gone."""
        try:
            self._event.emit(name, data)
        except RuntimeError:
            pass

    def _refresh_mine(self) -> None:
        self._set_text(self.ip_edit, " / ".join(local_ips()))
        self._set_text(self.code_edit, self.service.pair_code if self.service else "")

    def _set_text(self, edit: LineEdit, text: str) -> None:
        """Only touch the widget when the value really changed.

        ``setText`` 即使字符串完全相同也会重绘并移动光标；每次事件都做一遍就
        是肉眼可见的"闪"。
        """
        if edit.text() != text:
            edit.setText(text)

    def _set_status(self, text: str) -> None:
        if self.status_label.text() != text:
            self.status_label.setText(text)

    # -- my device actions -------------------------------------------------
    def _copy_info(self) -> None:
        text = f"IP: {self.ip_edit.text()}\n验证码: {self.code_edit.text()}"
        QGuiApplication.clipboard().setText(text)
        self._info("已复制本机信息")

    def _set_code(self) -> None:
        if self.service is None:
            self._warning("被控服务尚未就绪")
            return
        dialog = CodeDialog(self.service.pair_code, parent=self.window())
        if dialog.exec():
            self.service.set_pair_code(dialog.value)
            self._refresh_mine()
            self._info("验证码已更新")

    def _on_allow_toggled(self, checked: bool) -> None:
        if self.service is None:
            self._warning("被控服务尚未就绪")
            return
        self.service.set_allow_control(bool(checked))
        self._info("已允许被远程控制" if checked else "已关闭被控功能，他人将无法连接")
        if not checked:
            self._set_status(STATUS_IDLE)

    # -- connect -----------------------------------------------------------
    def _on_connect_clicked(self) -> None:
        # 自己已经开着这台主机的远程窗口，再说"远程已经启动"就是对的，别重复开。
        host = self.partner_ip.text().strip()
        port = P.DEFAULT_AGENT_PORT
        if ":" in host:
            host, _, maybe = host.rpartition(":")
            if maybe.isdigit():
                port = int(maybe)
        host = host.strip()
        for window in self.windows:
            if window.host == host and window.port == port:
                self._info("远程已经启动")
                window.raise_()
                window.activateWindow()
                return
        # 受控中不允许再控制别人
        if self.service is not None and self.service.is_busy:
            self._warning("现在受控于他人，无法控制其他设备")
            return
        code = self.partner_code.text().strip()
        if not host:
            self._warning("请输入伙伴 IP")
            return
        if not code:
            self._warning("请输入验证码")
            return
        self._start_session(host, port, code, source="form")

    def _start_session(self, host: str, port: int, code: str, *, source: str) -> None:
        if self._connecting:
            return
        if not code:
            self._warning("缺少验证码")
            return
        # 先按格式校验：验证码只可能是 4-6 位数字/字母。不校验的话，粘贴进来一个
        # 中文或全角字符会一路带到对端，在它的常量时间比较那一步抛 TypeError，
        # 握手直接断掉 —— 最后报的是"连接失败：peer closed the connection"，看着
        # 像对方掉线，其实是自己粘错了。
        try:
            code = normalise_code(code)
        except ValueError as exc:
            self._warning(str(exc))
            return
        self._connecting = True
        self.connect_btn.setText("校验中…")
        self.connect_btn.setEnabled(False)

        def worker() -> None:
            try:
                self.probe(host, port, code)
            except RemoteFingerprintChanged as exc:
                # 指纹不匹配不能吞成普通"探测失败"：它有专门的恢复路径（确认对端
                # 是本人 → 重新信任）。在这里被 except Exception 收走的话，用户在
                # 主面板点连接就永远看不到那个确认框。
                self._safe_emit(
                    "probe_fingerprint",
                    {
                        "host": host,
                        "port": port,
                        "code": code,
                        "source": source,
                        "error": str(exc),
                    },
                )
                return
            except Exception as exc:  # noqa: BLE001
                # 走 _safe_emit：探测线程回来时窗口可能已经销毁。
                self._safe_emit("probe_failed", {"host": host, "port": port, "error": str(exc)})
                return
            self._safe_emit(
                "probe_ok", {"host": host, "port": port, "code": code, "source": source}
            )

        threading.Thread(target=worker, name="lrdp-probe", daemon=True).start()

    @staticmethod
    def probe(host: str, port: int, code: str, timeout: float = 6.0) -> None:
        """Validate IP + code without opening a session window.

        Uses ``probe_only``: the agent checks the code, answers, and stops there
        - no session, no frames, no input injection, and the agent's single
        session slot stays free for the window we open next.

        Raises ``RemoteCodeChanged`` when the code stopped matching, which is
        what lets us say "被远程对象已更换验证码" instead of a generic failure.
        """
        from .client import ViewerClient

        client = ViewerClient(host, port, code=code, probe_only=True)
        try:
            client.connect(timeout=timeout)
        finally:
            client.close()

    def _open_window(self, host: str, port: int, code: str) -> None:
        partner = self.history.remember(host, port, code=code, success=True)
        self._refresh_history()
        self._set_text(self.partner_ip, host)
        self._set_text(self.partner_code, code)
        window = RemoteWindow(host, port, name=partner.label, code=code)
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        window.destroyed.connect(
            lambda _=None, w=window: self.windows.remove(w) if w in self.windows else None
        )
        self.windows.append(window)
        window.show()
        window.raise_()
        window.activateWindow()

    # -- history (R2) ------------------------------------------------------
    def _refresh_history(self) -> None:
        self.history_list.clear()
        for host, code, port in self.history.recent_entries():
            item = QListWidgetItem(f"{host}    {code or '（无验证码）'}")
            item.setData(Qt.ItemDataRole.UserRole, (host, code, port))
            self.history_list.addItem(item)
        if self.history_list.count() == 0:
            placeholder = QListWidgetItem("暂无连接记录")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.history_list.addItem(placeholder)

    def _history_target(self, item: QListWidgetItem | None) -> tuple[str, str, int] | None:
        """从列表项取出 (host, code, port)；占位项返回 None。"""
        data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return data if isinstance(data, tuple) else None

    def _on_history_menu(self, pos) -> None:  # noqa: ANN001
        target = self._history_target(self.history_list.itemAt(pos))
        if target is None:
            return
        menu = RoundMenu(parent=self)
        # 用信号绑定而不是比较 exec() 的返回值：菜单实现可能返回等价但不同的
        # 对象，`action is connect_action` 就永远是 False，两个菜单项都不响应。
        connect_action = Action(FluentIcon.LINK, "连接", menu)
        connect_action.triggered.connect(lambda: self._history_connect(target))
        delete_action = Action(FluentIcon.DELETE, "删除", menu)
        delete_action.triggered.connect(lambda: self._history_delete(target))
        menu.addAction(connect_action)
        menu.addAction(delete_action)
        menu.exec(self.history_list.mapToGlobal(pos))

    def _history_connect(self, target: tuple[str, str, int]) -> None:
        """填入 IP 与验证码并直接发起连接。"""
        host, code, port = target
        self._set_text(self.partner_ip, host)
        if code:
            self._set_text(self.partner_code, code)
        if port != P.DEFAULT_AGENT_PORT:
            self._set_text(self.partner_ip, f"{host}:{port}")
        self._on_connect_clicked()

    def _history_delete(self, target: tuple[str, str, int]) -> None:
        host, _code, port = target
        self.history.forget(host, port)
        self._refresh_history()
        self._info(f"已删除 {host}")

    def _on_history_double_clicked(self, item: QListWidgetItem) -> None:
        """双击回填 IP 与验证码。"""
        target = self._history_target(item)
        if target is None:
            return
        host, code, port = target
        self._set_text(self.partner_ip, f"{host}:{port}" if port != P.DEFAULT_AGENT_PORT else host)
        if code:
            self._set_text(self.partner_code, code)
        self._info(f"已填入 {host}")

    # -- cross-thread events ---------------------------------------------
    def _on_event(self, name: str, data: object) -> None:
        data = data or {}
        if name == "agent_ready":
            self.service = data["service"]
            self._refresh_mine()
            self.allow_switch.setChecked(self.service.allow_control)
            self._info("本机已可被远程")
        elif name == "agent_error":
            self._error(str(data.get("message", "被控服务启动失败")))
        elif name == "pair_code":
            self._refresh_mine()
        elif name == "client_connected":
            # R3a
            self._set_status(f"正在被 {data.get('client')} ({data.get('ip')}) 控制")
            self.mine_hint.setText("当前正被远程控制，可在此随时断开。")
        elif name == "client_disconnected":
            self._set_status(STATUS_IDLE)
            self.mine_hint.setText(HINT_IDLE)
        elif name == "client_rejected":
            # 有人趁我们被占用时来连：一个一直重试的控制端每秒能触发好几次 ——
            # 限流，让提示停留在"提示"的量级而不是糊满整个面板。
            now = time.monotonic()
            if now - getattr(self, "_last_reject_notice", 0.0) > 10.0:
                self._last_reject_notice = now
                self._warning("有新的连接请求被拒绝，当前正被他人控制")
        elif name == "probe_ok":
            self._unlock_connect()
            self._open_window(data["host"], data["port"], data["code"])
        elif name == "probe_failed":
            self._unlock_connect()
            self._handle_probe_failure(data)
        elif name == "probe_fingerprint":
            self._unlock_connect()
            self._ask_probe_retrust(data)
        elif name == "monitor":
            # 本机正在被别人控制，而对方切了被控端在推的显示器。
            try:
                index = int(data.get("index", 0)) + 1
            except (TypeError, ValueError):
                index = 1
            self._info(f"对方已切换到第 {index} 块显示器")

    def _unlock_connect(self) -> None:
        self._connecting = False
        self.connect_btn.setText("连接")
        self.connect_btn.setEnabled(True)

    def _ask_probe_retrust(self, data: dict) -> None:
        """主面板连接入口的「重新信任」裁决（与会话窗口同一个对话框）。

        对端重装、换运行目录都会换掉自签证书。确认 = 作废旧指纹、重新记录并继续
        连接；取消 = 保持拒绝。安全边界是操作者确认对端身份这个动作本身。
        """
        host = str(data.get("host", ""))
        dialog = RetrustDialog(host, str(data.get("error", "")), parent=self.window())
        if dialog.exec():
            try:
                forget_pinned_fingerprint(host, int(data.get("port", P.DEFAULT_AGENT_PORT)))
            except (TypeError, ValueError):
                self._error("重新信任失败：地址无效")
                return
            self._info("已重新信任，正在连接…")
            self._start_session(
                host,
                int(data.get("port", P.DEFAULT_AGENT_PORT)),
                str(data.get("code", "")),
                source=str(data.get("source", "form")),
            )
        else:
            self._warning("已拒绝连接：证书指纹不一致")

    def _handle_probe_failure(self, data: dict) -> None:
        message = str(data.get("error", ""))
        host = str(data.get("host", ""))
        try:
            port = int(data.get("port", P.DEFAULT_AGENT_PORT))
        except (TypeError, ValueError):
            # 缺 port 就按默认端口记：写成 0 会往历史里插一条 "主机:0"，下次点它
            # 就再也连不上。
            port = P.DEFAULT_AGENT_PORT
        self.history.remember(host, port, success=False)
        self._refresh_history()
        if "已更换验证码" in message:
            self._error(f"{host} 已更换验证码，请向对方索取新验证码")
        else:
            self._error(message or "连接失败")

    # -- toasts ------------------------------------------------------------
    def _info(self, msg: str) -> None:
        InfoBar.success(
            title=msg, content="", orient=Qt.Orientation.Horizontal,
            isClosable=True, position=InfoBarPosition.TOP, duration=3000, parent=self,
        )

    def _warning(self, msg: str) -> None:
        InfoBar.warning(
            title=msg, content="", orient=Qt.Orientation.Horizontal,
            isClosable=True, position=InfoBarPosition.TOP, duration=5000, parent=self,
        )

    def _error(self, msg: str) -> None:
        InfoBar.error(
            title=msg, content="", orient=Qt.Orientation.Horizontal,
            isClosable=True, position=InfoBarPosition.TOP, duration=8000, parent=self,
        )

    # -- lifecycle ---------------------------------------------------------
    def shutdown(self) -> None:
        """释放会话窗口与被控服务。幂等：closeEvent 与 aboutToQuit 各会调一次。"""
        for window in list(self.windows):
            window.close()
        if self.service is not None:
            self.service.on_event = lambda _n, _d: None
            # 先摘下引用再起线程：两次 shutdown 并发跑两个 stop() 会互相踩。
            service, self.service = self.service, None
            stopper = threading.Thread(target=service.stop, name="lrdp-shutdown", daemon=True)
            stopper.start()
            # 等它收完尾再让进程退出：dxcam/comtypes 的采集线程是 daemon，进程
            # 退出时被强杀 —— 正持有 DXGI/COM 内部锁的话会在 ExitProcess 阶段
            # 死锁。stop() 自身每步都有超时，这里最多再等 4 秒。
            stopper.join(timeout=4.0)

    def closeEvent(self, event) -> None:  # noqa: ANN001
        self.shutdown()
        super().closeEvent(event)


if __name__ == "__main__":
    import sys

    from PySide6.QtWidgets import QApplication

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    w = RemoteAssistWidget("remote_assist")
    w.setWindowTitle("局域网远程协助")
    w.resize(560, 720)
    # 独立运行：面板就是主窗口，关掉即退出程序 —— 把组件默认摘掉的属性要回来。
    w.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, True)
    w.show()
    app.aboutToQuit.connect(w.shutdown)
    sys.exit(app.exec())
