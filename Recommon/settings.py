"""用户可配置项。

正常情况下不需要改这个文件 —— 包根的 ``__init__.py`` 顶部有一个
「在这里配置」区块，改那里即可。这里只是配置的落地点。
"""

from __future__ import annotations

import sys
from pathlib import Path

# --- 可配置项 ---------------------------------------------------------

#: 远程窗口的图标，填文件路径字符串（.ico / .png）；None 表示用系统默认图标。
WINDOW_ICON: str | None = None

#: 数据根目录：验证码、连接历史、config.json、密钥证书都放在这下面（按 role
#: 分子目录）。None 表示跟随主程序所在目录下的 lan_remote_data\。
DATA_DIR: str | None = None

#: 验证码文件名（存在 DATA_DIR 下）。
PAIRING_FILE: str = "pairing.json"

#: 连接历史文件名（存在 DATA_DIR 下）。
HISTORY_FILE: str = "history.json"

#: 连接历史最多保留多少条。
HISTORY_LIMIT: int = 50

#: 是否由本组件自己挂 RotatingFileHandler（写到 log_dir()/lrdp.log）。
#:
#: 嵌进已有日志体系的宿主程序时必须设 False，否则每条记录会在两个文件里各落一遍。
#: 正常入口（包根 ``__init__.py``）会在导入时显式赋值，这里给一个安全的默认值，
#: 保证绕过包根直接用内部模块时也不会 AttributeError。
SETUP_FILE_LOGGING: bool = True


# --- 路径解析 ---------------------------------------------------------


def program_dir() -> Path:
    """主程序所在的目录。

    打包成 exe 后是 exe 所在目录；从源码运行时是入口脚本所在目录 ——
    也就是**宿主程序**的位置，不是本组件的位置。这样测试环境和打包后的
    存储路径保持一致。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    entry = sys.argv[0] if sys.argv else ""
    if entry and entry != "-c":
        path = Path(entry).resolve()
        if path.is_file():
            return path.parent
        if path.suffix == "":
            return path
    return Path.cwd()


def data_root() -> Path:
    """实际的存储目录：配置了就用配置的，否则跟随主程序。"""
    if DATA_DIR:
        root = Path(DATA_DIR).expanduser()
        if not root.is_absolute():
            root = program_dir() / root
    else:
        root = program_dir() / "lan_remote_data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def pairing_path() -> Path:
    return data_root() / PAIRING_FILE


def history_path() -> Path:
    return data_root() / HISTORY_FILE


def window_icon_path() -> Path | None:
    if not WINDOW_ICON:
        return None
    p = Path(WINDOW_ICON).expanduser()
    return p if p.is_absolute() else program_dir() / p


def log_dir() -> Path:
    """日志目录，与验证码/历史同级，跟随主程序位置。"""
    root = data_root() / "logs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def fix_stdio() -> None:
    """无控制台构建下，把 stdout/stderr 接到文件上。

    windowed 的 exe 里 ``sys.stdout`` / ``sys.stderr`` 是 ``None``，任何一次
    ``print()``（包括 argparse 的 ``--help`` 和异常回溯）都会抛
    ``AttributeError``，把真正的错误盖掉。这里让它们至少落到文件里。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    handle = open(log_dir() / "console.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle