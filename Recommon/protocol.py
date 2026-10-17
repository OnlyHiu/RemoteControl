"""LRDP - LAN Remote Desktop Protocol.

Wire format
------------
Every message is a 16-byte fixed header followed by ``length`` bytes of payload::

    offset  size  field
    0       4     magic   b"LRDP"
    4       1     version uint8
    5       1     type    Msg.*
    6       2     flags   bit0 = keyframe (bit1 reserved, unused)
    8       4     length  payload length in bytes
    12      4     seq     monotonically increasing sequence number

Two payload flavours are used:

* control messages -> UTF-8 JSON object (``Packet.json()``)
* media messages   -> packed struct + raw bytes (see ``FRAME_BODY``)
"""

from __future__ import annotations

import json
import socket
import struct
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

MAGIC = b"LRDP"
PROTO_VERSION = 1

DEFAULT_AGENT_PORT = 37121

HEADER = struct.Struct("<4sBBHII")  # magic, ver, type, flags, length, seq
FRAME_BODY = struct.Struct("<HHIQ")  # width, height, codec, ts_us
MOUSE_BODY = struct.Struct("<BBffii")  # action, button, x_ratio, y_ratio, dx, dy
KEY_BODY = struct.Struct("<BBHHI")  # action, vk, scan, flags, modifiers
WHEEL_BODY = struct.Struct("<ii")  # delta, horizontal(0/1)

FLAG_KEYFRAME = 1 << 0

MAX_PAYLOAD = 32 * 1024 * 1024  # sanity guard against a corrupt/huge frame


class Msg(IntEnum):
    # handshake
    HELLO = 1
    AUTH_REQ = 2
    AUTH_RESP = 3
    PAIR = 4
    REJECT = 7
    BYE = 8
    # session
    CTRL_REQ = 10
    CTRL_GRANT = 11
    # media (agent -> viewer)
    FRAME = 20
    # input (viewer -> agent)
    MOUSE = 30
    KEY = 31
    WHEEL = 32
    #: 切换被控端的显示器。载荷 {"index": 0|1|2...}
    SET_MONITOR = 33
    #: 被控端的确认回执（agent -> viewer）：
    #: {"index": 实际下标, "monitors": [...], "ok": bool, "error": str?}
    #: 请求方是先把按钮点亮再发请求的，没有回执就一直停在错的显示器上。
    MONITOR_STATE = 34
    # utility
    CLIPBOARD = 40
    PING = 50
    PONG = 51
    STATS = 60
    ERROR = 99


#: 剪贴板同步只走文本，且限制长度，避免一次粘贴几十兆把连接压垮。
MAX_CLIPBOARD_CHARS = 100_000


class Codec(IntEnum):
    JPEG = 1
    PNG = 2
    H264 = 3


class MouseAction(IntEnum):
    MOVE = 1
    DOWN = 2
    UP = 3


class MouseBtn(IntEnum):
    NONE = 0
    LEFT = 1
    RIGHT = 2
    MIDDLE = 3
    X1 = 4
    X2 = 5


class KeyAction(IntEnum):
    DOWN = 1
    UP = 2


# key flags carried in KEY_BODY
KEYF_EXTENDED = 1 << 0


class Reject(IntEnum):
    AUTH_FAILED = 1
    BAD_TOKEN = 2
    DENIED = 3
    BUSY = 4
    PROTO_MISMATCH = 5
    SHUTTING_DOWN = 6
    CONTROL_DISABLED = 7

    @property
    def text(self) -> str:
        return {
            Reject.AUTH_FAILED: "验证码不正确",
            Reject.BAD_TOKEN: "令牌已失效，请重新配对",
            Reject.DENIED: "对方拒绝了控制请求",
            Reject.BUSY: "对方已被别人控制",
            Reject.PROTO_MISMATCH: "协议版本不一致",
            Reject.SHUTTING_DOWN: "对方正在关闭",
            Reject.CONTROL_DISABLED: "对方关闭了远程控制授权",
        }.get(self, "连接被拒绝")


