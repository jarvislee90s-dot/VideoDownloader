"""wechat 模块单元测试：链接解析与 feed 响应解析。全部离线（网络函数不打桩不测）。"""
import pytest

from video_downloader.wechat import _is_wechat_url, _extract_short_uri
from video_downloader.wechat import _parse_feed_response
from video_downloader.wechat import _slugify


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


def _feed_json(**overrides):
    """构造标准 feed 响应样本，允许字段覆盖。"""
    feed_info = {
        "description": "测试视频标题",
        "durationMs": 65000,
        "fileSize": 1048576,
        "decodeKey": "",
        "h264VideoInfo": {"videoUrl": "https://cdn.example.com/h264.m3u8?v=1"},
        "h265VideoInfo": {"videoUrl": "https://cdn.example.com/h265.m3u8?v=1"},
    }
    feed_info.update(overrides.pop("feed_info", {}))
    payload = {
        "errCode": 0,
        "errMsg": "ok",
        "data": {
            "feedInfo": feed_info,
            "authorInfo": {"nickname": "测试作者"},
        },
    }
    payload.update(overrides)
    return payload


class TestParseFeedResponse:
    def test_ok_response(self):
        r = _parse_feed_response(_feed_json())
        assert r["title"] == "测试视频标题"
        assert r["duration"] == 65.0  # durationMs → 秒
        assert r["filesize"] == 1048576
        assert r["video_url"] == "https://cdn.example.com/h264.m3u8?v=1"
        assert r["decode_key"] == ""
        assert r["author"] == "测试作者"

    def test_h264_preferred_over_h265(self):
        r = _parse_feed_response(_feed_json())
        assert "h264" in r["video_url"]

    def test_fallback_to_plain_video_url(self):
        sample = _feed_json(feed_info={"h264VideoInfo": None, "h265VideoInfo": None,
                                       "videoUrl": "https://cdn.example.com/plain.mp4"})
        r = _parse_feed_response(sample)
        assert r["video_url"] == "https://cdn.example.com/plain.mp4"

    def test_fallback_to_h265(self):
        sample = _feed_json(feed_info={"h264VideoInfo": None})
        r = _parse_feed_response(sample)
        assert "h265" in r["video_url"]

    def test_no_video_url(self):
        sample = _feed_json(feed_info={"h264VideoInfo": None, "h265VideoInfo": None})
        r = _parse_feed_response(sample)
        assert r["video_url"] is None

    def test_decode_key_present(self):
        sample = _feed_json(feed_info={"decodeKey": "123456789"})
        r = _parse_feed_response(sample)
        assert r["decode_key"] == "123456789"

    def test_errcode_nonzero(self):
        sample = _feed_json(errCode=-1, errMsg="permission verification failed")
        r = _parse_feed_response(sample)
        assert r["error"] == "permission verification failed"

    def test_missing_fields_tolerated(self):
        # 响应结构异常时不抛异常，字段为 None
        r = _parse_feed_response({"data": {}})
        assert r["title"] is None
        assert r["video_url"] is None

    def test_filesize_missing(self):
        sample = _feed_json(feed_info={"fileSize": 0})
        r = _parse_feed_response(sample)
        assert r["filesize"] is None


class TestSlugify:
    def test_illegal_chars_replaced(self):
        assert _slugify('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"

    def test_truncated_to_80(self):
        assert len(_slugify("长" * 100)) == 80

    def test_empty_fallback(self):
        assert _slugify("") == "video"
        assert _slugify(None) == "video"

    def test_collapsed_underscores(self):
        assert _slugify("a///b___c") == "a_b_c"
