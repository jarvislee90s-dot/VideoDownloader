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

    def fake_download(url, on_progress=None, on_title=None):
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

    def fake_download(url, on_progress=None, on_title=None):
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

    def fake_download(url, on_progress=None, on_title=None):
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

    def fake_download(url, on_progress=None, on_title=None):
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


def test_worker_passes_on_title(tmp_path):
    qm = make(tmp_path)
    seen = []

    def fake_download(url, on_progress=None, on_title=None):
        if on_title:
            on_title("早期标题")     # 下载过程中得知标题
            seen.append("called")
        return "早期标题"

    w = Worker(qm, download_fn=fake_download, poll_interval=0.05)
    qm.add_task("https://a")
    w.start()
    time.sleep(0.3)
    w.stop()
    assert seen == ["called"]           # on_title 被透传并调用
    assert qm.list_tasks()[0]["title"] == "早期标题"
