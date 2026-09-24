"""领域服务：目录管理、证明登记与核查、申请快照、双岗审核、重评、查询。

所有写入都在显式事务内完成；核查队列与重评队列持久化，进程中断后由 worker 恢复。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from . import models as m
from .evaluation import (
    CycleContext,
    EvidenceCandidate,
    content_hash,
    evaluate_cycle,
    rolling_window,
)

REVIEW_SLA_HOURS = 72  # 核查队列审议期限
LEASE_STALE_MINUTES = 5


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


# ── 通用小工具 ────────────────────────────────────────────────

def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _one(conn, table, where):
    return conn.execute(select(table).where(where)).mappings().first()


def _require(conn, table, where, message: str) -> dict:
    row = _one(conn, table, where)
    if row is None:
        raise NotFound(message)
    return dict(row)


class DomainError(Exception):
    """请求与领域规则冲突（409）。"""


class NotFound(Exception):
    """引用对象不存在（404）。"""


# ── 人员与提供方 ──────────────────────────────────────────────

def register_person(conn, *, person_id: str, name: str, region: str) -> dict:
    if _one(conn, m.persons, m.persons.c.id == person_id) is not None:
        raise DomainError(f"人员 {person_id} 已存在")
    conn.execute(m.persons.insert().values(id=person_id, name=name, region=region, created_at=now()))
    return _require(conn, m.persons, m.persons.c.id == person_id, "人员不存在")


def register_provider(conn, *, provider_id: str, name: str) -> dict:
    if _one(conn, m.providers, m.providers.c.id == provider_id) is not None:
        raise DomainError(f"提供方 {provider_id} 已存在")
    conn.execute(
        m.providers.insert().values(
            id=provider_id, name=name, status="active", revoked_at=None,
            revoked_reason=None, created_at=now(),
        )
    )
    return _require(conn, m.providers, m.providers.c.id == provider_id, "提供方不存在")


def add_provider_relationship(conn, *, provider_id: str, person_id: str, relation: str) -> dict:
    _require(conn, m.providers, m.providers.c.id == provider_id, "提供方不存在")
    rid = new_id()
    conn.execute(
        m.provider_relationships.insert().values(
            id=rid, provider_id=provider_id, person_id=person_id,
            relation=relation, active=1, created_at=now(),
        )
    )
    return _require(conn, m.provider_relationships, m.provider_relationships.c.id == rid, "关系不存在")


def remove_provider_relationship(conn, *, provider_id: str, person_id: str) -> None:
    conn.execute(
        m.provider_relationships.update()
        .where(
            and_(
                m.provider_relationships.c.provider_id == provider_id,
                m.provider_relationships.c.person_id == person_id,
                m.provider_relationships.c.active == 1,
            )
        )
        .values(active=0)
    )


def _restricted_provider_ids(conn, person_id: str) -> set[str]:
    rows = conn.execute(
        select(m.provider_relationships.c.provider_id).where(
            and_(
                m.provider_relationships.c.person_id == person_id,
                m.provider_relationships.c.active == 1,
            )
        )
    ).all()
    return {r[0] for r in rows}


def revoke_provider(engine: Engine, *, provider_id: str, reason: str) -> dict:
    """提供方资格撤销：已生效续期只附风险；未生效申请入队重评。"""
    with engine.begin() as conn:
        provider = _require(conn, m.providers, m.providers.c.id == provider_id, "提供方不存在")
        if provider["status"] == "revoked":
            raise DomainError("提供方资格已被撤销")
        ts = now()
        conn.execute(
            m.providers.update()
            .where(m.providers.c.id == provider_id)
            .values(status="revoked", revoked_at=ts, revoked_reason=reason)
        )
        affected = _applications_touching_provider(conn, provider_id)
        counts = _enqueue_reevaluations(conn, affected, "provider_revoked", provider_id,
                                        f"提供方 {provider['name']}（{provider_id}）资格撤销：{reason}")
    run_reevaluation_queue(engine)
    return {"provider_id": provider_id, "status": "revoked", **counts}


# ── 课程目录（不可变版本）与等效规则换版 ──────────────────────

def add_course_version(conn, *, course_code: str, title: str, competence_scopes: list[str],
                       credits: float, valid_from: str, valid_until: str | None,
                       applicable_regions: list[str]) -> dict:
    last = conn.execute(
        select(m.course_catalog.c.version)
        .where(m.course_catalog.c.course_code == course_code)
        .order_by(m.course_catalog.c.version.desc())
    ).first()
    version = (last[0] + 1) if last else 1
    cid = new_id()
    conn.execute(
        m.course_catalog.insert().values(
            id=cid, course_code=course_code, version=version, title=title,
            competence_scopes=_json_dumps(competence_scopes), credits=credits,
            valid_from=valid_from, valid_until=valid_until,
            applicable_regions=_json_dumps(applicable_regions), created_at=now(),
        )
    )
    return _require(conn, m.course_catalog, m.course_catalog.c.id == cid, "课程不存在")


def _active_rules(conn) -> list[dict]:
    rows = conn.execute(
        select(m.equivalence_rules).where(m.equivalence_rules.c.status == "active")
    ).mappings().all()
    return [dict(r) for r in rows]


def publish_equivalence_rule(engine: Engine, *, group_code: str, title: str,
                             member_catalog_ids: list[str], effective_from: str) -> dict:
    """等效规则发布/换版：旧规则作废旧规则，新版本生效；未生效申请重评，已生效附风险。"""
    affected: list[str] = []
    with engine.begin() as conn:
        group = _one(conn, m.equivalence_groups, m.equivalence_groups.c.code == group_code)
        ts = now()
        if group is None:
            group_id = new_id()
            conn.execute(
                m.equivalence_groups.insert().values(
                    id=group_id, code=group_code, title=title,
                    current_rule_id=None, created_at=ts,
                )
            )
        else:
            group_id = group["id"]

        for catalog_id in member_catalog_ids:
            _require(conn, m.course_catalog, m.course_catalog.c.id == catalog_id,
                     f"等效组成员目录版本 {catalog_id} 不存在")

        last = conn.execute(
            select(m.equivalence_rules.c.version)
            .where(m.equivalence_rules.c.group_id == group_id)
            .order_by(m.equivalence_rules.c.version.desc())
        ).first()
        version = (last[0] + 1) if last else 1
        rule_id = new_id()
        conn.execute(
            m.equivalence_rules.insert().values(
                id=rule_id, group_id=group_id, version=version, status="active",
                effective_from=effective_from, superseded_at=None, created_at=ts,
            )
        )
        for catalog_id in member_catalog_ids:
            conn.execute(
                m.equivalence_rule_members.insert().values(id=new_id(), rule_id=rule_id,
                                                            course_catalog_id=catalog_id)
            )
        conn.execute(
            m.equivalence_rules.update()
            .where(
                and_(
                    m.equivalence_rules.c.group_id == group_id,
                    m.equivalence_rules.c.id != rule_id,
                    m.equivalence_rules.c.status == "active",
                )
            )
            .values(status="superseded", superseded_at=ts)
        )
        conn.execute(
            m.equivalence_groups.update()
            .where(m.equivalence_groups.c.id == group_id)
            .values(current_rule_id=rule_id)
        )
        # 仅当确为换版（存在被取代的规则）时触发重评
        superseded_exists = conn.execute(
            select(m.equivalence_rules.c.id).where(
                and_(
                    m.equivalence_rules.c.group_id == group_id,
                    m.equivalence_rules.c.status == "superseded",
                )
            )
        ).first()
        affected = []
        counts = {"affected": 0, "reevaluated": 0, "risk_annotated": 0}
        if superseded_exists is not None:
            # 受影响课程 = 新规则成员 ∪ 该组历史规则成员（移出组的旧成员同样要重评）
            group_rule_ids = [
                r[0]
                for r in conn.execute(
                    select(m.equivalence_rules.c.id).where(
                        m.equivalence_rules.c.group_id == group_id
                    )
                ).all()
            ]
            member_catalog_ids_all = {
                r[0]
                for r in conn.execute(
                    select(m.equivalence_rule_members.c.course_catalog_id).where(
                        m.equivalence_rule_members.c.rule_id.in_(group_rule_ids)
                    )
                ).all()
            }
            member_codes = {
                r[0]
                for r in conn.execute(
                    select(m.course_catalog.c.course_code).where(
                        m.course_catalog.c.id.in_(member_catalog_ids_all)
                    )
                ).all()
            }
            affected = _applications_touching_courses(conn, member_codes)
            counts = _enqueue_reevaluations(conn, affected, "equivalence_reversion", rule_id,
                                            f"等效组 {group_code} 规则换版至 v{version}（生效日 {effective_from}）")
    run_reevaluation_queue(engine)
    return {"group_code": group_code, "version": version, "rule_id": rule_id, **counts}


def set_renewal_requirement(conn, *, competence: str, required_credits: float,
                            period_years: int, per_cycle_cap: float | None) -> dict:
    existing = _one(conn, m.renewal_requirements,
                    m.renewal_requirements.c.competence == competence)
    values = dict(required_credits=required_credits, period_years=period_years,
                  per_cycle_cap=per_cycle_cap, updated_at=now())
    if existing is None:
        conn.execute(
            m.renewal_requirements.insert().values(id=new_id(), competence=competence, **values)
        )
    else:
        conn.execute(
            m.renewal_requirements.update()
            .where(m.renewal_requirements.c.competence == competence).values(**values)
        )
    return _require(conn, m.renewal_requirements,
                    m.renewal_requirements.c.competence == competence, "阈值不存在")


# ── 完成证明：登记、幂等、同标识异文 ──────────────────────────

def upload_evidence(engine: Engine, *, evidence_uid: str, provider_id: str, person_id: str,
                    course_code: str, completion_date: str, credits_claimed: float,
                    content_summary: str) -> dict:
    """提供方上传完成证明。

    - 内容指纹已存在（含跨机构）：相同证明再次提交，沿用原记录，不新建。
    - 同 evidence_uid 已存在但内容不同：暂停计入，新建 under_review 行并入核查队列。
    - 同 evidence_uid 已存在且内容相同：幂等返回原记录。
    """
    with engine.begin() as conn:
        _require(conn, m.providers, m.providers.c.id == provider_id, "提供方不存在")
        _require(conn, m.persons, m.persons.c.id == person_id, "人员不存在")
        digest = content_hash(
            person_id=person_id, course_code=course_code,
            completion_date=completion_date, credits_claimed=credits_claimed,
            content_summary=content_summary,
        )

        same_hash_row = _one(conn, m.completion_evidence,
                             m.completion_evidence.c.content_hash == digest)
        if same_hash_row is not None:
            # 相同证明已登记
            original_id = same_hash_row["duplicate_of_id"] or same_hash_row["id"]
            if same_hash_row["provider_id"] == provider_id:
                # 同一提供方再次提交：纯幂等，沿用原记录
                return {"status": "duplicate_existing", "evidence_id": same_hash_row["id"],
                        "original_record_id": original_id,
                        "note": "相同证明已登记，沿用原记录，未重复计入。"}
            # 多机构重复证明：建立指向原记录的重复行，供周期裁决说明
            eid = new_id()
            conn.execute(
                m.completion_evidence.insert().values(
                    id=eid, evidence_uid=evidence_uid, provider_id=provider_id,
                    person_id=person_id, course_code=course_code,
                    completion_date=completion_date, credits_claimed=credits_claimed,
                    content_summary=content_summary, content_hash=digest,
                    status="recorded", variant_of_id=None, duplicate_of_id=original_id,
                    check_result=None, void_reason=None, first_recorded_at=now(),
                    resolved_at=None,
                )
            )
            return {"status": "duplicate_existing", "evidence_id": eid,
                    "original_record_id": original_id,
                    "note": "相同证明已由其他机构登记，沿用原记录，本条标记为多机构重复提交。"}

        prior_uid = conn.execute(
            select(m.completion_evidence)
            .where(m.completion_evidence.c.evidence_uid == evidence_uid)
            .order_by(m.completion_evidence.c.first_recorded_at)
        ).mappings().first()

        ts = now()
        if prior_uid is not None:
            # 同标识不同内容 → 暂停计入并核查
            eid = new_id()
            conn.execute(
                m.completion_evidence.insert().values(
                    id=eid, evidence_uid=evidence_uid, provider_id=provider_id,
                    person_id=person_id, course_code=course_code,
                    completion_date=completion_date, credits_claimed=credits_claimed,
                    content_summary=content_summary, content_hash=digest,
                    status="under_review", variant_of_id=prior_uid["id"],
                    duplicate_of_id=None, check_result=None, void_reason=None,
                    first_recorded_at=ts, resolved_at=None,
                )
            )
            due = (datetime.now(timezone.utc) + timedelta(hours=REVIEW_SLA_HOURS)).isoformat()
            conn.execute(
                m.review_queue.insert().values(
                    id=new_id(), evidence_id=eid, reason="same_uid_mismatch",
                    status="queued", due_at=due, resolution=None, notes=None,
                    attempts=0, created_at=ts, resolved_at=None,
                )
            )
            return {"status": "under_review", "evidence_id": eid,
                    "variant_of": prior_uid["id"], "due_at": due,
                    "note": "同一证明标识内容不一致，暂停计入并进入核查。"}

        eid = new_id()
        conn.execute(
            m.completion_evidence.insert().values(
                id=eid, evidence_uid=evidence_uid, provider_id=provider_id,
                person_id=person_id, course_code=course_code,
                completion_date=completion_date, credits_claimed=credits_claimed,
                content_summary=content_summary, content_hash=digest,
                status="recorded", variant_of_id=None, duplicate_of_id=None,
                check_result=None, void_reason=None, first_recorded_at=ts, resolved_at=None,
            )
        )
        return {"status": "recorded", "evidence_id": eid}


def resolve_review(engine: Engine, *, queue_item_id: str, resolution: str,
                   notes: str | None = None) -> dict:
    """核查决议：confirmed 变记为正式记录；rejected 保持排除并记录核查结论。"""
    if resolution not in ("confirmed", "rejected"):
        raise DomainError("resolution 仅支持 confirmed | rejected")
    with engine.begin() as conn:
        item = _require(conn, m.review_queue, m.review_queue.c.id == queue_item_id,
                        "核查队列项不存在")
        if item["status"] == "resolved":
            raise DomainError("该核查事项已决议")
        evidence = _require(conn, m.completion_evidence,
                            m.completion_evidence.c.id == item["evidence_id"], "证明不存在")
        ts = now()
        conn.execute(
            m.review_queue.update()
            .where(m.review_queue.c.id == queue_item_id)
            .values(status="resolved", resolution=resolution, notes=notes, resolved_at=ts)
        )
        if resolution == "confirmed":
            conn.execute(
                m.completion_evidence.update()
                .where(m.completion_evidence.c.id == evidence["id"])
                .values(status="recorded", check_result="confirmed", resolved_at=ts)
            )
        else:
            conn.execute(
                m.completion_evidence.update()
                .where(m.completion_evidence.c.id == evidence["id"])
                .values(status="void", check_result="rejected",
                        void_reason="同标识异文核查未通过", resolved_at=ts)
            )
        affected = _applications_touching_evidence(conn, evidence["id"])
        counts = _enqueue_reevaluations(conn, affected, "review_resolved", evidence["id"],
                                        f"核查队列 {queue_item_id} 决议为 {resolution}")
    run_reevaluation_queue(engine)
    return {"queue_item_id": queue_item_id, "resolution": resolution, **counts}


def void_evidence(engine: Engine, *, evidence_id: str, reason: str) -> dict:
    """证明作废：未生效申请重评，已生效续期附风险。"""
    with engine.begin() as conn:
        evidence = _require(conn, m.completion_evidence,
                            m.completion_evidence.c.id == evidence_id, "证明不存在")
        if evidence["status"] == "void":
            raise DomainError("证明已作废")
        conn.execute(
            m.completion_evidence.update()
            .where(m.completion_evidence.c.id == evidence_id)
            .values(status="void", void_reason=reason)
        )
        affected = _applications_touching_evidence(conn, evidence_id)
        counts = _enqueue_reevaluations(conn, affected, "evidence_voided", evidence_id,
                                        f"证明 {evidence_id} 作废：{reason}")
    run_reevaluation_queue(engine)
    return {"evidence_id": evidence_id, "status": "void", **counts}


# ── 周期构建（当前视图 / 快照固定视图） ───────────────────────

def _catalog_rows_by_code(conn) -> dict[str, list[dict]]:
    rows = conn.execute(select(m.course_catalog)).mappings().all()
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["course_code"], []).append(
            {
                "id": r["id"], "version": r["version"], "title": r["title"],
                "competence_scopes": _json_loads(r["competence_scopes"], []),
                "credits": r["credits"], "valid_from": r["valid_from"],
                "valid_until": r["valid_until"],
                "applicable_regions": _json_loads(r["applicable_regions"], []),
            }
        )
    return grouped


def _active_equivalence_groups(conn) -> list[tuple[str, str, set[str]]]:
    groups = []
    for rule in _active_rules(conn):
        members = {
            r[0]
            for r in conn.execute(
                select(m.equivalence_rule_members.c.course_catalog_id).where(
                    m.equivalence_rule_members.c.rule_id == rule["id"]
                )
            ).all()
        }
        group_code = _require(conn, m.equivalence_groups,
                              m.equivalence_groups.c.id == rule["group_id"], "等效组不存在")["code"]
        groups.append((rule["id"], group_code, members))
    return groups


def _pinned_equivalence_groups(conn, rule_ids: list[str]) -> list[tuple[str, str, set[str]]]:
    if not rule_ids:
        return []
    rows = conn.execute(
        select(m.equivalence_rules).where(m.equivalence_rules.c.id.in_(rule_ids))
    ).mappings().all()
    groups = []
    for rule in rows:
        members = {
            r[0]
            for r in conn.execute(
                select(m.equivalence_rule_members.c.course_catalog_id).where(
                    m.equivalence_rule_members.c.rule_id == rule["id"]
                )
            ).all()
        }
        group = _require(conn, m.equivalence_groups,
                         m.equivalence_groups.c.id == rule["group_id"], "等效组不存在")
        groups.append((rule["id"], group["code"], members))
    return groups


def _person_evidence_candidates(conn, person_id: str) -> list[EvidenceCandidate]:
    providers = {
        r["id"]: dict(r)
        for r in conn.execute(select(m.providers)).mappings().all()
    }
    rows = conn.execute(
        select(m.completion_evidence)
        .where(m.completion_evidence.c.person_id == person_id)
        .order_by(m.completion_evidence.c.first_recorded_at, m.completion_evidence.c.id)
    ).mappings().all()
    candidates = []
    for order, r in enumerate(rows):
        provider = providers[r["provider_id"]]
        candidates.append(
            EvidenceCandidate(
                evidence_id=r["id"], evidence_uid=r["evidence_uid"],
                provider_id=r["provider_id"], provider_name=provider["name"],
                provider_status=provider["status"], course_code=r["course_code"],
                completion_date=r["completion_date"], credits=r["credits_claimed"],
                evidence_status=r["status"], recorded_order=order,
                content_hash=r["content_hash"], duplicate_of_id=r["duplicate_of_id"],
            )
        )
    return candidates


def _build_cycle(conn, *, person_id: str, competence: str, region: str, period_end: str,
                 pinned_rule_ids: list[str] | None, pinned_catalog_ids: set[str] | None = None,
                 requirement_override: dict | None = None):
    if requirement_override is not None:
        # 重评沿用申请发起时固定的阈值与周期，不受后续阈值调整影响
        req = requirement_override
    else:
        req = _require(
            conn, m.renewal_requirements,
            m.renewal_requirements.c.competence == competence,
            f"能力 {competence} 未配置续期阈值",
        )
    period_start, period_end = rolling_window(period_end, req["period_years"])
    catalog_rows = _catalog_rows_by_code(conn)
    if pinned_catalog_ids is not None:
        # 重评（非等效规则事件）时沿用申请快照固定的目录版本
        catalog_rows = {
            code: [row for row in rows if row["id"] in pinned_catalog_ids]
            for code, rows in catalog_rows.items()
        }
        catalog_rows = {code: rows for code, rows in catalog_rows.items() if rows}
    ctx = CycleContext(
        competence=competence, region=region, period_start=period_start,
        period_end=period_end, required_credits=req["required_credits"],
        per_cycle_cap=req["per_cycle_cap"],
        catalog_rows=catalog_rows,
        equivalence_groups=(
            _active_equivalence_groups(conn)
            if pinned_rule_ids is None
            else _pinned_equivalence_groups(conn, pinned_rule_ids)
        ),
    )
    return ctx, _person_evidence_candidates(conn, person_id), dict(req)


# ── 申请：发起即固定快照 ──────────────────────────────────────

ROLE_DIRECT = "direct"
ROLE_COMPLIANCE = "compliance"


def _snapshot_item_payload(item, idx: int, version_id: str) -> dict:
    return dict(
        id=new_id(), application_version_id=version_id, evidence_id=item.evidence_id,
        order_index=idx, provider_id=item.provider_id, provider_name=item.provider_name,
        provider_status=item.provider_status, course_catalog_id=item.course_catalog_id,
        course_code=item.course_code, course_version=item.course_version,
        course_title=item.course_title,
        competence_scopes=_json_dumps(item.competence_scopes),
        applicable_regions=_json_dumps(item.applicable_regions),
        credits=item.credits, credits_applied=item.credits_applied,
        completion_date=item.completion_date, evidence_status=item.evidence_status,
        equivalence_rule_id=item.equivalence_rule_id,
        dedup_fingerprint=item.content_hash,
        decision=item.decision, exclusion_category=item.category,
        exclusion_reason=item.reason,
    )


def create_application(conn, *, applicant_id: str, competence: str, period_end: str,
                       direct_reviewer_id: str, compliance_reviewer_id: str) -> dict:
    """发起续期申请：固定所引用的凭证、目录版本与学分快照，并校验双岗资格。"""
    applicant = _require(conn, m.persons, m.persons.c.id == applicant_id, "申请人不存在")
    for role, reviewer_id in (
        (ROLE_DIRECT, direct_reviewer_id),
        (ROLE_COMPLIANCE, compliance_reviewer_id),
    ):
        _require(conn, m.persons, m.persons.c.id == reviewer_id, f"{role} 审核人不存在")
        if reviewer_id == applicant_id:
            raise DomainError("审核人不能是申请人本人")
    if direct_reviewer_id == compliance_reviewer_id:
        raise DomainError("直属审核人与独立合规人不能为同一人")

    # 利益冲突：审核人不得与申请人引用的任一课程提供方存在受限关系
    ctx, candidates, req = _build_cycle(conn, person_id=applicant_id, competence=competence,
                                        region=applicant["region"], period_end=period_end,
                                        pinned_rule_ids=None)
    result = evaluate_cycle(ctx, candidates)
    provider_ids = {c.provider_id for c in candidates}
    for role, reviewer_id in (
        (ROLE_DIRECT, direct_reviewer_id),
        (ROLE_COMPLIANCE, compliance_reviewer_id),
    ):
        blocked = _restricted_provider_ids(conn, reviewer_id) & provider_ids
        if blocked:
            raise DomainError(
                f"{role} 审核人 {reviewer_id} 与课程提供方 {sorted(blocked)} 存在受限关系，应予回避"
            )

    ts = now()
    app_id = new_id()
    conn.execute(
        m.applications.insert().values(
            id=app_id, applicant_id=applicant_id, competence=competence,
            region=applicant["region"], period_start=ctx.period_start, period_end=ctx.period_end,
            required_credits=ctx.required_credits, per_cycle_cap=ctx.per_cycle_cap,
            period_years=req["period_years"],
            current_version=1, status="pending", decision=None,
            decided_at=None, created_at=ts,
        )
    )
    version_id = new_id()
    pinned_rule_ids = sorted({i.equivalence_rule_id for i in result.items if i.equivalence_rule_id})
    conn.execute(
        m.application_versions.insert().values(
            id=version_id, application_id=app_id, version=1, status="active",
            trigger="init", change_summary=None, pinned_rule_ids=_json_dumps(pinned_rule_ids),
            credits_accepted=result.credits_accepted,
            credits_required_met=1 if result.required_met else 0, created_at=ts,
        )
    )
    for idx, item in enumerate(result.items):
        conn.execute(m.application_evidence_snapshots.insert().values(
            **_snapshot_item_payload(item, idx, version_id)))
    for role, reviewer_id in (
        (ROLE_DIRECT, direct_reviewer_id),
        (ROLE_COMPLIANCE, compliance_reviewer_id),
    ):
        conn.execute(
            m.review_assignments.insert().values(
                id=new_id(), application_id=app_id, role=role,
                person_id=reviewer_id, assigned_at=ts,
            )
        )
    return _application_detail(conn, app_id)


def submit_opinion(engine: Engine, *, application_id: str, reviewer_id: str,
                   decision: str, comment: str | None, expected_version: int | None) -> dict:
    """审核意见落在提交人看到的申请版本上；并发以行级更新保证只绑定当前活跃版本。"""
    if decision not in ("approved", "rejected"):
        raise DomainError("decision 仅支持 approved | rejected")
    with engine.begin() as conn:
        app = _require(conn, m.applications, m.applications.c.id == application_id, "申请不存在")
        if app["status"] != "pending":
            raise DomainError("申请已结束，不再接受意见")
        if reviewer_id == app["applicant_id"]:
            raise DomainError("申请人不能审核自己的申请")
        assignment = _one(
            conn, m.review_assignments,
            and_(
                m.review_assignments.c.application_id == application_id,
                m.review_assignments.c.person_id == reviewer_id,
            ),
        )
        if assignment is None:
            raise DomainError("该人员不是本申请的指定审核人")

        version_row = _require(
            conn, m.application_versions,
            and_(
                m.application_versions.c.application_id == application_id,
                m.application_versions.c.version == app["current_version"],
            ),
            "当前申请版本不存在",
        )
        if expected_version is not None and expected_version != version_row["version"]:
            raise DomainError(
                f"版本冲突：您看到的是 v{expected_version}，申请当前为 v{version_row['version']}，"
                "意见只能落在各自看到的申请版本上"
            )

        # 同一审核人在同一版本只能发表一次意见
        dup = _one(
            conn, m.review_opinions,
            and_(
                m.review_opinions.c.application_id == application_id,
                m.review_opinions.c.application_version_id == version_row["id"],
                m.review_opinions.c.reviewer_id == reviewer_id,
            ),
        )
        if dup is not None:
            raise DomainError("您已在当前版本发表意见")

        ts = now()
        conn.execute(
            m.review_opinions.insert().values(
                id=new_id(), application_id=application_id,
                application_version_id=version_row["id"], version=version_row["version"],
                role=assignment["role"], reviewer_id=reviewer_id,
                decision=decision, comment=comment, created_at=ts,
            )
        )

        outcome = None
        if decision == "rejected":
            conn.execute(
                m.applications.update()
                .where(and_(m.applications.c.id == application_id,
                            m.applications.c.current_version == version_row["version"]))
                .values(status="rejected", decision="rejected", decided_at=ts)
            )
            outcome = "rejected"
        else:
            opinions = conn.execute(
                select(m.review_opinions.c.role, m.review_opinions.c.decision).where(
                    and_(
                        m.review_opinions.c.application_id == application_id,
                        m.review_opinions.c.application_version_id == version_row["id"],
                    )
                )
            ).all()
            approved_roles = {role for role, dec in opinions if dec == "approved"}
            if {ROLE_DIRECT, ROLE_COMPLIANCE} <= approved_roles:
                met = version_row["credits_required_met"] == 1
                final_decision = "approved" if met else "rejected"
                conn.execute(
                    m.applications.update()
                    .where(and_(m.applications.c.id == application_id,
                                m.applications.c.current_version == version_row["version"]))
                    .values(status=final_decision, decision=final_decision, decided_at=ts)
                )
                outcome = final_decision
        return {"application_id": application_id, "version": version_row["version"],
                "reviewer_id": reviewer_id, "recorded_decision": decision, "outcome": outcome}


# ── 重评队列 ──────────────────────────────────────────────────

def _applications_touching_provider(conn, provider_id: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            select(m.applications.c.id)
            .join(
                m.application_versions,
                m.application_versions.c.application_id == m.applications.c.id,
            )
            .join(
                m.application_evidence_snapshots,
                m.application_evidence_snapshots.c.application_version_id
                == m.application_versions.c.id,
            )
            .where(m.application_evidence_snapshots.c.provider_id == provider_id)
            .distinct()
        ).all()
    ]


def _applications_touching_evidence(conn, evidence_id: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            select(m.application_versions.c.application_id)
            .join(
                m.application_evidence_snapshots,
                m.application_evidence_snapshots.c.application_version_id
                == m.application_versions.c.id,
            )
            .where(m.application_evidence_snapshots.c.evidence_id == evidence_id)
            .distinct()
        ).all()
    ]


def _applications_touching_courses(conn, course_codes: set[str]) -> list[str]:
    if not course_codes:
        return []
    return [
        r[0]
        for r in conn.execute(
            select(m.application_versions.c.application_id)
            .join(
                m.application_evidence_snapshots,
                m.application_evidence_snapshots.c.application_version_id
                == m.application_versions.c.id,
            )
            .where(m.application_evidence_snapshots.c.course_code.in_(sorted(course_codes)))
            .distinct()
        ).all()
    ]


def _enqueue_reevaluations(conn, application_ids: list[str], event_type: str,
                           ref_id: str, detail: str) -> dict:
    """未生效申请入重评队列；已生效续期仅追加风险标注。返回两类计数。"""
    pending_ids = {
        r[0]
        for r in conn.execute(
            select(m.applications.c.id).where(
                and_(m.applications.c.id.in_(application_ids),
                     m.applications.c.status == "pending")
            )
        ).all()
    }
    # 已生效（approved）：保留原证据，仅追加风险标注
    approved_ids = {
        r[0]
        for r in conn.execute(
            select(m.applications.c.id).where(
                and_(m.applications.c.id.in_(application_ids),
                     m.applications.c.status == "approved")
            )
        ).all()
    }
    for app_id in approved_ids:
        conn.execute(
            m.risk_annotations.insert().values(
                id=new_id(), application_id=app_id, trigger_type=event_type,
                ref_id=ref_id, detail=detail, created_at=now(),
            )
        )
    enqueued = 0
    for app_id in pending_ids:
        # 同一事件对同一申请不重复入队
        exists = conn.execute(
            select(m.reevaluation_jobs.c.id).where(
                and_(
                    m.reevaluation_jobs.c.application_id == app_id,
                    m.reevaluation_jobs.c.event_type == event_type,
                    m.reevaluation_jobs.c.ref_id == ref_id,
                    m.reevaluation_jobs.c.status.in_(["queued", "processing"]),
                )
            )
        ).first()
        if exists is None:
            conn.execute(
                m.reevaluation_jobs.insert().values(
                    id=new_id(), application_id=app_id, event_type=event_type,
                    ref_id=ref_id, detail=detail, status="queued", attempts=0,
                    lease_owner=None, leased_at=None, result=None,
                    created_at=now(), processed_at=None,
                )
            )
            enqueued += 1
    return {"affected": len(application_ids), "reevaluated": enqueued,
            "risk_annotated": len(approved_ids)}


def _reevaluate(conn, job: dict) -> None:
    app = _require(conn, m.applications, m.applications.c.id == job["application_id"],
                   "申请不存在")
    current = _require(
        conn, m.application_versions,
        and_(
            m.application_versions.c.application_id == app["id"],
            m.application_versions.c.version == app["current_version"],
        ),
        "当前版本不存在",
    )
    pinned = _json_loads(current["pinned_rule_ids"], [])
    reversion = job["event_type"] == "equivalence_reversion"
    if reversion:
        pinned_catalog_ids = None
    else:
        # 撤销/作废/核查重评沿用快照固定的目录版本，目录新版本不得改变原裁决
        pinned_catalog_ids = {
            r[0]
            for r in conn.execute(
                select(m.application_evidence_snapshots.c.course_catalog_id).where(
                    and_(
                        m.application_evidence_snapshots.c.application_version_id == current["id"],
                        m.application_evidence_snapshots.c.course_catalog_id.isnot(None),
                    )
                )
            ).all()
        }
    ctx, candidates, _req = _build_cycle(
        conn, person_id=app["applicant_id"], competence=app["competence"],
        region=app["region"], period_end=app["period_end"],
        # 仅等效规则换版事件使用新规则与新目录；其余事件沿用申请固定的快照
        pinned_rule_ids=None if reversion else pinned,
        pinned_catalog_ids=pinned_catalog_ids,
        requirement_override={
            "competence": app["competence"],
            "required_credits": app["required_credits"],
            "per_cycle_cap": app["per_cycle_cap"],
            "period_years": app["period_years"],
        },
    )
    result = evaluate_cycle(ctx, candidates)

    new_items = [_comparable_item(i) for i in result.items]
    old_rows = conn.execute(
        select(m.application_evidence_snapshots)
        .where(m.application_evidence_snapshots.c.application_version_id == current["id"])
        .order_by(m.application_evidence_snapshots.c.order_index)
    ).mappings().all()
    old_items = [[r["evidence_id"], r["decision"], r["exclusion_category"],
                  round(r["credits_applied"] or 0.0, 2)] for r in old_rows]
    changed = (
        new_items != old_items
        or round(result.credits_accepted, 2) != round(current["credits_accepted"], 2)
        or (1 if result.required_met else 0) != current["credits_required_met"]
    )

    ts = now()
    if changed:
        new_version_no = app["current_version"] + 1
        new_version_id = new_id()
        new_pinned = (
            sorted({i.equivalence_rule_id for i in result.items if i.equivalence_rule_id})
            if job["event_type"] == "equivalence_reversion"
            else pinned
        )
        conn.execute(
            m.application_versions.insert().values(
                id=new_version_id, application_id=app["id"], version=new_version_no,
                status="active", trigger="reevaluation", change_summary=job["detail"],
                pinned_rule_ids=_json_dumps(new_pinned),
                credits_accepted=result.credits_accepted,
                credits_required_met=1 if result.required_met else 0, created_at=ts,
            )
        )
        for idx, item in enumerate(result.items):
            conn.execute(
                m.application_evidence_snapshots.insert().values(
                    **_snapshot_item_payload(item, idx, new_version_id))
            )
        conn.execute(
            m.application_versions.update()
            .where(m.application_versions.c.id == current["id"])
            .values(status="obsolete")
        )
        conn.execute(
            m.applications.update()
            .where(and_(m.applications.c.id == app["id"],
                        m.applications.c.current_version == current["version"]))
            .values(current_version=new_version_no)
        )
        to_version = new_version_no
    else:
        to_version = current["version"]

    conn.execute(
        m.reevaluation_logs.insert().values(
            id=new_id(), application_id=app["id"], event_type=job["event_type"],
            ref_id=job["ref_id"], from_version=current["version"], to_version=to_version,
            changed=1 if changed else 0,
            detail=(job["detail"] or "") + ("；产生新版本" if changed else "；裁决无实质变化，保留当前版本"),
            created_at=ts,
        )
    )
    conn.execute(
        m.reevaluation_jobs.update()
        .where(m.reevaluation_jobs.c.id == job["id"])
        .values(status="done", result="changed" if changed else "unchanged",
                processed_at=ts, lease_owner=None, leased_at=None)
    )


def _comparable_item(item) -> list:
    return [item.evidence_id, item.decision, item.category, round(item.credits_applied or 0.0, 2)]


def run_reevaluation_queue(engine: Engine, *, worker_id: str = "worker", limit: int = 100) -> dict:
    """处理待重评申请；崩溃后靠状态与租约恢复，审议不丢。"""
    processed = changed_jobs = 0
    for _ in range(limit):
        with engine.begin() as conn:
            stale_before = (
                datetime.now(timezone.utc) - timedelta(minutes=LEASE_STALE_MINUTES)
            ).isoformat()
            job = conn.execute(
                select(m.reevaluation_jobs)
                .where(
                    # 排队中，或租约僵死（进程中断）
                    (m.reevaluation_jobs.c.status == "queued")
                    | (
                        (m.reevaluation_jobs.c.status == "processing")
                        & (m.reevaluation_jobs.c.leased_at < stale_before)
                    )
                )
                .order_by(m.reevaluation_jobs.c.created_at)
                .limit(1)
            ).mappings().first()
            if job is None:
                break
            ts = now()
            updated = conn.execute(
                m.reevaluation_jobs.update()
                .where(
                    and_(
                        m.reevaluation_jobs.c.id == job["id"],
                        m.reevaluation_jobs.c.attempts == job["attempts"],
                    )
                )
                .values(status="processing", lease_owner=worker_id, leased_at=ts,
                        attempts=job["attempts"] + 1)
            )
            if updated.rowcount == 0:
                continue
            job_id = job["id"]
        try:
            with engine.begin() as conn:
                fresh = _require(conn, m.reevaluation_jobs,
                                 m.reevaluation_jobs.c.id == job_id, "重评任务不存在")
                _reevaluate(conn, dict(fresh))
            processed += 1
            changed_jobs += 1
        except Exception as exc:  # 退回队列，等待恢复重试
            with engine.begin() as conn:
                conn.execute(
                    m.reevaluation_jobs.update()
                    .where(m.reevaluation_jobs.c.id == job_id)
                    .values(status="queued", lease_owner=None, leased_at=None,
                            result=f"retry: {exc}")
                )
            raise
    return {"processed": processed}


def run_review_watchdog(engine: Engine) -> dict:
    """核查队列看门狗：逾期未决议事项升级标注（期限不因中断丢失）。"""
    overdue = []
    with engine.begin() as conn:
        ts = now()
        rows = conn.execute(
            select(m.review_queue).where(
                and_(m.review_queue.c.status == "queued", m.review_queue.c.due_at < ts)
            )
        ).mappings().all()
        for row in rows:
            note = row["notes"] or ""
            if "OVERDUE" not in note:
                overdue.append(row["id"])
                conn.execute(
                    m.review_queue.update()
                    .where(m.review_queue.c.id == row["id"])
                    .values(notes=(note + f" | OVERDUE since {ts}").strip(" |"))
                )
    return {"overdue_flagged": len(overdue)}


# ── 查询：申请明细 / 学分溯源 / 对外有效性 ────────────────────

def _snapshot_rows(conn, version_id: str) -> list[dict]:
    rows = conn.execute(
        select(m.application_evidence_snapshots)
        .where(m.application_evidence_snapshots.c.application_version_id == version_id)
        .order_by(m.application_evidence_snapshots.c.order_index)
    ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["competence_scopes"] = _json_loads(d["competence_scopes"], [])
        d["applicable_regions"] = _json_loads(d["applicable_regions"], [])
        out.append(d)
    return out


def _application_detail(conn, app_id: str) -> dict:
    app = _require(conn, m.applications, m.applications.c.id == app_id, "申请不存在")
    version = _require(
        conn, m.application_versions,
        and_(m.application_versions.c.application_id == app_id,
             m.application_versions.c.version == app["current_version"]),
        "当前版本不存在",
    )
    opinions = conn.execute(
        select(m.review_opinions).where(
            m.review_opinions.c.application_id == app_id
        ).order_by(m.review_opinions.c.created_at)
    ).mappings().all()
    assignments = conn.execute(select(m.review_assignments).where(
        m.review_assignments.c.application_id == app_id)).mappings().all()
    risks = conn.execute(
        select(m.risk_annotations).where(
            m.risk_annotations.c.application_id == app_id
        ).order_by(m.risk_annotations.c.created_at)
    ).mappings().all()
    reeval_logs = conn.execute(
        select(m.reevaluation_logs).where(
            m.reevaluation_logs.c.application_id == app_id
        ).order_by(m.reevaluation_logs.c.created_at)
    ).mappings().all()
    return {
        "application": dict(app),
        "version": dict(version),
        "items": _snapshot_rows(conn, version["id"]),
        "assignments": [dict(a) for a in assignments],
        "opinions": [dict(o) for o in opinions],
        "risk_annotations": [dict(r) for r in risks],
        "reevaluation_logs": [dict(r) for r in reeval_logs],
    }


def get_application(engine: Engine, application_id: str) -> dict:
    with engine.connect() as conn:
        return _application_detail(conn, application_id)


def applicant_detail(engine: Engine, *, applicant_id: str) -> dict:
    """申请人查询自己的全部证明、逐项裁决与申请。"""
    with engine.connect() as conn:
        evidences = conn.execute(
            select(m.completion_evidence)
            .where(m.completion_evidence.c.person_id == applicant_id)
            .order_by(m.completion_evidence.c.completion_date,
                      m.completion_evidence.c.first_recorded_at)
        ).mappings().all()
        apps = conn.execute(
            select(m.applications).where(
                m.applications.c.applicant_id == applicant_id
            ).order_by(m.applications.c.created_at)
        ).mappings().all()
        app_details = []
        for app in apps:
            detail = _application_detail(conn, app["id"])
            app_details.append({
                "application_id": app["id"],
                "competence": app["competence"],
                "status": app["status"],
                "decision": app["decision"],
                "period_start": app["period_start"],
                "period_end": app["period_end"],
                "current_version": app["current_version"],
                "required_credits": app["required_credits"],
                "credits_accepted": detail["version"]["credits_accepted"],
                "required_met": bool(detail["version"]["credits_required_met"]),
                "items": detail["items"],
                "risk_annotations": detail["risk_annotations"],
            })
        return {
            "applicant_id": applicant_id,
            "evidences": [dict(e) for e in evidences],
            "applications": app_details,
        }


def trace_credit(engine: Engine, *, applicant_id: str, evidence_id: str) -> dict:
    """抽查任一学分：它在各申请版本中被采用或排除的来龙去脉。"""
    with engine.connect() as conn:
        evidence = _one(
            conn, m.completion_evidence,
            and_(m.completion_evidence.c.id == evidence_id,
                 m.completion_evidence.c.person_id == applicant_id),
        )
        if evidence is None:
            raise NotFound("证明不存在或不属于该申请人")
        provider = _require(conn, m.providers,
                            m.providers.c.id == evidence["provider_id"], "提供方不存在")
        queue_items = conn.execute(
            select(m.review_queue).where(
                m.review_queue.c.evidence_id == evidence_id
            ).order_by(m.review_queue.c.created_at)
        ).mappings().all()

        trail = []
        versions = conn.execute(
            select(m.application_versions)
            .join(m.applications, m.applications.c.id == m.application_versions.c.application_id)
            .where(m.applications.c.applicant_id == applicant_id)
            .order_by(m.application_versions.c.application_id,
                      m.application_versions.c.version)
        ).mappings().all()
        for ver in versions:
            snap = _one(
                conn, m.application_evidence_snapshots,
                and_(
                    m.application_evidence_snapshots.c.application_version_id == ver["id"],
                    m.application_evidence_snapshots.c.evidence_id == evidence_id,
                ),
            )
            if snap is None:
                continue
            app = _require(conn, m.applications,
                           m.applications.c.id == ver["application_id"], "申请不存在")
            trail.append({
                "application_id": app["id"],
                "competence": app["competence"],
                "version": ver["version"],
                "version_status": ver["status"],
                "trigger": ver["trigger"],
                "application_status": app["status"],
                "decision": snap["decision"],
                "credits_applied": snap["credits_applied"],
                "exclusion_category": snap["exclusion_category"],
                "exclusion_reason": snap["exclusion_reason"],
                "dedup_fingerprint": snap["dedup_fingerprint"],
                "equivalence_rule_id": snap["equivalence_rule_id"],
                "provider_status_at_snapshot": snap["provider_status"],
                "course_catalog_id": snap["course_catalog_id"],
            })
        return {
            "evidence": dict(evidence),
            "provider": {"id": provider["id"], "name": provider["name"],
                         "status": provider["status"]},
            "review_queue": [dict(q) for q in queue_items],
            "application_trail": trail,
        }


def external_validity(engine: Engine, *, applicant_id: str, competence: str,
                      query_date: str) -> dict:
    """对外接口：只回答指定日期凭证是否有效，不暴露审议明细。"""
    with engine.connect() as conn:
        rows = conn.execute(
            select(m.applications).where(
                and_(
                    m.applications.c.applicant_id == applicant_id,
                    m.applications.c.competence == competence,
                    m.applications.c.status == "approved",
                    m.applications.c.decision == "approved",
                )
            )
        ).mappings().all()
        # 生效决定必须不晚于查询日期；周期覆盖查询日期
        valid_row = None
        for r in rows:
            decided_date = (r["decided_at"] or "")[:10]
            if decided_date <= query_date and r["period_end"] >= query_date:
                valid_row = r
                break
        return {
            "valid": valid_row is not None,
            "as_of": query_date,
            "competence": competence,
        }
