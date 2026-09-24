"""测试夹具与世界构建辅助。"""

import os

# 必须在导入 skill_engine 之前设置：避免模块级 app 创建文件库并启动后台线程
os.environ.setdefault("ENABLE_BACKGROUND_WORKER", "0")
os.environ.setdefault("DATABASE_PATH", ":memory:")

import pytest
from litestar.testing import TestClient

from skill_engine import create_app, services as svc
from skill_engine.database import create_database_engine, create_schema


@pytest.fixture
def engine():
    eng = create_database_engine(":memory:")
    create_schema(eng)
    return eng


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine, start_worker=False)) as test_client:
        yield test_client


@pytest.fixture
def world(engine):
    return World(engine)


class World:
    """以服务层快速搭建审议场景。"""

    def __init__(self, engine):
        self.engine = engine
        self.courses = {}      # course_code -> catalog id
        self.evidence = {}     # 业务键 -> evidence id

    def person(self, person_id="p1", name="张三", region="华东"):
        with self.engine.begin() as conn:
            svc.register_person(conn, person_id=person_id, name=name, region=region)
        return person_id

    def provider(self, provider_id="pr1", name="绿色学院"):
        with self.engine.begin() as conn:
            svc.register_provider(conn, provider_id=provider_id, name=name)
        return provider_id

    def relation(self, provider_id, person_id, relation="任职"):
        with self.engine.begin() as conn:
            svc.add_provider_relationship(
                conn, provider_id=provider_id, person_id=person_id, relation=relation
            )

    def course(self, code, *, title=None, scopes=("GF",), credits=10.0,
               valid_from="2000-01-01", valid_until=None, regions=None):
        with self.engine.begin() as conn:
            row = svc.add_course_version(
                conn, course_code=code, title=title or f"课程{code}",
                competence_scopes=list(scopes), credits=credits,
                valid_from=valid_from, valid_until=valid_until,
                applicable_regions=[] if regions is None else list(regions),
            )
        self.courses[code] = row["id"]
        return row["id"]

    def requirement(self, competence="GF", *, required=20.0, years=3, cap=None):
        with self.engine.begin() as conn:
            svc.set_renewal_requirement(
                conn, competence=competence, required_credits=required,
                period_years=years, per_cycle_cap=cap,
            )

    def evidence_upload(self, key, *, uid=None, provider="pr1", person="p1",
                        course="C1", date="2025-03-01", credits=10.0,
                        summary=None):
        result = svc.upload_evidence(
            self.engine, evidence_uid=uid or f"UID-{key}", provider_id=provider,
            person_id=person, course_code=course, completion_date=date,
            credits_claimed=credits,
            content_summary=summary or f"证明内容 {key}",
        )
        self.evidence[key] = result["evidence_id"]
        return result

    def reviewers(self, direct="d1", compliance="co1"):
        self.person(direct, name="直属审核人")
        self.person(compliance, name="独立合规人")
        return direct, compliance

    def application(self, *, applicant="p1", competence="GF", period_end="2026-09-24",
                    direct="d1", compliance="co1"):
        with self.engine.begin() as conn:
            detail = svc.create_application(
                conn, applicant_id=applicant, competence=competence,
                period_end=period_end, direct_reviewer_id=direct,
                compliance_reviewer_id=compliance,
            )
        return detail

    def approve_both(self, app_id, direct="d1", compliance="co1"):
        svc.submit_opinion(self.engine, application_id=app_id, reviewer_id=direct,
                           decision="approved", comment="专业范围符合",
                           expected_version=None)
        return svc.submit_opinion(self.engine, application_id=app_id,
                                  reviewer_id=compliance, decision="approved",
                                  comment="利益冲突已查", expected_version=None)

    def item_map(self, detail):
        return {item["evidence_id"]: item for item in detail["items"]}

    def equivalence(self, group_code, member_codes, *, title=None, effective_from="2000-01-01"):
        return svc.publish_equivalence_rule(
            self.engine, group_code=group_code, title=title or group_code,
            member_catalog_ids=[self.courses[c] for c in member_codes],
            effective_from=effective_from,
        )
