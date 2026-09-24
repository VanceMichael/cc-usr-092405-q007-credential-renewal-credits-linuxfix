"""持久化作业队列。

所有审议期限、核查 SLA、重评与版本激活工作都落库为 durable_jobs 行，
由后台 worker 轮询执行；进程中断后重启继续处理，不丢队列。

作业处理器通过 ``register_handler`` 注册，避免循环导入。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import Connection, Engine, select, update

from .models import durable_jobs
from .timeutil import iso_now, now

log = logging.getLogger("skill_engine.jobs")

# 作业锁超时：超过该时长的 running 作业视为随进程中断了，回收重跑
LOCK_TIMEOUT = timedelta(minutes=5)
MAX_ATTEMPTS = 5

Handler = Callable[[Connection, dict], None]
_HANDLERS: dict[str, Handler] = {}


def register_handler(job_type: str, handler: Handler) -> None:
    _HANDLERS[job_type] = handler


def enqueue(
    conn: Connection,
    job_type: str,
    payload: dict,
    *,
    run_at: str | None = None,
    dedupe: bool = True,
) -> int:
    """在同一事务内入队；dedupe 时同类型同载荷的未完成作业直接复用。"""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if dedupe:
        existing = conn.execute(
            select(durable_jobs.c.id).where(
                durable_jobs.c.job_type == job_type,
                durable_jobs.c.payload == body,
                durable_jobs.c.status.in_(("pending", "running")),
            )
        ).first()
        if existing:
            return existing.id
    result = conn.execute(
        durable_jobs.insert().values(
            job_type=job_type,
            payload=body,
            run_at=run_at or iso_now(),
            status="pending",
            attempts=0,
            created_at=iso_now(),
            updated_at=iso_now(),
        )
    )
    return int(result.inserted_primary_key[0])


def recover_interrupted(conn: Connection) -> int:
    """启动时回收中断前处于 running 的作业。"""
    cutoff = (now() - LOCK_TIMEOUT).isoformat(timespec="seconds")
    result = conn.execute(
        update(durable_jobs)
        .where(durable_jobs.c.status == "running", durable_jobs.c.locked_at < cutoff)
        .values(status="pending", updated_at=iso_now())
    )
    return int(result.rowcount or 0)


def run_due(conn: Connection, *, limit: int = 50) -> int:
    """执行到期作业；每个作业在调用方事务内运行，失败留待重试。"""
    due = conn.execute(
        select(durable_jobs)
        .where(durable_jobs.c.status == "pending", durable_jobs.c.run_at <= iso_now())
        .order_by(durable_jobs.c.id)
        .limit(limit)
    ).all()
    processed = 0
    for job in due:
        claimed = conn.execute(
            update(durable_jobs)
            .where(durable_jobs.c.id == job.id, durable_jobs.c.status == "pending")
            .values(status="running", locked_at=iso_now(), attempts=job.attempts + 1, updated_at=iso_now())
        )
        if claimed.rowcount != 1:
            continue
        handler = _HANDLERS.get(job.job_type)
        try:
            if handler is None:
                raise RuntimeError(f"未注册的作业类型: {job.job_type}")
            handler(conn, json.loads(job.payload))
        except Exception as exc:  # noqa: BLE001 - 失败必须落库而不是丢作业
            log.exception("作业 %s 执行失败", job.id)
            attempts = job.attempts + 1
            conn.execute(
                update(durable_jobs)
                .where(durable_jobs.c.id == job.id)
                .values(
                    status="failed" if attempts >= MAX_ATTEMPTS else "pending",
                    last_error=str(exc)[:2000],
                    updated_at=iso_now(),
                )
            )
        else:
            conn.execute(
                update(durable_jobs)
                .where(durable_jobs.c.id == job.id)
                .values(status="done", last_error=None, updated_at=iso_now())
            )
        processed += 1
    return processed


class JobWorker:
    """后台线程 worker：周期性回收中断作业并执行到期作业。"""

    def __init__(self, engine: Engine, interval: float = 1.0) -> None:
        self.engine = engine
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> int:
        """同步执行一轮（测试与启动恢复直接调用）。"""
        with self.engine.begin() as conn:
            recover_interrupted(conn)
            return run_due(conn)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="skill-engine-jobs", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - worker 不允许因单次失败退出
                log.exception("作业轮询失败，下个周期重试")
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
