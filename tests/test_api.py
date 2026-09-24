"""HTTP 接口、申请人明细、对外有效性与学分溯源。"""

from skill_engine import services as svc


def _setup(world):
    world.person()
    world.provider()
    world.course("C1", credits=10.0)
    world.course("C2", credits=12.0)
    world.requirement(required=20.0, cap=30.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", course="C1", date="2025-01-01")
    world.evidence_upload("b", uid="E2", course="C2", date="2025-02-01")


def test_http_full_flow(client, world):
    _setup(world)
    r = client.post("/applications", json={
        "applicant_id": "p1", "competence": "GF", "period_end": "2026-09-24",
        "direct_reviewer_id": "d1", "compliance_reviewer_id": "co1",
    })
    assert r.status_code == 201
    aid = r.json()["application"]["id"]

    r1 = client.post(f"/applications/{aid}/opinions",
                     json={"reviewer_id": "d1", "decision": "approved",
                           "comment": "专业范围符合"})
    assert r1.status_code == 201
    r2 = client.post(f"/applications/{aid}/opinions",
                     json={"reviewer_id": "co1", "decision": "approved"})
    assert r2.status_code == 201
    assert client.get(f"/applications/{aid}").json()["application"]["status"] == "approved"


def test_http_conflict_returns_409(client, world):
    _setup(world)
    # 申请人本人当审核人
    r = client.post("/applications", json={
        "applicant_id": "p1", "competence": "GF", "period_end": "2026-09-24",
        "direct_reviewer_id": "p1", "compliance_reviewer_id": "co1",
    })
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"


def test_http_missing_field_and_not_found(client, world):
    _setup(world)
    r = client.post("/applications", json={"applicant_id": "p1"})
    assert r.status_code == 409  # 领域校验：能力未配置/审核人缺失前置
    assert client.get("/applications/nope").status_code == 404


def test_http_provider_upload_and_review_queue(client, world):
    world.person()
    world.provider()
    world.course("C1")
    body = {"evidence_uid": "E1", "provider_id": "pr1", "person_id": "p1",
            "course_code": "C1", "completion_date": "2025-05-01",
            "credits_claimed": 10, "content_summary": "完成 40 学时"}
    assert client.post("/provider/evidence", json=body).status_code == 201
    assert client.post("/provider/evidence", json=body).json()["status"] == "duplicate_existing"
    body["content_summary"] = "完全不同的内容"
    mismatch = client.post("/provider/evidence", json=body).json()
    assert mismatch["status"] == "under_review"
    q = client.get("/admin/review-queue").json()
    assert q["count"] == 1
    qid = q["items"][0]["id"]
    r = client.post(f"/admin/review-queue/{qid}/resolve",
                    json={"resolution": "confirmed", "notes": "已核实"})
    assert r.status_code == 201


def test_applicant_detail_lists_line_items(world):
    _setup(world)
    detail = world.application()
    aid = detail["application"]["id"]
    out = svc.applicant_detail(world.engine, applicant_id="p1")
    assert out["applicant_id"] == "p1"
    assert len(out["evidences"]) == 2
    app_view = next(a for a in out["applications"] if a["application_id"] == aid)
    assert app_view["credits_accepted"] == 22
    categories = {i["exclusion_category"] for i in app_view["items"]}
    assert categories == {None}
    # 明细中包含逐项原因结构
    assert all("exclusion_reason" in i for i in app_view["items"])


def test_applicant_detail_explains_each_exclusion(world):
    world.person()
    world.provider()
    world.course("C1", credits=10.0)
    world.course("C2", credits=10.0, scopes=("OTHER",))
    world.requirement(required=20.0, cap=15.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", course="C1", date="2025-01-01")
    world.evidence_upload("b", uid="E2", course="C1", date="2025-02-01",
                          summary="同一课程重复")
    world.evidence_upload("c", uid="E3", course="C2", date="2025-03-01")
    detail = world.application()
    cats = {i["evidence_id"]: i["exclusion_category"] for i in detail["items"]}
    assert cats[world.evidence["a"]] in (None, "capped")
    assert cats[world.evidence["b"]] == "duplicate"
    assert cats[world.evidence["c"]] == "out_of_scope"
    # a 受封顶部分计入
    a_item = next(i for i in detail["items"] if i["evidence_id"] == world.evidence["a"])
    assert a_item["credits_applied"] == 10
    c_item = next(i for i in detail["items"] if i["evidence_id"] == world.evidence["c"])
    assert c_item["decision"] == "excluded"
    assert "能力范围" in c_item["exclusion_reason"]


def test_external_validity_only_answers_yes_or_no(world):
    _setup(world)
    aid = world.application()["application"]["id"]
    # 尚未生效
    v = svc.external_validity(world.engine, applicant_id="p1", competence="GF",
                              query_date="2026-09-24")
    assert set(v.keys()) == {"valid", "as_of", "competence"}
    assert v["valid"] is False

    world.approve_both(aid)
    # 生效日当天及周期内有效
    assert svc.external_validity(world.engine, applicant_id="p1", competence="GF",
                                 query_date="2026-09-24")["valid"] is True
    assert svc.external_validity(world.engine, applicant_id="p1", competence="GF",
                                 query_date="2025-01-01")["valid"] is False
    # 周期之外
    assert svc.external_validity(world.engine, applicant_id="p1", competence="GF",
                                 query_date="2030-01-01")["valid"] is False
    # 撤销提供方不改变已生效事实（风险另附，不对外暴露）
    svc.revoke_provider(world.engine, provider_id="pr1", reason="事后")
    assert svc.external_validity(world.engine, applicant_id="p1", competence="GF",
                                 query_date="2026-09-24")["valid"] is True
    # 不相关能力/人员
    assert svc.external_validity(world.engine, applicant_id="p1", competence="NOPE",
                                 query_date="2026-09-24")["valid"] is False


def test_credit_trace_shows_adoption_and_exclusion_history(world):
    world.person()
    world.provider()
    world.course("C1", credits=20.0)
    world.requirement(required=20.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01", credits=20.0)
    aid = world.application()["application"]["id"]

    trace = svc.trace_credit(world.engine, applicant_id="p1",
                             evidence_id=world.evidence["a"])
    assert trace["evidence"]["id"] == world.evidence["a"]
    assert trace["application_trail"][0]["decision"] == "included"
    assert trace["application_trail"][0]["credits_applied"] == 20
    assert trace["application_trail"][0]["dedup_fingerprint"]

    # 作废旧事件后，新版本显示排除，旧版本保留采用记录
    svc.void_evidence(world.engine, evidence_id=world.evidence["a"], reason="溯源测试")
    trace2 = svc.trace_credit(world.engine, applicant_id="p1",
                              evidence_id=world.evidence["a"])
    decisions = {(t["version"], t["decision"], t["exclusion_category"])
                 for t in trace2["application_trail"]}
    assert (1, "included", None) in decisions
    assert (2, "excluded", "void") in decisions


def test_credit_trace_rejects_other_applicant(world):
    world.person()
    world.person("p2", name="李四")
    world.provider()
    world.course("C1")
    world.requirement(required=10.0)
    world.reviewers()
    world.evidence_upload("a", uid="E1", date="2025-01-01")
    world.application()
    import pytest
    with pytest.raises(svc.NotFound):
        svc.trace_credit(world.engine, applicant_id="p2",
                         evidence_id=world.evidence["a"])
