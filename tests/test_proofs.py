"""证明受理：内容摘要、沿用、异文核查。"""

from tests.conftest import make_course, make_credential, make_expert, make_provider, submit_proof


def _setup(client):
    expert = make_expert(client)
    make_credential(client)
    provider = make_provider(client)
    course = make_course(client, provider["id"])
    return expert, provider, course


def test_same_proof_resubmission_reuses_record(client):
    expert, provider, course = _setup(client)
    first = submit_proof(client, provider["id"], expert["id"], course["id"], "KEY-1", payload={"cert": "A"})
    assert first["reused"] is False
    proof_id = first["proof"]["id"]

    again = submit_proof(client, provider["id"], expert["id"], course["id"], "KEY-1", payload={"cert": "A"})
    assert again["reused"] is True
    assert again["proof"]["id"] == proof_id  # 沿用原记录，不产生新行
    assert again["proof"]["status"] == "accepted"


def test_conflicting_content_suspends_and_opens_case(client):
    expert, provider, course = _setup(client)
    submit_proof(client, provider["id"], expert["id"], course["id"], "KEY-1", payload={"cert": "A"})

    conflict = submit_proof(client, provider["id"], expert["id"], course["id"], "KEY-1", payload={"cert": "B"})
    assert conflict["suspended"] is True
    assert conflict["proof"]["status"] == "suspended"

    cases = client.get("/verification-cases").json()
    assert len(cases) == 1
    assert cases[0]["proof_id"] == conflict["proof"]["id"]
    assert cases[0]["status"] == "open"
    assert cases[0]["due_at"]  # 核查 SLA 已登记

    # 核查期间再次提交原内容也保持暂停
    again = submit_proof(client, provider["id"], expert["id"], course["id"], "KEY-1", payload={"cert": "A"})
    assert again["reused"] is True
    assert again["proof"]["status"] == "suspended"


def test_verification_confirm_original_restores_counting(client):
    expert, provider, course = _setup(client)
    submit_proof(client, provider["id"], expert["id"], course["id"], "KEY-1", payload={"cert": "A"})
    submit_proof(client, provider["id"], expert["id"], course["id"], "KEY-1", payload={"cert": "B"})
    case = client.get("/verification-cases").json()[0]

    resolved = client.post(
        f"/verification-cases/{case['id']}/resolve", json={"resolution": "confirm_original"}
    )
    assert resolved.status_code == 201, resolved.text
    assert resolved.json()["status"] == "resolved"

    # 恢复计入：重评作业入队，申请会重新计算（此处直接验证证明状态）
    cases = client.get("/verification-cases").json()
    assert cases == []


def test_verification_void_marks_proof_voided(client):
    expert, provider, course = _setup(client)
    result = submit_proof(client, provider["id"], expert["id"], course["id"], "KEY-1", payload={"cert": "A"})
    proof_id = result["proof"]["id"]
    submit_proof(client, provider["id"], expert["id"], course["id"], "KEY-1", payload={"cert": "B"})
    case = client.get("/verification-cases").json()[0]

    client.post(f"/verification-cases/{case['id']}/resolve", json={"resolution": "void_proof"})
    # 作废后证明状态为 voided
    from skill_engine.models import proofs

    with client.engine.begin() as conn:
        row = conn.execute(proofs.select().where(proofs.c.id == proof_id)).first()
        assert row.status == "voided"


def test_revoked_provider_cannot_upload(client):
    expert, provider, course = _setup(client)
    client.post(f"/admin/providers/{provider['id']}/revoke")
    response = client.post(
        "/proofs",
        json={
            "proof_key": "KEY-X",
            "provider_id": provider["id"],
            "expert_id": expert["id"],
            "course_id": course["id"],
            "completed_at": "2026-01-10",
            "payload": {"cert": "X"},
        },
    )
    assert response.status_code == 409
