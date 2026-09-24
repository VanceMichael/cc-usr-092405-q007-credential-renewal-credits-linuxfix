"""续期申请与审议。

- 发起申请即固定所引用凭证与学分快照（snapshot_version = 1）。
- 直属审核人看专业范围，独立合规人看利益冲突；两人不能是申请人，
  也不能与课程提供方存在受限关系。
- 意见只能落在审核人各自看到的申请版本上；快照变化后旧版本意见失效。
- 双方 approve 后生效，证据冻结；驳回即 rejected。
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import Connection, select

from . import jobs
from .catalog import DomainError, NotFoundError, get_reviewer, restricted_provider_ids, row_to_dict
from .credits import evaluate_credits, load_credential
from .models import (
    application_events,
    application_items,
    effective_renewals,
    proof_submissions,
    renewal_applications,
    review_assignments,
    review_opinions,
)
from .timeutil import iso_now, minus_years, now, to_date, today

# 审议期限：发起后 30 天
REVIEW_SLA = timedelta(days=30)


def get_application(conn: Connection, application_id: int) -> dict:
    row = conn.execute(
        select(renewal_applications).where(renewal_applications.c.id == application_id)
    ).first()
    if not row:
        raise NotFoundError(f"申请不存在: {application_id}")
    return row_to_dict(row)


def log_event(conn: Connection, application_id: int, event_type: str, detail: dict) -> None:
    """申请事件日志：审议过程可追溯。"""
    conn.execute(
        application_events.insert().values(
            application_id=application_id,
            event_type=event_type,
            detail=json.dumps(detail, ensure_ascii=False, sort_keys=True),
            created_at=iso_now(),
        )
    )


def create_application(conn: Connection, *, credential_id: int, application_code: str) -> dict:
    """发起续期申请：计算滚动学分并固化快照，登记审议期限作业。"""
    credential = load_credential(conn, credential_id)
    existing = conn.execute(
        select(renewal_applications).where(renewal_applications.c.application_code == application_code)
    ).first()
    if existing:
        raise DomainError(f"申请编号已存在: {application_code}")
    open_existing = conn.execute(
        select(renewal_applications).where(
            renewal_applications.c.credential_id == credential_id,
            renewal_applications.c.status == "in_review",
        )
    ).first()
    if open_existing:
        raise DomainError(f"凭证 {credential['credential_code']} 已有在审申请 #{open_existing.id}")

    window_end = today()
    evaluation = evaluate_credits(conn, credential_id=credential_id, window_end=window_end)
    due = (now() + REVIEW_SLA).isoformat(timespec="seconds")
    result = conn.execute(
        renewal_applications.insert().values(
            application_code=application_code,
            credential_id=credential_id,
            expert_id=credential["expert_id"],
            status="in_review",
            snapshot_version=1,
            window_end=to_date_iso(window_end),
            window_start=to_date_iso(evaluation["window_start"]),
            review_due_at=due,
            created_at=iso_now(),
            updated_at=iso_now(),
        )
    )
    application_id = int(result.inserted_primary_key[0])
    _persist_snapshot(conn, application_id, 1, evaluation)
    log_event(
        conn,
        application_id,
        "created",
        {
            "window_start": str(evaluation["window_start"]),
            "window_end": str(window_end),
            "total_counted": evaluation["total_counted"],
            "satisfied": evaluation["satisfied"],
        },
    )
    # 审议期限入队：到期未办结会被标记逾期，系统中断不丢
    jobs.enqueue(conn, "review_deadline", {"application_id": application_id}, run_at=due)
    return get_application(conn, application_id)


def to_date_iso(day) -> str:
    return day.isoformat() if hasattr(day, "isoformat") else str(day)


def _persist_snapshot(conn: Connection, application_id: int, version: int, evaluation: dict) -> None:
    """把一次计算结果固化为快照分项（逐项含判定轨迹）。"""
    for item in evaluation["items"]:
        submission = conn.execute(
            select(proof_submissions.c.id)
            .where(proof_submissions.c.proof_id == item["proof_id"])
            .order_by(proof_submissions.c.id)
            .limit(1)
        ).first()
        conn.execute(
            application_items.insert().values(
                application_id=application_id,
                snapshot_version=version,
                proof_id=item["proof_id"],
                submission_id=submission.id,
                course_code=item["course_code"],
                provider_code=item["provider_code"],
                completed_at=item["completed_at"],
                proof_credits=item["proof_credits"],
                credits_counted=item["credits_counted"],
                decision=item["decision"],
                reasons=json.dumps(item["reasons"], ensure_ascii=False),
                trace=json.dumps(item["trace"], ensure_ascii=False),
                created_at=iso_now(),
            )
        )


def snapshot_items(conn: Connection, application_id: int, version: int | None = None) -> list[dict]:
    application = get_application(conn, application_id)
    version = version or application["snapshot_version"]
    rows = conn.execute(
        select(application_items)
        .where(
            application_items.c.application_id == application_id,
            application_items.c.snapshot_version == version,
        )
        .order_by(application_items.c.id)
    ).all()
    return [row_to_dict(row, json_fields=("reasons", "trace")) for row in rows]


def application_events_list(conn: Connection, application_id: int) -> list[dict]:
    rows = conn.execute(
        select(application_events)
        .where(application_events.c.application_id == application_id)
        .order_by(application_events.c.id)
    ).all()
    return [row_to_dict(row) for row in rows]


# ---------------------------------------------------------------- 审核人指派与资格

def assign_reviewer(conn: Connection, *, application_id: int, reviewer_id: int) -> dict:
    application = get_application(conn, application_id)
    if application["status"] != "in_review":
        raise DomainError(f"申请不在审议中: {application_id}")
    reviewer = get_reviewer(conn, reviewer_id)
    _check_reviewer_eligibility(conn, application, reviewer)
    duplicate = conn.execute(
        select(review_assignments).where(
            review_assignments.c.application_id == application_id,
            review_assignments.c.role == reviewer["role"],
        )
    ).first()
    if duplicate:
        raise DomainError(f"申请 #{application_id} 已指派 {reviewer['role']} 审核人")
    result = conn.execute(
        review_assignments.insert().values(
            application_id=application_id,
            reviewer_id=reviewer_id,
            role=reviewer["role"],
            created_at=iso_now(),
        )
    )
    log_event(conn, application_id, "reviewer_assigned", {"reviewer_id": reviewer_id, "role": reviewer["role"]})
    return row_to_dict(
        conn.execute(select(review_assignments).where(review_assignments.c.id == result.inserted_primary_key[0])).first()
    )


def _check_reviewer_eligibility(conn: Connection, application: dict, reviewer: dict) -> None:
    """审核人不能是申请人，也不能与申请涉及的提供方存在受限关系。"""
    from .models import experts, providers

    applicant = conn.execute(
        select(experts).where(experts.c.id == application["expert_id"])
    ).first()
    if applicant and applicant.expert_code == reviewer["reviewer_code"]:
        raise DomainError("审核人不能是申请人本人")
    provider_codes = {
        row.provider_code
        for row in conn.execute(
            select(application_items.c.provider_code)
            .where(
                application_items.c.application_id == application["id"],
                application_items.c.snapshot_version == application["snapshot_version"],
            )
            .distinct()
        ).all()
    }
    restricted = restricted_provider_ids(conn, reviewer["id"])
    if restricted:
        rows = conn.execute(select(providers).where(providers.c.id.in_(restricted))).all()
        conflict = {row.provider_code for row in rows} & provider_codes
        if conflict:
            raise DomainError(f"审核人与课程提供方存在受限关系: {sorted(conflict)}")


# ---------------------------------------------------------------- 意见与生效

def submit_opinion(
    conn: Connection, *, application_id: int, reviewer_id: int, snapshot_version: int, decision: str, comment: str = ""
) -> dict:
    """提交意见。意见只能落在审核人看到的版本上：版本不一致即拒绝。"""
    application = get_application(conn, application_id)
    if application["status"] != "in_review":
        raise DomainError(f"申请不在审议中: {application_id}")
    if decision not in ("approve", "reject"):
        raise DomainError("意见必须是 approve 或 reject", status_code=422)
    reviewer = get_reviewer(conn, reviewer_id)
    assignment = conn.execute(
        select(review_assignments).where(
            review_assignments.c.application_id == application_id,
            review_assignments.c.reviewer_id == reviewer_id,
        )
    ).first()
    if not assignment:
        raise DomainError("审核人未被指派到该申请")
    _check_reviewer_eligibility(conn, application, reviewer)
    if snapshot_version != application["snapshot_version"]:
        raise DomainError(
            f"意见基于版本 {snapshot_version}，当前申请版本为 {application['snapshot_version']}，请刷新后重审"
        )
    conn.execute(
        review_opinions.insert().values(
            application_id=application_id,
            reviewer_id=reviewer_id,
            role=reviewer["role"],
            snapshot_version=snapshot_version,
            decision=decision,
            comment=comment,
            created_at=iso_now(),
        )
    )
    log_event(
        conn,
        application_id,
        "opinion_submitted",
        {"reviewer_id": reviewer_id, "role": reviewer["role"], "decision": decision, "version": snapshot_version},
    )
    if decision == "reject":
        conn.execute(
            renewal_applications.update()
            .where(renewal_applications.c.id == application_id)
            .values(status="rejected", updated_at=iso_now())
        )
        log_event(conn, application_id, "rejected", {"by": reviewer_id})
        return get_application(conn, application_id)
    _maybe_finalize(conn, application_id)
    return get_application(conn, application_id)


def _maybe_finalize(conn: Connection, application_id: int) -> None:
    """当前版本下两岗都 approve 即生效，冻结证据。"""
    application = get_application(conn, application_id)
    version = application["snapshot_version"]
    rows = conn.execute(
        select(review_opinions).where(
            review_opinions.c.application_id == application_id,
            review_opinions.c.snapshot_version == version,
        )
    ).all()
    approved_roles = {row.role for row in rows if row.decision == "approve"}
    if not {"supervisor", "compliance"} <= approved_roles:
        return
    credential = load_credential(conn, application["credential_id"])
    effective_from = today()
    valid_until = minus_years(effective_from, -int(credential["cycle_years"]))
    conn.execute(
        effective_renewals.insert().values(
            application_id=application_id,
            credential_id=application["credential_id"],
            expert_id=application["expert_id"],
            effective_from=effective_from.isoformat(),
            valid_until=valid_until.isoformat(),
            snapshot_version=version,
            created_at=iso_now(),
        )
    )
    conn.execute(
        renewal_applications.update()
        .where(renewal_applications.c.id == application_id)
        .values(status="approved", updated_at=iso_now())
    )
    log_event(
        conn,
        application_id,
        "approved",
        {"effective_from": effective_from.isoformat(), "valid_until": valid_until.isoformat(), "version": version},
    )


# ---------------------------------------------------------------- 重评（仅未生效申请）

def reevaluate_application(conn: Connection, application_id: int, *, trigger: str, detail: dict) -> dict:
    """对尚未生效的申请按最新目录/状态重算学分；结果变化则升版本。"""
    application = get_application(conn, application_id)
    if application["status"] != "in_review":
        return application  # 已生效/已驳回的不动
    evaluation = evaluate_credits(
        conn, credential_id=application["credential_id"], window_end=to_date(application["window_end"])
    )
    old_items = snapshot_items(conn, application_id)
    changed = _snapshot_differs(old_items, evaluation["items"])
    if changed:
        new_version = application["snapshot_version"] + 1
        conn.execute(
            renewal_applications.update()
            .where(renewal_applications.c.id == application_id)
            .values(snapshot_version=new_version, updated_at=iso_now())
        )
        _persist_snapshot(conn, application_id, new_version, evaluation)
        log_event(
            conn,
            application_id,
            "reevaluated",
            {
                "trigger": trigger,
                "detail": detail,
                "from_version": application["snapshot_version"],
                "to_version": new_version,
                "total_counted": evaluation["total_counted"],
            },
        )
    else:
        log_event(conn, application_id, "reevaluated_unchanged", {"trigger": trigger, "detail": detail})
    return get_application(conn, application_id)


def _snapshot_differs(old_items: list[dict], new_items: list[dict]) -> bool:
    def key(item: dict):
        return (item["proof_id"], item["decision"], round(float(item["credits_counted"]), 6), tuple(item["reasons"]))

    return sorted(key(i) for i in old_items) != sorted(key(i) for i in new_items)


def open_applications_for_provider(conn: Connection, provider_id: int) -> list[int]:
    """当前快照引用了该提供方证明的在审申请。"""
    from .models import proofs, providers

    provider = conn.execute(select(providers).where(providers.c.id == provider_id)).first()
    if not provider:
        return []
    result = []
    rows = conn.execute(
        select(renewal_applications).where(renewal_applications.c.status == "in_review")
    ).all()
    for row in rows:
        hit = conn.execute(
            select(application_items.c.id)
            .join(proofs, application_items.c.proof_id == proofs.c.id)
            .where(
                application_items.c.application_id == row.id,
                application_items.c.snapshot_version == row.snapshot_version,
                proofs.c.provider_id == provider_id,
            )
            .limit(1)
        ).first()
        if hit:
            result.append(row.id)
    return result


def open_applications_for_proof(conn: Connection, proof_id: int) -> list[int]:
    """当前快照引用了该证明的在审申请。"""
    result = []
    rows = conn.execute(
        select(renewal_applications).where(renewal_applications.c.status == "in_review")
    ).all()
    for row in rows:
        hit = conn.execute(
            select(application_items.c.id).where(
                application_items.c.application_id == row.id,
                application_items.c.snapshot_version == row.snapshot_version,
                application_items.c.proof_id == proof_id,
            )
        ).first()
        if hit:
            result.append(row.id)
    return result


def open_applications_for_group(conn: Connection, group_code: str) -> list[int]:
    """当前快照引用了该等效组课程的在审申请。"""
    from .models import courses, proofs

    result = []
    rows = conn.execute(
        select(renewal_applications).where(renewal_applications.c.status == "in_review")
    ).all()
    for row in rows:
        hit = conn.execute(
            select(application_items.c.id)
            .join(proofs, application_items.c.proof_id == proofs.c.id)
            .join(courses, proofs.c.course_id == courses.c.id)
            .where(
                application_items.c.application_id == row.id,
                application_items.c.snapshot_version == row.snapshot_version,
                courses.c.equivalence_group == group_code,
            )
            .limit(1)
        ).first()
        if hit:
            result.append(row.id)
    return result
