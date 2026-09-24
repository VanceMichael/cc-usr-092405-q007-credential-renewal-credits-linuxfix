"""申请发起快照固定与双岗审核规则。"""

import pytest

from skill_engine import services as svc
from skill_engine.models import application_versions, review_opinions
from sqlalchemy import select


def test_application_pins_catalog_and_credits_snapshot(world):
    world.person()
    world.provider()
    world.course("C1", credits=10.0)
    world.course("C2", credits=12.0)
    world.requirement(required=20.0, cap=30.0)
    world.evidence_upload("a", uid="E1", course="C1", date="2025-01-01", credits=10.0)
    world.evidence_upload("b", uid="E2", course="C2", date="2025-02-01", credits=12.0)
    world.reviewers()
    detail = world.application()
    assert detail["version"]["version"] == 1
    assert detail["version"]["credits_accepted"] == 22.0
    assert detail["version"]["credits_required_met"] == 1

    # 快照后再上传的新证明不影响已固定版本
    world.course("C3", credits=8.0)
    world.evidence_upload("c", uid="E3", course="C3", date="2025-03-01", credits=8.0)
    same = svc.get_application(world.engine, detail["application"]["id"])
    assert same["version"]["version"] == 1
    assert len(same["items"]) == 2


def test_snapshot_catalog_change_does_not_affect_pending_when_no_event(world):
    world.person()
    world.provider()
    world.course("C1", credits=10.0, scopes=("GF",))
    world.requirement(required=10.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01")
    detail = world.application()
    cat_v1 = detail["items"][0]["course_catalog_id"]
    # 发布课程新版本（范围缩窄）——无重评事件，待审申请仍固定旧版本
    world.course("C1", scopes=("OTHER",), credits=10.0)
    same = svc.get_application(world.engine, detail["application"]["id"])
    assert same["items"][0]["course_catalog_id"] == cat_v1
    assert same["items"][0]["decision"] == "included"


def test_reviewer_cannot_be_applicant(world):
    world.person("p1")
    world.person("co1")
    world.provider()
    world.course("C1")
    world.requirement()
    with pytest.raises(svc.DomainError, match="申请人"):
        with world.engine.begin() as conn:
            svc.create_application(conn, applicant_id="p1", competence="GF",
                                   period_end="2026-09-24",
                                   direct_reviewer_id="p1",
                                   compliance_reviewer_id="co1")


def test_two_reviewers_must_differ(world):
    world.person("p1")
    world.person("d1")
    world.provider()
    world.course("C1")
    world.requirement()
    with pytest.raises(svc.DomainError, match="同一人"):
        with world.engine.begin() as conn:
            svc.create_application(conn, applicant_id="p1", competence="GF",
                                   period_end="2026-09-24",
                                   direct_reviewer_id="d1",
                                   compliance_reviewer_id="d1")


def test_reviewer_conflict_with_provider_rejected(world):
    world.person("p1")
    world.provider("pr1")
    world.course("C1")
    world.requirement()
    world.evidence_upload("a", uid="E1", date="2025-01-01")
    world.person("d1")
    world.person("co1")
    # 直属审核人与提供方存在受限关系
    world.relation("pr1", "d1", relation="兼职讲师")
    with pytest.raises(svc.DomainError, match="受限关系"):
        world.application()


def test_conflict_removed_allows_application(world):
    world.person("p1")
    world.provider("pr1")
    world.course("C1")
    world.requirement()
    world.evidence_upload("a", uid="E1", date="2025-01-01")
    world.reviewers()
    world.relation("pr1", "d1")
    with pytest.raises(svc.DomainError):
        world.application()
    with world.engine.begin() as conn:
        svc.remove_provider_relationship(conn, provider_id="pr1", person_id="d1")
    detail = world.application()
    assert detail["application"]["status"] == "pending"


def test_dual_approval_required_and_roles(world):
    world.person()
    world.provider()
    world.course("C1")
    world.requirement(required=10.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01")
    detail = world.application()
    aid = detail["application"]["id"]

    # 仅直属批准，申请仍 pending
    svc.submit_opinion(world.engine, application_id=aid, reviewer_id="d1",
                       decision="approved", comment="专业范围符合",
                       expected_version=None)
    assert svc.get_application(world.engine, aid)["application"]["status"] == "pending"

    # 合规批准后生效
    out = svc.submit_opinion(world.engine, application_id=aid, reviewer_id="co1",
                             decision="approved", comment=None, expected_version=None)
    assert out["outcome"] == "approved"
    assert svc.get_application(world.engine, aid)["application"]["status"] == "approved"


def test_compliance_rejection_decides_rejected(world):
    world.person()
    world.provider()
    world.course("C1")
    world.requirement(required=10.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01")
    aid = world.application()["application"]["id"]
    out = svc.submit_opinion(world.engine, application_id=aid, reviewer_id="co1",
                             decision="rejected", comment="存在利益冲突",
                             expected_version=None)
    assert out["outcome"] == "rejected"
    assert svc.get_application(world.engine, aid)["application"]["status"] == "rejected"


def test_double_approval_even_when_credits_below_requirement_rejects(world):
    world.person()
    world.provider()
    world.course("C1", credits=5.0)
    world.requirement(required=20.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01", credits=5.0)
    detail = world.application()
    assert detail["version"]["credits_required_met"] == 0
    aid = detail["application"]["id"]
    world.approve_both(aid)
    assert svc.get_application(world.engine, aid)["application"]["decision"] == "rejected"


def test_non_assigned_reviewer_and_applicant_cannot_opine(world):
    world.person()
    world.provider()
    world.course("C1")
    world.requirement(required=10.0)
    world.reviewers()
    world.person("x9", name="无关人员")
    world.evidence_upload("a", uid="E1", date="2025-01-01")
    aid = world.application()["application"]["id"]
    with pytest.raises(svc.DomainError, match="指定审核人"):
        svc.submit_opinion(world.engine, application_id=aid, reviewer_id="x9",
                           decision="approved", comment=None, expected_version=None)
    with pytest.raises(svc.DomainError, match="申请人"):
        svc.submit_opinion(world.engine, application_id=aid, reviewer_id="p1",
                           decision="approved", comment=None, expected_version=None)


def test_opinion_version_conflict_concurrency(world):
    world.person()
    world.provider()
    world.course("C1")
    world.requirement(required=10.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01")
    detail = world.application()
    aid = detail["application"]["id"]

    # 合规人看着 v1 打开；此时一个重评事件把申请推进到 v2
    world.course("C2", credits=4.0)
    world.evidence_upload("b", uid="E2", course="C2", date="2025-02-01")
    # 通过核查确认触发重评（b 本身不是异文）；改用直接重评：以证据作废/新规则事件
    # 这里用等效换版不相关，改为：先让 b 成为同标识异文再确认 -> 产生新版本
    # 简化：直接构造一个会改变裁决的事件——撤销提供方后 b 也无关。
    # 采用证据作废 a 会降分但仍产生 v2
    svc.void_evidence(world.engine, evidence_id=world.evidence["a"], reason="补充测试事件")
    app = svc.get_application(world.engine, aid)
    assert app["application"]["current_version"] == 2

    # 合规人仍提交 expected_version=1 -> 版本冲突
    with pytest.raises(svc.DomainError, match="版本冲突"):
        svc.submit_opinion(world.engine, application_id=aid, reviewer_id="co1",
                           decision="approved", comment=None, expected_version=1)

    # 不带预期版本（看到最新 v2）则可在 v2 落意见
    out = svc.submit_opinion(world.engine, application_id=aid, reviewer_id="co1",
                             decision="approved", comment=None, expected_version=None)
    assert out["version"] == 2
    with world.engine.connect() as conn:
        versions = conn.execute(
            select(application_versions.c.version, application_versions.c.status)
            .where(application_versions.c.application_id == aid)
            .order_by(application_versions.c.version)
        ).all()
        opinions = conn.execute(select(review_opinions.c.version)).all()
    assert versions == [(1, "obsolete"), (2, "active")]
    # 旧版本上没有任何意见落上
    assert all(v != 1 for (v,) in opinions)


def test_same_reviewer_one_opinion_per_version(world):
    world.person()
    world.provider()
    world.course("C1")
    world.requirement(required=10.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01")
    aid = world.application()["application"]["id"]
    svc.submit_opinion(world.engine, application_id=aid, reviewer_id="d1",
                       decision="approved", comment=None, expected_version=None)
    with pytest.raises(svc.DomainError, match="已在当前版本"):
        svc.submit_opinion(world.engine, application_id=aid, reviewer_id="d1",
                           decision="approved", comment=None, expected_version=None)
