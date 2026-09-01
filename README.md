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

> **Windows 注意**：Windows 自带 SSDP 服务（SSDPSRV）会独占 1900 多播端口，
> 导致 DLNA 发现失败。以管理员身份运行服务器时会自动停止该服务；
> 否则请手动执行 `net stop SSDPSRV`（恢复：`net start SSDPSRV`）。
> 局域网内其他设备（电视/手机）不受影响；同机运行的 VLC 与服务器争用多播端口，
> 可能发现不了设备，属 Windows 平台限制，此时请用下方「打开网络串流」方式播放。

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
| 客户端门户 | `/client/` 注册（待审批）/登录/资源预览/申请限时链接/撤销 |
| 白名单审批 | `/admin` 控制台：审批注册申请、移出白名单、查看全部链接 |
| 限时链接 | `/play/<id>?token=…`，10 分钟-2 小时可配，到期失效，可直接粘贴 VLC |
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
| POST | `/api/links` | 申请限时链接 `{media_id, ttl}` | 客户端/管理员 |
| GET | `/api/links` | 我的有效链接 | 客户端/管理员 |
| DELETE | `/api/links/<id>` | 撤销链接 | 所有者/管理员 |
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

检测到 `ffmpeg` 在 PATH 中时自动启用；未安装则直连串流不受影响。
安装方式任选：

```powershell
winget install Gyan.FFmpeg
# 或手动下载放到 C:\ffmpeg\bin\ffmpeg.exe
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
| `--device-name` | DLNA 设备友好名称（电视上显示的名字） |
| `--no-dlna` | 禁用 DLNA/UPnP |
| `--dlna-no-auto-fix` | 不自动停止 Windows SSDPSRV 服务 |

## 支持的媒体格式

| 类型 | 扩展名 |
| --- | --- |
| 视频 | `.mp4 .m4v .mkv .webm .avi .mov .flv .wmv .ts` |
| 音频 | `.mp3 .wav .flac .aac .ogg .m4a` |

> 提示：`source/` 目录支持子文件夹；放入新文件后在管理页面点「刷新」即可看到。
