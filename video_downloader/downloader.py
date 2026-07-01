import os
import re
import time
import html
import json
import base64
import urllib.request
import urllib.parse
import yt_dlp
from video_downloader.config import (RESOLUTION_FORMATS, DEFAULT_RESOLUTION, DEFAULT_OUTPUT_DIR, DEFAULT_PROXY, SITE_PROXY_MAP, TARGET_SITE_DOMAIN)
from video_downloader import bilibili


def _get_proxy_for_url(url: str, user_proxy: str = None) -> str:
    """根据 URL 获取代理地址：用户指定 > 站点配置 > 默认"""
    if user_proxy is not None:
        return user_proxy
    for domain, proxy in SITE_PROXY_MAP.items():
        if domain in url:
            return proxy
    return DEFAULT_PROXY


def _fetch_page(url: str, proxy: str = None) -> str:
    """获取网页内容"""
    proxy_handler = urllib.request.ProxyHandler({
        'http': proxy or '',
        'https': proxy or ''
    }) if proxy else urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)
    opener.addheaders = [('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36')]
    resp = opener.open(url, timeout=30)
    return resp.read().decode('utf-8', errors='replace')


_PACKER_RE = re.compile(r"}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\)", re.DOTALL)


def _unpack_packer(block: str) -> str:
    """解码 Dean Edwards packer (eval(function(p,a,c,k,e,d){...}))，返回还原后的 JS 源码"""
    m = _PACKER_RE.search(block)
    if not m:
        return ''
    p, a, c, k = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split('|')
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"

    def decode(word: str) -> int:
        n = 0
        for ch in word:
            n = n * a + digits.index(ch)
        return n

    def repl(mo):
        word = mo.group(0)
        try:
            n = decode(word)
        except ValueError:
            return word
        return k[n] if 0 <= n < len(k) and k[n] else word

    return re.sub(r'\b\w+\b', repl, p)


def _extract_site_video_url(url: str, proxy: str = None) -> tuple:
    """从目标站点文章页提取视频真实地址，返回 (title, video_url)

    页面结构（2026 起）：文章正文里有一段 Dean Edwards packer，解包后 document.write
    一个 <script src="/videos/melon_detail_play.js?...">；该 JS 又是一段 packer，
    解包后写入 <div class="ql-video-mse" data-url="<m3u8>">。这里复刻该流程拿到 m3u8。
    """
    content = _fetch_page(url, proxy)

    # 标题：优先 JSON-LD headline，回退到 <title>
    title = None
    json_ld = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', content, re.DOTALL)
    if json_ld:
        try:
            data = json.loads(json_ld.group(1))
            title = data.get('headline') or data.get('name')
        except (json.JSONDecodeError, ValueError):
            pass
    if not title:
        tm = re.search(r'<title>(.*?)</title>', content, re.DOTALL)
        if tm:
            title = tm.group(1).strip()

    # 第一层 packer：解包后是 document.write("<script src=\"<path>?<静态参数>&u="
    #   +encodeURIComponent("<token>")+"&t="+时间桶>)。不同页面（/posts/ 用
    #   melon_detail_play.js，/videos/ 用 detail_play.js 且带 id/img/ads）静态参数不同，
    #   这里统一提取「路径+静态参数前缀（以 u= 结尾）」和 token，再拼上时间桶。
    # 注意解包后的 JS 字面量里 src= 后有数量不定的转义反斜杠，故只用不含引号的稳定片段匹配。
    unpacked = _unpack_packer(content)
    prefix_match = re.search(r'(/videos/[^"]*?u=)', unpacked)
    token_match = re.search(r'encodeURIComponent\("([^"]+)"\)', unpacked)
    if not (prefix_match and token_match):
        return (title, None)
    play_prefix = prefix_match.group(1)     # 如 /videos/detail_play.js?id=46780&...&u=
    token = token_match.group(1)
    t_bucket = int(time.time()) // 1000 // 1800  # 与站点一致的 30 分钟时间桶
    play_js_url = f'https://{TARGET_SITE_DOMAIN}' + play_prefix + urllib.parse.quote(token) + f'&t={t_bucket}'

    # 第二层 packer：解包后写入 <div class="ql-video-mse" ...>，含若干 data-* 属性。
    # 注意解包后的 JS 字面量里引号前有数量不定的转义反斜杠，data-* 值里 & 写成 &amp; 实体。
    play_js = _fetch_page(play_js_url, proxy)
    play_unpacked = _unpack_packer(play_js)

    def attr(name: str) -> str:
        m = re.search(rf'{name}=\\*"([^"\\]*)', play_unpacked)
        return html.unescape(m.group(1)) if m else ''

    data_url = attr('data-url')
    data_api = attr('data-api')
    data_key = attr('data-key')
    data_iv = attr('data-iv')

    # 新流程（player3）：data-url 是带坏 auth_key 的诱饵，真实地址需请求 data-api，
    # 返回 JSON {data: <base64密文>}，用 AES-128-CBC 解密得到 m3u8。
    # 密钥来源：部分页面（/posts/）在 data-key/data-iv 里嵌入真密钥（16字节），
    # 另一些（/videos/ 老视频）只放占位符（如 "A"/"C"），此时用站点固定密钥。
    _FIXED_KEY = b'ad9972b0430a186e'
    _FIXED_IV = b'f18ae198ecd2efa0'
    key = data_key.encode() if len(data_key) == 16 else _FIXED_KEY
    iv = data_iv.encode() if len(data_iv) == 16 else _FIXED_IV

    if data_api:
        try:
            from Cryptodome.Cipher import AES
            from Cryptodome.Util.Padding import unpad
            api_body = _fetch_page(f'https://{TARGET_SITE_DOMAIN}' + data_api, proxy)
            payload = json.loads(api_body).get('data', '')
            cipher = AES.new(key, AES.MODE_CBC, iv)
            decrypted = unpad(cipher.decrypt(base64.b64decode(payload)), AES.block_size)
            return (title, decrypted.decode('utf-8', 'replace'))
        except Exception:
            # 解密失败则回退到 data-url（旧站点/旧页面可能仍可直接用）
            pass

    # 旧流程：data-url 即可直接下载的 m3u8
    if data_url:
        return (title, data_url)

    return (title, None)


