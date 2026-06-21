import logging

from celery import Celery

from app.config import get_settings
from app.services.workflow_jobs import WORKFLOW_QUEUE_DEFAULT
from app.utils.active_database import activate_active_library_database

logger = logging.getLogger(__name__)


def bootstrap_worker_database() -> None:
    try:
        activate_active_library_database()
    except Exception as exc:
        # When multiple workers start together, one may race the startup-time DB
        # bootstrap. The live backend already initializes the schema, so workers
        # can continue and rely on runtime sessions afterward.
        logger.warning("Worker bootstrap DB activation skipped after startup race: %s", exc)


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
    task_default_queue=WORKFLOW_QUEUE_DEFAULT,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_create_missing_queues=True,
)

celery_app.autodiscover_tasks(["app.workers"])
