"""The controlling side's connection: handshake, frames down / input up.

Deliberately Qt-free - it hands decoded RGB numpy arrays to a callback that
runs on the reader thread, so the caller converts to ``QImage`` there instead
of on the GUI thread.

Liveness model
--------------
There is no "the screen did not change" case in a remote desktop: if frames
stop arriving something is wrong.  The client therefore

* sends ``PING`` every ``KEEPALIVE_S`` seconds so idle-looking links are not
  reaped by NAT/firewall/endpoint-inspection boxes, and
* gives up after ``DEAD_S`` seconds of total silence instead of sitting on a
  half-open socket forever.

``recv`` uses a timeout so a vanished peer surfaces as an error rather than a
thread that never returns.
"""

from __future__ import annotations

import logging
import platform
import queue
import socket
import ssl
import threading
import time
import traceback
from typing import Callable

from ..Recommon import protocol as P
from ..Recommon.codec import create_decoder
from ..Recommon.config import Config
from ..Recommon.security import peer_certificate_fingerprint, viewer_tls_context

log = logging.getLogger(__name__)

FrameHook = Callable[[object, int], None]  # (rgb ndarray, ts_us)
EventHook = Callable[[str, dict], None]

KEEPALIVE_S = 5.0
DEAD_S = 30.0
RECV_TIMEOUT_S = 10.0

#: Events ``ViewerClient`` emits through ``on_event``, with their payload keys.
#: Deliberately disjoint from the session results the owner synthesises for
#: itself - see ``RemoteWindow._result``.
CLIENT_EVENTS: dict[str, tuple[str, ...]] = {
    "session_started": ("w", "h", "fps"),
    "session_ended": ("reason", "client"),
    # PONG 回显的是我们 PING 里的 ts（见 agent 的 PING 处理），减一下就是
    # 真实 RTT；对端太旧没回显时 rtt_ms 为 None。
    "pong": ("rtt_ms",),
    "rejected": (),
    # 被控端确认切换了显示器。ok=False 时 index 是它当前那块，不是请求的那块。
    "monitor": ("index", "monitors", "ok", "error"),
    # agent-side telemetry, so the viewer can show *why* a stream is slow.
    # kb 是最近一两秒的平均帧大小（不是当前帧）；scale 恒为 100 —— 分辨率是
    # 刻意钉死的（AdaptiveQuality / SCALES=(1.0,)），界面已不再显示它。
    "stats": ("q", "scale", "cost_ms", "budget_ms", "kb", "q_eff"),
}


#: 拒绝原因里只有这些是"再试也没用"的终态。其余（BUSY、SHUTTING_DOWN、认不出的
#: code）一律按"暂时的"处理：多开窗口、探测与建连撞车、被控端正在重启、上一个
#: 会话收尾稍慢，都会撞出这些拒绝 —— 而一旦被当成永久错误，remote_window 会
#: 停止重试并在 1.5 秒后把用户的窗口整个关掉。宁可多试几次，不能错杀窗口。
_TERMINAL_REJECTS = frozenset(
    {
        int(P.Reject.AUTH_FAILED),
        int(P.Reject.BAD_TOKEN),
        int(P.Reject.DENIED),
        int(P.Reject.PROTO_MISMATCH),
        int(P.Reject.CONTROL_DISABLED),
    }
)


def _reject_is_terminal(reason: object) -> bool:
    """对端的拒绝是不是"再试也没用"；认不出的 code 一律按暂时处理。

    判错的代价不对称：错误地重试只是多等几个退避档、状态栏多报几次；
    错误地终态是把用户的窗口整个关掉。所以终态用白名单。
    """
    return P.reject_code(reason) in _TERMINAL_REJECTS


class ConnectionError_(Exception):
    """Raised when the remote side refuses us. The message is user-facing.

    ``permanent`` 区分"对方不要我们"（不重试）与"链路抖了一下"（重试是对的）。
    盲目重试一次拒绝，就是无休止的"请求被拒绝"刷屏和一个永远不关的远程窗口。
    """

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class RemoteCodeChanged(ConnectionError_):
    """The stored verification code no longer works: they rotated it."""

    def __init__(self, message: str) -> None:
        super().__init__(message, permanent=True)


