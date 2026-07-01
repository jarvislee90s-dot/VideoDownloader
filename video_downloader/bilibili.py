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


def _extract_ids(url: str) -> tuple[str | None, str | None, str | None, int | None]:
    """从 B站 URL 或页面提取 (bvid, cid, title, duration_seconds)。"""
    from curl_cffi import requests as cffi_requests

    bvid_match = re.search(r"(BV[a-zA-Z0-9]+)", url)
    bvid = bvid_match.group(1) if bvid_match else None

    resp = cffi_requests.get(url, impersonate="chrome", timeout=30)
    if resp.status_code != 200:
        return bvid, None, None, None

    cid_match = re.search(r'"cid":(\d+)', resp.text)
    cid = cid_match.group(1) if cid_match else None

    title_match = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.DOTALL)
    title = title_match.group(1).strip() if title_match else None
    if title:
        title = re.sub(r"_哔哩哔哩_bilibili$", "", title).strip()

    duration = None
    dm = re.search(r'"duration":(\d+)', resp.text)
    if dm:
        try:
            duration = int(dm.group(1))
        except ValueError:
            pass

    return bvid, cid, title, duration


def _detect_v_voucher(data: dict) -> bool:
    """检测 playurl 返回是否为 v_voucher 风控（无真实视频流）。"""
    if data.get("code") != 0:
        return False
    d = data.get("data", {})
    return "v_voucher" in d and "dash" not in d and "durl" not in d


def _get_stream_urls_with_cookies(bvid: str, cid: str) -> tuple[str | None, str | None, bool, int | None, int | None]:
    """带 buvid cookies 调 playurl。返回 (video_url, audio_url, is_v_voucher, video_bw, audio_bw)。"""
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
        return None, None, True, None, None
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
) -> tuple[str | None, str | None, bool, int | None, int | None]:
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
        return None, None, True, None, None
    return _parse_playurl_data(data)


def _parse_playurl_data(data: dict) -> tuple[str | None, str | None, bool, int | None, int | None]:
    """解析 playurl 返回，得到 (video_url, audio_url, is_v_voucher, video_bw, audio_bw)。

    bandwidth 单位为 bits/s，用于估算文件大小（bandwidth * duration / 8），
    比 HEAD 请求更可靠（B站音频 URL 常对 HEAD 返回 404）。
    """
    if data.get("code") != 0:
        return None, None, False, None, None
    d = data["data"]
    if "dash" in d:
        dash = d["dash"]
        videos = dash.get("video", [])
        audios = dash.get("audio", [])
        best_video = max(videos, key=lambda v: v.get("height", 0) * v.get("width", 0)) if videos else None
        best_audio = audios[0] if audios else None
        v_url = best_video.get("baseUrl") or best_video.get("base_url") if best_video else None
        a_url = best_audio.get("baseUrl") or best_audio.get("base_url") if best_audio else None
        v_bw = best_video.get("bandwidth") if best_video else None
        a_bw = best_audio.get("bandwidth") if best_audio else None
        return v_url, a_url, False, v_bw, a_bw
    elif "durl" in d:
        durls = d["durl"]
        # durl 直链一般只有 url，没有 bandwidth；总大小由下载时获取
        return (durls[0]["url"] if durls else None), None, False, None, None
    return None, None, False, None, None


def _download_stream(
    url: str,
    tmp_path: Path,
    headers: dict,
    on_progress=None,
    total_size: int | None = None,
) -> int:
    """用 curl_cffi 下载单个流，并回调进度（percent, speed_bytes/s, eta_seconds）。

    返回实际下载到的总字节数；若 GET 响应 Content-Length 可用且比传入的
    total_size 更合理，则用它替换 total_size 作为进度分母。
    """
    import time
    from curl_cffi import requests as cffi_requests

    downloaded = 0
    start_time = time.monotonic()
    r = cffi_requests.get(url, headers=headers, impersonate="chrome", timeout=120, stream=True)
    try:
        # GET 的 Content-Length 通常比 HEAD 更可靠
        get_cl = int(r.headers.get("Content-Length", 0) or 0)
        if get_cl > (total_size or 0):
            total = get_cl
        else:
            total = total_size or get_cl
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and total:
                        percent = downloaded / total * 100
                        elapsed = time.monotonic() - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        eta = max((total - downloaded) / speed, 0) if speed > 0 else 0
                        on_progress(percent, speed, eta)
    finally:
        r.close()
    return downloaded


def _emit_dash_progress(state: dict, total_bytes: int, on_progress):
    """合并视频/音频双路进度，回调统一的 (percent, speed, eta)。"""
    import time

    video_percent = state.get("video_percent", 0.0)
    audio_percent = state.get("audio_percent", 0.0)
    video_speed = state.get("video_speed", 0.0)
    audio_speed = state.get("audio_speed", 0.0)

    # 按大小加权百分比；没有大小则简单平均
    if total_bytes > 0 and state.get("video_size") and state.get("audio_size"):
        v_size = state["video_size"]
        a_size = state["audio_size"]
        percent = (video_percent * v_size + audio_percent * a_size) / total_bytes
    else:
        percent = (video_percent + audio_percent) / 2

    speed = video_speed + audio_speed
    if speed > 0 and total_bytes > 0:
        # 估算剩余字节 = 总大小 * (1 - 平均完成比例)
        remaining = total_bytes * (1 - percent / 100.0)
        eta = max(remaining / speed, 0)
    else:
        eta = 0

    # ffmpeg 合并阶段没进度；当两路都已下载完成（percent 达到 100）后，
    # 用启动后总时长做一个缓慢增长的兜底百分比，避免进度条卡在 99.9%。
    if percent >= 100:
        elapsed = time.monotonic() - state.get("start_time", time.monotonic())
        # 合并通常几秒到几十秒，这里按 30 秒从 99.9% 走到 99.99%
        merge_percent = min(99.9 + elapsed / 30.0 * 0.1, 99.99)
        percent = merge_percent

    on_progress(percent, speed, eta)


