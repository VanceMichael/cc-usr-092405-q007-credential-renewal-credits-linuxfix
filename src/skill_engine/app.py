"""Litestar 应用装配。"""

from __future__ import annotations

import os

# 同步处理器由 Litestar 自动放入线程池执行（SQLite 驱动本身为同步）
os.environ.setdefault("LITESTAR_WARN_IMPLICIT_SYNC_TO_THREAD", "0")

from litestar import Litestar, get
from litestar.response import Response
from sqlalchemy import text

from . import services as svc
from .database import create_database_engine, create_schema
from .routes import AdminController, ApplicantController, ApplicationController, ProviderController
from .worker import BackgroundWorker


def _domain_error_handler(_request, exc: svc.DomainError) -> Response:
    return Response(status_code=409, content={"error": "conflict", "detail": str(exc)})


def _not_found_handler(_request, exc: svc.NotFound) -> Response:
    return Response(status_code=404, content={"error": "not_found", "detail": str(exc)})


@get("/health", tags=["system"])
def health(engine: object) -> dict[str, str]:
    with engine.connect() as connection:  # type: ignore[attr-defined]
        connection.execute(text("SELECT 1"))
    return {"status": "ok", "storage": "sqlite"}


@get(
    "/external/validity",
    tags=["external"],
    description="对外接口：仅回答指定日期凭证是否有效，不返回任何审议明细。",
)
def external_validity(engine: object, applicant_id: str, competence: str, date: str) -> dict:
    return svc.external_validity(
        engine,  # type: ignore[arg-type]
        applicant_id=applicant_id,
        competence=competence,
        query_date=date,
    )


def create_app(engine=None, *, start_worker: bool | None = None) -> Litestar:
    storage = engine or create_database_engine()
    if engine is None:
        # 默认文件库由容器入口先执行 Alembic；此处兜底确保结构存在
        create_schema(storage)
    else:
        create_schema(storage)

    if start_worker is None:
        start_worker = os.getenv("ENABLE_BACKGROUND_WORKER", "1") != "0"

    def _engine_provider() -> object:
        return storage

    async def _on_startup(app: Litestar) -> None:
        if start_worker:
            worker = BackgroundWorker(storage)
            app.state.worker = worker
            worker.start()

    async def _on_shutdown(app: Litestar) -> None:
        worker = getattr(app.state, "worker", None)
        if worker is not None:
            worker.stop()

    return Litestar(
        route_handlers=[
            health,
            external_validity,
            AdminController,
            ProviderController,
            ApplicationController,
            ApplicantController,
        ],
        dependencies={"engine": _engine_provider},
        exception_handlers={
            svc.DomainError: _domain_error_handler,
            svc.NotFound: _not_found_handler,
        },
        on_startup=[_on_startup],
        on_shutdown=[_on_shutdown],
    )


app = create_app()
