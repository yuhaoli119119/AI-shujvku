from celery import Celery

from app.config import get_settings
from app.db.session import init_db
from app.services.library_manager import LibraryManager


def bootstrap_worker_database() -> None:
    settings = get_settings()
    manager = LibraryManager()
    active = manager.get_active_library()
    if active:
        try:
            manager.activate_library(active.name)
            return
        except Exception:
            pass
    init_db(settings.database_url)


bootstrap_worker_database()
settings = get_settings()

celery_app = Celery(
    "literature_ai",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
)

celery_app.autodiscover_tasks(["app.workers"])
