"""后台 worker：生命周期、崩溃恢复与看门狗联动。"""

import time

from skill_engine import services as svc
from skill_engine.worker import BackgroundWorker


def test_worker_tick_processes_void_event(world):
    world.person()
    world.provider()
    world.course("C1", credits=20.0)
    world.requirement(required=20.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01", credits=20.0)
    aid = world.application()["application"]["id"]

    # 直接插入待处理重评作业，模拟进程重启后的恢复
    from skill_engine.models import reevaluation_jobs
    from sqlalchemy import select
    with world.engine.begin() as conn:
        # 先作废证明，再手工入队（绕过事件触发时的同步处理）
        from skill_engine.models import completion_evidence
        conn.execute(completion_evidence.update().values(status="void", void_reason="恢复演练"))
        conn.execute(reevaluation_jobs.insert().values(
            id=svc.new_id(), application_id=aid, event_type="evidence_voided",
            ref_id=world.evidence["a"], detail="恢复演练", status="queued", attempts=0,
            lease_owner=None, leased_at=None, result=None,
            created_at=svc.now(), processed_at=None))

    worker = BackgroundWorker(world.engine, interval_seconds=0.05)
    worker.tick()
    detail = svc.get_application(world.engine, aid)
    assert detail["application"]["current_version"] == 2
    assert detail["items"][0]["exclusion_category"] == "void"


def test_worker_start_stop_lifecycle(engine):
    worker = BackgroundWorker(engine, interval_seconds=0.05)
    worker.start()
    worker.start()  # 幂等
    time.sleep(0.15)
    worker.stop()
    assert worker._thread is None
