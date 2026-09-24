"""HTTP 接口层。

- 管理端：主数据、课程目录、等效版本、周期上限、证明受理、核查队列。
- 审议端：申请发起、审核人指派、版本化意见。
- 申请人：查询自己的申请明细（逐项采用/排除轨迹）与生效续期风险。
- 对外接口：只回答指定日期凭证是否有效。
"""

from __future__ import annotations

import json

from litestar import get, post
from litestar.exceptions import HTTPException
from sqlalchemy import Engine, select, text

from . import applications, catalog, jobs, proofs, triggers
from .catalog import DomainError, row_to_dict
from .models import (
    credentials,
    effective_renewals,
    experts,
    renewal_applications,
    risk_annotations,
)
from .timeutil import iso_now, to_date

# ---------------------------------------------------------------- 装配辅助


def ensure_schema(engine: Engine) -> None:
    """内存库（测试）直接建表；文件库结构由 alembic 管理。"""
    if engine.url.database == ":memory:":
        from .models import metadata

        metadata.create_all(engine)


def build_worker(engine: Engine) -> jobs.JobWorker:
    triggers.register_all_handlers()
    worker = jobs.JobWorker(engine)
    # 启动即回收中断前处于 running 的作业；库结构未迁移时跳过（由 alembic 负责）
    try:
        with engine.begin() as conn:
            jobs.recover_interrupted(conn)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("skill_engine").warning("作业队列表尚未就绪，跳过中断恢复")
    return worker


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except DomainError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _require(data: dict, *fields: str) -> None:
    missing = [field for field in fields if data.get(field) is None]
    if missing:
        raise HTTPException(status_code=422, detail=f"缺少字段: {', '.join(missing)}")


# ---------------------------------------------------------------- 基础


