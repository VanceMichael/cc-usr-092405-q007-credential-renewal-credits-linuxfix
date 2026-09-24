"""触发重评与风险标注：撤销/作废/换版只影响未生效申请；队列抗中断。"""

import json

from skill_engine.models import durable_jobs
from tests.conftest import (
    make_course,
    make_credential,
    make_expert,
    make_provider,
    submit_proof,
    tick,
)


def _reviewer(client, code, role):
    response = client.post("/admin/reviewers", json={"reviewer_code": code, "name": "审核人", "role": role})
    assert response.status_code == 201, response.text
    return response.json()


def _approve(client, application_id, version=1):
    supervisor = _reviewer(client, f"REV-S-{application_id}", "supervisor")
    compliance = _reviewer(client, f"REV-C-{application_id}", "compliance")
    client.post(f"/applications/{application_id}/assignments", json={"reviewer_id": supervisor["id"]})
    client.post(f"/applications/{application_id}/assignments", json={"reviewer_id": compliance["id"]})
    client.post(
        f"/applications/{application_id}/opinions",
        json={"reviewer_id": supervisor["id"], "snapshot_version": version, "decision": "approve"},
    )
    client.post(
        f"/applications/{application_id}/opinions",
        json={"reviewer_id": compliance["id"], "snapshot_version": version, "decision": "approve"},
    )


def _setup(client, expert_code, app_code, provider_code):
    expert = make_expert(client, code=expert_code)
    credential = make_credential(client, expert_code=expert_code, code=f"CRED-{expert_code}", credits=5.0)
    provider = make_provider(client, code=provider_code)
    course = make_course(client, provider["id"], code=f"CRS-{expert_code}", credits=5.0)
    proof = submit_proof(client, provider["id"], expert["id"], course["id"], f"K-{expert_code}")
    application = client.post(
        "/applications", json={"credential_id": credential["id"], "application_code": app_code}
    ).json()
    return expert, credential, provider, course, proof, application


def test_provider_revocation_reevaluates_open_and_annotates_effective(client):
    # 申请一：先生效；申请二：留在审
    expert1, credential1, provider, course, proof1, app1 = _setup(client, "EXP-1", "APP-1", "PRV-1")
    _approve(client, app1["id"])
    assert client.get(f"/applications/{app1['id']}").json()["status"] == "approved"

    expert2 = make_expert(client, code="EXP-2")
    credential2 = make_credential(client, expert_code="EXP-2", code="CRED-EXP-2", credits=5.0)
    submit_proof(client, provider["id"], expert2["id"], course["id"], "K-EXP-2")
    app2 = client.post("/applications", json={"credential_id": credential2["id"], "application_code": "APP-2"}).json()

    # 撤销提供方资格
    client.post(f"/admin/providers/{provider['id']}/revoke")
    tick(client)

    # 在审申请被重评：提供方撤销的证明被排除，版本升级
    detail2 = client.get(f"/applications/{app2['id']}").json()
    assert detail2["snapshot_version"] == 2
    item = detail2["items"][0]
    assert item["decision"] == "excluded" and "provider_revoked" in item["reasons"]

    # 已生效续期：原证据保留（仍 counted），但追加风险标注
    detail1 = client.get(f"/applications/{app1['id']}").json()
    assert detail1["status"] == "approved"
    assert detail1["items"][0]["decision"] == "counted"
    renewals = client.get("/me/renewals", params={"expert_code": "EXP-1"}).json()
    assert len(renewals) == 1
    kinds = [a["kind"] for a in renewals[0]["risk_annotations"]]
    assert "provider_revoked" in kinds

    # 对外接口仍按原有效期回答
    validity = client.get(
        f"/external/credentials/{credential1['credential_code']}/validity", params={"on": "2026-10-01"}
    )
    assert validity.json()["valid"] is True


def test_proof_void_reevaluates_open_application(client):
    expert, credential, provider, course, proof, application = _setup(client, "EXP-1", "APP-1", "PRV-1")
    proof_id = proof["proof"]["id"]

    client.post(f"/proofs/{proof_id}/void")
    tick(client)

    detail = client.get(f"/applications/{application['id']}").json()
    assert detail["snapshot_version"] == 2
    item = detail["items"][0]
    assert item["decision"] == "excluded" and "proof_voided" in item["reasons"]


