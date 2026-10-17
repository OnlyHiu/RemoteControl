"""History of remote partners, kept by the controlling side.

Each entry remembers the verification code last seen for that machine so a
retry can be one click.  When the remote side changes its code the stored one
stops working - that failure is exactly what tells us to say "被远程对象已更换验证码"
rather than something vague.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field

from ..Recommon import settings
from ..Recommon.protocol import DEFAULT_AGENT_PORT

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Partner:
    host: str
    port: int = DEFAULT_AGENT_PORT
    name: str = ""
    code: str = ""
    last_used: float = field(default_factory=time.time)
    successes: int = 0

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def label(self) -> str:
        return self.name or self.host


class PartnerHistory:
    def __init__(self, role: str = "viewer", limit: int | None = None) -> None:
        # 跟随主程序目录，见 Recommon.settings。role 仅用于测试隔离。
        self.path = settings.history_path() if role == "viewer" else (
            settings.data_root() / f"history-{role}.json"
        )
        self.limit = limit if limit is not None else settings.HISTORY_LIMIT
        self._lock = threading.Lock()
        self._items: dict[str, Partner] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("连接历史读取失败（%s），已从空开始", exc)
            return
        for item in raw if isinstance(raw, list) else []:
            try:
                entry = Partner(**item)
            except TypeError:
                continue
            self._items[entry.key] = entry

    def _save(self) -> None:
        """原子写盘。**绝不把异常冒到调用方**。

        ``remember()`` 是在 GUI 线程上被 ``assist_widget._open_window`` /
        ``_handle_probe_failure`` 调的，一次 PermissionError 冒上去就是"点连接、
        窗口没开出来"。内存里的历史照旧更新，只是这一回没落盘。

        临时名必须唯一（同 ``Recommon.config._save_unlocked``）：history.json
        是跟随主程序目录的固定文件，两个 LRDP.exe 实例（或同进程里两个
        PartnerHistory，各持各的锁）同时写时固定的 history.tmp 会互相顶掉。
        """
        items = sorted(self._items.values(), key=lambda p: p.last_used, reverse=True)
        payload = [asdict(p) for p in items[: self.limit]]
        tmp: str | None = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001 - 见 docstring：这条路上不能抛
            log.warning("连接历史保存失败 %s：%s", self.path, exc)
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # -- api ---------------------------------------------------------------
    def all(self) -> list[Partner]:
        """Most recently used first.

        Windows 上 ``time.time()`` 只精确到 ~15 ms，所以前后两条可能共享同一
        时间戳，顺序就取决于排序是否稳定 —— 用 host 做次级键让它确定。
        """
        with self._lock:
            return sorted(
                self._items.values(), key=lambda p: (p.last_used, p.host), reverse=True
            )

    def recent_entries(self) -> list[tuple[str, str, int]]:
        """去重后的 (IP, 验证码, 端口)，最近使用在前。

        历史列表要显示、回填、删除，所以三项一起取。
        """
        seen: set[str] = set()
        out: list[tuple[str, str, int]] = []
        for partner in self.all():
            if partner.host and partner.host not in seen:
                seen.add(partner.host)
                out.append((partner.host, partner.code, partner.port))
        return out

    def get(self, host: str, port: int = DEFAULT_AGENT_PORT) -> Partner | None:
        with self._lock:
            return self._items.get(f"{host}:{port}")

    def remember(
        self,
        host: str,
        port: int = DEFAULT_AGENT_PORT,
        *,
        name: str = "",
        code: str = "",
        success: bool = False,
    ) -> Partner:
        """Record an attempt. A known code is never blanked by a later attempt."""
        with self._lock:
            key = f"{host}:{port}"
            entry = self._items.get(key) or Partner(host=host, port=port)
            entry.last_used = time.time()
            if name:
                entry.name = name
            if code:
                entry.code = code
            if success:
                entry.successes += 1
            self._items[key] = entry
            self._save()
            return entry

    def update_code(self, host: str, port: int, code: str) -> None:
        with self._lock:
            entry = self._items.get(f"{host}:{port}")
            if entry is None:
                entry = Partner(host=host, port=port)
            entry.code = code
            entry.last_used = time.time()
            self._items[entry.key] = entry
            self._save()

    def forget(self, host: str, port: int = DEFAULT_AGENT_PORT) -> None:
        with self._lock:
            self._items.pop(f"{host}:{port}", None)
            self._save()
