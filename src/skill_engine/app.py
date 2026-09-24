"""Litestar 应用装配。"""

from litestar import Litestar
from litestar.di import Provide

from .database import create_database_engine
from .http import ROUTE_HANDLERS, build_worker, ensure_schema


def create_app(engine=None, *, start_worker: bool = True) -> Litestar:
    storage = engine or create_database_engine()
    ensure_schema(storage)
    worker = build_worker(storage)

    async def on_startup(app: Litestar) -> None:
        # 启动即恢复中断前积压的作业（核查队列、审议期限、重评）
        try:
            worker.tick()
        except Exception:  # noqa: BLE001 - 库结构未迁移时不阻塞启动
            import logging

            logging.getLogger("skill_engine").warning("作业队列尚未就绪，等待迁移完成后由后台线程处理")
        if start_worker:
            worker.start()

    async def on_shutdown(app: Litestar) -> None:
        worker.stop()

    return Litestar(
        route_handlers=list(ROUTE_HANDLERS),
        dependencies={
            "engine": Provide(lambda: storage, sync_to_thread=False),
            "worker": Provide(lambda: worker, sync_to_thread=False),
        },
        on_startup=[on_startup],
        on_shutdown=[on_shutdown],
    )


app = create_app()
