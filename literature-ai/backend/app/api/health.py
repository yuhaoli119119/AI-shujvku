from fastapi import APIRouter
import os

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "git_commit": os.environ.get("LITAI_GIT_COMMIT")}
