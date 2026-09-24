"""完成证明受理。

- 提供方上传的证明带内容摘要（content_hash）。
- 相同证明再次提交：沿用原记录，不产生新行。
- 同标识异文：暂停计入，进入核查队列（持久化，带 SLA）。
- 证明作废：只重评尚未生效的申请，生效续期保留原证据并追加风险标注。
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy import Connection, select

from . import jobs
from .catalog import DomainError, NotFoundError, get_course, get_provider, row_to_dict
from .models import experts, proof_submissions, proofs, verification_cases
from .timeutil import iso_now, now

# 核查队列处理时限
VERIFICATION_SLA = timedelta(days=7)


def digest_payload(payload: dict) -> str:
    """内容摘要：对规范化后的载荷取 SHA-256。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_proof(conn: Connection, proof_id: int) -> dict:
    row = conn.execute(select(proofs).where(proofs.c.id == proof_id)).first()
    if not row:
        raise NotFoundError(f"证明不存在: {proof_id}")
    return row_to_dict(row)


def submit_proof(
    conn: Connection,
    *,
    proof_key: str,
    provider_id: int,
    expert_id: int,
    course_id: int,
    completed_at: str,
    payload: dict,
) -> dict:
    """受理一份完成证明，返回受理结果与是否沿用原记录。"""
    provider = get_provider(conn, provider_id)
    if provider["status"] != "active":
        raise DomainError(f"提供方资格已撤销，不能上传证明: {provider['provider_code']}")
    get_course(conn, course_id)
    expert = conn.execute(select(experts).where(experts.c.id == expert_id)).first()
    if not expert:
        raise NotFoundError(f"专家不存在: {expert_id}")

    content_hash = digest_payload(payload)
    existing = conn.execute(select(proofs).where(proofs.c.proof_key == proof_key)).first()

    if existing is None:
        result = conn.execute(
            proofs.insert().values(
                proof_key=proof_key,
                provider_id=provider_id,
                expert_id=expert_id,
                course_id=course_id,
                completed_at=completed_at,
                content_hash=content_hash,
                status="accepted",
                created_at=iso_now(),
                updated_at=iso_now(),
            )
        )
        proof_id = int(result.inserted_primary_key[0])
        _record_submission(conn, proof_id, provider_id, content_hash, payload, outcome="accepted")
        return {"proof": get_proof(conn, proof_id), "reused": False, "suspended": False}

    proof_id = existing.id
    if existing.content_hash == content_hash:
        # 相同证明再次提交：沿用原记录
        _record_submission(conn, proof_id, provider_id, content_hash, payload, outcome="reused")
        return {"proof": get_proof(conn, proof_id), "reused": True, "suspended": existing.status == "suspended"}

    # 同标识异文：暂停计入并进入核查
    _record_submission(conn, proof_id, provider_id, content_hash, payload, outcome="conflict")
    if existing.status != "suspended":
        conn.execute(proofs.update().where(proofs.c.id == proof_id).values(status="suspended", updated_at=iso_now()))
        _open_verification_case(conn, proof_id, reason="conflicting_content")
    return {"proof": get_proof(conn, proof_id), "reused": False, "suspended": True}


def _record_submission(
    conn: Connection, proof_id: int, provider_id: int, content_hash: str, payload: dict, *, outcome: str
) -> int:
    result = conn.execute(
        proof_submissions.insert().values(
            proof_id=proof_id,
            provider_id=provider_id,
            content_hash=content_hash,
            payload=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            outcome=outcome,
            created_at=iso_now(),
        )
    )
    return int(result.inserted_primary_key[0])


def _open_verification_case(conn: Connection, proof_id: int, *, reason: str) -> int:
    due = (now() + VERIFICATION_SLA).isoformat(timespec="seconds")
    result = conn.execute(
        verification_cases.insert().values(
            proof_id=proof_id, reason=reason, status="open", due_at=due, created_at=iso_now()
        )
    )
    case_id = int(result.inserted_primary_key[0])
    # SLA 到期提醒入队：系统中断后仍会触发
    jobs.enqueue(conn, "verification_sla", {"case_id": case_id}, run_at=due)
    return case_id


def resolve_verification_case(conn: Connection, case_id: int, *, resolution: str) -> dict:
    """核查结论：confirm_original（维持原记录）或 void_proof（作废证明）。"""
    row = conn.execute(select(verification_cases).where(verification_cases.c.id == case_id)).first()
    if not row:
        raise NotFoundError(f"核查案件不存在: {case_id}")
    if row.status != "open":
        raise DomainError(f"核查案件已结案: {case_id}")
    if resolution not in ("confirm_original", "void_proof"):
        raise DomainError("核查结论必须是 confirm_original 或 void_proof", status_code=422)

    conn.execute(
        verification_cases.update()
        .where(verification_cases.c.id == case_id)
        .values(status="resolved", resolution=resolution, resolved_at=iso_now())
    )
    if resolution == "confirm_original":
        conn.execute(
            proofs.update().where(proofs.c.id == row.proof_id).values(status="accepted", updated_at=iso_now())
        )
        jobs.enqueue(conn, "proof_status_changed", {"proof_id": row.proof_id})
    else:
        void_proof(conn, row.proof_id, reason="verification_voided")
    return row_to_dict(
        conn.execute(select(verification_cases).where(verification_cases.c.id == case_id)).first()
    )


def void_proof(conn: Connection, proof_id: int, *, reason: str = "manual_void") -> dict:
    """作废证明：在审申请重评，生效续期保留证据并追加风险标注。"""
    proof = get_proof(conn, proof_id)
    if proof["status"] != "voided":
        conn.execute(proofs.update().where(proofs.c.id == proof_id).values(status="voided", updated_at=iso_now()))
    jobs.enqueue(conn, "proof_status_changed", {"proof_id": proof_id, "reason": reason})
    return get_proof(conn, proof_id)


def list_open_cases(conn: Connection) -> list[dict]:
    rows = conn.execute(
        select(verification_cases).where(verification_cases.c.status == "open").order_by(verification_cases.c.id)
    ).all()
    return [row_to_dict(row) for row in rows]
