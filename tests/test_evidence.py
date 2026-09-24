"""证明登记：幂等沿用、多机构重复、同标识异文核查队列。"""

import pytest

from skill_engine import services as svc
from skill_engine.models import completion_evidence, review_queue
from sqlalchemy import select
from sqlalchemy.engine import Engine


def _ev_rows(engine: Engine):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(completion_evidence).order_by(completion_evidence.c.first_recorded_at)
        ).mappings().all()]


def test_identical_resubmission_same_provider_is_idempotent(world):
    world.person()
    world.provider()
    world.course("C1")
    payload = dict(uid="E1", provider="pr1", person="p1", course="C1",
                   date="2025-05-01", credits=10.0, summary="完成碳核算 40 学时")
    first = world.evidence_upload("a", **payload)
    second = world.evidence_upload("b", **payload)
    assert first["status"] == "recorded"
    assert second["status"] == "duplicate_existing"
    assert second["evidence_id"] == first["evidence_id"]
    assert len(_ev_rows(world.engine)) == 1


def test_same_content_from_other_provider_reuses_original_record(world):
    world.person()
    world.provider("pr1")
    world.provider("pr2", name="外区机构")
    world.course("C1")
    a = world.evidence_upload("a", uid="E1", provider="pr1", summary="同一证明")
    b = world.evidence_upload("b", uid="E9", provider="pr2", summary="同一证明")
    assert b["status"] == "duplicate_existing"
    assert b["original_record_id"] == a["evidence_id"]
    assert b["evidence_id"] != a["evidence_id"]
    rows = {r["id"]: r for r in _ev_rows(world.engine)}
    assert rows[b["evidence_id"]]["duplicate_of_id"] == a["evidence_id"]


def test_same_uid_different_content_pauses_and_queues(world):
    world.person()
    world.provider()
    world.course("C1")
    world.requirement()
    world.evidence_upload("a", uid="E1", summary="内容甲")
    mismatch = world.evidence_upload("b", uid="E1", summary="内容乙——学时不同")
    assert mismatch["status"] == "under_review"

    with world.engine.connect() as conn:
        ev = conn.execute(select(completion_evidence).where(
            completion_evidence.c.id == mismatch["evidence_id"]
        )).mappings().one()
        q = conn.execute(select(review_queue).where(
            review_queue.c.evidence_id == mismatch["evidence_id"]
        )).mappings().one()
    assert ev["status"] == "under_review"
    assert ev["variant_of_id"] is not None
    assert q["status"] == "queued"
    assert q["reason"] == "same_uid_mismatch"


def test_review_confirmation_restores_credit(world):
    world.person()
    world.provider()
    world.course("C1")
    world.requirement(required=10.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2020-05-01", summary="内容甲")
    mismatch = world.evidence_upload("b", uid="E1", date="2025-06-01",
                                     course="C1", summary="内容乙")
    # 确认前：under_review 不计入
    detail = world.application()
    items = world.item_map(detail)
    assert items[mismatch["evidence_id"]]["exclusion_category"] == "under_review"

    with world.engine.connect() as conn:
        qid = conn.execute(select(review_queue.c.id)).mappings().one()["id"]
    svc.resolve_review(world.engine, queue_item_id=qid, resolution="confirmed")
    detail2 = svc.get_application(world.engine, detail["application"]["id"])
    assert detail2["application"]["current_version"] == 2
    confirmed = next(i for i in detail2["items"]
                     if i["evidence_id"] == mismatch["evidence_id"])
    assert confirmed["decision"] == "included"


def test_review_rejection_voids(world):
    world.person()
    world.provider()
    world.course("C1")
    world.requirement(required=10.0)
    world.evidence_upload("a", uid="E1", summary="内容甲")
    mismatch = world.evidence_upload("b", uid="E1", summary="内容乙")
    with world.engine.connect() as conn:
        qid = conn.execute(select(review_queue.c.id)).mappings().one()["id"]
    svc.resolve_review(world.engine, queue_item_id=qid, resolution="rejected",
                       notes="查无此记录")
    rows = {r["id"]: r for r in _ev_rows(world.engine)}
    assert rows[mismatch["evidence_id"]]["status"] == "void"
    assert rows[mismatch["evidence_id"]]["check_result"] == "rejected"


def test_review_queue_overdue_watchdog(world):
    world.person()
    world.provider()
    world.course("C1")
    world.evidence_upload("a", uid="E1", summary="内容甲")
    world.evidence_upload("b", uid="E1", summary="内容乙")
    # 手工把期限改到过去，模拟系统中断后重启看门狗
    with world.engine.begin() as conn:
        conn.execute(review_queue.update().values(due_at="2000-01-01T00:00:00+00:00"))
    out = svc.run_review_watchdog(world.engine)
    assert out["overdue_flagged"] == 1
    with world.engine.connect() as conn:
        row = conn.execute(select(review_queue)).mappings().one()
    assert "OVERDUE" in (row["notes"] or "")
    # 再跑一次不重复标注
    assert svc.run_review_watchdog(world.engine)["overdue_flagged"] == 0
