# 微信视频号下载支持 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** VideoDownloader 新增微信视频号（channels.weixin.qq.com）下载：贴链接 → 队列 → curl_cffi 带浏览器登录态取流 → 下载 → 自动解密 → 可播放 mp4。

**Architecture:** 完全复刻现有 B站 模式——新增 `wechat.py`（链接解析 + cookies + feed API + 流下载）与 `wechat_decrypt.py`（ISAAC64 + XOR 解密，纯函数独立模块）；`downloader.py` 的 `prefetch_meta()` 与 `download()` 各加一个分支。worker/queue_manager/server/前端零改动。

**Tech Stack:** Python 3, curl_cffi（`impersonate="chrome"`）, browser_cookie3（已在 requirements.txt）, pytest。

**Spec:** `docs/superpowers/specs/2026-09-14-wechat-channels-download-design.md`

**实现基准说明（给执行者的关键背景）：**
- 解密算法唯一实现基准是 wx_channel（nobiyou/wx_channel v5.7.9，2026-09 仍活跃）`pkg/util/isaac64.go`。注意另一份源码 JohnABC/WechatSphDecrypt `decrypt.go` 的 `rand64Init` 末尾**不调用** `isaac64()`，而 wx_channel 的 `randinit` 末尾**调用**——两者第一轮输出不同。以 wx_channel 为准（它与官方 WASM `WxIsaac64(seed).generate(131072)` 的行为对应）。
- 本计划中 ISAAC64 的 Python 参考实现已预先验证过，测试向量（必须使用，勿自行重算）：
  - seed=0 → `9d39247e33776d412af7398005aaa5c7`（前 16 字节 hex）
  - seed=1 → `e19ed5d2ca98af2da7a18d07cab39b52`
  - seed=12345 → `e8721821768522e9899fb4a5e5539b95a6184afec644bf538196a38b9ba64ae7`（前 32 字节 hex）
- 生成语义：每步 `randcnt -= 1` 后取 `randrsl[randcnt]`（倒序），uint64 按 8 字节小端拆分**再反转字节序**输出。
- 加密区长度：优先读 CDN 响应头 `X-enclen`（十进制字符串），缺失/非法回退 131072。
- decodeKey 是十进制字符串（如 `"123456789"`），`int(decode_key)` 直接转 seed；空串/非数字 → 无加密或报错。
- 解密校验：解密后前 32 字节窗口内出现 `ftyp`/`styp`/`moov`/`mdat` 任一魔数即通过（对齐 wx_channel `looksLikeMediaHeader`）。
- 现有测试基线：`python -m pytest tests/ -q` → 14 passed。每个任务结束时必须保持全绿。

**网络相关注意：** Task 1/2/3 全部离线（纯函数），可无网执行。Task 4 起需要网络与用户已扫码登录的 Chrome。Windows 环境（Git Bash），路径用正斜杠。

---

### Task 1: wechat_decrypt.py — ISAAC64 密钥流生成

**Files:**
- Create: `video_downloader/wechat_decrypt.py`
- Test: `tests/test_wechat_decrypt.py`

- [ ] **Step 1: 写失败测试（密钥流向量）**

```python
"""wechat_decrypt 单元测试：ISAAC64 密钥流 + XOR 解密 + 魔数校验。全部离线。"""
import pytest

from video_downloader.wechat_decrypt import (
    isaac64_keystream,
    decrypt_data,
    looks_like_media_header,
    parse_key,
)


class TestIsaac64Keystream:
    # 以下向量由 wx_channel pkg/util/isaac64.go 的语义预先计算验证，勿改动
    def test_seed_0(self):
        assert isaac64_keystream(0, 16).hex() == "9d39247e33776d412af7398005aaa5c7"

    def test_seed_1(self):
        assert isaac64_keystream(1, 16).hex() == "e19ed5d2ca98af2da7a18d07cab39b52"

    def test_seed_12345_32bytes(self):
        assert isaac64_keystream(12345, 32).hex() == (
            "e8721821768522e9899fb4a5e5539b95a6184afec644bf538196a38b9ba64ae7"
        )

    def test_length_shorter_than_one_block(self):
        assert len(isaac64_keystream(12345, 3)) == 3

    def test_length_spanning_multiple_blocks(self):
        # 131072 = 128KB 需要跨多轮 isaac64()（每轮 256 个 uint64 = 2048 字节）
        ks = isaac64_keystream(1, 131072)
        assert len(ks) == 131072
        # 确定性：同样 seed 两次生成一致
        assert ks == isaac64_keystream(1, 131072)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wechat_decrypt.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'video_downloader.wechat_decrypt'`

