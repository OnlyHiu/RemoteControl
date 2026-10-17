"""Latest-wins frame bus.

Both pipelines (agent capture -> encode, viewer decode -> render) want the
*newest* frame and are happy to drop everything older.  A queue would only
accumulate latency under load; this single-slot mailbox never does.
"""

from __future__ import annotations

import threading
from typing import Generic, TypeVar

T = TypeVar("T")


class FrameSlot(Generic[T]):
    """Thread-safe mailbox that keeps exactly one item (the newest)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._item: T | None = None
        self._closed = False
        self.dropped = 0

    def put(self, item: T) -> None:
        with self._cond:
            if self._closed:
                # close() 之后再写入会把 closed 属性（“已关闭且已取空”）顶回
                # False，收尾中的发送循环又活过来一拍；dropped 计数也失真。
                return
            if self._item is not None:
                self.dropped += 1
            self._item = item
            self._cond.notify_all()

    def take(self, timeout: float | None = None) -> T | None:
        """Block until an item arrives (or *timeout*), then consume it."""
        with self._cond:
            # wait_for 在超时前会一直等条件成立 —— 单次 wait() 被虚假唤醒
            # 会在没超时、没关闭时就返回 None。
            self._cond.wait_for(
                lambda: self._item is not None or self._closed, timeout
            )
            item, self._item = self._item, None
            return item

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed and self._item is None
