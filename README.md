# StreamMedia — 私人流媒体服务器

纯 Python 标准库实现的轻量流媒体服务器：把 `source/` 目录里的视频/音频文件，
以网络串流方式提供给 VLC 等播放器，并提供浏览器客户端门户与管理控制台。

零第三方依赖（Python 3.8+，密码哈希使用 argon2-cffi），无需 Node.js。HLS 直播转码为可选功能（需 ffmpeg）。

## 快速开始

```powershell
# 1. 安装密码哈希依赖（Argon2id）
pip install argon2-cffi

# 2. 生成模拟视频源（可选；也可直接把视频文件拷入 source\）
python tools\generate_samples.py

# 3. 启动服务器
python server.py --port 8000 --admin-user admin --admin-pass 你的管理员密码

# 4. 访问
#    客户端门户  http://localhost:8000/client/   （注册 → 等审批 → 申请限时链接）
#    管理控制台  http://localhost:8000/admin      （媒体管理 / 白名单审批）
```

> 管理员账号首次启动时创建（密码用 Argon2id 哈希存储于 `data/db.json`）；
> 未指定 `--admin-pass` 时默认 `admin123`，生产使用请务必修改。

## 访问控制流程

```
客户端注册  →  状态 pending  →  管理员在控制台审批  →  加入白名单(status=active)
白名单用户登录  →  浏览资源/预览  →  申请限时播放链接(默认30分钟)
链接 URL 粘贴到 VLC 打开网络串流即可播放，到期自动失效，可随时撤销
```

- 媒体播放一律需要**限时链接**（`/play/<id>?token=…`）或管理员会话
- 链接 token 仅在创建时可见一次，服务端只存 SHA-256 哈希
- 用户密码使用 **Argon2id** 哈希（argon2-cffi）
- DLNA 通道（电视/投屏设备）无需登录，属局域网信任模式

## VLC 播放（DLNA / UPnP）

服务器作为 DLNA MediaServer 广播到局域网，电视、手机播放器、VLC 可直接浏览播放：

1. VLC → `播放列表` → 左侧 `本地网络` → `Universal Plug'n'Play`
2. 展开设备 `StreamMedia`，浏览目录并双击播放
3. 或在 VLC 播放地址栏直接输入 `upnp://uuid:设备UUID`（UUID 见管理页面）

> **Windows 注意**：默认保留 Windows SSDP 服务（SSDPSRV）运行，由其缓存我们的
> NOTIFY 通告并代表我们响应 M-SEARCH——AIMP/WMP 等 Windows UPnP 客户端可正常发现。
> 局域网内其他设备（电视/手机）不受影响。同机运行的 VLC 自带 SSDP 栈，与
> SSDPSRV 争用 1900 多播端口可能发现不了设备（Windows 平台限制），此时可用
> `--dlna-auto-fix-ssdp` 停掉 SSDPSRV（需管理员），或改用下方「打开网络串流」方式播放。

## VLC 播放（打开网络串流）

1. 客户端门户或管理控制台申请限时链接并复制
2. VLC → `媒体` → `打开网络串流`（Ctrl+N），粘贴 `http://host:8000/play/<id>?token=…`
3. 点击播放。支持拖动进度条（HTTP Range）；链接到期后需重新申请

局域网内其他设备访问时，把 `localhost` 换成服务器 IP（如 `http://192.168.1.10:8000`）。

## 目录结构

```
├── source\                 # 媒体源目录（模拟视频存储，放视频/音频文件即可）
├── hls\                    # HLS 分片输出（运行时自动创建）
├── data\                   # 用户/会话/限时链接数据库（JSON，运行时自动创建）
├── client\                 # 客户端门户（登录/注册/预览/申请限时链接）
├── static\                 # 管理控制台（含管理员登录页）
├── server.py               # 入口：python server.py [--host] [--port] [--source]
├── streamer\
│   ├── app.py              # HTTP 服务：路由 / 认证 / Range 串流 / REST API
│   ├── auth.py             # Argon2id 密码哈希 + token 工具
│   ├── store.py            # JSON 数据库：管理员/用户白名单/会话/限时链接
│   ├── library.py          # 媒体扫描 + 元数据探测（AVI/WAV/MP4 时长解析）
│   ├── hls.py              # HLS 直播流管理（ffmpeg 子进程，可选）
│   ├── dlna.py             # DLNA/UPnP：SSDP 发现 + ContentDirectory + ConnectionManager
│   └── util.py             # 路径安全 / MIME / ffmpeg 检测
└── tools\generate_samples.py  # 模拟视频源生成器（纯 stdlib）
```

## 功能

