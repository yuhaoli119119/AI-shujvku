from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.corrections import router as corrections_router
from app.api.external_analysis import router as external_analysis_router
from app.api.evidence import router as evidence_router
from app.api.extraction import router as extraction_router
from app.api.health import router as health_router
from app.api.impact_metadata import router as impact_metadata_router
from app.api.jobs import router as jobs_router
from app.api.libraries import router as libraries_router
from app.api.library_filter import router as library_filter_router
from app.api.papers import router as papers_router
from app.api.references import router as references_router
from app.api.retrieval import router as retrieval_router
from app.api.settings import router as settings_router
from app.api.system import router as system_router
from app.api.writing import router as writing_router
from app.api.writer import router as writer_router
from app.api.verification import router as verification_router
from app.api.workbench import router as workbench_router
from app.config import get_settings
from app.mcp import mcp_http_app, mcp_server
from app.mcp.auth import enforce_mcp_auth
from app.utils.active_database import activate_active_library_database


@asynccontextmanager
async def lifespan(_: FastAPI):
    info = activate_active_library_database()

    # Log the actual active database after startup
    startup_logger = logging.getLogger("app.startup")
    startup_logger.info(
        "Database source-of-truth: kind=%s, library=%s, configured=%s, effective=%s",
        info["db_kind"],
        info["active_library"] or "(none)",
        info["db_url_masked"],
        info.get("effective_db_path"),
    )

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(mcp_server.session_manager.run())
        yield


app = FastAPI(
    title="Literature AI Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.middleware("http")(enforce_mcp_auth)


@app.middleware("http")
async def no_cache_frontend_assets(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith(("/pages/", "/shared/")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.include_router(health_router, prefix="/api")
app.include_router(system_router, prefix="/api/system", tags=["system"])
app.include_router(libraries_router, prefix="/api/libraries", tags=["libraries"])
app.include_router(impact_metadata_router, prefix="/api/library/impact-metadata", tags=["impact-metadata"])
app.include_router(library_filter_router, prefix="/api/library/papers", tags=["library-filter"])
app.include_router(jobs_router, prefix="/api/jobs", tags=["jobs"])
app.include_router(papers_router, prefix="/api/papers", tags=["papers"])
app.include_router(references_router, prefix="/api/papers", tags=["references"])
app.include_router(writing_router, prefix="/api/writing", tags=["writing"])
app.include_router(writer_router, prefix="/api/writer", tags=["writer"])
app.include_router(verification_router, prefix="/api/reviews", tags=["verification"])
app.include_router(corrections_router, prefix="/api/corrections", tags=["corrections"])
app.include_router(external_analysis_router, prefix="/api/external-analysis", tags=["external-analysis"])
app.include_router(settings_router, prefix="/api/settings", tags=["settings"])
app.include_router(retrieval_router, prefix="/api/retrieval", tags=["retrieval"])
app.include_router(evidence_router, prefix="/api/evidence", tags=["evidence"])
app.include_router(extraction_router, prefix="/api/extraction", tags=["extraction"])
app.include_router(workbench_router, prefix="/api/workbench", tags=["workbench"])
app.mount("/mcp", mcp_http_app)

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
async def root() -> dict[str, str]:
    return {"message": "Literature AI backend is running"}
