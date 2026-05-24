from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.corrections import router as corrections_router
from app.api.external_analysis import router as external_analysis_router
from app.api.evidence import router as evidence_router
from app.api.extraction import router as extraction_router
from app.api.health import router as health_router
from app.api.libraries import router as libraries_router
from app.api.papers import router as papers_router
from app.api.references import router as references_router
from app.api.retrieval import router as retrieval_router
from app.api.settings import router as settings_router
from app.api.system import router as system_router
from app.api.writer import router as writer_router
from app.config import get_settings
from app.db.session import init_db
from app.mcp import mcp_http_app, mcp_server
from app.mcp.auth import enforce_mcp_auth


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    # 确保默认库注册表和目录结构存在
    from app.services.library_manager import LibraryManager
    mgr = LibraryManager()
    # 使用默认库的 DB 初始化（LibraryManager._ensure_registry 已处理）
    active = mgr.get_active_library()
    if active:
        try:
            mgr.activate_library(active.name)
        except Exception:
            from pathlib import Path as _Path
            db_path = _Path(active.root_path) / "database.sqlite"
            init_db(f"sqlite:///{db_path.as_posix()}")
    else:
        init_db(settings.database_url)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(mcp_server.session_manager.run())
        yield


app = FastAPI(
    title="Literature AI Backend",
    version="0.1.0",
    lifespan=lifespan,
)

app.middleware("http")(enforce_mcp_auth)
app.include_router(health_router, prefix="/api")
app.include_router(system_router, prefix="/api/system", tags=["system"])
app.include_router(libraries_router, prefix="/api/libraries", tags=["libraries"])
app.include_router(papers_router, prefix="/api/papers", tags=["papers"])
app.include_router(references_router, prefix="/api/papers", tags=["references"])
app.include_router(writer_router, prefix="/api/writer", tags=["writer"])
app.include_router(corrections_router, prefix="/api/corrections", tags=["corrections"])
app.include_router(external_analysis_router, prefix="/api/external-analysis", tags=["external-analysis"])
app.include_router(settings_router, prefix="/api/settings", tags=["settings"])
app.include_router(retrieval_router, prefix="/api/retrieval", tags=["retrieval"])
app.include_router(evidence_router, prefix="/api/evidence", tags=["evidence"])
app.include_router(extraction_router, prefix="/api/extraction", tags=["extraction"])
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
