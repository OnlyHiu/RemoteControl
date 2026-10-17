"""Frame encoding / decoding.

JPEG is the P0/P3 codec: on a gigabit LAN a 1080p JPEG at quality 60 is
~120 KB, so 30 fps is only ~3.5 MB/s - well inside budget, and far simpler
than an H.264 pipeline.

``TurboJpegEncoder`` (libjpeg-turbo) is the fast path, ``PillowEncoder`` is
the zero-extra-dependency fallback.  Both speak the same ``FrameEncoder``
interface so the pipeline does not care which one it got.
"""

from __future__ import annotations

import io
import logging
import threading
from abc import ABC, abstractmethod
from fractions import Fraction

import numpy as np

from .protocol import Codec

log = logging.getLogger(__name__)

#: Serialises the H.264 probe/cache below.  ``_h264_context()`` is called from
#: the encode path and from ``create_encoder()``; two threads searching at once
#: would each burn a hardware probe and race on ``_H264_PICKED``.
_H264_LOCK = threading.Lock()


class FrameEncoder(ABC):
    codec: int = Codec.JPEG

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def encode(self, rgb: np.ndarray, quality: int) -> bytes:
        """Encode an ``(H, W, 3) uint8`` RGB array."""


class TurboJpegEncoder(FrameEncoder):
    """libjpeg-turbo via PyTurboJPEG - roughly 3-5x faster than Pillow."""

    name = "turbojpeg"

    def __init__(self) -> None:
        from turbojpeg import TJFLAG_FASTDCT, TJPF_RGB, TurboJPEG

        self._tj = TurboJPEG()
        self._flags = TJFLAG_FASTDCT
        self._pf = TJPF_RGB

    def encode(self, rgb: np.ndarray, quality: int) -> bytes:
        return self._tj.encode(
            np.ascontiguousarray(rgb), quality=int(quality), pixel_format=self._pf, flags=self._flags
        )


class PillowEncoder(FrameEncoder):
    name = "pillow"

    def __init__(self) -> None:
        from PIL import Image

        self._img = Image
        self.effective_quality = 0

    def encode(self, rgb: np.ndarray, quality: int) -> bytes:
        buf = io.BytesIO()
        self._img.fromarray(rgb, "RGB").save(
            buf, format="JPEG", quality=int(quality), optimize=False, subsampling=2
        )
        # JPEG 逐帧生效：生效值就是这一次用的值。
        self.effective_quality = int(quality)
        return buf.getvalue()


#: 硬件优先：1080p 软件编码要 11~17 ms，几乎吃掉一个 16.7 ms 的帧间隔，60 fps
#: 没有余量；NVENC/QSV/AMF 只要 1~3 ms。顺序按办公 PC 上的常见度排。
#: 但「能开」不等于「能用」：探测会真的试编码并数帧延迟，压帧不吐的候选直接
#: 跳过（见 _probe_h264）—— 操作跟手比省 CPU 重要。
_H264_CODECS = ("h264_nvenc", "h264_qsv", "h264_amf", "h264_mf", "libx264")

#: 探测时喂多少帧来观察"边喂边出"。得大于编码器的流水线深度才看得出缓冲。
_PROBE_FRAMES = 24

#: 允许编码器压多少帧不吐（0 = 边喂边出）。1 帧 = 33ms@30fps，可以忍；
#: h264_mf 实测恒定压 12 帧（≈400ms 延迟，任何码控选项都一样）。
_PROBE_MAX_HELD = 1


def _encoder_options(name: str, gop: int) -> dict[str, str]:
    """Per-encoder knobs. 探测与真会话必须用同一套，否则探测的是另一回事。"""
    if name == "libx264":
        # 限 2 线程：FFmpeg 默认按核心数开线程，8 核就是每帧起 8 个线程、
        # 30fps 下每秒 240 次唤醒与同步，纯粹是额外开销。2 线程已能跟上 1080p30
        # （实测编码 ~10ms，预算 33.3ms）。不要加 sliced_threads：坏码流的根因
        # 在解码端，已单独处理。
        return {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "g": str(gop),
            "bf": "0",
            "threads": "2",
        }
    return {"preset": "ll", "g": str(gop), "bf": "0"}


