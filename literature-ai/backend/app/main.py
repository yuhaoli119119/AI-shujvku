from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
import logging

import anyio
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.analysis_readonly import router as analysis_readonly_router
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.website_jobs import router as website_jobs_router
from app.api.website_evidence import router as website_evidence_router
from app.api.impact_metadata import router as impact_metadata_router
from app.api.libraries import router as libraries_router
from app.api.library_filter import router as library_filter_router
from app.api.module_locks import router as module_locks_router
from app.api.papers import router as papers_router
from app.api.papers.listing import list_papers
from app.schemas.api import PaperListItemResponse
from app.api.references import router as references_router
from app.api.rebuild import router as rebuild_router
from app.api.share import router as share_router
from app.api.settings import (
    apply_persisted_settings_to_runtime,
    router as settings_router,
)
from app.api.system import router as system_router
from app.config import get_settings
from app.db.session import session_scope
from app.oauth import router as oauth_router
from app.security.exports import enforce_export_boundary
from app.security.share import enforce_share_protection
from app.utils.active_database import activate_active_library_database

@asynccontextmanager
async def lifespan(_: FastAPI):
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = 200

    info = activate_active_library_database()
    startup_logger = logging.getLogger("app.startup")
    startup_logger.info(
        "Database source-of-truth: kind=%s, library=%s, configured=%s",
        info["db_kind"],
        info["active_library"] or "(none)",
        info["db_url_masked"],
    )
    try:
        apply_persisted_settings_to_runtime()
    except Exception:
        startup_logger.exception("Failed to apply persisted runtime settings during startup")
    settings = get_settings()
    # Old workflow_jobs cleanup removed
    async with AsyncExitStack() as stack:
        yield

app = FastAPI(
    title="Literature AI Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.middleware("http")(enforce_export_boundary)
app.middleware("http")(enforce_share_protection)

@app.middleware("http")
async def no_cache_frontend_assets(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith(("/pages/", "/shared/")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.include_router(analysis_readonly_router, prefix="/api", tags=["analysis-readonly"])
app.include_router(health_router, prefix="/api")
app.include_router(website_jobs_router, prefix="/api/jobs", tags=["jobs"])
app.include_router(website_evidence_router, prefix="/api/evidence", tags=["evidence"])
app.include_router(auth_router, prefix="/api")
app.include_router(system_router, prefix="/api/system", tags=["system"])
app.include_router(libraries_router, prefix="/api/libraries", tags=["libraries"])
app.include_router(impact_metadata_router, prefix="/api/library/impact-metadata", tags=["impact-metadata"])
app.include_router(library_filter_router, prefix="/api/library/papers", tags=["library-filter"])
app.include_router(module_locks_router, prefix="/api/module-locks", tags=["module-locks"])
app.add_api_route("/api/papers", list_papers, methods=["GET"], response_model=list[PaperListItemResponse], response_model_exclude_unset=True)
app.include_router(papers_router, prefix="/api/papers", tags=["papers"])
app.include_router(rebuild_router, prefix="/api/rebuild", tags=["rebuild"])
app.include_router(references_router, prefix="/api/papers", tags=["references"])
app.include_router(settings_router, prefix="/api/settings", tags=["settings"])
app.include_router(share_router, prefix="/api")
app.include_router(oauth_router)

frontend_dir = Path("/frontend")
if not frontend_dir.exists():
    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"

frontend_pages_dir = frontend_dir / "pages"
if frontend_pages_dir.exists():
    app.mount("/pages", StaticFiles(directory=str(frontend_pages_dir), html=True), name="pages")

frontend_shared_dir = frontend_dir / "shared"
if frontend_shared_dir.exists():
    app.mount("/shared", StaticFiles(directory=str(frontend_shared_dir)), name="shared")


@app.get("/")
async def root():
    """Send browsers landing on the gateway root to the workbench.

    The owner gateway proxies ``/`` here (after its session check) so the
    redirect happens in the content phase; that is what lets nginx gate the
    root path with ``auth_request`` (a rewrite-phase ``return`` would run
    before the access check).
    """
    return RedirectResponse(url="/pages/literature_library/index.html", status_code=302)
