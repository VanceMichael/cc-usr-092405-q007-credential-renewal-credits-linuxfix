"""滚动周期裁决引擎纯函数测试。"""

from skill_engine.evaluation import (
    CycleContext,
    EvidenceCandidate,
    content_hash,
    evaluate_cycle,
    rolling_window,
)


def course(cid, code="C1", version=1, scopes=("GF",), regions=(), valid_from="2000-01-01",
           valid_until=None, credits=10.0):
    return {
        "id": cid, "course_code": code, "version": version, "title": f"课程{code}",
        "competence_scopes": list(scopes), "credits": credits,
        "valid_from": valid_from, "valid_until": valid_until,
        "applicable_regions": list(regions),
    }


def ev(eid, *, date="2025-03-01", credits=10.0, course="C1", provider="pr1",
       status="recorded", order=0, digest=None, duplicate_of=None,
       provider_status="active", uid=None):
    return EvidenceCandidate(
        evidence_id=eid, evidence_uid=uid or f"U{eid}", provider_id=provider,
        provider_name=f"机构{provider}", provider_status=provider_status,
        course_code=course, completion_date=date, credits=credits,
        evidence_status=status, recorded_order=order,
        content_hash=digest or f"hash-{eid}", duplicate_of_id=duplicate_of,
    )


def ctx(catalog, *, groups=(), competence="GF", region="华东", start="2023-09-24",
        end="2026-09-24", required=20.0, cap=None):
    return CycleContext(
        competence=competence, region=region, period_start=start, period_end=end,
        required_credits=required, per_cycle_cap=cap,
        catalog_rows=catalog, equivalence_groups=list(groups),
    )


def by_id(result, eid):
    return next(i for i in result.items if i.evidence_id == eid)


def test_window_included_and_required_met():
    c = ctx({"C1": [course("cat1", credits=12.0)],
             "C2": [course("cat2", code="C2", credits=10.0)]})
    result = evaluate_cycle(c, [ev("a", course="C1", date="2025-01-01", credits=12,
                                  digest="ha"),
                                ev("b", course="C2", date="2025-06-01", credits=10,
                                  digest="hb")])
    assert result.credits_accepted == 22
    assert result.required_met is True
    assert all(i.included for i in result.items)


def test_credits_come_from_catalog_not_claim():
    # 提供方申报 99，课程目录登记 10，以目录为准
    c = ctx({"C1": [course("cat1", credits=10.0)]})
    result = evaluate_cycle(c, [ev("a", credits=99.0)])
    assert result.credits_accepted == 10
    assert result.items[0].credits == 10


def test_expired_outside_window_and_course_validity():
    # 完成日期在滚动窗口之外
    c = ctx({"C1": [course("cat1")]})
    r1 = evaluate_cycle(c, [ev("old", date="2020-01-01")])
    assert by_id(r1, "old").category == "expired"
    assert "滚动周期" in by_id(r1, "old").reason

    # 课程有效期在完成之日已届满
    c2 = ctx({"C1": [course("cat1", valid_from="2020-01-01", valid_until="2024-12-31")]})
    r2 = evaluate_cycle(c2, [ev("late", date="2025-06-01")])
    assert by_id(r2, "late").category == "expired"
    assert "有效期已" in by_id(r2, "late").reason


def test_out_of_scope_competence_and_region():
    c = ctx({"C1": [course("cat1", scopes=("ESG",), regions=("华北",))]})
    r = evaluate_cycle(c, [ev("a", date="2025-01-01")])
    item = by_id(r, "a")
    assert item.category == "out_of_scope"
    assert "能力范围" in item.reason

    c2 = ctx({"C1": [course("cat1", regions=("华北", "华南"))]})
    r2 = evaluate_cycle(c2, [ev("a", date="2025-01-01")])
    assert by_id(r2, "a").category == "out_of_scope"
    assert "地区" in by_id(r2, "a").reason

    # 课程不在目录
    r3 = evaluate_cycle(ctx({}), [ev("a", course="ZZ", date="2025-01-01")])
    assert by_id(r3, "a").category == "out_of_scope"


