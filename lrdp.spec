# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec —— 单文件、无控制台的 LRDP.exe。

    pyinstaller lrdp.spec --noconfirm

要点（README「打包要点」一节有展开说明）：

* 大量第三方库是**函数内延迟导入**的（快速启动 + 优雅降级），静态分析看不见，
  所以要 collect_submodules / collect_data_files 显式收进来。
* 排除 QtWebEngine（独占 ~150MB），只留 QtCore/QtGui/QtWidgets。
* console=False ⇒ sys.stdout/stderr 是 None，app.py 一进来就把它们接到
  lan_remote_data/logs/console.log。
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
import os
import tempfile

# 入口是一段**生成的**引导脚本，绝对导入这个包；真正被分析的是
# ``RemoteControl.app``，于是 ``from .viewer...`` 这些相对导入能带着正确的包
# 上下文被解析，整个子包都会进包。
#
# 直接把 app.py 当入口是不行的：PyInstaller 永远把入口当独立脚本执行，
# 它的 ``__package__`` 是空的，所有相对导入在静态分析里全是死的 ——
# agent/ Recommon/ viewer/ 会被静默丢掉，exe 一启动就 ModuleNotFoundError。
HERE = os.path.abspath(SPECPATH)
PARENT = os.path.dirname(HERE)
PKG = os.path.basename(HERE)

_BUILD = tempfile.mkdtemp(prefix="lrdp-build-")
ENTRY = os.path.join(_BUILD, "lrdp_entry.py")
with open(ENTRY, "w", encoding="utf-8") as _fh:
    _fh.write(
        "import sys\n"
        f"sys.path.insert(0, {PARENT!r})\n"
        f"from {PKG}.app import main\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )
PATHS = [PARENT]

# 延迟导入的第三方后端：缺一个就是运行时 ModuleNotFoundError。
HIDDEN = [
    "mss",
    "av",
    "av.video",
    "av.audio",
    "cryptography",
    "cryptography.hazmat.primitives.asymmetric.ec",
    "cryptography.hazmat.primitives.serialization",
    "comtypes",
    "comtypes.client",
    "qfluentwidgets",
]
for _name in ("dxcam", "turbojpeg"):
    try:
        HIDDEN.extend(collect_submodules(_name))
    except Exception:  # 未安装的可选加速后端，跳过
        pass

# mss 附带平台扩展与数据文件；av 附带 ffmpeg 库；comtypes 运行时生成 gen 包。
DATAS = (
    collect_data_files("mss")
    + collect_data_files("av")
    + collect_data_files("qfluentwidgets")
)

a = Analysis(
    [ENTRY],
    pathex=PATHS,
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # PySide6 里只有这三件套是本项目用得到的，其余（尤其 QtWebEngine）纯占体积。
    excludes=[
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtBluetooth",
        "PySide6.QtPositioning",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtSql",
        "PySide6.QtTest",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "tkinter",
        "unittest",
        "pydoc_data",
        "test",
        "numpy.tests",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="LRDP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 远程控制类工具开 UPX 会被杀软当加壳报毒
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 无黑框；fix_stdio() 会把 stdout/stderr 接到日志文件
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
