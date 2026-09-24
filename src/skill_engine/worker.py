"""后台恢复 worker：持续推进核查看门狗与重评队列。

队列全部持久化在 SQLite 中；进程中断重启后，queued 与租约僵死的任务
会被重新拾起，核查审议期限与重评工作不会因系统中断丢失。
"""

from __future__ import annotations

import logging
import threading

from .services import run_reevaluation_queue, run_review_watchdog

logger = logging.getLogger("skill_engine.worker")


class BackgroundWorker:
    def __init__(self, engine, *, interval_seconds: float = 5.0, worker_id: str = "bg-worker"):
        self.engine = engine
        self.interval_seconds = interval_seconds
        self.worker_id = worker_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> None:
        run_review_watchdog(self.engine)
        run_reevaluation_queue(self.engine, worker_id=self.worker_id)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.tick()
            except Exception:  # 单次失败不得杀死恢复循环
                logger.exception("后台 worker 处理失败，将在下个周期重试")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="skill-engine-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
