"""The controlled side: accept one viewer, authenticate it, stream the screen.

Pipeline (see README "线程模型")::

    capture thread --put--> FrameSlot --take--> sender thread --> socket
                                          \
                                           recv thread --> InputInjector

The single-slot ``FrameSlot`` is what keeps latency flat: it holds exactly one
frame and overwrites it, so a slow encode or a congested link drops frames
instead of queuing them up behind you.
"""

from __future__ import annotations

import logging
import platform
import select
import socket
import ssl
import threading
import time
import traceback
from typing import Callable

from .capture import ScreenCapture, create_capture, list_monitors, region_looks_bright
from .injector import InputInjector
from ..Recommon import protocol as P
from ..Recommon import clipboard
from ..Recommon.codec import FrameEncoder, create_encoder
from ..Recommon.config import Config, disable_high_resolution_timers, enable_high_resolution_timers
from ..Recommon.framebus import FrameSlot
from ..Recommon.pairing import PairingCode
from ..Recommon.security import agent_tls_context

log = logging.getLogger(__name__)

EventHook = Callable[[str, dict], None]

#: 会话 socket 在 ``select`` 上等多久再看有没有数据。只影响读方向的检查节奏：
#: 不用 socket 超时是因为 ``settimeout`` 会同时管住 ``sendall``，推流时一次
#: 慢写（链路拥塞）就会被误判成断链，把发送线程干掉。
SESSION_POLL_S = 1.0

#: 对端连续这么久不说话就判定它没了。控制端每 5 秒发一次 PING，所以 30 秒是
#: 连丢 6 次心跳 —— 足够宽松，正常的网络抖动不会误杀。
SESSION_SILENCE_S = 30.0

#: 连续多少次发送失败才认定连接真的坏了、停止推流。一次瞬时错误不该杀掉推流，
#: 但连续失败还坚持重试只是拖着死连接。
MAX_CONSECUTIVE_SEND_FAILURES = 8

#: 会话结束后多久才真正暂停采集。刚断就重连是常态，每次重连都 pause()/resume()
#: 等于把采集后端（DXGI 桌面复制）整个重建一遍。空闲的应用照样会暂停，只是晚
#: 10 秒。
CAPTURE_LINGER_S = 10.0


def _socket_pending(sock: socket.socket) -> int:
    """已解密、还躺在 SSL 内部缓冲里的字节数；裸 socket 恒为 0。

    ``select`` 只看内核收缓冲，而 TLS 会把后续记录先解密切好放进自己的内部
    缓冲，此时内核缓冲已空 —— select 就漏掉了这些数据。轮询前先看这里，有就
    直接取，不进 select。
    """
    pending = getattr(sock, "pending", None)
    if pending is None:
        return 0
    try:
        return int(pending())
    except (OSError, ValueError):
        return 0


#: 分辨率刻意钉在 100%（见 AdaptiveQuality）。降像素会让远程协助屏幕上的文字
#: 没法读，比掉帧更糟 —— 所以唯一可调的是 JPEG 质量。
SCALES = (1.0,)


class AdaptiveQuality:
    """Chase a *time* budget via JPEG quality alone.

    A byte budget only reacts to how complex the picture is.  What actually
    hurts is spending longer on encode+send than one frame interval: then
    frames pile up behind the socket and latency runs away.  So the knob is
    driven by measured cost against the frame interval.

    Resolution is held at 100% on purpose.  The consequence is that on a
    genuinely constrained link the frame *rate* falls instead - which is the
    right trade here, since a slow-but-legible screen is usable and a fast
    blurry one is not.
    """

    def __init__(self, interval_s: float, low: int = 45, high: int = 85) -> None:
        self.budget_ms = interval_s * 1000.0
        self.low, self.high = low, high
        self.quality = 70
        self.cost_ms = 0.0

    @property
    def scale(self) -> float:
        return SCALES[0]

    @property
    def scale_pct(self) -> int:
        return 100

    def update(self, cost_ms: float, _size: int = 0) -> int:
        """``cost_ms`` is encode + send time for the frame just delivered.

        H.264 的码率只在开流时定（见 H264Encoder._open），所以这里的返回值只在
        下一次开流（新会话、尺寸变化）时才真正生效；JPEG 回退路径才是逐帧生效。
        对外报数请用 encoder.effective_quality（生效值），别报计算值。
        """
        # 慢 EMA + 10% 余量：一帧慢不该把画质拉下来，只有持续过载才降。
        self.cost_ms = self.cost_ms * 0.85 + cost_ms * 0.15
        if self.cost_ms > self.budget_ms * 1.1 and self.quality > self.low:
            self.quality -= 2
        elif self.cost_ms < self.budget_ms * 0.5 and self.quality < self.high:
            self.quality += 1
        self.quality = max(self.low, min(self.high, self.quality))
        return self.quality


