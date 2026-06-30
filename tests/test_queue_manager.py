import json
import os
import time
from video_downloader.queue_manager import QueueManager


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
