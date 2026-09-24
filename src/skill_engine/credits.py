"""滚动周期学分计算引擎。

对凭证要求的能力范围，在滚动窗口内逐项评估专家的每份证明，
给出 counted / capped / excluded 结论与完整判定轨迹（trace），
覆盖重复、封顶、过期、范围不符、地区适用、提供方资格与证明状态。
"""

from __future__ import annotations

from sqlalchemy import Connection, select

from .catalog import NotFoundError, active_equivalence_versions, load_caps, row_to_dict
from .models import courses, credentials, experts, proofs, providers
from .timeutil import minus_years, to_date


def load_credential(conn: Connection, credential_id: int) -> dict:
    row = conn.execute(select(credentials).where(credentials.c.id == credential_id)).first()
    if not row:
        raise NotFoundError(f"凭证不存在: {credential_id}")
    return row_to_dict(row, json_fields=("required_scopes",))


def compute_window(credential: dict, window_end):
    """滚动窗口：[window_end - cycle_years, window_end]。"""
    return minus_years(window_end, int(credential["cycle_years"])), window_end


def evaluate_credits(conn: Connection, *, credential_id: int, window_end) -> dict:
    """逐项评估并返回计算结果（不落库，调用方决定如何固化快照）。"""
    credential = load_credential(conn, credential_id)
    expert_row = conn.execute(select(experts).where(experts.c.id == credential["expert_id"])).first()
    expert = row_to_dict(expert_row)
    window_start, window_end = compute_window(credential, window_end)
    required_scopes = set(credential["required_scopes"])
    caps = load_caps(conn)
    equivalence = active_equivalence_versions(conn, on_date=window_end)

    proof_rows = conn.execute(
        select(proofs).where(proofs.c.expert_id == credential["expert_id"]).order_by(proofs.c.completed_at, proofs.c.id)
    ).all()

    items: list[dict] = []
    for proof_row in proof_rows:
        proof = row_to_dict(proof_row)
        course = row_to_dict(
            conn.execute(select(courses).where(courses.c.id == proof["course_id"])).first(),
            json_fields=("scopes", "regions"),
        )
        provider = row_to_dict(conn.execute(select(providers).where(providers.c.id == proof["provider_id"])).first())
        items.append(_build_item(proof, course, provider))

    # 第一遍：逐项硬排除（证明状态 / 提供方资格 / 窗口与有效期 / 范围 / 地区）
    for item in items:
        _hard_checks(item, required_scopes, equivalence, expert, window_start, window_end)

    # 第二遍：多机构重复证明——同一内容摘要只计一次，取最早提交
    seen_hashes: dict[str, dict] = {}
    for item in items:
        if item["decision"] == "excluded":
            continue
        keeper = seen_hashes.get(item["content_hash"])
        if keeper is None:
            seen_hashes[item["content_hash"]] = item
            item["trace"].append({"check": "duplicate", "result": "first_seen", "detail": "该内容摘要首次出现，计入"})
        else:
            item["decision"] = "excluded"
            item["reasons"].append("duplicate")
            item["credits_counted"] = 0.0
            item["trace"].append(
                {
                    "check": "duplicate",
                    "result": "excluded",
                    "detail": f"与证明 #{keeper['proof_id']} 内容摘要相同（多机构重复证明），不重复计入",
                }
            )

    # 第三遍：周期上限（能力范围与等效组），按完成日期先后消耗额度
    scope_used: dict[str, float] = {}
    group_used: dict[str, float] = {}
    for item in items:
        if item["decision"] == "excluded":
            continue
        counted = item["credits_counted"]
        for scope in item["course_scopes"]:
            cap = caps.get(f"scope:{scope}")
            if cap:
                allowed = max(0.0, cap["max_credits"] - scope_used.get(scope, 0.0))
                counted = min(counted, allowed)
                item["trace"].append(
                    {
                        "check": "scope_cap",
                        "result": "applied",
                        "detail": f"能力范围 {scope} 周期上限 {cap['max_credits']}，已用 {scope_used.get(scope, 0.0)}",
                    }
                )
        group = item["equivalence_group"]
        if group:
            cap = caps.get(f"equivalence_group:{group}")
            if cap:
                allowed = max(0.0, cap["max_credits"] - group_used.get(group, 0.0))
                counted = min(counted, allowed)
                item["trace"].append(
                    {
                        "check": "group_cap",
                        "result": "applied",
                        "detail": f"等效组 {group} 周期上限 {cap['max_credits']}，已用 {group_used.get(group, 0.0)}",
                    }
                )
        if counted < item["credits_counted"]:
            item["decision"] = "capped" if counted > 0 else "excluded"
            item["reasons"].append("cap")
            item["trace"].append(
                {
                    "check": "cap",
                    "result": item["decision"],
                    "detail": f"周期上限封顶：{item['credits_counted']} -> {counted}",
                }
            )
            item["credits_counted"] = counted
        for scope in item["course_scopes"]:
            scope_used[scope] = scope_used.get(scope, 0.0) + counted
        if group:
            group_used[group] = group_used.get(group, 0.0) + counted

    total = sum(item["credits_counted"] for item in items)
    return {
        "credential_id": credential_id,
        "expert_id": credential["expert_id"],
        "window_start": window_start,
        "window_end": window_end,
        "required_credits": credential["required_credits"],
        "total_counted": total,
        "satisfied": total >= credential["required_credits"],
        "items": items,
    }