def _is_target_site_url(url: str) -> bool:
    return TARGET_SITE_DOMAIN in url


def _format_bytes(n: float) -> str:
    if n is None or n <= 0:
        return "??"
    for unit in ['B', 'KiB', 'MiB', 'GiB']:
        if n < 1024:
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}TiB"


def _format_seconds(s: float) -> str:
    if s is None or s < 0:
        return "??:??:??"
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def _make_progress_hook(interval: int = 10):
    """创建进度回调，每 interval 秒打印一行进度"""
    last_log = [0]

    def hook(d):
        if d['status'] == 'downloading':
            now = time.time()
            if now - last_log[0] < interval:
                return
            last_log[0] = now
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes', 0)
            speed = d.get('speed', 0)
            eta = d.get('eta')
            total_str = _format_bytes(total_bytes) if total_bytes else '??'
            down_str = _format_bytes(downloaded)
            speed_str = _format_bytes(speed) + '/s' if speed else '??'
            eta_str = _format_seconds(eta) if eta is not None else '??:??:??'
            if total_bytes:
                pct = f"{downloaded / total_bytes * 100:.1f}%"
                print(f"  {pct}  已下载: {down_str}/{total_str}  速度: {speed_str}  预计剩余: {eta_str}")
            else:
                print(f"  已下载: {down_str}  速度: {speed_str}  预计剩余: {eta_str}")
        elif d['status'] == 'finished':
            total_bytes = d.get('total_bytes')
            print(f"  下载完成! 总大小: {_format_bytes(total_bytes)}")
    return hook


