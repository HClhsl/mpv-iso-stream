# mpv-iso-stream

在 mpv 中直接播放网盘（Alist / 115 等）上的**蓝光原盘 ISO**——不下载、不落盘，
自动解析 BDMV 结构、选择正片播放列表、处理无缝分支，并提供章节菜单。

> Play Blu-ray ISO images hosted on cloud drives directly in mpv via HTTP Range
> requests. The proxy parses the UDF filesystem, MPLS playlists and CLPI EP maps,
> stitches seamless-branching segments into one virtual stream, and exposes
> chapter metadata to an mpv Lua script. Windows 10+ and Python 3.8+ required.

## 功能

- **在线播放，不下载**：通过 HTTP Range 请求按需读取 ISO，只占用元数据带宽（通常 1–2 MiB）即可就绪。
- **BDMV 正道解析**：遍历 UDF 目录树，解析 `BDMV/PLAYLIST/*.mpls`、`CLIPINF/*.clpi`，按播放时长自动选择正片。
- **UDF 2.50 元数据分区**：正确解析元数据分区映射，兼容绝大多数蓝光原盘镜像。
- **无缝分支（seamless branching）**：按 EP map 在访问单元边界裁剪每段 m2ts 并拼接为虚拟连续流，不再漏段或重播。
- **章节菜单**：从 PlayMark 提取章节；播放中按 `Ctrl+M` 打开 uosc 菜单（无 uosc 时退化为 OSD 列表）。
- **容错**：网盘 CDN 返回 403（签名/风控）时自动刷新直链重试；损坏或不支持的镜像自动回退为“最大 m2ts”扫描模式。
- **自检**：内置 `--selftest`，合成 UDF/MPLS/CLPI 镜像离线验证解析与拼接逻辑。

## 工作原理

```
 Alist/115 直链
      │  HTTP Range（1 MiB 块 + 128 KiB 缓存）
      ▼
 iso_stream_proxy.py ── UDF → BDMV → MPLS/CLPI ──► 虚拟连续流（多段 m2ts 拼接）
      │                                                    │
      │  http://127.0.0.1:8080/movie.m2ts                  │ 章节 JSON
      ▼                                                    ▼
    mpv ◄──────────── iso_bd_play.lua / iso_bd_chapters.lua
```

- `iso_stream_proxy.py`：本地 HTTP 代理。解析远端 ISO 的 UDF 文件系统与蓝光导航数据，
  把若干物理区间映射成一个逻辑连续文件，供 mpv 以普通视频方式播放。
- `iso_bd_play.lua`：在 mpv 内按 `Ctrl+b`，读取剪贴板中的 ISO 直链，启动代理并等待就绪后播放。
- `iso_bd_chapters.lua`：检测到代理流后拉取章节元数据，提供 `Ctrl+M` 章节菜单。

## 环境要求

- **Windows 10+**（启动器与剪贴板读取依赖 `curl.exe` 和 PowerShell；代理本身是跨平台的）
- **mpv 0.37+**（开发测试于 0.40；MPV_lazy 集成版可直接使用）
- **Python 3.8+**（MPV_lazy 自带 `python.exe`；独立安装需在 PATH 中可用）
- ISO 需**未加密**（不含 AACS）。加密盘需要商业方案，本项目不支持。
- mpv 需支持 Lua 脚本（官方构建默认启用）。

## 安装

### 方式一：自动安装

1. 下载/克隆本仓库。
2. 双击 `install.bat`，输入包含 `mpv.exe` 的目录（例如 `D:\mpv-lazy-full`）。
   它会把代理、两个 Lua 脚本和 `play-iso.bat` 一起复制过去。
3. 重启 mpv。

### 方式二：手动复制

```
<mpv 根目录>/
├── mpv.exe
├── python.exe                  ← MPV_lazy 自带；独立 mpv 可省略（用 PATH 中的 python）
├── iso_stream_proxy.py         ← 复制到这里
├── play-iso.bat                ← 可选，双击启动器
└── portable_config/
    └── scripts/
        ├── iso_bd_play.lua     ← 复制到这里
        └── iso_bd_chapters.lua ← 复制到这里
```

