"""微信视频号下载辅助（channels.weixin.qq.com）。

取流链路（实测验证，详见 spec）：
browser_cookie3 读 Chrome 登录 cookies → curl_cffi(impersonate='chrome')
POST /finder-preview/api/feed/get_feed_info（body: baseReq.generalToken='' + shortUri）
→ 响应含 h264VideoInfo.videoUrl / h265VideoInfo.videoUrl / decodeKey（加密标记）。
接口无签名头，卡点只在登录态；401 → 提示用户扫码登录。

结构对齐 bilibili.py：URL 谓词 + prefetch_meta + download(on_progress, on_meta)。
"""
import re
import urllib.parse

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
