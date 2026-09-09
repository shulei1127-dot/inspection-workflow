"""FastAPI application with lifespan and scheduler."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from apps.api.routers import change_logs, email_tool, health, inspection_library, monitor, oauth, review, sales_confirm, statistics, sync, task_logs, triggers, visit, work_orders
from apps.api.routers import ws as websocket
from core.config import get_settings
from core.db import create_all
from core.logging import configure_logging
from apps.api.auth_middleware import AuthMiddleware

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()

    # Ensure database tables exist
    create_all()

    # Clean up any stale "running" sync logs from previous crashes
    from services.sync_service import cleanup_stale_running_logs
    from services.pts_closure_service import recover_stalled_closure_wip
    from core.db import SessionLocal
    with SessionLocal() as db:
        cleanup_stale_running_logs(db)
        recovered = recover_stalled_closure_wip(db)
        if recovered:
            import logging
            logging.getLogger(__name__).info("Recovered %d stalled closure work orders", recovered)

    # Start scheduler
    scheduler = None
    if settings.scheduler_enabled:
        from apscheduler.schedulers.background import BackgroundScheduler
        from scheduler.jobs import register_jobs

        scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)
        register_jobs(scheduler)
        scheduler.start()
        app.state.scheduler = scheduler

    yield

    # Shutdown: give running jobs up to 30 seconds to finish
    if scheduler:
        scheduler.shutdown(wait=True)


settings = get_settings()
app = FastAPI(
    title="inspection_workflow",
    description="巡检工单执行流程自动化",
    version="1.0.0",
    lifespan=lifespan,
    debug=settings.log_level == "DEBUG",
)

app.add_middleware(AuthMiddleware)

# Register API routers
app.include_router(health.router)
app.include_router(sync.router)
app.include_router(monitor.router)
app.include_router(triggers.router)
app.include_router(work_orders.router)
app.include_router(statistics.router)
app.include_router(email_tool.router)
app.include_router(websocket.router)
app.include_router(review.router)
app.include_router(sales_confirm.router)
app.include_router(visit.router)
app.include_router(change_logs.router)
app.include_router(inspection_library.router)
app.include_router(task_logs.router)
app.include_router(oauth.router)

# Serve frontend static files (built Vue app)
if STATIC_DIR.is_dir() and (STATIC_DIR / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def serve_spa(request: Request, path: str):
        """Serve the Vue SPA: try static file first, then fall back to index.html."""
        # Try to serve a real static file
        file_path = STATIC_DIR / path
        if path and file_path.is_file():
            return FileResponse(str(file_path))
        # Fall back to index.html for SPA routing
        index = STATIC_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        return HTMLResponse("<h1>Frontend not built</h1><p>Run <code>cd frontend && npm run build</code></p>", status_code=404)
