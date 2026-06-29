import os
from downloader import download, _get_proxy_for_url
from config import DEFAULT_OUTPUT_DIR

print("=" * 50)
print("  视频下载工具")
print("=" * 50)

# 1. 输入 URL
url = input("\n请输入视频链接: ").strip()
if not url:
    print("链接不能为空")
    exit(1)

# 2. 自动配置：最佳质量 + 默认输出目录 + 自动代理
#    输出目录固定到脚本所在目录下，避免从其他目录（如根目录）运行时把 ./downloads 解析到只读路径。
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
output = os.path.normpath(
    DEFAULT_OUTPUT_DIR if os.path.isabs(DEFAULT_OUTPUT_DIR)
    else os.path.join(_SCRIPT_DIR, DEFAULT_OUTPUT_DIR)
)
proxy = _get_proxy_for_url(url)
proxy_val = proxy if proxy else "无(直连)"

print(f"\n配置:  质量=best  输出={output}  代理={proxy_val}")

# 3. 确认并下载（直接回车即开始）
confirm = input("\n开始下载? [Y/n]: ").strip().lower()
if confirm != "n":
    try:
        download(url, resolution="best", output_dir=output, proxy=proxy)
        print("\n下载完成!")
    except RuntimeError as e:
        print(f"\n下载失败: {e}")
else:
    print("已取消")