- [ ] **Step 3: 写最小实现（逐行翻译 wx_channel isaac64.go）**

```python
"""微信视频号加密流解密（ISAAC64 PRNG + XOR）。

算法逐行翻译自 nobiyou/wx_channel v5.7.9 的 pkg/util/isaac64.go（Go），
对应官方 WASM decrypt-video-core 的 WxIsaac64(seed).generate(131072) 语义：
- 状态：randrsl[256]、randcnt、mm[256]、aa/bb/cc
- randinit：golden 常量 mix 4 轮 → 两轮 mm 填充（先 +randrsl 后 +mm）→ 末尾跑一轮 isaac64() → randcnt=256
- generate：每次 randcnt-1 后取 randrsl[randcnt]（倒序），uint64 小端拆分后反转字节序
注意：JohnABC/WechatSphDecrypt 的 rand64Init 末尾不跑 isaac64()，与本实现不兼容，勿混用。
"""
MASK64 = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C13


class _Isaac64:
    def __init__(self, seed: int):
        self.randrsl = [0] * 256
        self.randcnt = 0
        self.mm = [0] * 256
        self.aa = self.bb = self.cc = 0
        self.randrsl[0] = seed & MASK64
        self._randinit()

    @staticmethod
    def _mix(a, b, c, d, e, f, g, h):
        a = (a - e) & MASK64; f ^= h >> 9;  h = (h + a) & MASK64
        b = (b - f) & MASK64; g ^= (a << 9) & MASK64; a = (a + b) & MASK64
        c = (c - g) & MASK64; h ^= b >> 23; b = (b + c) & MASK64
        d = (d - h) & MASK64; a ^= (c << 15) & MASK64; c = (c + d) & MASK64
        e = (e - a) & MASK64; b ^= d >> 14; d = (d + e) & MASK64
        f = (f - b) & MASK64; c ^= (e << 20) & MASK64; e = (e + f) & MASK64
        g = (g - c) & MASK64; d ^= f >> 17; f = (f + g) & MASK64
        h = (h - d) & MASK64; e ^= (g << 14) & MASK64; g = (g + h) & MASK64
        return a, b, c, d, e, f, g, h

    def _randinit(self):
        a = b = c = d = e = f = g = h = _GOLDEN
        for _ in range(4):
            a, b, c, d, e, f, g, h = self._mix(a, b, c, d, e, f, g, h)
        for j in range(0, 256, 8):
            a = (a + self.randrsl[j]) & MASK64
            b = (b + self.randrsl[j + 1]) & MASK64
            c = (c + self.randrsl[j + 2]) & MASK64
            d = (d + self.randrsl[j + 3]) & MASK64
            e = (e + self.randrsl[j + 4]) & MASK64
            f = (f + self.randrsl[j + 5]) & MASK64
            g = (g + self.randrsl[j + 6]) & MASK64
            h = (h + self.randrsl[j + 7]) & MASK64
            a, b, c, d, e, f, g, h = self._mix(a, b, c, d, e, f, g, h)
            self.mm[j:j + 8] = [a, b, c, d, e, f, g, h]
        for j in range(0, 256, 8):
            a = (a + self.mm[j]) & MASK64
            b = (b + self.mm[j + 1]) & MASK64
            c = (c + self.mm[j + 2]) & MASK64
            d = (d + self.mm[j + 3]) & MASK64
            e = (e + self.mm[j + 4]) & MASK64
            f = (f + self.mm[j + 5]) & MASK64
            g = (g + self.mm[j + 6]) & MASK64
            h = (h + self.mm[j + 7]) & MASK64
            a, b, c, d, e, f, g, h = self._mix(a, b, c, d, e, f, g, h)
            self.mm[j:j + 8] = [a, b, c, d, e, f, g, h]
        self._isaac64()
        self.randcnt = 256

    def _isaac64(self):
        self.cc = (self.cc + 1) & MASK64
        self.bb = (self.bb + self.cc) & MASK64
        for j in range(256):
            x = self.mm[j]
            m = j % 4
            if m == 0:
                self.aa = (~(self.aa ^ ((self.aa << 21) & MASK64))) & MASK64
            elif m == 1:
                self.aa ^= self.aa >> 5
            elif m == 2:
                self.aa ^= (self.aa << 12) & MASK64
            else:
                self.aa ^= self.aa >> 33
            self.aa = (self.aa + self.mm[(j + 128) % 256]) & MASK64
            y = (self.mm[(x >> 3) % 256] + self.aa + self.bb) & MASK64
            self.mm[j] = y
            self.bb = (self.mm[(y >> 11) % 256] + x) & MASK64
            self.randrsl[j] = self.bb

    def generate(self, length: int) -> bytes:
        out = bytearray()
        while len(out) < length:
            if self.randcnt == 0:
                self._isaac64()
                self.randcnt = 256
            self.randcnt -= 1
            val = self.randrsl[self.randcnt]
            out += val.to_bytes(8, "little")[::-1]
        return bytes(out[:length])


def isaac64_keystream(seed: int, length: int) -> bytes:
    """生成 ISAAC64 密钥流（对应 wx_channel GenerateDecryptorArray(seed, length)）。"""
    return _Isaac64(seed).generate(length)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_wechat_decrypt.py -v`
