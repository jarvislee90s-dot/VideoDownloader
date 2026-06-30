"""Bilibili 下载辅助（curl_cffi 绕过 412 / v_voucher 风控）。

逻辑参考并改编自 video-summary 技能中的 process.py：
- 用 curl_cffi impersonate="chrome" 请求 B站页面和 playurl API
- 优先用 buvid3/4 匿名 cookies；若触发 v_voucher 风控，再读取浏览器登录态 cookies
- DASH 流分别下载视频/音频并用 ffmpeg 合并；直链则直接下载
- 下载过程中实时回调进度
"""
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _is_bilibili_url(url: str) -> bool:
    return "bilibili.com" in url or "b23.tv" in url


def _slugify(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', text or 'video')
    text = re.sub(r'_+', '_', text).strip('_')
    return text[:80] or "video"


def _run_cmd(cmd: list[str], timeout: int = 120) -> None:
    subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout)


def _extract_ids(url: str) -> tuple[str | None, str | None, str | None]:
    """从 B站 URL 或页面提取 (bvid, cid, title)。"""
    from curl_cffi import requests as cffi_requests

    bvid_match = re.search(r"(BV[a-zA-Z0-9]+)", url)
    bvid = bvid_match.group(1) if bvid_match else None

    resp = cffi_requests.get(url, impersonate="chrome", timeout=30)
    if resp.status_code != 200:
        return bvid, None, None

    cid_match = re.search(r'"cid":(\d+)', resp.text)
    cid = cid_match.group(1) if cid_match else None

    title_match = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.DOTALL)
    title = title_match.group(1).strip() if title_match else None
    if title:
        title = re.sub(r"_哔哩哔哩_bilibili$", "", title).strip()

    return bvid, cid, title


def _detect_v_voucher(data: dict) -> bool:
    """检测 playurl 返回是否为 v_voucher 风控（无真实视频流）。"""
    if data.get("code") != 0:
        return False
    d = data.get("data", {})
    return "v_voucher" in d and "dash" not in d and "durl" not in d


def _get_stream_urls_with_cookies(bvid: str, cid: str) -> tuple[str | None, str | None, bool]:
    """带 buvid cookies 调 playurl。返回 (video_url, audio_url, is_v_voucher)。"""
    from curl_cffi import requests as cffi_requests

    s = cffi_requests.Session(impersonate="chrome")
    try:
        fr = s.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=20).json()
        s.cookies.set("buvid3", fr["data"]["b_3"], domain=".bilibili.com")
        s.cookies.set("buvid4", fr["data"]["b_4"], domain=".bilibili.com")
    except Exception:
        pass
    s.get(f"https://www.bilibili.com/video/{bvid}", timeout=20)

    resp = s.get(
        "https://api.bilibili.com/x/player/wbi/playurl",
        params={"bvid": bvid, "cid": cid, "fnval": 4048, "fnver": 0, "fourk": 1, "qn": 80},
        timeout=30,
    )
    data = resp.json()
    if _detect_v_voucher(data):
        return None, None, True
    return _parse_playurl_data(data)


def _load_browser_cookies(browser: str, domain: str = ".bilibili.com") -> dict[str, str]:
    """从浏览器读取指定 domain 的 cookies。"""
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

    try:
        cj = loader(domain_name=domain)
        return {c.name: c.value for c in cj}
    except Exception:
        return {}


def _get_stream_urls_with_browser_cookies(
    bvid: str, cid: str, browser: str
) -> tuple[str | None, str | None, bool]:
    """从浏览器读取登录态 cookies 调 B站 playurl。"""
    from curl_cffi import requests as cffi_requests

    cookies = _load_browser_cookies(browser)
    if not cookies:
        return None, None, False

    s = cffi_requests.Session(impersonate="chrome")
    for name, value in cookies.items():
        s.cookies.set(name, value, domain=".bilibili.com")

    s.get(f"https://www.bilibili.com/video/{bvid}", timeout=20)
    resp = s.get(
        "https://api.bilibili.com/x/player/wbi/playurl",
        params={"bvid": bvid, "cid": cid, "fnval": 4048, "fnver": 0, "fourk": 1, "qn": 80},
        timeout=30,
    )
    data = resp.json()
    if _detect_v_voucher(data):
        return None, None, True
    return _parse_playurl_data(data)


