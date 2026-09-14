"""微信视频号下载辅助（channels.weixin.qq.com）。

取流链路（实测验证，详见 spec）：
wechat_login.py 扫码登录 → cookies 持久化在 profile_browser/ → 无头 Playwright 提取
→ curl_cffi(impersonate='chrome') POST /finder-preview/api/feed/get_feed_info
（body: baseReq.generalToken='' + shortUri）→ 响应 feedInfo.h264VideoInfo.videoUrl
等字段 → CDN 直链下载（stodownload，Referer 同源）→ decodeKey 存在时 ISAAC64 解密。
接口无签名头，卡点只在登录态；401 → 提示运行 wechat_login.py 扫码。

实测限制：部分内容发布者关闭了网页播放，get_feed_info 对这些内容不返回视频字段
（页面显示"可前往微信观看"），此时报"内容可能不支持网页播放"。

结构对齐 bilibili.py：URL 谓词 + prefetch_meta + download(on_progress, on_meta)。
"""
import os
import re
import time
import urllib.parse
from pathlib import Path

from curl_cffi import requests as cffi_requests

from video_downloader.config import WECHAT_FEED_API_URL, WECHAT_COOKIE_BROWSER, WECHAT_PROFILE_DIR
from video_downloader.wechat_decrypt import DEFAULT_ENC_LEN, decrypt_data


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


_LOGIN_HINT = (
    "未检测到视频号登录态。请在本项目目录运行 python wechat_login.py，"
    "用微信扫码登录一次（登录状态会保存，之后无需再扫），然后点重试"
)


def _load_profile_cookies() -> dict[str, str]:
    """从 wechat_login.py 扫码落盘的专用 profile 提取登录 cookies。

    Chrome/Edge 新版启用 App-Bound Encryption，browser_cookie3 即使提权也无法解密
    （实测 RequiresAdminError/Unable to get key），因此主路线是工具自带 profile。
    Playwright 以无头模式打开 profile 读取 cookies 后立即关闭，开销约 1-2 秒。
    """
    profile = Path(WECHAT_PROFILE_DIR)
    if not profile.is_absolute():
        profile = Path(__file__).resolve().parent.parent / profile
    if not profile.is_dir():
        return {}
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(str(profile), headless=True)
            try:
                cookies = ctx.cookies("https://channels.weixin.qq.com")
            finally:
                ctx.close()
        return {c["name"]: c["value"] for c in cookies if c.get("value")}
    except Exception:
        return {}


