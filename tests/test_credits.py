"""滚动周期学分计算：逐项说明重复、封顶、过期、范围不符、地区与等效。"""

from tests.conftest import (
    make_course,
    make_credential,
    make_expert,
    make_provider,
    submit_proof,
)


def _items_by_course(application):
    return {item["course_code"]: item for item in application["items"]}


def test_evaluation_explains_each_exclusion(client):
    expert = make_expert(client)
    credential = make_credential(client, credits=10.0)
    provider = make_provider(client)

    ok = make_course(client, provider["id"], code="OK", credits=6.0)
    old = make_course(client, provider["id"], code="OLD", credits=5.0)
    out_of_scope = make_course(client, provider["id"], code="SCOPE", credits=5.0, scopes=("risk-model",))
    wrong_region = make_course(client, provider["id"], code="REGION", credits=5.0, regions=("EU-DE",))

    submit_proof(client, provider["id"], expert["id"], ok["id"], "K-OK", completed_at="2026-01-10")
    # 过期：完成日期在滚动窗口（2023-09-24 起）之前
    submit_proof(client, provider["id"], expert["id"], old["id"], "K-OLD", completed_at="2020-01-01")
    submit_proof(client, provider["id"], expert["id"], out_of_scope["id"], "K-SCOPE", completed_at="2026-01-10")
    submit_proof(client, provider["id"], expert["id"], wrong_region["id"], "K-REGION", completed_at="2026-01-10")

    application = client.post(
        "/applications", json={"credential_id": credential["id"], "application_code": "APP-1"}
    ).json()
    detail = client.get(f"/applications/{application['id']}").json()
    items = _items_by_course(detail)

    assert items["OK"]["decision"] == "counted"
    assert items["OK"]["credits_counted"] == 6.0
    assert items["OLD"]["decision"] == "excluded" and "expired" in items["OLD"]["reasons"]
    assert items["SCOPE"]["decision"] == "excluded" and "scope" in items["SCOPE"]["reasons"]
    assert items["REGION"]["decision"] == "excluded" and "region" in items["REGION"]["reasons"]
    # 抽查任一学分：轨迹完整说明采用/排除的来龙去脉
    for item in items.values():
        assert item["trace"], item
        assert all({"check", "result", "detail"} <= set(step) for step in item["trace"])


def test_duplicate_content_across_providers_counted_once(client):
    expert = make_expert(client)
    credential = make_credential(client, credits=20.0)
    provider_a = make_provider(client, "PRV-A")
    provider_b = make_provider(client, "PRV-B")
    course_a = make_course(client, provider_a["id"], code="CRS-A", credits=5.0)
    course_b = make_course(client, provider_b["id"], code="CRS-B", credits=5.0)

    # 多机构重复证明：同一内容（同摘要）经两家机构提交
    same_payload = {"cert": " identical-content "}
    submit_proof(client, provider_a["id"], expert["id"], course_a["id"], "K-A", payload=same_payload)
    submit_proof(client, provider_b["id"], expert["id"], course_b["id"], "K-B", payload=same_payload)

    application = client.post(
        "/applications", json={"credential_id": credential["id"], "application_code": "APP-1"}
    ).json()
    detail = client.get(f"/applications/{application['id']}").json()
    items = _items_by_course(detail)

    assert items["CRS-A"]["decision"] == "counted"
    assert items["CRS-B"]["decision"] == "excluded"
    assert "duplicate" in items["CRS-B"]["reasons"]
    duplicate_trace = [s for s in items["CRS-B"]["trace"] if s["check"] == "duplicate"][0]
    assert "重复" in duplicate_trace["detail"]


def test_period_cap_caps_credits(client):
    expert = make_expert(client)
    credential = make_credential(client, credits=100.0)
    provider = make_provider(client)
    first = make_course(client, provider["id"], code="C1", credits=6.0)
    second = make_course(client, provider["id"], code="C2", credits=6.0)
    # 能力范围周期上限 8 学分：第一门全计，第二门只计 2
    client.post(
        "/admin/period-caps",
        json={"cap_key": "scope:green-finance", "cap_type": "scope", "max_credits": 8.0},
    )

    submit_proof(client, provider["id"], expert["id"], first["id"], "K-1", completed_at="2026-01-10")
    submit_proof(client, provider["id"], expert["id"], second["id"], "K-2", completed_at="2026-02-10")

    application = client.post(
        "/applications", json={"credential_id": credential["id"], "application_code": "APP-1"}
    ).json()
    detail = client.get(f"/applications/{application['id']}").json()
    items = _items_by_course(detail)

    assert items["C1"]["decision"] == "counted" and items["C1"]["credits_counted"] == 6.0
    assert items["C2"]["decision"] == "capped" and items["C2"]["credits_counted"] == 2.0
    assert "cap" in items["C2"]["reasons"]


