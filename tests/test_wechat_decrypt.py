"""wechat_decrypt 单元测试：ISAAC64 密钥流 + XOR 解密 + 魔数校验。全部离线。"""
from video_downloader.wechat_decrypt import isaac64_keystream


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
