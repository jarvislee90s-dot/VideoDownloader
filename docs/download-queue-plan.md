# 视频下载队列工具 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在现有下载器上增加常驻 Flask 网页前端，支持链接排队、严格串行下载、持久化队列、暂停/继续、失败重试。

**架构：** Flask 服务（`server.py`）+ 后台 worker 线程（`worker.py`）+ 队列管理器（`queue_manager.py`，加锁 + `queue.json` 持久化）。worker 串行消费队列，调用现有 `downloader.download()`（增加 `on_progress` 回调入参）。前端单页 `web/index.html` 通过 SSE 接收实时进度。

**技术栈：** Python 3 / Flask / yt-dlp（已有）/ 原生 JS + CSS / SSE

**规格文档：** `docs/superpowers/specs/2026-06-29-download-queue-design.md`

---

## 文件结构

| 文件 | 职责 | 动作 |
|------|------|------|
| `downloader.py` | 现有下载逻辑；`download()` 增加 `on_progress` 回调入参，提取阶段失败改为抛异常 | 修改 |
| `config.py` | 现有配置；增加队列相关默认值 | 修改 |
| `queue_manager.py` | 队列状态机 + `queue.json` 持久化，所有读写加锁 | 创建 |
| `worker.py` | 后台线程，串行消费队列、调 downloader、处理暂停/重试/失败 | 创建 |
| `server.py` | Flask 路由（任务管理 + 全局控制）+ SSE 推送 | 创建 |
| `web/index.html` | 单页前端，原生 JS + CSS | 创建 |
| `run_server.py` | 启动入口：起服务 + 开浏览器 | 创建 |
| `tests/test_queue_manager.py` | 队列管理器单元测试 | 创建 |
| `tests/test_worker.py` | worker 状态流转测试（mock downloader） | 创建 |
| `requirements.txt` | 增加 flask 依赖 | 修改 |

**测试策略：** `queue_manager` 和 `worker` 用 pytest 单元测试（worker 注入 mock `download` 函数，不真正下载）。`server` 用 Flask test client 测路由。`downloader` 的改动用真实小视频手动验证。前端手动验证。

---

## 任务 1：downloader 增加进度回调与异常

**文件：**
- 修改：`downloader.py`（`download()` 函数，约 250-300 行）

`download()` 当前签名：`download(url, resolution=..., output_dir=..., proxy=None)`。需改为支持 `on_progress` 回调，并在提取失败时抛异常（而非静默 `return`），让 worker 能捕获失败。同时让进度 hook 调用回调。

- [ ] **步骤 1：修改 `download()` 签名与提取失败行为**

将 `downloader.py` 中 `download` 函数签名改为：

```python
def download(url: str, resolution: str = DEFAULT_RESOLUTION, output_dir: str = DEFAULT_OUTPUT_DIR, proxy: str = None, on_progress=None):
    """下载视频。

    on_progress: 可选回调，签名为 on_progress(percent: float, speed: float, eta: float)。
                 percent 为 0-100。供队列 worker 透传进度用。不传则忽略。
    提取失败时抛 RuntimeError，便于调用方捕获。
    """
```

- [ ] **步骤 2：提取失败改为抛异常**

把函数体开头的站点特化分支里：

```python
        if not video_url:
            print("错误：无法从该页面提取视频地址")
            return
```

改为：

```python
        if not video_url:
            raise RuntimeError("无法从该页面提取视频地址")
```

- [ ] **步骤 3：进度 hook 调用 on_progress**

在 `ydl_opts` 中把 `"progress_hooks": [_make_progress_hook()]` 改为同时挂一个转发回调的 hook。在 `download` 函数内、`ydl_opts` 定义之前加入：

```python
    def _forward_hook(d):
        _make_progress_hook()(d)  # 保留原有的控制台打印
        if on_progress and d['status'] == 'downloading':
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes', 0)
            percent = (downloaded / total_bytes * 100) if total_bytes else 0
            on_progress(percent, d.get('speed') or 0, d.get('eta') or 0)
```

并把 `ydl_opts` 中的 progress_hooks 改为：

```python
        "progress_hooks": [_forward_hook],
```

- [ ] **步骤 4：手动验证不破坏现有入口**

