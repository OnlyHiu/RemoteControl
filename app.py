"""Standalone launcher for the remote-assistance panel.

The panel itself is an embeddable ``QFrame`` (``viewer.assist_widget``); this
only gives it a window when it is run on its own.  Inside the host application
you would instead do::

    from RemoteControl.viewer.assist_widget import RemoteAssistWidget
    widget = RemoteAssistWidget("remote_assist", parent=some_container)

    LRDP.exe --selftest   headless end-to-end check, writes logs/selftest.txt
"""

from __future__ import annotations

import os

# 必须在 numpy 被 import 之前设好：OpenBLAS/MKL 会按核心数起一池子 worker，
# 而这里只用 numpy 做数组拷贝和切片，不需要 BLAS 多线程。
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import argparse
import io
import logging
import sys
from contextlib import redirect_stdout
from pathlib import Path

#: 打包后的包名。onefile 构建里 ``__file__`` 指向随机命名的 ``_MEIPASS`` 目录，
#: 反推不出包名，所以这里写死 —— 改名仓库目录/包名时要同步改这一行。
PACKAGE_NAME = "RemoteControl"

# 项目其余部分都用相对导入，需要一个父包。``python app.py`` 与 ``LRDP.exe`` 都把
# 这个文件当 ``__main__`` 执行，没有父包 —— 所以补上：源码运行时本文件所在目录
# 本身就是这个包。
if not __package__:
    _HERE = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False):
        __package__ = PACKAGE_NAME
    else:
        if str(_HERE.parent) not in sys.path:
            sys.path.insert(0, str(_HERE.parent))
        __package__ = _HERE.name


def _fix_stdio() -> None:
    """无控制台构建下把 stdout/stderr 接到文件。实现见 ``Recommon.settings``。"""
    from .Recommon.config import fix_stdio

    fix_stdio()


def setup_logging(verbose: bool) -> Path:
    from logging.handlers import RotatingFileHandler

    from .Recommon import settings

    directory = settings.log_dir()
    log_path = directory / "lrdp.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    # 包根 ``__init__`` 在被 import 时已经挂过一个文件处理器；不先摘掉的话每条
    # 记录会在同一个 lrdp.log 里落两遍。
    _drop_managed_handlers(root)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler._lrdp_managed = True
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    stream_handler._lrdp_managed = True
    root.addHandler(stream_handler)
    return directory


def _drop_managed_handlers(root: logging.Logger) -> None:
    """摘掉本组件之前挂上的处理器（标记见 _setup_file_logging）。"""
    for handler in list(root.handlers):
        if getattr(handler, "_lrdp_managed", False):
            root.removeHandler(handler)


def _run_selftest() -> int:
    """Full handshake/stream/input round trip, with no GUI and no desktop.

    仓库默认不含 ``tests/loopback.py``；缺了就写一份 SKIP 报告而不是抛回溯。
    """
    from .Recommon.config import log_dir

    try:
        from .tests import loopback
    except ImportError as exc:
        report = f"SELFTEST SKIP: 未找到端到端测试包（{exc}）\n"
        _write_selftest_report(report)
        print(report, end="")
        print("SELFTEST SKIP")
        return 0

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = loopback.main(["--seconds", "2"])
    report = buffer.getvalue()
    _write_selftest_report(report)
    print(report)
    print(f"report       : {log_dir() / 'selftest.txt'}")
    print("SELFTEST " + ("PASS" if code == 0 else "FAIL"))
    return code


def _write_selftest_report(report: str) -> None:
    from .Recommon.config import log_dir

    report_path = log_dir() / "selftest.txt"
    try:
        with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(report)
            handle.flush()
            os.fsync(handle.fileno())
        status = f"ok, {report_path.stat().st_size} bytes"
    except Exception as exc:  # noqa: BLE001 - 要的是诊断信息，不是崩溃
        status = f"FAILED {type(exc).__name__}: {exc}"
    log = logging.getLogger("lrdp.launcher")
    log.info("自检报告 %s（%s）", report_path, status)


def main(argv: list[str] | None = None) -> int:
    _fix_stdio()

    parser = argparse.ArgumentParser(prog="LRDP", description="局域网远程协助")
    parser.add_argument("--selftest", action="store_true", help="运行端到端自检（无界面）")
    parser.add_argument("--fps", type=int, default=30, help="被控端最大帧率（默认 30）")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    args, _rest = parser.parse_known_args(argv)

    log_dir = setup_logging(args.verbose)
    log = logging.getLogger("lrdp.launcher")
    log.info("启动 argv=%s 日志目录=%s", sys.argv, log_dir)

    if args.selftest:
        return _run_selftest()

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMainWindow

    from .Recommon.config import enable_high_resolution_timers
    from .viewer.assist_widget import RemoteAssistWidget

    # 用 QApplication 而不是 QGuiApplication：面板由一堆 widget 组成，
    # 而 QGuiApplication 只支持无窗口的 GUI。
    enable_high_resolution_timers()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("lan-remote")

    window = QMainWindow()
    window.setWindowTitle("局域网远程协助")
    panel = RemoteAssistWidget("remote_assist", parent=window, target_fps=args.fps)
    window.setCentralWidget(panel)
    window.resize(560, 720)
    window.show()
    app.aboutToQuit.connect(panel.shutdown)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