class _NoopLogger:
    """抑制 yt-dlp 的所有日志输出"""
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def list_formats(url: str, proxy: str = None):
    """列出网站提供的所有可用视频格式/分辨率"""
    effective_proxy = _get_proxy_for_url(url, proxy)
    if _is_target_site_url(url):
        title, video_url = _extract_site_video_url(url, effective_proxy)
        print(f"\n标题: {title or '未知'}")
        if video_url:
            print(f"视频地址: {video_url}")
            ydl_opts = {"proxy": effective_proxy, "quiet": False, "no_warnings": False}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=False)
                info = ydl.sanitize_info(info)
                print(f"时长: {info.get('duration')} 秒")
                print(f"\n可用格式:")
                for f in info.get("formats", []):
                    h = f.get("height")
                    ext = f.get("ext")
                    vcodec = f.get("vcodec", "none")
                    acodec = f.get("acodec", "none")
                    if h:
                        print(f"  {f['format_id']}: {h}p, ext={ext}, v={vcodec}, a={acodec}")
        return

    ydl_opts = {
        "proxy": effective_proxy,
        "cookies_from_browser": ("safari",),
        "quiet": False,
        "no_warnings": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        info = ydl.sanitize_info(info)
        print(f"\n标题: {info.get('title')}")
        print(f"时长: {info.get('duration')} 秒")
        print(f"\n可用格式:")
        for f in info.get("formats", []):
            h = f.get("height")
            ext = f.get("ext")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            if h:
                print(f"  {f['format_id']}: {h}p, ext={ext}, v={vcodec}, a={acodec}")
    return info


def _probe_meta(url: str, proxy: str = None) -> dict:
    """extract_info(download=False) 取元数据（不下载媒体），返回 dict 或 None。

    用于下载前获取时长/总大小/标题，让队列在下载过程中显示这些信息。
    HLS m3u8 的时长通常能从 EXTINF 求和得到；总大小对 HLS 常为未知（返回 None）。
    """
    opts = {
        "proxy": proxy,
        "quiet": True,
        "no_warnings": True,
        "no_progress": True,
        "logger": _NoopLogger(),
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        return None


def _filesize_of(info: dict):
    """从 extract_info 结果里尽量取出总字节数；未知返回 None。"""
    if not isinstance(info, dict):
        return None
    if info.get("filesize"):
        return info["filesize"]
    if info.get("filesize_approx"):
        return info["filesize_approx"]
    # 单流无总大小时，尝试从 formats 求和（取最大候选）
    sizes = [f.get("filesize") or f.get("filesize_approx") for f in info.get("formats", [])]
    sizes = [s for s in sizes if s]
    return max(sizes) if sizes else None


def prefetch_meta(url: str, proxy: str = None, on_meta=None):
    """在任务加入队列后、开始下载前，异步获取并更新标题/时长/总大小。

    让等待中的任务也能显示正确信息。失败则静默忽略，不影响后续下载。
    """
    if not on_meta:
        return
    effective_proxy = _get_proxy_for_url(url, proxy)

    # B站：专用元数据获取
    if bilibili._is_bilibili_url(url):
        try:
            bilibili.prefetch_meta(url, on_meta=on_meta)
        except Exception:
            pass
        return

    # 目标站点：从页面提取标题，再探测 m3u8 拿时长/总大小
    if _is_target_site_url(url):
        try:
            title, video_url = _extract_site_video_url(url, effective_proxy)
            if video_url:
                meta = _probe_meta(video_url, effective_proxy)
                on_meta(title=title or None,
                        duration=(meta or {}).get("duration"),
                        filesize=_filesize_of(meta))
        except Exception:
            pass
        return

    # 通用站点：yt-dlp 取元数据
    try:
        meta = _probe_meta(url, effective_proxy)
        if isinstance(meta, dict):
            on_meta(title=meta.get("title"),
                    duration=meta.get("duration"),
                    filesize=_filesize_of(meta))
    except Exception:
        pass


def download(url: str, resolution: str = DEFAULT_RESOLUTION, output_dir: str = DEFAULT_OUTPUT_DIR, proxy: str = None, on_progress=None, on_meta=None):
    """下载视频。

    on_progress: 可选回调，签名为 on_progress(percent: float, speed: float, eta: float)。
                 percent 为 0-100。供队列 worker 透传进度用。不传则忽略。
    on_meta: 可选回调，签名为 on_meta(title=None, duration=None, filesize=None)。
             在得知视频标题/时长/总大小后调用（可能多次，随信息逐步得知），
             让队列在下载过程中就显示视频名与这些信息。不传则忽略。
    提取失败时抛 RuntimeError，便于调用方捕获。
    """
    effective_proxy = _get_proxy_for_url(url, proxy)

    # B站：yt-dlp 直接请求会 412，使用 curl_cffi 绕过风控
    if bilibili._is_bilibili_url(url):
        os.makedirs(output_dir, exist_ok=True)
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', 'video')
        out_path = os.path.join(output_dir, f"{safe_title}.mp4")
        print(f"\n正在下载: B站视频")
        try:
            title = bilibili.download(
                url,
                output_path=out_path,
                on_progress=on_progress,
                on_meta=on_meta,
            )
            if title:
                new_path = os.path.join(output_dir, f"{re.sub(r'[<>:"/\\|?*]', '_', title)}.mp4")
                os.replace(out_path, new_path)
                out_path = new_path
            # 下载完成后用真实文件大小覆盖前期估算
            if on_meta and os.path.exists(out_path):
                on_meta(filesize=os.path.getsize(out_path))
            return title
        except Exception as e:
            raise RuntimeError(f"B站下载失败：{e}")

    if _is_target_site_url(url):
        title, video_url = _extract_site_video_url(url, effective_proxy)
        if not video_url:
            raise RuntimeError("无法从该页面提取视频地址")
        download_url = video_url
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title or 'video')
        outtmpl = os.path.join(output_dir, f"{safe_title}.%(ext)s")
        fmt = "best"
        # 目标站点标题下载前已知；再探测 m3u8 拿时长/总大小，一并通知队列
        meta = _probe_meta(video_url, effective_proxy)
        if on_meta:
            on_meta(title=title or None,
                    duration=(meta or {}).get("duration"),
                    filesize=_filesize_of(meta))
    else:
        download_url = url
        outtmpl = os.path.join(output_dir, "%(title)s.%(ext)s")
        fmt = RESOLUTION_FORMATS.get(resolution, RESOLUTION_FORMATS[DEFAULT_RESOLUTION])
        title = None

    os.makedirs(output_dir, exist_ok=True)
    print(f"\n正在下载: {title or '视频'}")

    console_hook = _make_progress_hook()
    pushed_size = [None]  # 已推送过的总大小估算，用于节流避免抖动

    def _forward_hook(d):
        console_hook(d)  # 保留原有的控制台打印（10秒节流正常工作）
        if d['status'] == 'downloading':
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes', 0)
            frag_count = d.get('fragment_count')
            # 百分比用 downloaded/total（与 yt-dlp 自身进度条一致，正常递增）。
            # 注：HLS 的 fragment_index 要等整个分片下完才 +1，早期一直是 0，
            # 不能用它算百分比（会让进度条卡在 0%），仅在没有 total 时作兜底。
            if on_progress:
                if total_bytes:
                    percent = downloaded / total_bytes * 100
                elif frag_count:
                    percent = (d.get('fragment_index') or 0) / frag_count * 100
                else:
                    percent = 0
                on_progress(percent, d.get('speed') or 0, d.get('eta') or 0)
            # 总大小是 yt-dlp 的估算值（HLS：平均分片大小×分片数），下载初期抖动极大
            # （会在真实值上下大幅跳动）。策略：跳过最不稳定的开头（已下载 <1MB），
            # 取第一个较稳定的估算值后锁定，整个下载过程不再变动（前端标“约”），
            # 下载完成后用磁盘真实大小替换为精确值。
            if total_bytes and on_meta and pushed_size[0] is None and downloaded >= 1048576:
                pushed_size[0] = total_bytes
                on_meta(filesize=int(total_bytes))

    ydl_opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "proxy": effective_proxy,
        "quiet": True,
        "no_warnings": True,
        "no_progress": True,
        "logger": _NoopLogger(),
        "progress_hooks": [_forward_hook],
        # 目标站点的 HLS 是 AES-128 加密 TS 分片列表，分片多且经代理拉取易抖动。
        # 单线程顺序下载分片，规避 yt-dlp 并发下载时 .part 重命名竞态
        # （该竞态会导致 "fragment not found" → 跳过片段 → 最终 "downloaded file is empty"）。
        "concurrent_fragment_downloads": 1,
        "retries": 10,
        "fragment_retries": 10,
        "retry_sleep": {"fragment": 2},
        "http_chunk_size": 1048576,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        if title is None:
            # 通用站点：先取一次元数据拿标题/时长/总大小，下载前就通知队列，
            # 再实际下载。取元数据失败则忽略（保持旧的“下载完才有名”回退）。
            try:
                meta = ydl.extract_info(download_url, download=False)
                if isinstance(meta, dict):
                    title = meta.get("title") or title
                    if on_meta:
                        on_meta(title=title, duration=meta.get("duration"),
                                filesize=_filesize_of(meta))
            except Exception:
                pass
        try:
            ydl.download([download_url])
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            # 把 yt-dlp 的英文错误翻成对用户友好的中文提示，便于队列里一眼看懂。
            if "No video formats found" in msg or "Unsupported URL" in msg:
                raise RuntimeError("无法下载：该链接可能不是视频（如图文笔记/受地区限制/需要登录），或站点暂不支持")
            raise RuntimeError(f"下载失败：{msg}")

    # yt-dlp 在处理单流 HLS fMP4 时，remux 后偶尔会残留 *.part / *.part-FragN.part /
    # *.ytdl 等临时碎片文件。下载完成后清理这些残留，只保留最终成品。
    for name in os.listdir(output_dir):
        if name.endswith(('.part', '.ytdl')) or '.part-Frag' in name:
            try:
                os.remove(os.path.join(output_dir, name))
            except OSError:
                pass

    # 下载完成后，用磁盘上真实文件大小替换下载过程中的估算值（精确、不再抖动）。
    # 目标站点的文件名由我们用 safe_title 控制，可直接定位；通用站点文件名由 yt-dlp 按
    # %(title)s 生成，难以精确匹配，沿用估算值（直接下载的 filesize 本就精确）。
    if on_meta and _is_target_site_url(url):
        final_path = os.path.join(output_dir, f"{safe_title}.mp4")
        if os.path.exists(final_path):
            on_meta(filesize=os.path.getsize(final_path))

    return title
