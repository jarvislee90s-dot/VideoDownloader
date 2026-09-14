"""微信视频号下载辅助（channels.weixin.qq.com）。

取流链路（实测验证，详见 spec）：
browser_cookie3 读 Chrome 登录 cookies → curl_cffi(impersonate='chrome')
POST /finder-preview/api/feed/get_feed_info（body: baseReq.generalToken='' + shortUri）
→ 响应含 h264VideoInfo.videoUrl / h265VideoInfo.videoUrl / decodeKey（加密标记）。
接口无签名头，卡点只在登录态；401 → 提示用户扫码登录。

结构对齐 bilibili.py：URL 谓词 + prefetch_meta + download(on_progress, on_meta)。
"""
import re
import time
import urllib.parse

from curl_cffi import requests as cffi_requests

from video_downloader.config import WECHAT_FEED_API_URL, WECHAT_COOKIE_BROWSER


def _is_wechat_url(url: str) -> bool:
    """匹配视频号两种链接形态（finder-preview 带 id、/sph/ 短链）。

    注意排除 mp.weixin.qq.com（公众号文章，非视频号）。
    """
    if "channels.weixin.qq.com" in url:
        return True
    return bool(re.search(r"weixin\.qq\.com/sph/", url))


def _extract_short_uri(url: str) -> str | None:
    """两种形态统一提取 shortUri（即 feed id）。

    - finder-preview: ?id=XXX 参数
    - /sph/ 短链: 路径末段
    """
    parsed = urllib.parse.urlparse(url)
    if "channels.weixin.qq.com" in parsed.netloc:
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("id"):
            return qs["id"][0]
    m = re.search(r"/sph/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    return None


class WechatLoginRequired(RuntimeError):
    """登录态缺失/失效。错误信息直接面向用户。"""


_LOGIN_HINT = "未检测到 Chrome 登录态，请先用 Chrome 打开 channels.weixin.qq.com 扫码登录，然后点重试"


def _load_browser_cookies(browser: str, domains: tuple[str, ...]) -> dict[str, str]:
    """从浏览器读取指定域 cookies（结构对齐 bilibili._load_browser_cookies）。"""
    try:
        import browser_cookie3
    except ImportError:
        return {}
    loaders = {
        "chrome": browser_cookie3.chrome,
        "firefox": browser_cookie3.firefox,
        "safari": browser_cookie3.safari,
        "edge": browser_cookie3.edge,
    }
    loader = loaders.get(browser.lower())
    if loader is None:
        return {}
    cookies: dict[str, str] = {}
    for domain in domains:
        try:
            cj = loader(domain_name=domain)
            cookies.update({c.name: c.value for c in cj})
        except Exception:
            continue
    return cookies


def _build_session() -> cffi_requests.Session:
    """带登录 cookies 的 chrome 指纹会话。cookies 为空时抛 WechatLoginRequired。"""
    cookies = _load_browser_cookies(
        WECHAT_COOKIE_BROWSER, (".weixin.qq.com", ".qq.com")
    )
    if not cookies:
        raise WechatLoginRequired(_LOGIN_HINT)
    s = cffi_requests.Session(impersonate="chrome")
    for name, value in cookies.items():
        s.cookies.set(name, value, domain=".weixin.qq.com")
    return s


def _get_feed_info(short_uri: str) -> dict:
    """POST feed API 拿视频信息。401/errCode!=0 → WechatLoginRequired（单一降级）。"""
    s = _build_session()
    # 同源暖场：GET 一次 finder-preview 页面，确保会话 cookies 完整
    s.get("https://channels.weixin.qq.com/finder-preview/pages/sph", timeout=30)
    resp = s.post(
        WECHAT_FEED_API_URL,
        json={"baseReq": {"generalToken": ""}, "shortUri": short_uri},
        headers={
            "Content-Type": "application/json",
            "Referer": "https://channels.weixin.qq.com/finder-preview/pages/sph",
        },
        timeout=30,
    )
    if resp.status_code == 401:
        raise WechatLoginRequired(_LOGIN_HINT)
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"视频号接口返回异常（HTTP {resp.status_code}）")
    return data


def _parse_feed_response(data: dict) -> dict:
    """防御式解析 feed 响应（字段名对齐 wx_channel api_client.js buildSharedFeedCompatResponse）。"""
    payload = data.get("data") or {}
    err_code = data.get("errCode")
    feed = payload.get("feedInfo") or {}
    author = payload.get("authorInfo") or {}

    def _v(obj, *names):
        for n in names:
            v = obj.get(n)
            if v:
                return v
        return None

    h264 = feed.get("h264VideoInfo") or {}
    h265 = feed.get("h265VideoInfo") or {}
    video_url = _v(h264, "videoUrl") or feed.get("videoUrl") or _v(h265, "videoUrl")

    duration_ms = feed.get("durationMs") or 0
    file_size = feed.get("fileSize") or 0

    error = None
    if isinstance(err_code, int) and err_code != 0:
        error = data.get("errMsg") or f"errCode {err_code}"

    return {
        "title": feed.get("description") or None,
        "duration": duration_ms / 1000.0 if duration_ms else None,
        "filesize": file_size or None,
        "video_url": video_url or None,
        "decode_key": feed.get("decodeKey") or "",
        "author": author.get("nickname") or None,
        "error": error,
    }
