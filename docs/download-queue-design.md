# 视频下载队列工具 — 设计文档

日期：2026-06-29

## 背景与目标

现有项目是单次交互式下载脚本（`run_interactive.py`：贴一个链接 → 确认 → 下载 → 退出），无法排队。目标是增加一个**常驻网页前端**，支持：

- 持续追加视频链接，新链接进入等待队列
- 严格串行处理（一次只下一个），前一个下完才下下一个
- 持久化队列，服务重启后未完成任务自动恢复
- 失败任务自动重试，超限后跳过，不阻塞队列
- 像迅雷一样的暂停/继续（单任务 + 整个队列）

使用者只需运行一个启动脚本，浏览器自动打开，全程在网页上操作，不碰其他 `.py`。

## 使用流程

```
运行 run_server.py
  → 后台启动 Flask 服务 + 队列 worker 线程
  → 自动打开浏览器到本地页面（如 http://127.0.0.1:5000）
网页上操作：
  - 粘贴链接 → 加入队列
  - 看队列状态（等待中/下载中/已暂停/已完成/失败）
  - 看实时下载进度
  - 暂停/继续单个任务或整个队列
关闭：关浏览器或 Ctrl+C，队列存到 queue.json，下次启动自动恢复
```

`run_interactive.py`（终端问答式入口）保留，作为不开网页、快速下单个视频的备选，与网页入口共用 `downloader.py`。

## 技术栈

- 后端：Flask（一个轻量依赖）+ 后台 worker 线程
- 前端：单页 `index.html`，原生 JS，无框架，原生 CSS
- 进度推送：SSE（Server-Sent Events）
- 持久化：`queue.json`（本地文件）

选型理由：Flask 是"本地小工具 + 网页"的平衡点，路由/SSE 简洁；纯标准库方案需手写路由/JSON/SSE，冗长易错；FastAPI 与同步 yt-dlp 套用需绕线程池，过度设计。

## 架构与文件结构

```
VideoDownloader/
├── downloader.py          # 现有，核心不动；download() 增加 on_progress 回调入参
├── config.py              # 现有，加队列相关默认值（端口、重试次数、并发等）
├── queue_manager.py       # 新增：队列状态机 + 持久化(queue.json)，加锁
├── worker.py              # 新增：后台线程，串行消费队列、调 downloader
├── server.py              # 新增：Flask 路由 + SSE 进度推送
├── web/
│   └── index.html         # 新增：单页前端（原生 JS + CSS）
├── queue.json             # 运行时生成：持久化队列状态
├── downloads/             # 现有，下载成品
└── run_server.py          # 新增：启动入口（启动服务 + 开浏览器）
```

### 组件职责（边界清晰）

- **queue_manager**：只管队列状态（增/取/标记/暂停/继续）和存盘，不知道怎么下载。所有读写加 `threading.Lock`。
- **worker**：只管"取一个 pending → 调 downloader → 回填结果"，单线程串行。检查全局暂停标志和单任务暂停标志。
- **server**：只管 HTTP 接口和进度推送，不直接下载。
- **downloader**：现有下载逻辑。`download()` 加 `on_progress` 回调参数，透传 yt-dlp 进度；不传该参数时行为不变（兼容 `run_interactive.py`）。

每个组件能独立理解和测试，互不耦合内部实现。

## 任务对象与状态机

### 任务对象

```json
{
  "id": "a3f9",
  "url": "https://example.com/video/...",
  "title": null,
  "status": "pending",
  "progress": 0,
  "speed": null,
  "added_at": 1782745000,
  "retries": 0,
  "error": null
}
```

### 状态机

```
pending ──(worker 取出)──▶ downloading ──(完成)──▶ done
   ▲                          │
   │                          ├──(暂停)──▶ paused ──(继续)──▶ pending
   │                          │
   │                          ├──(失败 & retries<3)──▶ pending  (回队尾重试)
   │                          │
   │                          └──(失败 & retries>=3)──▶ failed
   │
   └──(暂停)──▶ paused ──(继续)──▶ pending
```

状态枚举：`pending`（等待中）、`downloading`（下载中）、`paused`（已暂停）、`done`（已完成）、`failed`（失败）。

### 串行数据流

```
worker 线程循环:
  1. 若全局 queue_paused → 休眠等待，回到 1
  2. 从 queue_manager 取第一个 status=pending 的任务
     若无 → 休眠，等新任务唤醒
  3. 标记为 downloading
  4. 调 downloader.download(url, on_progress=回调)
       ↳ on_progress 定期触发 → 更新该任务 progress/speed → 存盘
       ↳ 回调内检测暂停标志：若该任务被暂停 → 中断下载，标记 paused，不消耗 retries
  5. 成功 → status=done
     失败 → retries<3 则 status=pending（回队尾），retries>=3 则 status=failed
  6. 存盘 queue.json，回到 1
```

## 暂停/继续机制

### 整个队列暂停/继续