def _load_browser_cookies(browser: str, domains: tuple[str, ...]) -> dict[str, str]:
    """从系统浏览器读取指定域 cookies（fallback：profile 缺失时尝试）。"""
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
    """带登录 cookies 的 chrome 指纹会话。cookies 为空时抛 WechatLoginRequired。

    cookie 来源优先级：wechat_login.py 扫码 profile > 系统浏览器（browser_cookie3）。
    """
    cookies = _load_profile_cookies()
    if not cookies:
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
    """POST feed API 拿视频信息。401 → WechatLoginRequired；errCode!=0 由 _parse_feed_response 归入 error 字段。"""
    s = _build_session()
    # 同源暖场：GET 一次 finder-preview 页面，确保会话 cookies 完整
    s.get("https://channels.weixin.qq.com/finder-preview/pages/sph", timeout=30)
    resp = s.post(
        WECHAT_FEED_API_URL,
        json={"baseReq": {"generalToken": ""}, "shortUri": short_uri},
        headers={
            "Content-Type": "application/json",
            "Origin": "https://channels.weixin.qq.com",
            "Referer": "https://channels.weixin.qq.com/finder-preview/pages/sph",
            # 缺 Sec-Fetch-* 头会被风控判为非浏览器请求（实测 401）
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
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
    scene = payload.get("sceneInfo") or {}

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
        # shortUri -> objectId 映射键：登录用户自己的内容可用它去工作台匹配明文直链
        "dynamic_export_id": scene.get("dynamicExportId") or None,
    }


_WORKSPACE_POST_LIST_URL = (
    "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_list"
)


def _workspace_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Origin": "https://channels.weixin.qq.com",
        "Referer": "https://channels.weixin.qq.com/platform",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


def _find_own_media(dynamic_export_id: str, s: cffi_requests.Session,
                    max_pages: int = 10) -> dict | None:
    """在工作台 post_list 里按 dynamicExportId 匹配自己的内容，返回 media dict。

    微信网页端 finder-preview 接口对多数内容不返回视频字段（仅微信内观看限制），
    但登录用户自己发布的内容在工作台直接给明文 CDN 地址。翻页上限防死循环。
    """
    page = 1
    while page <= max_pages:
        resp = s.post(
            _WORKSPACE_POST_LIST_URL,
            json={"orderType": 3, "pageNumber": page, "pageSize": 50},
            headers=_workspace_headers(),
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            return None
        try:
            items = resp.json().get("data", {}).get("list", [])
        except ValueError:
            return None
        if not items:
            return None
        bare = dynamic_export_id.replace("export/", "")
        for it in items:
            oid = it.get("objectId") or ""
            if oid == dynamic_export_id or oid == bare or oid.replace("export/", "") == bare                     or it.get("exportId") == dynamic_export_id:
                media_list = (it.get("desc") or {}).get("media") or []
                if media_list:
                    m = media_list[0]
                    def _int(v):
                        try:
                            return int(v)
                        except (TypeError, ValueError):
                            return None

                    return {
                        "url": m.get("fullUrl") or m.get("url"),
                        "filesize": _int(m.get("fullFileSize")) or _int(m.get("fileSize")),
                        "duration": _int(m.get("videoPlayLen")),
                        "decode_key": m.get("decodeKey") or "",
                    }
        page += 1
    return None


def _slugify(text: str | None) -> str:
    """文件名安全化（与 bilibili._slugify 同规则）。"""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text or "video")
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:80] or "video"


def _download_stream(url: str, tmp_path: Path, on_progress=None,
                     total_size: int | None = None) -> None:
    """curl_cffi 流式下载（结构对齐 bilibili._download_stream）。

    - 必须 CDN 同源 Referer，否则 403（登录态失效映射为 WechatLoginRequired）
    - on_progress 原样透传（percent, speed, eta），不做 try/except 包裹——
      worker 通过在回调里抛 _PauseRequested 实现暂停，必须允许向上传播
    """
    downloaded = 0
    start = time.monotonic()
    r = cffi_requests.get(
        url,
        headers={"Referer": "https://channels.weixin.qq.com/"},
        impersonate="chrome",
        timeout=120,
        stream=True,
    )
    try:
        if r.status_code == 403:
            raise WechatLoginRequired("登录态可能已失效，请重新扫码登录后重试")
        r.raise_for_status()
        # GET 的 Content-Length 通常比 feed API 的 fileSize 更可靠
        get_cl = int(r.headers.get("Content-Length", 0) or 0)
        if get_cl > (total_size or 0):
            total = get_cl
        else:
            total = total_size or get_cl
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if on_progress and total:
                    percent = downloaded / total * 100
                    elapsed = time.monotonic() - start
                    speed = downloaded / elapsed if elapsed > 0 else 0
                    eta = max((total - downloaded) / speed, 0) if speed > 0 else 0
                    on_progress(percent, speed, eta)
    finally:
        r.close()


def _read_x_enclen(headers) -> int | None:
    """CDN 响应头 X-enclen（十进制加密区长度）。缺失/非法返回 None。"""
    raw = headers.get("X-enclen")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def prefetch_meta(url: str, on_meta=None) -> None:
    """入队后异步拉标题/时长/大小（对齐 bilibili.prefetch_meta 签名语义）。

    任何失败（含 WechatLoginRequired）静默吞掉：预取失败不影响入队，
    下载阶段会给出正式错误提示。
    """
    if not on_meta:
        return
    short_uri = _extract_short_uri(url)
    if not short_uri:
        return
    try:
        raw = _get_feed_info(short_uri)
        info = _parse_feed_response(raw)
    except Exception:
        return
    on_meta(title=info["title"], duration=info["duration"], filesize=info["filesize"])


