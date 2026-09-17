"""FastAPI application factory.

Startup order matters: the database must exist and be seeded before the
scheduler reads its settings, and stale runs left by a crashed process must be
cleared before the run manager accepts new work.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import __app_name__, __version__
from .config import get_paths
from .database import init_db
from .logging_setup import configure_logging
from .services.run_manager import manager, reset_stale_runs
from .services.scheduler import scheduler
from .web import STATIC_DIR, render

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the database, recover stale runs, start the scheduler."""
    configure_logging()
    init_db(get_paths())
    reset_stale_runs()
    scheduler.start()
    logger.info("%s %s ready", __app_name__, __version__)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        manager.shutdown(wait=False)
        logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """Build the application with every router mounted."""
    app = FastAPI(
        title=__app_name__,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from .routers import (
        compare, config_ai, dashboard, events, monitoring, network,
        providers, reports, runs, settings,
    )

    app.include_router(dashboard.router)
    # Registered before the monitoring CRUD router so the /monitoring/ai/*
    # paths are matched by their own handlers.
    app.include_router(config_ai.router)
    app.include_router(monitoring.router)
    app.include_router(reports.router)
    app.include_router(compare.router)
    app.include_router(events.router)
    app.include_router(runs.router)
    app.include_router(providers.router)
    app.include_router(network.router)
    app.include_router(settings.router)

    @app.exception_handler(404)
    async def not_found(request: Request, exc):  # noqa: ANN001
        return render(request, "error.html", {"code": 404, "message": "页面不存在。"}, 404)

    @app.exception_handler(500)
    async def server_error(request: Request, exc):  # noqa: ANN001
        logger.exception("Unhandled error on %s", request.url.path)
        return render(
            request,
            "error.html",
            {"code": 500, "message": "服务器内部错误，请查看日志。"},
            500,
        )

    @app.get("/healthz", response_class=HTMLResponse, include_in_schema=False)
    async def healthz() -> HTMLResponse:
        return HTMLResponse("ok")

    return app


app = create_app()
