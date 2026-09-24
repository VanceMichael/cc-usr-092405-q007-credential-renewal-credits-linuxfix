"""三类重评事件：提供方撤销、证明作废、等效规则换版；已生效仅附风险。"""

from skill_engine import services as svc
from skill_engine.models import (
    application_versions,
    reevaluation_jobs,
    reevaluation_logs,
    risk_annotations,
)
from sqlalchemy import select


def _versions(engine, app_id):
    with engine.connect() as conn:
        return conn.execute(
            select(application_versions.c.version, application_versions.c.status,
                   application_versions.c.credits_accepted)
            .where(application_versions.c.application_id == app_id)
            .order_by(application_versions.c.version)
        ).all()


def test_provider_revocation_reevaluates_pending(world):
    world.person()
    world.provider("pr1")
    world.course("C1", credits=20.0)
    world.requirement(required=20.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01", credits=20.0)
    aid = world.application()["application"]["id"]

    out = svc.revoke_provider(world.engine, provider_id="pr1", reason="资质核查未过")
    assert out["reevaluated"] == 1
    detail = svc.get_application(world.engine, aid)
    assert detail["application"]["current_version"] == 2
    item = detail["items"][0]
    assert item["exclusion_category"] == "provider_revoked"
    assert detail["version"]["credits_accepted"] == 0
    assert detail["version"]["credits_required_met"] == 0


def test_provider_revocation_after_approval_keeps_evidence_adds_risk(world):
    world.person()
    world.provider("pr1")
    world.course("C1", credits=20.0)
    world.requirement(required=20.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01", credits=20.0)
    aid = world.application()["application"]["id"]
    world.approve_both(aid)

    out = svc.revoke_provider(world.engine, provider_id="pr1", reason="事后问题")
    assert out["reevaluated"] == 0
    assert out["risk_annotated"] == 1
    detail = svc.get_application(world.engine, aid)
    # 已生效续期保留原状态、原版本、原证据
    assert detail["application"]["status"] == "approved"
    assert detail["application"]["current_version"] == 1
    assert detail["items"][0]["decision"] == "included"
    risk = detail["risk_annotations"][0]
    assert risk["trigger_type"] == "provider_revoked"
    assert "事后问题" in risk["detail"]


def test_evidence_void_pending_drops_credit(world):
    world.person()
    world.provider()
    world.course("C1", credits=10.0)
    world.course("C2", credits=10.0)
    world.requirement(required=20.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", course="C1", date="2025-01-01")
    world.evidence_upload("b", uid="E2", course="C2", date="2025-02-01")
    aid = world.application()["application"]["id"]

    svc.void_evidence(world.engine, evidence_id=world.evidence["b"], reason="证明系伪造")
    detail = svc.get_application(world.engine, aid)
    assert detail["application"]["current_version"] == 2
    assert detail["version"]["credits_accepted"] == 10
    assert detail["version"]["credits_required_met"] == 0
    by_ev = {i["evidence_id"]: i for i in detail["items"]}
    assert by_ev[world.evidence["b"]]["exclusion_category"] == "void"
    # 原证据 a 仍保留在新版本快照中
    assert by_ev[world.evidence["a"]]["decision"] == "included"


def test_evidence_void_after_approval_only_risk(world):
    world.person()
    world.provider()
    world.course("C1", credits=20.0)
    world.requirement(required=20.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01", credits=20.0)
    aid = world.application()["application"]["id"]
    world.approve_both(aid)
    svc.void_evidence(world.engine, evidence_id=world.evidence["a"], reason="核查发现瑕疵")
    detail = svc.get_application(world.engine, aid)
    assert detail["application"]["status"] == "approved"
    assert detail["risk_annotations"][0]["trigger_type"] == "evidence_voided"
    # 快照保留原证据
    assert detail["items"][0]["evidence_status"] == "recorded"


def test_equivalence_rule_reversion_pending(world):
    world.person()
    world.provider()
    world.course("C1", credits=10.0)
    world.course("C2", credits=10.0)
    world.requirement(required=10.0)
    world.reviewers()
    # 初版等效规则：C1、C2 同组，跨地区/跨机构只计一次
    world.equivalence("ESG-BASIC", ["C1", "C2"])
    world.evidence_upload("a", uid="E1", course="C1", date="2025-01-01")
    world.evidence_upload("b", uid="E2", course="C2", date="2025-02-01")
    detail = world.application()
    aid = detail["application"]["id"]
    assert detail["version"]["credits_accepted"] == 10
    excluded = next(i for i in detail["items"] if i["evidence_id"] == world.evidence["b"])
    assert excluded["exclusion_category"] == "duplicate"

    # 换版：C2 移出等效组，b 应恢复计入
    world.equivalence("ESG-BASIC", ["C1"])
    detail2 = svc.get_application(world.engine, aid)
    assert detail2["application"]["current_version"] == 2
    assert detail2["version"]["credits_accepted"] == 20
    by_ev = {i["evidence_id"]: i for i in detail2["items"]}
    assert by_ev[world.evidence["b"]]["decision"] == "included"
    # 旧版本保留且标记 obsolete，可回溯
    assert _versions(world.engine, aid) == [(1, "obsolete", 10.0), (2, "active", 20.0)]
    logs = detail2["reevaluation_logs"]
    assert logs[-1]["changed"] == 1
    assert logs[-1]["event_type"] == "equivalence_reversion"


def test_equivalence_reversion_removing_all_members_restores_credits(world):
    world.person()
    world.provider()
    world.course("C1", credits=10.0)
    world.course("C2", credits=10.0)
    world.requirement(required=20.0)
    world.reviewers()
    world.equivalence("G", ["C1", "C2"])
    world.evidence_upload("a", uid="E1", course="C1", date="2025-01-01")
    world.evidence_upload("b", uid="E2", course="C2", date="2025-02-01")
    aid = world.application()["application"]["id"]
    assert svc.get_application(world.engine, aid)["version"]["credits_accepted"] == 10

    # v2 空组：两课均移出；受影响申请按历史成员也能被找到
    out = world.equivalence("G", [])
    assert out["reevaluated"] == 1
    detail = svc.get_application(world.engine, aid)
    assert detail["application"]["current_version"] == 2
    assert detail["version"]["credits_accepted"] == 20


def test_equivalence_reversion_after_approval_only_risk(world):
    world.person()
    world.provider()
    world.course("C1", credits=10.0)
    world.course("C2", credits=10.0)
    world.requirement(required=10.0)
    world.reviewers()
    world.equivalence("ESG-BASIC", ["C1", "C2"])
    world.evidence_upload("a", uid="E1", course="C1", date="2025-01-01")
    world.evidence_upload("b", uid="E2", course="C2", date="2025-02-01")
    aid = world.application()["application"]["id"]
    world.approve_both(aid)
    out = world.equivalence("ESG-BASIC", ["C1"])
    assert out["risk_annotated"] == 1
    assert out["reevaluated"] == 0
    detail = svc.get_application(world.engine, aid)
    assert detail["application"]["current_version"] == 1
    assert len(detail["risk_annotations"]) == 1


def test_reevaluation_without_change_keeps_version(world):
    world.person()
    world.provider("pr1")
    world.provider("pr2", name="无关机构")
    world.course("C1", credits=20.0)
    world.requirement(required=20.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", provider="pr1", date="2025-01-01", credits=20.0)
    aid = world.application()["application"]["id"]
    # 撤销一个与本申请无关的提供方，但本申请并未引用它 -> 不会入队
    out = svc.revoke_provider(world.engine, provider_id="pr2", reason="无关")
    assert out["affected"] == 0
    assert svc.get_application(world.engine, aid)["application"]["current_version"] == 1

    # 直接插一条引用了本申请但裁决不变的作业（模拟重复事件），处理后不前进版本
    with world.engine.begin() as conn:
        from skill_engine.services import new_id, now
        conn.execute(reevaluation_jobs.insert().values(
            id=new_id(), application_id=aid, event_type="provider_revoked",
            ref_id="pr2", detail="无实质影响事件", status="queued", attempts=0,
            lease_owner=None, leased_at=None, result=None,
            created_at=now(), processed_at=None))
    svc.run_reevaluation_queue(world.engine)
    detail = svc.get_application(world.engine, aid)
    assert detail["application"]["current_version"] == 1
    assert detail["reevaluation_logs"][-1]["changed"] == 0


def test_reevaluation_keeps_application_threshold_snapshot(world):
    world.person()
    world.provider()
    world.course("C1", credits=20.0)
    world.requirement(required=20.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01", credits=20.0)
    aid = world.application()["application"]["id"]
    # 申请发起后阈值上调；因证明作废触发重评，仍按旧阈值裁决，不改变达标结论
    world.requirement(required=40.0)
    with world.engine.begin() as conn:
        from skill_engine.services import new_id, now
        conn.execute(reevaluation_jobs.insert().values(
            id=new_id(), application_id=aid, event_type="provider_revoked",
            ref_id="pr-none", detail="阈值快照检验", status="queued", attempts=0,
            lease_owner=None, leased_at=None, result=None,
            created_at=now(), processed_at=None))
    svc.run_reevaluation_queue(world.engine)
    detail = svc.get_application(world.engine, aid)
    assert detail["version"]["credits_accepted"] == 20
    assert detail["version"]["credits_required_met"] == 1
    assert detail["application"]["required_credits"] == 20.0


def test_reevaluation_queue_recovers_stale_lease(world):
    world.person()
    world.provider()
    world.course("C1", credits=20.0)
    world.requirement(required=20.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01", credits=20.0)
    aid = world.application()["application"]["id"]
    svc.void_evidence(world.engine, evidence_id=world.evidence["a"], reason="x")
    # 事件已同步处理；再模拟一个僵死租约（进程中断在处理中途）
    with world.engine.begin() as conn:
        from skill_engine.services import new_id, now
        jid = new_id()
        conn.execute(reevaluation_jobs.insert().values(
            id=jid, application_id=aid, event_type="evidence_voided",
            ref_id=world.evidence["a"], detail="中断恢复", status="processing",
            attempts=1, lease_owner="dead-worker",
            leased_at="2000-01-01T00:00:00+00:00", result=None,
            created_at=now(), processed_at=None))
    svc.run_reevaluation_queue(world.engine)
    with world.engine.connect() as conn:
        job = conn.execute(select(reevaluation_jobs).where(
            reevaluation_jobs.c.id == jid)).mappings().one()
    assert job["status"] == "done"
