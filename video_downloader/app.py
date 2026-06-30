import threading
import webbrowser

from video_downloader.server import create_app, find_free_port
from video_downloader.config import SERVER_HOST, SERVER_PORT


def open_browser_delayed(url: str, delay: float = 1.2):
    def _open():
        import time
        time.sleep(delay)
        webbrowser.open(url)
    threading.Thread(target=_open, daemon=True).start()


def run():
    port = find_free_port(SERVER_PORT)
    url = f"http://{SERVER_HOST}:{port}"
    print(f"启动中... 浏览器将打开 {url}")
    print("按 Ctrl+C 退出。")
    open_browser_delayed(url)
    create_app().run(host=SERVER_HOST, port=port, threaded=True)


if __name__ == "__main__":
    run()