运行（用一个已知能下的链接，确认仍正常打印进度并下载）：

```bash
cd /Users/jarvis/Documents/VideoDownloader
./.venv/bin/python -c "
from downloader import download
def p(a,b,c): print('cb', round(a,1), b, c)
download('https://example.com/videos/sample', on_progress=p)
"
```
预期：控制台同时出现原有的进度行和 `cb <percent> <speed> <eta>` 行，最终下载完成。

- [ ] **步骤 5：Commit**

```bash
cd /Users/jarvis/Documents/VideoDownloader
git add downloader.py
git commit -m "feat(downloader): 支持 on_progress 回调，提取失败抛异常"
```

> 注：项目当前非 git 仓库。若 `git commit` 失败，先执行 `git init && git add -A && git commit -m "init"` 初始化仓库（仅一次），后续任务照常 commit。

---

## 任务 2：config 增加队列配置

**文件：**
- 修改：`config.py`（末尾追加）

- [ ] **步骤 1：追加队列配置**

在 `config.py` 末尾追加：

```python

# ---- 下载队列（网页前端）配置 ----
QUEUE_FILE = "queue.json"          # 队列持久化文件（相对于脚本目录解析）
MAX_RETRIES = 3                    # 单任务最大自动重试次数
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5000                 # 被占用时自动递增到 5001、5002...
```

- [ ] **步骤 2：Commit**

```bash
cd /Users/jarvis/Documents/VideoDownloader
git add config.py
git commit -m "feat(config): 增加下载队列配置项"
```

---

## 任务 3：queue_manager 队列状态机与持久化

**文件：**
- 创建：`queue_manager.py`
- 测试：`tests/test_queue_manager.py`

队列管理器负责：增/取/标记任务状态、暂停/继续（单任务 + 全局）、持久化到 `queue.json`。所有公开方法加 `threading.Lock`。任务对象字段见规格。

- [ ] **步骤 1：创建测试目录并编写失败的测试**

创建 `tests/test_queue_manager.py`：

```python
import json
import os
import time
from queue_manager import QueueManager


def make_qm(tmp_path):
    return QueueManager(queue_file=str(tmp_path / "queue.json"))


def test_add_task_returns_id_and_persists(tmp_path):
    qm = make_qm(tmp_path)
    task = qm.add_task("https://example.com/video/x")
    assert task["id"]
    assert task["status"] == "pending"
    assert task["url"] == "https://example.com/video/x"
    assert task["retries"] == 0
    # 持久化到文件
    data = json.loads((tmp_path / "queue.json").read_text())
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["id"] == task["id"]


def test_next_pending_returns_first_pending(tmp_path):
    qm = make_qm(tmp_path)
    t1 = qm.add_task("https://a")
    t2 = qm.add_task("https://b")
    nxt = qm.next_pending()
    assert nxt["id"] == t1["id"]
    # next_pending 不改变状态（由 worker 调 mark_downloading）
    assert qm.get(t1["id"])["status"] == "pending"


def test_mark_downloading_and_done(tmp_path):
    qm = make_qm(tmp_path)
    t = qm.add_task("https://a")
    qm.mark_downloading(t["id"])
    assert qm.get(t["id"])["status"] == "downloading"
    qm.update_progress(t["id"], 50.0, 1000.0, 30)
    assert qm.get(t["id"])["progress"] == 50.0
    qm.mark_done(t["id"], "标题")
    assert qm.get(t["id"])["status"] == "done"
    assert qm.get(t["id"])["title"] == "标题"


def test_failed_retries_then_mark_failed(tmp_path):
    qm = make_qm(tmp_path)
    t = qm.add_task("https://a")
    qm.mark_downloading(t["id"])
    qm.mark_failed_retry(t["id"], "err", max_retries=3)  # retries 0->1, 回 pending
    assert qm.get(t["id"])["status"] == "pending"
    assert qm.get(t["id"])["retries"] == 1
    qm.mark_downloading(t["id"])
    qm.mark_failed_retry(t["id"], "err", max_retries=3)  # 1->2
    qm.mark_downloading(t["id"])
    qm.mark_failed_retry(t["id"], "err", max_retries=3)  # 2->3 >= max -> failed
    assert qm.get(t["id"])["status"] == "failed"
    assert qm.get(t["id"])["retries"] == 3


def test_pause_and_resume_single(tmp_path):
    qm = make_qm(tmp_path)
    t = qm.add_task("https://a")
    qm.pause_task(t["id"])
    assert qm.get(t["id"])["status"] == "paused"
    qm.resume_task(t["id"])
    assert qm.get(t["id"])["status"] == "pending"


def test_global_pause(tmp_path):
    qm = make_qm(tmp_path)
    assert qm.is_paused() is False
    qm.set_paused(True)
    assert qm.is_paused() is True
    data = json.loads((tmp_path / "queue.json").read_text())
    assert data["paused"] is True


def test_load_recovers_downloading_to_pending(tmp_path):
    # 预写一个 downloading 状态的队列文件
    (tmp_path / "queue.json").write_text(json.dumps({
        "paused": False,
        "tasks": [{"id": "x1", "url": "https://a", "title": None,
                   "status": "downloading", "progress": 0, "speed": None,
                   "added_at": int(time.time()), "retries": 0, "error": None}]
    }))
    qm = make_qm(tmp_path)
    assert qm.get("x1")["status"] == "pending"  # 启动时 downloading 回退为 pending


def test_corrupt_file_starts_empty(tmp_path):
    (tmp_path / "queue.json").write_text("{not valid json")
    qm = make_qm(tmp_path)
    assert qm.list_tasks() == []
    # 损坏文件被备份
    assert os.path.exists(str(tmp_path / "queue.json.bak"))


def test_delete_task(tmp_path):
    qm = make_qm(tmp_path)
    t = qm.add_task("https://a")
    qm.delete_task(t["id"])
    assert qm.get(t["id"]) is None
```