class _Session(threading.Thread):
    """One authenticated viewer."""

    def __init__(
        self,
        sock: socket.socket,
        addr: tuple[str, int],
        client_id: str,
        client_name: str,
        service: "AgentService",
        injector: InputInjector,
    ) -> None:
        super().__init__(name=f"lrdp-session-{addr[0]}", daemon=True)
        self.sock = sock
        self.addr = addr
        self.client_id = client_id
        self.client_name = client_name
        self.service = service
        self.injector = injector
        self._send_lock = threading.Lock()
        # 刻意不叫 _stop：threading.Thread 有私有方法 Thread._stop()，CPython 在
        # _wait_for_tstate_lock() 里会调它。把它覆盖成 Event 之后，走那条内部
        # 路径的操作（is_alive()、某些 join 路径）会抛
        # "TypeError: 'Event' object is not callable"。
        self._stopping = threading.Event()
        self._closed_by_us = False
        self._clip_sent = ""    # 最近推给对端的文本
        self._clip_local = ""   # 我们自己写入的文本（防回声）
        self._clip_pending: str | None = None  # 对端推来、待写入本机的文本（单槽，轮询线程消费）
        self._clip_quiet_until = 0.0  # 写入剪贴板后一段时间内不回推（时间窗防回声）
        self._sender: threading.Thread | None = None
        self.seq = 0
        self.frames_sent = 0
        self.bytes_sent = 0
        #: STATS 里的 kb 只统计两次上报之间发出的帧；终身总计另记（收尾摘要）。
        self._stat_frames = 0
        self._stat_bytes = 0

    # -- lifecycle ---------------------------------------------------------
    def shutdown(self) -> None:
        self._stopping.set()
        self._closed_by_us = True
        # shutdown(SHUT_RDWR) 双向关闭，自己这条 recv 随后就会报"对端关闭"而
        # 怪错了人。调用栈才指出真正发起方。
        log.info("请求关闭会话\n调用栈：\n%s", "".join(traceback.format_stack()[-8:]))
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def run(self) -> None:
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # 发送缓冲开太大就看不到背压：sendall() 立刻返回，而兆字节堆在队列
            # 里，延迟越跑越远、画质控制器也永远学不到链路已经饱和。收到几帧的
            # 量就够让耗时统计诚实。
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
            except OSError:
                pass
            self.injector.set_region(self.service.capture.region)
            # 新流必须从关键帧开始：编码器是全局共享的，否则它会从上一个个会话
            # 的 GOP 继续走，而新解码器手上什么都没有。
            self.service.reset_encoder()
            log.info("会话开始：%s（%s）", self.client_name or "?", self.addr[0])
            self._sender = threading.Thread(
                target=self._send_loop, name=f"lrdp-sender-{self.addr[0]}", daemon=True
            )
            self._sender.start()
            self._clipper = threading.Thread(
                target=self._clipboard_loop, name=f"lrdp-clip-{self.addr[0]}", daemon=True
            )
            self._clipper.start()
            self._recv_loop()
        except (ConnectionError, OSError) as exc:
            if self._closed_by_us:
                log.info("会话 %s 已本地关闭（我们主动关闭）", self.addr[0])
            elif str(exc) == P.Reject.DENIED.text or "said goodbye" in str(exc):
                log.info("会话 %s 结束：%s", self.addr[0], exc)
            else:
                log.info(
                    "会话 %s 异常结束：%s\n调用栈：\n%s",
                    self.addr[0],
                    exc,
                    "".join(traceback.format_stack()[-8:]),
                )
        except Exception:  # noqa: BLE001 - never let a session thread die quietly
            log.exception("会话 %s 崩溃", self.addr[0])
        finally:
            self._stopping.set()
            self.injector.reset()
            # 先 shutdown 再 close：Windows 上 closesocket 不保证唤醒别的线程里
            # 阻塞的 sendall。SHUT_RDWR 会把阻塞中的收发打成错误返回，线程才
            # 收得了工。
            log.info("会话结束，关闭 socket（本会话已发 %d 帧）", self.frames_sent)
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.service._on_session_end(self)
            log.info(
                "会话 %s 已关闭（%d 帧，%.1f MB，客户端=%s）",
                self.addr[0],
                self.frames_sent,
                self.bytes_sent / 1e6,
                self.client_name or "?",
            )

    # -- outbound ----------------------------------------------------------
    def _emit(self, data: bytes) -> None:
        with self._send_lock:
            P.send_packet(self.sock, data)

    def _emit_control(self, data: bytes, tries: int = 1) -> None:
        """Reply to a keepalive without queueing behind a 100 ms frame write.

        慢链路上一次帧写会把锁占住整个写入时间，PONG 排在后面的话读线程也跟着
        停，心跳堆积，一条勉强的链路就变成断线。控制流量很小：试一下，丢就丢。

        ``tries`` 给"丢不起"的回执用（切屏确认）：隔几十毫秒多抢几次锁，且每次
        都是**非阻塞**抢 —— 在这里等锁的是会话的 recv 线程，它一停整个输入通道
        跟着停（控制端的包没人读、发送缓冲被塞满，界面整个卡死）。
        """
        for attempt in range(max(1, tries)):
            if self._send_lock.acquire(blocking=False):
                try:
                    P.send_packet(self.sock, data)
                except (ConnectionError, OSError):
                    pass
                finally:
                    self._send_lock.release()
                return
            if attempt + 1 < tries:
                time.sleep(0.03)

    def _send_loop(self) -> None:
        """Encode and push the newest frame.

        刻意**不**因为发送失败就拆会话：连接死没死是读线程说了算（它才看得到
        EOF）。在这里拆会把发送侧的任何抖动（一次编码错误、一次采集卡顿）都变成
        "对方已断开"，而链路本身好好的。
        """
        try:
            self._send_loop_inner()
        except Exception:  # noqa: BLE001 - keep the session alive, see above
            log.exception("发送线程已停止，会话由收包线程的看门狗收尾")

    def _send_loop_inner(self) -> None:
        cfg = self.service
        interval = 1.0 / max(1, cfg.target_fps)
        quality = AdaptiveQuality(interval)
        stats_at = time.perf_counter() + 2.0
        send_failures = 0  # 连续发送失败计数，见下方 _emit 的 except
        while not self._stopping.is_set():
            # 帧槽被 close() 之后 take() 不再阻塞、直接返回 None（见 FrameSlot）。
            # 服务已经停了却还有会话在跑的话，这个循环就变成**无休眠忙转**。
            if cfg.frames.closed:
                log.info("帧槽已关闭，发送线程退出")
                return
            # take() 本来就阻塞到**下一帧**，节拍由采集循环决定。在这里再 sleep
            # 只会白白多出一份每帧延迟，一个字的吞吐都不增加。
            frame = cfg.frames.take(timeout=0.5)
            if frame is None:
                continue
            if self._stopping.is_set():
                # 会话已收尾，这一帧必须让给下一个会话。编码器是全局共享的，
                # 新会话 reset_encoder() 之后的开场 IDR 只有一次：在这里抢先编码，
                # 就把新流的 pts 0（关键帧）发往了死 socket，新流只能从 P 帧开始，
                # 对端要等下一个 GOP 边界才有画面。take() 可能在 _stopping 置位
                # 之后才醒（阻塞超时 0.5 秒），循环顶上的检查拦不住，所以编码前
                # 必须再查一次。
                return
            # stop() 会把 self.capture 收走（置 None），重建采集也可能短暂为
            # None：拿住局部引用再用，避免戳一个已经被收走的对象。
            cap = cfg.capture
            if cap is not None:
                self.injector.set_region(cap.region)

            t0 = time.perf_counter()
            try:
                blob = cfg.encode(frame, quality.quality)
            except Exception:  # noqa: BLE001
                # PIL 把图像问题也报成 OSError，和坏掉的 socket 撞同一个类型。
                # 编码失败只值一帧，不该把整个会话拆掉。
                log.exception("编码失败，丢弃该帧")
                continue
            encode_ms = (time.perf_counter() - t0) * 1000
            if not blob:
                # 编码器攒参考帧时会吐空包。空帧解码端直接跳过，发出去只污染
                # 双方的计数。
                continue

            self.seq += 1
            h, w = frame.shape[:2]
            t1 = time.perf_counter()
            try:
                self._emit(
                    P.pack_frame(w, h, self.service.encoder_codec, P.now_us(), blob, seq=self.seq)
                )
            except (ConnectionError, OSError):
                # 一次失败不收工：慢链路上的瞬时错误不该杀掉推流。连续失败说明
                # 连接真的坏了 —— 收工退出，由 _recv_loop 的"发送线程已死"看门狗
                # 把会话收掉（PONG 照发，静默检查看不见发送线程死）。
                send_failures += 1
                if send_failures >= MAX_CONSECUTIVE_SEND_FAILURES:
                    log.warning(
                        "连续 %d 次发送失败，停止推流（会话由收包线程收尾）",
                        send_failures,
                    )
                    return
                time.sleep(min(0.5, 0.05 * send_failures))
                continue
            send_failures = 0
            send_ms = (time.perf_counter() - t1) * 1000
            cost_ms = encode_ms + send_ms

            self.frames_sent += 1
            self.bytes_sent += len(blob)
            self._stat_frames += 1
            self._stat_bytes += len(blob)
            quality.update(cost_ms)
            # 故意用 INFO：帧率一低，DEBUG 就把判断"编码慢还是链路慢"需要的
            # 那几个数藏掉了。编码与发送分开报，因为两者的修法不同：编码慢是
            # CPU，发送慢是网络。
            if self.seq % 30 == 0:
                log.info(
                    "帧#%d %dx%d q=%d %.0fKB 编码=%.1fms 发送=%.1fms（%.1f Mbit/s） 预算=%.1fms（生效 q=%d）",
                    self.seq,
                    w,
                    h,
                    quality.quality,
                    len(blob) / 1024,
                    encode_ms,
                    send_ms,
                    len(blob) * 8 / max(send_ms, 0.001) / 1000,
                    quality.budget_ms,
                    self.service.encoder_effective_quality or quality.quality,
                )
            if time.perf_counter() >= stats_at:
                stats_at = time.perf_counter() + 2.0
                self._emit_stats(quality)

    def _emit_stats(self, quality: AdaptiveQuality) -> None:
        """Let the controlling side show *why* it is slow instead of guessing."""
        # kb 只算这一轮上报之间发出的帧：终身均值在几百帧之后几乎不再变化，
        # 回答不了"现在一帧多大"——而那正是想知道"为什么卡"的人要看的数。
        frames, self._stat_frames = self._stat_frames, 0
        data, self._stat_bytes = self._stat_bytes, 0
        try:
            self._emit_control(
                P.pack_msg(
                    P.Msg.STATS,
                    {
                        "q": quality.quality,
                        # 计算值与生效值分开报：H.264 会话内 q 再怎么变也进不了
                        # 已开的编码上下文。
                        "q_eff": self.service.encoder_effective_quality,
                        "scale": quality.scale_pct,
                        "cost_ms": round(quality.cost_ms, 1),
                        "budget_ms": round(quality.budget_ms, 1),
                        "kb": round(data / max(1, frames) / 1024, 1),
                    },
                )
            )
        except (ConnectionError, OSError):
            pass

    def _clipboard_loop(self) -> None:
        """把本机剪贴板的变化推给控制端（对方复制、我这边粘贴的反向）。

        轮询而非事件：被控端不依赖 Qt，拿不到剪贴板变化通知。
        """
        while not self._stopping.wait(0.8):
            # 先把对端推来的内容写进本机剪贴板：写剪贴板在这个线程做，绝不占用
            # 会话的 recv 线程（见 _dispatch_inner 的 CLIPBOARD）。
            pending, self._clip_pending = self._clip_pending, None
            if pending:
                ok = clipboard.try_set_text(pending)
                log.info("剪贴板写入结果=%s", ok)
                # 写入会触发本机剪贴板回读 → 又推回对端的环形回声。文本比较是
                # 第一道（记 _clip_local），时间窗是第二道：写入后一小段时间内
                # 一律不推（Windows 会规范化内容，来回一圈文本就对不上了）。
                self._clip_quiet_until = time.monotonic() + 2.0
            try:
                text = clipboard.get_text()
            except Exception:  # noqa: BLE001
                continue

            # 文本优先：剪贴板一次只有一类内容
            if (
                text
                and text != self._clip_sent
                and text != self._clip_local
                and time.monotonic() >= self._clip_quiet_until
            ):
                if len(text) <= P.MAX_CLIPBOARD_CHARS:
                    try:
                        # 阻塞发送：剪贴板内容不能丢。_emit_control 拿不到锁就丢弃，
                        # 而推流时 _send_lock 常被帧占用，消息会被静默吞掉。
                        self._emit(P.pack_msg(P.Msg.CLIPBOARD, {"text": text}))
                    except (ConnectionError, OSError):
                        # 发送失败不收工：不记 _clip_sent，下一轮自动重试；连接
                        # 真的坏了由 _recv_loop 收尾。
                        continue
                    self._clip_sent = text
                continue

    # -- inbound -----------------------------------------------------------
    @staticmethod
    def _monitor_index(pkt: P.Packet) -> int:
        """Read the requested monitor out of a SET_MONITOR packet.

        载荷来自线上，不一定是数字：坏值必须变成一个越界请求交给 ``_set_monitor``
        拒绝，而不是抛异常把会话带走。
        """
        try:
            return int(pkt.json().get("index", 0))
        except (TypeError, ValueError):
            return -1

    @staticmethod
    def _ping_ts(pkt: P.Packet) -> int:
        """PING 里的时间戳，原样回显给对端算 RTT。

        不回显对端就算不了：两端时钟不同步，只有把它自己的 ts 送回去，减法才
        抵消时钟差。解析不出来就用本机钟兜底。
        """
        try:
            ts = pkt.json().get("ts")
            return int(ts) if ts is not None else P.now_us()
        except (AttributeError, TypeError, ValueError):
            return P.now_us()

    def _recv_loop(self) -> None:
        """Read input until the peer goes away - including by *going quiet*.

        拔网线、防火墙静默丢包都不会带来 FIN 或 RST，所以 ``recv`` 会一直挂着。
        而发送线程**故意**不因为一次发送失败就拆会话（见 ``_send_loop``），光靠
        它没人会去结束它：会话槽被永久占死（之后所有连接都是 BUSY），采集也继续
        为一个没人看的屏幕满速跑。所以静默看门狗和"发送线程已死"看门狗都放在
        这里 —— 判定会话生死的人只能是它。

        用 ``select`` 定期醒来查静默时长，而不是给 socket 设超时：后者会连
        ``sendall`` 一起管住（见 ``SESSION_POLL_S``）。
        """
        silence_s = self.service.session_silence_s
        last_rx = time.monotonic()
        while not self._stopping.is_set():
            # 发送线程死了（连续发送失败收工、或抛了意外异常）就没人再推帧。
            # PONG 走 _emit_control 照发不误，控制端每 5 秒的 PING 也照常被收到
            # —— 两边的静默看门狗都被它续命，会话会变成"画面永久冻结但显示已连接"
            # 的僵尸，永不自愈。在这里收尾，run() 的 finally 会把会话槽还回去。
            sender = self._sender
            if sender is not None and not sender.is_alive():
                raise ConnectionError("sender thread is gone")
            # select 只看内核收缓冲；TLS 已经解密进 SSL 缓冲的数据它看不见
            # （见 _socket_pending）。有 pending 就直接取、不进 select。
            if not _socket_pending(self.sock):
                try:
                    ready, _, _ = select.select([self.sock], [], [], SESSION_POLL_S)
                except (OSError, ValueError):
                    # 套接字被关掉了（stop() / shutdown 之后）：当作对端已走。
                    raise ConnectionError("session socket is gone")
                if not ready:
                    silent_for = time.monotonic() - last_rx
                    if silent_for > silence_s:
                        log.warning(
                            "对端已 %.0f 秒没有任何数据（心跳也没了），结束会话",
                            silent_for,
                        )
                        raise ConnectionError("viewer went silent")
                    continue
            pkt = P.recv_packet(self.sock)
            last_rx = time.monotonic()
            self._dispatch(pkt)
    def _dispatch(self, pkt: P.Packet) -> None:
        try:
            self._dispatch_inner(pkt)
        except (ConnectionError, OSError):
            # Control flow, not damage: "viewer said goodbye" and dead links
            # are raised from here and must reach run(), which ends the session.
            raise
        except Exception:  # noqa: BLE001
            # 畸形或意外的输入包不该把会话一起带走（短载荷上的 struct.error
            # 会杀掉整条连接）。
            log.exception("忽略无法解析的输入包 type=%s len=%d", pkt.type, len(pkt.payload))

    def _dispatch_inner(self, pkt: P.Packet) -> None:
        t = pkt.type
        if t == P.Msg.MOUSE:
            action, button, xr, yr, _dx, _dy = P.MOUSE_BODY.unpack(pkt.payload)
            self.injector.mouse(action, button, xr, yr)
        elif t == P.Msg.KEY:
            action, vk, scan, flags, _mods = P.KEY_BODY.unpack(pkt.payload)
            if getattr(self, "_key_logs", 0) < 10:
                self._key_logs = getattr(self, "_key_logs", 0) + 1
                log.info("收到按键 action=%s vk=%s scan=%s flags=%s", action, vk, scan, flags)
            self.injector.key(action, vk, scan, ext=bool(flags & P.KEYF_EXTENDED))
        elif t == P.Msg.WHEEL:
            delta, horizontal = P.WHEEL_BODY.unpack(pkt.payload)
            self.injector.wheel(delta, bool(horizontal))
        elif t == P.Msg.SET_MONITOR:
            # 回执必须真的发回去。请求方是先点亮按钮、再发这个包的；不回执也不
            # 校验上界的话，{"index":99} 之后按钮一直停在第 100 块屏上，而被控端
            # 实际在抓另一块（甚至整张虚拟桌面）。
            ack = self.service._set_monitor(self._monitor_index(pkt))
            # 非阻塞多抢几次，而不是排队等一帧发完：排队等的人是 recv 线程，
            # 它一停控制端的输入包就没人读、发送缓冲被塞满，那边界面整个卡死。
            # 回执丢一次不要紧（按钮停在乐观状态，下次操作即纠正），输入通道不能停。
            self._emit_control(P.pack_msg(P.Msg.MONITOR_STATE, ack), tries=3)
        elif t == P.Msg.CLIPBOARD:
            # 对端复制、本机粘贴。**不在这里写剪贴板**：OpenClipboard /
            # SetClipboardData 在剪贴板被占时会拖时间，而这是会话的 recv 线程 ——
            # 它一停，输入包没人读、上行缓冲塞满，控制端就是"键鼠失效但画面正常"。
            # 交给剪贴板轮询线程（单槽、最新优先）。
            text = str(pkt.json().get("text", ""))[: P.MAX_CLIPBOARD_CHARS]
            log.info("收到剪贴板 %d 字符", len(text))
            if text and text != self._clip_local:
                self._clip_local = text
                self._clip_pending = text
        elif t == P.Msg.PING:
            # 回显对端的时间戳而不是自己的钟（见 _ping_ts）。
            self._emit_control(P.pack_msg(P.Msg.PONG, {"ts": self._ping_ts(pkt)}))
        elif t == P.Msg.BYE:
            raise ConnectionError("viewer said goodbye")
        else:
            log.debug("忽略消息类型 %s", t)