def test_equivalence_group_cap(client):
    expert = make_expert(client)
    credential = make_credential(client, credits=100.0)
    provider = make_provider(client)
    first = make_course(client, provider["id"], code="G1", credits=5.0, equivalence_group="ESG-BASIC")
    second = make_course(client, provider["id"], code="G2", credits=5.0, equivalence_group="ESG-BASIC")
    client.post(
        "/admin/period-caps",
        json={"cap_key": "equivalence_group:ESG-BASIC", "cap_type": "equivalence_group", "max_credits": 5.0},
    )

    submit_proof(client, provider["id"], expert["id"], first["id"], "K-1", completed_at="2026-01-10")
    submit_proof(client, provider["id"], expert["id"], second["id"], "K-2", completed_at="2026-02-10")

    application = client.post(
        "/applications", json={"credential_id": credential["id"], "application_code": "APP-1"}
    ).json()
    items = _items_by_course(client.get(f"/applications/{application['id']}").json())
    assert items["G1"]["credits_counted"] == 5.0
    assert items["G2"]["decision"] == "excluded" and "cap" in items["G2"]["reasons"]


def test_cross_region_equivalence_counts_via_active_version(client):
    expert = make_expert(client, region="CN-BJ")
    credential = make_credential(client, credits=5.0)
    provider = make_provider(client)
    # 课程只直接适用于上海，但等效组生效版本覆盖北京
    course = make_course(
        client, provider["id"], code="XR", credits=5.0, regions=("CN-SH",), equivalence_group="ESG-CORE"
    )
    submit_proof(client, provider["id"], expert["id"], course["id"], "K-1", completed_at="2026-01-10")

    # 未发布等效版本：地区不符
    application = client.post(
        "/applications", json={"credential_id": credential["id"], "application_code": "APP-1"}
    ).json()
    items = _items_by_course(client.get(f"/applications/{application['id']}").json())
    assert items["XR"]["decision"] == "excluded" and "region" in items["XR"]["reasons"]


def test_cross_region_equivalence_after_publish(client):
    expert = make_expert(client, region="CN-BJ")
    credential = make_credential(client, credits=5.0)
    provider = make_provider(client)
    course = make_course(
        client, provider["id"], code="XR", credits=5.0, regions=("CN-SH",), equivalence_group="ESG-CORE"
    )
    submit_proof(client, provider["id"], expert["id"], course["id"], "K-1", completed_at="2026-01-10")
    client.post(
        "/admin/equivalence-versions",
        json={"group_code": "ESG-CORE", "regions": ["CN-SH", "CN-BJ"], "effective_from": "2026-01-01"},
    )

    application = client.post(
        "/applications", json={"credential_id": credential["id"], "application_code": "APP-1"}
    ).json()
    items = _items_by_course(client.get(f"/applications/{application['id']}").json())
    assert items["XR"]["decision"] == "counted"
    region_trace = [s for s in items["XR"]["trace"] if s["check"] == "region"][0]
    assert "等效组" in region_trace["detail"]


def test_course_validity_window_enforced(client):
    expert = make_expert(client)
    credential = make_credential(client, credits=5.0)
    provider = make_provider(client)
    course = make_course(client, provider["id"], code="V", credits=5.0, valid_from="2026-01-01", valid_to="2026-06-30")
    # 完成日期超出课程有效期
    submit_proof(client, provider["id"], expert["id"], course["id"], "K-1", completed_at="2026-08-01")

    application = client.post(
        "/applications", json={"credential_id": credential["id"], "application_code": "APP-1"}
    ).json()
    items = _items_by_course(client.get(f"/applications/{application['id']}").json())
    assert items["V"]["decision"] == "excluded" and "expired" in items["V"]["reasons"]