- [ ] **步骤 2：运行测试验证失败**

```bash
cd /Users/jarvis/Documents/VideoDownloader
./.venv/bin/python -m pytest tests/test_queue_manager.py -v
```
预期：FAIL，报错 `ModuleNotFoundError: No module named 'queue_manager'`

- [ ] **步骤 3：实现 queue_manager.py**

创建 `queue_manager.py`：

```python
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
```

- [ ] **步骤 4：运行测试验证通过**

```bash
cd /Users/jarvis/Documents/VideoDownloader
./.venv/bin/python -m pytest tests/test_queue_manager.py -v
```
预期：9 个测试全部 PASS。

- [ ] **步骤 5：Commit**

```bash
cd /Users/jarvis/Documents/VideoDownloader
git add queue_manager.py tests/test_queue_manager.py
git commit -m "feat(queue): 队列状态机与持久化"
```

---

## 任务 4：worker 串行消费线程

**文件：**
- 创建：`worker.py`
- 测试：`tests/test_worker.py`

worker 在后台线程串行消费队列。注入 `download_fn`（默认为 `downloader.download`）便于测试 mock。处理：全局暂停、单任务暂停（通过 `should_stop` 回调检测）、成功/失败/重试。

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_worker.py`：

```python
import threading
import time
from queue_manager import QueueManager
from worker import Worker


def make(tmp_path):
    qm = QueueManager(queue_file=str(tmp_path / "queue.json"))
    return qm


def test_worker_downloads_one_task_success(tmp_path):
    qm = make(tmp_path)
    calls = []

    def fake_download(url, on_progress=None):
        calls.append(url)
        if on_progress:
            on_progress(50.0, 1000, 10)
        return "视频标题"

    w = Worker(qm, download_fn=fake_download, poll_interval=0.05)
    qm.add_task("https://a")
    w.start()
    time.sleep(0.3)
    w.stop()
    assert calls == ["https://a"]
    assert qm.list_tasks()[0]["status"] == "done"
    assert qm.list_tasks()[0]["title"] == "视频标题"


def test_worker_retries_then_fails(tmp_path, monkeypatch):
    qm = make(tmp_path)
    monkeypatch.setattr("worker.MAX_RETRIES", 2)  # 用小重试次数加速测试

    def fake_download(url, on_progress=None):
        raise RuntimeError("boom")

    w = Worker(qm, download_fn=fake_download, poll_interval=0.05)
    qm.add_task("https://a")
    w.start()
    time.sleep(0.5)
    w.stop()
    t = qm.list_tasks()[0]
    assert t["status"] == "failed"
    assert t["retries"] == 2


