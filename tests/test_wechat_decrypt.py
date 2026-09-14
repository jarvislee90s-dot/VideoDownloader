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

    def test_other_box_types_and_last_window(self):
        # styp/moov/mdat 在 offset 4 均可识别；ftyp 在 offset 28（最后一个有效窗口）也能识别
        for box in (b"styp", b"moov", b"mdat"):
            header = b"\x00\x00\x00\x18" + box + b"isom\x00\x00\x02\x00isomiso2"
            assert looks_like_media_header(header) is True, box
        tail_window = b"\x00" * 28 + b"ftyp"
        assert looks_like_media_header(tail_window) is True


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

    def test_short_file_with_default_enc_len(self):
        # 文件远小于默认 enc_len(131072)：min 截断到 len(data)，roundtrip 仍成功
        plain = b"\x00\x00\x00\x18ftypisom" + b"C" * 36
        ks = isaac64_keystream(999, len(plain))
        cipher = bytes(p ^ k for p, k in zip(plain, ks))
        result = decrypt_data(cipher, "999")  # 默认 enc_len=131072
        assert bytes(result) == plain
