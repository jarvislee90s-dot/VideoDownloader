# 设计：微信视频号下载支持（wechat 模块）

日期：2026-09-14
状态：已获用户确认（需求澄清 6 问全部拍板，7 节设计逐节通过）

## 背景与目标

VideoDownloader 现支持通用站点（yt-dlp）、B站（curl_cffi 专用模块）、某加密 HLS 目标站点。本设计新增**微信视频号（channels.weixin.qq.com）**下载，集成进现有下载队列（贴链接 → 排队 → 串行下载 → 网页看进度）。

验收标准（用户确认）：
1. 示例链接 `channels.weixin.qq.com/finder-preview/pages/sph?id=AZrL4kL5m9` 能下载出可正常播放的 mp4，且队列网页显示实时进度
2. 两种链接形态（finder-preview 带 id、`/sph/` 短链）都能解析并下载
3. 未登录时任务失败信息明确指引用户扫码登录，而非未知错误

## 研究结论（支撑本设计的事实，均已实测/源码验证）

- yt-dlp master（2026-09）extractor 列表中 weixin/wechat 零命中，无现成 extractor 可用
- finder-preview 页面为 ~2.4KB JS 空壳（`div#app`），媒体地址全部来自运行时接口
- 取流接口：POST `https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info`，body `{"baseReq":{"generalToken":"<token>"},"shortUri":"<id>"}`（`/sph?id=` 走 shortUri，exportId 形态走 `exportId` 字段）。实测：**不带登录态返回 401 "permission verification failed"**；带同源 cookies 的普通 POST 即可（无签名头）——证据：nobiyou/wx_channel `internal/assets/inject/api_client.js` 的 `fetchSharedFeedInfo` 用浏览器原生 `fetch(..., {credentials:'include'})` 调用同一接口
- token 来源：URL 参数 `token` 或页面 cookie；generalToken 传空串时 wx_channel 也走同一路径（其默认实现传空串）
- 响应体（防御式解析，字段名来自 wx_channel `buildSharedFeedCompatResponse` 与前端 bundle 实测）：
  - `data.feedInfo.description`（标题）、`durationMs`/`videoDuration`（时长）
  - `data.feedInfo.h264VideoInfo.videoUrl`、`h265VideoInfo.videoUrl`、`videoUrl`（取流优先级 h264 > videoUrl > h265）
  - `data.feedInfo.decodeKey`（**加密标记**，部分视频流加密）
  - `data.authorInfo.nickname`（作者）、`coverUrl`/`thumbUrl`（封面）
- **加密风险（探索笔记遗漏、本设计补上）**：wx_channel `internal/assets/inject/decrypt.js` 证实部分视频流经 ISAAC64 PRNG 生成 128KB 密钥流做 XOR 加密（官方 WASM：`wasm_video_decode.wasm`），密钥 seed 与接口响应 `decodeKey` 关联。不解密则下载文件无法播放。**解密算法已有完整可翻译源码**（见下方"可抄实现对照表"）：wx_channel `pkg/util/isaac64.go`（201 行 Go，ISAAC64 完整实现）+ `internal/utils/crypto_helper.go`（`ParseKey`：decodeKey 十进制字符串直接 `ParseUint` 转 uint64 seed，无额外哈希/变换；`DecryptFileInPlace`：只解密文件头 128KB；`looksLikeMediaHeader`：解密校验 ftyp/styp/moov/mdat 四魔数）
- **加密区长度语义**：两处源码有差异——JohnABC/WechatSphDecrypt `download.go` 用 HTTP 响应头 `X-enclen` 指定加密长度；wx_channel（2026-09 仍在更新，v5.7.9）用固定 131072（128KB）。实现时优先读 CDN 响应头 `X-enclen`，缺失则取 131072。注：wx_channel `pkg/decrypt/decrypt.go` 头部注释标明其代码源自 Hanson/WechatSphDecrypt（原 repo 已删，JohnABC fork 仍在），两份 Go 实现互为印证
- GitHub 四大流派（res-downloader 19.8k★ MITM 代理、ltaoo/wx_channels_download 9.3k★ 代理注入微信PC、qiye45/wechatVideoDownload 5.8k★ mitmdump 监听、nobiyou/wx_channel 2.6k★ 注入微信PC）均依赖装证书或微信 PC 客户端，无一支持纯分享链接下载，故走自研接口路线

## 用户决策记录

| 问题 | 决策 |
|------|------|
| 集成形态 | 集成进 VideoDownloader 队列（非独立脚本） |
| 登录态 | browser_cookie3 读 Chrome 登录 cookies + curl_cffi 重放（与 B站 方案同构） |
| 链接形态 | finder-preview 短链页 + `/sph/` 短链两种（本期不做作者主页批量） |
| 加密流 | 完整支持：下载 + 自动解密 |
| 风控降级 | 单一策略：提示扫码登录（不做多级降级链） |
| 代码架构 | B站式专用模块（不做插件化重构，不做 yt-dlp extractor） |

## 架构