Expected: PASS（5 个测试）

- [ ] **Step 5: Commit**

```bash
git add video_downloader/wechat_decrypt.py tests/test_wechat_decrypt.py
git commit -m "feat(wechat): ISAAC64 密钥流生成（翻译自 wx_channel isaac64.go）"
```

---

### Task 2: wechat_decrypt.py — parse_key / decrypt_data / looks_like_media_header

**Files:**
- Modify: `video_downloader/wechat_decrypt.py`（追加 3 个函数）
- Test: `tests/test_wechat_decrypt.py`（追加测试类）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_wechat_decrypt.py`：

```python
class TestParseKey:
    def test_decimal_string(self):
        assert parse_key("12345") == 12345

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            parse_key("abc")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_key("")


class TestLooksLikeMediaHeader:
    def test_ftyp_at_offset_4(self):
        # 标准 MP4 头：4 字节 size + "ftyp"
        header = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
        assert looks_like_media_header(header) is True

    def test_random_garbage(self):
        assert looks_like_media_header(bytes(range(32))) is False

    def test_too_short(self):
        assert looks_like_media_header(b"\x00\x00") is False


class TestDecryptData:
    def test_roundtrip(self):
        # 任意明文（含 ftyp 魔数）→ XOR 加密 → decrypt_data 还原
        plain = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"A" * 16
        ks = isaac64_keystream(777, len(plain))
        cipher = bytes(p ^ k for p, k in zip(plain, ks))
        result = decrypt_data(cipher, "777", enc_len=len(plain))
        assert result == plain

    def test_only_encrypted_prefix_is_decrypted(self):
        # enc_len 之后的部分保持不变（模拟 128KB 加密区 + 明文尾部）
        plain = b"\x00\x00\x00\x18ftypisom" + b"B" * 8
        tail = b"TAIL" * 2  # enc_len 之外的尾部
        ks = isaac64_keystream(888, len(plain))
        cipher = bytes(p ^ k for p, k in zip(plain, ks)) + tail
        result = decrypt_data(cipher, "888", enc_len=len(plain))
        assert result[:len(plain)] == plain
        assert result[len(plain):] == tail

    def test_invalid_key_returns_none(self):
        assert decrypt_data(b"\x00" * 32, "not-a-number") is None

    def test_decrypted_still_garbage_returns_none(self):
        # 错误 key：解密后无魔数 → 返回 None
        garbage = bytes(range(256)) * 4  # 1KB，无 ftyp/moov 等魔数窗口
        result = decrypt_data(garbage, "1", enc_len=len(garbage))
        assert result is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wechat_decrypt.py -v`
Expected: FAIL，`ImportError: cannot import name 'parse_key'`

- [ ] **Step 3: 实现（追加到 wechat_decrypt.py 末尾）**

```python
# ---- 加密区长度默认值（wx_channel crypto_helper.go: prefixLen = 131072）----
DEFAULT_ENC_LEN = 131072

# MP4/流媒体魔数（对齐 wx_channel looksLikeMediaHeader：前 32 字节窗口内任意 box type）
_MEDIA_BOX_TYPES = (b"ftyp", b"styp", b"moov", b"mdat")