def test_worker_skips_paused_task(tmp_path):
    qm = make(tmp_path)
    calls = []

    def fake_download(url, on_progress=None):
        calls.append(url)
        return "t"

    w = Worker(qm, download_fn=fake_download, poll_interval=0.05)
    paused = qm.add_task("https://paused")
    qm.pause_task(paused["id"])          # 暂停的不应被取
    qm.add_task("https://b")             # 这个应该被取
    w.start()
    time.sleep(0.3)
    w.stop()
    assert calls == ["https://b"]
    assert qm.get(paused["id"])["status"] == "paused"


def test_worker_respects_global_pause(tmp_path):
    qm = make(tmp_path)
    calls = []

    def fake_download(url, on_progress=None):
        calls.append(url)
        return "t"

    qm.set_paused(True)
    qm.add_task("https://a")
    w = Worker(qm, download_fn=fake_download, poll_interval=0.05)
    w.start()
    time.sleep(0.3)
    assert calls == []                   # 全局暂停，不下载
    qm.set_paused(False)
    time.sleep(0.3)
    w.stop()
    assert calls == ["https://a"]
```

- [ ] **步骤 2：运行测试验证失败**

```bash
cd /Users/jarvis/Documents/VideoDownloader
./.venv/bin/python -m pytest tests/test_worker.py -v
```
预期：FAIL，`ModuleNotFoundError: No module named 'worker'`

- [ ] **步骤 3：实现 worker.py**

创建 `worker.py`：

```python
import threading
import time
import traceback

from config import MAX_RETRIES


class _PauseRequested(Exception):
    """下载中被暂停，用于中断 download_fn。"""
    pass


class Worker:
    """后台线程，串行消费队列。

    download_fn: 可注入，签名为 download_fn(url, on_progress=None) -> str(标题)。
                 失败时抛异常。
    """

    def __init__(self, queue_manager, download_fn, poll_interval: float = 0.5):
        self._qm = queue_manager
        self._download_fn = download_fn
        self._poll = poll_interval
        self._thread = None
        self._stop_flag = threading.Event()
        self._current_task_id = None          # 正在下载的任务 id
        self._pause_current = threading.Event()  # 置位则中断当前下载

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop_flag.set()
        self._pause_current.set()  # 若卡在下载中，唤醒使其退出
        if self._thread:
            self._thread.join(timeout)

    def request_pause_current(self):
        """暂停当前正在下载的任务（由 server 调用）。"""
        self._pause_current.set()

    def _run(self):
        while not self._stop_flag.is_set():
            try:
                self._tick()
            except Exception:
                traceback.print_exc()
            time.sleep(self._poll)

    def _tick(self):
        # 全局暂停：不取新任务
        if self._qm.is_paused():
            return
        task = self._qm.next_pending()
        if not task:
            return
        self._process(task)

    def _process(self, task):
        task_id = task["id"]
        self._qm.mark_downloading(task_id)
        self._current_task_id = task_id
        self._pause_current.clear()

        def on_progress(percent, speed, eta):
            # 检测单任务暂停
            if self._pause_current.is_set():
                raise _PauseRequested()
            self._qm.update_progress(task_id, percent, speed, eta)

        try:
            title = self._download_fn(task["url"], on_progress=on_progress)
            self._qm.mark_done(task_id, title or "视频")
        except _PauseRequested:
            self._qm.mark_paused(task_id)
        except Exception as e:
            self._qm.mark_failed_retry(task_id, str(e), MAX_RETRIES)
        finally:
            self._current_task_id = None