- 全局开关 `queue_paused`。worker 每次取下个任务前检查：若暂停则休眠，不开始新任务。
- **当前正在下载的任务不受影响**，会下完。下完后 worker 停住，直到点"继续"。
- 接口：`POST /api/queue/pause`、`POST /api/queue/resume`

### 单个任务暂停/继续

- 暂停"等待中"任务：只标记为 `paused`，不让它开始；继续后回 `pending` 照常下载，无损。
- 暂停"下载中"任务：**立即停止当前下载，已下部分作废**；继续时从头重下。
- 接口：`POST /api/tasks/<id>/pause`、`POST /api/tasks/<id>/resume`

**⚠️ 技术约束**：目标站点通常是 HLS 加密分片流，**不支持断点续传**。暂停下载中的任务 = 从头重下。这是协议限制，无法实现"接着下"。

实现：暂停下载中的任务时，worker 通过进度回调检测到暂停标志，中断 yt-dlp 下载（抛特殊异常），标记为 `paused` 而非 `failed`，不消耗重试次数。

## API 设计

### 任务管理（JSON）

```
POST   /api/tasks            { url }          → 加入队列，返回 task
GET    /api/tasks                             → 返回整个队列
DELETE /api/tasks/<id>                        → 删除任务（任意状态）
POST   /api/tasks/<id>/retry                  → 手动重试 failed 任务（retries 归零，回 pending）
POST   /api/tasks/<id>/pause                  → 暂停任务
POST   /api/tasks/<id>/resume                 → 继续任务
```

### 全局控制

```
POST   /api/queue/pause                       → 暂停整个队列
POST   /api/queue/resume                      → 继续整个队列
GET    /api/queue/state                       → 返回 { paused: bool }
```

### 实时进度推送（SSE）

```
GET /api/events  (text/event-stream)
  → 队列任何变化时推送：
     data: { "tasks": [整个队列快照], "paused": false }
  → 前端 EventSource 监听，收到即重渲染
```

推送频率：yt-dlp progress hook 每 10 秒报一次进度（沿用现有 `_make_progress_hook` 节奏），状态变更即时推。

### 静态页面

```
GET / → web/index.html
```

## 前端界面

单页，自上而下三块：

1. **顶栏**：标题 + `[⏸ 暂停全部 / ▶ 继续]` 全局开关
2. **输入区**：链接输入框 + `[加入队列]` 按钮（空链接前端拦截不发请求）
3. **队列区**：按状态分组展示，可折叠
   - ▼ 下载中（置顶）：标题 + 进度条（百分比/速度/剩余时间）+ `[⏸][✕]`
   - ▼ 等待中：URL + `[⏸][✕]`
   - ▼ 已暂停：URL + `[▶][✕]`
   - ▼ 已完成（默认折叠）：标题 + `[✕]`
   - ▼ 失败（默认折叠）：标题 + 重试次数 `重试2/3` + `[↻重试][✕]`

### 按钮按状态显示

| 状态 | 按钮 |
|------|------|
| 下载中 | `[⏸暂停] [✕删除]` |
| 等待中 | `[⏸暂停] [✕删除]` |
| 已暂停 | `[▶继续] [✕删除]` |
| 已完成 | `[✕删除]` |
| 失败 | `[↻重试] [✕删除]` |

### 样式

原生 CSS，浅色简洁风。状态色：下载中=蓝、等待=灰、暂停=黄、完成=绿、失败=红。SSE 断开时顶栏显示"连接中断，重连中…"，自动重连。

## 错误处理与边界

- **非目标站点链接**：允许入队，走 yt-dlp 通用逻辑，失败按重试→跳过处理，不崩服务。
- **重复链接**：允许重复入队，不去重。
- **下载失败分类**：提取失败 / 下载中断（代理抖动、SSL EOF、片段 403）都算一次失败，retries+1；yt-dlp 内部已重试 10 次仍失败才算一次。重试 3 次仍失败 → `failed`，不阻塞，可手动重试。
- **手动重试**：`failed` → `pending`，retries 归零，回队尾。
- **启动恢复**：读 `queue.json`，所有 `downloading` 改回 `pending`（上次中断需重下），worker 自动接着干。
- **优雅关闭**（Ctrl+C）：等当前片段写完，停 worker，存盘退出。
- **queue.json 损坏**：捕获异常，备份为 `queue.json.bak`，从空队列重启。
- **端口冲突**：默认 `127.0.0.1:5000`，被占用则自动找下一个可用端口（5001、5002…），启动日志和页面显示实际地址。
- **并发安全**：worker 线程与 Flask 请求线程都读写队列 → `threading.Lock` 保护；SSE 事件用线程安全队列传递。

## 配置项（config.py 新增）

```python
QUEUE_FILE = "queue.json"          # 队列持久化文件
MAX_RETRIES = 3                    # 单任务最大自动重试次数
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5000                 # 被占用时自动递增
```

## 不做的事（YAGNI）

- 不做用户认证（本地工具，只绑 127.0.0.1）
- 不做下载历史/统计页面
- 不做断点续传（HLS 协议限制）
- 不做去重
- 不做多用户/多队列
