import os
import re
import sys
import json
import queue
import subprocess
import threading

from flask import Flask, request, jsonify, Response, send_from_directory

from video_downloader import PROJECT_ROOT
from video_downloader.config import SERVER_HOST, SERVER_PORT, DEFAULT_OUTPUT_DIR
from video_downloader.queue_manager import QueueManager
from video_downloader.worker import Worker
import video_downloader.downloader as downloader

_QUEUE_FILE = os.path.join(PROJECT_ROOT, "queue.json")


def _downloads_path():
    """下载目录绝对路径（与 interactive 一致：相对路径基于项目根目录解析）。"""
    return os.path.normpath(
        DEFAULT_OUTPUT_DIR if os.path.isabs(DEFAULT_OUTPUT_DIR)
        else os.path.join(PROJECT_ROOT, DEFAULT_OUTPUT_DIR)
    )


def _open_folder(path: str):
    """跨平台用默认程序打开文件夹或文件。失败时打印到 stderr，便于排查。"""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        print(f"打开下载文件夹失败 ({path}): {e}", file=sys.stderr)


def create_app(queue_file=None):
    app = Flask(__name__, static_folder=None)
    qm = QueueManager(queue_file=queue_file or _QUEUE_FILE)

    # 事件广播：任何队列变更后 put 一条通知，SSE 客户端据此拉取快照
    _event_q = queue.Queue()

    def notify():
        _event_q.put(None)  # 哨兵值，触发客户端刷新

    worker = Worker(qm, download_fn=downloader.download, on_change=notify)
    worker.start()

    # 包装 qm：在变更类操作后 notify。简单起见用钩子列表
    app.config["qm"] = qm
    app.config["worker"] = worker
    app.config["notify"] = notify

    def snapshot():
        return jsonify({"paused": qm.is_paused(), "tasks": qm.list_tasks()})

    def snapshot_json():
        # SSE 流在生成器里执行，已脱离请求/应用上下文，不能用 jsonify。
        # 直接 json.dumps 构造同样的快照字符串。
        return json.dumps({"paused": qm.is_paused(), "tasks": qm.list_tasks()},
                          ensure_ascii=False)

    # ---- 任务管理 ----
    @app.post("/api/tasks")
    def add_task():
        url = (request.get_json(silent=True) or {}).get("url", "").strip()
        if not url:
            return jsonify({"error": "url 不能为空"}), 400
        task = qm.add_task(url)

        # 后台异步拉取标题/时长/总大小，让等待中的任务也能正确显示
        def _on_meta(**fields):
            qm.set_meta(task["id"], **fields)
            notify()

        def _fetch_meta():
            downloader.prefetch_meta(url, on_meta=_on_meta)

        threading.Thread(target=_fetch_meta, daemon=True).start()
        notify()
        return jsonify(task), 201

    @app.get("/api/tasks")
    def list_tasks():
        return snapshot()

    @app.delete("/api/tasks/<task_id>")
    def delete_task(task_id):
        # 若删除的是当前下载中的任务，先中断
        if worker._current_task_id == task_id:
            worker.request_pause_current()
        ok = qm.delete_task(task_id)
        notify()
        return ("", 204) if ok else (jsonify({"error": "not found"}), 404)

    @app.post("/api/tasks/<task_id>/retry")
    def retry_task(task_id):
        qm.reset_for_retry(task_id)
        notify()
        return jsonify(qm.get(task_id) or {"error": "not found"}), 200

    @app.post("/api/tasks/<task_id>/pause")
    def pause_task(task_id):
        if worker._current_task_id == task_id:
            worker.request_pause_current()  # 下载中的：中断 -> mark_paused
        else:
            qm.pause_task(task_id)          # 等待中的：直接标记
        notify()
        return jsonify(qm.get(task_id) or {"error": "not found"}), 200

    @app.post("/api/tasks/<task_id>/resume")
    def resume_task(task_id):
        qm.resume_task(task_id)
        notify()
        return jsonify(qm.get(task_id) or {"error": "not found"}), 200

    # ---- 全局控制 ----
    @app.post("/api/queue/pause")
    def pause_queue():
        qm.set_paused(True)
        notify()
        return jsonify({"paused": True})

    @app.post("/api/queue/resume")
    def resume_queue():
        qm.set_paused(False)
        notify()
        return jsonify({"paused": False})

    @app.get("/api/queue/state")
    def queue_state():
        return jsonify({"paused": qm.is_paused()})

    # ---- 下载文件夹 ----
    @app.post("/api/open-downloads")
    def open_downloads():
        path = _downloads_path()
        os.makedirs(path, exist_ok=True)
        _open_folder(path)
        return jsonify({"ok": True, "path": path})

    # ---- 播放已完成的视频 ----
    @app.post("/api/tasks/<task_id>/play")
    def play_task(task_id):
        task = qm.get(task_id)
        if not task or task.get("status") != "done":
            return jsonify({"error": "任务不存在或未完成"}), 400

        title = task.get("title") or "video"
        output_dir = _downloads_path()
        if not os.path.isdir(output_dir):
            return jsonify({"error": "下载目录不存在"}), 404

        safe_title = re.sub(r'[<>\:"/\\|?*\x00-\x1f]', '_', title)
        candidates = []
        for name in os.listdir(output_dir):
            lower = name.lower()
            if lower.endswith((".mp4", ".mkv", ".webm", ".mov", ".avi")):
                if safe_title in name or title in name:
                    candidates.append(os.path.join(output_dir, name))

        if not candidates:
            return jsonify({"error": "未找到视频文件"}), 404

        # 如果匹配到多个，取最近修改的（最可能是本次下载的）
        path = max(candidates, key=os.path.getmtime)
        _open_folder(path)
        return jsonify({"ok": True, "path": path})

    # ---- SSE ----
    @app.get("/api/events")
    def events():
        def stream():
            # 先发一次当前快照
            yield f"data: {snapshot_json()}\n\n"
            while True:
                try:
                    _event_q.get(timeout=15)
                except queue.Empty:
                    pass
                yield f"data: {snapshot_json()}\n\n"
        return Response(stream(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    # ---- 前端页面 ----
    @app.get("/")
    def index():
        return send_from_directory(os.path.join(PROJECT_ROOT, "web"), "index.html")

    return app


def find_free_port(preferred: int) -> int:
    """preferred 被占用则递增。"""
    import socket
    port = preferred
    while port < preferred + 100:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((SERVER_HOST, port))
                return port
            except OSError:
                port += 1
    return preferred


def run():
    app = create_app()
    port = find_free_port(SERVER_PORT)
    print(f"服务启动: http://{SERVER_HOST}:{port}")
    app.run(host=SERVER_HOST, port=port, threaded=True)


if __name__ == "__main__":
    run()
