from celery import Celery

from app.config import get_settings
from app.services.workflow_jobs import WORKFLOW_QUEUE_DEFAULT

# Database schema bootstrap is owned by the backend lifespan. Compose waits for
# that service to become healthy before starting workers, so importing the
# Celery app must never execute DDL against a live database.
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