def reject_code(value: object) -> int | None:
    """线上 reason 值对应的 ``Reject`` 成员值；认不出来返回 ``None``。

    reason 是**对端给的**：可能是整数、数字字符串，也可能根本不是我们这套枚举。
    硬造 ``Reject(value)`` 会抛 ValueError，把一次普通拒绝变成
    "连接失败：42 is not a valid Reject"。浮点/布尔/None 一律按"认不出"处理，
    别截成 3 再报一句**错的**原因给操作者。
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return int(Reject(int(value)))
    except (TypeError, ValueError):
        return None


def reject_text(value: object) -> str:
    """把线上 reason 翻成人话；认不出来也要有一句给操作者看的话。"""
    code = reject_code(value)
    return Reject(code).text if code is not None else "连接被拒绝（对方未说明原因）"


@dataclass(slots=True)
class Packet:
    type: int
    payload: bytes = b""
    flags: int = 0
    seq: int = 0

    def json(self) -> dict[str, Any]:
        return json.loads(self.payload.decode("utf-8"))

    def frame(self) -> tuple[int, int, int, int, bytes]:
        """Return ``(width, height, codec, ts_us, encoded_bytes)``."""
        if len(self.payload) < FRAME_BODY.size:
            raise ValueError("truncated frame packet")
        w, h, codec, ts = FRAME_BODY.unpack_from(self.payload)
        return w, h, codec, ts, self.payload[FRAME_BODY.size :]


# --------------------------------------------------------------------------
# packing helpers
# --------------------------------------------------------------------------


def json_payload(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def pack_frame(
    width: int,
    height: int,
    codec: int,
    ts_us: int,
    data: bytes,
    *,
    keyframe: bool = True,
    seq: int = 0,
) -> bytes:
    flags = FLAG_KEYFRAME if keyframe else 0
    body = FRAME_BODY.pack(width, height, codec, ts_us) + data
    return HEADER.pack(MAGIC, PROTO_VERSION, Msg.FRAME, flags, len(body), seq) + body


def pack_msg(msg_type: int, obj: dict[str, Any], *, seq: int = 0, flags: int = 0) -> bytes:
    body = json_payload(obj)
    return HEADER.pack(MAGIC, PROTO_VERSION, msg_type, flags, len(body), seq) + body


# --------------------------------------------------------------------------
# socket helpers (blocking sockets on both ends)
# --------------------------------------------------------------------------


def recv_exact(sock: socket.socket, size: int) -> bytes:
    """Read exactly *size* bytes or raise ``ConnectionError`` on EOF.

    A timeout before any byte has arrived is benign and re-raised so the
    caller can retry.  A timeout *mid-packet* is fatal: the partial bytes are
    gone and the stream is left desynchronised, so quietly resuming would turn
    the next message into garbage.
    """
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        try:
            chunk = sock.recv(min(remaining, 1 << 20))
        except socket.timeout:
            if chunks:
                raise ConnectionError("timed out mid-packet; stream is out of sync")
            raise
        if not chunk:
            raise ConnectionError("peer closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    # size == 0 时循环不执行、chunks 为空：必须显式返回 b""，否则下面按
    # "多于一个元素才 join"的写法会取 chunks[0] 抛 IndexError。
    return b"".join(chunks) if len(chunks) > 1 else (chunks[0] if chunks else b"")


def send_packet(sock: socket.socket, packet: bytes) -> None:
    sock.sendall(packet)


def recv_packet(sock: socket.socket) -> Packet:
    head = recv_exact(sock, HEADER.size)
    magic, ver, ptype, flags, length, seq = HEADER.unpack(head)
    if magic != MAGIC:
        raise ConnectionError("bad magic, not an LRDP stream")
    if ver != PROTO_VERSION:
        raise ConnectionError(f"unsupported protocol version {ver}")
    if length > MAX_PAYLOAD:
        raise ConnectionError(f"payload too large ({length} bytes)")
    payload = recv_exact(sock, length) if length else b""
    return Packet(type=ptype, payload=payload, flags=flags, seq=seq)


def now_us() -> int:
    return time.time_ns() // 1000


def endpoint(addr: str, port: int = DEFAULT_AGENT_PORT) -> tuple[str, int]:
    return (addr, port)