@get("/health", sync_to_thread=True)
def health(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ok", "storage": "sqlite"}


# ---------------------------------------------------------------- 主数据与目录


@post("/admin/experts", sync_to_thread=True)
def create_expert(engine: Engine, data: dict) -> dict:
    _require(data, "expert_code", "name", "home_region")

    def work(conn):
        existing = conn.execute(select(experts).where(experts.c.expert_code == data["expert_code"])).first()
        if existing:
            raise DomainError(f"专家已存在: {data['expert_code']}")
        result = conn.execute(
            experts.insert().values(
                expert_code=data["expert_code"],
                name=data["name"],
                home_region=data["home_region"],
                created_at=iso_now(),
            )
        )
        return dict(conn.execute(select(experts).where(experts.c.id == result.inserted_primary_key[0])).first()._mapping)

    with engine.begin() as conn:
        return _guard(work, conn)


@post("/admin/credentials", sync_to_thread=True)
def create_credential(engine: Engine, data: dict) -> dict:
    _require(data, "credential_code", "expert_code", "required_scopes", "required_credits")

    def work(conn):
        expert = conn.execute(select(experts).where(experts.c.expert_code == data["expert_code"])).first()
        if not expert:
            raise DomainError(f"专家不存在: {data['expert_code']}", status_code=404)
        existing = conn.execute(
            select(credentials).where(credentials.c.credential_code == data["credential_code"])
        ).first()
        if existing:
            raise DomainError(f"凭证已存在: {data['credential_code']}")
        result = conn.execute(
            credentials.insert().values(
                credential_code=data["credential_code"],
                expert_id=expert.id,
                required_scopes=json.dumps(data["required_scopes"], ensure_ascii=False),
                cycle_years=int(data.get("cycle_years", 3)),
                required_credits=float(data["required_credits"]),
                created_at=iso_now(),
            )
        )
        row = conn.execute(select(credentials).where(credentials.c.id == result.inserted_primary_key[0])).first()
        return row_to_dict(row, json_fields=("required_scopes",))

    with engine.begin() as conn:
        return _guard(work, conn)


@post("/admin/providers", sync_to_thread=True)
def create_provider(engine: Engine, data: dict) -> dict:
    _require(data, "provider_code", "name")
    with engine.begin() as conn:
        return _guard(catalog.create_provider, conn, data["provider_code"], data["name"])


@post("/admin/providers/{provider_id:int}/revoke", sync_to_thread=True)
def revoke_provider(engine: Engine, provider_id: int) -> dict:
    with engine.begin() as conn:
        return _guard(catalog.revoke_provider, conn, provider_id)


@post("/admin/reviewers", sync_to_thread=True)
def create_reviewer(engine: Engine, data: dict) -> dict:
    _require(data, "reviewer_code", "name", "role")
    with engine.begin() as conn:
        return _guard(catalog.create_reviewer, conn, data["reviewer_code"], data["name"], data["role"])


@post("/admin/reviewers/{reviewer_id:int}/restrictions", sync_to_thread=True)
def add_restriction(engine: Engine, reviewer_id: int, data: dict) -> dict:
    _require(data, "provider_id")
    with engine.begin() as conn:
        return _guard(catalog.add_restriction, conn, reviewer_id, int(data["provider_id"]), data.get("reason", ""))


@post("/admin/courses", sync_to_thread=True)
def create_course(engine: Engine, data: dict) -> dict:
    _require(data, "course_code", "provider_id", "title", "credits", "scopes", "valid_from", "regions")
    with engine.begin() as conn:
        return _guard(
            catalog.create_course,
            conn,
            course_code=data["course_code"],
            provider_id=int(data["provider_id"]),
            title=data["title"],
            credits=float(data["credits"]),
            scopes=list(data["scopes"]),
            valid_from=data["valid_from"],
            valid_to=data.get("valid_to"),
            regions=list(data["regions"]),
            equivalence_group=data.get("equivalence_group"),
        )


@post("/admin/period-caps", sync_to_thread=True)
def set_period_cap(engine: Engine, data: dict) -> dict:
    _require(data, "cap_key", "cap_type", "max_credits")
    with engine.begin() as conn:
        return _guard(catalog.set_period_cap, conn, data["cap_key"], data["cap_type"], float(data["max_credits"]))


@post("/admin/equivalence-versions", sync_to_thread=True)
def publish_equivalence(engine: Engine, data: dict) -> dict:
    _require(data, "group_code", "regions", "effective_from")
    with engine.begin() as conn:
        return _guard(
            catalog.publish_equivalence_version,
            conn,
            group_code=data["group_code"],
            regions=list(data["regions"]),
            effective_from=data["effective_from"],
        )


# ---------------------------------------------------------------- 证明受理与核查


@post("/proofs", sync_to_thread=True)
def submit_proof(engine: Engine, data: dict) -> dict:
    _require(data, "proof_key", "provider_id", "expert_id", "course_id", "completed_at", "payload")
    with engine.begin() as conn:
        return _guard(
            proofs.submit_proof,
            conn,
            proof_key=data["proof_key"],
            provider_id=int(data["provider_id"]),
            expert_id=int(data["expert_id"]),
            course_id=int(data["course_id"]),
            completed_at=data["completed_at"],
            payload=dict(data["payload"]),
        )


@post("/proofs/{proof_id:int}/void", sync_to_thread=True)
def void_proof(engine: Engine, proof_id: int) -> dict:
    with engine.begin() as conn:
        return _guard(proofs.void_proof, conn, proof_id)


@get("/verification-cases", sync_to_thread=True)
def list_verification_cases(engine: Engine) -> list[dict]:
    with engine.begin() as conn:
        return proofs.list_open_cases(conn)


@post("/verification-cases/{case_id:int}/resolve", sync_to_thread=True)
def resolve_verification_case(engine: Engine, case_id: int, data: dict) -> dict:
    _require(data, "resolution")
    with engine.begin() as conn:
        return _guard(proofs.resolve_verification_case, conn, case_id, resolution=data["resolution"])


# ---------------------------------------------------------------- 申请与审议


@post("/applications", sync_to_thread=True)
def create_application(engine: Engine, data: dict) -> dict:
    _require(data, "credential_id", "application_code")
    with engine.begin() as conn:
        return _guard(
            applications.create_application,
            conn,
            credential_id=int(data["credential_id"]),
            application_code=data["application_code"],
        )


def _application_detail(conn, application_id: int) -> dict:
    application = applications.get_application(conn, application_id)
    application["items"] = applications.snapshot_items(conn, application_id)
    application["events"] = applications.application_events_list(conn, application_id)
    return application


@get("/applications/{application_id:int}", sync_to_thread=True)
def get_application(engine: Engine, application_id: int) -> dict:
    with engine.begin() as conn:
        return _guard(_application_detail, conn, application_id)


@post("/applications/{application_id:int}/assignments", sync_to_thread=True)
def assign_reviewer(engine: Engine, application_id: int, data: dict) -> dict:
    _require(data, "reviewer_id")
    with engine.begin() as conn:
        return _guard(
            applications.assign_reviewer, conn, application_id=application_id, reviewer_id=int(data["reviewer_id"])
        )


@post("/applications/{application_id:int}/opinions", sync_to_thread=True)
def submit_opinion(engine: Engine, application_id: int, data: dict) -> dict:
    _require(data, "reviewer_id", "snapshot_version", "decision")
    with engine.begin() as conn:
        return _guard(
            applications.submit_opinion,
            conn,
            application_id=application_id,
            reviewer_id=int(data["reviewer_id"]),
            snapshot_version=int(data["snapshot_version"]),
            decision=data["decision"],
            comment=data.get("comment", ""),
        )


# ---------------------------------------------------------------- 申请人视图


@get("/me/applications", sync_to_thread=True)
def my_applications(engine: Engine, expert_code: str) -> list[dict]:
    """申请人查询自己的申请明细（逐项学分采用/排除轨迹与审议事件）。"""
    with engine.begin() as conn:
        expert = conn.execute(select(experts).where(experts.c.expert_code == expert_code)).first()
        if not expert:
            raise HTTPException(status_code=404, detail=f"专家不存在: {expert_code}")
        rows = conn.execute(
            select(renewal_applications).where(renewal_applications.c.expert_id == expert.id)
        ).all()
        return [_application_detail(conn, row.id) for row in rows]


@get("/me/renewals", sync_to_thread=True)
def my_renewals(engine: Engine, expert_code: str) -> list[dict]:
    """申请人查询自己的生效续期与后续风险标注。"""
    with engine.begin() as conn:
        expert = conn.execute(select(experts).where(experts.c.expert_code == expert_code)).first()
        if not expert:
            raise HTTPException(status_code=404, detail=f"专家不存在: {expert_code}")
        rows = conn.execute(select(effective_renewals).where(effective_renewals.c.expert_id == expert.id)).all()
        result = []
        for row in rows:
            item = dict(row._mapping)
            annotations = conn.execute(
                select(risk_annotations).where(risk_annotations.c.effective_renewal_id == row.id)
            ).all()
            item["risk_annotations"] = [dict(a._mapping) for a in annotations]
            result.append(item)
        return result


# ---------------------------------------------------------------- 对外接口


@get("/external/credentials/{credential_code:str}/validity", sync_to_thread=True)
def external_validity(engine: Engine, credential_code: str, on: str) -> dict:
    """对外接口：只回答指定日期凭证是否有效，不暴露学分明细。"""
    with engine.begin() as conn:
        credential = conn.execute(
            select(credentials).where(credentials.c.credential_code == credential_code)
        ).first()
        if not credential:
            raise HTTPException(status_code=404, detail=f"凭证不存在: {credential_code}")
        day = to_date(on)
        renewal = conn.execute(
            select(effective_renewals)
            .where(effective_renewals.c.credential_id == credential.id)
            .order_by(effective_renewals.c.valid_until.desc())
        ).first()
        valid = bool(
            renewal
            and to_date(renewal.effective_from) <= day <= to_date(renewal.valid_until)
        )
        return {"credential_code": credential_code, "on": on, "valid": valid}


# ---------------------------------------------------------------- 运维


@post("/ops/jobs/tick", sync_to_thread=True)
def jobs_tick(worker: jobs.JobWorker) -> dict:
    return {"processed": worker.tick()}


ROUTE_HANDLERS = [
    health,
    create_expert,
    create_credential,
    create_provider,
    revoke_provider,
    create_reviewer,
    add_restriction,
    create_course,
    set_period_cap,
    publish_equivalence,
    submit_proof,
    void_proof,
    list_verification_cases,
    resolve_verification_case,
    create_application,
    get_application,
    assign_reviewer,
    submit_opinion,
    my_applications,
    my_renewals,
    external_validity,
    jobs_tick,
]