class RemoteFingerprintChanged(ConnectionError_):
    """远端证书指纹与首次连接记录不一致。

    常见原因不是攻击而是对端重装 / 换了运行目录（自签证书每个安装各一份）。
    拒绝是对的，但必须给操作者一条恢复路径 —— 确认对端是本人后重新信任
    （``forget_pinned_fingerprint`` + 重连），而不是去手改 JSON。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, permanent=True)


def forget_pinned_fingerprint(
    host: str, port: int, config: Config | None = None
) -> None:
    """作废记录在案的证书指纹（操作者已确认对端换的是新证书）。

    下次连接重新记录新指纹 —— TOFU 的"第一次"从头开始。*config* 只为测试隔离
    而存在，正常调用不传。
    """
    (config or Config("viewer")).pop(f"fp:{host}:{port}")


def _friendly_reason(exc: BaseException) -> str:
    """Turn socket internals into something an operator can act on.

    "peer closed the connection" is accurate but meaningless to the person
    watching the screen - what they need to know is that the other side went
    away and whether it is coming back.
    """
    text = str(exc) or type(exc).__name__
    lowered = text.lower()
    if "peer closed" in lowered or "premature end" in lowered:
        return "对方已断开连接"
    if isinstance(exc, ConnectionResetError) or "reset" in lowered:
        return "连接被重置"
    if isinstance(exc, ConnectionAbortedError) or "aborted" in lowered:
        return "连接被中止"
    if "timed out" in lowered or "timeout" in lowered:
        return "连接超时"
    return text


def _ensure_client_id(config: Config) -> str:
    """A stable identity so we can be recognised across restarts."""
    import uuid

    cid = config.get("client_id")
    if not cid:
        cid = uuid.uuid4().hex
        config["client_id"] = cid
    return str(cid)


class ViewerClient:
    def __init__(
        self,
        host: str,
        port: int = P.DEFAULT_AGENT_PORT,
        *,
        client_id: str | None = None,
        client_name: str | None = None,
        code: str = "",
        probe_only: bool = False,
        on_frame: FrameHook | None = None,
        on_event: EventHook | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.config = Config("viewer")
        self.client_id = client_id or _ensure_client_id(self.config)
        self.client_name = client_name or platform.node() or "viewer"
        self.code = code
        #: 只验 IP+验证码，不建立会话。见 connect()。
        self.probe_only = bool(probe_only)
        self.on_frame = on_frame or (lambda _f, _t: None)
        self.on_event = on_event or (lambda _n, _d: None)

        self.screen_w = 0
        self.screen_h = 0
        self.region = (0, 0, 1, 1)
        self.remote_fps = 30
        self.fingerprint = ""
        # 只有 connect() 读到 CTRL_GRANT 才会填这两个，但它们到处都可能被读到
        # （面板、窗口、宿主的自定义代码），不能在 connect() 之前缺席。
        self.monitors: list[tuple[int, int, int, int]] = []
        self.monitor_index = 0

        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        # 出站队列：调用方（尤其是 GUI 线程的键鼠/切屏/剪贴板）只入队即返回，
        # 真正写 socket 的只有 _send_loop 一条线程。同步 sendall 曾把 GUI 线程
        # 卡死：对端一慢就永远等在写上。队列有界 —— 真堵死了丢包，不堵人。
        self._outbox: queue.Queue[bytes] = queue.Queue(maxsize=512)
        self._writer: threading.Thread | None = None
        self._reader: threading.Thread | None = None
        self._keepalive: threading.Thread | None = None
        self._stop = threading.Event()
        self._end_lock = threading.Lock()
        self._ended = False
        self._closed_by_us = False
        self._asked_to_auth = False
        self._clip_local = ""   # 我们写入本机剪贴板的内容（防回声）
        self._clip_sent = ""    # 我们推给对端的内容
        self._clip_recv_at = 0.0  # 最近一次"收到对端剪贴板并写入"的时刻（时间窗防回声）
        self._last_rx = 0.0
        self._ending = False    # 对端给了终局答复（REJECT），该收工了
        self._decode = create_decoder()
        self.seq = 0
        self.frames = 0
        self.dropped = 0
        self.warmup = 0  # 关键帧之前跳过的 P 帧数（"重连后多久有画面"）

    # -- connect -----------------------------------------------------------
    def connect(self, timeout: float = 10.0) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            sock = viewer_tls_context().wrap_socket(raw, server_hostname=self.host)
        except (OSError, ssl.SSLError):
            raw.close()
            raise

        # TOFU pinning: trust on first contact, treat a changed fingerprint
        # afterwards as someone sitting in the middle.
        # 清理必须连着这个 try：_check_fingerprint() 抛异常时 self._sock 还没
        # 赋值，下面的异常处理够不着它，TLS 会话就这么挂在进程里（每个被拒的
        # 证书泄漏一个 fd + 收发缓冲区）。
        try:
            fp = peer_certificate_fingerprint(sock)
            self._check_fingerprint(fp)
        except BaseException:
            try:
                sock.close()
            except OSError:
                pass
            raise
        self.fingerprint = fp
        self._sock = sock
        self._asked_to_auth = False

        def send_now(payload: bytes) -> None:
            """Handshake writes must fail loudly; only runtime sends are quiet."""
            with self._send_lock:
                P.send_packet(sock, payload)

        try:
            send_now(
                P.pack_msg(
                    P.Msg.HELLO,
                    {"client_id": self.client_id, "client_name": self.client_name},
                )
            )
            opening = P.recv_packet(sock)
            # 被控端可能在【打招呼阶段】就拒人（例如已关闭"允许被控"），那时它
            # 发的是拒绝包而不是 HELLO 应答。不识别的话我们会把它当成 HELLO 应答
            # 吞掉，接着发 AUTH_REQ、读到 EOF，于是把明确的"对方已关闭远程控制权限"
            # 说成了 "peer closed the connection"。注意 REJECT 包里没有 ok 字段。
            if opening.type == P.Msg.REJECT:
                reason = opening.json().get("reason", P.Reject.DENIED)
                self._abort("rejected")
                raise ConnectionError_(P.reject_text(reason), permanent=_reject_is_terminal(reason))
            if opening.type == P.Msg.AUTH_RESP and not opening.json().get("ok", True):
                reason = opening.json().get("reason", P.Reject.DENIED)
                self._abort("rejected")
                raise ConnectionError_(P.reject_text(reason), permanent=_reject_is_terminal(reason))
            hello = opening.json()
            log.debug("对端握手信息：%s", hello)

            self._asked_to_auth = True
            send_now(
                P.pack_msg(
                    P.Msg.AUTH_REQ,
                    {
                        "client_id": self.client_id,
                        "client_name": self.client_name,
                        "pair_code": self.code or "",
                        # 探测握手：对端验完验证码就收工，不建会话、不推流、
                        # 不占它唯一的会话槽。
                        "probe": self.probe_only,
                    },
                )
            )
            resp = P.recv_packet(sock).json()
            if not resp.get("ok"):
                reason = resp.get("reason", P.Reject.AUTH_FAILED)
                self._abort("rejected")
                # 认得出"验证码不正确"才能说人话（"被远程对象已更换验证码"）。
                # 认不出的 reason 也要有话说：原因是对端给的，见 P.reject_text。
                if P.reject_code(reason) == int(P.Reject.AUTH_FAILED):
                    raise RemoteCodeChanged("被远程对象已更换验证码")
                raise ConnectionError_(P.reject_text(reason), permanent=_reject_is_terminal(reason))

            if self.probe_only:
                # 到这里说明 IP 和验证码都对。探测只需要这个答案，所以既不读
                # CTRL_GRANT 也不起读线程/心跳线程。完整握手来验一个验证码是有
                # 代价的：被控端会在那一刻建起一个完整权限的会话（接受键鼠注入、
                # 开始推流、占住唯一的会话槽），而真正的会话窗口还没打开 ——
                # 紧接着的正式连接就会撞上"已被别人控制"。
                self._release(send_bye=False)
                log.info("验证码校验通过：%s:%d", self.host, self.port)
                return

            opening = P.recv_packet(sock)
            if opening.type == P.Msg.REJECT:
                reason = opening.json().get("reason", P.Reject.DENIED)
                self._abort("rejected")
                raise ConnectionError_(P.reject_text(reason), permanent=_reject_is_terminal(reason))
            if opening.type != P.Msg.CTRL_GRANT:
                self._abort()
                raise ConnectionError_(f"握手异常：收到消息类型 {opening.type}")

            grant = opening.json()
            self.screen_w = int(grant.get("w", 0))
            self.screen_h = int(grant.get("h", 0))
            self.region = tuple(grant.get("region") or (0, 0, self.screen_w, self.screen_h))
            self.remote_fps = int(grant.get("fps", 30))
            #: 被控端所有显示器的矩形（虚拟桌面坐标），下标 0 = 显示器 1。
            self.monitors: list[tuple[int, int, int, int]] = [
                tuple(m) for m in (grant.get("monitors") or [[0, 0, self.screen_w, self.screen_h]])
            ]
            #: 当前正在推流的是第几个显示器（0 起）。
            self.monitor_index: int = int(grant.get("monitor", 0))
        except ConnectionError_:
            self._abort("rejected")
            raise
        except Exception as exc:  # noqa: BLE001
            # 一旦已经请求过认证，答复就是终局：要么放行要么拒绝。注意
            # ConnectionError（"peer closed the connection"）是 OSError 的子类，
            # 朴素的"网络抖动"处理会把拒绝当瞬时错误，然后永远重试。
            self._abort("handshake failed")
            raise ConnectionError_(
                f"连接失败：{exc}", permanent=self._asked_to_auth
            ) from exc

        self._stop.clear()
        self._ended = False
        self._ending = False
        self._last_rx = time.monotonic()
        sock.settimeout(RECV_TIMEOUT_S)
        self._writer = threading.Thread(target=self._send_loop, name="lrdp-writer", daemon=True)
        self._writer.start()
        self._reader = threading.Thread(target=self._read_loop, name="lrdp-reader", daemon=True)
        self._reader.start()
        self._keepalive = threading.Thread(
            target=self._keepalive_loop, name="lrdp-keepalive", daemon=True
        )
        self._keepalive.start()
        self._emit_event(
            "session_started", {"w": self.screen_w, "h": self.screen_h, "fps": self.remote_fps}
        )

    def close(self, reason: str = "closed") -> None:
        """Stop the session. *reason* is logged so the caller is identifiable."""
        self._stop.set()
        self._closed_by_us = True
        self._release(send_bye=True)
        for thread in (self._reader, self._writer, self._keepalive):
            if thread is not None:
                thread.join(timeout=2.0)
        # 原因优先，调用栈只在它能说明问题的时候才打：close() 有好几条路径，
        # 有意收尾时栈只是复述调用现场，看着像崩溃。"对方已断开连接"是最常见的
        # 正常收尾，它每次都从 _teardown 进来，栈一模一样、没有诊断价值。
        if reason in ("断开", "已断开", "closed", "对方已断开连接"):
            log.info("控制端已关闭会话：%s", reason)
        else:
            log.info(
                "控制端已关闭会话：%s\n调用栈：\n%s",
                reason,
                "".join(traceback.format_stack()[-8:]),
            )
        self._emit_ended(reason)

    def _release(self, *, send_bye: bool) -> None:
        """Hand the socket back.  Idempotent.

        ``send_bye`` is False for a completed probe: the far side has already
        closed, so a BYE would only log a failed write.
        """
        sock, self._sock = self._sock, None
        if sock is None:
            return
        if send_bye:
            # 先在活的 socket 上发 BYE。在那之前清空 self._sock 会让 _send()
            # 变成空操作，对端只看到 EOF、报成 "peer closed the connection" 而不是
            # "viewer said goodbye"，日志就读不懂了。
            # 限时发送：发送缓冲被塞满时 sendall 会永远等下去。0.5 秒发不出去就
            # 当没发，对端看到 EOF 一样收工。
            bye_ok = True
            try:
                sock.settimeout(0.5)
                with self._send_lock:
                    P.send_packet(sock, P.pack_msg(P.Msg.BYE, {"reason": "closed"}))
            except (OSError, ConnectionError):
                bye_ok = False
            finally:
                # 超时是**整个 socket 的**：留在 0.5 s 不还，读线程之后每一次
                # recv_packet 都只剩 0.5 s，一次慢包就变成 "timed out mid-packet"。
                try:
                    sock.settimeout(RECV_TIMEOUT_S)
                except OSError:
                    pass
            if bye_ok:
                # 等对端把会话槽放掉，紧接着的重连才不会撞上"对方已被别人控制"。
                log.info("已发 BYE，等对端收尾")
                self._wait_for_peer_close(sock)
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _wait_for_peer_close(self, sock: socket.socket) -> None:
        """发完 BYE 等对端把会话槽放掉，但**绝不自己再去 recv**。

        对端收到 BYE 才释放它的会话槽，所以要等一会；但等的方式只能是等**读
        线程**退出 —— 它退出就意味着对端已经关了连接（EOF）。自己再开一路 recv
        的话，同一个 socket 上就有两个读者瓜分字节流（SSLSocket 上并发 recv
        还不线程安全），而且顺手把超时改成 0.3 s 会连读线程下一次 recv 的余量
        一起偷走。

        必须限时：对端正在推流时 recv 永远有数据，无限等就是"关闭软件卡死"。
        """
        reader = self._reader
        if reader is not None and reader.is_alive():
            reader.join(timeout=1.5)
            return
        # 还没起读线程（探测握手、建连中途失败）：这一路没人跟我们抢，自己排空
        # 到对端关闭，才不会把半开连接留给调用方。
        try:
            sock.settimeout(0.3)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and sock.recv(4096):
                pass
        except (OSError, ConnectionError):
            pass
        finally:
            try:
                sock.settimeout(RECV_TIMEOUT_S)
            except OSError:
                pass

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._stop.is_set()

    # -- outbound input ----------------------------------------------------
    def send_mouse(self, action: int, button: int, x_ratio: float, y_ratio: float) -> None:
        self._send(
            P.HEADER.pack(P.MAGIC, P.PROTO_VERSION, P.Msg.MOUSE, 0, P.MOUSE_BODY.size, 0)
            + P.MOUSE_BODY.pack(action, button, x_ratio, y_ratio, 0, 0)
        )

    def send_wheel(self, delta: int, horizontal: bool = False) -> None:
        self._send(
            P.HEADER.pack(P.MAGIC, P.PROTO_VERSION, P.Msg.WHEEL, 0, P.WHEEL_BODY.size, 0)
            + P.WHEEL_BODY.pack(delta, 1 if horizontal else 0)
        )

    def send_key(self, action: int, vk: int, scan: int, ext: bool = False) -> None:
        flags = P.KEYF_EXTENDED if ext else 0
        self._send(
            P.HEADER.pack(P.MAGIC, P.PROTO_VERSION, P.Msg.KEY, 0, P.KEY_BODY.size, 0)
            + P.KEY_BODY.pack(action, vk, scan, flags, 0)
        )

    def send_clipboard(self, text: str) -> None:
        """把本机剪贴板文本推给对端，实现「这边复制、那边粘贴」。"""
        if not text or len(text) > P.MAX_CLIPBOARD_CHARS:
            return
        self._clip_sent = text
        self._send(P.pack_msg(P.Msg.CLIPBOARD, {"text": text}))

    def set_monitor(self, index: int) -> None:
        """切换被控端正在推流的显示器（下标 0 = 显示器 1）。

        本地立即更新 ``monitor_index`` 让界面响应跟手；真值以对端的
        ``monitor`` 事件为准，被拒时那里会把它改回来。
        """
        self.monitor_index = int(index)
        self._send(P.pack_msg(P.Msg.SET_MONITOR, {"index": int(index)}))

    # -- internals ---------------------------------------------------------
    def _check_fingerprint(self, fingerprint: str) -> None:
        stored = self.config.get(f"fp:{self.host}:{self.port}")
        if not stored:
            self.config[f"fp:{self.host}:{self.port}"] = fingerprint
        elif stored != fingerprint:
            raise RemoteFingerprintChanged("远端证书指纹已改变，可能存在中间人，已拒绝连接")

    def _abort(self, why: str = "aborted") -> None:
        """Close the socket from this side.

        必须置 ``_closed_by_us``：关掉 socket 会让我们自己的 recv 也返回 EOF，
        没有这个标志它就会报 "peer closed" 并把一个自己要求的关闭怪到对端头上。
        """
        self._closed_by_us = True
        log.info("中止会话：%s\n调用栈：\n%s", why, "".join(traceback.format_stack()[-8:]))
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _send(self, data: bytes) -> None:
        """入队即返回，绝不阻塞调用方。

        这里会被 GUI 线程的 Qt 槽（鼠标/按键事件）走到。同步 sendall 是"界面
        概率性卡死"的根因：网络或对端一慢，调用线程就永远等在写上。真正的 socket
        写统一由 _send_loop 做（也就天然保住了包序），队列满则丢弃并告警：宁可丢
        一个出站包，也不堵死人机界面。
        """
        try:
            self._outbox.put_nowait(data)
        except queue.Full:
            log.warning("发送队列已满（网络堵塞？），丢弃一个出站包")

    def _send_loop(self) -> None:
        """唯一碰 socket 写的线程：把队列里的包按序发出去。

        失败只记日志、不结束会话 —— 断开与否由读线程判定（发送失败就拆会话曾把
        "编码抖动"误报成"对方断开"）。
        """
        while not self._stop.is_set():
            try:
                data = self._outbox.get(timeout=0.5)
            except queue.Empty:
                continue
            sock = self._sock
            if sock is None:
                continue
            pending = self._outbox.qsize()
            if pending > 64:
                # 诊断：输入失效而画面正常 = 上行发不出去。队列积压是最早的可见
                # 征兆（对端不读、或 sendall 阻塞），先于丢包。
                log.warning("发送队列积压 %d 个包（对端读不动？）", pending)
            t0 = time.monotonic()
            try:
                with self._send_lock:
                    P.send_packet(sock, data)
            except (OSError, ConnectionError):
                log.debug("发送失败，断开交由读线程报告", exc_info=True)
            elapsed = time.monotonic() - t0
            if elapsed > 1.0:
                log.warning("一个出站包发送耗时 %.1f 秒（网络/对端阻塞）", elapsed)

    def _keepalive_loop(self) -> None:
        """Keep the link warm and notice a dead peer instead of hanging."""
        while not self._stop.wait(KEEPALIVE_S):
            try:
                self._send(P.pack_msg(P.Msg.PING, {"ts": P.now_us()}))
            except (OSError, ConnectionError):
                return
            if time.monotonic() - self._last_rx > DEAD_S:
                log.warning("%.0f 秒未收到 %s 的数据", DEAD_S, self.host)
                self._abort("keepalive watchdog: no traffic")
                self._emit_ended("连接超时")
                return

    def _read_loop(self) -> None:
        sock = self._sock
        assert sock is not None
        reason = "stream closed"
        try:
            while not self._stop.is_set():
                try:
                    pkt = P.recv_packet(sock)
                except socket.timeout:
                    # 单次超时本身不致命。采集卡住或链路安静一阵不该赔上整个
                    # 会话 —— 该由 keepalive 看门狗来判定对端是否还在。
                    if time.monotonic() - self._last_rx > DEAD_S:
                        reason = "连接超时"
                        log.warning("%.0f 秒未收到 %s 的数据", DEAD_S, self.host)
                        break
                    continue
                self._last_rx = time.monotonic()
                self._dispatch(pkt)
                if self._ending:
                    reason = "remote side rejected"
                    break
        except (ConnectionError, OSError) as exc:
            reason = _friendly_reason(exc)
            if self._closed_by_us:
                log.info("流已本地关闭（我们主动关闭）")
            elif "peer closed" in str(exc).lower():
                # 对端正常关闭时的预期路径，栈只会指回这里。附上异常类型：干净
                # FIN（对端或链路中间设备正常关闭）与 RST、超时必须一眼可分 ——
                # 跨机对日志时这是唯一能分辨"谁先断"的线索。
                log.info("流已结束：%s（%s）", exc, type(exc).__name__)
            else:
                log.info("流异常结束：%s\n调用栈：\n%s", exc, "".join(traceback.format_stack()[-6:]))
        finally:
            self._stop.set()
            if self._closed_by_us:
                # shutdown() 让我们自己的 recv 也返回 EOF，在这里记 "peer closed"
                # 就是把一个自己要求的关闭怪到对端头上。真实原因由 close() 报。
                return
            # 关键数字：0 表示什么都没收到（被控端没在产，或者包全被丢了），
            # 正数表示流是好的、问题在渲染。
            log.info(
                "会话结束：共收到 %d 帧（丢弃 %d，等关键帧跳过 %d），原因=%s",
                self.frames,
                self.dropped,
                self.warmup,
                reason,
            )
            self._emit_ended(reason)

    # -- inbound dispatch --------------------------------------------------
    def _dispatch(self, pkt: P.Packet) -> None:
        """Handle one inbound packet, never letting a bad one end the stream.

        每个包都可能抛异常（拆帧头、解 JSON、调 on_frame）。没有这道守卫时一个
        坏包会用裸 traceback 杀掉读线程，而原因随后被报成 "stream closed" ——
        一个坏包看起来和"对端消失"完全一样。

        (ConnectionError, OSError) 是控制流而非损坏 —— 它们要结束会话，必须
        传到 _read_loop。
        """
        try:
            self._dispatch_inner(pkt)
        except (ConnectionError, OSError):
            raise
        except Exception:  # noqa: BLE001
            self.dropped += 1
            log.warning(
                "忽略无法解析的入站包 type=%s len=%d（累计丢弃 %d）",
                pkt.type,
                len(pkt.payload),
                self.dropped,
            )

    def _dispatch_inner(self, pkt: P.Packet) -> None:
        if pkt.type == P.Msg.FRAME:
            _w, _h, codec, ts, blob = pkt.frame()
            try:
                rgb = self._decode.decode(codec, blob)
            except Exception:  # noqa: BLE001 - one bad frame must not kill the stream
                self.dropped += 1
                log.debug("丢弃损坏帧（累计 %d）", self.dropped)
                return
            if rgb is None:
                # 空载荷，或关键帧之前/重同步中的 P 帧。分开计数：会话刚建立时
                # 跳过多少帧，直接回答"重连后多久才有画面"。
                if blob:
                    self.warmup += 1
                    if self.warmup in (1, 60):
                        log.info("等待关键帧，已跳过 %d 帧（重连首帧的代价）", self.warmup)
                return  # h264 预热或重新同步中
            self.frames += 1
            if self.frames == 1:
                log.info("收到首帧 %dx%d", rgb.shape[1], rgb.shape[0])
            elif self.frames % 300 == 0:
                log.info("已收到 %d 帧", self.frames)
            self.on_frame(rgb, ts)
        elif pkt.type == P.Msg.PONG:
            # 对端回显的是我们 PING 里的 ts：减一下就是 RTT，时钟差在减法里抵消。
            rtt_ms: float | None = None
            try:
                ts = pkt.json().get("ts")
                if ts is not None:
                    rtt_ms = max(0.0, (P.now_us() - int(ts)) / 1000.0)
            except (AttributeError, TypeError, ValueError):
                rtt_ms = None
            self._emit_event("pong", {"rtt_ms": rtt_ms})
        elif pkt.type == P.Msg.STATS:
            self._emit_event("stats", pkt.json())
        elif pkt.type == P.Msg.CLIPBOARD:
            # 对端复制、本机粘贴。
            text = str(pkt.json().get("text", ""))[: P.MAX_CLIPBOARD_CHARS]
            if text and text != self._clip_local:
                self._clip_local = text
                # 时间窗防回声：写入会触发本机 dataChanged，处理程序靠这个时间戳
                # 认出"这是我刚写进去的"，不再回发。只比文本不够 —— Windows 会
                # 规范化内容，来回一圈文本就对不上，两台机器会在各自的会话里环形
                # 乒乓。
                self._clip_recv_at = time.monotonic()
                from ..Recommon import clipboard

                clipboard.try_set_text(text)
        elif pkt.type == P.Msg.MONITOR_STATE:
            # 被控端对切屏请求的确认。ok=False 时 index 是它*当前*那块，
            # 调用方要据此把高亮拨回去，而不是停在刚点亮的那块上。
            ack = pkt.json()
            try:
                self.monitor_index = int(ack.get("index", self.monitor_index))
            except (TypeError, ValueError):
                pass
            self._emit_event("monitor", ack)
        elif pkt.type == P.Msg.REJECT:
            self._emit_event("rejected", pkt.json())
            # REJECT is a terminal answer: stop reading instead of falling
            # through to a blocking recv that would never come back.
            self._ending = True

    # -- event emission ----------------------------------------------------
    def _emit_event(self, name: str, data: dict) -> None:
        try:
            self.on_event(name, data)
        except RuntimeError:
            # 窗口/宿主已经销毁（Qt 包装对象已删）。退出/关窗时事件还会飞一会儿，
            # 这不是错误，降为 debug 免得刷屏。
            log.debug("事件回调目标已销毁：%s", name)
        except Exception:  # noqa: BLE001 - a hook must not kill the reader thread
            log.exception("事件回调失败：%s", name)

    def _emit_ended(self, reason: str) -> None:
        """Report session end exactly once, however we got here.

        ``client`` 就是本实例：窗口必须能分辨"已死会话的迟到事件"和"活会话的
        事件" —— 照单全收迟到事件会把新会话的输入 sink 拆掉。
        """
        with self._end_lock:
            if self._ended:
                return
            self._ended = True
        self._emit_event("session_ended", {"reason": reason, "client": self})