def parse_key(decode_key: str) -> int:
    """decodeKey 十进制字符串 → seed。非法输入抛 ValueError（对齐 wx_channel ParseKey）。"""
    return int(decode_key.strip())


def looks_like_media_header(data: bytes) -> bool:
    """解密校验：前 32 字节窗口内出现 ftyp/styp/moov/mdat 任一即视为媒体文件。"""
    limit = min(len(data), 32)
    for i in range(4, limit - 3):
        if data[i:i + 4] in _MEDIA_BOX_TYPES:
            return True
    return False


def decrypt_data(data: bytes, decode_key: str, enc_len: int = DEFAULT_ENC_LEN) -> bytes | None:
    """XOR 解密 data 的前 enc_len 字节；成功返回新 bytes，失败返回 None。

    - decodeKey 非法（空/非数字）→ None（调用方决定如何报错）
    - 解密后头部无媒体魔数 → None（视为 key 错误，保留密文由调用方处理）
    """
    try:
        seed = parse_key(decode_key)
    except (ValueError, AttributeError):
        return None
    enc_len = min(enc_len, len(data))
    ks = isaac64_keystream(seed, enc_len)
    out = bytearray(data)
    for i in range(enc_len):
        out[i] ^= ks[i]
    if not looks_like_media_header(bytes(out[:32])):
        return None
    return bytes(out)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_wechat_decrypt.py -v`
Expected: PASS（全部 12 个测试）

- [ ] **Step 5: 全量回归 + Commit**

Run: `python -m pytest tests/ -q` → Expected: 26 passed（14 旧 + 12 新）

```bash
git add video_downloader/wechat_decrypt.py tests/test_wechat_decrypt.py
git commit -m "feat(wechat): XOR 解密 + decodeKey 解析 + 魔数校验"
```

---

### Task 3: wechat.py — 链接解析与配置

**Files:**
- Modify: `video_downloader/config.py`（文件末尾追加 2 个常量）
- Create: `video_downloader/wechat.py`（本任务只写解析部分）
- Test: `tests/test_wechat.py`

- [ ] **Step 1: config.py 追加常量**

在 `video_downloader/config.py` 末尾追加：

```python

# ---- 微信视频号（wechat channels）配置 ----
WECHAT_COOKIE_BROWSER = "chrome"  # browser_cookie3 读取哪个浏览器的登录态
WECHAT_FEED_API_URL = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
```

- [ ] **Step 2: 写失败测试**

```python
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
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/test_wechat.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'video_downloader.wechat'`

- [ ] **Step 4: 实现 wechat.py（本任务只含解析；后续任务追加）**

```python
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
```

- [ ] **Step 5: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_wechat.py tests/ -q`
Expected: PASS（新增 9 个；总计 35 passed）

- [ ] **Step 6: Commit**

```bash
git add video_downloader/config.py video_downloader/wechat.py tests/test_wechat.py
git commit -m "feat(wechat): 视频号链接解析（finder-preview 与 /sph/ 两种形态）"
```

---

### Task 4: wechat.py — feed API 客户端与响应解析

**Files:**
- Modify: `video_downloader/wechat.py`（追加 cookies/取流/解析函数）
- Test: `tests/test_wechat.py`（追加响应解析测试）

- [ ] **Step 1: 写失败测试（响应解析纯函数，可离线测）**

追加到 `tests/test_wechat.py`：

```python
from video_downloader.wechat import _parse_feed_response


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wechat.py -v -k ParseFeed`
Expected: FAIL，`ImportError: cannot import name '_parse_feed_response'`

- [ ] **Step 3: 实现 cookies 获取 + feed API 调用 + 响应解析**

追加到 `video_downloader/wechat.py`：

```python
import time

from curl_cffi import requests as cffi_requests


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
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/ -q`
Expected: 44 passed（35 + 9 新增）

- [ ] **Step 5: 冒烟验证（需网络，不需登录态即可验证报错路径）**

Run: `python -c "
from video_downloader.wechat import _get_feed_info, WechatLoginRequired
try:
    _get_feed_info('AZrL4kL5m9')
    print('UNEXPECTED: got data without login')
except WechatLoginRequired as e:
    print('OK, login hint:', e)
except Exception as e:
    print('OTHER:', type(e).__name__, e)
"`
Expected（未登录环境）: `OK, login hint: 未检测到 Chrome 登录态...`；
（已登录环境）: 打印 `UNEXPECTED` 或正常返回——两种都算通过，主要验证不崩溃。
若用户 Chrome 未登录视频号，此步输出 OK 即达标。

