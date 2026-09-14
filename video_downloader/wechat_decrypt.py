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


def decrypt_data(data: bytes, decode_key: str, enc_len: int = DEFAULT_ENC_LEN) -> bytes | bytearray | None:
    """XOR 解密 data 的前 enc_len 字节；成功返回新 bytes/bytearray，失败返回 None。

    - decodeKey 非法（空/非数字）→ None（调用方决定如何报错）
    - 解密后头部无媒体魔数 → None（视为 key 错误，保留密文由调用方处理）
    """
    try:
        seed = parse_key(decode_key)
    except (ValueError, AttributeError):
        return None
    enc_len = max(0, min(enc_len, len(data)))
    ks = isaac64_keystream(seed, enc_len)
    out = bytearray(data)
    for i in range(enc_len):
        out[i] ^= ks[i]
    if not looks_like_media_header(bytes(out[:32])):
        return None
    return out