| 功能 | 说明 |
| --- | --- |
| 客户端门户 | `/client/` 注册（待审批）/登录/资源预览/申请限时链接/推流密钥/直播大厅 |
| 白名单审批 | `/admin` 控制台：审批注册申请、移出白名单、查看全部链接 |
| 限时链接 | `/play/<id>?token=…`，10 分钟-2 小时可配，到期失效，可直接粘贴 VLC |
| HTTP-TS 推流 | MPEG-TS over HTTP POST 多用户鉴权推流，chunked TS 直播转发 |
| 密码安全 | Argon2id 哈希（argon2-cffi），登录会话 HttpOnly Cookie，token 存 SHA-256 |
| 直连串流 | Range/206 支持，多客户端并发，VLC 拖动进度条 |
| DLNA/UPnP | SSDP 发现、ContentDirectory Browse、ConnectionManager，电视/手机直接浏览 |
| HLS 直播 | `POST /api/streams/<id>` 启动（需 ffmpeg，管理员） |
| 媒体探测 | 纯 Python 解析 AVI/WAV/MP4 容器头，无 ffmpeg 也能显示时长 |
| 安全 | 路径遍历防护；媒体访问需 token；仅读 source 目录 |

## REST API

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/auth/register` | 注册（进入待审批） | 公开 |
| POST | `/api/auth/login` | 登录（仅白名单） | 公开 |
| POST | `/api/auth/logout` | 退出 | 客户端 |
| GET | `/api/auth/me` | 当前会话信息 | 公开 |
| POST | `/api/admin/login` | 管理员登录 | 公开 |
| POST | `/api/admin/logout` | 管理员退出 | 管理员 |
| GET | `/api/admin/users` | 用户列表（待审批/白名单） | 管理员 |
| POST | `/api/admin/users/<u>/approve` | 批准注册 | 管理员 |
| POST | `/api/admin/users/<u>/reject` | 拒绝注册 | 管理员 |
| DELETE | `/api/admin/users/<u>` | 移出白名单 | 管理员 |
| POST | `/api/links` | 申请限时链接 `{media_id, ttl}` 或 `{live_id, ttl}` | 客户端/管理员 |
| GET | `/api/links` | 我的有效链接 | 客户端/管理员 |
| DELETE | `/api/links/<id>` | 撤销链接 | 所有者/管理员 |
| POST | `/api/stream-keys` | 申请推流密钥 `{stream_id, ttl}` | 客户端/管理员 |
| GET | `/api/stream-keys` | 我的推流密钥 | 客户端/管理员 |
| DELETE | `/api/stream-keys/<id>` | 撤销推流密钥 | 所有者/管理员 |
| GET | `/api/live` | 在线直播列表 | 客户端/管理员 |
| POST | `/ingest/<id>?key=…` | HTTP-TS 推流入口 | 有效密钥 |
| GET | `/live/<id>?token=…` | 直播流（chunked TS） | token/管理员 |
| POST | `/api/admin/live/<id>/kick` | 踢出推流端 | 管理员 |
| GET | `/api/admin/links` | 全部有效链接 | 管理员 |
| GET | `/api/videos` | 媒体源列表 | 客户端/管理员 |
| GET | `/api/status` | 服务器状态 | 公开 |
| GET/POST/DELETE | `/api/streams…` | HLS 流管理 | 管理员 |
| GET | `/play/<id>?token=…` | 媒体串流（Range，限时） | token/管理员 |

### UPnP 端点

| 路径 | 说明 |
| --- | --- |
| `/rootDesc.xml` | 设备描述（MediaServer:1） |
| `/upnp/control/contentdirectory` | ContentDirectory SOAP（Browse 等） |
| `/upnp/control/connectionmanager` | ConnectionManager SOAP（GetProtocolInfo 等） |
| `/upnp/scpd/<service>.xml` | 服务 SCPD 文档 |
| `/MediaItems/<id>` | DLNA 媒体资源（支持 Range） |

## 启用 HLS 直播（可选）

检测到 `ffmpeg` 时自动启用 HLS 转码（查找顺序：PATH → `D:\ffmpeg\ffmpeg.exe` →
`C:\ffmpeg\bin\ffmpeg.exe` → WinGet/chocolatey → pip 安装的 imageio-ffmpeg）。
安装方式任选：

```powershell
winget install Gyan.FFmpeg
# 或 pip install imageio-ffmpeg（自动携带静态 ffmpeg 二进制）
```

启动 HLS 后，VLC 打开网络串流粘贴 `http://localhost:8000/hls/<hash>/index.m3u8` 即可。

## 命令行参数

```
python server.py --host 0.0.0.0 --port 8000 --source source --hls-dir hls \
                 --data-dir data --admin-user admin --admin-pass 你的密码 \
                 --device-name StreamMedia [--no-dlna] [--dlna-no-auto-fix]
```

| 参数 | 说明 |
| --- | --- |
| `--host` / `--port` | 监听地址与端口 |
| `--source` / `--hls-dir` | 媒体源目录 / HLS 输出目录 |
| `--data-dir` | 用户与链接数据库目录 |
| `--admin-user` / `--admin-pass` | 管理员账号（首次运行创建，之后以数据库为准） |
| `--device-name` | DLNA 设备友好名称（电视/AIMP 上显示的名字） |
| `--no-dlna` | 禁用 DLNA/UPnP |
| `--dlna-auto-fix-ssdp` | 停止 Windows SSDPSRV 服务（需管理员），启用同机 VLC 发现；但 AIMP 将无法发现服务器。默认保留 SSDPSRV 以支持 AIMP |

