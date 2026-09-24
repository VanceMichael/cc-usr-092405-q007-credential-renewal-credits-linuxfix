"""SQLite 引擎与结构初始化。"""

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from .models import metadata


def create_database_engine(database_path: str | None = None, *, static_pool: bool = False):
    """创建引擎。

    static_pool 用于测试中的内存库：所有线程共用同一连接，
    使提供方上传（线程池执行）与后台 worker 能看到同一份数据。
    文件库启用 WAL 与 busy_timeout，提升并发与中断恢复能力。
    """
    path = Path(database_path or os.getenv("DATABASE_PATH", "data/skills.sqlite3"))
    in_memory = str(path) == ":memory:"
    if in_memory or static_pool:
        return create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def create_schema(engine) -> None:
    """按当前 metadata 直接建表（测试与快速引导使用；生产走 Alembic 迁移）。"""
    metadata.create_all(engine)
