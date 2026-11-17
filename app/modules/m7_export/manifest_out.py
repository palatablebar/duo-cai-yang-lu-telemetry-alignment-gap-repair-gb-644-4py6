"""运行清单导出：run_id、输入 sha256、规则版本、参数、计数与合计。"""

from __future__ import annotations

import json

from app.modules.m6_batches import history
from app.modules.m8_dst import service as dst_service
from app.errors import NotFoundError


def build(conn, run_id: str) -> dict:
    payload = history.manifest_of(conn, run_id)
    if payload is None:
        raise NotFoundError("批次不存在", code="RUN_NOT_FOUND", detail={"run_id": run_id})
    dst_windows = dst_service.dst_windows_of(conn, run_id)
    if dst_windows:
        payload["dst_fold"] = {
            "windows": dst_windows,
            "fold_buckets": payload.get("counts", {}).get("fold_buckets", 0),
            "first_occurrence": payload.get("counts", {}).get("fold_first", 0),
            "second_occurrence": payload.get("counts", {}).get("fold_second", 0),
            "naive_quarantined": payload.get("counts", {}).get("fold_naive_quarantined", 0),
        }
    return payload


def to_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def file_name(run_id: str) -> str:
    return f"manifest-{run_id}.json"


def compact(payload: dict) -> dict:
    return {
        "run_id": payload.get("run_id"),
        "rule_version": payload.get("rule_version"),
        "created_at": payload.get("created_at"),
        "inputs": payload.get("inputs", []),
        "counts": payload.get("counts", {}),
        "totals": payload.get("totals", {}),
    }
