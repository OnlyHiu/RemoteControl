"""本机的固定验证码。

操作者设置一次就长期有效、可随时修改 —— 远程协助需要对方能随时回拨，
所以不用「随机 + 有时效」的一次性码。安全性相应弱一些。

存储逻辑：
    读到有效验证码 → 原样使用（**不重新生成**）
    读不到 / 文件损坏 → 才随机生成并写回
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading

from . import settings

log = logging.getLogger(__name__)

CODE_PATTERN = re.compile(r"^[0-9A-Za-z]{4,6}$")
CODE_LENGTH = 6


def generate_code() -> str:
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def normalise_code(code: str) -> str:
    """返回规范化后的验证码；格式不对则抛 ValueError。"""
    cleaned = (code or "").strip()
    if not CODE_PATTERN.match(cleaned):
        raise ValueError("验证码需为 4-6 位数字或英文字母")
    return cleaned


class PairingCode:
    """本机展示给控制方的验证码。"""

    def __init__(self, role: str = "agent") -> None:
        # 默认落在主程序目录，测试环境和打包后路径一致，验证码因此不会
        # "每次启动都变"。role 仅用于测试隔离。
        self.path = (
            settings.pairing_path()
            if role == "agent"
            else settings.data_root() / f"pairing-{role}.json"
        )
        self._lock = threading.Lock()
        loaded = self._load()
        if loaded is not None:
            self._code = loaded
            log.info("验证码已从 %s 加载", self.path)
        else:
            self._code = generate_code()
            log.warning("%s 中没有可用的验证码，已重新生成并保存", self.path)
            self._save()

    # -- 持久化 -----------------------------------------------------------
    def _load(self) -> str | None:
        """读取已保存的验证码；任何问题都返回 None 并说明原因。"""
        if not self.path.exists():
            log.info("验证码文件尚不存在：%s", self.path)
            return None
        try:
            raw = self.path.read_text("utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("无法读取 %s：%s", self.path, exc)
            return None
        try:
            value = json.loads(raw).get("code")
            return normalise_code(str(value)) if value else None
        except Exception as exc:  # noqa: BLE001
            # 文件存在但解析不了 —— 多半是被别的软件改写/加密过。
            log.warning("无法解析 %s（%s）：%r", self.path, exc, raw[:80])
            return None

    def _save(self) -> None:
        """写盘并回读校验，确保下次一定能读到。"""
        payload = json.dumps({"code": self._code}, ensure_ascii=False)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001
            log.error("验证码保存失败 %s：%s", self.path, exc)
            return
        # 回读一次：写成功不代表读得回来（杀软/同步盘/权限都可能改写文件）。
        try:
            check = json.loads(self.path.read_text("utf-8")).get("code")
        except Exception as exc:  # noqa: BLE001
            log.error("验证码已写入但读不回来：%s", exc)
            return
        if str(check) != self._code:
            log.error("磁盘上的验证码与内存不一致：%r != %r", check, self._code)

    # -- 接口 -------------------------------------------------------------
    @property
    def code(self) -> str:
        with self._lock:
            return self._code

    def set_code(self, code: str) -> str:
        """设置新验证码，返回实际保存的值。"""
        cleaned = normalise_code(code)
        with self._lock:
            self._code = cleaned
            self._save()
        log.info("操作者修改了验证码")
        return cleaned

    def regenerate(self) -> str:
        return self.set_code(generate_code())

    def verify(self, supplied: str) -> bool:
        """常量时间比较，避免计时侧信道。"""
        return hmac_compare((supplied or "").strip(), self.code)


def hmac_compare(a: str, b: str) -> bool:
    """常量时间比较；两边都是任意文本也安全。

    ``hmac.compare_digest`` 的 str 版本只接受 ASCII，遇到非 ASCII 直接抛
    TypeError —— 而这里比的是**对端输进来的验证码**：粘贴时带进一个中文或全角
    字符，握手就在这一行炸掉。转成 UTF-8 字节再比，非 ASCII 就是普通的不匹配。

    surrogatepass：json.loads 能造出带孤立代理项的 str（"\\ud800"），普通 encode
    会再抛 UnicodeEncodeError —— 同一类"线上文本炸掉握手"。
    """
    import hmac

    return hmac.compare_digest(
        a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass")
    )