- [ ] **Step 6: Commit**

```bash
git add video_downloader/wechat.py tests/test_wechat.py
git commit -m "feat(wechat): feed API 客户端 + 响应防御式解析 + 登录态异常"
```

---

### Task 5: wechat.py — 流下载 + 解密编排 + prefetch/download 入口

**Files:**
- Modify: `video_downloader/wechat.py`（追加下载与入口函数）
- Test: `tests/test_wechat.py`（追加 slugify 测试）

- [ ] **Step 1: 写失败测试（slugify 离线可测）**

追加到 `tests/test_wechat.py`：

```python
from video_downloader.wechat import _slugify


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wechat.py -v -k Slugify`
Expected: FAIL，`ImportError: cannot import name '_slugify'`

- [ ] **Step 3: 实现流下载 + 入口函数**

追加到 `video_downloader/wechat.py`：

```python
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _slugify(text: str | None) -> str:
    """文件名安全化（与 bilibili._slugify 同规则）。"""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text or "video")
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:80] or "video"


def _download_stream(url: str, tmp_path: Path, on_progress=None,
                     total_size: int | None = None, state: dict | None = None) -> None:
    """curl_cffi 流式下载（结构对齐 bilibili._download_stream）。

    - 必须 CDN 同源 Referer，否则 403
    - state 非 None 时（视频/音频双路场景，视频号当前为单流，保留扩展位）
      进度转发给合并器；否则直接回调
    - enc_len（X-enclen 响应头）由调用方从 resp.headers 读取，这里透传
    """
    import time as _time

    r = cffi_requests.get(
        url,
        headers={"Referer": "https://channels.weixin.qq.com/"},
        impersonate="chrome",
        timeout=120,
        stream=True,
    )
    if r.status_code == 403:
        raise WechatLoginRequired("登录态可能已失效，请重新扫码登录后重试")
    r.raise_for_status()
    get_cl = int(r.headers.get("Content-Length", 0) or 0)
    total = get_cl if get_cl > (total_size or 0) else (total_size or get_cl)
    downloaded = 0
    start = _time.monotonic()
    with open(tmp_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=256 * 1024):
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)
            if on_progress and total:
                percent = downloaded / total * 100
                elapsed = _time.monotonic() - start
                speed = downloaded / elapsed if elapsed > 0 else 0
                eta = max((total - downloaded) / speed, 0) if speed > 0 else 0
                on_progress(percent, speed, eta)
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
    """入队后异步拉标题/时长/大小（对齐 bilibili.prefetch_meta 签名语义）。"""
    if not on_meta:
        return
    short_uri = _extract_short_uri(url)
    if not short_uri:
        return
    try:
        raw = _get_feed_info(short_uri)
        info = _parse_feed_response(raw)
    except WechatLoginRequired:
        # 预取失败不影响入队，下载阶段会给出登录提示
        return
    except Exception:
        return
    on_meta(title=info["title"], duration=info["duration"], filesize=info["filesize"])


def download(url: str, output_path: str, on_progress=None, on_meta=None,
             browser: str = WECHAT_COOKIE_BROWSER) -> str:
    """下载视频号视频到 output_path，返回标题。

    流程：feed API 取流 → curl_cffi 下载（.part）→ 需要则解密 → 校验 → 改名。
    登录态缺失/失效抛 WechatLoginRequired（worker 捕获后按普通失败处理，错误信息面向用户）。
    """
    import time as _time

    short_uri = _extract_short_uri(url)
    if not short_uri:
        raise RuntimeError("无法从链接解析视频号 id（支持 finder-preview?id= 与 /sph/ 两种形态）")

    raw = _get_feed_info(short_uri)
    info = _parse_feed_response(raw)
    if info["error"]:
        raise WechatLoginRequired(f"请先在 Chrome 扫码登录视频号网页版后重试（接口返回：{info['error']}）")
    if not info["video_url"]:
        raise RuntimeError("该内容可能是图片/图文类型，暂不支持下载")
    if on_meta:
        on_meta(title=info["title"], duration=info["duration"], filesize=info["filesize"])

    title = info["title"] or short_uri
    safe_title = _slugify(title)
    out_dir = os.path.dirname(output_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    final_path = Path(output_path)
    part_path = final_path.with_name(final_path.name + ".part")

    enc_len: int | None = None

    def _stream_progress(p, s, e):
        if on_progress:
            on_progress(p, s, e)

    # 取真实 Content-Length 与 X-enclen 需要在流响应里读，_download_stream 内部已处理进度；
    # X-enclen 在这里通过第二次轻量 HEAD 获取（失败忽略，回退默认值）
    try:
        head = cffi_requests.head(
            info["video_url"],
            headers={"Referer": "https://channels.weixin.qq.com/"},
            impersonate="chrome", timeout=15,
        )
        enc_len = _read_x_enclen(head.headers)
    except Exception:
        enc_len = None

    try:
        _download_stream(info["video_url"], part_path, _stream_progress,
                         total_size=info["filesize"])

        # 加密流：解密 + 魔数校验；失败保留密文（.encrypted）便于诊断
        if info["decode_key"]:
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
```