```
video_downloader/
├── bilibili.py        # 不动
├── wechat.py          # 新增 ~300 行：链接解析 + cookies + 取流接口 + 下载
├── wechat_decrypt.py  # 新增 ~120 行：Isaac64 PRNG + XOR 解密（独立模块便于单测）
├── downloader.py      # 仅改 2 处：prefetch_meta() 与 download() 各加一个 wechat 分支（与 bilibili 分支并列）
├── config.py          # 新增常量：WECHAT_COOKIE_BROWSER = "chrome"、WECHAT_FEED_API_URL
└── web/index.html     # 不动（错误信息通过现有失败态展示，无新 UI）
```

分发模式与现有 B站 完全一致：`_is_wechat_url(url)` 谓词命中走专用分支，未命中走 yt-dlp。worker、queue_manager、server 全部不动。

### wechat.py 职责

```
_is_wechat_url(url)            # 匹配 channels.weixin.qq.com / weixin.qq.com/sph
_extract_short_uri(url)        # 两种形态 → 统一 shortUri
_get_cookies()                 # browser_cookie3 读 chrome 的 weixin.qq.com/qq.com 域
_get_feed_info(short_uri)      # curl_cffi Session(impersonate='chrome')，
                               #   先 GET finder-preview 页面暖场拿 cookies，
                               #   再 POST feed API，返回解析后的 FeedInfo dataclass
prefetch_meta(url, on_meta)    # 接口里直接有标题/时长/大小字段，供队列等待态显示
download(url, output_path, on_progress, on_meta)
                               # curl_cffi stream 下载 + Referer 头 + .part 临时文件
                               #   → 需要(decodeKey 存在)则调 wechat_decrypt
                               #   → 校验 ftyp 魔数 → 改名最终文件
```

### wechat_decrypt.py 职责

逐行翻译 wx_channel `pkg/util/isaac64.go`（Go → Python，算法完全确定，无需逆向试探）：

- `class Isaac64`：`randrsl[256]`、`randcnt`、`mm[256]`、`aa/bb/cc` 状态；`randinit(seed)`（golden 常量 `0x9e3779b97f4a7c13`，mix 8 变量混合，两轮 mm 填充）、`isaac64()`（`j%4` 分支的移位/取反 + `mm[(x>>3)%256]` 查表）、`generate(length)`（每轮 `randcnt--` 取 `randrsl[randcnt]`，uint64 按 8 字节小端拆分后**反转字节序**输出——这是两份 Go 源码一致的关键细节，对应 `__wx_channels_decrypt` 里 `decryptor_array.set(r.reverse())`）
- `decrypt_file(src, dst, decode_key: str) -> bool`：`ParseKey` 语义（十进制字符串 → `int`，非法则报错）；生成 131072 字节密钥流，对文件头部 XOR（加密区长度优先取 CDN 响应头 `X-enclen`，缺失用 131072）；头部校验 `looksLikeMediaHeader` 语义（前 32 字节内出现 ftyp/styp/moov/mdat 任一即通过）
- 纯函数无网络依赖，可用固定 seed + 已知明文做单测

### 数据流

```
用户贴链接 → queue_manager 入队 → worker 取任务
  → prefetch_meta（异步）：POST feed API → on_meta(title, duration, filesize)
  → download：
      1. 读 Chrome cookies → POST get_feed_info（401 → 单一降级，见错误处理）
      2. 选流：h264 > videoUrl > h265
      3. curl_cffi stream 下载到 <title>.mp4.part（进度回调 percent/speed/eta）
      4. decodeKey 存在 → wechat_decrypt 解密 → ftyp 校验
      5. 改名 <slugified title>.mp4 → on_meta(filesize=磁盘真实大小)
```

文件名沿用 B站 `_slugify` 规则（非法字符替换、80 字符截断）。

### 与现有代码的接口核对（自审结论）

- worker 注入的 `download_fn(url, on_progress, on_meta) -> title`（`worker.py:16`）→ `downloader.download()` 签名完全匹配，wechat 分支在 `downloader.download()` 内部实现，worker 不感知
- `on_progress(percent, speed, eta)` 高频回调由 worker 自行节流（`worker.py:73-77` 每秒一次），wechat 下载回调无需自己节流
- `on_meta(title/duration/filesize)` 任意子集多次调用由 `queue_manager.set_meta` 支持（只更新非 None 字段，`queue_manager.py:108-116`），prefetch 阶段先给 title/duration、下载阶段再补 filesize 的时序安全
- 队列任务失败走 `mark_failed_retry`（自动重试 3 次）→ 登录态缺失类错误会自动重试 3 次再进 failed 态，这是现有全局行为，不为本功能特判
- browser_cookie3 已是 requirements.txt 依赖且 bilibili.py 已有 `_load_browser_cookies` 成熟实现（含 chrome/firefox/safari/edge loader 表），wechat.py 直接复用该模式
- config.py 新增 `WECHAT_COOKIE_BROWSER = "chrome"` 与 `WECHAT_FEED_API_URL` 两个常量，命名与现有 `DEFAULT_*`/`SITE_PROXY_MAP` 风格一致
- URL 谓词互斥性：`_is_wechat_url` 匹配 `channels.weixin.qq.com` 与 `weixin.qq.com/sph`，与 `_is_bilibili_url`（bilibili.com/b23.tv）、`TARGET_SITE_DOMAIN`（91nt.com）无交集，downloader.py 中三个分支按 B站 → wechat → 目标站点顺序检查互不干扰

