import os
import json
import queue
import threading

from flask import Flask, request, jsonify, Response, send_from_directory

from config import SERVER_HOST, SERVER_PORT
from queue_manager import QueueManager
from worker import Worker
import downloader

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_QUEUE_FILE = os.path.join(_SCRIPT_DIR, "queue.json")


def create_app():
    app = Flask(__name__, static_folder=None)
    qm = QueueManager(queue_file=_QUEUE_FILE)
    worker = Worker(qm, download_fn=downloader.download)
    worker.start()

    # 事件广播：任何队列变更后 put 一条通知，SSE 客户端据此拉取快照
    _event_q = queue.Queue()

    def notify():
        _event_q.put(None)  # 哨兵值，触发客户端刷新

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
        return send_from_directory(os.path.join(_SCRIPT_DIR, "web"), "index.html")

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
