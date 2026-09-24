"""审议流程：快照固定、审核人资格、版本化意见、批准生效。"""

from tests.conftest import (
    make_course,
    make_credential,
    make_expert,
    make_provider,
    submit_proof,
    tick,
)


def _make_reviewer(client, code, role):
    response = client.post("/admin/reviewers", json={"reviewer_code": code, "name": "审核人", "role": role})
    assert response.status_code == 201, response.text
    return response.json()


def _setup_application(client, *, expert_code="EXP-1", course_credits=6.0, required=5.0):
    expert = make_expert(client, code=expert_code)
    credential = make_credential(client, expert_code=expert_code, credits=required)
    provider = make_provider(client)
    course = make_course(client, provider["id"], credits=course_credits)
    submit_proof(client, provider["id"], expert["id"], course["id"], "K-1", completed_at="2026-01-10")
    application = client.post(
        "/applications", json={"credential_id": credential["id"], "application_code": "APP-1"}
    ).json()
    return expert, credential, provider, application


def test_application_fixes_snapshot(client):
    expert, credential, provider, application = _setup_application(client)
    detail = client.get(f"/applications/{application['id']}").json()
    assert detail["snapshot_version"] == 1
    assert detail["status"] == "in_review"
    assert detail["window_start"] == "2023-09-24"  # 滚动 3 年
    assert detail["window_end"] == "2026-09-24"
    assert len(detail["items"]) == 1
    assert detail["items"][0]["decision"] == "counted"
    assert detail["review_due_at"]  # 审议期限已登记


def test_reviewer_cannot_be_applicant(client):
    expert, credential, provider, application = _setup_application(client)
    applicant_as_reviewer = _make_reviewer(client, expert["expert_code"], "supervisor")
    response = client.post(
        f"/applications/{application['id']}/assignments", json={"reviewer_id": applicant_as_reviewer["id"]}
    )
    assert response.status_code == 409
    assert "申请人" in response.json()["detail"]


def test_reviewer_with_restricted_relation_rejected(client):
    expert, credential, provider, application = _setup_application(client)
    reviewer = _make_reviewer(client, "REV-1", "supervisor")
    client.post(
        f"/admin/reviewers/{reviewer['id']}/restrictions",
        json={"provider_id": provider["id"], "reason": "持股"},
    )
    response = client.post(f"/applications/{application['id']}/assignments", json={"reviewer_id": reviewer["id"]})
    assert response.status_code == 409
    assert "受限关系" in response.json()["detail"]


def test_opinion_must_match_seen_version(client):
    expert, credential, provider, application = _setup_application(client)
    supervisor = _make_reviewer(client, "REV-S", "supervisor")
    compliance = _make_reviewer(client, "REV-C", "compliance")
    client.post(f"/applications/{application['id']}/assignments", json={"reviewer_id": supervisor["id"]})
    client.post(f"/applications/{application['id']}/assignments", json={"reviewer_id": compliance["id"]})

    # 并发：合规人基于版本 1 提交时，版本已被重评推到 2 → 拒绝
    client.post(f"/admin/providers/{provider['id']}/revoke")
    tick(client)  # 触发重评，版本升到 2
    detail = client.get(f"/applications/{application['id']}").json()
    assert detail["snapshot_version"] == 2

    stale = client.post(
        f"/applications/{application['id']}/opinions",
        json={"reviewer_id": compliance["id"], "snapshot_version": 1, "decision": "approve"},
    )
    assert stale.status_code == 409
    assert "版本" in stale.json()["detail"]

    # 基于当前版本 2 提交则被接受
    fresh = client.post(
        f"/applications/{application['id']}/opinions",
        json={"reviewer_id": compliance["id"], "snapshot_version": 2, "decision": "approve"},
    )
    assert fresh.status_code == 201, fresh.text


def test_both_approvals_make_renewal_effective(client):
    expert, credential, provider, application = _setup_application(client)
    supervisor = _make_reviewer(client, "REV-S", "supervisor")
    compliance = _make_reviewer(client, "REV-C", "compliance")
    client.post(f"/applications/{application['id']}/assignments", json={"reviewer_id": supervisor["id"]})
    client.post(f"/applications/{application['id']}/assignments", json={"reviewer_id": compliance["id"]})

    client.post(
        f"/applications/{application['id']}/opinions",
        json={"reviewer_id": supervisor["id"], "snapshot_version": 1, "decision": "approve"},
    )
    # 只有一方批准时不生效
    assert client.get(f"/applications/{application['id']}").json()["status"] == "in_review"

    client.post(
        f"/applications/{application['id']}/opinions",
        json={"reviewer_id": compliance["id"], "snapshot_version": 1, "decision": "approve"},
    )
    detail = client.get(f"/applications/{application['id']}").json()
    assert detail["status"] == "approved"

    # 对外接口只回答指定日期是否有效
    valid = client.get(f"/external/credentials/{credential['credential_code']}/validity", params={"on": "2026-10-01"})
    assert valid.json() == {"credential_code": credential["credential_code"], "on": "2026-10-01", "valid": True}
    invalid = client.get(
        f"/external/credentials/{credential['credential_code']}/validity", params={"on": "2031-01-01"}
    )
    assert invalid.json()["valid"] is False


def test_reject_opinion_closes_application(client):
    expert, credential, provider, application = _setup_application(client)
    supervisor = _make_reviewer(client, "REV-S", "supervisor")
    client.post(f"/applications/{application['id']}/assignments", json={"reviewer_id": supervisor["id"]})
    client.post(
        f"/applications/{application['id']}/opinions",
        json={"reviewer_id": supervisor["id"], "snapshot_version": 1, "decision": "reject", "comment": "范围不符"},
    )
    assert client.get(f"/applications/{application['id']}").json()["status"] == "rejected"


def test_unassigned_reviewer_cannot_opine(client):
    expert, credential, provider, application = _setup_application(client)
    stranger = _make_reviewer(client, "REV-X", "supervisor")
    response = client.post(
        f"/applications/{application['id']}/opinions",
        json={"reviewer_id": stranger["id"], "snapshot_version": 1, "decision": "approve"},
    )
    assert response.status_code == 409


def test_applicant_views_own_detail(client):
    expert, credential, provider, application = _setup_application(client)
    mine = client.get("/me/applications", params={"expert_code": expert["expert_code"]}).json()
    assert len(mine) == 1
    assert mine[0]["id"] == application["id"]
    assert mine[0]["items"][0]["trace"]  # 逐项来龙去脉可见
    assert mine[0]["events"]  # 审议事件可见
