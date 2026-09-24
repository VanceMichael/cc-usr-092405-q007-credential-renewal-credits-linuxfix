"""触发器与持久作业处理器。

提供方撤销、证明作废/恢复、等效换版：
- 尚未生效的申请 → 重新评估（可能升快照版本）；
- 已生效的续期 → 保留原证据，追加风险标注。

审议期限与核查 SLA 到期也由持久作业驱动。
"""

from __future__ import annotations

import json

from sqlalchemy import Connection, select

from . import jobs
from .applications import (
    get_application,
    open_applications_for_group,
    open_applications_for_proof,
    open_applications_for_provider,
    reevaluate_application,
)
from .catalog import row_to_dict
from .models import (
    application_items,
    effective_renewals,
    equivalence_group_versions,
    proofs,
    providers,
    risk_annotations,
    verification_cases,
)
from .timeutil import iso_now


def register_all_handlers() -> None:
    jobs.register_handler("provider_revoked", handle_provider_revoked)
    jobs.register_handler("proof_status_changed", handle_proof_status_changed)
    jobs.register_handler("equivalence_version_activation", handle_equivalence_activation)
    jobs.register_handler("review_deadline", handle_review_deadline)
    jobs.register_handler("verification_sla", handle_verification_sla)


# ---------------------------------------------------------------- 风险标注

def _annotate_effective(conn: Connection, *, kind: str, detail: dict, proof_ids: set[int] | None = None,
                        provider_id: int | None = None, group_code: str | None = None) -> int:
    """给受影响的已生效续期追加风险标注（不改动原证据）。返回标注条数。"""
    renewals = conn.execute(select(effective_renewals)).all()
    count = 0
    for renewal in renewals:
        items = conn.execute(
            select(application_items)
            .where(
                application_items.c.application_id == renewal.application_id,
                application_items.c.snapshot_version == renewal.snapshot_version,
            )
        ).all()
        affected = False
        for item in items:
            proof = conn.execute(select(proofs).where(proofs.c.id == item.proof_id)).first()
            if proof_ids and item.proof_id in proof_ids:
                affected = True
            if provider_id and proof and proof.provider_id == provider_id:
                affected = True
            if group_code and proof:
                from .models import courses

                course = conn.execute(select(courses).where(courses.c.id == proof.course_id)).first()
                if course and course.equivalence_group == group_code:
                    affected = True
        if not affected:
            continue
        # 同一续期同一事件类型只标注一次
        existing = conn.execute(
            select(risk_annotations).where(
                risk_annotations.c.effective_renewal_id == renewal.id,
                risk_annotations.c.kind == kind,
                risk_annotations.c.detail == json.dumps(detail, ensure_ascii=False, sort_keys=True),
            )
        ).first()
        if existing:
            continue
        conn.execute(
            risk_annotations.insert().values(
                effective_renewal_id=renewal.id,
                kind=kind,
                detail=json.dumps(detail, ensure_ascii=False, sort_keys=True),
                created_at=iso_now(),
            )
        )
        count += 1
    return count


# ---------------------------------------------------------------- 作业处理器

def handle_provider_revoked(conn: Connection, payload: dict) -> None:
    provider_id = payload["provider_id"]
    provider = row_to_dict(conn.execute(select(providers).where(providers.c.id == provider_id)).first())
    for application_id in open_applications_for_provider(conn, provider_id):
        reevaluate_application(
            conn,
            application_id,
            trigger="provider_revoked",
            detail={"provider_code": provider["provider_code"]},
        )
    _annotate_effective(
        conn,
        kind="provider_revoked",
        detail={"provider_code": provider["provider_code"], "revoked_at": provider["revoked_at"]},
        provider_id=provider_id,
    )


def handle_proof_status_changed(conn: Connection, payload: dict) -> None:
    proof_id = payload["proof_id"]
    proof = row_to_dict(conn.execute(select(proofs).where(proofs.c.id == proof_id)).first())
    for application_id in open_applications_for_proof(conn, proof_id):
        reevaluate_application(
            conn,
            application_id,
            trigger="proof_status_changed",
            detail={"proof_id": proof_id, "status": proof["status"]},
        )
    if proof["status"] == "voided":
        _annotate_effective(
            conn,
            kind="proof_voided",
            detail={"proof_id": proof_id, "proof_key": proof["proof_key"]},
            proof_ids={proof_id},
        )


def handle_equivalence_activation(conn: Connection, payload: dict) -> None:
    version = conn.execute(
        select(equivalence_group_versions).where(equivalence_group_versions.c.id == payload["version_id"])
    ).first()
    if not version:
        return
    group_code = version.group_code
    for application_id in open_applications_for_group(conn, group_code):
        reevaluate_application(
            conn,
            application_id,
            trigger="equivalence_version",
            detail={"group_code": group_code, "version": version.version},
        )
    _annotate_effective(
        conn,
        kind="equivalence_version",
        detail={"group_code": group_code, "version": version.version},
        group_code=group_code,
    )


def handle_review_deadline(conn: Connection, payload: dict) -> None:
    application = get_application(conn, payload["application_id"])
    if application["status"] != "in_review":
        return
    from .applications import log_event

    log_event(conn, application["id"], "review_overdue", {"due_at": application["review_due_at"]})


def handle_verification_sla(conn: Connection, payload: dict) -> None:
    case = conn.execute(
        select(verification_cases).where(verification_cases.c.id == payload["case_id"])
    ).first()
    if not case or case.status != "open":
        return
    conn.execute(
        verification_cases.update()
        .where(verification_cases.c.id == case.id)
        .values(escalated=1)
    )