def download(url: str, output_path: str, on_progress=None, on_meta=None) -> str:
    """下载视频号视频到 output_path，返回标题。

    流程：feed API 取流 → curl_cffi 下载（.part）→ 需要则 ISAAC64 解密 →
    改名落盘。登录态缺失/失效抛 WechatLoginRequired（错误信息面向用户）；
    解密失败抛 RuntimeError 并保留 .encrypted 密文便于诊断。
    """
    short_uri = _extract_short_uri(url)
    if not short_uri:
        raise RuntimeError("无法从链接解析视频号 id（支持 finder-preview?id= 与 /sph/ 两种形态）")

    raw = _get_feed_info(short_uri)
    info = _parse_feed_response(raw)
    if info["error"]:
        raise WechatLoginRequired(f"登录态可能已失效，请重跑 python wechat_login.py 扫码后重试（接口返回：{info['error']}）")
    if not info["video_url"]:
        # 回退：登录用户自己的内容在 finder-preview 不给流（网页播放未开放），
        # 但工作台 post_list 有明文直链——用 dynamicExportId 匹配自己的内容。
        # 链接本身是 objectId 形态（export 前缀 / UzFf 开头）时直接作匹配键
        dyn = info.get("dynamic_export_id")
        if not dyn and (short_uri.startswith("export/") or short_uri.startswith("UzFf")):
            dyn = short_uri
        own_media = None
        if dyn:
            try:
                own_media = _find_own_media(dyn, _build_session())
            except Exception:
                own_media = None
        if not own_media or not own_media.get("url"):
            raise RuntimeError(
                "该内容未提供网页播放地址：可能是图片/图文类型，或发布者限制了仅微信内观看"
                "（网页端提示'可前往微信观看'）。仅视频号账号本人发布的内容支持网页端下载"
            )
        info["video_url"] = own_media["url"]
        if own_media.get("filesize"):
            info["filesize"] = own_media["filesize"]
        if own_media.get("duration"):
            info["duration"] = own_media["duration"]
        if own_media.get("decode_key"):
            info["decode_key"] = own_media["decode_key"]
    if on_meta:
        on_meta(title=info["title"], duration=info["duration"], filesize=info["filesize"])

    title = info["title"] or short_uri
    out_dir = os.path.dirname(output_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    final_path = Path(output_path)
    part_path = final_path.with_name(final_path.name + ".part")

    try:
        _download_stream(info["video_url"], part_path, on_progress,
                         total_size=info["filesize"])

        # 加密流：解密 + 魔数校验；失败保留密文（.encrypted）便于诊断
        if info["decode_key"]:
            # X-enclen（十进制加密区长度）仅加密流需要：轻量 HEAD 预检，
            # 失败忽略，解密回退 DEFAULT_ENC_LEN
            try:
                head = cffi_requests.head(
                    info["video_url"],
                    headers={"Referer": "https://channels.weixin.qq.com/"},
                    impersonate="chrome", timeout=15,
                )
                enc_len = _read_x_enclen(head.headers)
            except Exception:
                enc_len = None
            cipher = part_path.read_bytes()
            plain = decrypt_data(cipher, info["decode_key"],
                                 enc_len=enc_len or DEFAULT_ENC_LEN)
            if plain is None:
                enc_path = part_path.with_name(part_path.name + ".encrypted")
                os.replace(part_path, enc_path)
                raise RuntimeError(
                    f"解密失败（decodeKey={info['decode_key'][:8]}...），"
                    f"密文已保留：{enc_path.name}，请反馈该链接"
                )
            part_path.write_bytes(plain)

        os.replace(part_path, final_path)
    except Exception:
        # 中断/失败：清理 .part 残留（.encrypted 保留）
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError:
                pass
        raise

    if on_meta and final_path.exists():
        on_meta(filesize=os.path.getsize(final_path))
    return title
