"""滚动周期学分裁决引擎（纯函数，无 IO）。

逐项裁决类别：
- accepted                 采用（可能因周期上限只部分计入）
- duplicate                重复（内容指纹 / 同一课程 / 等效组，含多机构重复证明）
- capped                   周期上限已用尽
- expired                  完成日期在滚动窗口外，或课程在完成之日不在有效期
- out_of_scope             能力范围不符、地区不适用、课程不在目录
- under_review             同标识异文等待核查
- void                     证明已作废
- provider_revoked         提供方资格已撤销
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

EPS = 1e-9


# ── 证明指纹 ──────────────────────────────────────────────────

def normalize_summary(summary: str) -> str:
    return " ".join(summary.split())


def content_hash(*, person_id: str, course_code: str, completion_date: str,
                 credits_claimed: float, content_summary: str) -> str:
    """相同证明的内容指纹。刻意不含提供方：跨机构再次提交同一证明亦应命中。"""
    payload = json.dumps(
        {
            "person_id": person_id,
            "course_code": course_code.strip().upper(),
            "completion_date": completion_date,
            "credits_claimed": round(float(credits_claimed), 2),
            "content_summary": normalize_summary(content_summary),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── 输入/输出结构 ─────────────────────────────────────────────

@dataclass
class EvidenceCandidate:
    evidence_id: str
    evidence_uid: str
    provider_id: str
    provider_name: str
    provider_status: str  # active | revoked
    course_code: str
    completion_date: str
    credits: float
    evidence_status: str  # recorded | under_review | void
    recorded_order: int  # 首次登记顺序，用于同日确定性排序
    content_hash: str
    duplicate_of_id: str | None = None  # 相同证明再次提交时指向原记录


@dataclass
class CycleContext:
    competence: str
    region: str
    period_start: str
    period_end: str
    required_credits: float
    per_cycle_cap: float | None
    # course_code -> 该课程全部不可变目录版本
    catalog_rows: dict[str, list[dict[str, Any]]]
    # 当前（或快照固定）的等效规则：每组 (rule_id, group_code, 成员目录版本 id 集合)
    equivalence_groups: list[tuple[str, str, set[str]]]


@dataclass
class ItemResult:
    evidence_id: str
    evidence_uid: str
    provider_id: str
    provider_name: str
    provider_status: str
    course_code: str
    completion_date: str
    evidence_status: str
    content_hash: str
    course_catalog_id: str | None = None
    course_version: int | None = None
    course_title: str | None = None
    competence_scopes: list[str] = field(default_factory=list)
    applicable_regions: list[str] = field(default_factory=list)
    equivalence_group: str | None = None
    equivalence_rule_id: str | None = None
    credits: float = 0.0
    credits_applied: float = 0.0
    decision: str = "excluded"  # included | excluded
    category: str | None = None  # 见模块文档；included 且部分封顶时为 capped
    reason: str = ""

    @property
    def included(self) -> bool:
        return self.decision == "included"


@dataclass
class CycleResult:
    items: list[ItemResult]
    credits_accepted: float
    required_credits: float
    per_cycle_cap: float | None
    required_met: bool


# ── 课程目录解析 ──────────────────────────────────────────────

def resolve_course(course_code: str, completion_date: str,
                   catalog_rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    """找到完成之日处于有效期的目录版本；多个命中时取最新版本。"""
    candidates = []
    for row in catalog_rows.get(course_code, []):
        if row["valid_from"] > completion_date:
            continue
        if row["valid_until"] and completion_date > row["valid_until"]:
            continue
        candidates.append(row)
    if not candidates:
        return None
    return max(candidates, key=lambda r: r["version"])


# ── 裁决主流程 ────────────────────────────────────────────────

def evaluate_cycle(ctx: CycleContext, evidences: Iterable[EvidenceCandidate]) -> CycleResult:
    ordered = sorted(evidences, key=lambda e: (e.completion_date, e.recorded_order, e.evidence_id))

    # 目录版本 id -> (等效规则 id, 组代码)
    catalog_to_group: dict[str, tuple[str, str]] = {}
    for rule_id, group_code, member_ids in ctx.equivalence_groups:
        for catalog_id in member_ids:
            catalog_to_group[catalog_id] = (rule_id, group_code)

    items: list[ItemResult] = []
    seen_hashes: dict[str, str] = {}          # content_hash -> 已采用证据 id
    seen_courses: dict[str, str] = {}         # course_code -> 已采用证据 id
    seen_groups: dict[str, str] = {}          # group_code -> 已采用证据 id
    accepted_total = 0.0

    for ev in ordered:
        item = ItemResult(
            evidence_id=ev.evidence_id,
            evidence_uid=ev.evidence_uid,
            provider_id=ev.provider_id,
            provider_name=ev.provider_name,
            provider_status=ev.provider_status,
            course_code=ev.course_code,
            completion_date=ev.completion_date,
            evidence_status=ev.evidence_status,
            content_hash=ev.content_hash,
            credits=round(float(ev.credits), 2),
        )

        # 0) 相同证明再次提交（含多机构）：沿用原记录，不重复计分
        if ev.duplicate_of_id is not None:
            item.category = "duplicate"
            item.reason = (
                f"与原记录 {ev.duplicate_of_id} 内容完全一致的重复提交"
                f"（提交提供方 {ev.provider_name}），学分沿用原记录，本条不重复计入。"
            )
            items.append(item)
            continue

        # 1) 证明作废
        if ev.evidence_status == "void":
            item.category = "void"
            item.reason = "完成证明已作废，不得计入。"
            items.append(item)
            continue

        # 2) 同标识异文，核查未决
        if ev.evidence_status == "under_review":
            item.category = "under_review"
            item.reason = "同一证明标识出现不同内容，暂停计入并进入人工核查。"
            items.append(item)
            continue

        # 3) 提供方资格撤销
        if ev.provider_status == "revoked":
            item.category = "provider_revoked"
            item.reason = f"课程提供方 {ev.provider_name}（{ev.provider_id}）资格已撤销，其证明不予计入。"
            items.append(item)
            continue

        # 4) 目录解析（课程在完成之日是否处于有效期）
        course = resolve_course(ev.course_code, ev.completion_date, ctx.catalog_rows)
        if course is None:
            rows = ctx.catalog_rows.get(ev.course_code, [])
            if not rows:
                item.category = "out_of_scope"
                item.reason = f"课程 {ev.course_code} 不在能力课程目录中，范围不符。"
            else:
                latest = max(rows, key=lambda r: r["version"])
                item.category = "expired"
                if latest["valid_until"] and ev.completion_date > latest["valid_until"]:
                    item.reason = (
                        f"课程 {ev.course_code} 有效期已于 {latest['valid_until']} 届满，"
                        f"完成日期 {ev.completion_date} 在有效期之后。"
                    )
                else:
                    item.reason = (
                        f"课程 {ev.course_code} 自 {latest['valid_from']} 起生效，"
                        f"完成日期 {ev.completion_date} 早于课程生效日。"
                    )
            items.append(item)
            continue

        item.course_catalog_id = course["id"]
        item.course_version = course["version"]
        item.course_title = course["title"]
        item.competence_scopes = list(course["competence_scopes"])
        item.applicable_regions = list(course["applicable_regions"])
        # 学分以课程目录的权威登记为准，提供方申报值仅存于证明
        item.credits = round(float(course["credits"]), 2)
        group_info = catalog_to_group.get(course["id"])
        group_code = group_info[1] if group_info else None
        item.equivalence_group = group_code
        item.equivalence_rule_id = group_info[0] if group_info else None

        # 5) 能力范围
        if ctx.competence not in course["competence_scopes"]:
            item.category = "out_of_scope"
            item.reason = (
                f"课程 {ev.course_code} 的能力范围为 {', '.join(course['competence_scopes']) or '（空）'}，"
                f"不覆盖本次审议能力 {ctx.competence}。"
            )
            items.append(item)
            continue

        # 6) 地区适用（空列表表示全境适用）
        if course["applicable_regions"] and ctx.region not in course["applicable_regions"]:
            item.category = "out_of_scope"
            item.reason = (
                f"课程 {ev.course_code} 仅适用于 {', '.join(course['applicable_regions'])}，"
                f"申请人所在地区 {ctx.region} 不适用。"
            )
            items.append(item)
            continue

        # 7) 滚动周期窗口
        if ev.completion_date < ctx.period_start or ev.completion_date > ctx.period_end:
            item.category = "expired"
            item.reason = (
                f"完成日期 {ev.completion_date} 落在滚动周期 "
                f"{ctx.period_start} 至 {ctx.period_end} 之外。"
            )
            items.append(item)
            continue

        # 8) 重复：内容指纹（含跨机构）→ 同一课程 → 等效组
        if ev.content_hash in seen_hashes:
            item.category = "duplicate"
            item.reason = (
                f"相同证明已由记录 {seen_hashes[ev.content_hash]} 采用"
                f"（内容指纹一致，疑似多机构重复证明），本条沿用原记录，不重复计入。"
            )
            items.append(item)
            continue
        if ev.course_code in seen_courses:
            item.category = "duplicate"
            item.reason = (
                f"同一课程 {ev.course_code} 已有记录 {seen_courses[ev.course_code]} 计入，"
                "重复完成不重复计分。"
            )
            items.append(item)
            continue
        if group_code is not None and group_code in seen_groups:
            item.category = "duplicate"
            first = seen_groups[group_code]
            item.reason = (
                f"课程属于等效组 {group_code}，等效课程记录 {first} 已计入，"
                "跨地区/跨提供方等效课程在同一周期只计一次。"
            )
            items.append(item)
            continue

        # 9) 周期上限
        cap = ctx.per_cycle_cap
        if cap is not None and accepted_total >= cap - EPS:
            item.category = "capped"
            item.reason = f"周期上限 {cap:g} 学分已用尽，本条 {item.credits:g} 学分不再计入。"
            items.append(item)
            continue

        item.decision = "included"
        if cap is not None and accepted_total + item.credits > cap + EPS:
            applied = round(cap - accepted_total, 2)
            item.credits_applied = max(applied, 0.0)
            item.category = "capped"
            item.reason = (
                f"部分计入 {item.credits_applied:g} 学分；受周期上限 {cap:g} 限制，"
                f"超出 {round(item.credits - item.credits_applied, 2):g} 学分未计入。"
            )
        else:
            item.credits_applied = item.credits

        accepted_total = round(accepted_total + item.credits_applied, 2)
        seen_hashes[ev.content_hash] = ev.evidence_id
        seen_courses[ev.course_code] = ev.evidence_id
        if group_code is not None:
            seen_groups[group_code] = ev.evidence_id
        items.append(item)

    return CycleResult(
        items=items,
        credits_accepted=accepted_total,
        required_credits=ctx.required_credits,
        per_cycle_cap=ctx.per_cycle_cap,
        required_met=accepted_total + EPS >= ctx.required_credits,
    )


def rolling_window(period_end: str, period_years: int) -> tuple[str, str]:
    end = date.fromisoformat(period_end)
    try:
        start = end.replace(year=end.year - period_years)
    except ValueError:  # 2 月 29 日
        start = end.replace(year=end.year - period_years, day=28)
    return start.isoformat(), end.isoformat()
