import argparse
from downloader import download, list_formats

parser = argparse.ArgumentParser(description="视频下载工具 (yt-dlp)")
parser.add_argument("url", help="视频网页链接")
parser.add_argument("-r", "--resolution", default="best",
                    choices=["best", "2160p", "1080p", "720p", "480p", "360p", "audio"],
                    help="目标分辨率 (默认: best)")
parser.add_argument("-o", "--output", default="./downloads", help="输出目录")
parser.add_argument("-l", "--list-formats", action="store_true", help="仅列出可用格式，不下载")

args = parser.parse_args()

if args.list_formats:
    list_formats(args.url)
else:
    download(args.url, resolution=args.resolution, output_dir=args.output)
