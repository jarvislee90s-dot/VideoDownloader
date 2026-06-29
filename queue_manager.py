import json
import os
import time
import threading
import secrets


class QueueManager:
    """下载队列状态机 + 持久化。所有公开方法线程安全。"""

    def __init__(self, queue_file: str):
        self._file = queue_file
        self._lock = threading.Lock()
        self._tasks = []          # list[dict]
        self._paused = False
        self._load()

    # ---- 持久化 ----
    def _load(self):
        """启动时加载；损坏则备份并从空开始。downloading 任务回退为 pending。"""
        if not os.path.exists(self._file):
            self._tasks, self._paused = [], False
            return
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._tasks = data.get("tasks", [])
            self._paused = data.get("paused", False)
            for t in self._tasks:
                if t["status"] == "downloading":
                    t["status"] = "pending"
                    t["progress"] = 0
                    t["speed"] = None
        except (json.JSONDecodeError, OSError, KeyError):
            # 损坏：备份后从空开始
            try:
                os.replace(self._file, self._file + ".bak")
            except OSError:
                pass
            self._tasks, self._paused = [], False

    def _save(self):
        tmp = self._file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"paused": self._paused, "tasks": self._tasks}, f,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, self._file)  # 原子写

    # ---- 增删查 ----
    def add_task(self, url: str) -> dict:
        with self._lock:
            task = {
                "id": secrets.token_hex(4),
                "url": url,
                "title": None,
                "status": "pending",
                "progress": 0,
                "speed": None,
                "added_at": int(time.time()),
                "retries": 0,
                "error": None,
            }
            self._tasks.append(task)
            self._save()
            return dict(task)

    def get(self, task_id: str):
        with self._lock:
            for t in self._tasks:
                if t["id"] == task_id:
                    return dict(t)
            return None

    def list_tasks(self):
        with self._lock:
            return [dict(t) for t in self._tasks]

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t["id"] != task_id]
            changed = len(self._tasks) < before
            if changed:
                self._save()
            return changed

    def next_pending(self):
        """返回第一个 pending 任务（不改变状态）。无则返回 None。"""
        with self._lock:
            for t in self._tasks:
                if t["status"] == "pending":
                    return dict(t)
            return None

    # ---- 状态流转 ----
    def mark_downloading(self, task_id: str):
        with self._lock:
            self._mut(task_id, lambda t: t.update(status="downloading", progress=0))
            self._save()

    def update_progress(self, task_id: str, percent: float, speed: float, eta: float):
        with self._lock:
            self._mut(task_id, lambda t: t.update(progress=percent, speed=speed))
            self._save()

    def mark_done(self, task_id: str, title: str):
        with self._lock:
            self._mut(task_id, lambda t: t.update(status="done", progress=100,
                                                   speed=None, title=title, error=None))
            self._save()

    def mark_failed_retry(self, task_id: str, error: str, max_retries: int):
        """失败：retries<max 则回 pending 重试，否则 failed。"""
        with self._lock:
            def f(t):
                t["retries"] += 1
                t["error"] = error
                if t["retries"] >= max_retries:
                    t["status"] = "failed"
                else:
                    t["status"] = "pending"
                    t["progress"] = 0
                    t["speed"] = None
            self._mut(task_id, f)
            self._save()

    def reset_for_retry(self, task_id: str):
        """手动重试：failed -> pending，retries 归零。"""
        with self._lock:
            self._mut(task_id, lambda t: t.update(status="pending", retries=0,
                                                   progress=0, speed=None, error=None))
            self._save()

    def mark_paused(self, task_id: str):
        """把任务标记为 paused（worker 中断下载时调用）。"""
        with self._lock:
            self._mut(task_id, lambda t: t.update(status="paused", speed=None))
            self._save()

    def pause_task(self, task_id: str):
        """前端暂停：pending->paused 直接标记。"""
        with self._lock:
            def f(t):
                if t["status"] == "pending":
                    t["status"] = "paused"
            self._mut(task_id, f)
            self._save()

    def resume_task(self, task_id: str):
        with self._lock:
            self._mut(task_id, lambda t: t.update(status="pending") if t["status"] == "paused" else None)
            self._save()

    # ---- 全局暂停 ----
    def set_paused(self, paused: bool):
        with self._lock:
            self._paused = paused
            self._save()

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    # ---- 内部 ----
    def _mut(self, task_id, fn):
        for t in self._tasks:
            if t["id"] == task_id:
                fn(t)
                return
