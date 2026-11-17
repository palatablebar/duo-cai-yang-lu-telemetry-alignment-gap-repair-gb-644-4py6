"""DST 编排：回拨表加载、时间轴构建、受影响窗口与缺口核对。

时间轴的每一行都能从 UTC 桶追回原始行号、本地墙钟、第几次经过与回拨标记；
正式结果（网格）、覆盖度（窗口）与导出（CSV/清单）共用同一份判读结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app import config, manifest, store
from app.errors import InputError, NotFoundError
from app.modules.m2_timebase import coerce

from . import fold, transition


@dataclass
class DstTable:
    key: str
    file_name: str
    sha256: str
    size: int
    windows: list[transition.FoldWindow] = field(default_factory=list)


def load_table(key: str) -> DstTable:
    if key not in config.DST_SAMPLES:
        raise InputError("未知回拨表", code="DST_TABLE_UNKNOWN", detail={"dst": key})
    path = config.dst_file(key)
    if not path.exists():
        raise NotFoundError("回拨表不存在", code="DST_TABLE_MISSING", detail={"path": str(path)})
    raw = path.read_bytes()
    table = DstTable(
        key=key,
        file_name=path.name,
        sha256=manifest.sha256_bytes(raw),
        size=len(raw),
    )
    from app.modules.m1_intake import decode

    decoded = decode.decode_bytes(raw)
    try:
        table.windows = transition.parse_rows(decoded.text)
    except transition.DstTableError as exc:
        raise InputError("回拨表无法解析", code="DST_TABLE_INVALID", detail={"reason": str(exc)}) from exc
    return table


def dst_choices() -> list[dict]:
    return [
        {"key": key, "label": label, "file": file_name}
        for key, (file_name, label) in config.DST_SAMPLES.items()
    ]


def store_dst(conn, run_id: str, table: DstTable) -> None:
    payload = [
        (
            run_id,
            item.station,
            item.sensor_id,
            coerce.minute_text(item.start),
            coerce.minute_text(item.end),
            item.pre_minutes,
            item.post_minutes,
        )
        for item in table.windows
    ]
    store.insert_many(
        conn,
        "INSERT INTO dst_windows(run_id, station, sensor_id, fold_start_utc, fold_end_utc,"
        " pre_offset_minutes, post_offset_minutes) VALUES (?,?,?,?,?,?,?)",
        payload,
    )


def dst_windows_of(conn, run_id: str) -> list[dict]:
    rows = store.rows(
        conn,
        "SELECT * FROM dst_windows WHERE run_id = ? ORDER BY sensor_id, fold_start_utc",
        (run_id,),
    )
    return [dict(row) for row in rows]


def timeline_view(conn, run_id: str) -> dict:
    """可追溯时间轴：网格点 + 回拨判读，按 UTC 时刻排序并标注本地先后。

    OBSERVED 点的回拨判读直接读采信阶段落库的权威结论（按书写时刻判读）；
    GAP/FILLED 点没有原始采样，才按回拨窗口的 UTC 几何位置推断所属次数。
    """
    grid_rows = store.rows(
        conn,
        "SELECT g.sensor_id, g.bucket_utc, g.value, g.unit, g.state, g.reason, g.source_row,"
        " g.in_fold, g.fold_occurrence, s.quality, s.flags, s.local_clock, s.local_offset_minutes"
        " FROM grid g LEFT JOIN samples s"
        " ON s.run_id = g.run_id AND s.sensor_id = g.sensor_id AND s.bucket_utc = g.bucket_utc"
        " WHERE g.run_id = ? ORDER BY g.bucket_utc, g.sensor_id",
        (run_id,),
    )
    dst_rows = dst_windows_of(conn, run_id)
    points: list[dict] = []
    fold_buckets = 0
    first_count = 0
    second_count = 0
    for row in grid_rows:
        bucket = coerce.to_utc(row["bucket_utc"])
        if row["in_fold"]:
            # 权威路径：采信时已按书写偏移判读；只统计实有采样桶。
            occurrence = row["fold_occurrence"]
            local_time = row["local_clock"] or fold.local_clock(
                bucket, timedelta(minutes=row["local_offset_minutes"] or 0)
            )
            local_offset = _offset_text(
                timedelta(minutes=row["local_offset_minutes"] or 0)
            )
            mark = {
                "in_fold": True,
                "occurrence": occurrence,
                "local_offset": local_offset,
                "local_time": local_time,
                "dst_reason": "DST_FOLD_OCCURRENCE",
            }
            fold_buckets += 1
            if occurrence == 1:
                first_count += 1
            elif occurrence == 2:
                second_count += 1
        else:
            # 几何路径：只为 GAP/FILLED 桶标注「本应属于第几次经过」，不计入权威回拨桶数。
            mark = _mark_for(dst_rows, row["sensor_id"], bucket)
        points.append(
            {
                "sensor_id": row["sensor_id"],
                "bucket_utc": row["bucket_utc"],
                "local_time": mark["local_time"],
                "local_offset": mark["local_offset"],
                "occurrence": mark["occurrence"],
                "in_fold": mark["in_fold"],
                "dst_reason": mark["dst_reason"],
                "value": row["value"],
                "unit": row["unit"],
                "state": row["state"],
                "reason": row["reason"],
                "source_row": row["source_row"],
                "quality": row["quality"],
                "flags": row["flags"] or "",
            }
        )
    return {
        "run_id": run_id,
        "dst_windows": dst_rows,
        "points": points,
        "summary": {
            "fold_windows": len(dst_rows),
            "fold_buckets": fold_buckets,
            "first_occurrence": first_count,
            "second_occurrence": second_count,
        },
    }


def _mark_for(dst_rows: list[dict], sensor_id: str, bucket: datetime) -> dict:
    """几何推断（仅用于无原始采样的 GAP/FILLED 桶）。

    回拨 UTC 跨度前半段对应第一次经过（夏令时 +pre），后半段对应第二次（冬令时
    +post）；跨度之外按桶与窗口的相对位置取常规偏移展示。
    """
    chosen = None
    for row in dst_rows:
        if row["sensor_id"] == sensor_id:
            chosen = row
            break
    if chosen is None:
        for row in dst_rows:
            if row["sensor_id"] == transition.WILDCARD:
                chosen = row
                break
    if chosen is None:
        return {
            "in_fold": False,
            "occurrence": 0,
            "local_offset": "+00:00",
            "local_time": fold.local_clock(bucket, timedelta(0)),
            "dst_reason": "",
        }
    start = coerce.to_utc(chosen["fold_start_utc"])
    end = coerce.to_utc(chosen["fold_end_utc"])
    pre = timedelta(minutes=chosen["pre_offset_minutes"])
    post = timedelta(minutes=chosen["post_offset_minutes"])
    if not (start <= bucket < end):
        offset = pre if bucket < start else post
        return {
            "in_fold": False,
            "occurrence": 0,
            "local_offset": _offset_text(offset),
            "local_time": fold.local_clock(bucket, offset),
            "dst_reason": "",
        }
    midpoint = start + (end - start) / 2
    if bucket < midpoint:
        occurrence, offset = 1, pre
    else:
        occurrence, offset = 2, post
    return {
        "in_fold": True,
        "occurrence": occurrence,
        "local_offset": _offset_text(offset),
        "local_time": fold.local_clock(bucket, offset),
        "dst_reason": "DST_FOLD_OCCURRENCE",
    }


def _offset_text(offset: timedelta) -> str:
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def affected_view(conn, run_id: str) -> dict:
    """受回拨影响的 15 分钟窗口与缺口：结论直接读落库网格/窗口，不另算一套。"""
    dst_rows = dst_windows_of(conn, run_id)
    if not dst_rows:
        return {"run_id": run_id, "dst_windows": [], "windows": [], "gaps": [], "fold_range": None}

    window_rows = store.rows(
        conn,
        "SELECT * FROM windows WHERE run_id = ? ORDER BY sensor_id, window_start",
        (run_id,),
    )
    grid_rows = store.rows(
        conn,
        "SELECT * FROM grid WHERE run_id = ? ORDER BY sensor_id, bucket_utc",
        (run_id,),
    )
    affected_windows: list[dict] = []
    fold_min, fold_max = None, None
    for dst in dst_rows:
        start = coerce.to_utc(dst["fold_start_utc"])
        end = coerce.to_utc(dst["fold_end_utc"])
        fold_min = start if fold_min is None else min(fold_min, start)
        fold_max = end if fold_max is None else max(fold_max, end)
        for row in window_rows:
            w_start = coerce.to_utc(row["window_start"])
            w_end = w_start + timedelta(minutes=config.WINDOW_MINUTES)
            if w_start < end and w_end > start and (
                dst["sensor_id"] == transition.WILDCARD or row["sensor_id"] == dst["sensor_id"]
            ):
                affected_windows.append(
                    {
                        "sensor_id": row["sensor_id"],
                        "window_start": row["window_start"],
                        "observed": row["observed"],
                        "expected": row["expected"],
                        "coverage": row["coverage"],
                        "state": row["state"],
                    }
                )

    affected_gaps: list[dict] = []
    for dst in dst_rows:
        start = coerce.to_utc(dst["fold_start_utc"])
        end = coerce.to_utc(dst["fold_end_utc"])
        for row in grid_rows:
            bucket = coerce.to_utc(row["bucket_utc"])
            if row["state"] == "GAP" and start <= bucket < end and (
                dst["sensor_id"] == transition.WILDCARD or row["sensor_id"] == dst["sensor_id"]
            ):
                affected_gaps.append(
                    {
                        "sensor_id": row["sensor_id"],
                        "bucket_utc": row["bucket_utc"],
                        "reason": row["reason"],
                    }
                )
    return {
        "run_id": run_id,
        "dst_windows": dst_rows,
        "windows": affected_windows,
        "gaps": affected_gaps,
        "fold_range": None if fold_min is None else {
            "start": coerce.minute_text(fold_min),
            "end": coerce.minute_text(fold_max),
        },
    }
