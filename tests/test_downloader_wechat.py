"""downloader.py 微信分支分发测试（打桩 wechat 模块，不发网络请求）。"""
import pytest

import video_downloader.wechat as wechat
from video_downloader import downloader


WECHAT_URL = "https://channels.weixin.qq.com/finder-preview/pages/sph?id=AZrL4kL5m9"


class TestPrefetchDispatch:
    def test_wechat_url_goes_to_wechat_prefetch(self, monkeypatch):
        called = {}
        monkeypatch.setattr(wechat, "prefetch_meta",
                            lambda url, on_meta=None: called.setdefault("url", url))
        downloader.prefetch_meta(WECHAT_URL, on_meta=lambda **k: None)
        assert called["url"] == WECHAT_URL

    def test_bilibili_url_not_intercepted_by_wechat(self, monkeypatch):
        called = {}
        monkeypatch.setattr(wechat, "prefetch_meta",
                            lambda url, on_meta=None: called.setdefault("hit", True))
        # bilibili 分支先于 wechat 检查，wechat.prefetch_meta 不应被调用
        monkeypatch.setattr(downloader.bilibili, "prefetch_meta",
                            lambda url, on_meta=None: called.setdefault("bili", True))
        downloader.prefetch_meta("https://www.bilibili.com/video/BV1xx411c7mD",
                                 on_meta=lambda **k: None)
        assert called.get("bili") is True
        assert "hit" not in called


class TestDownloadDispatch:
    def test_wechat_url_goes_to_wechat_download(self, monkeypatch, tmp_path):
        captured = {}

        def fake_download(url, output_path, on_progress=None, on_meta=None, **kw):
            captured["output_path"] = output_path
            from pathlib import Path
            Path(output_path).write_bytes(b"fake")
            return "标题"

        monkeypatch.setattr(wechat, "download", fake_download)
        monkeypatch.setattr(downloader.os.path, "getsize", lambda p: 4)
        meta_calls = []
        title = downloader.download(WECHAT_URL, output_dir=str(tmp_path),
                                    on_meta=lambda **k: meta_calls.append(k))
        assert title == "标题"
        assert captured["output_path"].endswith(".mp4")

    def test_generic_url_not_intercepted(self, monkeypatch, tmp_path):
        # 非微信链接不应走 wechat.download
        monkeypatch.setattr(wechat, "download",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("误入 wechat 分支")))
        monkeypatch.setattr(downloader, "_is_target_site_url", lambda u: False)
        # 通用分支走 yt-dlp，这里打桩 YoutubeDL 避免真实下载
        class FakeYDL:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download=False):
                return {"title": "t"}
            def download(self, urls): pass
        monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYDL)
        downloader.download("https://example.com/video.mp4", output_dir=str(tmp_path))

    def test_pause_exception_passes_through_unchanged(self, monkeypatch, tmp_path):
        # worker._PauseRequested 是模块私有类，暂停从 on_progress 回调栈抛出。
        # wechat 分支不得把它转成 RuntimeError，否则 worker 的
        # `except _PauseRequested` 匹配不上、暂停被误标为失败。
        class _PauseRequested(Exception):
            pass

        def fake_download(url, output_path, on_progress=None, on_meta=None, **kw):
            on_progress(50.0, 1024.0, 1.0)
            return "不会到这里"

        monkeypatch.setattr(wechat, "download", fake_download)

        def raising_progress(percent, speed, eta):
            raise _PauseRequested()

        with pytest.raises(_PauseRequested):
            downloader.download(WECHAT_URL, output_dir=str(tmp_path),
                                on_progress=raising_progress)
