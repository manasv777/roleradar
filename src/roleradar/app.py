"""FastAPI application."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from roleradar import db as database
from roleradar import scheduler
from roleradar.routers.scout import router as scout_router
from roleradar.scout import runner
from roleradar.settings import settings

logger = logging.getLogger(__name__)

# Playwright drives Chromium as a subprocess, which needs the proactor loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=settings.log_level)
    database.db.initialize(settings.db_path)
    scheduler.start()
    try:
        yield
    finally:
        # Order matters: an in-flight run still writes to the database, so it
        # has to be stopped before the connection pool goes away.
        await scheduler.stop()
        await runner.cancel_scout_run()
        from roleradar.scout.browser import close_shared_browser

        await close_shared_browser()
        await database.db.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="roleradar",
        version="0.1.0",
        summary="Scout internship and new-grad roles in the fields you pick.",
        lifespan=lifespan,
    )
    # Local-only tool: the UI is served from a dev server on another port.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:3100",
            "http://127.0.0.1:3100",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(scout_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"name": "roleradar", "version": "0.1.0", "docs": "/docs"}

    return app


app = create_app()