def test_equivalence_version_change_reevaluates_open_only(client):
    # 专家在北京，课程只直接适用上海；第一版等效规则不含北京 → 排除
    expert = make_expert(client, code="EXP-1", region="CN-BJ")
    credential = make_credential(client, expert_code="EXP-1", code="CRED-EXP-1", credits=5.0)
    provider = make_provider(client)
    course = make_course(
        client, provider["id"], code="XR", credits=5.0, regions=("CN-SH",), equivalence_group="ESG-CORE"
    )
    submit_proof(client, provider["id"], expert["id"], course["id"], "K-1")
    client.post(
        "/admin/equivalence-versions",
        json={"group_code": "ESG-CORE", "regions": ["CN-SH"], "effective_from": "2026-01-01"},
    )
    application = client.post(
        "/applications", json={"credential_id": credential["id"], "application_code": "APP-1"}
    ).json()
    items = client.get(f"/applications/{application['id']}").json()["items"]
    assert items[0]["decision"] == "excluded" and "region" in items[0]["reasons"]

    # 等效规则换版（纳入北京）：在审申请被重评后计入
    client.post(
        "/admin/equivalence-versions",
        json={"group_code": "ESG-CORE", "regions": ["CN-SH", "CN-BJ"], "effective_from": "2026-09-01"},
    )
    tick(client)
    detail = client.get(f"/applications/{application['id']}").json()
    assert detail["snapshot_version"] == 2
    assert detail["items"][0]["decision"] == "counted"


def test_effective_renewal_untouched_by_equivalence_change(client):
    expert = make_expert(client, code="EXP-1", region="CN-BJ")
    credential = make_credential(client, expert_code="EXP-1", code="CRED-EXP-1", credits=5.0)
    provider = make_provider(client)
    course = make_course(
        client, provider["id"], code="XR", credits=5.0, regions=("CN-SH",), equivalence_group="ESG-CORE"
    )
    submit_proof(client, provider["id"], expert["id"], course["id"], "K-1")
    client.post(
        "/admin/equivalence-versions",
        json={"group_code": "ESG-CORE", "regions": ["CN-SH", "CN-BJ"], "effective_from": "2026-01-01"},
    )
    application = client.post(
        "/applications", json={"credential_id": credential["id"], "application_code": "APP-1"}
    ).json()
    _approve(client, application["id"])

    # 换版移除北京：已生效续期保留原证据，只追加风险标注
    client.post(
        "/admin/equivalence-versions",
        json={"group_code": "ESG-CORE", "regions": ["CN-SH"], "effective_from": "2026-09-01"},
    )
    tick(client)
    detail = client.get(f"/applications/{application['id']}").json()
    assert detail["status"] == "approved"
    assert detail["items"][0]["decision"] == "counted"
    renewals = client.get("/me/renewals", params={"expert_code": "EXP-1"}).json()
    kinds = [a["kind"] for a in renewals[0]["risk_annotations"]]
    assert "equivalence_version" in kinds


def test_jobs_survive_restart(client):
    """系统中断（新建 worker 模拟重启）不丢核查队列、审议期限与重评工作。"""
    expert, credential, provider, course, proof, application = _setup(client, "EXP-1", "APP-1", "PRV-1")

    # 撤销提供方后不重评（模拟中断前只落了队）
    client.post(f"/admin/providers/{provider['id']}/revoke")
    with client.engine.begin() as conn:
        pending = conn.execute(durable_jobs.select().where(durable_jobs.c.status == "pending")).all()
        assert any(json.loads(row.payload).get("provider_id") == provider["id"] for row in pending)

    # 模拟重启：新应用实例复用同一数据库，启动即恢复队列
    from skill_engine import create_app
    from litestar.testing import TestClient

    restarted = create_app(client.engine, start_worker=False)
    with TestClient(restarted) as client2:
        response = client2.post("/ops/jobs/tick")
        assert response.status_code == 201
        detail = client2.get(f"/applications/{application['id']}").json()
        assert detail["snapshot_version"] == 2
        assert "provider_revoked" in detail["items"][0]["reasons"]


def test_review_deadline_marks_overdue(client, monkeypatch):
    expert, credential, provider, course, proof, application = _setup(client, "EXP-1", "APP-1", "PRV-1")

    # 时间快进到审议期限之后
    monkeypatch.setenv("SKILL_ENGINE_NOW", "2026-11-01T09:00:00")
    tick(client)
    detail = client.get(f"/applications/{application['id']}").json()
    event_types = [event["event_type"] for event in detail["events"]]
    assert "review_overdue" in event_types


def test_verification_sla_escalates(client, monkeypatch):
    expert = make_expert(client)
    make_credential(client)
    provider = make_provider(client)
    course = make_course(client, provider["id"])
    submit_proof(client, provider["id"], expert["id"], course["id"], "K-1", payload={"cert": "A"})
    submit_proof(client, provider["id"], expert["id"], course["id"], "K-1", payload={"cert": "B"})

    # 核查 SLA 到期后仍未结案 → 升级标记
    monkeypatch.setenv("SKILL_ENGINE_NOW", "2026-10-10T09:00:00")
    tick(client)
    cases = client.get("/verification-cases").json()
    assert cases[0]["escalated"] == 1