并在文件顶部 import 区补充：

```python
from video_downloader.wechat_decrypt import DEFAULT_ENC_LEN, decrypt_data
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/ -q`
Expected: 48 passed（44 + 4 slugify）

- [ ] **Step 5: Commit**

```bash
git add video_downloader/wechat.py tests/test_wechat.py
git commit -m "feat(wechat): 流下载 + ISAAC64 解密编排 + prefetch/download 入口"
```

---

### Task 6: downloader.py 集成（两个分支）

**Files:**
- Modify: `video_downloader/downloader.py:286-324`（prefetch_meta 加分支）、`video_downloader/downloader.py:327-361`（download 加分支）
- Test: `tests/test_downloader_wechat.py`

- [ ] **Step 1: 写失败测试（用 monkeypatch 打桩 wechat 模块，验证分发正确）**

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_downloader_wechat.py -v`
Expected: FAIL（wechat 分支不存在，prefetch/download 走通用路径）

- [ ] **Step 3: downloader.py 加两个分支**

`video_downloader/downloader.py` 顶部 import 区补充：

```python
from video_downloader import wechat
```

`prefetch_meta()`（现 286 行）中，在 B站 分支之后插入：

```python
    # 微信视频号：专用 feed API（curl_cffi + 浏览器登录 cookies）
    if wechat._is_wechat_url(url):
        try:
            wechat.prefetch_meta(url, on_meta=on_meta)
        except Exception:
            pass
        return
