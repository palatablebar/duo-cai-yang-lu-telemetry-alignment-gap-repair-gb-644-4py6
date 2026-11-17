"""回拨时段判读：把每条采样判成第几次经过该本地时刻，并产出可追溯时间轴。

判读依据是采样原始写法自带的 UTC 偏移（`+02:00` / `+01:00` / `Z`），不依赖时区
数据库：落在回拨窗口内的「书写时刻」，写夏令时偏移即第一次（occurrence=1），写
冬令时偏移即第二次（occurrence=2）。窗口内却没有任何显式偏移的本地时刻无法区分
先后，判 `DST_NAIVE_FOLD` 并隔离，避免静默归并出双倍覆盖或缺口。

窗口之外的采样 occurrence=0，本地墙钟按其书写偏移还原；窗口内的本地墙钟按所属
那次的偏移还原，因此两段「02:15」在时间轴上分别显示为 02:15(+02:00) 与
02:15(+01:00)，但对应不同的 UTC 分钟桶，天然不碰撞。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .transition import FoldWindow


@dataclass(frozen=True)
class FoldVerdict:
    in_fold: bool
    occurrence: int          # 0=窗口外；1=第一次（夏令时）；2=第二次（冬令时）
    local_offset: timedelta  # 还原本地墙钟所用的偏移
    naive: bool              # 窗口内但原始写法无显式偏移


def explicit_offset_seconds(raw_text: str) -> int | None:
    """从 ISO8601 原文提取书写偏移的秒数；`Z` 给 0，裸本地时间给 None。"""
    raw = (raw_text or "").strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        return 0
    sign_pos = max(raw.rfind("+"), raw.rfind("-"))
    if sign_pos <= 0:
        return None
    fragment = raw[sign_pos:]
    hours_part, _, minutes_part = fragment[1:].partition(":")
    if not hours_part.isdigit():
        return None
    hours = int(hours_part)
    minutes = int(minutes_part) if minutes_part and minutes_part.isdigit() else 0
    total = hours * 3600 + minutes * 60
    return total if fragment.startswith("+") else -total


def judge(
    window: FoldWindow | None,
    *,
    raw_instant: datetime,
    written_offset: timedelta | None,
) -> FoldVerdict:
    """raw_instant 是设备书写时刻解析出的 UTC（未减设备漂移）；written_offset 是其书写偏移。

    回拨是「设备写了哪一个本地时刻、带哪段区时」的性质。本地墙钟 = 书写 UTC + 书写
    偏移，先判断该墙钟是否落在回拨重复本地区间 `[local_fold_start, local_fold_end)`，
    再用书写偏移区分第一次（夏令时 +pre）与第二次（冬令时 +post）。设备漂移只影响
    最终落入哪个 UTC 分钟桶，不参与 DST 归属。
    """
    if window is None:
        offset = written_offset if written_offset is not None else timedelta(0)
        return FoldVerdict(False, 0, offset, False)

    offset = written_offset if written_offset is not None else timedelta(0)
    local_instant = raw_instant + offset
    if not window.contains_local(local_instant):
        return FoldVerdict(False, 0, offset, False)

    if written_offset is None:
        return FoldVerdict(True, 0, window.post_offset, True)
    if written_offset == window.pre_offset:
        return FoldVerdict(True, 1, window.pre_offset, False)
    if written_offset == window.post_offset:
        return FoldVerdict(True, 2, window.post_offset, False)
    # 窗口内写了第三种偏移：无法归入任一次，按不可信隔离。
    return FoldVerdict(True, 0, window.post_offset, True)


def local_clock(corrected_utc: datetime, offset: timedelta) -> str:
    return (corrected_utc + offset).strftime("%Y-%m-%d %H:%M")
