"""微信公众号文章内嵌视频下载辅助（mp.weixin.qq.com）。

文章内的 wxv_ 视频走 mpvideo.qpic.cn CDN，但直链 URL 带 dis_k/dis_t 签名，
脱离浏览器会话后立即 403（实测）。因此取流与下载必须在 Playwright 上下文内完成：
打开文章页 → 劫持 mpvideo 响应字节流 → 落盘。登录态复用 wechat_login.py 的
profile_browser/（公众号文章通常免登录可看，profile 仅保证环境一致）。

结构对齐 wechat.py：URL 谓词 + prefetch_meta + download(on_progress, on_meta)。
"""
import re
from pathlib import Path

from video_downloader.config import WECHAT_PROFILE_DIR
from video_downloader.wechat import WechatLoginRequired, _slugify


def _is_mp_article_url(url: str) -> bool:
    """匹配公众号文章页（mp.weixin.qq.com/s/... 等），排除视频号域。"""
    if "mp.weixin.qq.com" not in url:
        return False
    return True


def _extract_wxv_ids(html: str) -> list[str]:
    """从文章 HTML 提取去重后的 wxv_ 视频 id（保持出现顺序）。"""
    return list(dict.fromkeys(re.findall(r"wxv_[a-zA-Z0-9_]{10,}", html)))


def _download_article_videos(article_url: str, out_dir: Path, title_hint: str,
                             on_progress=None, on_meta=None,
                             max_videos: int = 20, timeout_ms: int = 120_000) -> list[Path]:
    """在 Playwright 里打开文章并劫持 mpvideo 媒体响应，把每个视频写到 out_dir。

    返回落盘文件路径列表（顺序 = 拦截到媒体流的顺序）。拦截间隔超过
    idle_ms 毫秒且至少拿到 1 个视频，或到达 max_videos/timeout 时结束。
    """
    from playwright.sync_api import sync_playwright

    profile = Path(WECHAT_PROFILE_DIR)
    if not profile.is_absolute():
        profile = Path(__file__).resolve().parent.parent / profile

    captured: dict[str, list[bytes]] = {}   # 请求 URL 首段 -> 字节块
    order: list[str] = []                   # 拦截到的顺序（按首次出现）

    def route_handler(route):
        url = route.request.url
        key = url.split("?")[0]
        if key not in captured:
            captured[key] = []
            order.append(key)
        captured[key].append(route.fetch().body())
        route.continue_()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile), headless=True,
            viewport={"width": 1280, "height": 900},
        )
        try:
            # 公众号文章一般免登录可看，profile 仅用于环境一致；真需要登录时播放器会自己提示
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            ctx.route("**mpvideo.qpic.cn**", route_handler)
            page.goto(article_url, timeout=60_000)
            page.wait_for_timeout(5_000)

            # 滚动到底触发懒加载的播放器，再逐个点击播放按钮让播放器发起媒体请求
            page.evaluate(
                "() => new Promise(res => {"
                "  let y = 0; const t = setInterval(() => {"
                "    y += 800; window.scrollTo(0, y);"
                "    if (y >= document.body.scrollHeight) { clearInterval(t); res(); }"
                "  }, 300); })"
            )
            page.wait_for_timeout(3_000)
            _click_video_players(page)

            # 等待拦截：有新视频就续等，连续 8 秒无新增或达上限则收尾
            deadline = timeout_ms / 1000.0
            waited = 0.0
            last_count = 0
            idle = 0.0
            while waited < deadline and len(order) < max_videos:
                page.wait_for_timeout(1_000)
                waited += 1.0
                if len(order) > last_count:
                    last_count = len(order)
                    idle = 0.0
                    _click_video_players(page)  # 新播放器出现则继续点
                else:
                    idle += 1.0
                    if order and idle >= 8:
                        break
        finally:
            ctx.close()

    if not order:
        raise RuntimeError("文章中未发现可下载的视频（可能无视频或需要特殊播放环境）")

    files: list[Path] = []
    multi = len(order) > 1
    for i, key in enumerate(order, 1):
        data = b"".join(captured[key])
        name = _slugify(title_hint or "公众号视频")
        if multi:
            name = f"{name}_{i:02d}"
        out = out_dir / f"{name}.mp4.part"
        out.write_bytes(data)
        final = out.with_suffix("")
        out.replace(final)
        if on_meta:
            on_meta(filesize=len(data))
        files.append(final)
    return files


def _click_video_players(page) -> None:
    """逐个点击页面上的 mpvideo 播放按钮/封面，触发播放器拉流。"""
    try:
        players = page.query_selector_all(
            "[data-mpvid], .video_iframe, iframe[src*='video_player_tmpl']"
        )
        for el in players:
            try:
                el.scroll_into_view_if_needed()
                # mpvideo 容器点击即播放（封面上的播放键）；iframe 内的点击穿透不可靠，
                # 但绝大多数 mpvideo 在点击容器后就会预加载媒体流
                el.click(timeout=2_000)
                page.wait_for_timeout(800)
            except Exception:
                continue
    except Exception:
        pass


def prefetch_meta(url: str, on_meta=None) -> None:
    """公众号文章的元数据预取：标题轻量可取，视频数/大小需渲染页面，不做。"""
    return


def download(url: str, output_dir: str, on_progress=None, on_meta=None) -> list[str]:
    """下载公众号文章里的所有内嵌视频到 output_dir，返回落盘路径列表。

    on_meta(filesize=...) 在每个视频落盘后回调（与 wechat/bilibili 的子集更新约定一致）。
    on_progress 不适用（劫持方式拿不到流式进度），保留参数仅为签名对齐。
    """
    from curl_cffi import requests as cffi_requests

    resp = cffi_requests.get(url, impersonate="chrome", timeout=30)
    resp.raise_for_status()
    html = resp.text

    title_m = re.search(r'<meta property="og:title" content="([^"]*)"', html) \
        or re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    title = title_m.group(1).strip() if title_m else "公众号视频"
    ids = _extract_wxv_ids(html)
    if on_meta:
        on_meta(title=title)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = _download_article_videos(url, out_dir, title,
                                     on_progress=on_progress, on_meta=on_meta,
                                     max_videos=max(len(ids), 1))
    return [str(f) for f in files]


_ = WechatLoginRequired  # 保留导入供调用方 except 引用（downloader 分支统一捕获）
