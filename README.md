# RemoteControl · 局域网远程协助（LRDP）

> **下载**：[Releases](https://github.com/OnlyHiu/RemoteControl/releases) 里取 `LRDP.exe`
> —— 单文件、免安装、免 Python。仓库里只有源码与 `lrdp.spec`，不存二进制。

**一个程序，两种身份。** 在同一台机器上双击 `LRDP.exe`，它既是**被控端**也是**控制端**：
对方输入你的 IP 和 6 位验证码就能看到你的屏幕、接管你的键鼠、同步剪贴板；
你也可以填对方的 IP 反过来控制它。

| | |
|---|---|
| **画面** | DXGI 桌面复制采集，H.264 硬件编码优先、JPEG 兜底，帧槽「最新优先」不排队 |
| **操作** | 键鼠注入走 `SendInput`，带扫描码所以非 US 布局也对；小键盘、方向键、右 Ctrl 都对 |
| **安全** | 自签 ECDSA 证书 + 指纹固定（TOFU）、固定验证码常量时间比较、TLS 加密 |
| **多屏** | 被控端有几块屏就显示几个按钮，切换有确认回执，不会"切了却还在抓另一块" |
| **分发** | 单个 `LRDP.exe`，无黑框、无安装、无 Python 依赖（走 [Releases](#下载)） |

> 仅供**自己有权管理的设备之间**在可信内网使用。未签名的远程控制程序会被杀软与
> SmartScreen 拦，请先加白名单（见 [分发与杀软](#分发与杀软)）。

---

## 目录

- [下载](#下载)
- [快速开始](#快速开始)
- [功能一览](#功能一览)
- [目录结构](#目录结构)
- [从源码运行](#从源码运行)
- [交互流程](#交互流程)
- [安全设计](#安全设计)
- [架构与线程模型](#架构与线程模型)
- [性能](#性能)
- [嵌入自己的 Qt 程序](#嵌入自己的-qt-程序)
- [分发与杀软](#分发与杀软)

---

## 下载

源码在仓库里，**编译好的 exe 在 [Releases](https://github.com/OnlyHiu/RemoteControl/releases)**。

| 资产 | 说明 |
|---|---|
| `LRDP.exe` | Windows x64，单文件、无黑框、免安装，约 94 MB |

仓库刻意**不存二进制**：94 MB 的 exe 提交进 git 会永久留在历史里，clone 的代价
由所有后来人承担，而且一旦发新版本旧 blob 也删不掉。所以 `dist/` 在 `.gitignore` 里，
每次发布挂一个 release 资产。

想自己编（会强制用 `dxcam`，除非你改 `lrdp.spec`）：

```powershell
pip install pyinstaller
pyinstaller lrdp.spec --noconfirm --clean     # 产物在 dist\LRDP.exe
```

exe 的命令行参数：

| 命令 | 作用 |
|---|---|
| `LRDP.exe` | 打开「局域网远程协助」主面板 |
| `LRDP.exe --fps 60` | 被控端最大帧率（默认 30） |
| `LRDP.exe -v` | 详细日志 |
| `LRDP.exe --selftest` | 无界面自检 |

运行期数据全在 **exe 同目录的 `lan_remote_data\`**（不写 exe 旁边之外的地方，
因为那里可能是只读目录或 UAC 虚拟化位置）：

```
lan_remote_data\pairing.json       本机固定验证码
lan_remote_data\history.json       连接历史
lan_remote_data\agent\             被控端 config.json + TLS 证书/私钥
lan_remote_data\viewer\            控制端 config.json（含已固定的证书指纹）
lan_remote_data\logs\lrdp.log      运行日志（2 MB 轮转 × 3）
lan_remote_data\logs\console.log   无终端时的 stdout/stderr
```

> **为什么没有 dxcam**：它会拖进整套 OpenCV，包体 +70 MB，逼近 GitHub 单文件
> 100 MB 的硬上限。所以发布的 exe 走 mss（GDI BitBlt）采集，1440p 实测约 25 fps。
> 想要 DXGI 采集（快一个数量级），自己装 `dxcam` 后用 `lrdp.spec` 重编。

## 快速开始

**两台 Windows 机器，同一网段（或可路由）。**

1. 两台机器各下载一份 `LRDP.exe`，放在同一个目录下，各自双击运行。
2. 被控端主面板上会显示本机 IP 和 **6 位验证码**（长期有效，随时可改）。
   把这两项告诉要控制它的人。
3. 控制端在「远程伙伴设备」里填对方 IP + 验证码，点「连接」。
4. 点一下远程窗口里的画面即可捕获键鼠；`F11` 全屏，`Ctrl+Shift+L` 释放，
   `Ctrl+Shift+D` 断开。
5. 断线会自动重连。想踢掉当前会话，在被控端主面板关掉「允许被控」。

```
控制端  192.168.1.23  ──TCP 37121──▶  被控端  192.168.1.10
         RemoteWindow                      RemoteAssistWidget
```

## 功能一览

- **实时画面**：H.264（NVENC/QSV/AMF/Media Foundation 硬件编码优先，逐个真编码探测）
  优先，不可用时退到 JPEG；分辨率固定 100%，只按「编码+发送耗时 vs 帧预算」自适应画质。
- **键鼠注入**：鼠标移动/按键/滚轮/侧键、完整键盘（含 F1~F24、方向键、导航键、
  小键盘、左右 Ctrl/Alt/Shift）；点击画面捕获、`Ctrl+Shift+L` 释放，失焦自动补发抬起。
- **剪贴板同步**：双向文本同步，带文本比较 + 时间窗双重防回声，只处理文本、上限 10 万字符。
- **多显示器**：按 Windows 自己的显示器编号排序，切换后有回执，界面上"切了"就是真切了。
- **连接历史**：记录每个伙伴的 IP / 验证码 / 端口，双击回填、右键连接或删除。
- **连接前探测**：点「连接」先只验 IP + 验证码，通过了才开真正的会话窗口。
- **断线重连**：指数退避（0.2 s 起，上限 3 s），状态栏显示累计断开次数。
- **实时遥测**：状态栏显示 RTT、接收/渲染帧率、被控端画质与帧大小，一眼看出慢在哪一端。
- **安全**：「允许被控」总开关、固定验证码、证书指纹固定与重新信任流程、并发握手上限、
  死亡连接看门狗、窗口关闭不留活会话。

## 目录结构

```
RemoteControl/
├── __init__.py               # 嵌入入口：配置区块 + use_logger + 依赖自检
├── app.py                    # 独立运行入口（被控端/控制端同一程序）
├── lrdp.spec                 # PyInstaller 打包脚本（单文件、无控制台）
├── Recommon/                 # 两端共用
│   ├── protocol.py           #   二进制帧格式 + 消息定义 + socket 收发
│   ├── security.py           #   自签 TLS 证书 + 指纹固定
│   ├── codec.py              #   帧编解码（H.264 探测/编码 + JPEG 兜底）
│   ├── framebus.py           #   「最新帧优先」单槽信箱
│   ├── pairing.py            #   固定验证码（长效，可随时修改）
│   ├── clipboard.py          #   Win32 剪贴板文本读写
│   ├── settings.py           #   可配置项与路径解析
│   └── config.py             #   配置持久化、网卡地址、定时器精度、COM 公寓
├── agent/                    # 被控端（被远程控制的一方）
│   ├── capture.py            #   屏幕采集（dxcam 快路径 / mss 兜底）
│   ├── injector.py           #   键鼠注入（ctypes SendInput）
│   └── server.py             #   会话服务、握手认证、媒体推送
├── viewer/                   # 控制端（发起控制的一方）
│   ├── client.py             #   连接管理、收流解码
│   ├── video_widget.py       #   画面渲染 + 输入采集
│   ├── remote_window.py      #   会话窗口（重连、状态栏、多屏切换）
│   ├── history.py            #   连接历史
│   └── assist_widget.py      #   主面板（可嵌入 QFrame）
├── lrdp.spec                 # PyInstaller 打包脚本（单文件、无控制台）
└── dist/LRDP.exe             # 本地打包产物（.gitignore 掉，走 Releases 发布）
```

> 仓库不含 `tests/`，所以 `--selftest` 会打印 `SELFTEST SKIP` 并在
> `lan_remote_data\logs\selftest.txt` 写明原因，而不是抛回溯。
> 想跑完整端到端自检，把带 `loopback.main()` 的 `tests/` 放回来即可。

## 从源码运行

```powershell
# 1. 虚拟环境
python -m venv .venv

# 2. 依赖
.venv\Scripts\python.exe -m pip install PySide6 PySide6-Fluent-Widgets numpy Pillow av cryptography mss

# 3. 跑起来
.venv\Scripts\python.exe app.py
```

可选加速（**强烈建议装 `dxcam`**）：

```powershell
.venv\Scripts\python.exe -m pip install dxcam        # DXGI 桌面复制采集，远快于 GDI BitBlt
.venv\Scripts\python.exe -m pip install turbojpeg    # JPEG 编解码快 2~4 倍
```

> Releases 里发布的 `LRDP.exe` **不含 dxcam**（见 [下载](#下载)）。
>
> 若 `Activate.ps1` 被执行策略拦截：
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

## 交互流程

1. 被控端启动后，主面板显示本机 IP 与 **6 位验证码**（长期有效，可随时点齿轮修改）。
2. 控制端填对方 IP 与验证码，点「连接」——先做一次不建会话的探测。
3. 校验通过后打开远程窗口；后续可从「连接历史」双击/右键一键再来。
4. 点画面捕获键鼠，`F11` 全屏，`Ctrl+Shift+L` 交还键鼠，`Ctrl+Shift+D` 断开。
5. 被控端主面板的「允许被控」开关可随时中断当前会话。
6. 证书指纹对不上时（比如对端重装过），会弹确认框：确认是本人再「重新信任并连接」。

## 安全设计

| 机制 | 说明 |
|---|---|
| 验证码 | 被控端设置的 6 位固定码，可随时修改；每次连接都校验，常量时间比较 |
| 验证码变更检测 | 控制端记住所用验证码，被改后明确提示"被远程对象已更换验证码" |
| 指纹固定 | 自签 ECDSA 证书，首次连接记录指纹（TOFU），之后不一致即拒绝并给出恢复路径 |
| TLS | TLS 1.2+，媒体流与输入上行都走加密通道 |
| 主动断开 | 主面板「允许被控」关闭即踢掉当前会话 |
| 唯一会话 | 被控端同时只接受一个控制端，第二个直接判 BUSY |
| 并发握手上限 | 最多 8 个在途握手，防 slowloris 把线程堆满 |
| 端口独占 | `SO_EXCLUSIVEADDRUSE`：第二个实例绑不上去，直接报"端口被占用" |

## 架构与线程模型

```
被控端  capture 线程 ──put──▶ FrameSlot ──take──▶ send 线程 ──▶ socket
        （按目标帧率节拍）    （只留 1 帧）    （编码 + 自适应画质）
                                     recv 线程 ──▶ InputInjector / 剪贴板

控制端  reader 线程 ──解码──▶ to_qimage ──put──▶ FrameSlot ──▶ GUI 线程 paintEvent
                              writer 线程 ──take──▶ 出站队列 ──▶ socket
                              keepalive 线程（每 5 s PING，30 s 静默判死）
```

`FrameSlot` 是延迟不滚雪球的关键：它**永远只存最新一帧**，编码或网络跟不上时直接丢帧，
而不是排队积压。任何「无界队列」的写法都会在网络抖动后把延迟越攒越大。

控制端渲染同理：读线程只往单槽里塞，GUI 线程自己拉。排队信号每帧带一张 6 MB 的
QImage，窗口一被遮挡就会把进程撑爆。

## 性能

参考数据（Windows 11 / Python 3.13 / 2560×1440 / 局域网）：

| 环节 | 耗时 | 换算帧率 |
|---|---|---|
| 采集 (mss / GDI BitBlt) 1440p | ~35 ms | ~28 fps |
| 采集 (dxcam / DXGI) 1440p | ~2 ms | 500+ fps |
| 编码 (libx264 ultrafast, 2 线程) | ~10 ms | 100 fps |
| 解码 + numpy → QImage | ~2 ms | 500 fps |

**结论：30 fps @ 1440p 两端都有余量**，实测双端跑同一台机器 24~26 fps、RTT 1 ms。
装了 `dxcam` + 硬件编码时余量更大。

带宽：H.264 下桌面场景约 40 KB/帧 × 30 fps ≈ **10 Mbit/s**。

> **关于"自适应"的一个诚实说明**：画质控制器跑的是**耗时预算**（编码+发送 vs 帧间隔），
> 不是码率预算。但 **H.264 的码率只能在开流时确定**，会话内改要重开编码上下文（丢参考帧、
> 重新发 IDR），代价远大于收益。所以 `quality` 在 H.264 路径上**只在下次开流时生效**，
> 只有 JPEG 回退路径是逐帧生效的。状态栏会同时显示 `q=计算值→生效值`，别被"看起来在
> 自适应"的数字骗了。

## 嵌入自己的 Qt 程序

`RemoteAssistWidget` 是一个自带的 `QFrame`，可以直接塞进已有界面：

```python
from RemoteControl import RemoteAssistWidget, shutdown

panel = RemoteAssistWidget("remote_assist", parent=container, target_fps=30)
container.layout().addWidget(panel)
# 退出前：
shutdown(panel)
```

要接宿主自己的日志体系：

```python
from RemoteControl import use_logger
use_logger(configure_logger())     # 之后记录走宿主的处理器，不会重复落盘
```

配置项（数据目录、窗口图标、历史条数、是否自己管日志）集中在包根
`__init__.py` 顶部的「在这里配置」区块。

内部一律用**相对导入**，且谁都不碰 `sys.path`，所以整棵树挂在一个包前缀下，
`pyinstaller your_host.py` 能原样带走。（写成 `from Recommon.config import ...`
只是"碰巧当前目录在 `sys.path` 上"而已，PyInstaller 不会解析它，然后整包丢掉。）

## 分发与杀软

**未签名的远程控制程序 + SmartScreen/杀软 = 一定会被拦。** 这是这类工具的固有特征，
不是程序有问题。给使用者三条路：

1. **内网自用**：把 `LRDP.exe` 加进杀软白名单（或在 Windows 安全中心的「保护历史记录」
   里点「允许」）。
2. **分发给他人**：用代码签名证书签一遍（`signtool sign /fd SHA256 /tr ... LRDP.exe`），
   签名后 SmartScreen 才会给一次性放行。
3. **拉源码自己编**：`lrdp.spec` 就在仓库里，改完直接 `pyinstaller lrdp.spec`。

另外，发布的 exe 是**单文件无黑框**的，运行时会在同目录创建 `lan_remote_data\`
（验证码、证书、日志）。放进 Program Files 之类需要管理员权限的目录时，
请放到用户可写的地方，或改 `__init__.py` 里的 `DATA_DIR`。

## License

MIT