def test_duplicate_content_hash_same_course_and_equivalence_group():
    c = ctx({
        "C1": [course("cat1")],
        "C2": [course("cat2", code="C2")],
    })
    same = content_hash(person_id="p1", course_code="C1", completion_date="2025-01-01",
                        credits_claimed=10, content_summary="同一内容")
    # 内容指纹重复（多机构）
    r = evaluate_cycle(c, [
        ev("a", provider="pr1", digest=same, order=0),
        ev("b", provider="pr2", digest=same, order=1),
    ])
    assert by_id(r, "a").included
    assert by_id(r, "b").category == "duplicate"

    # 同一课程重复
    r2 = evaluate_cycle(ctx({"C1": [course("cat1")]}), [
        ev("a", date="2025-01-01", digest="h1", order=0),
        ev("b", date="2025-02-01", digest="h2", order=1),
    ])
    assert by_id(r2, "b").category == "duplicate"

    # 等效组跨课程只计一次
    groups = (("rule1", "G1", {"cat1", "cat2"}),)
    r3 = evaluate_cycle(ctx({"C1": [course("cat1")], "C2": [course("cat2", code="C2")]},
                            groups=groups), [
        ev("a", course="C1", date="2025-01-01", digest="h1", order=0),
        ev("b", course="C2", date="2025-02-01", digest="h2", order=1),
    ])
    assert by_id(r3, "a").included
    assert by_id(r3, "b").category == "duplicate"
    assert "等效" in by_id(r3, "b").reason
    assert by_id(r3, "a").equivalence_rule_id == "rule1"


def test_duplicate_submission_pointer_excluded_first():
    c = ctx({"C1": [course("cat1")]})
    r = evaluate_cycle(c, [ev("dup", duplicate_of="orig")])
    assert by_id(r, "dup").category == "duplicate"
    assert "orig" in by_id(r, "dup").reason


def test_cycle_cap_full_and_partial():
    c = ctx({"C1": [course("cat1")]}, cap=15)
    r = evaluate_cycle(c, [
        ev("a", date="2025-01-01", credits=10, digest="h1", order=0),
        ev("b", date="2025-02-01", credits=10, digest="h2",
           course="C9", order=1),
    ])
    # C9 不在目录会先被范围排除；用不同课程码但需在目录
    c2 = ctx({"C1": [course("cat1")], "C2": [course("cat2", code="C2")]}, cap=15)
    r = evaluate_cycle(c2, [
        ev("a", course="C1", date="2025-01-01", credits=10, digest="h1", order=0),
        ev("b", course="C2", date="2025-02-01", credits=10, digest="h2", order=1),
    ])
    assert r.credits_accepted == 15
    b = by_id(r, "b")
    assert b.included and b.category == "capped"
    assert b.credits_applied == 5

    # 再添一条完全无法计入
    c3 = ctx({"C1": [course("cat1")], "C2": [course("cat2", code="C2")],
              "C3": [course("cat3", code="C3")]}, cap=15)
    r3 = evaluate_cycle(c3, [
        ev("a", course="C1", date="2025-01-01", credits=10, digest="h1", order=0),
        ev("b", course="C2", date="2025-02-01", credits=10, digest="h2", order=1),
        ev("d", course="C3", date="2025-03-01", credits=10, digest="h3", order=2),
    ])
    assert by_id(r3, "d").category == "capped"
    assert by_id(r3, "d").decision == "excluded"


def test_under_review_void_and_revoked():
    base = {"C1": [course("cat1")]}
    r = evaluate_cycle(ctx(base), [ev("a", status="under_review")])
    assert by_id(r, "a").category == "under_review"

    r2 = evaluate_cycle(ctx(base), [ev("a", status="void")])
    assert by_id(r2, "a").category == "void"

    r3 = evaluate_cycle(ctx(base), [ev("a", provider_status="revoked")])
    assert by_id(r3, "a").category == "provider_revoked"


def test_rolling_window():
    assert rolling_window("2026-09-24", 3) == ("2023-09-24", "2026-09-24")
    # 闰年 2 月 29 日回退
    assert rolling_window("2024-02-29", 1) == ("2023-02-28", "2024-02-29")


def test_content_hash_ignores_provider_and_whitespace():
    h1 = content_hash(person_id="p1", course_code="c1", completion_date="2025-01-01",
                      credits_claimed=10, content_summary="内容 A")
    h2 = content_hash(person_id="p1", course_code="C1", completion_date="2025-01-01",
                      credits_claimed=10, content_summary=" 内容   A ")
    assert h1 == h2
