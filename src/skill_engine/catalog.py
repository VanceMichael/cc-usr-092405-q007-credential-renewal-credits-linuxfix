"""课程目录与主数据服务。

课程目录保存能力范围、有效期、地区适用、等效组与周期上限；
等效组规则按版本管理，换版只影响尚未生效的申请。
"""

from __future__ import annotations

import json

from sqlalchemy import Connection, select

from . import jobs
from .models import (
    courses,
    equivalence_group_versions,
    period_caps,
    providers,
    reviewer_restrictions,
    reviewers,
)
from .timeutil import iso_now, to_date, today


class DomainError(Exception):
    """业务规则冲突，映射为 HTTP 409。"""

    def __init__(self, detail: str, status_code: int = 409) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class NotFoundError(DomainError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail, status_code=404)


def _loads(raw: str | None) -> list:
    return json.loads(raw) if raw else []


def row_to_dict(row, json_fields: tuple[str, ...] = ()) -> dict:
    data = dict(row._mapping)
    for field in json_fields:
        data[field] = _loads(data.get(field))
    return data


# ---------------------------------------------------------------- 提供方

def create_provider(conn: Connection, provider_code: str, name: str) -> dict:
    existing = conn.execute(select(providers).where(providers.c.provider_code == provider_code)).first()
    if existing:
        raise DomainError(f"提供方已存在: {provider_code}")
    result = conn.execute(
        providers.insert().values(provider_code=provider_code, name=name, status="active", created_at=iso_now())
    )
    return get_provider(conn, int(result.inserted_primary_key[0]))


def get_provider(conn: Connection, provider_id: int) -> dict:
    row = conn.execute(select(providers).where(providers.c.id == provider_id)).first()
    if not row:
        raise NotFoundError(f"提供方不存在: {provider_id}")
    return row_to_dict(row)


def revoke_provider(conn: Connection, provider_id: int) -> dict:
    """撤销提供方资格：只触发在审申请重评，生效续期追加风险标注。"""
    provider = get_provider(conn, provider_id)
    if provider["status"] == "revoked":
        return provider
    conn.execute(
        providers.update().where(providers.c.id == provider_id).values(status="revoked", revoked_at=iso_now())
    )
    jobs.enqueue(conn, "provider_revoked", {"provider_id": provider_id})
    return get_provider(conn, provider_id)


# ---------------------------------------------------------------- 审核人

def create_reviewer(conn: Connection, reviewer_code: str, name: str, role: str) -> dict:
    if role not in ("supervisor", "compliance"):
        raise DomainError("审核人类型必须是 supervisor 或 compliance", status_code=422)
    existing = conn.execute(select(reviewers).where(reviewers.c.reviewer_code == reviewer_code)).first()
    if existing:
        raise DomainError(f"审核人已存在: {reviewer_code}")
    result = conn.execute(
        reviewers.insert().values(reviewer_code=reviewer_code, name=name, role=role, created_at=iso_now())
    )
    return get_reviewer(conn, int(result.inserted_primary_key[0]))


def get_reviewer(conn: Connection, reviewer_id: int) -> dict:
    row = conn.execute(select(reviewers).where(reviewers.c.id == reviewer_id)).first()
    if not row:
        raise NotFoundError(f"审核人不存在: {reviewer_id}")
    return row_to_dict(row)


def add_restriction(conn: Connection, reviewer_id: int, provider_id: int, reason: str = "") -> dict:
    get_reviewer(conn, reviewer_id)
    get_provider(conn, provider_id)
    result = conn.execute(
        reviewer_restrictions.insert().values(
            reviewer_id=reviewer_id, provider_id=provider_id, reason=reason, created_at=iso_now()
        )
    )
    row = conn.execute(
        select(reviewer_restrictions).where(reviewer_restrictions.c.id == result.inserted_primary_key[0])
    ).first()
    return row_to_dict(row)


def restricted_provider_ids(conn: Connection, reviewer_id: int) -> set[int]:
    rows = conn.execute(
        select(reviewer_restrictions.c.provider_id).where(reviewer_restrictions.c.reviewer_id == reviewer_id)
    ).all()
    return {row.provider_id for row in rows}


# ---------------------------------------------------------------- 课程目录