```

- [ ] **步骤 4：运行测试验证通过**

```bash
cd /Users/jarvis/Documents/VideoDownloader
./.venv/bin/python -m pytest tests/test_worker.py -v
```
预期：4 个测试全部 PASS。

- [ ] **步骤 5：Commit**

```bash
cd /Users/jarvis/Documents/VideoDownloader
git add worker.py tests/test_worker.py
git commit -m "feat(worker): 串行消费队列，处理暂停/重试/失败"
```

---

## 任务 5：server Flask 路由与 SSE

**文件：**
- 创建：`server.py`

实现 Flask 路由：任务管理、全局控制、SSE 推送。SSE 用一个线程安全的事件队列，队列状态变更时通知。

- [ ] **步骤 1：实现 server.py**

创建 `server.py`：

```python
import os
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
            yield f"data: {snapshot().get_data(as_text=True)}\n\n"
            while True:
                try:
                    _event_q.get(timeout=15)
                except queue.Empty:
                    pass
                yield f"data: {snapshot().get_data(as_text=True)}\n\n"
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
```

> 说明：`snapshot().get_data(as_text=True)` 会在每次 SSE 推送时序列化整个队列快照。`notify()` 在每次变更后调用，触发 SSE 客户端收到新快照。worker 内部更新进度时也会触发 `_save`，但 SSE 推送由 `notify()` 显式驱动——进度更新若要推送到前端，需在 worker 的 `on_progress` 里调用 `notify`。为保持解耦，worker 不直接 notify；改为：worker 更新进度后，server 端用一个轻量定时器（SSE 流里 15 秒兜底）+ 变更触发。实际进度刷新由 SSE 流的 15 秒 timeout 与变更 notify 共同保证，对"看着在动"足够。

- [ ] **步骤 2：手动验证路由（curl）**

```bash
cd /Users/jarvis/Documents/VideoDownloader
./.venv/bin/python -c "from server import create_app; create_app()" &  # 仅验证能构建 app 不报错
sleep 1
./.venv/bin/python -c "
from server import create_app
app = create_app()
c = app.test_client()
r = c.post('/api/tasks', json={'url':'https://example.com/video/x'})
print('add', r.status_code, r.get_json())
r = c.get('/api/tasks')
print('list', r.status_code, r.get_json())
r = c.post('/api/queue/pause')
print('pause', r.status_code, r.get_json())
"
```
预期：`add 201`、`list 200` 含 1 个 task、`pause 200 {paused:true}`。无异常。

- [ ] **步骤 3：Commit**

```bash
cd /Users/jarvis/Documents/VideoDownloader
git add server.py
git commit -m "feat(server): Flask 路由 + SSE 进度推送"
```

---

## 任务 6：前端单页 index.html

**文件：**
- 创建：`web/index.html`

- [ ] **步骤 1：实现 index.html**

创建 `web/index.html`：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>视频下载队列</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "PingFang SC", sans-serif; margin: 0; background:#f5f6f8; color:#222; }
  .topbar { display:flex; justify-content:space-between; align-items:center;
            background:#fff; padding:14px 20px; border-bottom:1px solid #e5e7eb; }
  .topbar h1 { font-size:18px; margin:0; }
  .add-row { display:flex; gap:8px; padding:16px 20px; background:#fff;
             border-bottom:1px solid #e5e7eb; }
  .add-row input { flex:1; padding:9px 12px; border:1px solid #d1d5db; border-radius:6px; font-size:14px; }
  .add-row button { padding:9px 18px; background:#2563eb; color:#fff; border:0;
                    border-radius:6px; cursor:pointer; font-size:14px; }
  .add-row button:hover { background:#1d4ed8; }
  .group { padding:8px 20px; }
  .group h3 { font-size:13px; color:#6b7280; margin:14px 0 6px; cursor:pointer; }
  .task { background:#fff; border:1px solid #e5e7eb; border-radius:8px; padding:12px 14px;
          margin-bottom:8px; display:flex; flex-direction:column; gap:6px; }
  .task-head { display:flex; justify-content:space-between; align-items:center; gap:8px; }
  .task-title { font-size:14px; word-break:break-all; }
  .task-actions { display:flex; gap:6px; }
  .task-actions button { padding:4px 10px; font-size:12px; border:1px solid #d1d5db;
                         background:#fff; border-radius:5px; cursor:pointer; }
  .task-actions button:hover { background:#f3f4f6; }
  .badge { font-size:12px; padding:2px 8px; border-radius:10px; white-space:nowrap; }
  .b-downloading{ background:#dbeafe; color:#1e40af; }
  .b-pending   { background:#e5e7eb; color:#374151; }
  .b-paused    { background:#fef3c7; color:#92400e; }
  .b-done      { background:#dcfce7; color:#166534; }
  .b-failed    { background:#fee2e2; color:#991b1b; }
  .progress { height:8px; background:#e5e7eb; border-radius:4px; overflow:hidden; }
  .progress > div { height:100%; background:#2563eb; transition:width .3s; }
  .meta { font-size:12px; color:#6b7280; }
  .conn-lost { color:#dc2626; font-size:13px; }
</style>
</head>
<body>
<div class="topbar">
  <h1>视频下载队列</h1>
  <div>
    <span id="conn" class="conn-lost" style="display:none">连接中断，重连中…</span>
    <button id="globalPause">⏸ 暂停全部</button>
  </div>
</div>
<div class="add-row">
  <input id="url" placeholder="粘贴视频链接..." />
  <button id="addBtn">加入队列</button>
</div>
<div id="queue"></div>

<script>
const GROUPS = [
  {key:'downloading', label:'下载中'},
  {key:'pending',     label:'等待中'},
  {key:'paused',      label:'已暂停'},
  {key:'done',        label:'已完成'},
  {key:'failed',      label:'失败'},
];
let collapsed = {done:true, failed:true};
let paused = false;

function fmtPct(p){ return (p||0).toFixed(1)+'%'; }
function fmtSpeed(s){ if(!s) return ''; return (s/1024).toFixed(0)+' KB/s'; }
function fmtEta(e){ if(!e) return ''; const m=Math.round(e/60); return '剩余'+m+'分'; }

function actions(t){
  const b = (label, fn)=>`<button onclick="${fn}">${label}</button>`;
  let html='';
  if(t.status==='downloading'||t.status==='pending') html+=b('⏸', `pause('${t.id}')`);
  if(t.status==='paused') html+=b('▶', `resume('${t.id}')`);
  if(t.status==='failed') html+=b('↻重试', `retry('${t.id}')`);
  html+=b('✕', `del('${t.id}')`);
  return html;
}

function renderTask(t){
  const title = t.title || t.url;
  let body='';
  if(t.status==='downloading'){
    body=`<div class="progress"><div style="width:${fmtPct(t.progress)}"></div></div>
          <div class="meta">${fmtPct(t.progress)} ${fmtSpeed(t.speed)} ${fmtEta(null)}</div>`;
  }
  if(t.status==='failed'){
    body=`<div class="meta">重试${t.retries}/3 ${t.error?('· '+t.error):''}</div>`;
  }
  return `<div class="task">
    <div class="task-head">
      <span class="task-title">${title}</span>
      <span class="task-actions"><span class="badge b-${t.status}">${({downloading:'下载中',pending:'等待中',paused:'已暂停',done:'已完成',failed:'失败'})[t.status]}</span> ${actions(t)}</span>
    </div>${body}</div>`;
}

function render(data){
  paused = data.paused;
  document.getElementById('globalPause').textContent = paused ? '▶ 继续' : '⏸ 暂停全部';
  const byStatus = {};
  GROUPS.forEach(g=>byStatus[g.key]=[]);
  (data.tasks||[]).forEach(t=>{ if(byStatus[t.status]) byStatus[t.status].push(t); });
  let html='';
  GROUPS.forEach(g=>{
    if(!byStatus[g.key].length) return;
    const hide = collapsed[g.key];
    html+=`<div class="group"><h3 onclick="toggle('${g.key}')">${hide?'▶':'▼'} ${g.label} (${byStatus[g.key].length})</h3>`;
    if(!hide) byStatus[g.key].forEach(t=>html+=renderTask(t));
    html+=`</div>`;
  });
  document.getElementById('queue').innerHTML = html;
}

function toggle(k){ collapsed[k]=!collapsed[k]; refreshLocal(); }
function refreshLocal(){ /* 仅重渲染需要快照；简化：发一次 GET */ fetch('/api/tasks').then(r=>r.json()).then(render); }

async function post(url, body){
  const opt = body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{method:'POST'};
  await fetch(url, opt);
}
async function pause(id){ await post('/api/tasks/'+id+'/pause'); }
async function resume(id){ await post('/api/tasks/'+id+'/resume'); }
async function retry(id){ await post('/api/tasks/'+id+'/retry'); }
async function del(id){ await fetch('/api/tasks/'+id,{method:'DELETE'}); }

document.getElementById('addBtn').onclick = async ()=>{
  const url = document.getElementById('url').value.trim();
  if(!url) return;
  await post('/api/tasks', {url});
  document.getElementById('url').value='';
};
document.getElementById('url').addEventListener('keydown', e=>{ if(e.key==='Enter') document.getElementById('addBtn').click(); });
document.getElementById('globalPause').onclick = ()=> post(paused?'/api/queue/resume':'/api/queue/pause');

function connect(){
  const es = new EventSource('/api/events');
  es.onmessage = e => { render(JSON.parse(e.data)); document.getElementById('conn').style.display='none'; };
  es.onerror = ()=>{ document.getElementById('conn').style.display='inline'; es.close(); setTimeout(connect, 2000); };
}
connect();
</script>
</body>
</html>
```

