"""HTTP 路由：管理端、提供方上传、申请审核、申请人查询、对外有效性。"""

from __future__ import annotations

from typing import Any

from litestar import Controller, delete, get, post
from sqlalchemy import select

from . import models as m
from . import services as svc


def _fields(data: dict[str, Any], names: list[str]) -> dict[str, Any]:
    missing = [name for name in names if data.get(name) is None]
    if missing:
        raise svc.DomainError(f"缺少必填字段：{', '.join(missing)}")
    return {name: data[name] for name in names}


# ── 管理端 ────────────────────────────────────────────────────

class AdminController(Controller):
    path = "/admin"
    tags = ["admin"]

    @post("/persons")
    def create_person(self, *, engine: Any, data: dict[str, Any]) -> dict:
        f = _fields(data, ["person_id", "name", "region"])
        with engine.begin() as conn:
            return svc.register_person(conn, **f)

    @post("/providers")
    def create_provider(self, *, engine: Any, data: dict[str, Any]) -> dict:
        f = _fields(data, ["provider_id", "name"])
        with engine.begin() as conn:
            return svc.register_provider(conn, **f)

    @post("/provider-relationships")
    def add_relationship(self, *, engine: Any, data: dict[str, Any]) -> dict:
        f = _fields(data, ["provider_id", "person_id", "relation"])
        with engine.begin() as conn:
            return svc.add_provider_relationship(conn, **f)

    @delete("/provider-relationships", status_code=200)
    def remove_relationship(self, *, engine: Any, data: dict[str, Any]) -> dict:
        f = _fields(data, ["provider_id", "person_id"])
        with engine.begin() as conn:
            svc.remove_provider_relationship(conn, **f)
        return {"removed": f}

    @post("/providers/{provider_id:str}/revoke")
    def revoke_provider(self, *, engine: Any, provider_id: str, data: dict[str, Any]) -> dict:
        reason = (data or {}).get("reason", "")
        return svc.revoke_provider(engine, provider_id=provider_id, reason=reason)

    @post("/courses")
    def add_course(self, *, engine: Any, data: dict[str, Any]) -> dict:
        f = _fields(data, ["course_code", "title", "competence_scopes", "credits",
                           "valid_from", "applicable_regions"])
        with engine.begin() as conn:
            return svc.add_course_version(
                conn,
                course_code=f["course_code"], title=f["title"],
                competence_scopes=f["competence_scopes"], credits=f["credits"],
                valid_from=f["valid_from"],
                valid_until=data.get("valid_until"),
                applicable_regions=f["applicable_regions"],
            )

    @post("/equivalence-rules")
    def publish_equivalence(self, *, engine: Any, data: dict[str, Any]) -> dict:
        f = _fields(data, ["group_code", "title", "member_catalog_ids", "effective_from"])
        return svc.publish_equivalence_rule(engine, **f)

    @post("/requirements")
    def set_requirement(self, *, engine: Any, data: dict[str, Any]) -> dict:
        f = _fields(data, ["competence", "required_credits", "period_years"])
        with engine.begin() as conn:
            return svc.set_renewal_requirement(
                conn,
                competence=f["competence"], required_credits=f["required_credits"],
                period_years=f["period_years"], per_cycle_cap=data.get("per_cycle_cap"),
            )

    @post("/evidence/{evidence_id:str}/void")
    def void_evidence(self, *, engine: Any, evidence_id: str, data: dict[str, Any]) -> dict:
        reason = (data or {}).get("reason", "")
        return svc.void_evidence(engine, evidence_id=evidence_id, reason=reason)

    @get("/review-queue")
    def list_review_queue(self, *, engine: Any, status: str | None = None) -> dict:
        with engine.connect() as conn:
            stmt = select(m.review_queue).order_by(m.review_queue.c.created_at)
            if status:
                stmt = stmt.where(m.review_queue.c.status == status)
            rows = [dict(r) for r in conn.execute(stmt).mappings().all()]
        return {"items": rows, "count": len(rows)}

    @post("/review-queue/{queue_item_id:str}/resolve")
    def resolve_review(self, *, engine: Any, queue_item_id: str, data: dict[str, Any]) -> dict:
        f = _fields(data, ["resolution"])
        return svc.resolve_review(
            engine, queue_item_id=queue_item_id, resolution=f["resolution"],
            notes=data.get("notes"),
        )


# ── 提供方上传 ────────────────────────────────────────────────

class ProviderController(Controller):
    path = "/provider"
    tags = ["provider"]

    @post("/evidence")
    def upload_evidence(self, *, engine: Any, data: dict[str, Any]) -> dict:
        f = _fields(data, ["evidence_uid", "provider_id", "person_id", "course_code",
                           "completion_date", "credits_claimed", "content_summary"])
        return svc.upload_evidence(engine, **f)


# ── 申请与审核 ────────────────────────────────────────────────

class ApplicationController(Controller):
    path = "/applications"
    tags = ["applications"]

    @post()
    def create_application(self, *, engine: Any, data: dict[str, Any]) -> dict:
        f = _fields(data, ["applicant_id", "competence", "period_end",
                           "direct_reviewer_id", "compliance_reviewer_id"])
        with engine.begin() as conn:
            return svc.create_application(conn, **f)

    @get("/{application_id:str}")
    def get_application(self, *, engine: Any, application_id: str) -> dict:
        return svc.get_application(engine, application_id)

    @post("/{application_id:str}/opinions")
    def submit_opinion(self, *, engine: Any, application_id: str, data: dict[str, Any]) -> dict:
        f = _fields(data, ["reviewer_id", "decision"])
        return svc.submit_opinion(
            engine, application_id=application_id, reviewer_id=f["reviewer_id"],
            decision=f["decision"], comment=data.get("comment"),
            expected_version=data.get("expected_version"),
        )


# ── 申请人查询与抽查溯源 ──────────────────────────────────────

class ApplicantController(Controller):
    path = "/applicants"
    tags = ["applicants"]

    @get("/{applicant_id:str}/detail")
    def applicant_detail(self, *, engine: Any, applicant_id: str) -> dict:
        return svc.applicant_detail(engine, applicant_id=applicant_id)

    @get("/{applicant_id:str}/credits/{evidence_id:str}/trace")
    def trace_credit(self, *, engine: Any, applicant_id: str, evidence_id: str) -> dict:
        return svc.trace_credit(engine, applicant_id=applicant_id, evidence_id=evidence_id)
