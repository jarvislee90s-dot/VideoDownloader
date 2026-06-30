import threading
import time
import traceback

from config import MAX_RETRIES


class _PauseRequested(Exception):
    """下载中被暂停，用于中断 download_fn。"""
    pass


class Worker:
    """后台线程，串行消费队列。

    download_fn: 可注入，签名为 download_fn(url, on_progress=None, on_title=None) -> str(标题)。
                 失败时抛异常。
    on_change: 可选回调，队列状态变更后调用（无参），供 server 触发 SSE 推送。
    """

    def __init__(self, queue_manager, download_fn, poll_interval: float = 0.5, on_change=None):
        self._qm = queue_manager
        self._download_fn = download_fn
        self._poll = poll_interval
        self._on_change = on_change
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
        last_save = [0.0]

        def on_progress(percent, speed, eta):
            # 检测单任务暂停
            if self._pause_current.is_set():
                raise _PauseRequested()
            # yt-dlp 进度事件频率很高，节流到每秒最多写一次（每次写都会原子落盘）
            now = time.time()
            if now - last_save[0] < 1.0 and percent < 99.0:
                return
            last_save[0] = now
            self._qm.update_progress(task_id, percent, speed, eta)
            if self._on_change:
                self._on_change()

        def on_title(title):
            # 下载过程中得知标题，立即写入并推送，让前端显示视频名而非链接
            self._qm.set_title(task_id, title)
            if self._on_change:
                self._on_change()

        try:
            title = self._download_fn(task["url"], on_progress=on_progress, on_title=on_title)
            self._qm.mark_done(task_id, title or "视频")
            if self._on_change:
                self._on_change()
        except _PauseRequested:
            self._qm.mark_paused(task_id)
            if self._on_change:
                self._on_change()
        except Exception as e:
            self._qm.mark_failed_retry(task_id, str(e), MAX_RETRIES)
            if self._on_change:
                self._on_change()
        finally:
            self._current_task_id = None
