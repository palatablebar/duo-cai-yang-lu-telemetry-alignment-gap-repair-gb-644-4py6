"""DST 回拨表：描述监测站本地时区的秋季回拨（fold）窗口。

与设备偏移表分离：偏移表管「设备时钟漂移」，本表管「本地时区的夏令时切换」。
回拨窗口用 UTC 半开区间 `[fold_start_utc, fold_end_utc)` 表达，长度恰好是
`2 × (pre_offset - post_offset)`（欧洲回拨为 2 小时），对应两段各 1 小时的经过；
被重复的本地墙钟区间长度为 `pre_offset - post_offset`（如柏林本地 02:00–03:00）。
窗口内同一本地墙钟时刻出现两次：第一次按 `pre_offset`（夏令时）书写，第二次按
`post_offset`（冬令时）书写。

`sensor_id` 为 `*` 的条目对全站生效；具体传感器条目优先于全站条目。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.modules.m2_timebase import coerce

DST_COLUMNS = (
    "station",
    "sensor_id",
    "fold_start_utc",
    "fold_end_utc",
    "pre_offset_minutes",
    "post_offset_minutes",
)

ALIASES = {
    "站点": "station",
    "传感器": "sensor_id",
    "回拨起": "fold_start_utc",
    "回拨开始": "fold_start_utc",
    "回拨止": "fold_end_utc",
    "回拨结束": "fold_end_utc",
    "夏令时偏移分": "pre_offset_minutes",
    "前偏移分": "pre_offset_minutes",
    "冬令时偏移分": "post_offset_minutes",
    "后偏移分": "post_offset_minutes",
}

WILDCARD = "*"


@dataclass(frozen=True)
class FoldWindow:
    station: str
    sensor_id: str
    start: datetime
    end: datetime
    pre_offset: timedelta
    post_offset: timedelta

    @property
    def fold_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def pre_minutes(self) -> int:
        return int(self.pre_offset.total_seconds() // 60)

    @property
    def post_minutes(self) -> int:
        return int(self.post_offset.total_seconds() // 60)

    @property
    def repeat_minutes(self) -> int:
        """本地墙钟被重复的时长（欧洲回拨为 60 分钟）。"""
        return int(((self.end - self.start) / 2).total_seconds() // 60)

    @property
    def local_fold_start(self) -> datetime:
        """重复本地区间起点（墙钟），如柏林 02:00。"""
        return self.start + self.pre_offset

    @property
    def local_fold_end(self) -> datetime:
        """重复本地区间终点（墙钟），如柏林 03:00。"""
        return self.local_fold_start + timedelta(minutes=self.repeat_minutes)

    def contains_local(self, local_instant: datetime) -> bool:
        return self.local_fold_start <= local_instant < self.local_fold_end

    def describe(self) -> dict:
        return {
            "station": self.station,
            "sensor_id": self.sensor_id,
            "fold_start": coerce.minute_text(self.start),
            "fold_end": coerce.minute_text(self.end),
            "fold_minutes": self.fold_minutes,
            "pre_offset_minutes": self.pre_minutes,
            "post_offset_minutes": self.post_minutes,
            # 两次经过的本地起点是同一面墙钟（02:00），区别只在所带偏移。
            "local_first_start": self.local_fold_start.strftime("%Y-%m-%d %H:%M"),
            "local_second_start": self.local_fold_start.strftime("%Y-%m-%d %H:%M"),
            "local_fold_end": self.local_fold_end.strftime("%Y-%m-%d %H:%M"),
            "window": "[fold_start, fold_end)",
        }


class DstTableError(ValueError):
    """回拨表结构非法；调用方映射成输入错误。"""


def _canonical(name: str) -> str:
    key = (name or "").replace("﻿", "").strip()
    if key in DST_COLUMNS:
        return key
    return ALIASES.get(key, ALIASES.get(key.lower(), key))


def _minutes(text: str, field: str) -> timedelta:
    value = coerce.parse_seconds(text)
    if value is None:
        raise DstTableError(f"{field} 为空")
    return timedelta(minutes=int(value))


def parse_rows(text: str) -> list[FoldWindow]:
    reader = csv.reader(io.StringIO(text, newline=""))
    header: list[str] | None = None
    windows: list[FoldWindow] = []
    for record in reader:
        if not any((cell or "").strip() for cell in record):
            continue
        if header is None:
            header = [_canonical(cell) for cell in record]
            missing = [name for name in DST_COLUMNS if name not in header]
            if missing:
                raise DstTableError("回拨表缺列: " + ",".join(missing))
            continue
        cells = {header[index]: (record[index] or "").strip() for index in range(min(len(header), len(record)))}
        sensor_id = cells.get("sensor_id", "") or WILDCARD
        start = coerce.to_utc(cells.get("fold_start_utc", ""))
        end = coerce.to_utc(cells.get("fold_end_utc", ""))
        pre = _minutes(cells.get("pre_offset_minutes", ""), "pre_offset_minutes")
        post = _minutes(cells.get("post_offset_minutes", ""), "post_offset_minutes")
        if end <= start:
            raise DstTableError(f"回拨窗口倒置: {sensor_id}")
        if pre <= post:
            raise DstTableError(f"仅支持秋季回拨（前偏移须大于后偏移）: {sensor_id}")
        # 回拨的 UTC 跨度 = 两次经过的本地重复时长之和 = 2 × (pre - post)。
        if (end - start) != 2 * (pre - post):
            raise DstTableError(f"回拨窗口 UTC 跨度须等于两倍前后偏移之差: {sensor_id}")
        windows.append(
            FoldWindow(
                station=cells.get("station", "") or "station",
                sensor_id=sensor_id,
                start=start,
                end=end,
                pre_offset=pre,
                post_offset=post,
            )
        )
    return windows


def select_for(windows: list[FoldWindow], sensor_id: str) -> FoldWindow | None:
    """具体传感器条目优先；否则取全站 `*` 条目；都没有返回 None。"""
    specific = [item for item in windows if item.sensor_id == sensor_id]
    if specific:
        return specific[0]
    wildcard = [item for item in windows if item.sensor_id == WILDCARD]
    return wildcard[0] if wildcard else None
