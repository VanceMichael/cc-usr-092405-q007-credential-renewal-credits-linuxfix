"""时间与日期工具。

支持通过环境变量 ``SKILL_ENGINE_NOW`` 固定当前时间（ISO 格式），
便于测试与演练；未设置时使用真实 UTC 时间。
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

_DATE_FMT = "%Y-%m-%d"


def now() -> datetime:
    """当前 UTC 时间（naive，便于与库存储的字符串比较）。"""
    override = os.getenv("SKILL_ENGINE_NOW")
    if override:
        return datetime.fromisoformat(override).replace(tzinfo=None)
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today() -> date:
    return now().date()


def iso_now() -> str:
    return now().isoformat(timespec="seconds")


def to_date(value: str) -> date:
    return datetime.strptime(value, _DATE_FMT).date()


def to_iso_day(value: date) -> str:
    return value.strftime(_DATE_FMT)


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def add_days(day: date, days: int) -> date:
    return day + timedelta(days=days)


def minus_years(day: date, years: int) -> date:
    """滚动窗口起点：结束日向前推 N 年。"""
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 2 月 29 日
        return day.replace(year=day.year - years, day=28)
