"""M8 回拨时间轴的入口：时间轴页、时间轴 JSON、受影响窗口与缺口。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app import errors, store, web
from app.modules.m6_batches import history

from . import service

router = APIRouter(tags=["m8 回拨时间轴"])


@router.get("/dst", response_class=HTMLResponse)
def dst_page(request: Request):
    conn = store.session()
    try:
        run = history.latest(conn)
        view = service.timeline_view(conn, run["run_id"]) if run else None
        affected = service.affected_view(conn, run["run_id"]) if run else None
    finally:
        conn.close()
    return web.render(request, "dst.html", "dst", view=view, affected=affected)


@router.get("/api/dst/timeline")
def dst_timeline(run_id: str | None = Query(default=None, max_length=64)):
    conn = store.session()
    try:
        target = _resolve(conn, run_id)
        return service.timeline_view(conn, target)
    finally:
        conn.close()


@router.get("/api/dst/affected")
def dst_affected(run_id: str | None = Query(default=None, max_length=64)):
    conn = store.session()
    try:
        target = _resolve(conn, run_id)
        return service.affected_view(conn, target)
    finally:
        conn.close()


def _resolve(conn, run_id: str) -> str:
    run = history.get(conn, run_id) if run_id else history.latest(conn)
    if run is None:
        raise errors.NotFoundError("还没有批次", code="NO_RUN")
    return run["run_id"]