def _parse_playurl_data(data: dict) -> tuple[str | None, str | None, bool]:
    """解析 playurl 返回，得到 (video_url, audio_url, is_v_voucher)。"""
    if data.get("code") != 0:
        return None, None, False
    d = data["data"]
    if "dash" in d:
        dash = d["dash"]
        videos = dash.get("video", [])
        audios = dash.get("audio", [])
        best_video = max(videos, key=lambda v: v.get("height", 0) * v.get("width", 0)) if videos else None
        best_audio = audios[0] if audios else None
        v_url = best_video.get("baseUrl") or best_video.get("base_url") if best_video else None
        a_url = best_audio.get("baseUrl") or best_audio.get("base_url") if best_audio else None
        return v_url, a_url, False
    elif "durl" in d:
        durls = d["durl"]
        return (durls[0]["url"] if durls else None), None, False
    return None, None, False


def _download_stream(
    url: str,
    tmp_path: Path,
    headers: dict,
    on_progress=None,
    total_size: int | None = None,
) -> None:
    """用 curl_cffi 下载单个流，并回调进度（合并后的进度）。"""
    from curl_cffi import requests as cffi_requests

    downloaded = 0
    r = cffi_requests.get(url, headers=headers, impersonate="chrome", timeout=120, stream=True)
    try:
        total = total_size or int(r.headers.get("Content-Length", 0) or 0)
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and total:
                        on_progress(downloaded / total * 100)
    finally:
        r.close()


def download(
    url: str,
    output_path: str,
    on_progress=None,
    browser: str = "chrome",
) -> str:
    """下载 Bilibili 视频到 output_path，返回标题。

    on_progress: 回调(percent: float)
    """
    from curl_cffi import requests as cffi_requests

    bvid, cid, title = _extract_ids(url)
    if not bvid or not cid:
        raise RuntimeError("无法从 B站 URL 提取 bvid/cid")

    safe_title = _slugify(title)
    out_dir = os.path.dirname(output_path) or "."
    os.makedirs(out_dir, exist_ok=True)

    # 策略1：buvid cookies
    v_url, a_url, is_v_voucher = _get_stream_urls_with_cookies(bvid, cid)

    # 策略2：浏览器登录态 cookies
    if is_v_voucher or not v_url:
        v_url2, a_url2, is_v2 = _get_stream_urls_with_browser_cookies(bvid, cid, browser)
        if not is_v2 and v_url2:
            v_url, a_url = v_url2, a_url2
        else:
            raise RuntimeError("B站视频触发登录风控，请确保浏览器已登录 Bilibili 后再试")

    if not v_url:
        raise RuntimeError("B站 playurl API 未返回视频流")

    headers = {"Referer": "https://www.bilibili.com", "User-Agent": "Mozilla/5.0"}

    # 估算总大小，用于进度（分别下载时各自按 50% 权重合并）
    total_size = None
    try:
        h = cffi_requests.head(v_url, headers=headers, impersonate="chrome", timeout=15)
        total_size = int(h.headers.get("Content-Length", 0) or 0)
    except Exception:
        pass

    v_tmp = a_tmp = None
    try:
        if a_url:
            # DASH：分别下载视频和音频，合并
            v_tmp = Path(out_dir) / f"_{safe_title}_video_only.m4s"
            a_tmp = Path(out_dir) / f"_{safe_title}_audio_only.m4s"

            def v_progress(p):
                if on_progress:
                    on_progress(p / 2)

            def a_progress(p):
                if on_progress:
                    on_progress(50 + p / 2)

            with ThreadPoolExecutor(max_workers=2) as executor:
                f_video = executor.submit(_download_stream, v_url, v_tmp, headers, v_progress)
                f_audio = executor.submit(_download_stream, a_url, a_tmp, headers, a_progress)
                f_video.result()
                f_audio.result()

            _run_cmd([
                "ffmpeg", "-y", "-i", str(v_tmp), "-i", str(a_tmp),
                "-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart",
                output_path,
            ], timeout=120)
            v_tmp.unlink(missing_ok=True)
            a_tmp.unlink(missing_ok=True)
        else:
            # 直链
            def single_progress(p):
                if on_progress:
                    on_progress(p)

            _download_stream(v_url, Path(output_path), headers, single_progress, total_size)

        return title or "video"
    except Exception:
        # 清理临时文件，避免中断后残留 .m4s
        if v_tmp:
            v_tmp.unlink(missing_ok=True)
        if a_tmp:
            a_tmp.unlink(missing_ok=True)
        if os.path.exists(output_path):
            os.remove(output_path)
        raise