## 支持的媒体格式

| 类型 | 扩展名 |
| --- | --- |
| 视频 | `.mp4 .m4v .mkv .webm .avi .mov .flv .wmv .ts` |
| 无损/高保真音频 | `.flac .wav .dsf .dff .dsd .wv .ape` |
| 有损音频 | `.mp3 .aac .ogg .opus .m4a` |
| 字幕 | `.ass .srt` |

> MKV 支持：纯 stdlib 的 EBML/Matroska 解析器提取时长（TimestampScale×Duration）、
> 轨道编码（H.264/HEVC/AV1/VP9/FLAC/AAC/DTS…）、分辨率、采样率与声道，
> 无 ffmpeg 也能完整显示元数据。

> 字幕支持：解析 ASS（Dialogue 时间轴/[Script Info] 标题）与 SRT 时长、自动识别
> 文件名语言标签（`.zh-cn` `.sc` `.tc` `.en` 等）。DLNA 浏览时字幕**挂载到同名视频**
> （额外 `res` 元素 + 三星 `sec:CaptionInfoEx`），不单独列出；网页端可申请限时
> 下载链接（`text/x-ass` / `application/x-subrip`）。

> 高保真支持：FLAC（含 Vorbis Comments 标签）、DSD（DSF/DFF，DSD64-512，含 ID3v2 标签）。
> 服务器自动解析采样率/码率/声道/时长并写入 DIDL（`bitrate`、`sampleFrequency`、
> `nrAudioChannels`、`upnp:artist/album/genre`），AIMP 等客户端可显示完整元数据。

## 直播推流（HTTP-TS / MPEG-TS over HTTP POST）

服务器通过 **HTTP-TS** 协议接收多用户推流：推流端把连续的 MPEG-TS 字节流
POST 到服务器，服务器实时转发给凭限时链接观看的观众（VLC 网络串流）。

### 协议与鉴权流程

```
白名单用户 --POST /api/stream-keys {stream_id, ttl}--> 限时推流密钥(哈希存储)
推流端 -----POST /ingest/<stream_id>?key=<密钥>----------> 服务器(TS环形缓冲, 以最近PAT/关键帧为接入点)
观众 --------POST /api/links {live_id, ttl}-------------> 限时观看链接
观众 --------GET /live/<stream_id>?token=<观看链接>-------> chunked MPEG-TS 实时流(VLC 直接播放)
```

| 特性 | 说明 |
| --- | --- |
| 多用户鉴权 | 推流密钥与观看链接均为**限时 token**（60s-6h），服务端只存 SHA-256 |
| 占用保护 | 同一流名同时只允许一个推流端；密钥绑定流名与用户 |
| 流式缓冲 | 188 字节 TS 包环形缓冲（1.5MB / 20s），支持 chunked/裸流两种 POST |
| 快速接入 | 观众从中途加入时，从缓冲内最近 PAT/随机访问点开始输出 |
| 管理 | 管理员可查看推流统计（速率/观众数）并**踢出**推流端（立即断连） |

### 推流端示例（ffmpeg）

```powershell
ffmpeg -re -i 输入源 -c copy -f mpegts "http://服务器IP:8000/ingest/live1?key=推流密钥"
# 转码推流：ffmpeg -re -i input -c:v libx264 -c:a aac -f mpegts "http://IP:8000/ingest/live1?key=KEY"
```

> VLC 的 access=http 输出为服务器模式，不支持 HTTP-TS POST 推流；
> 推流请用 ffmpeg/OBS(GStreamer 输出) 等支持 HTTP 输出的工具。

## AIMP 播放（DLNA 客户端）

[AIMP](https://www.aimp.ru/) + [aimp_dlna 插件](https://github.com/ArtemIzmaylov/aimp_dlna)：

1. 确保 Windows SSDP 服务（SSDPSRV）正在运行（默认不动它，启动日志会提示状态）
2. AIMP → 音乐库 → **DLNA** → 展开 `StreamMedia` → 双击曲目播放
3. FLAC/DSD 原样传输（不转码），由 AIMP 解码输出

> aimp_dlna 使用 Windows UPnP 框架（UPNPLib）：发现依赖 SSDPSRV 缓存我们的
> NOTIFY 通告；浏览调用 `BrowseDirectChildren`；按 URL 扩展名过滤可播放文件。
> 本服务器已按该插件行为适配（URL 以扩展名结尾、DIDL 提供 size/bitrate/duration
> 及 upnp:artist/album/genre 等字段）。

> **Windows 说明**：默认**保留 SSDPSRV 运行**以支持 AIMP/WMP。
> 如需同机 VLC 发现（VLC 自带 SSDP 栈与 SSDPSRV 冲突），用
> `--dlna-auto-fix-ssdp`（需管理员）停止 SSDPSRV；此时 AIMP 插件将无法发现服务器。