def _build_item(proof: dict, course: dict, provider: dict) -> dict:
    return {
        "proof_id": proof["id"],
        "proof_key": proof["proof_key"],
        "proof_status": proof["status"],
        "content_hash": proof["content_hash"],
        "course_code": course["course_code"],
        "course_scopes": course["scopes"],
        "course_regions": course["regions"],
        "course_valid_from": course["valid_from"],
        "course_valid_to": course["valid_to"],
        "equivalence_group": course["equivalence_group"],
        "provider_code": provider["provider_code"],
        "provider_status": provider["status"],
        "completed_at": proof["completed_at"],
        "proof_credits": course["credits"],
        "credits_counted": course["credits"],
        "decision": "counted",
        "reasons": [],
        "trace": [],
    }


def _hard_checks(item: dict, required_scopes: set, equivalence: dict, expert: dict, window_start, window_end) -> None:
    """逐项硬排除；任一不通过即 excluded，轨迹记录每一步。"""

    def fail(reason: str, check: str, detail: str) -> None:
        item["decision"] = "excluded"
        item["credits_counted"] = 0.0
        item["reasons"].append(reason)
        item["trace"].append({"check": check, "result": "excluded", "detail": detail})

    if item["proof_status"] == "voided":
        return fail("proof_voided", "proof_status", "证明已作废，不计入")
    if item["proof_status"] == "suspended":
        return fail("proof_suspended", "proof_status", "证明存在同标识异文，核查期间暂停计入")
    item["trace"].append({"check": "proof_status", "result": "ok", "detail": "证明状态有效"})

    if item["provider_status"] == "revoked":
        return fail("provider_revoked", "provider_status", "课程提供方资格已撤销，不计入")
    item["trace"].append({"check": "provider_status", "result": "ok", "detail": "提供方资格有效"})

    completed = to_date(item["completed_at"])
    if completed < window_start or completed > window_end:
        return fail(
            "expired",
            "window",
            f"完成日期 {item['completed_at']} 不在滚动周期 {window_start} ~ {window_end} 内",
        )
    valid_from = to_date(item["course_valid_from"])
    valid_to = to_date(item["course_valid_to"]) if item["course_valid_to"] else None
    if completed < valid_from or (valid_to and completed > valid_to):
        return fail("expired", "course_validity", f"完成日期 {item['completed_at']} 超出课程有效期")
    item["trace"].append({"check": "window", "result": "ok", "detail": "完成日期在滚动周期与课程有效期内"})

    matched = sorted(set(item["course_scopes"]) & required_scopes)
    if not matched:
        return fail(
            "scope",
            "scope",
            f"课程范围 {item['course_scopes']} 与凭证要求 {sorted(required_scopes)} 无交集",
        )
    item["trace"].append({"check": "scope", "result": "ok", "detail": f"命中能力范围 {matched}"})

    home = expert["home_region"]
    if home in item["course_regions"]:
        item["trace"].append({"check": "region", "result": "ok", "detail": f"课程直接适用于地区 {home}"})
        return
    group = item["equivalence_group"]
    version = equivalence.get(group) if group else None
    if version and home in version["regions"]:
        item["trace"].append(
            {
                "check": "region",
                "result": "ok",
                "detail": f"经等效组 {group} v{version['version']} 跨地区互认为 {home}",
            }
        )
        return
    return fail(
        "region",
        "region",
        f"课程适用地区 {item['course_regions']} 不含 {home}，且无生效的等效规则",
    )
