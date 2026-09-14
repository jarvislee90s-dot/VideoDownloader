"""wechat 模块单元测试：链接解析与 feed 响应解析。全部离线（网络函数不打桩不测）。"""
import pytest

from video_downloader.wechat import _is_wechat_url, _extract_short_uri


class TestIsWechatUrl:
    def test_finder_preview_url(self):
        assert _is_wechat_url(
            "https://channels.weixin.qq.com/finder-preview/pages/sph?id=AZrL4kL5m9"
        ) is True

    def test_sph_short_url(self):
        assert _is_wechat_url("https://channels.weixin.qq.com/sph/AZrL4kL5m9") is True

    def test_weixin_sph_short_url(self):
        assert _is_wechat_url("https://weixin.qq.com/sph/AZrL4kL5m9") is True

    def test_bilibili_not_wechat(self):
        assert _is_wechat_url("https://www.bilibili.com/video/BV1xx") is False

    def test_plain_weixin_article_not_wechat(self):
        # mp.weixin.qq.com 公众号文章不是视频号
        assert _is_wechat_url("https://mp.weixin.qq.com/s/abc123") is False


class TestExtractShortUri:
    def test_finder_preview_id_param(self):
        url = "https://channels.weixin.qq.com/finder-preview/pages/sph?id=AZrL4kL5m9"
        assert _extract_short_uri(url) == "AZrL4kL5m9"

    def test_sph_path_segment(self):
        assert _extract_short_uri("https://channels.weixin.qq.com/sph/AZrL4kL5m9") == "AZrL4kL5m9"

    def test_weixin_sph_path_segment(self):
        assert _extract_short_uri("https://weixin.qq.com/sph/AZrL4kL5m9") == "AZrL4kL5m9"

    def test_trailing_query_on_sph_path(self):
        assert _extract_short_uri("https://weixin.qq.com/sph/AZrL4kL5m9?from=sync") == "AZrL4kL5m9"

    def test_unparseable_returns_none(self):
        assert _extract_short_uri("https://channels.weixin.qq.com/") is None
