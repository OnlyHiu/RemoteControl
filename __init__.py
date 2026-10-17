"""Remote assistance as a drop-in widget.

Copy this folder into another project and use it like any other widget
（folder name is up to you）::

    from RemoteControl import RemoteAssistWidget

    panel = RemoteAssistWidget("remote_assist", parent=container)
    ...
    shutdown(panel)                                 # or hook aboutToQuit

Everything under ``agent/`` ``Recommon/`` ``viewer/`` imports its siblings with
*relative* names (``from ..Recommon.config import ...``) and never touches
``sys.path``, so the whole tree resolves under one package prefix and
``pyinstaller your_host.py`` keeps all of it.  Absolute imports look tidier but
PyInstaller would not see them and would drop the sub-packages from the build.

Requirements on the host environment: ``PySide6``, ``PySide6-Fluent-Widgets``,
``numpy``, ``Pillow``, ``av``.

Naming
------
The folder may be called anything the host likes; everything inside refers to
itself relatively.  ``Recommon`` is a historical typo, kept because renaming it
would churn every import for nothing.  ``app.py`` is the one file that has to
know the package name, because PyInstaller runs it as ``__main__``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 必须在 numpy 被 import 之前设好：OpenBLAS/MKL 会按核心数起一池子 worker，
# 而这里只用 numpy 做数组拷贝和切片，不需要 BLAS 多线程。
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

_ROOT = Path(__file__).resolve().parent

# =========================================================================
#  在这里配置
#
#  接入自己的软件时通常只需要改这一段。
# =========================================================================

from .Recommon import settings as _settings

#: 远程窗口图标，填文件路径（.ico / .png）。None = 系统默认。
#: 相对路径会按主程序所在目录解析。
_settings.WINDOW_ICON = None

#: 数据根目录。**验证码、连接历史、config.json、密钥证书**都放在这个目录下
#: （再按 role 分 agent\ viewer\ security\ 子目录）。
#:
#: None = 跟随主程序所在目录下的 lan_remote_data\；填相对路径按主程序目录
#: 解析，填绝对路径就用绝对路径。
_settings.DATA_DIR = None

#: 连接历史最多保留多少条。
_settings.HISTORY_LIMIT = 50

#: 是否由本组件自己挂一个文件日志处理器（写到日志目录的 lrdp.log）。
#:
#: 只有当宿主程序【完全不管日志】时才需要。如果你已经在宿主里生成了日志
#: （尤其是也写 lrdp.log），把它改成 False，否则每条记录会落两遍。
_settings.SETUP_FILE_LOGGING = True

# =========================================================================

import logging

log = logging.getLogger("lan_remote")

#: Set by ``use_logger`` so the fallback file handler stays out of the way.
_external_logger: logging.Logger | None = None


def use_logger(target: logging.Logger) -> None:
    """Write lan-remote's records through the host application's logger.

    Everything here logs via the standard hierarchy (``viewer.client``,
    ``agent.server``, ``Recommon.codec`` ...), so wiring the handlers across is
    all it takes - no call site changes.  Call it once after your own
    ``configure_logger()``.  Propagation is turned off for our loggers so records
    reach your handler exactly once instead of being doubled up.
    """
    global _external_logger
    _external_logger = target

    # 目标 logger 可能只是「继承」了处理器（basicConfig 把它们挂在 root 上），
    # 自己的 .handlers 是空的 —— 直接复制会静默丢掉所有记录。
    handlers = list(target.handlers) or list(logging.getLogger().handlers)
    level = target.level if target.level != logging.NOTSET else logging.getLogger().level

    for name in ("lan_remote", "viewer", "agent", "Recommon"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.propagate = False
        for handler in handlers:
            logger.addHandler(handler)
        if level != logging.NOTSET:
            logger.setLevel(level)
    logging.getLogger("lan_remote").info(
        "日志已接入 %r（%d 个处理器，级别=%s）",
        target.name,
        len(handlers),
        logging.getLevelName(level),
    )


def _setup_file_logging() -> None:
    """嵌入宿主时也要留下日志。

    ``app.setup_logging`` 只在 ``app.main()`` 里执行，宿主不会调用它。宿主若已
    经有日志体系，把 ``SETUP_FILE_LOGGING`` 改成 False，否则每条记录会落两遍。
    """
    from logging.handlers import RotatingFileHandler

    from .Recommon import settings

    if not _settings.SETUP_FILE_LOGGING:
        return

    root = logging.getLogger()
    # root 或宿主 logger 已挂任何处理器，就认为日志已有人管。
    if root.handlers or (_external_logger is not None and _external_logger.handlers):
        return
    if _external_logger is not None:
        return  # use_logger() 已接管

    directory = settings.log_dir()
    handler = RotatingFileHandler(
        directory / "lrdp.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    # 标记：app.setup_logging 会先摘掉它再挂自己那份，免得每条记录落两遍。
    handler._lrdp_managed = True
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    log.info("远程协助组件已就绪，日志目录：%s", directory)


# 缺 GUI 依赖时要在这里就明确报错，否则会从某个 widget 深处冒出一个裸 ImportError。
import importlib

for _dep in ("PySide6", "qfluentwidgets"):
    try:
        importlib.import_module(_dep)
    except ImportError as exc:  # pragma: no cover - environment problem
        raise ImportError(
            "lan-remote needs PySide6 and PySide6-Fluent-Widgets; "
            "run: pip install PySide6 PySide6-Fluent-Widgets"
        ) from exc

_settings.fix_stdio()
_setup_file_logging()

# The embeddable panel - this is what the host application usually wants.
from .viewer.assist_widget import RemoteAssistWidget  # noqa: E402

# The session window, for hosts that want to open one themselves instead of
# letting the panel do it.
from .viewer.remote_window import RemoteWindow  # noqa: E402

# Building blocks, for hosts that need to drive the engine directly.
from .agent.server import AgentService  # noqa: E402
from .viewer.client import ConnectionError_, RemoteCodeChanged, ViewerClient  # noqa: E402

# 载入后逐个报出实际解析到的文件：宿主里排查「到底加载的是谁的模块」时，
# 这一行就能定案。
for _name in ("Recommon", "agent", "viewer"):
    _mod = sys.modules.get(f"{__name__}.{_name}")
    if _mod is not None:
        log.debug("模块解析 %-24s -> %s", f"{__name__}.{_name}", getattr(_mod, "__file__", _mod))

__all__ = [
    "RemoteAssistWidget",
    "RemoteWindow",
    "AgentService",
    "ViewerClient",
    "ConnectionError_",
    "RemoteCodeChanged",
    "use_logger",
    "shutdown",
    "close",
    "run_standalone",
    "root",
]


def shutdown(*targets: object) -> None:
    """关闭本组件的任意对象：主面板 / 会话窗口 / 被控服务 / 控制端客户端。

    嵌入宿主后，把退出逻辑接到这里即可，不必逐类记关闭方法::

        RemoteControl.shutdown(panel)                   # 主面板
        RemoteControl.shutdown(window, client, service)  # 也可以逐个关

    每类目标自动选择合适的关闭方式，返回前会等后台收尾线程结束，所以调用方
    随后直接退出进程即可，不会把半死的采集/连接线程留给退出阶段。
    """
    import threading

    for target in targets:
        if target is None:
            continue
        if isinstance(target, RemoteAssistWidget):
            target.shutdown()
        elif isinstance(target, RemoteWindow):
            target.close()
        elif isinstance(target, AgentService):
            target.stop()
        elif isinstance(target, ViewerClient):
            target.close()
        else:
            for name in ("shutdown", "stop", "close"):
                fn = getattr(target, name, None)
                if callable(fn):
                    fn()
                    break
            else:
                raise TypeError(f"不知道如何关闭 {type(target).__name__}（没有 shutdown/stop/close）")
    # 收尾：会话断开与被控服务的 stop 都可能在后台线程里跑（各自内部有限时），
    # 等它们结束再返回，调用方随后退出进程才是干净收场。
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and thread.name in (
            "lrdp-close",
            "lrdp-shutdown",
        ):
            thread.join(timeout=4.0)


#: ``close`` 是 ``shutdown`` 的别名，语义相同。
close = shutdown

#: Folder holding this package - handy for locating bundled resources.
root: Path = _ROOT


def run_standalone(argv: list[str] | None = None) -> int:
    """Launch the panel in its own window.  Equivalent to ``app.py``."""
    from .app import main

    return main(argv)
