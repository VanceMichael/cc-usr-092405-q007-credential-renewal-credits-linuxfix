"""测试夹具：内存库 + 固定时钟。"""

from __future__ import annotations

import os

import pytest
from litestar.testing import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from skill_engine import create_app

NOW = "2026-09-24T09:00:00"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SKILL_ENGINE_NOW", NOW)
    engine = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    app = create_app(engine, start_worker=False)
    with TestClient(app) as test_client:
        test_client.engine = engine  # 便于测试直接查库
        yield test_client


def make_expert(client, code="EXP-1", region="CN-SH"):
    response = client.post("/admin/experts", json={"expert_code": code, "name": "专家", "home_region": region})
    assert response.status_code == 201, response.text
    return response.json()


def make_credential(client, expert_code="EXP-1", code="CRED-1", scopes=("green-finance",), credits=10.0, cycle_years=3):
    response = client.post(
        "/admin/credentials",
        json={
            "credential_code": code,
            "expert_code": expert_code,
            "required_scopes": list(scopes),
            "required_credits": credits,
            "cycle_years": cycle_years,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_provider(client, code="PRV-1"):
    response = client.post("/admin/providers", json={"provider_code": code, "name": "培训机构"})
    assert response.status_code == 201, response.text
    return response.json()


def make_course(
    client,
    provider_id,
    code="CRS-1",
    credits=5.0,
    scopes=("green-finance",),
    regions=("CN-SH",),
    valid_from="2020-01-01",
    valid_to=None,
    equivalence_group=None,
):
    response = client.post(
        "/admin/courses",
        json={
            "course_code": code,
            "provider_id": provider_id,
            "title": "课程",
            "credits": credits,
            "scopes": list(scopes),
            "valid_from": valid_from,
            "valid_to": valid_to,
            "regions": list(regions),
            "equivalence_group": equivalence_group,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def submit_proof(client, provider_id, expert_id, course_id, key, completed_at="2026-01-10", payload=None):
    response = client.post(
        "/proofs",
        json={
            "proof_key": key,
            "provider_id": provider_id,
            "expert_id": expert_id,
            "course_id": course_id,
            "completed_at": completed_at,
            "payload": payload if payload is not None else {"cert": key},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def tick(client):
    response = client.post("/ops/jobs/tick")
    assert response.status_code == 201, response.text
    return response.json()["processed"]