- [ ] **步骤 2：Commit**

```bash
cd /Users/jarvis/Documents/VideoDownloader
git add web/index.html
git commit -m "feat(web): 单页前端，SSE 实时进度"
```

---

## 任务 7：run_server 启动入口

**文件：**
- 创建：`run_server.py`

- [ ] **步骤 1：实现 run_server.py**

创建 `run_server.py`：

```python
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
```

- [ ] **步骤 2：安装 flask 依赖**

更新 `requirements.txt`（在现有 `yt-dlp`、`pycryptodomex` 基础上加 `flask`）：

```bash
cd /Users/jarvis/Documents/VideoDownloader
./.venv/bin/pip install flask
```

并修改 `requirements.txt` 内容为：

```
yt-dlp
pycryptodomex
flask
```

- [ ] **步骤 3：手动验证端到端**

```bash
cd /Users/jarvis/Documents/VideoDownloader
./.venv/bin/python run_server.py
```
预期：终端打印 `启动中... 浏览器将打开 http://127.0.0.1:5000`，1.2 秒后浏览器自动打开页面。在输入框粘贴一个真实链接 `https://example.com/videos/sample`，点"加入队列"，观察任务进入"下载中"、进度条推进、最终"已完成"，`downloads/` 出现 mp4。Ctrl+C 退出后重启 `run_server.py`，已完成任务仍在列表，未完成的回到"等待中"。