class AgentService:
    """Owns capture and the single active session."""

    # 抓不到东西（None）是硬故障，值得立刻恢复；一整片全黑的缓冲要软得多：
    # 真黑屏（黑色锁屏、休眠的显示器、全黑视频）和丢失的复制句柄长得一模一样
    # —— 所以黑帧照发，只用来喂重建启发式，后者会先问 mss 要第二意见
    # （见 _watch_black_streak）。
    REBUILD_AFTER_NONE = 30  # ~1 s
    #: 全黑多久后做第一次交叉验证（问 mss"屏幕是不是真的黑"）。
    BLACK_PROBE_AFTER = 60  # ~2 s
    #: 交叉验证之后隔多久复查一次。屏幕可以一直黑着，但后端也可能在黑屏当中坏掉。
    BLACK_PROBE_EVERY = 300  # ~10 s
    #: 交叉验证跑不起来（mss 也不可用）时的兜底：按帧数硬重建。
    REBUILD_AFTER_BLACK = 300  # ~10 s

    def __init__(
        self,
        *,
        config: Config | None = None,
        capture: ScreenCapture | None = None,
        injector: InputInjector | None = None,
        host: str = "0.0.0.0",
        port: int = P.DEFAULT_AGENT_PORT,
        target_fps: int = 30,
        session_silence_s: float = SESSION_SILENCE_S,
        on_event: EventHook | None = None,
    ) -> None:
        self.config = config or Config("agent")
        self.host = host
        self.port = int(self.config.get("port", port))
        self.target_fps = int(self.config.get("fps", target_fps))
        #: 可注入：测试不必真等 30 秒就能验证会话看门狗。
        self.session_silence_s = float(session_silence_s)
        self.on_event = on_event or (lambda _n, _d: None)

        self.capture = capture
        self.frames: FrameSlot = FrameSlot()
        # 可注入：测试可以断言事件而不真的移动本机光标。
        self.injector: InputInjector = injector if injector is not None else InputInjector()
        self.pairing = PairingCode("agent")
        self._allow_control = bool(self.config.get("allow_control", True))

        self._encoder: FrameEncoder | None = None
        self._encoder_lock = threading.Lock()
        self._tls = agent_tls_context("agent")
        self._capture_failures = 0
        self._black_streak = 0  # 连续全黑帧数，见 _watch_black_streak
        self._pending_rebuild = False  # 控制端要求切换显示器，等采集循环去重建
        self._timers_raised = False    # 是否已申请高精度定时器（成对归还）

        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._capture_thread: threading.Thread | None = None
        self._session: _Session | None = None
        self._session_lock = threading.Lock()
        self._stop = threading.Event()
        #: 在途握手。``stop()`` 必须能把它们掐掉：监听套接字关了只能挡住**新**连接，
        #: 已经连上、正卡在 ``recv_packet`` 上的握手还会活最多 15 秒，然后照样验码
        #: 通过、建出会话 —— 那是一个「服务已停止、却还在接受键鼠注入」的活会话。
        self._hs_lock = threading.Lock()
        self._hs_sockets: set[socket.socket] = set()
        #: 同时握手的数量上限。每个握手是一个线程 + 一次完整 ECDSA TLS 握手，
        #: 而握手最多可以什么都不发就挂 15 秒（slowloris）。
        self._hs_limit = 8

    # -- identity ----------------------------------------------------------
    @property
    def name(self) -> str:
        return str(self.config.get("name", platform.node() or "agent"))

    @property
    def pair_code(self) -> str:
        """Fixed until the operator changes it - see ``Recommon.pairing``."""
        return self.pairing.code

    @property
    def is_busy(self) -> bool:
        with self._session_lock:
            return self._session is not None

    @property
    def allow_control(self) -> bool:
        """Whether this machine accepts incoming control at all (R5)."""
        return bool(self._allow_control)

    def set_allow_control(self, value: bool) -> None:
        self._allow_control = bool(value)
        self.config["allow_control"] = self._allow_control
        if not self._allow_control:
            # 已经有人在里面时关门会让这个开关失去意义 —— 一起把当前会话踢掉。
            self.kick_current()
        self._emit_event("allow_control", {"value": self._allow_control})

    def set_pair_code(self, code: str) -> str:
        cleaned = self.pairing.set_code(code)
        self._emit_event("pair_code", {"code": cleaned})
        return cleaned

    def regenerate_pair_code(self) -> str:
        code = self.pairing.regenerate()
        self._emit_event("pair_code", {"code": code})
        return code

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        # 采集用 time.sleep() 节拍；没有 1 ms 定时器分辨率，每次节拍最多偏 15 ms，
        # 画面就会一顿一顿。成对：stop() 里归还。
        # 只在第一次 start 申请：连续两次 start() 不该把引用计数 +2，而 stop()
        # 只还得起一次。
        if not self._timers_raised:
            enable_high_resolution_timers()
            self._timers_raised = True
        # stop() 会把帧槽 close 掉，而 close 不可逆 —— 重启必须换新槽，否则发送
        # 线程第一轮就"帧槽已关闭"直接收工。旧会话拿的是旧槽的引用，由它安静退出。
        self.frames = FrameSlot()
        if self.capture is None:
            # stop() 把采集后端释放后置 None，所以重启会走到这里重建。要用自定义
            # 采集的宿主，在 stop() 之后重新赋值 self.capture。
            self.capture = create_capture(
                int(self.config.get("monitor", 0)), target_fps=self.target_fps
            )
        self.injector.set_region(self.capture.region)
        # 编码器必须在采集**之后**建：探测 h264_mf 会初始化 COM，抢在前面的话
        # dxcam 后面的 comtypes 初始化会死在 RPC_E_CHANGED_MODE 上，采集被迫退回
        # 慢后端。
        self._encoder = create_encoder(fps=self.target_fps)

        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Windows 上要用 SO_EXCLUSIVEADDRUSE，不能用 SO_REUSEADDR：后者允许第二个
        # 进程绑同一个端口，两个"被控端"会按不确定的规则瓜分进来的连接（会话随机
        # 打到其中一台，还互相把对方判成 BUSY）。独占绑定让第二个实例直接绑失败，
        # 由调用方报"端口被占用"，这才是对的。
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.host, self.port))
        self._listener.listen(4)
        self.port = self._listener.getsockname()[1]

        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="lrdp-capture", daemon=True
        )
        self._capture_thread.start()

        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="lrdp-accept", daemon=True
        )
        self._accept_thread.start()

        # 刻意不重新生成验证码：固定验证码要求重启后保持不变。
        self._emit_event("state", {"state": "listening", "port": self.port})
        log.info("被控服务已监听 %s:%d", self.host, self.port)

    def stop(self) -> None:
        self._stop.set()
        with self._session_lock:
            if self._session:
                self._session.shutdown()
        if self._listener:
            try:
                self._listener.close()
            except OSError:
                pass
        # 在途握手必须一起掐掉。监听套接字只挡得住新连接；已经连上、正卡在
        # recv_packet 上的握手还能活最多 15 秒，之后照样验码通过、占槽、建出会话
        # —— 而 frames 此刻已经 close，那个会话有键鼠权限却永远收不到画面。
        with self._hs_lock:
            pending = list(self._hs_sockets)
        for hs in pending:
            try:
                hs.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                hs.close()
            except OSError:
                pass
        if pending:
            log.info("已掐断 %d 个在途握手", len(pending))
        self.frames.close()
        if self._capture_thread:
            self._capture_thread.join(timeout=2.0)
        if self._accept_thread:
            self._accept_thread.join(timeout=2.0)
        if self.capture:
            self.capture.close()
            # 释放过的采集后端不能再用（DxcamCapture.close 连内部句柄一起拆）。
            # 置 None，start() 才会重建 —— 否则重启后复用一个死实例。
            self.capture = None
        # 硬件编码器的句柄在这里还给驱动，而不是等进程退出（宿主程序里停掉又
        # 重启服务是常规操作）。
        close_encoder = getattr(self._encoder, "close", None)
        if close_encoder is not None:
            try:
                close_encoder()
            except Exception:  # noqa: BLE001
                log.exception("关闭编码器失败")
        if self._timers_raised:
            self._timers_raised = False
            disable_high_resolution_timers()
        self._emit_event("state", {"state": "stopped"})

    def kick_current(self) -> None:
        """Drop whoever is connected. Pairing is a single shared code, so there
        is no per-client credential to revoke - rotating the code does that."""
        with self._session_lock:
            if self._session is not None:
                self._session.shutdown()

    # -- internals ---------------------------------------------------------
    def _emit_event(self, name: str, data: dict) -> None:
        try:
            self.on_event(name, data)
        except Exception:  # noqa: BLE001 - a UI hook must not kill the service
            log.exception("事件回调失败：%s", name)

    @property
    def encoder_codec(self) -> int:
        return int(getattr(self._encoder, "codec", P.Codec.JPEG))

    @property
    def encoder_effective_quality(self) -> int | None:
        """编码器真正生效的 quality；还没开过流时为 None。

        与 AdaptiveQuality.quality（计算值）不是一回事：H.264 的码率只在开流时
        定，会话内调 quality 传不进已开的上下文。STATS/状态栏报这个值，才不会让
        一个不生效的控制器"看起来在工作"。
        """
        value = getattr(self._encoder, "effective_quality", None)
        return int(value) or None

    def reset_encoder(self) -> None:
        """Begin a stream that starts on a keyframe (see H264Encoder.reset)."""
        reset = getattr(self._encoder, "reset", None)
        if reset is not None:
            with self._encoder_lock:
                reset()

    def encode(self, frame, quality: int) -> bytes:
        with self._encoder_lock:
            return self._encoder.encode(frame, quality)

    def _has_session(self) -> bool:
        with self._session_lock:
            return self._session is not None

    def _capture_loop(self) -> None:
        """Pace capture at the streaming rate; the slot keeps only the newest.

        桌面复制并不稳定：改显示模式、UAC 弹窗、锁屏/解锁、某些全屏程序都会让它
        丢句柄，之后 ``grab()`` 永远返回空（或一张全黑的旧缓冲）。所以盯着它，
        必要时重建后端 —— 但帧要照发：黑帧也是帧，而"死句柄"必须与"屏幕本来就是
        黑的"区分开（见 _watch_black_streak）。
        """
        interval = 1.0 / max(1, self.target_fps)
        last = time.perf_counter()
        # 初值为 True：后端刚 open() 时是开着采集的，所以第一个空闲周期要把它停掉。
        # 写成 False 就永远没人 pause()，dxcam 的线程会一直转。
        live = True
        # 会话结束到真正 pause() 之间的缓冲（CAPTURE_LINGER_S）。初值取"已经空闲
        # 很久"，所以服务启动后的第一个空闲周期仍立刻暂停。
        idle_since = time.monotonic() - CAPTURE_LINGER_S
        while not self._stop.is_set():
            # stop() 在收尾时会把 self.capture 置 None，重建失败也会。每一拍先取
            # 局部引用再用，别去戳一个已经被收走的对象。
            cap = self.capture
            if not self._has_session():
                # 没人连着就别采集：空载时 dxcam 的两个线程照样满速转。有人连上
                # 才开，断开就停。但刚断就重连是常态，linger 之内不拆，免得重连
                # 风暴把 pause()/resume() 当心跳用。
                if idle_since is None:
                    idle_since = time.monotonic()
                if live and time.monotonic() - idle_since >= CAPTURE_LINGER_S:
                    log.info("无会话 %.0f 秒，暂停采集", CAPTURE_LINGER_S)
                    if cap is not None:
                        cap.pause()
                    live = False
                time.sleep(0.25)
                last = time.perf_counter()
                continue
            idle_since = None
            if cap is not None and not live:
                log.info("会话已建立，恢复采集")
                cap.resume()
                live = True
                last = time.perf_counter()

            # 控制端要求切换显示器：在这里重建，保证不与 grab() 并发。
            if self._pending_rebuild:
                self._pending_rebuild = False
                self._rebuild_capture()
                cap = self.capture  # 重建后是新后端，旧的已经关了

            frame = None
            if cap is not None:
                try:
                    frame = cap.grab()
                except Exception:  # noqa: BLE001 - a transient grab failure must not stop capture
                    log.exception("采集异常")

            # 黑帧探测只看稀疏采样：逐像素求 max 每帧要扫完整个 6MB。
            black = frame is not None and int(frame[::8, ::8].max(initial=0)) <= 2

            if frame is None:
                self._black_streak = 0
                self._capture_failures += 1
                if self._capture_failures == 1:
                    log.info("采集返回无效帧（未抓到画面）")
                if self._capture_failures >= self.REBUILD_AFTER_NONE:
                    log.warning(
                        "采集连续 %d 帧没抓到画面，重建采集后端", self._capture_failures
                    )
                    self._capture_failures = 0
                    self._rebuild_capture()
            else:
                if self._capture_failures:
                    log.info("采集已恢复（此前 %d 帧无效）", self._capture_failures)
                self._capture_failures = 0
                # 每帧都送，**黑帧也是帧**（真黑屏时控制端就永远停在最后一张图上，
                # 画面冻结）。空载的 CPU 问题由上面的 pause/resume 解决。
                self.frames.put(frame)
                if black:
                    self._black_streak += 1
                    self._watch_black_streak()
                else:
                    self._black_streak = 0

            last += interval
            delay = last - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                last = time.perf_counter()

    def _watch_black_streak(self) -> None:
        """连续全黑时判断「屏幕真的黑」还是「采集后端在交旧缓冲」。

        帧已经照发了，这里只决定要不要重建 —— 而判据不能只看黑不黑：真黑屏和
        丢句柄的死缓冲长得一样，按帧数硬拆就是每 10 秒白拆一次采集后端。所以
        问一句 mss（与 DXGI 无关的第二双眼睛，见 ``region_looks_bright``）：

        * mss 也黑  → 屏幕确实全黑，不动它，等画面自己回来；
        * mss 有内容 → 只有我们在交黑帧，后端已经废了，重建；
        * mss 也测不出来 → 退回旧策略，按帧数兜底重建。
        """
        n = self._black_streak
        if n < self.BLACK_PROBE_AFTER or (n - self.BLACK_PROBE_AFTER) % self.BLACK_PROBE_EVERY:
            return
        cap = self.capture
        bright = region_looks_bright(cap.region) if cap is not None else None
        if bright is None:
            if n >= self.REBUILD_AFTER_BLACK:
                log.warning("全黑帧无法交叉验证（mss 不可用），按连续 %d 帧重建采集后端", n)
                self._black_streak = 0
                self._rebuild_capture()
            return
        if bright:
            log.warning("采集连续 %d 帧全黑，但屏幕实际有内容，重建采集后端", n)
            self._black_streak = 0
            self._rebuild_capture()
        elif n == self.BLACK_PROBE_AFTER:
            # 只记一次：真黑屏会一直黑下去，每 10 秒一条 INFO 纯属日志噪音。
            log.info("采集连续 %d 帧全黑，mss 确认屏幕确实全黑（照发，不重建）", n)

    def _set_monitor(self, index: int) -> dict:
        """切换到第 index 个显示器（0 起）。控制端在远程窗口里点按钮触发。

        返回给请求方的确认载荷（``P.Msg.MONITOR_STATE``）：请求方是把按钮先点亮、
        再发这个请求的，没有回执的话被控端一旦拒收，界面就会永远停在错的显示器
        上，而画面其实还在原来那块。

        只置标志、不直接重建采集后端：重建会 close() 掉旧的 dxcam 实例，而
        ``_capture_loop`` 线程可能正在 grab()，那会撞上 assert。真正动手的是
        ``_capture_loop`` 自己。
        """
        monitors = [list(m) for m in list_monitors()]
        current = int(self.config.get("monitor", 0))
        # 上界必须校验。mss/dxcam 拿到越界下标不会报错，只会安静地退回
        # monitors[0] —— 那是"所有屏拼起来的虚拟桌面"。于是 {"index": 99}
        # 表面上切换成功，实际开始抓一张横跨全部显示器的巨图，而且这个下标还会
        # 被写进 config.json 持久下来。
        if index < 0 or index >= len(monitors):
            log.warning(
                "拒绝切换到第 %d 块显示器：本机只有 %d 块（当前仍是第 %d 块）",
                index + 1,
                len(monitors),
                current + 1,
            )
            return {
                "index": current,
                "monitors": monitors,
                "ok": False,
                "error": f"对方只有 {len(monitors)} 块显示器",
            }
        if index == current:
            # 已经是这块屏，别白重建一次采集后端
            return {"index": index, "monitors": monitors, "ok": True}
        log.info("切换到第 %d 块显示器", index + 1)
        self.config["monitor"] = index
        self._pending_rebuild = True
        self._emit_event("monitor", {"index": index, "monitors": monitors})
        return {"index": index, "monitors": monitors, "ok": True}

    def _rebuild_capture(self) -> None:
        log.warning("重建采集后端（分辨率变化 / 句柄丢失？）")
        self._black_streak = 0
        # 先摘下来再关：stop() 也在收这个对象，两边同时 close 同一个 dxcam 实例
        # 会撞出它内部的 assert。
        old, self.capture = self.capture, None
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                log.exception("关闭旧采集后端失败")
        if self._stop.is_set():
            # 重建期间服务被停掉了：别把没人收的新后端建出来。
            return
        try:
            fresh = create_capture(
                int(self.config.get("monitor", 0)), target_fps=self.target_fps
            )
            if self._stop.is_set():
                fresh.close()
                return
            # create_capture() 出来是暂停态（见 DxcamCapture.open）；重建发生在
            # 会话当中，所以要立刻恢复采集。
            fresh.resume()
            self.injector.set_region(fresh.region)
            self.capture = fresh
        except Exception:  # noqa: BLE001 - keep trying on the next tick
            log.exception("重建采集后端失败")

    def _accept_loop(self) -> None:
        assert self._listener is not None
        self._listener.settimeout(0.5)
        while not self._stop.is_set():
            try:
                raw, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._hs_lock:
                if len(self._hs_sockets) >= self._hs_limit:
                    # 半开连接已经把握手线程占满了：不值得再为它做一次 TLS 握手。
                    # 占位必须在这里、而不是握手线程里做 —— 放在线程里的话 N 个
                    # 并发连接会同时通过检查、同时做完整 ECDSA TLS 握手，上限形同虚设。
                    log.warning("并发握手已达上限 %d，拒绝 %s", self._hs_limit, addr[0])
                    try:
                        raw.close()
                    except OSError:
                        pass
                    continue
                # 先登记裸 socket 再起线程：stop() 因此连**还在做 TLS 握手**的
                # 连接也掐得掉。
                self._hs_sockets.add(raw)
            threading.Thread(
                target=self._handshake,
                args=(raw, addr),
                name=f"lrdp-hs-{addr[0]}",
                daemon=True,
            ).start()

    # -- handshake ---------------------------------------------------------
    def _handshake(self, raw: socket.socket, addr: tuple[str, int]) -> None:
        raw.settimeout(15.0)
        try:
            sock = self._tls.wrap_socket(raw, server_side=True)
        except (OSError, ssl.SSLError) as exc:
            log.warning("与 %s 的 TLS 握手失败：%s", addr[0], exc)
            with self._hs_lock:
                self._hs_sockets.discard(raw)
            raw.close()
            return

        # 同一条连接，登记从裸 socket 换成套好的 TLS socket。
        with self._hs_lock:
            self._hs_sockets.discard(raw)
            self._hs_sockets.add(sock)
        session: _Session | None = None
        claimed = False   # 我们占着槽？
        started = False   # 会话线程已经起跑？（起跑之后就归它自己收尾）
        try:
            if self._stop.is_set():
                # 服务在 TLS 握手上就停了。别再往下走。
                raise ConnectionError("agent is stopping")
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # 服务可能正好在这次握手的中间被停掉（关监听套接字只能挡新连接）。
            # 不查这一下，握手会继续验码、占槽、建会话 —— 而 frames 此时已经
            # close，那个会话于是「有权限、没画面」，sender 线程还在空转。
            if self._stop.is_set():
                raise ConnectionError("agent is stopping")
            hello = P.recv_packet(sock).json()
            client_id = str(hello.get("client_id", ""))
            client_name = str(hello.get("client_name", client_id or addr[0]))
            if not client_id:
                raise ConnectionError("client did not identify itself")

            with self._session_lock:
                taken = self._session is not None

            if not self._allow_control and not taken:
                # 这里必须发 REJECT 而不是 AUTH_RESP：此时还没走到认证，控制端正
                # 等着 HELLO 应答。发 AUTH_RESP 会被它当成 HELLO 应答吞掉，然后读到
                # EOF，把"已关闭远程控制权限"报成 "peer closed the connection"。
                self._send(
                    sock,
                    P.pack_msg(P.Msg.REJECT, {"reason": int(P.Reject.CONTROL_DISABLED)}),
                )
                raise ConnectionError(P.Reject.CONTROL_DISABLED.text)

            self._send(
                sock,
                P.pack_msg(
                    P.Msg.HELLO,
                    {
                        "agent_id": platform.node(),
                        "agent_name": self.name,
                        "version": "0.2",
                        "busy": taken,
                    },
                ),
            )

            auth = P.recv_packet(sock).json()
            if taken:
                # 在认证之前就拒：否则一个正忙的被控端会白白消耗掉一次配对码，
                # 还登记一个根本不让进的客户端。
                self._send(
                    sock,
                    P.pack_msg(P.Msg.AUTH_RESP, {"ok": False, "reason": int(P.Reject.BUSY)}),
                )
                raise ConnectionError(P.Reject.BUSY.text)

            if not self._authenticate(auth):
                self._send(
                    sock,
                    P.pack_msg(
                        P.Msg.AUTH_RESP, {"ok": False, "reason": int(P.Reject.AUTH_FAILED)}
                    ),
                )
                raise ConnectionError(P.Reject.AUTH_FAILED.text)

            if bool(auth.get("probe")):
                # 只验 IP+验证码的握手：回完 AUTH_RESP 就结束，不占会话槽、
                # 不建 _Session、不发 CTRL_GRANT。控制端点"连接"时先做一次这样的
                # 探测，而那时真正的窗口还没打开 —— 走完整握手的话，这一瞬间会有
                # 一个拿完整权限、接受键鼠注入的会话存在，还把唯一的会话槽占住，
                # 紧接着的正式连接就会撞上"已被别人控制"。
                self._send(sock, P.pack_msg(P.Msg.AUTH_RESP, {"ok": True, "probe": True}))
                log.info("验证码校验通过（探测，不建立会话）：%s", client_name or addr[0])
                # 发完就关：我们已经读完了对端发的全部请求，没有未读数据，
                # 所以这是一个干净的 FIN，对端读到的仍然是 AUTH_RESP。
                try:
                    sock.close()
                except OSError:
                    pass
                return

            self._send(sock, P.pack_msg(P.Msg.AUTH_RESP, {"ok": True}))

            # ---- 占槽 ----------------------------------------------------
            # 「检查」和「占用」必须在同一个锁里，而且必须早于 CTRL_GRANT 和
            # start()。否则：并发握手全都通过检查 → 多个控制端同时拿到活会话；
            # 对端在 CTRL_GRANT 之后立刻断开 → 会话线程在占槽之前就死掉，
            # _on_session_end 的身份判断不成立，随后这个**死掉的**会话被写进槽里，
            # 之后所有连接都判 BUSY。
            # 「先占槽」带来的旧风险（起跑前抛异常导致槽永久占住）由下面的
            # finally 兜底：起跑之前失败就由握手线程自己把槽还回去。
            with self._session_lock:
                if self._session is not None:
                    self._send(
                        sock,
                        P.pack_msg(
                            P.Msg.AUTH_RESP,
                            {"ok": False, "reason": int(P.Reject.BUSY)},
                        ),
                    )
                    raise ConnectionError(P.Reject.BUSY.text)
                session = _Session(sock, addr, client_id, client_name, self, self.injector)
                self._session = session
                claimed = True

            try:
                w, h = self.capture.width, self.capture.height
                x, y, rw, rh = self.capture.region
                monitors = list_monitors()
                self._send(
                    sock,
                    P.pack_msg(
                        P.Msg.CTRL_GRANT,
                        {
                            "w": w,
                            "h": h,
                            "region": [x, y, rw, rh],
                            # 真值来自编码器本身，别硬编码 JPEG：H.264 推流时也报
                            # JPEG 会把查日志的人带偏。
                            "codec": self.encoder_codec,
                            "fps": self.target_fps,
                            # 多屏：所有显示器的矩形 + 当前正在推的是第几个（0 起）
                            "monitors": [list(m) for m in monitors],
                            "monitor": int(self.config.get("monitor", 0)),
                        },
                    ),
                )
                sock.settimeout(None)
                session.start()
                started = True
            finally:
                # 起跑之前就失败（对端已经走了、发包失败…）：槽是我们占的，得我们
                # 还。起跑之后归会话线程自己（_on_session_end 是条件化的）。
                if claimed and not started and session is not None:
                    with self._session_lock:
                        if self._session is session:
                            log.info("握手未完成，释放会话槽：%s", addr[0])
                            self._session = None
            # 事件放在起跑之后：这样"被控制中"只在真的有会话时才对面板说。
            self._emit_event("state", {"state": "busy", "client": client_name})
            self._emit_event("client_connected", {"client": client_name, "ip": addr[0]})
        except Exception as exc:  # noqa: BLE001
            log.info("与 %s 握手失败：%s", addr[0], exc)
            self._emit_event("client_rejected", {"ip": addr[0], "reason": str(exc)})
            # 拒绝后立刻 close 可能在对方读完理由之前就 RST，它看到截断的流就分不清
            # "被拒绝"还是"链路断了"，于是永远重试。所以只关写侧，让对端先把内容
            # 排空。
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            try:
                sock.settimeout(1.0)
                sock.recv(1)  # returns once the peer closes its side
            except (OSError, ssl.SSLError):
                pass
            try:
                sock.close()
            except OSError:
                pass
        finally:
            with self._hs_lock:
                self._hs_sockets.discard(sock)

    def _authenticate(self, auth: dict) -> bool:
        """Every session is checked against the fixed verification code.

        刻意没有"记住的令牌"快路径：需求就是每次连接都校验 IP + 验证码，而且
        验证码被改时另一端要能*发现*并说出来。
        """
        return self.pairing.verify(str(auth.get("pair_code", "")))

    def _on_session_end(self, session: _Session) -> None:
        with self._session_lock:
            if self._session is session:
                self._session = None
        self._emit_event("client_disconnected", {"client": session.client_name})
        self._emit_event("state", {"state": "listening", "port": self.port})

    @staticmethod
    def _send(sock: socket.socket, data: bytes) -> None:
        P.send_packet(sock, data)