def prefetch_meta(
    url: str,
    on_meta=None,
    browser: str = "chrome",
) -> None:
    """仅获取 B站视频元数据（标题/时长/估算大小），不下载。

    用于任务加入队列后、实际下载前，让等待中的任务也能显示信息。
    """
    from curl_cffi import requests as cffi_requests

    bvid, cid, title, duration = _extract_ids(url)
    if not bvid or not cid:
        return

    if on_meta:
        on_meta(title=title, duration=duration)

    # 用 playurl bandwidth * duration / 8 估算文件大小
    v_url, a_url, is_v_voucher, v_bw, a_bw = _get_stream_urls_with_cookies(bvid, cid)
    if is_v_voucher or not v_url:
        v_url2, a_url2, is_v2, v_bw2, a_bw2 = _get_stream_urls_with_browser_cookies(bvid, cid, browser)
        if not is_v2 and v_url2:
            v_url, a_url = v_url2, a_url2
            v_bw, a_bw = v_bw2, a_bw2
        else:
            return

    if not v_url or duration is None:
        return

    total_size = None
    if v_bw:
        total_size = int(v_bw * duration / 8)
    if a_url and a_bw:
        total_size = (total_size or 0) + int(a_bw * duration / 8)

    if on_meta and total_size:
        on_meta(filesize=total_size)


def download(
    url: str,
    output_path: str,
    on_progress=None,
    on_meta=None,
    browser: str = "chrome",
) -> str:
    """下载 Bilibili 视频到 output_path，返回标题。

    on_progress: 回调(percent: float, speed: float, eta: float)
    on_meta: 回调(title=None, duration=None, filesize=None)
    """
    import time
    from curl_cffi import requests as cffi_requests

    bvid, cid, title, duration = _extract_ids(url)
    if not bvid or not cid:
        raise RuntimeError("无法从 B站 URL 提取 bvid/cid")

    safe_title = _slugify(title)
    out_dir = os.path.dirname(output_path) or "."
    os.makedirs(out_dir, exist_ok=True)

    # 策略1：buvid cookies
    v_url, a_url, is_v_voucher, v_bw, a_bw = _get_stream_urls_with_cookies(bvid, cid)

    # 策略2：浏览器登录态 cookies
    if is_v_voucher or not v_url:
        v_url2, a_url2, is_v2, v_bw2, a_bw2 = _get_stream_urls_with_browser_cookies(bvid, cid, browser)
        if not is_v2 and v_url2:
            v_url, a_url = v_url2, a_url2
            v_bw, a_bw = v_bw2, a_bw2
        else:
            raise RuntimeError("B站视频触发登录风控，请确保浏览器已登录 Bilibili 后再试")

    if not v_url:
        raise RuntimeError("B站 playurl API 未返回视频流")

    headers = {"Referer": "https://www.bilibili.com", "User-Agent": "Mozilla/5.0"}

    # 提前通知标题/时长/总大小，让前端在下载开始前就显示
    if on_meta:
        on_meta(title=title, duration=duration)

    # 估算总大小：优先用 playurl 返回的 bandwidth * duration / 8；
    # 它比 HEAD 请求可靠（B站音频 URL 常对 HEAD 返回 404 或错误长度）。
    def _size_from_bw(bw):
        if bw and duration:
            return int(bw * duration / 8)
        return None

    total_size = _size_from_bw(v_bw)
    audio_size = _size_from_bw(a_bw)

    # 兜底：尝试 HEAD（直链或没有 bandwidth 时）
    if not total_size:
        try:
            h = cffi_requests.head(v_url, headers=headers, impersonate="chrome", timeout=15)
            total_size = int(h.headers.get("Content-Length", 0) or 0)
        except Exception:
            pass
    if a_url and not audio_size:
        try:
            h = cffi_requests.head(a_url, headers=headers, impersonate="chrome", timeout=15)
            audio_size = int(h.headers.get("Content-Length", 0) or 0)
        except Exception:
            pass

    total_bytes = (total_size or 0) + (audio_size or 0)
    if on_meta and total_bytes > 0:
        on_meta(filesize=total_bytes)

    v_tmp = a_tmp = None
    try:
        if a_url:
            # DASH：分别下载视频和音频，合并
            v_tmp = Path(out_dir) / f"_{safe_title}_video_only.m4s"
            a_tmp = Path(out_dir) / f"_{safe_title}_audio_only.m4s"

            state = {"video_percent": 0.0, "audio_percent": 0.0,
                     "video_speed": 0.0, "audio_speed": 0.0,
                     "video_size": total_size or 0,
                     "audio_size": audio_size or 0,
                     "start_time": time.monotonic()}

            def v_progress(p, s, e):
                if not on_progress:
                    return
                state["video_percent"] = p
                state["video_speed"] = s
                _emit_dash_progress(state, total_bytes, on_progress)

            def a_progress(p, s, e):
                if not on_progress:
                    return
                state["audio_percent"] = p
                state["audio_speed"] = s
                _emit_dash_progress(state, total_bytes, on_progress)

            with ThreadPoolExecutor(max_workers=2) as executor:
                f_video = executor.submit(
                    _download_stream, v_url, v_tmp, headers, v_progress, total_size
                )
                f_audio = executor.submit(
                    _download_stream, a_url, a_tmp, headers, a_progress, audio_size
                )
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
            def single_progress(p, s, e):
                if on_progress:
                    on_progress(p, s, e)

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