## 使用

### mpv 内一键播放（推荐）

1. 在 Alist/115 网页上复制 ISO 的直链。
2. 在 mpv 中按 `Ctrl+b`。
3. OSD 显示“正在解析原盘导航数据… N 秒”，完成后自动播放正片。
4. 播放中按 `Ctrl+M` 打开章节菜单。

换片：重新复制链接后再按一次 `Ctrl+b`。

### 双击启动器

双击 `play-iso.bat`（或把链接作为参数：`play-iso.bat "http://..."`）。
解析完成后自动调用同目录/安装目录的 `mpv.exe` 播放。

### 命令行

```bat
python iso_stream_proxy.py "http://.../movie.iso"            :: 起代理并打印播放地址
python iso_stream_proxy.py "http://.../movie.iso" --mpv      :: 起代理并自动拉起 mpv
python iso_stream_proxy.py "http://.../movie.iso" --list     :: 只列出播放列表与章节后退出
python iso_stream_proxy.py "http://.../movie.iso" --playlist 00001
python iso_stream_proxy.py --selftest                        :: 离线自检
```

| 参数 | 说明 |
|---|---|
| `--port PORT` | 本地监听端口，默认 8080 |
| `--playlist 序号\|编号` | 指定播放列表（`--list` 可查看） |
| `--index N` | 回退模式下选择第 N 个大文件 |
| `--log-file PATH` | 实时写日志（排查问题必备） |
| `--mpv` | 就绪后自动启动 mpv（按脚本目录 → 当前目录 → PATH 查找） |
| `--mpv-exe PATH` | 显式指定 mpv 可执行文件路径（配合 `--mpv`） |
| `--selftest` | 运行内置自检，无需网络 |

代理端点：

- `/movie.m2ts`：选定的正片流
- `/bd/<编号>.m2ts`：任意播放列表（`/` 首页有列表）
- `/movie.json`、`/bd/<编号>.json`：章节元数据

## 排查问题

所有环节都会写入日志（`Ctrl+b` 方式默认写到 mpv 根目录的 `iso-play-last.log`）。
典型输出：

```
UDF 分区定位：1.3s（ISO 21.64 GiB）
检测到 UDF 2.50 元数据分区：映射序号 1（物理分区 0），元数据文件 @分区块 0（0.25 MiB）
FSD @ISO 0xA0000，根目录 ICB：分区 1 块 2（元数据分区映射）
UDF 目录遍历：定位到 BDMV（1.8s）
播放列表解析：12 个有效，耗时 3.2s
[就绪] 耗时 11.4s｜HTTP 请求 68 次｜下载 1.86 MiB
```

- **提示 sign 过期 / HTTP 401**：网盘直链有时效，重新复制最新链接再试。
- **日志出现大量“CDN 返回 403，刷新直链”**：网盘在限速，代理会自动退避重试；若持续失败，稍后再试。
- **“未找到 BDMV 结构，使用裸扫描模式”**：镜像不是标准 BDMV，或解析遇到未知布局；裸扫描仍可播放，但没有章节。
- **解析很久**：查看日志各阶段耗时与下载量；正常元数据阶段只有 1–2 MiB。播放中卡顿通常是网盘带宽不足，与解析无关。

## 已知限制

- **不支持蓝光交互菜单**（HDMV/BD-J）。这是 mpv / libbluray 的固有限制，与代理无关；本项目提供章节菜单作为替代。
- **不支持加密 ISO**（AACS/BD+）。
- 启动器与剪贴板功能面向 Windows；代理脚本本身可在其他平台手动使用（需自行调整 `curl -o NUL` 等细节）。

## 开发与自检

```bat
python iso_stream_proxy.py --selftest
```

自检使用符合 ECMA-167 布局的合成镜像，覆盖：

- MPLS PlayItem / PlayMark 解析
- CLPI EP map（PTS ↔ SPN 往返）
- UDF 目录遍历（普通布局 + UDF 2.50 元数据分区布局）
- 虚拟流跨段 Range 拼接与章节 JSON

## 许可证

[MIT](LICENSE)