```

`download()`（现 327 行）中，在 B站 分支（`if bilibili._is_bilibili_url(url):` 块）之后插入：

```python
    # 微信视频号：feed API 取流 + ISAAC64 解密（curl_cffi + 浏览器登录 cookies）
    if wechat._is_wechat_url(url):
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, "video.mp4")
        print("\n正在下载: 微信视频号")
        try:
            title = wechat.download(
                url,
                output_path=out_path,
                on_progress=on_progress,
                on_meta=on_meta,
            )
            if title:
                new_path = os.path.join(
                    output_dir, f"{re.sub(r'[<>:\"/\\\\|?*]', '_', title)}.mp4")
                if new_path != out_path:
                    os.replace(out_path, new_path)
                    out_path = new_path
            if on_meta and os.path.exists(out_path):
                on_meta(filesize=os.path.getsize(out_path))
            return title
        except wechat.WechatLoginRequired as e:
            # 登录类错误不重试无意义，但沿用全局重试机制（3 次后进 failed 态显示提示）
            raise RuntimeError(str(e))
        except Exception as e:
            raise RuntimeError(f"微信视频号下载失败：{e}")
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/ -q`
Expected: 53 passed（48 + 5 新增）

- [ ] **Step 5: CLI 快速冒烟（不需要登录态，验证失败路径的报错可读）**

Run: `python main.py "https://channels.weixin.qq.com/finder-preview/pages/sph?id=AZrL4kL5m9" 2>&1 | tail -3`
Expected: 明确的失败信息（登录提示或下载失败），而非堆栈崩溃。

- [ ] **Step 6: Commit**

```bash
git add video_downloader/downloader.py tests/test_downloader_wechat.py
git commit -m "feat(wechat): downloader 集成视频号分支（prefetch + download）"
```

---

### Task 7: README 更新 + 端到端手工验收

**Files:**
- Modify: `README.md`（功能列表、常见问题、项目结构）

- [ ] **Step 1: 更新 README**

三处修改：

1. 简介段「支持通用站点、加密 HLS 流以及 **B站（bilibili.com）**」后追加「以及 **微信视频号（channels.weixin.qq.com）**」
2. 「常见问题」追加一节：

```markdown
- **微信视频号下载失败提示"未检测到 Chrome 登录态"**：视频号需要登录态。先用 Chrome 打开 [channels.weixin.qq.com](https://channels.weixin.qq.com) 扫码登录，然后回到队列点任务的重试 ↻。登录态失效（403）时同样重新扫码即可。加密视频解密失败时会保留 `.encrypted` 密文文件，请携带链接反馈。
```

3. 项目结构 `video_downloader/` 树中，`bilibili.py` 行后追加：

```
│   ├── wechat.py              # 微信视频号（channels.weixin.qq.com）下载专用逻辑
│   ├── wechat_decrypt.py      # 视频号加密流解密（ISAAC64 + XOR）
```

- [ ] **Step 2: 全量回归**

Run: `python -m pytest tests/ -q` → Expected: 53 passed

```bash
git add README.md
git commit -m "docs: README 补充微信视频号支持说明"
```

- [ ] **Step 3: 端到端手工验收（需要用户配合：Chrome 扫码登录视频号）**

由用户在 Chrome 打开 `https://channels.weixin.qq.com` 扫码登录后，执行：

```bash
python run_server.py
```

浏览器队列页依次验证（对应 spec 三条验收标准）：

1. 粘贴 `https://channels.weixin.qq.com/finder-preview/pages/sph?id=AZrL4kL5m9` → 加入队列 → 等待中即显示标题/时长 → 下载中进度条推进 → 完成后文件可播放（对应验收①）
2. 粘贴 `https://weixin.qq.com/sph/<某短链>`（或 `channels.weixin.qq.com/sph/xxx`）→ 同样完成（对应验收②）
3. 临时清掉 Chrome 的视频号 cookies（或用无痕环境的备份），加一个任务 → 失败信息为「未检测到 Chrome 登录态，请先用 Chrome 打开 channels.weixin.qq.com 扫码登录，然后点重试」（对应验收③）

若第 1/2 步下载的是加密流且解密失败（保留 .encrypted）：这是唯一需要现场调试的场景——把 .encrypted 文件与链接反馈给计划作者，用真实样本核对 X-enclen 头与 decodeKey 数值范围。

- [ ] **Step 4: 验收通过后收尾提交**

```bash
git add -A
git commit -m "chore: 微信视频号下载支持验收通过" --allow-empty
```

---

## 任务依赖与执行顺序

Task 1 → 2 → 3 → 4 → 5 → 6 → 7 严格线性（后一个任务 import 前一个的产物）。Task 1-3 离线可执行；Task 4 Step 5、Task 6 Step 5、Task 7 Step 3 需要网络/用户登录配合。

## 自审记录（writing-plans Self-Review）

1. **Spec coverage**：spec 的"架构"两文件（wechat.py、wechat_decrypt.py）→ Task 1-5；"downloader.py 两处分支" → Task 6；"错误处理表"五行 → Task 4（WechatLoginRequired：无 cookies/401）、Task 5（无 videoUrl→图文提示、403→重扫码、解密失败→保留 .encrypted）；"测试策略"三类单测 → Task 1/2（解密）、Task 3/4/5（链接解析+响应解析）、Task 6（分发）；三条验收标准 → Task 7 Step 3。config 常量 → Task 3 Step 1。slugify → Task 5。无遗漏。
2. **Placeholder scan**：无 TBD/TODO；所有代码步骤含完整代码；"追加到 XX"均给出完整代码块。
3. **Type consistency**：`_parse_feed_response` 返回 dict 键（title/duration/filesize/video_url/decode_key/author/error）在 Task 4 测试与 Task 5 `download()`/`prefetch_meta()` 使用一致；`WechatLoginRequired` 在 Task 4 定义、Task 5/6 抛出/捕获一致；`decrypt_data(data, decode_key: str, enc_len: int)` 在 Task 2 定义、Task 5 调用（传字符串 decode_key）一致；`isaac64_keystream(seed: int, length: int)` 与测试向量一致。