#: 探测胜出的编码器名，缓存起来让搜索每进程只跑一次。探测硬件编码器意味着要
#: **打开**一个临时 CodecContext、会占用 NVENC/QSV/AMF 设备句柄，而
#: _h264_context() 每次 H264Encoder._open() 都会调（每个会话、每次改分辨率），
#: 不缓存就等于每次白开一个设备句柄。
_H264_PICKED: str | None = None


def _probe_h264(name: str) -> bool:
    """Can *name* encode here **without buffering frames**?

    开一个 128x128 上下文并真的喂 ``_PROBE_FRAMES`` 帧，数 flush 前吐出多少。
    两件事只有真编码才测得出：候选可能列得出来却打不开（没设备、驱动问题）；
    也可能开得了但压着帧不放 —— h264_mf 恒定压 12 帧（30fps 下 ≈400ms 额外
    手到屏延迟），对远程协助来说比慢一点的编码器更糟。

    PyAV 的 CodecContext 没有 ``close()``，只有 ``__dealloc__`` 会跑
    ``avcodec_free_context``，所以靠重新绑定局部变量来释放探测上下文。
    """
    import av

    ctx = None
    try:
        ctx = av.codec.CodecContext.create(name, "w")
        ctx.width, ctx.height, ctx.pix_fmt = 128, 128, "yuv420p"
        ctx.time_base = Fraction(1, 30)
        ctx.framerate = Fraction(30, 1)
        ctx.options = _encoder_options(name, 60)
        ctx.open()
        blank = np.zeros((128, 128, 3), dtype=np.uint8)
        produced = 0
        for i in range(_PROBE_FRAMES):
            frame = av.VideoFrame.from_ndarray(blank, format="rgb24")
            frame.pts = i
            produced += len(ctx.encode(frame))
        held = _PROBE_FRAMES - produced
        if held > _PROBE_MAX_HELD:
            log.info(
                "h264 编码器 %s 压着 %d 帧不吐（≈%dms 延迟），跳过",
                name,
                held,
                held * 33,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - not present / refused to open
        log.info("h264 编码器 %s 不可用：%s", name, exc)
        return False
    finally:
        ctx = None


def _h264_context():
    """Return (context, codec_name) for the fastest H.264 encoder available.

    The returned context is always *unopened* - the probe opens a throwaway
    copy, because a CodecContext cannot be re-parameterised once opened.
    """
    global _H264_PICKED

    import av

    from .config import init_com_mta

    # 编码上下文在哪条线程上 open，哪条线程就必须是 MTA —— h264_mf
    # （Media Foundation）在 STA 线程上 open 必然失败（实测 EINVAL）。
    # 采集侧（comtypes/dxcam）则钉 STA，两者靠「不同线程」共存。
    init_com_mta()

    with _H264_LOCK:
        # 已有胜者时直接建未打开的上下文，成本为零。
        if _H264_PICKED is not None:
            try:
                return av.codec.CodecContext.create(_H264_PICKED, "w"), _H264_PICKED
            except Exception:  # noqa: BLE001 - cached encoder vanished; re-probe
                log.warning("已缓存的 h264 编码器 %s 不可用，重新探测", _H264_PICKED)
                _H264_PICKED = None

        for name in _H264_CODECS:
            if not _probe_h264(name):
                continue
            _H264_PICKED = name
            return av.codec.CodecContext.create(name, "w"), name
    raise RuntimeError("no h264 encoder available")


def _force_libx264() -> None:
    """把缓存钉死在 libx264。

    硬件编码器「探测通过、真会话却打不开」时的退路（见 H264Encoder._open）：
    记住这次教训，别再把打不开的编码器一遍遍发给后续会话。
    """
    global _H264_PICKED
    with _H264_LOCK:
        _H264_PICKED = "libx264"


class H264Encoder(FrameEncoder):
    """Inter-frame coding via H.264 - the whole point is the delta.

    JPEG resends the whole screen every frame, so a desktop that barely moves
    costs as much as one in full motion.  H.264 codes only what changed, which
    is a 10-100x reduction for real desktop use and is what makes a slow link
    usable at full resolution.

    Stateless on the outside (``encode`` -> bytes), but the codec is
    necessarily a stream: output for a frame may be empty if the encoder is
    still buffering, and every session starts on a keyframe so a fresh decoder
    can join.
    """

    codec = Codec.H264
    name = "h264"

    def __init__(self, fps: int = 30, keyframe_every_s: float = 2.0) -> None:
        self._fps = max(1, fps)
        self._gop = max(self._fps, int(self._fps * keyframe_every_s))
        self._ctx = None
        self._size: tuple[int, int] | None = None
        self._pts = 0
        self.codec_name = "libx264"
        #: 开流时真正生效的 quality。H.264 的码率只在 _open 定，会话内再怎么调
        #: 也传不进已开的上下文；对外报「生效值」，才不会让人以为自适应在实时起作用。
        self.effective_quality = 0

    def _open(self, width: int, height: int, quality: int) -> None:
        from fractions import Fraction

        for attempt in (0, 1):
            ctx, name = _h264_context()
            ctx.width, ctx.height = width, height
            ctx.pix_fmt = "yuv420p"
            ctx.time_base = Fraction(1, self._fps)
            ctx.framerate = Fraction(self._fps, 1)
            # ~0.06 bits/pixel at quality 70, scaled by the adaptive controller.
            # H.264 的码率只能在开流时定，运行中改要重开编码上下文（丢参考帧、
            # 重新发 IDR），代价远大于收益。所以 quality 在这里生效一次，而不是
            # 逐帧生效 —— 日志里那个 q= 也只在重开时才会真的变。JPEG 回退路径
            # （PillowEncoder）才是逐帧生效的。
            bpp = 0.06 * (quality / 70.0)
            ctx.bit_rate = max(400_000, int(width * height * self._fps * bpp))
            # 必须设 thread_count 属性：写进 ctx.options 的 "threads" 映射不到
            # CodecContext 上，仍然是"按核心数开线程" —— 8 核就起 7 个 worker
            # 在屏障上空转。
            ctx.thread_count = 2
            ctx.thread_type = "SLICE"
            # 与探测用同一套选项（_encoder_options）：探测的必须就是将要开的。
            ctx.options = _encoder_options(name, self._gop)
            try:
                ctx.open()
                break
            except Exception:  # noqa: BLE001
                if name == "libx264" or attempt:
                    raise
                # 探测时能开、真会话却开不起来（驱动状态、线程公寓……）：把教训
                # 记进缓存退回软件编码，否则发送线程每帧都会抛一次「编码失败」。
                log.warning("h264 编码器 %s 打不开，回退到 libx264", name, exc_info=True)
                _force_libx264()
        self._ctx = ctx
        self._size = (width, height)
        self.codec_name = name
        self.effective_quality = int(quality)

    def encode(self, rgb: np.ndarray, quality: int) -> bytes:
        import av

        h, w = rgb.shape[:2]
        if self._ctx is None or self._size != (w, h):
            self._open(w, h, quality)
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
        frame.pts = self._pts
        self._pts += 1
        out = [bytes(p) for p in self._ctx.encode(frame)]
        return b"".join(out)

    def reset(self) -> None:
        """Start a fresh stream that opens on a keyframe.

        H.264 is a continuous stream, so a decoder joining mid-GOP can show
        nothing until the next IDR.  Since one encoder serves one session at a
        time, dropping and reopening the context at session start guarantees the
        very first frame a viewer receives is decodable.
        """
        self.close()
        self._pts = 0

    def close(self) -> None:
        # VideoCodecContext has no close(); dropping the reference is all
        # that is needed and all that is available.
        self._ctx = None


def create_encoder(fps: int = 30) -> FrameEncoder:
    """H.264 when available; JPEG is the fallback and is always usable.

    构造与试编码都在**一条全新的 MTA 线程**上做：验证必须真的 open() 一个编码
    上下文，而 h264_mf 在 STA 线程上 open 必然失败。调用方（AgentService.start）
    的线程刚被 create_capture() 钉成 STA —— 公寓模型是每线程的，另起一条线程，
    采集和编码就能各自拿到自己要的模型。
    """
    box: dict[str, object] = {}

    def _build() -> None:
        try:
            enc = H264Encoder(fps=fps)
            # prove the codec actually works before committing to it
            enc.encode(np.zeros((64, 64, 3), dtype=np.uint8), 60)
            # 试编码用的上下文立刻归还：真上下文到会话的发送线程上才开。
            enc.close()
            box["enc"] = enc
        except Exception as exc:  # noqa: BLE001
            box["exc"] = exc

    worker = threading.Thread(target=_build, name="lrdp-enc-probe", daemon=True)
    worker.start()
    worker.join()
    if "enc" in box:
        enc = box["enc"]
        log.info("编码器：h264/%s", enc.codec_name)
        return enc  # type: ignore[return-value]
    log.info("h264 不可用（%s），回退到 JPEG", box.get("exc"))
    return PillowEncoder()


def _has_idr(data: bytes) -> bool:
    """载荷里有没有 IDR(5) 或 SPS(7) —— 没有的话解不了，直接丢弃。

    重连后到下一个关键帧之间的 P 帧没有参考帧，送进解码器必然报 "Invalid
    data"。与其让解码器抛异常再重置，不如在入口就滤掉，静默等到关键帧。
    """
    i, n = 0, len(data)
    while True:
        j = data.find(b"\x00\x00\x00\x01", i)
        if j < 0 or j + 4 >= n:
            return False
        if (data[j + 4] & 0x1F) in (5, 7):
            return True
        i = j + 4


class StreamDecoder:
    """Codec-aware decoding.

    JPEG is stateless; H.264 is a stream, so the decoder instance holds the
    reference frames and must live as long as the session.  Frames before the
    first keyframe return ``None`` rather than garbage.
    """

    def __init__(self) -> None:
        self._jpeg = _jpeg_decoder()
        self._ctx = None
        self._ready = False  # 本条流是否已拿到过关键帧

    def decode(self, codec: int, data: bytes) -> np.ndarray | None:
        if codec == Codec.H264:
            return self._decode_h264(data)
        return self._jpeg(data)

    def _decode_h264(self, data: bytes) -> np.ndarray | None:
        import av

        if self._ctx is None:
            self._ctx = av.codec.CodecContext.create("h264", "r")
        if not data:
            return None
        # 没等到关键帧就丢弃。注意不能只在 _ctx is None 时查：新会话刚建立时
        # _ctx 可能还留着上一条流的状态，此时先到的 P 帧一样没有参考帧。
        if not self._ready and not _has_idr(data):
            return None
        try:
            # 整个载荷作为一个包送进去，不要用 parse() 拆：parse() 会把一个访问
            # 单元拆成 SPS/PPS/切片多个包再逐个 decode()，切片单独送进去时解码器
            # 还没建立 SPS/PPS 与参考帧状态，P 帧必然报 "Invalid data found"。
            decoded = self._ctx.decode(av.Packet(data))
        except Exception:  # noqa: BLE001 - resync at the next keyframe
            # warning 而不是 debug：解码失败时画面就是黑的，而 debug 级别在
            # 线上日志里根本看不见。附上载荷头部：Annex-B 码流必须以
            # 00 00 00 01 开头，不是的话就是封装格式不匹配。
            log.warning(
                "h264 解码失败，等待关键帧（载荷 %d 字节，头 16 字节=%s）",
                len(data),
                data[:16].hex() if data else "(空)",
                exc_info=True,
            )
            self._ctx = None
            self._ready = False
            return None
        if not decoded:
            return None
        self._ready = True
        return decoded[-1].to_ndarray(format="rgb24")


_JPEG_DECODER = None


def _jpeg_decoder():
    """Return ``decode(bytes) -> np.ndarray (H, W, 3) uint8 RGB``.

    结果缓存：每次建连都重新探测一遍 turbojpeg 纯属浪费（断线重连风暴时还会
    刷日志噪音）。JPEG 解码器无状态，全局复用是安全的（h264 另有 StreamDecoder）。
    """
    global _JPEG_DECODER
    if _JPEG_DECODER is not None:
        return _JPEG_DECODER
    probe = PillowEncoder().encode(np.zeros((8, 8, 3), dtype=np.uint8), 50)

    try:
        from turbojpeg import TJPF_RGB, TurboJPEG

        tj = TurboJPEG()

        def decode_turbo(data: bytes) -> np.ndarray:
            return tj.decode(data, pixel_format=TJPF_RGB)

        assert decode_turbo(probe).shape == (8, 8, 3)
        _JPEG_DECODER = decode_turbo
        return decode_turbo
    except Exception as exc:  # noqa: BLE001
        log.info("turbojpeg 解码不可用（%s），回退到 Pillow", exc)

    from PIL import Image

    def decode_pillow(data: bytes) -> np.ndarray:
        img = Image.open(io.BytesIO(data))
        img.load()
        return np.asarray(img.convert("RGB"), dtype=np.uint8)

    _JPEG_DECODER = decode_pillow
    return decode_pillow


def create_decoder() -> StreamDecoder:
    return StreamDecoder()
