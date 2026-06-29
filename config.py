# 分辨率到 yt-dlp format 字符串的映射
RESOLUTION_FORMATS = {
    "best": "bestvideo*+bestaudio/best",
    "2160p": "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    "audio": "bestaudio/best",
}
DEFAULT_RESOLUTION = "best"
DEFAULT_OUTPUT_DIR = "./downloads"

# 代理设置：None = 不使用代理（Bilibili 等国内站不需要）
# 下载 91nt.com 等被墙站点时，在交互界面或参数文件中指定代理
# 格式如 "http://127.0.0.1:29290"
DEFAULT_PROXY = None

# 站点专属代理配置（优先级高于 DEFAULT_PROXY）
SITE_PROXY_MAP = {
    "91nt.com": "http://127.0.0.1:29290",
}


# ---- 下载队列（网页前端）配置 ----
QUEUE_FILE = "queue.json"          # 队列持久化文件（相对于脚本目录解析）
MAX_RETRIES = 3                    # 单任务最大自动重试次数
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5000                 # 被占用时自动递增到 5001、5002...