def create_course(
    conn: Connection,
    *,
    course_code: str,
    provider_id: int,
    title: str,
    credits: float,
    scopes: list[str],
    valid_from: str,
    valid_to: str | None,
    regions: list[str],
    equivalence_group: str | None,
) -> dict:
    get_provider(conn, provider_id)
    if credits <= 0:
        raise DomainError("课程学分必须为正数", status_code=422)
    if valid_to and to_date(valid_to) < to_date(valid_from):
        raise DomainError("课程有效期结束早于开始", status_code=422)
    existing = conn.execute(select(courses).where(courses.c.course_code == course_code)).first()
    if existing:
        raise DomainError(f"课程已存在: {course_code}")
    result = conn.execute(
        courses.insert().values(
            course_code=course_code,
            provider_id=provider_id,
            title=title,
            credits=credits,
            scopes=json.dumps(scopes, ensure_ascii=False),
            valid_from=valid_from,
            valid_to=valid_to,
            regions=json.dumps(regions, ensure_ascii=False),
            equivalence_group=equivalence_group,
            created_at=iso_now(),
        )
    )
    return get_course(conn, int(result.inserted_primary_key[0]))


def get_course(conn: Connection, course_id: int) -> dict:
    row = conn.execute(select(courses).where(courses.c.id == course_id)).first()
    if not row:
        raise NotFoundError(f"课程不存在: {course_id}")
    return row_to_dict(row, json_fields=("scopes", "regions"))


def set_period_cap(conn: Connection, cap_key: str, cap_type: str, max_credits: float) -> dict:
    if cap_type not in ("scope", "equivalence_group"):
        raise DomainError("上限类型必须是 scope 或 equivalence_group", status_code=422)
    if max_credits <= 0:
        raise DomainError("上限学分必须为正数", status_code=422)
    existing = conn.execute(select(period_caps).where(period_caps.c.cap_key == cap_key)).first()
    if existing:
        conn.execute(
            period_caps.update().where(period_caps.c.cap_key == cap_key).values(cap_type=cap_type, max_credits=max_credits)
        )
    else:
        conn.execute(
            period_caps.insert().values(
                cap_key=cap_key, cap_type=cap_type, max_credits=max_credits, created_at=iso_now()
            )
        )
    row = conn.execute(select(period_caps).where(period_caps.c.cap_key == cap_key)).first()
    return row_to_dict(row)


def load_caps(conn: Connection) -> dict[str, dict]:
    rows = conn.execute(select(period_caps)).all()
    return {row.cap_key: row_to_dict(row) for row in rows}


# ---------------------------------------------------------------- 等效组版本

def publish_equivalence_version(
    conn: Connection, *, group_code: str, regions: list[str], effective_from: str
) -> dict:
    """发布等效组新版本。生效日期在未来时入队激活，到期只重评在审申请。"""
    rows = conn.execute(
        select(equivalence_group_versions)
        .where(equivalence_group_versions.c.group_code == group_code)
        .order_by(equivalence_group_versions.c.version.desc())
    ).all()
    version = (rows[0].version + 1) if rows else 1
    result = conn.execute(
        equivalence_group_versions.insert().values(
            group_code=group_code,
            version=version,
            regions=json.dumps(regions, ensure_ascii=False),
            effective_from=effective_from,
            created_at=iso_now(),
        )
    )
    version_id = int(result.inserted_primary_key[0])
    if to_date(effective_from) > today():
        # 未来生效：入队到期激活，系统中断后仍会执行
        jobs.enqueue(
            conn,
            "equivalence_version_activation",
            {"group_code": group_code, "version_id": version_id},
            run_at=f"{effective_from}T00:00:00",
        )
    else:
        jobs.enqueue(conn, "equivalence_version_activation", {"group_code": group_code, "version_id": version_id})
    return get_equivalence_version(conn, version_id)


def get_equivalence_version(conn: Connection, version_id: int) -> dict:
    row = conn.execute(
        select(equivalence_group_versions).where(equivalence_group_versions.c.id == version_id)
    ).first()
    if not row:
        raise NotFoundError(f"等效版本不存在: {version_id}")
    return row_to_dict(row, json_fields=("regions",))


def active_equivalence_versions(conn: Connection, on_date=None) -> dict[str, dict]:
    """每个等效组在指定日期生效的最新版本。"""
    day = on_date or today()
    rows = conn.execute(select(equivalence_group_versions)).all()
    active: dict[str, dict] = {}
    for row in rows:
        if to_date(row.effective_from) > day:
            continue
        current = active.get(row.group_code)
        if current is None or row.version > current["version"]:
            active[row.group_code] = row_to_dict(row, json_fields=("regions",))
    return active
