import os
import sys
import threading
import webbrowser

# 确保能 import 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import run, find_free_port
from config import SERVER_HOST, SERVER_PORT


def open_browser_delayed(url: str, delay: float = 1.2):
    def _open():
        import time
        time.sleep(delay)
        webbrowser.open(url)
    threading.Thread(target=_open, daemon=True).start()


if __name__ == "__main__":
    port = find_free_port(SERVER_PORT)
    url = f"http://{SERVER_HOST}:{port}"
    print(f"启动中... 浏览器将打开 {url}")
    print("按 Ctrl+C 退出。")
    open_browser_delayed(url)
    # 复用 server.run 但用已确定的 port
    from server import create_app
    create_app().run(host=SERVER_HOST, port=port, threaded=True)
