"""Settings → 网络与数据源.

Network mode, per-collector enable/disable, user RSS feeds, transport
(system / direct / custom proxy) and the 连接诊断 button.

Nothing here tells the user to obtain connectivity in any particular way. The
product's job is to reason about which sources are reachable and behave
sensibly when some are not.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import network_service, settings_service
from ..services.collection_planner import reset_shared_cache
from ..web import partial, redirect

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings/network")


def _checkbox(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "on", "yes"}


@router.post("/mode")
def save_mode(mode: str = Form("auto"), session: Session = Depends(get_db)):
    """Choose 自动 / 中国大陆网络 / 国际网络."""
    try:
        chosen = network_service.set_network_mode(session, mode)
    except ValueError as exc:
        return redirect("/settings", str(exc), "error")
    label = network_service.MODE_LABELS[chosen]
    return redirect("/settings", f"网络模式已设为「{label}」。", "ok")


@router.post("/collectors")
def save_collectors(
    collector_gdelt_enabled: str = Form(None),
    collector_google_news_enabled: str = Form(None),
    collector_rss_enabled: str = Form(None),
    gdelt_min_interval_seconds: str = Form("2.0"),
    collection_cache_ttl_minutes: int = Form(30),
    network_precheck_enabled: str = Form(None),
    session: Session = Depends(get_db),
):
    """Enable/disable sources and tune how politely they are called."""
    try:
        interval = max(0.0, min(30.0, float(gdelt_min_interval_seconds)))
    except (TypeError, ValueError):
        return redirect("/settings", "GDELT 最小请求间隔必须是数字。", "error")

    ttl = max(0, min(60, int(collection_cache_ttl_minutes)))

    enabled = {
        "collector_gdelt_enabled": _checkbox(collector_gdelt_enabled),
        "collector_google_news_enabled": _checkbox(collector_google_news_enabled),
        "collector_rss_enabled": _checkbox(collector_rss_enabled),
    }
    if not any(enabled.values()):
        return redirect("/settings", "至少需要启用一个数据源。", "error")

    settings_service.set_many(
        session,
        {
            **enabled,
            "gdelt_min_interval_seconds": interval,
            "collection_cache_ttl_minutes": ttl,
            "network_precheck_enabled": _checkbox(network_precheck_enabled),
        },
    )
    # The cache TTL changed; drop what was stored under the old one.
    reset_shared_cache()
    return redirect("/settings", "数据源设置已保存。", "ok")


@router.post("/transport")
def save_transport(
    connection_mode: str = Form("system"),
    http_proxy: str = Form(""),
    https_proxy: str = Form(""),
    session: Session = Depends(get_db),
):
    """Save the connection mode. Any proxy password goes to the OS keyring."""
    try:
        transport = network_service.save_transport(
            session, connection_mode, http_proxy, https_proxy
        )
    except ValueError as exc:
        return redirect("/settings", str(exc), "error")
    return redirect("/settings", f"连接方式已保存：{transport.describe()}", "ok")


@router.post("/feeds")
def add_feed(
    url: str = Form(...),
    title: str = Form(""),
    session: Session = Depends(get_db),
):
    """Add an RSS/Atom feed - the source type that needs no search engine."""
    try:
        feed = network_service.add_feed(session, url, title)
    except ValueError as exc:
        return redirect("/settings", str(exc), "error")
    return redirect("/settings", f"已添加订阅源 {feed.title or feed.domain}。", "ok")


@router.post("/feeds/{feed_id}/toggle")
def toggle_feed(feed_id: int, session: Session = Depends(get_db)):
    feed = network_service.toggle_feed(session, feed_id)
    if feed is None:
        return redirect("/settings", "订阅源不存在。", "error")
    return redirect("/settings", "已启用订阅源。" if feed.enabled else "已停用订阅源。", "ok")


@router.post("/feeds/{feed_id}/delete")
def delete_feed(feed_id: int, session: Session = Depends(get_db)):
    if not network_service.delete_feed(session, feed_id):
        return redirect("/settings", "订阅源不存在。", "error")
    return redirect("/settings", "已删除订阅源。", "ok")


@router.post("/diagnose")
def diagnose(request: Request, session: Session = Depends(get_db)):
    """Run 连接诊断 and render the result fragment.

    Output is deliberately free of credentials: probe details carry status
    codes and error classes, never headers, keys or proxy passwords.
    """
    try:
        probes = network_service.run_diagnostics(session)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Diagnostics failed")
        return partial(
            request,
            "partials/network_diagnostics.html",
            {"error": f"连接诊断未完成：{type(exc).__name__}", "probes": []},
        )

    return partial(
        request,
        "partials/network_diagnostics.html",
        {"probes": [p.as_dict() for p in probes if p is not None]},
    )