### 可抄实现对照表（spec 中每个方法的外部证据）

| 本设计方法 | 前期探测证据 / 可抄源码 |
|------|------|
| `_is_wechat_url` / `_extract_short_uri` | wx_channel `internal/assets/inject/api_client.js:86-96`（shortUri 从 `/sph/` 末段或 `?id=` 提取）；本仓库 bilibili.py `_is_bilibili_url` 同构 |
| `_get_cookies` | bilibili.py:93-114 `_load_browser_cookies` 现成实现（browser_cookie3 已在 requirements.txt） |
| `_get_feed_info`（POST get_feed_info） | 本地实测：无登录态 401（接口存在、无签名头）；wx_channel `api_client.js:145-170` `fetchSharedFeedInfo`（body 结构、credentials:'include' 语义） |
| 响应字段解析 | wx_channel `api_client.js:234-262` `buildSharedFeedCompatResponse`（h264/h265/videoUrl 优先级、decodeKey、durationMs、authorInfo 字段名全部来自该源码）；前端 bundle feed.408a968c.js 实测同样字段 |
| `_slugify` | bilibili.py:21-24 现成实现，直接复用 |
| 流下载 + 进度回调 | bilibili.py:170-208 `_download_stream`（curl_cffi stream + percent/speed/eta 回调）同构改造（加 Referer 头） |
| `Isaac64` PRNG | wx_channel `pkg/util/isaac64.go` 全文 201 行 Go 源码（已逐行读过），算法确定可翻译 |
| `ParseKey`（decodeKey→seed） | wx_channel `crypto_helper.go:99-104`：十进制字符串 `ParseUint` 直接转 uint64，**无额外变换**——原 spec 标注的"唯一边做边试风险点"被此源码消除 |
| 加密区长度 | JohnABC/WechatSphDecrypt `download.go:26`（HTTP 头 `X-enclen`）与 wx_channel `crypto_helper.go:80`（固定 131072）两处证据，实现时优先 X-enclen |
| 解密校验（ftyp） | wx_channel `crypto_helper.go:106-129` `looksLikeMediaHeader`（ftyp/styp/moov/mdat 四魔数，前 32 字节窗口） |

## 错误处理（单一降级哲学，所有信息走现有任务失败态展示）

| 场景 | 任务失败信息 |
|------|------|
| browser_cookie3 未安装 / 读不到 cookies | 未检测到 Chrome 登录态，请先用 Chrome 打开 channels.weixin.qq.com 扫码登录，然后点重试 |
| feed API 401 / errCode≠0（含"permission verification failed"） | 同上（提示扫码登录后重试） |
| 响应无任何 videoUrl | 该内容可能是图片/图文类型，暂不支持下载 |
| CDN 下载 403 | 登录态可能已失效，请重新扫码登录后重试 |
| 解密后 ftyp 校验失败 | 保留密文文件（改名 .encrypted 便于诊断），报"解密失败，文件已保留待排查" |

## 测试策略

- 单测（无网络，pytest，与现有 tests/ 风格一致）：
  - 链接解析：两种形态各一个用例 + 非视频号链接负例
  - feed 响应解析：构造 JSON 样本（含 h264/h265/缺失字段变体）验证防御式取值
  - 解密函数：固定 seed 生成密钥流，XOR 已知明文/密文往返验证；ftyp 校验正负例
- 手工验收：用户扫码登录 Chrome 后，用示例链接跑通三条验收标准

## 已知风险与对策

1. ~~Isaac64 seed 转换细节~~ **已消除**：自审中找到 wx_channel `crypto_helper.go` `ParseKey`（decodeKey 十进制串直接转 uint64）与 `pkg/util/isaac64.go` 完整 Go 实现，解密算法从"边做边试"变为"逐行翻译"，剩余工作只是 Go→Python 语法转换 + 用已知样本回归
2. **腾讯风控升级**：若未来校验 UA/IP 与浏览器一致性，curl_cffi 重放可能失效。对策：impersonate='chrome' + 同源暖场请求已最大化模拟；真失效时升级到内置 Playwright 持久化浏览器方案（已在需求澄清中排除为非本期，留作二期）
3. **generalToken**：wx_channel 默认传空串可用；若实测要求非空，从页面 cookie 的 token 字段取（前端 bundle 证实 `se().token || Qa("token")` 的取值链），实现中预留该取值路径
4. **X-enclen 与 128KB 语义差异**：老实现读响应头、新实现固定 128KB。对策：优先读 `X-enclen`，缺失回退 131072；解密校验（魔数）失败时保留密文，两种长度语义不会导致不可诊断的坏文件
