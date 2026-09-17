"""Intelligence event browsing and timelines."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import EventStatus
from ..repositories import events as events_repo
from ..repositories import modules as modules_repo
from ..web import redirect, render

router = APIRouter(prefix="/events")


@router.get("")
def event_list(
    request: Request,
    q: str = "",
    status: str = "",
    module_id: int = 0,
    session: Session = Depends(get_db),
):
    """Searchable list of tracked events."""
    items = events_repo.list_events(
        session,
        status=status or None,
        module_id=module_id or None,
        term=q,
        limit=200,
    )
    return render(
        request,
        "event_list.html",
        {
            "nav": "events",
            "events": items,
            "q": q,
            "status": status,
            "module_id": module_id,
            "modules": modules_repo.list_modules(session, include_archived=True),
            "statuses": EventStatus.ALL,
        },
    )


@router.get("/{event_id}")
def event_detail(event_id: int, request: Request, session: Session = Depends(get_db)):
    """Full timeline: every observation, its metrics and its sources."""
    event = events_repo.get_event(session, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return render(request, "event_detail.html", {"nav": "events", "event": event})


@router.post("/{event_id}/status")
def event_status(
    event_id: int, status: str = Form(...), session: Session = Depends(get_db)
):
    """Move an event between active / watching / resolved / archived."""
    event = events_repo.get_event(session, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    if status not in EventStatus.ALL:
        return redirect(f"/events/{event_id}", "无效的状态值。", "error")
    event.status = status
    session.flush()
    return redirect(f"/events/{event_id}", f"事件状态已更新为 {status}。", "ok")