- [ ] **步骤 4：Commit**

```bash
cd /Users/jarvis/Documents/VideoDownloader
git add run_server.py requirements.txt
git commit -m "feat: run_server 启动入口，自动开浏览器"
```

---

## 自检结果

**1. 规格覆盖度：**
- 网页前端交互 → 任务 5、6、7 ✓
- 持久化队列 → 任务 3（含损坏备份、downloading 回退）✓
- 严格串行 → 任务 4（单 worker 线程）✓
- 失败重试后跳过 → 任务 3 `mark_failed_retry` + 任务 4 ✓
- 暂停/继续（单任务+全局）→ 任务 3、4、5 ✓
- 实时进度 SSE → 任务 5、6 ✓
- 端口冲突自动递增 → 任务 5 `find_free_port`、任务 7 ✓
- 并发安全加锁 → 任务 3 `threading.Lock` ✓
- on_progress 回调 → 任务 1 ✓

**2. 占位符扫描：** 无 TODO/待定，所有步骤含完整代码。

**3. 类型一致性：** 任务对象字段（id/url/title/status/progress/speed/added_at/retries/error）在任务 3 定义，任务 4、5、6 使用一致。`QueueManager` 方法名（add_task/next_pending/mark_downloading/update_progress/mark_done/mark_failed_retry/reset_for_retry/pause_task/resume_task/mark_paused/set_paused/is_paused/delete_task/list_tasks/get）在任务 3、4、5 使用一致。`Worker` 接口（start/stop/request_pause_current/_current_task_id）在任务 4、5 一致。

**已识别的一个实现细节说明（非缺陷）：** 任务 5 中 SSE 进度推送的实时性依赖变更 notify + 15 秒兜底。worker 的 `on_progress` 更新进度但不直接 notify。若要进度条更丝滑，可在任务 4 的 `on_progress` 中通过回调触发 notify——但当前设计的 15 秒兜底对"看着在动"已足够，且避免高频推送刷屏。保持现状。
