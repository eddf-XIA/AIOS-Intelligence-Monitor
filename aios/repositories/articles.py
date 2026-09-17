"""Persistence and lookup for collected source documents."""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import RawArticle
from ..timeutil import utcnow


def get_by_url_hash(session: Session, url_hash: str) -> Optional[RawArticle]:
    return session.scalar(select(RawArticle).where(RawArticle.url_hash == url_hash))


def get_by_content_hash(session: Session, content_hash: str) -> Optional[RawArticle]:
    if not content_hash:
        return None
    return session.scalar(
        select(RawArticle).where(RawArticle.content_hash == content_hash).limit(1)
    )


def upsert(session: Session, payload: dict) -> tuple[RawArticle, bool]:
    """Insert an article, or refresh the existing row for the same URL.

    Returns ``(article, created)``. Re-seeing a URL updates ``last_seen_at`` and
    fills in a body we did not have before, but never duplicates the row - that
    is what keeps the table from growing without bound across daily runs.
    """
    url_hash = payload["url_hash"]
    existing = get_by_url_hash(session, url_hash)
    if existing is not None:
        existing.last_seen_at = utcnow()
        if payload.get("body_text") and not existing.body_text:
            existing.body_text = payload["body_text"]
            existing.content_hash = payload.get("content_hash") or existing.content_hash
        if payload.get("run_id"):
            existing.run_id = payload["run_id"]
        if payload.get("trust_score", 0) > (existing.trust_score or 0):
            existing.trust_score = payload["trust_score"]
        if payload.get("topic_id") and not existing.topic_id:
            existing.topic_id = payload["topic_id"]
            existing.module_id = payload.get("module_id")
        session.flush()
        return existing, False

    article = RawArticle(**payload)
    session.add(article)
    session.flush()
    return article, True


def list_for_run(session: Session, run_id: int, limit: int = 500) -> list[RawArticle]:
    stmt = (
        select(RawArticle)
        .where(RawArticle.run_id == run_id)
        .order_by(RawArticle.trust_score.desc(), RawArticle.id)
        .limit(limit)
    )
    return list(session.scalars(stmt))


def count_for_run(session: Session, run_id: int) -> int:
    return session.scalar(
        select(func.count()).select_from(RawArticle).where(RawArticle.run_id == run_id)
    ) or 0


def search(session: Session, term: str, limit: int = 50) -> list[RawArticle]:
    like = f"%{term.strip()}%"
    stmt = (
        select(RawArticle)
        .where(
            RawArticle.title.like(like)
            | RawArticle.source.like(like)
            | RawArticle.domain.like(like)
        )
        .order_by(RawArticle.collected_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def recent(session: Session, days: int = 7, limit: int = 200) -> list[RawArticle]:
    cutoff = utcnow() - dt.timedelta(days=days)
    stmt = (
        select(RawArticle)
        .where(RawArticle.collected_at >= cutoff)
        .order_by(RawArticle.collected_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def total_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(RawArticle)) or 0
