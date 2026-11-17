"""M8 秋季回拨：两次经过的真实先后、受影响窗口与缺口、可追溯时间轴与对账一致性。

核心命题：本地墙钟 02:00–03:00 在回拨日出现两次，按书写偏移区分第一次（+02:00）与
第二次（+01:00），两次落到不同的 UTC 桶——既不能当新增采样造成双倍覆盖，也不能只
留一段造成缺口。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app import store
from app.errors import InputError
from app.modules.m6_batches import service as batch_service
from app.modules.m7_export import csv_out, evidence, service as export_service
from app.modules.m8_dst import fold, service as dst_service, transition

from expected import DST, DST_FIRST_BUCKET, DST_SECOND_BUCKET


# —— 纯函数：回拨表与判读规则 ——

def _window():
    return transition.FoldWindow(
        station="berlin",
        sensor_id="*",
        start=transition.coerce.to_utc("2026-10-25T00:00:00Z"),
        end=transition.coerce.to_utc("2026-10-25T02:00:00Z"),
        pre_offset=timedelta(hours=2),
        post_offset=timedelta(hours=1),
    )


def test_fold_local_interval_is_repeated_wall_clock():
    """回拨重复的是本地 02:00–03:00；UTC 跨度两小时，本地重复一小时。"""
    window = _window()
    assert window.local_fold_start.strftime("%H:%M") == "02:00"
    assert window.local_fold_end.strftime("%H:%M") == "03:00"
    assert window.repeat_minutes == 60
    assert window.fold_minutes == 120


def test_judge_distinguishes_two_occurrences():
    """同一本地时刻，+02:00 判第一次、+01:00 判第二次；窗口外为 0。"""
    window = _window()
    raw = transition.coerce.to_utc("2026-10-25T00:15:00Z")  # 本地 02:15(+2)
    first = fold.judge(window, raw_instant=raw, written_offset=timedelta(hours=2))
    second = fold.judge(
        window,
        raw_instant=transition.coerce.to_utc("2026-10-25T01:15:00Z"),
        written_offset=timedelta(hours=1),
    )
    assert (first.in_fold, first.occurrence, first.naive) == (True, 1, False)
    assert (second.in_fold, second.occurrence, second.naive) == (True, 2, False)

    outside = fold.judge(
        window,
        raw_instant=transition.coerce.to_utc("2026-10-24T23:58:00Z"),
        written_offset=timedelta(hours=2),
    )
    assert outside.in_fold is False and outside.occurrence == 0


def test_judge_naive_local_in_fold_is_unresolvable():
    """回拨段内的裸本地时间无法区分先后，判 naive，调用方必须隔离而非静默归并。"""
    window = _window()
    # 裸本地时间没有书写偏移：本地时刻即按 UTC 传入，02:30 落在重复区间 [02:00,03:00)。
    verdict = fold.judge(
        window,
        raw_instant=transition.coerce.to_utc("2026-10-25T02:30:00Z"),
        written_offset=None,
    )
    assert verdict.in_fold is True and verdict.naive is True


def test_explicit_offset_seconds_parses_forms():
    assert fold.explicit_offset_seconds("2026-10-25T02:15:00+02:00") == 7200
    assert fold.explicit_offset_seconds("2026-10-25T02:15:00+01:00") == 3600
    assert fold.explicit_offset_seconds("2026-10-25T00:00:00Z") == 0
    assert fold.explicit_offset_seconds("2026-10-25T02:30:00") is None


def test_table_rejects_bad_span():
    """UTC 跨度不等于两倍偏移差的回拨表直接拒绝，不接受静默错配。"""
    with pytest.raises(transition.DstTableError):
        transition.parse_rows(
            "station,sensor_id,fold_start_utc,fold_end_utc,pre_offset_minutes,post_offset_minutes\n"
            "x,*,2026-10-25T01:00:00Z,2026-10-25T02:00:00Z,120,60\n"
        )


# —— 端到端：采信、隔离、对账 ——

def test_dst_run_reconciles_five_accounts(conn, run_dst):
    """五账全平，回拨账把回拨桶拆成第一次 + 第二次。"""
    view = batch_service.reconcile_view(conn, run_dst["run_id"])
    assert view["balanced"] is True
    names = [line["name"] for line in view["lines"]]
    assert names == ["行账", "网格账", "数值账", "窗口账", "回拨账"]
    fold_line = next(line for line in view["lines"] if line["name"] == "回拨账")
    assert int(fold_line["expected"]) == DST["fold_buckets"]
    assert int(fold_line["actual"]) == DST["fold_first"] + DST["fold_second"]
    assert fold_line["delta"] == "0"


def test_naive_fold_row_is_quarantined_not_dropped(conn, run_dst):
    """裸本地时间行进隔离且原因码正确，行账仍守恒，记录一条不丢。"""
    counts = run_dst["counts"]
    assert counts["fold_naive_quarantined"] == 1
    assert counts["raw_records"] == (
        counts["accepted"] + counts["duplicates"] + counts["quarantined"]
    )
    rows = store.rows(
        conn,
        "SELECT code FROM quarantine WHERE run_id = ? AND source_row = 130",
        (run_dst["run_id"],),
    )
    assert rows and rows[0]["code"] == "DST_NAIVE_FOLD"


def test_two_occurrences_land_in_distinct_buckets(conn, run_dst):
    """同一本地 02:15 的两次经过落到两个 UTC 桶，且各自只计一次，无同桶碰撞。"""
    collisions = store.rows(
        conn,
        "SELECT sensor_id, bucket_utc, COUNT(*) AS n FROM samples"
        " WHERE run_id = ? AND in_fold = 1 GROUP BY sensor_id, bucket_utc HAVING n > 1",
        (run_dst["run_id"],),
    )
    assert collisions == []
    samples = {
        row["bucket_utc"]: row
        for row in store.rows(
            conn,
            "SELECT bucket_utc, fold_occurrence, value FROM samples"
            " WHERE run_id = ? AND sensor_id = 'TH-01' AND bucket_utc IN (?, ?)",
            (run_dst["run_id"], DST_FIRST_BUCKET, DST_SECOND_BUCKET),
        )
    }
    assert samples[DST_FIRST_BUCKET]["fold_occurrence"] == 1
    assert samples[DST_SECOND_BUCKET]["fold_occurrence"] == 2
    assert Decimal(samples[DST_FIRST_BUCKET]["value"]) != Decimal(
        samples[DST_SECOND_BUCKET]["value"]
    )


def test_fold_counts_split_evenly(conn, run_dst):
    counts = run_dst["counts"]
    assert counts["fold_buckets"] == DST["fold_buckets"]
    assert counts["fold_first"] == DST["fold_first"]
    assert counts["fold_second"] == DST["fold_second"]
    assert counts["fold_first"] + counts["fold_second"] == counts["fold_buckets"]
    assert counts["duplicates"] == 0


# —— 覆盖与缺口：回拨本身不制造双倍覆盖，也不制造缺口 ——

def test_dense_sensor_fold_windows_fully_covered(conn, run_dst):
    """TH-01 回拨跨越的 8 个 15 分钟窗口全部 15/15、OK，证明无双倍覆盖也无缺口。"""
    affected = dst_service.affected_view(conn, run_dst["run_id"])
    th = [w for w in affected["windows"] if w["sensor_id"] == "TH-01"]
    assert len(th) == DST["th_fold_windows"]
    assert all(w["observed"] == 15 and w["expected"] == 15 for w in th)
    assert all(w["state"] == "OK" for w in th)
    # 覆盖度恒为 1.000，绝不会因为两次经过而超过应有桶数。
    assert all(Decimal(w["coverage"]) == Decimal("1.000") for w in th)


def test_dense_sensor_has_no_gap_inside_fold(conn, run_dst):
    """密集传感器在回拨 UTC 跨度内没有任何 GAP；缺口只来自采样稀疏的 PM-02。"""
    affected = dst_service.affected_view(conn, run_dst["run_id"])
    assert all(g["sensor_id"] == "PM-02" for g in affected["gaps"])
    th_gaps = [g for g in affected["gaps"] if g["sensor_id"] == "TH-01"]
    assert th_gaps == []


# —— 可追溯时间轴 ——

def test_timeline_orders_occurrences_and_keeps_local_clock(conn, run_dst):
    view = dst_service.timeline_view(conn, run_dst["run_id"])
    repeated = [
        p
        for p in view["points"]
        if p["sensor_id"] == "TH-01" and p["bucket_utc"] in (DST_FIRST_BUCKET, DST_SECOND_BUCKET)
    ]
    assert len(repeated) == 2
    by_bucket = {p["bucket_utc"]: p for p in repeated}
    first = by_bucket[DST_FIRST_BUCKET]
    second = by_bucket[DST_SECOND_BUCKET]
    # 两段本地墙钟都显示 02:15，但区时与 UTC 桶不同，先后由 UTC 时刻确定。
    assert first["local_time"].endswith("02:15") and second["local_time"].endswith("02:15")
    assert first["local_offset"] == "+02:00" and second["local_offset"] == "+01:00"
    assert first["occurrence"] == 1 and second["occurrence"] == 2
    assert view["summary"]["first_occurrence"] == DST["fold_first"]
    assert view["summary"]["second_occurrence"] == DST["fold_second"]


def test_evidence_cell_carries_fold_trace(conn, run_dst):
    """网格单元格证据能追回第几次经过、本地墙钟与来源行，形成可追溯链。"""
    cell = evidence.cell(conn, run_dst["run_id"], "TH-01", DST_SECOND_BUCKET)
    assert cell["in_fold"] is True
    assert cell["fold_occurrence"] == 2
    assert cell["local_offset_minutes"] == 60
    assert cell["local_clock"].endswith("02:15")
    assert cell["source_row"] is not None


# —— 导出与正式结果按同一结论对账 ——

def test_grid_csv_marks_fold_with_same_verdict(conn, run_dst):
    _, rows = csv_out.grid_export(conn, run_dst["run_id"])
    fold_rows = [r for r in rows if r[10] == 1]
    assert len(fold_rows) == DST["fold_buckets"]
    assert {r[11] for r in fold_rows} == {1, 2}
    # 导出里的回拨桶数与批次计数、时间轴完全一致。
    assert sum(1 for r in rows if r[10] == 1) == run_dst["counts"]["fold_buckets"]


def test_manifest_carries_dst_section(conn, run_dst):
    import json

    payload = json.loads(export_service.manifest_text(conn, run_dst["run_id"]))
    assert payload["dst_fold"]["fold_buckets"] == DST["fold_buckets"]
    assert payload["dst_fold"]["first_occurrence"] == DST["fold_first"]
    assert payload["dst_fold"]["second_occurrence"] == DST["fold_second"]
    assert payload["dst_fold"]["naive_quarantined"] == 1
    # 回拨表作为第三个输入进入 sha256 摘要。
    assert len(payload["inputs"]) == 3


def test_export_summary_reports_fold(conn, run_dst):
    summary = export_service.summary(conn, run_dst["run_id"])
    assert summary["dst_fold_windows"] == 1
    assert summary["dst_fold_buckets"] == DST["fold_buckets"]


# —— 幂等重跑：不新增结果，也不丢记录 ——

def test_dst_rerun_is_idempotent(conn, run_dst):
    before_runs = store.value(conn, "SELECT COUNT(*) FROM runs", (), 0)
    before_grid = store.value(conn, "SELECT COUNT(*) FROM grid", (), 0)
    before_fold = store.value(
        conn, "SELECT COUNT(*) FROM samples WHERE in_fold = 1", (), 0
    )

    again = batch_service.execute_run(
        conn, sample="dst_fallback", offsets="dst_device_offsets", dst="berlin_fallback"
    )
    assert again["reused"] is True
    assert again["run_id"] == run_dst["run_id"]
    assert store.value(conn, "SELECT COUNT(*) FROM runs", (), 0) == before_runs
    assert store.value(conn, "SELECT COUNT(*) FROM grid", (), 0) == before_grid
    assert store.value(conn, "SELECT COUNT(*) FROM samples WHERE in_fold = 1", (), 0) == before_fold
    assert again["counts"] == run_dst["counts"]


def test_rerun_keeps_every_record(conn, run_dst):
    """重跑前后原始行、采纳、隔离数量逐一不变。"""
    batch_service.execute_run(
        conn, sample="dst_fallback", offsets="dst_device_offsets", dst="berlin_fallback"
    )
    counts = run_dst["counts"]
    assert store.value(conn, "SELECT COUNT(*) FROM samples WHERE run_id = ?", (run_dst["run_id"],), 0) == counts["accepted"]
    assert store.value(conn, "SELECT COUNT(*) FROM quarantine WHERE run_id = ?", (run_dst["run_id"],), 0) == counts["quarantined"]
    assert store.value(conn, "SELECT COUNT(*) FROM dst_windows WHERE run_id = ?", (run_dst["run_id"],), 0) == 1


# —— 发布失败保留旧版 ——

def test_failed_publish_keeps_previous_run_intact(conn, run_dst, monkeypatch):
    """新批次落库中途抛错时事务回滚：旧批次仍在、计数不变、无半张表。"""
    from app.modules.m6_batches import service as m6

    original = store.insert_many
    calls = {"n": 0}

    def flaky(conn_, sql, records):
        # 在写网格时炸掉，模拟发布阶段失败（此前 offsets/samples 已写入，须一并回滚）。
        if "INSERT INTO grid" in sql:
            calls["n"] += 1
            raise RuntimeError("simulated publish failure")
        return original(conn_, sql, records)

    # 强制产生一个新批次键，确保走到落库事务而不是命中幂等缓存。
    monkeypatch.setattr(m6.manifest, "run_key", lambda *a, **k: "forced-failure-key")
    monkeypatch.setattr(store, "insert_many", flaky)
    with pytest.raises(RuntimeError):
        batch_service.execute_run(
            conn, sample="dst_fallback", offsets="dst_device_offsets", dst="berlin_fallback"
        )
    monkeypatch.undo()

    # 旧批次仍是唯一批次，五账与网格点数完好；失败批次没留下任何半张表。
    assert store.value(conn, "SELECT COUNT(*) FROM runs", (), 0) == 1
    view = batch_service.reconcile_view(conn, run_dst["run_id"])
    assert view["balanced"] is True
    assert store.value(conn, "SELECT COUNT(*) FROM grid WHERE run_id = ?", (run_dst["run_id"],), 0) == DST["expected_buckets"]
    assert store.value(conn, "SELECT COUNT(*) FROM dst_windows", (), 0) == 1
    assert calls["n"] == 1


# —— 无回拨表的历史批次保持不变 ——

def test_run_without_dst_table_has_no_fold(conn, run_main):
    """不传回拨表时不产生任何回拨标记，键与结论与旧规则完全一致。"""
    assert run_main["counts"].get("fold_buckets", 0) == 0
    rows = store.rows(
        conn, "SELECT COUNT(*) AS n FROM samples WHERE run_id = ? AND in_fold = 1",
        (run_main["run_id"],),
    )
    assert rows[0]["n"] == 0
    assert store.rows(conn, "SELECT COUNT(*) AS n FROM dst_windows", ())[0]["n"] == 0


def test_unknown_dst_table_rejected(conn):
    with pytest.raises(InputError) as error:
        batch_service.execute_run(
            conn, sample="dst_fallback", offsets="dst_device_offsets", dst="nope"
        )
    assert error.value.code == "DST_TABLE_UNKNOWN"
